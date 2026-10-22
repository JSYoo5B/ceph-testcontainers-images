import contextlib
import io
import json
from pathlib import Path
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

    def refusal(self, code, text):
        calls = []

        def docker(*command, timeout=120, check_exit=True):
            calls.append(list(command))
            return code, text

        with mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check, "remove_container", return_value={"status": "passed", "errors": []}):
            result = extension_check.capture_refusal(dict(HELPER), self.output)
        return result, calls[0]

    def test_tcpdump_must_not_start_without_net_raw(self):
        result, command = self.refusal(1, "exec /usr/bin/tcpdump: operation not permitted\n")
        self.assertEqual(result["status"], "passed")
        self.assertIn("--cap-drop=ALL", command)
        self.assertFalse(any(arg.startswith("--cap-add") for arg in command))
        self.assertEqual(command[command.index("--entrypoint=tcpdump") + 2], "--version")
        for code, text in ((0, "tcpdump version 4.99.3\n"), (1, "tcpdump: no such file\n")):
            with self.subTest(text=text):
                result, _ = self.refusal(code, text)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", "quick_failure"))

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
