import ast
import copy
import hashlib
import io
import json
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import capture_experiment as experiment
import capture_experiment_supervisor as supervisor
import extension_check

HEADER = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, extension_check.CAPTURE_SNAPLEN, 1)
RECORD = struct.pack("<IIII", 1, 2, 60, 60) + bytes(range(60))
PREFIX = HEADER + RECORD * 2
STATISTICS = b"3 packets captured\n3 packets received by filter\n0 packets dropped by kernel\n"


OBSERVED = {"default": "default", "ignore": "ignored"}


def receipt(extra=5, sigxfsz="default", code=-25, sent=(), running=False, stderr=STATISTICS, prefix=PREFIX):
    size = len(prefix)
    text = stderr.decode()
    return {
        "schema": experiment.SCHEMA,
        "launcher": {"restore_signals": sigxfsz == "default", "sigxfsz": sigxfsz, "python": "3.11.2",
                     "own_sigxfsz": "ignored"},
        "child": {"pid": 77, "argv": ["tcpdump"]},
        "events": [{"op": "wait_stderr", "contains": "listening", "seen": True},
                   {"op": "limit", "prefix": size, "extra": extra, "status": "applied", "bytes": size + extra,
                    "size_before": size, "size_after": size, "prefix_sha256": hashlib.sha256(prefix).hexdigest(),
                    "own_before": [-1, -1], "own_after": [-1, -1], "before": [-1, -1],
                    "after": [size + extra, size + extra], "sigxfsz": OBSERVED[sigxfsz]}],
        "exit": {"returncode": code, "signal": None, "running": running, "reaped_with_sigkill": running,
                 "sent": list(sent)},
        "stderr": {"head": text, "tail": "", "total": len(text), "retained": len(text), "overflow": False},
    }


class OracleBranchTests(unittest.TestCase):
    """Each branch is its own observed outcome."""

    def test_cut_record_header_after_sigxfsz(self):
        result = experiment.assess(receipt(5), PREFIX + RECORD[:5])
        self.assertEqual((result["verdict"], result["reasons"]), ("observed", []))
        self.assertEqual((result["termination"], result["cut_record"]), ("killed_by_SIGXFSZ", "header"))
        self.assertEqual(result["limit"], {"prefix": len(PREFIX), "extra": 5, "bytes": len(PREFIX) + 5,
                                           "sigxfsz": "default"})

    def test_cut_record_body_after_sigxfsz(self):
        result = experiment.assess(receipt(17), PREFIX + RECORD[:17])
        self.assertEqual((result["verdict"], result["termination"], result["cut_record"]),
                         ("observed", "killed_by_SIGXFSZ", "body"))

    def test_a_limit_on_a_record_boundary_cuts_nothing(self):
        result = experiment.assess(receipt(len(RECORD)), PREFIX + RECORD)
        self.assertEqual((result["verdict"], result["cut_record"]), ("observed", "none"))

    def test_ignored_sigxfsz_leaves_tcpdump_running_until_sigterm(self):
        result = experiment.assess(receipt(17, sigxfsz="ignore", code=0, sent=["SIGTERM"]), PREFIX + RECORD[:17])
        self.assertEqual((result["verdict"], result["termination"], result["cut_record"]),
                         ("observed", "exited_0_after_SIGTERM", "body"))
        # The statistics count packets that were not written: kept as printed.
        self.assertEqual(result["statistics"]["captured"], 3)

    def test_a_write_error_exit_is_its_own_outcome(self):
        result = experiment.assess(receipt(5, sigxfsz="ignore", code=1), PREFIX + RECORD[:5])
        self.assertEqual((result["verdict"], result["termination"]), ("observed", "exited_1"))

    def test_a_child_that_never_ended_is_reaped_and_reported_as_running(self):
        result = experiment.assess(receipt(5, sigxfsz="ignore", code=-9, running=True), PREFIX + RECORD[:5])
        self.assertEqual((result["verdict"], result["termination"]), ("observed", "still_running"))

    def test_another_signal_keeps_its_number(self):
        result = experiment.assess(receipt(5, code=-6), PREFIX + RECORD[:5])
        self.assertEqual(result["termination"], "killed_by_signal_6")


