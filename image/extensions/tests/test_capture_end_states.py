"""The host side of the capture end-state experiments: the supervisor
channel, the order of one file-size-limit experiment, and the kept-running
loopback judge. Nothing here runs Docker, tcpdump or a file-size limit."""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import capture_experiment as experiment
import extension_check

HEADER = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, extension_check.CAPTURE_SNAPLEN, 1)
RECORD = struct.pack("<IIII", 1, 2, 60, 60) + bytes(range(60))
PREFIX = HEADER + RECORD * 2


class PipeProcess:
    """A stand-in for the `docker exec -i` process: its standard output is a
    real pipe that the test writes to, its standard input a buffer."""

    def __init__(self, argv, **_):
        self.argv = argv
        read, self.write_fd = os.pipe()
        self.stdout = os.fdopen(read, "rb", buffering=0)
        self.stdin = mock.MagicMock()
        self.sent = []
        self.stdin.write.side_effect = lambda data: self.sent.append(json.loads(data))
        self.returncode, self.killed = None, False

    def emit(self, data):
        os.write(self.write_fd, data)

    def hang_up(self):
        os.close(self.write_fd)

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed, self.returncode = True, -9

    def wait(self):
        return self.returncode


class ChannelTests(unittest.TestCase):
    def channel(self):
        processes = []

        def popen(argv, **options):
            processes.append(PipeProcess(argv, **options))
            return processes[-1]

        channel = extension_check.SupervisorChannel("capturer", {"argv": ["tcpdump"], "pcap": "/tmp/x.pcap",
                                                                 "sigxfsz": "default"}, popen=popen)
        self.addCleanup(lambda: processes[0].stdout.close())
        return channel, processes[0]

    def test_it_runs_the_supervisor_through_docker_exec(self):
        channel, process = self.channel()
        self.assertEqual(process.argv[:4], ["docker", "exec", "-i", "capturer"])
        self.assertEqual(process.argv[4:6], ["python3", "-c"])
        channel.send({"op": "wait_exit", "timeout": 5})
        self.assertEqual(process.sent, [{"op": "wait_exit", "timeout": 5}])

    def test_an_event_counts_only_once_its_newline_arrived(self):
        channel, process = self.channel()
        process.emit(b'{"op": "limit", "status": "app')
        with self.assertRaises(extension_check.CheckError):
            channel.event(0.2)
        process.emit(b'lied"}\n')
        self.assertEqual(channel.event(1), {"op": "limit", "status": "applied"})

    def test_coalesced_events_are_read_one_at_a_time(self):
        channel, process = self.channel()
        process.emit(b'{"op": "a"}\n{"op": "b"}\n')
        self.assertEqual([channel.event(1)["op"], channel.event(1)["op"]], ["a", "b"])

    def test_a_disconnect_or_a_malformed_line_is_never_an_event(self):
        for data, hang_up in ((b"", True), (b'{"op": "limit"', True), (b"not json\n", False), (b"[1]\n", False)):
            with self.subTest(data=data):
                channel, process = self.channel()
                process.emit(data)
                if hang_up:
                    process.hang_up()
                with self.assertRaises(extension_check.CheckError):
                    channel.event(1)

    def test_an_overlong_line_is_refused(self):
        channel, process = self.channel()
        writer = threading.Thread(target=lambda: process.emit(b"x" * (extension_check.CHANNEL_LINE_LIMIT + 10)))
        writer.start()
        with self.assertRaises(extension_check.CheckError):
            channel.event(5)
        writer.join()

    def test_finish_returns_every_line_and_close_ends_the_exec(self):
        channel, process = self.channel()
        process.emit(b'{"op": "a"}\n')
        channel.event(1)
        process.emit(b'{"schema": "s"}\n')
        process.hang_up()
        self.assertEqual(channel.finish(1), '{"op": "a"}\n{"schema": "s"}\n')
        channel.close()
        self.assertTrue(process.killed)


class ScriptedChannel:
    """A channel that answers each command from a script and records the order."""

    def __init__(self, log, answers):
        self.log, self.answers, self.closed = log, answers, False

    def __call__(self, container, config):
        self.log.append(("start", config["sigxfsz"]))
        return self

    def send(self, command):
        self.log.append(("send", command["op"]))
        self.pending = self.answers.get(command["op"])

    def event(self, timeout=30):
        answer = self.pending
        if isinstance(answer, Exception):
            raise answer
        return answer

    def finish(self, timeout=30):
        self.log.append(("finish",))
        return "receipt\n"

    def close(self):
        self.closed = True


def applied(prefix, extra, sigxfsz="default"):
    return {"op": "limit", "status": "applied", "prefix": prefix, "extra": extra, "bytes": prefix + extra,
            "before": [-1, -1], "after": [prefix + extra, prefix + extra], "own_before": [-1, -1],
            "own_after": [-1, -1], "size_before": prefix, "size_after": prefix,
            "prefix_sha256": "0" * 64, "sigxfsz": experiment.OBSERVED_DISPOSITION[sigxfsz]}


