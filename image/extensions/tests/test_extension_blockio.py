"""Blockio image admission and help parsing; Docker is substituted explicitly."""
import contextlib
import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check

PROBE = extension_check.EXTENSIONS["blockio"]["probe"]
OUTPUT = "CHECK\tpassed\tcli:dmsetup\nCOMPLETE\tblockio\n"


class BlockioCheckTests(unittest.TestCase):
    def test_quick_only_contract_cannot_enter_published_or_functional_checks(self):
        self.assertNotIn("blockio", extension_check.PUBLISHED)
        self.assertEqual(extension_check.EXTENSIONS["blockio"]["contract"], "1")
        self.assertEqual(extension_check.arguments(["--extension", "blockio", "--image", "helper"]).extension,
                         "blockio")
        for extra in (["--functional", "--target", "ceph"], ["--rate"], ["--packet-faults"],
                      ["--selector", "ipv4-tcp-u32", "--functional", "--target", "ceph"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                extension_check.arguments(["--extension", "blockio", "--image", "helper", *extra])

    def test_probe_cannot_access_host_devices_or_add_capabilities(self):
        commands = []

        def docker(*command, **kwargs):
            commands.append(command)
            return 0, ""

        image = {"image_id": "sha256:" + "1" * 64, "platform": "linux/amd64"}
        # The fake Docker result exercises orchestration only; native quick
        # execution separately proves the user-space image contract.
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout=OUTPUT, stderr="")), \
                mock.patch.object(extension_check, "remove_container", return_value={"status": "passed"}):
            extension_check.probe("blockio", image, mock.Mock(probe_timeout=10), Path(directory))
        create = commands[0]
        self.assertIn("--network=none", create)
        self.assertIn("--cap-drop=ALL", create)
        self.assertIn("--user=0:0", create)
        self.assertFalse(any(x.startswith(("--privileged", "--device", "--mount", "--volume", "--cap-add"))
                             for x in create))
        self.assertEqual(image["status"], "passed")

    def test_failed_or_incomplete_probe_is_not_accepted(self):
        for output, code in ((OUTPUT.replace("passed", "failed"), 1), (OUTPUT.split("COMPLETE")[0], 0)):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(extension_check, "docker", return_value=(0, "")), \
                    mock.patch.object(extension_check.subprocess, "run",
                                      return_value=mock.Mock(returncode=code, stdout=output, stderr="")), \
                    mock.patch.object(extension_check, "remove_container", return_value={"status": "passed"}):
                image = {"image_id": "sha256:" + "1" * 64, "platform": "linux/amd64"}
                extension_check.probe("blockio", image, mock.Mock(probe_timeout=10), Path(directory))
                self.assertEqual(image["status"], "failed")

    def test_help_parser_rejects_a_missing_option_and_a_loader_failure(self):
        function = re.search(r"cli_help\(\) \{.*?\n\}", PROBE.read_text(), re.S).group()
        for help_text, exit_code, expected in (("--find --show", 0, 0), ("--find", 0, 1),
                                                ("--find --show", 127, 1)):
            with self.subTest(help_text=help_text, exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                tool = Path(directory) / "tool"
                args = Path(directory) / "arguments"
                # An executable substitute exposes only help text. Its argument
                # receipt proves that the parser never calls a device command.
                tool.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > '" + str(args) + "'\n"
                                "printf '%s\\n' '" + help_text + "'\nexit " + str(exit_code) + "\n")
                tool.chmod(0o755)
                result = subprocess.run(["sh", "-c", function + '\ncli_help "$1" --find --show', "probe", str(tool)])
                self.assertEqual(result.returncode, expected)
                self.assertEqual(args.read_text(), "--help\n")

    def test_probe_shell_is_valid_and_dmsetup_has_no_kernel_query(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(PROBE)]).returncode, 0)
        script = PROBE.read_text()
        shell = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(shell, r"(?:^|[;&|])\s*dmsetup\s+(?:targets|version|create|remove)\b")
        self.assertNotIn("modprobe", shell)
        self.assertNotRegex(shell, r"/dev/(?!null\b)")


if __name__ == "__main__":
    unittest.main()