class OracleNegativeControlTests(unittest.TestCase):
    def inconclusive(self, value, data=PREFIX + RECORD[:5], reason=""):
        result = experiment.assess(value, data)
        self.assertEqual(result["verdict"], "inconclusive", result)
        self.assertTrue(any(reason in item for item in result["reasons"]), result["reasons"])
        return result

    def test_sigkill_is_never_relabelled_as_sigxfsz_or_success(self):
        for sent, text in (([], "unrequested"), (["SIGKILL"], "sent by the supervisor")):
            with self.subTest(sent=sent):
                result = self.inconclusive(receipt(5, code=-9, sent=sent), reason=text)
                self.assertEqual(result["termination"], "killed_by_SIGKILL")

    def test_sigxfsz_death_contradicts_an_ignored_disposition(self):
        self.inconclusive(receipt(5, sigxfsz="ignore", code=-25), reason="told to ignore")

    def test_missing_null_and_wrong_typed_receipts(self):
        for value in (None, [], "text", 0, {}, dict(receipt(), schema="other")):
            with self.subTest(value=value):
                self.inconclusive(value, reason="no supervisor receipt")

    def test_missing_null_and_wrong_typed_fields_stay_inconclusive(self):
        mutations = {
            "launcher": [None, "x", {}, {"sigxfsz": "maybe"}],
            "child": [None, [], {"pid": None}, {"pid": "7"}, {"pid": True}, {"pid": 0}],
            "events": [None, {}, [1], "x"],
            "exit": [None, [], {}, {"returncode": None, "running": False, "reaped_with_sigkill": False, "sent": []},
                     {"returncode": "0", "running": False, "reaped_with_sigkill": False, "sent": []},
                     {"returncode": True, "running": False, "reaped_with_sigkill": False, "sent": []},
                     {"returncode": 0, "running": 0, "reaped_with_sigkill": False, "sent": []},
                     {"returncode": 0, "running": False, "reaped_with_sigkill": False, "sent": [1]},
                     {"returncode": 0, "running": False, "reaped_with_sigkill": False, "sent": None}],
            "stderr": [None, "x", {}, {"head": "", "tail": "", "total": None, "retained": 0, "overflow": False},
                       {"head": None, "tail": "", "total": 0, "retained": 0, "overflow": False},
                       {"head": "", "tail": "", "total": 0, "retained": 0, "overflow": "no"},
                       {"head": "", "tail": "", "total": True, "retained": 0, "overflow": False}],
        }
        for key, values in mutations.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    changed = receipt()
                    changed[key] = value
                    self.inconclusive(changed)
            with self.subTest(key=key, value="missing"):
                changed = receipt()
                del changed[key]
                self.inconclusive(changed)
        for key in ("sigxfsz", "restore_signals", "python", "own_sigxfsz"):
            for value in (None, 1, ["x"], "bogus"):
                with self.subTest(launcher=key, value=value):
                    changed = receipt()
                    changed["launcher"][key] = value
                    if key in ("python", "own_sigxfsz") and value == "bogus":
                        continue  # any text is a recorded string
                    self.inconclusive(changed)

    def test_restore_signals_must_match_the_named_disposition(self):
        changed = receipt()
        changed["launcher"]["restore_signals"] = False
        self.inconclusive(changed, reason="SIGXFSZ setting")

    def test_stderr_overflow_is_inconclusive_and_statistics_stay_unknown(self):
        changed = receipt()
        changed["stderr"].update(total=9000, retained=len(STATISTICS), overflow=True)
        result = self.inconclusive(changed, reason="overflowed")
        self.assertIsNone(result["statistics"])
        self.assertEqual(result["termination"], "killed_by_SIGXFSZ")

    def test_overflow_counts_must_agree_with_the_flag(self):
        for total, retained, overflow in ((9000, 10, False), (10, 10, True), (5, 10, False)):
            with self.subTest(total=total, retained=retained, overflow=overflow):
                changed = receipt()
                changed["stderr"].update(total=total, retained=retained, overflow=overflow)
                self.inconclusive(changed, reason="contradict")

    def test_the_limit_event_must_be_exactly_one_and_applied(self):
        changed = receipt()
        changed["events"] = [event for event in changed["events"] if event["op"] != "limit"]
        self.inconclusive(changed, reason="0 limit events")
        changed = receipt()
        changed["events"].append(copy.deepcopy(changed["events"][1]))
        self.inconclusive(changed, reason="2 limit events")
        for status in ("refused", "unavailable", "error", None, 5):
            with self.subTest(status=status):
                changed = receipt()
                changed["events"][1]["status"] = status
                self.inconclusive(changed, reason="not applied")

    def test_limit_fields_that_are_missing_null_or_wrong_typed(self):
        for key in ("prefix", "extra", "bytes", "before", "after", "own_before", "own_after", "size_before",
                    "size_after", "prefix_sha256", "sigxfsz"):
            for value in (None, "x", True, [1], [None, None], [1, "2"], -1.5):
                with self.subTest(key=key, value=value):
                    changed = receipt()
                    if changed["events"][1][key] == value:
                        continue
                    changed["events"][1][key] = value
                    self.inconclusive(changed)
            with self.subTest(key=key, value="missing"):
                changed = receipt()
                del changed["events"][1][key]
                self.inconclusive(changed)

    def test_limit_rules(self):
        cases = {
            "reads": lambda e: e.update(after=[999999, 999999]),
            "already": lambda e: e.update(before=[e["bytes"], e["bytes"]]),
            "own limit changed": lambda e: e.update(own_after=[10, 10]),
            "not stable": lambda e: e.update(size_after=e["size_after"] + 1),
            "SHA-256": lambda e: e.update(prefix_sha256="zz"),
            "SIGXFSZ is": lambda e: e.update(sigxfsz="ignored"),
            "consistent integers": lambda e: e.update(bytes=e["bytes"] + 1),
            "consistent integers ": lambda e: e.update(extra=0, bytes=e["prefix"]),
        }
        for reason, mutate in cases.items():
            with self.subTest(reason=reason):
                changed = receipt()
                mutate(changed["events"][1])
                self.inconclusive(changed, reason=reason.strip())

    def test_an_unknown_sigxfsz_disposition_is_never_accepted(self):
        for mode, observed in (("default", "unknown"), ("default", "caught"), ("default", "ignored"),
                               ("ignore", "unknown"), ("ignore", "caught"), ("ignore", "default"),
                               ("ignore", "ignore")):
            with self.subTest(mode=mode, observed=observed):
                changed = receipt(sigxfsz=mode, code=0, sent=["SIGTERM"])
                changed["events"][1]["sigxfsz"] = observed
                self.inconclusive(changed, reason="SIGXFSZ is %s" % observed)

    def test_each_requested_mode_matches_only_its_observed_disposition(self):
        for mode, observed in (("default", "default"), ("ignore", "ignored")):
            with self.subTest(mode=mode):
                changed = receipt(sigxfsz=mode, code=0, sent=["SIGTERM"])
                self.assertEqual(changed["events"][1]["sigxfsz"], observed)
                self.assertEqual(experiment.assess(changed, PREFIX + RECORD[:5])["verdict"], "observed")

    def test_file_rules(self):
        data = PREFIX + RECORD[:5]
        self.inconclusive(receipt(5), data=None, reason="not bytes")
        self.inconclusive(receipt(5), data=bytearray(data), reason="not bytes")
        self.inconclusive(receipt(5), data=PREFIX[:-3], reason="recorded prefix")
        self.inconclusive(receipt(5), data=PREFIX[:-1] + b"X" + RECORD[:5], reason="recorded prefix")
        self.inconclusive(receipt(5), data=PREFIX + RECORD[:6], reason="over its limit")
        self.inconclusive(receipt(5), data=b"\0" * 4 + PREFIX[4:] + RECORD[:5], reason="")
        # A prefix that ends inside a record is not a verified boundary.
        short = PREFIX[:-10]
        self.inconclusive(receipt(5, prefix=short), data=short + RECORD[:5], reason="complete records")
        # A prefix with a header and no record proves nothing.
        self.inconclusive(receipt(5, prefix=HEADER), data=HEADER + RECORD[:5], reason="no record")

    def test_other_pcap_problems_are_not_hidden_by_a_cut_tail(self):
        bad = struct.pack("<IIII", 1, 2000000, 60, 60) + bytes(60)
        self.inconclusive(receipt(len(bad)), data=PREFIX + bad, reason="record 2")

    def test_a_cut_record_that_disagrees_with_its_kind_is_inconclusive(self):
        with mock.patch.object(extension_check, "parse_pcap", return_value=(
                [(bytes(60), 60)] * 2, ["a record header is cut at byte 1"])) as parser:
            parser.side_effect = [([(bytes(60), 60)] * 2, []),
                                  ([(bytes(60), 60)] * 2, ["a record header is cut at byte 1"])]
            self.inconclusive(receipt(17), data=PREFIX + RECORD[:17], reason="inconsistent")

    def test_every_negative_control_leaves_branch_fields_without_a_verdict(self):
        result = experiment.assess(None, b"")
        self.assertEqual((result["verdict"], result["termination"], result["cut_record"], result["statistics"]),
                         ("inconclusive", None, None, None))