class LimitExperimentTests(unittest.TestCase):
    def run_experiment(self, ack, cut="header", prefix=PREFIX):
        log = []
        answers = {"wait_stderr": {"op": "wait_stderr", "seen": True}, "limit": ack,
                   "wait_exit": {"op": "wait_exit", "exited": True}}
        channel = ScriptedChannel(log, answers)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        def exchange(peer, target, ports, output):
            log.append(("exchange", len(ports)))
            return [{"port": port} for port in ports]

        assessment = {"verdict": "observed", "termination": "killed_by_SIGXFSZ", "cut_record": cut}
        with mock.patch.object(extension_check, "exchange", exchange), \
                mock.patch.object(extension_check, "stable_prefix", lambda *a: log.append(("prefix",)) or prefix), \
                mock.patch.object(extension_check, "retrieve", lambda *a: prefix + b"\x00" * 8), \
                mock.patch.object(experiment, "read_receipt", lambda text: {"schema": experiment.SCHEMA}), \
                mock.patch.object(experiment, "assess", lambda receipt, data: assessment):
            record = extension_check.limit_experiment({"capturer": "c", "peer": "p"}, "eth0", "10.0.0.3", "10.0.0.2",
                                                      directory.name, Path(directory.name), cut, channel=channel)
        return record, log, channel

    def test_later_traffic_follows_only_an_applied_limit(self):
        spec = extension_check.LIMIT_EXPERIMENTS["header"]
        record, log, channel = self.run_experiment(applied(len(PREFIX), spec["extra"]))
        self.assertEqual(record["status"], "observed", record.get("error"))
        self.assertEqual(log, [("start", "default"), ("send", "wait_stderr"), ("exchange", 1), ("prefix",),
                               ("send", "limit"), ("exchange", 2), ("send", "wait_exit"), ("finish",)])
        self.assertTrue(channel.closed)
        self.assertFalse(record["gating"])

    def test_an_ambiguous_acknowledgement_releases_nothing_and_sends_no_second_limit(self):
        extra = extension_check.LIMIT_EXPERIMENTS["header"]["extra"]
        for name, ack in (("refused", {"op": "limit", "status": "refused", "error": "the file is not exactly the prefix"}),
                          ("readback mismatch", dict(applied(len(PREFIX), extra), after=[-1, -1])),
                          ("wrong disposition", dict(applied(len(PREFIX), extra), sigxfsz="ignored")),
                          ("another event", {"op": "wait_exit", "exited": False}),
                          ("disconnected", extension_check.CheckError("The supervisor disconnected", "channel"))):
            with self.subTest(name=name):
                record, log, channel = self.run_experiment(ack)
                self.assertEqual(record["status"], "inconclusive")
                self.assertEqual([entry for entry in log if entry[0] == "exchange"], [("exchange", 1)])
                self.assertEqual(log.count(("send", "limit")), 1)
                self.assertNotIn(("finish",), log)
                self.assertTrue(channel.closed)
                self.assertIn("error", record)

    def test_a_prefix_that_is_not_whole_records_is_never_limited(self):
        record, log, _ = self.run_experiment(applied(len(PREFIX) - 3, 8), prefix=PREFIX[:-3])
        self.assertEqual(record["status"], "inconclusive")
        self.assertNotIn(("send", "limit"), log)

    def test_the_two_cuts_use_their_own_limits_and_dispositions(self):
        self.assertEqual(extension_check.LIMIT_EXPERIMENTS,
                         {"header": {"extra": 8, "sigxfsz": "default"},
                          "body": {"extra": extension_check.PCAP_RECORD + 20, "sigxfsz": "ignore"}})
        self.assertLess(extension_check.LIMIT_EXPERIMENTS["header"]["extra"], extension_check.PCAP_RECORD)
        self.assertGreater(extension_check.LIMIT_EXPERIMENTS["body"]["extra"], extension_check.PCAP_RECORD)


def sample(**changes):
    value = {"status": None, "process": "S (sleeping)", "lo": {"up": True}, "namespace": "net:[1]", "interfaces": []}
    value.update(changes)
    return value


class LoopbackJudgeTests(unittest.TestCase):
    def judge(self, samples, status=0, problems=()):
        return extension_check.loopback_judge("net:[1]", samples, {"status": status}, {}, list(problems))

    def test_kept_running_needs_every_sample_and_a_clean_sigterm(self):
        self.assertEqual(self.judge([sample()] * 3), ("kept_running", []))
        self.assertEqual(self.judge([sample(), sample(status=1)]), ("exited", []))
        for name, samples, status, problems in (
                ("stopped", [sample(), sample(process="T (stopped)")], 0, ()),
                ("no process", [sample(process=None)], 0, ()),
                ("another namespace", [sample(namespace="net:[2]")], 0, ()),
                ("the interface stays", [sample(interfaces=[{"name": "eth0"}])], 0, ()),
                ("lo down", [sample(lo={"up": False})], 0, ()),
                ("lo unknown", [sample(lo=None)], 0, ()),
                ("unclean SIGTERM", [sample()], 1, ()),
                ("a bad file", [sample()], 0, ("the selected flow lacks response",)),
                ("no sample", [], 0, ())):
            with self.subTest(name=name):
                outcome, reasons = self.judge(samples, status, problems)
                self.assertEqual(outcome, "inconclusive")
                self.assertTrue(reasons)


class WiringTests(unittest.TestCase):
    def test_the_experiments_are_opt_in_and_never_in_the_gate(self):
        args = extension_check.arguments(["--extension", "capture", "--image", "helper", "--functional",
                                          "--target", "t"])
        self.assertFalse(args.capture_experiments)
        args = extension_check.arguments(["--extension", "capture", "--image", "helper", "--functional",
                                          "--target", "t", "--capture-experiments"])
        self.assertTrue(args.capture_experiments)
        self.assertNotIn("limit", extension_check.CAPTURE_STOPS)
        self.assertNotIn("kept_running_loopback", extension_check.CAPTURE_STOPS)


if __name__ == "__main__":
    unittest.main()
