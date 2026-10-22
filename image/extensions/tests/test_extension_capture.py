import contextlib
import io
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check

HELPER = {"reference": "ceph-check:extension-capture", "image_id": "sha256:" + "c" * 64, "platform": "linux/amd64"}
PROBE_OUTPUT = "".join("CHECK\tpassed\t%s\n" % name for name in (
    "path:sh", "path:sleep", "path:tcpdump", "path:python3", "sleep-infinity", "version:tcpdump",
    "user:unprivileged", "file-capability:net_raw", "filter:compile", "filter:compile-ip6", "filter:invalid",
    "capture:loopback", "python-runtime", "writable:/tmp")) + \
    "VERSION\ttcpdump\ttcpdump version 4.99.3 libpcap version 1.10.3\nCOMPLETE\tcapture\n"


class CaptureQuickCheckTests(unittest.TestCase):
    """The capture helper's quick check, with Docker replaced by fakes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)

    def test_only_the_quick_check_is_offered_for_capture(self):
        self.assertEqual(extension_check.arguments(["--extension", "capture", "--image", "x"]).extension, "capture")
        stderr = io.StringIO()
        for extra in (["--functional", "--target", "t"], ["--rate"], ["--packet-faults"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                extension_check.arguments(["--extension", "capture", "--image", "x", *extra])
        self.assertIn("only the quick check", stderr.getvalue())

    def run_probe(self, extension):
        """The docker create arguments of a probe, which then passes."""
        created = []

        def docker(*command, timeout=120, check_exit=True):
            if command[0] == "create":
                created.append(list(command))
            return 0, ""

        result = mock.Mock(returncode=0, stdout=PROBE_OUTPUT.replace("capture", extension) if extension == "netem"
                           else PROBE_OUTPUT, stderr="")
        image = dict(HELPER)
        args = mock.Mock(probe_timeout=10)
        with mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check.subprocess, "run", return_value=result), \
                mock.patch.object(extension_check, "remove_container", return_value={"status": "passed", "errors": []}):
            extension_check.probe(extension, image, args, self.output)
        return created[0], image

    def test_capture_probe_runs_as_the_image_user_with_net_raw_alone(self):
        command, image = self.run_probe("capture")
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--cap-add=NET_RAW", command)
        self.assertIn("--network=none", command)
        self.assertFalse(any(arg.startswith("--user") for arg in command))
        self.assertEqual(image["status"], "passed")
        self.assertEqual(image["versions"]["tcpdump"], "tcpdump version 4.99.3 libpcap version 1.10.3")
        netem, _ = self.run_probe("netem")
        self.assertIn("--user=0:0", netem)
        self.assertFalse(any(arg.startswith("--cap-add") for arg in netem))

    STARTED = {"ExitCode": 255, "Error": "", "StartedAt": "2026-10-22T22:27:18Z"}

    def refusal(self, code, text, state=None, create_error=None):
        calls = []

        def docker(*command, timeout=120, check_exit=True):
            calls.append(list(command))
            if command[0] == "create":
                if create_error:
                    raise extension_check.CheckError(create_error)
                return 0, ""
            if command[0] == "start":
                return code, text
            return 0, json.dumps(self.STARTED if state is None else state)

        with mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check, "remove_container", return_value={"status": "passed", "errors": []}):
            result = extension_check.capture_refusal(dict(HELPER), self.output)
        return result, calls[0]

    def test_tcpdump_must_not_start_without_net_raw(self):
        result, command = self.refusal(255, "exec /usr/bin/tcpdump: operation not permitted\n")
        self.assertEqual(result["status"], "passed")
        self.assertIn("--cap-drop=ALL", command)
        self.assertFalse(any(arg.startswith("--cap-add") for arg in command))
        self.assertEqual(command[command.index("--entrypoint=tcpdump") + 2], "--version")

    NEVER_STARTED = {"ExitCode": 0, "Error": "", "StartedAt": "0001-01-01T00:00:00Z"}
    # Docker or the runtime never ran the process: these say nothing about the image.
    SETUP_FAILURES = (
        (125, "permission denied while trying to connect to the Docker daemon socket\n", NEVER_STARTED),
        (126, "OCI runtime create failed: unable to start container process\n",
         dict(NEVER_STARTED, ExitCode=126, Error="OCI runtime create failed")),
        (255, "exec /usr/bin/tcpdump: operation not permitted\n", dict(NEVER_STARTED, ExitCode=255)),
        (1, "Error response from daemon: container is marked for removal\n", STARTED))

    def test_only_the_runtime_exec_refusal_passes(self):
        for code, text, state in (
                (0, "tcpdump version 4.99.3\n", dict(self.STARTED, ExitCode=0)),
                # tcpdump itself ran and failed: its capability was granted.
                (1, "tcpdump: Operation not permitted\n", dict(self.STARTED, ExitCode=1)),
                (255, "tcpdump: lo: You don't have permission to perform this capture\n", None),
                (255, "exec /usr/bin/tcpdump: operation not permitted\nmore\n", None)):
            with self.subTest(code=code, text=text):
                result, _ = self.refusal(code, text, state)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", "quick_failure"))
        result, _ = self.refusal(0, "", create_error="permission denied while trying to connect to the Docker daemon")
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "checker_failure"))

    def test_docker_and_runtime_setup_failures_are_checker_failures(self):
        for code, text, state in self.SETUP_FAILURES:
            with self.subTest(code=code, text=text):
                result, _ = self.refusal(code, text, state)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", "checker_failure"))
                self.assertIn("did not run", result["error"])
                image = dict(HELPER, labels={})

                def probe(extension, checked, args, output):
                    checked.update(status="passed", checks=[], cleanup={"status": "passed", "errors": []})

                docker_calls = {"create": (0, ""), "start": (code, text), "container": (0, json.dumps(state))}

                def docker(*command, timeout=120, check_exit=True):
                    if command[0] == "image":
                        return 0, json.dumps([{"Config": {"Labels": {}}}])
                    return docker_calls[command[0]]

                output = self.output / ("main-%d-%d" % (code, len(text)))
                with mock.patch.object(extension_check.check, "inspect_image", return_value=dict(image)), \
                        mock.patch.object(extension_check, "docker", side_effect=docker), \
                        mock.patch.object(extension_check, "probe", side_effect=probe), \
                        mock.patch.object(extension_check, "remove_container",
                                          return_value={"status": "passed", "errors": []}), \
                        mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    self.assertEqual(extension_check.main(["--extension", "capture", "--image", "x", "--output-dir",
                                                           str(output)]), 1)
                report = json.loads((output / "extension-report.json").read_text())
                self.assertEqual((report["status"], report["failure_kind"]), ("failed", "checker_failure"))

    def test_a_tcpdump_that_starts_without_net_raw_fails_the_image(self):
        image = dict(HELPER, labels={})

        def probe(extension, checked, args, output):
            checked.update(status="passed", checks=[{"name": "path:sh", "status": "passed"}],
                           cleanup={"status": "passed", "errors": []})

        refusal = {"name": "refused-without-net_raw", "status": "failed", "failure_kind": "quick_failure",
                   "cleanup": {"status": "passed", "errors": []}}
        with mock.patch.object(extension_check.check, "inspect_image", return_value=dict(image)), \
                mock.patch.object(extension_check, "docker", return_value=(0, json.dumps([{"Config": {"Labels": {}}}]))), \
                mock.patch.object(extension_check, "probe", side_effect=probe), \
                mock.patch.object(extension_check, "capture_refusal", return_value=refusal), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = extension_check.main(["--extension", "capture", "--image", "x", "--output-dir",
                                         str(self.output / "quick")])
        report = json.loads((self.output / "quick" / "extension-report.json").read_text())
        self.assertEqual((code, report["status"], report["failure_kind"]), (1, "failed", "image_unsupported"))
        self.assertEqual(report["contract"], "1")
        self.assertEqual(report["image"]["checks"][-1]["name"], "refused-without-net_raw")


PROBE = Path(extension_check.EXTENSIONS["capture"]["probe"])


def udp_frame(port, payload):
    """An Ethernet/IPv4/UDP frame to 127.0.0.1:port."""
    udp = struct.pack(">HHHH", 40000, port, 8 + len(payload), 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, bytes([127, 0, 0, 1]),
                     bytes([127, 0, 0, 1]))
    return b"\x00" * 12 + b"\x08\x00" + ip + udp


def pcap(*frames, snaplen=128, link=1):
    data = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, snaplen, link)
    for frame in frames:
        data += struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame
    return data


class CaptureOracleTests(unittest.TestCase):
    """The loopback oracle embedded in check-capture.sh, run on synthetic captures."""

    def oracle(self, data, status=0, stderr=None):
        script = PROBE.read_text()
        code = script[script.index("<<'ORACLE'\n") + len("<<'ORACLE'\n"):script.index("\nORACLE\n")]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "lo.pcap").write_bytes(data)
            Path(directory, "stderr").write_text(stderr if stderr is not None else (
                "tcpdump: listening on lo, link-type EN10MB (Ethernet), snapshot length 128 bytes\n"
                "1 packet captured\n2 packets received by filter\n0 packets dropped by kernel\n"))
            return subprocess.run([sys.executable, "-c", code, directory, str(status)], capture_output=True).returncode

    def test_only_the_selected_datagram_passes(self):
        selected, excluded = udp_frame(7000, b"capture-check-7000"), udp_frame(7001, b"capture-check-7001")
        self.assertEqual(self.oracle(pcap(selected)), 0)
        for name, data in (("excluded only", pcap(excluded)), ("both", pcap(selected, excluded)),
                           ("selected port, other payload", pcap(udp_frame(7000, b"capture-check-7001"))),
                           ("empty", pcap()), ("cut record", pcap(selected)[:-3]),
                           ("other snaplen", pcap(selected, snaplen=262144)), ("other link type", pcap(selected, link=113)),
                           ("no header", b"")):
            with self.subTest(name=name):
                self.assertNotEqual(self.oracle(data), 0)
        self.assertNotEqual(self.oracle(pcap(selected), status=1), 0)
        self.assertNotEqual(self.oracle(pcap(selected), stderr="0 packets captured\n"), 0)

    def test_the_probe_uses_only_the_contract_executables(self):
        # sh builtins plus sh, sleep, tcpdump and python3; no grep, tr, rm and the like.
        script = PROBE.read_text()
        shell = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        for heredoc in ("READY", "ORACLE"):
            start = shell.index("<<'%s'\n" % heredoc)
            shell = shell[:start] + shell[shell.index("\n%s\n" % heredoc, start) + len(heredoc) + 2:]
        for tool in ("grep", "tr", "rm", "cat", "sed", "awk", "head", "tail", "seq", "id", "mkdir", "ls", "cut",
                     "mktemp", "getcap", "timeout"):
            with self.subTest(tool=tool):
                self.assertIsNone(re.search(r"(^|[;&|(`]|\$\(|\bthen|\bdo|\belse)\s*%s\b" % tool, shell, re.M),
                                  tool + " is not a contract executable")


class CapturePublicationTests(unittest.TestCase):
    def test_the_capture_helper_is_not_published_yet(self):
        import publish
        self.assertEqual(extension_check.PUBLISHED, ("netem",))
        report = {"status": "passed", "level": "functional", "extension": "capture", "contract": "1"}
        with self.assertRaisesRegex(publish.PublishError, "unpublished extension 'capture'"):
            publish.validate_extension_report(report, "capture", "amd64")

    def test_the_capture_job_builds_and_checks_but_cannot_publish(self):
        workflow = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "test.yml").read_text()
        job = workflow[workflow.index("  capture-extension:"):workflow.index("  promote-extension:")]
        self.assertIn("make capture-image", job)
        self.assertIn("--extension capture", job)
        self.assertNotIn("packages: write", job)
        self.assertNotIn("publish.py", job)
        self.assertNotIn("docker/login-action", job)


if __name__ == "__main__":
    unittest.main()