class ReadReceiptTests(unittest.TestCase):
    def test_the_last_line_is_the_receipt(self):
        text = json.dumps({"op": "wait_stderr"}) + "\n" + json.dumps(receipt()) + "\n"
        self.assertEqual(experiment.read_receipt(text), receipt())

    def test_unreadable_output_has_no_receipt(self):
        good = json.dumps(receipt())
        for text in ("", "\n", "not json\n", good[:-5], good + "\n" + json.dumps({"op": "late"}),
                     json.dumps([receipt()]), "null", good + "\ntrailing garbage"):
            with self.subTest(text=text[:30]):
                self.assertIsNone(experiment.read_receipt(text))
                self.assertEqual(experiment.assess(experiment.read_receipt(text), PREFIX)["verdict"],
                                 "inconclusive")


class LauncherTests(unittest.TestCase):
    def test_the_sigxfsz_disposition_must_be_named(self):
        for value in (None, "", "restore", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                experiment.supervisor_config(["tcpdump"], "/tmp/x.pcap", value)
        self.assertEqual(experiment.supervisor_config(["tcpdump"], "/tmp/x.pcap", "ignore")["sigxfsz"], "ignore")

    def test_the_command_runs_the_supervisor_source_with_the_config(self):
        config = experiment.supervisor_config(["tcpdump"], "/tmp/x.pcap", "default")
        command = experiment.supervisor_command("helper", config)
        self.assertEqual(command[:5], ["exec", "-i", "helper", "python3", "-c"])
        self.assertEqual(json.loads(command[6]), config)
        compile(command[5], "capture_experiment_supervisor", "exec")

    def test_tcpdump_arguments_are_the_functional_checks(self):
        seen = []

        def fake_docker(*arguments, **kwargs):
            seen.append(arguments)
            raise RuntimeError("stop")

        with mock.patch.object(extension_check, "docker", fake_docker), self.assertRaises(RuntimeError):
            extension_check.start_capture("helper", "/tmp/c", "eth0", "tcp port 7000", 9)
        self.assertEqual(list(seen[0][seen[0].index("/tmp/c") + 1:]),
                         experiment.tcpdump_argv("/tmp/c", "eth0", "tcp port 7000", 9)[1:])

    # The supervisor needs exactly these modules; each must import under -I.
    SUPERVISOR_MODULES = {"hashlib", "json", "resource", "signal", "subprocess", "sys", "threading", "time"}

    def test_the_supervisor_uses_only_the_standard_library(self):
        tree = ast.parse(experiment.SUPERVISOR_SOURCE)
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertLessEqual(names, self.SUPERVISOR_MODULES)
        for name in sorted(names):
            with self.subTest(module=name):
                done = subprocess.run([sys.executable, "-I", "-c", "import " + name], capture_output=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_the_new_files_stay_within_the_oldest_python_that_ci_uses(self):
        # CI runs the unit tests on Python 3.9 (.github/workflows/test.yml); the helper has 3.11.
        files = [Path(__file__), Path(experiment.__file__), Path(supervisor.__file__)]
        newer = ("stdlib_module_names", "bit_count", "strict=", "slots=True", "kw_only", "TaskGroup", "tomllib")
        for path in files:
            text = path.read_text()
            with self.subTest(path=path.name):
                ast.parse(text, feature_version=(3, 9))
                if path != Path(__file__):  # this file names the words
                    self.assertEqual([word for word in newer if word in text], [])

    def test_the_source_runs_as_the_helper_runs_it(self):
        config = experiment.supervisor_config([sys.executable, "-c", "import sys; sys.stderr.write('ready\\n')"],
                                              "/nonexistent.pcap", "default")
        commands = json.dumps({"op": "wait_stderr", "contains": "ready", "timeout": 5}) + "\n" + \
            json.dumps(experiment.limit_command(10, 5)) + "\n"
        done = subprocess.run([sys.executable, "-I", "-c", experiment.SUPERVISOR_SOURCE, json.dumps(config)],
                              input=commands, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        found = experiment.read_receipt(done.stdout)
        self.assertEqual(found["events"][0], {"op": "wait_stderr", "contains": "ready", "seen": True})
        self.assertIn(found["events"][1]["status"], ("refused", "unavailable"))
        self.assertEqual(found["exit"]["returncode"], 0)
        # No applied limit: the experiment has no result.
        self.assertEqual(experiment.assess(found, b"")["verdict"], "inconclusive")


class FakeChild:
    def __init__(self, stderr=b"", code=0, running=False, pid=4242):
        self.pid, self.sent = pid, []
        self.stderr = io.BufferedReader(io.BytesIO(stderr))
        self.returncode = None if running else code

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def send_signal(self, number):
        self.sent.append(number)
        self.returncode = 0

    def kill(self):
        self.returncode = -9


class FakeResource:
    RLIMIT_FSIZE = 1

    def __init__(self, own=(-1, -1), child=(-1, -1), fail=None):
        self.own, self.child, self.fail, self.calls = own, child, fail, []

    def getrlimit(self, kind):
        return self.own

    def prlimit(self, pid, kind, new=None):
        if self.fail:
            raise self.fail
        old = self.child
        if new is not None:
            self.calls.append((pid, kind, tuple(new)))
            self.child = tuple(new)
        return old


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.pcap = self.directory / "c.pcap"
        self.pcap.write_bytes(PREFIX)
        self.proc = self.directory / "proc"
        (self.proc / "4242").mkdir(parents=True)
        (self.proc / "self").mkdir()
        self.status(ignored=True)
        patcher = mock.patch.object(supervisor, "FINAL_WAIT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def status(self, ignored):
        text = "Name:\ttcpdump\nSigIgn:\t%016x\nSigCgt:\t0000000000004002\n" % (1 << 24 if ignored else 0)
        (self.proc / "4242" / "status").write_text(text)
        (self.proc / "self" / "status").write_text(text)

    def run_supervisor(self, commands, child=None, module=None, sigxfsz="ignore", **config):
        child = child or FakeChild()
        popen = mock.Mock(return_value=child)
        events = []
        config = {"argv": ["tcpdump"], "pcap": str(self.pcap), "sigxfsz": sigxfsz, **config}
        found = supervisor.run(config, commands, events.append, popen=popen, module=module, proc=str(self.proc))
        return found, events, popen

    def test_the_limit_is_lowered_on_the_child_alone_and_read_back(self):
        module = FakeResource(own=(100, 200))
        found, events, _ = self.run_supervisor([{"op": "limit", "prefix": len(PREFIX), "extra": 5}], module=module)
        limit = found["events"][0]
        self.assertEqual(limit["status"], "applied")
        self.assertEqual(module.calls, [(4242, 1, (len(PREFIX) + 5,) * 2)])
        self.assertEqual((limit["before"], limit["after"]), ([-1, -1], [len(PREFIX) + 5] * 2))
        self.assertEqual((limit["own_before"], limit["own_after"]), ([100, 200], [100, 200]))
        self.assertEqual(limit["prefix_sha256"], hashlib.sha256(PREFIX).hexdigest())
        self.assertEqual((limit["size_before"], limit["size_after"], limit["sigxfsz"]),
                         (len(PREFIX), len(PREFIX), "ignored"))
        self.assertEqual(events[0], limit)

    def test_a_receipt_with_an_applied_limit_passes_the_oracle_after_a_cut(self):
        module = FakeResource()
        self.status(ignored=False)
        found, _, _ = self.run_supervisor([{"op": "limit", "prefix": len(PREFIX), "extra": 17}], module=module,
                                          child=FakeChild(code=-25, stderr=STATISTICS), sigxfsz="default")
        found = json.loads(json.dumps(found))
        result = experiment.assess(found, PREFIX + RECORD[:17])
        self.assertEqual((result["verdict"], result["termination"], result["cut_record"]),
                         ("observed", "killed_by_SIGXFSZ", "body"), result)

    def test_a_genuine_receipt_of_each_mode_round_trips_through_the_oracle(self):
        for mode, ignored, code in (("default", False, -25), ("ignore", True, 0)):
            with self.subTest(mode=mode):
                self.status(ignored)
                child = FakeChild(code=code, stderr=STATISTICS)
                sent = [{"op": "signal", "name": "SIGTERM"}] if mode == "ignore" else []
                if mode == "ignore":
                    child = FakeChild(running=True, stderr=STATISTICS)
                found, _, _ = self.run_supervisor([{"op": "limit", "prefix": len(PREFIX), "extra": 5}] + sent,
                                                  child=child, module=FakeResource(), sigxfsz=mode)
                result = experiment.assess(json.loads(json.dumps(found)), PREFIX + RECORD[:5])
                self.assertEqual((result["verdict"], result["reasons"]), ("observed", []))

    def test_a_disconnected_output_still_reaps_the_child_and_keeps_the_failure(self):
        for child in (FakeChild(running=True), FakeChild(running=True, stderr=STATISTICS)):
            def emit(event):
                raise BrokenPipeError(32, "closed")

            popen = mock.Mock(return_value=child)
            with self.assertRaises(BrokenPipeError):
                supervisor.run({"argv": ["tcpdump"], "pcap": str(self.pcap), "sigxfsz": "ignore"},
                               [{"op": "wait_exit", "timeout": 0}, {"op": "wait_exit", "timeout": 0}], emit,
                               popen=popen, module=None, proc=str(self.proc))
            self.assertEqual(child.returncode, -9)
            self.assertTrue(child.stderr.closed)

    def test_a_cleanup_failure_does_not_replace_the_output_failure(self):
        child = FakeChild(running=True)
        child.kill = mock.Mock(side_effect=ProcessLookupError(3, "gone"))

        def emit(event):
            raise BrokenPipeError(32, "closed")

        with self.assertRaises(BrokenPipeError):
            supervisor.run({"argv": ["tcpdump"], "sigxfsz": "ignore"}, [{"op": "wait_exit", "timeout": 0}], emit,
                           popen=mock.Mock(return_value=child), module=None, proc=str(self.proc))

    def test_a_file_that_is_not_the_prefix_is_refused_and_the_limit_stays(self):
        module = FakeResource()
        self.pcap.write_bytes(PREFIX + b"x")
        found, _, _ = self.run_supervisor([{"op": "limit", "prefix": len(PREFIX), "extra": 5}], module=module)
        self.assertEqual(found["events"][0]["status"], "refused")
        self.assertEqual(module.calls, [])
        missing = self.directory / "none.pcap"
        found, _, _ = self.run_supervisor([{"op": "limit", "prefix": len(PREFIX), "extra": 5}], module=module,
                                          pcap=str(missing))
        self.assertEqual((found["events"][0]["status"], module.calls), ("refused", []))

    def test_unavailable_prlimit_and_errors_are_results(self):
        command = [{"op": "limit", "prefix": len(PREFIX), "extra": 5}]
        for module, status in ((None, "unavailable"), (mock.Mock(spec=["RLIMIT_FSIZE", "getrlimit"]), "unavailable"),
                               (FakeResource(fail=PermissionError(1, "denied")), "error"),
                               (FakeResource(fail=ProcessLookupError(3, "gone")), "error"),
                               (FakeResource(fail=ValueError("not above")), "error")):
            with self.subTest(module=module, status=status):
                found, _, _ = self.run_supervisor(command, module=module)
                self.assertEqual(found["events"][0]["status"], status)
                self.assertNotIn("after", found["events"][0])

    def test_malformed_limit_commands_are_refused(self):
        module = FakeResource()
        for command in ({"op": "limit"}, {"op": "limit", "prefix": "1", "extra": 5},
                        {"op": "limit", "prefix": True, "extra": 5}, {"op": "limit", "prefix": 1, "extra": -1}):
            with self.subTest(command=command):
                found, _, _ = self.run_supervisor([command], module=module)
                self.assertEqual(found["events"][0]["status"], "refused")
        self.assertEqual(module.calls, [])

    def test_the_sigxfsz_disposition_is_read_from_the_process(self):
        for ignored, expected in ((True, "ignored"), (False, "default")):
            self.status(ignored)
            self.assertEqual(supervisor.signal_disposition(4242, signal.SIGXFSZ, str(self.proc)), expected)
        (self.proc / "4242" / "status").write_text("SigIgn:\tzz\nSigCgt:\t0\n")
        self.assertEqual(supervisor.signal_disposition(4242, signal.SIGXFSZ, str(self.proc)), "unknown")
        (self.proc / "4242" / "status").write_text("SigIgn:\t0\n")
        self.assertEqual(supervisor.signal_disposition(4242, signal.SIGXFSZ, str(self.proc)), "unknown")
        self.assertEqual(supervisor.signal_disposition(9999, signal.SIGXFSZ, str(self.proc)), "unknown")
        (self.proc / "4242" / "status").write_text("SigIgn:\t0\nSigCgt:\t%x\n" % (1 << 24))
        self.assertEqual(supervisor.signal_disposition(4242, signal.SIGXFSZ, str(self.proc)), "caught")

    def test_the_disposition_selects_restore_signals_and_is_recorded(self):
        for name, restore in (("default", True), ("ignore", False)):
            with self.subTest(name=name):
                found, _, popen = self.run_supervisor([], sigxfsz=name)
                self.assertIs(popen.call_args.kwargs["restore_signals"], restore)
                self.assertEqual((found["launcher"]["restore_signals"], found["launcher"]["sigxfsz"]),
                                 (restore, name))
                self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.PIPE)
                self.assertEqual(found["launcher"]["own_sigxfsz"], "ignored")

    def test_stderr_goes_to_a_pipe_and_overflow_is_counted(self):
        noise = b"".join(b"line %04d\n" % number for number in range(50))
        found, _, _ = self.run_supervisor([], child=FakeChild(stderr=noise), head=20, tail=15)
        kept = found["stderr"]
        self.assertEqual((kept["total"], kept["retained"], kept["overflow"]), (len(noise), 35, True))
        self.assertEqual((kept["head"], kept["tail"]), (noise[:20].decode(), noise[-15:].decode()))

    def test_stderr_within_retention_is_kept_whole(self):
        found, _, _ = self.run_supervisor([], child=FakeChild(stderr=STATISTICS))
        kept = found["stderr"]
        self.assertEqual((kept["head"] + kept["tail"], kept["overflow"]), (STATISTICS.decode(), False))

    def test_retention_across_chunks(self):
        retention = supervisor.Retention(4, 3)
        for chunk in (b"ab", b"cdef", b"g", b"hijk"):
            retention.feed(chunk)
        self.assertEqual(retention.snapshot(), {"head": "abcd", "tail": "ijk", "total": 11, "retained": 7,
                                                "overflow": True})

    def test_signals_and_a_child_that_never_ends(self):
        found, _, _ = self.run_supervisor([{"op": "signal", "name": "SIGTERM"}, {"op": "wait_exit", "timeout": 0}],
                                          child=FakeChild(running=True))
        self.assertEqual(found["exit"]["sent"], ["SIGTERM"])
        self.assertEqual(found["events"][0], {"op": "signal", "name": "SIGTERM", "sent": True})
        found, _, _ = self.run_supervisor([], child=FakeChild(running=True))
        self.assertEqual((found["exit"]["running"], found["exit"]["reaped_with_sigkill"], found["exit"]["sent"]),
                         (True, True, []))

    def test_unknown_commands_are_recorded_and_do_nothing(self):
        found, _, _ = self.run_supervisor([None, 5, {"op": "signal", "name": "SIGHUP"}, {"op": "x"},
                                           {"op": "wait_exit", "timeout": "soon"}])
        self.assertEqual([event["op"] for event in found["events"]], ["invalid"] * 5)
        self.assertEqual(found["exit"]["sent"], [])

    def test_main_streams_events_and_ends_with_the_receipt(self):
        out = io.StringIO()
        stdin = io.StringIO('{"op": "wait_exit", "timeout": 0}\nnot json\n')
        config = {"argv": ["tcpdump"], "sigxfsz": "ignore", "pcap": str(self.pcap)}
        with mock.patch.object(supervisor, "run", wraps=supervisor.run) as run, \
                mock.patch.object(subprocess, "Popen", return_value=FakeChild()):
            supervisor.main(["-c", json.dumps(config)], stdin, out)
        self.assertEqual(run.call_count, 1)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([line.get("op") for line in lines[:-1]], ["wait_exit", "invalid"])
        self.assertEqual(lines[-1]["schema"], supervisor.SCHEMA)

    def test_a_bad_config_is_refused_before_a_child_starts(self):
        for config in ({}, {"argv": [], "sigxfsz": "ignore"}, {"argv": "tcpdump", "sigxfsz": "ignore"},
                       {"argv": [1], "sigxfsz": "ignore"}, {"argv": ["x"], "sigxfsz": "restore"},
                       {"argv": ["x"]}):
            with self.subTest(config=config), mock.patch.object(subprocess, "Popen") as popen, \
                    self.assertRaises(SystemExit):
                supervisor.main(["-c", json.dumps(config)], io.StringIO(), io.StringIO())
            popen.assert_not_called()

    def test_a_real_child_ends_by_a_signal_and_its_stderr_is_kept(self):
        code = "import os, signal, sys; sys.stderr.write('x' * 9000); sys.stderr.flush(); " \
               "os.kill(os.getpid(), signal.SIGUSR1)"
        with mock.patch.object(supervisor, "FINAL_WAIT", 10):
            found = supervisor.run({"argv": [sys.executable, "-c", code], "sigxfsz": "default"}, [], lambda e: None,
                                   module=None, proc=str(self.proc))
        self.assertEqual((found["exit"]["returncode"], found["exit"]["signal"]), (-signal.SIGUSR1, "SIGUSR1"))
        self.assertEqual((found["stderr"]["total"], found["stderr"]["overflow"]), (9000, True))


if __name__ == "__main__":
    unittest.main()
