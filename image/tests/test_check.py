import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check


class ImageContractTests(unittest.TestCase):
    def test_bad_roles_references_and_duplicates_are_rejected(self):
        for values in (["control"], ["mon=image"], ["all="], ["all= "],
                       ["all=--help"], ["all=first", "all=second"]):
            with self.subTest(values=values), self.assertRaises(check.CheckError):
                check.image_arguments(values)
        self.assertEqual(check.image_arguments(["all=repo/ceph:v1@sha256:abc"]),
                         {"all": "repo/ceph:v1@sha256:abc"})

    def test_partial_roles_cannot_run_full_check(self):
        with self.assertRaises(check.CheckError):
            check.runtime_sets({"control": "control", "osd": "osd"})
        images = {role: role for role in check.ROLES}
        sets = check.runtime_sets(images)
        self.assertEqual(sets["mixed"], {role: role for role in check.ROLES[:-1]})
        self.assertEqual(set(sets["all"].values()), {"all"})

    def test_multiarch_probe_uses_runnable_index_id_without_resolving_tag_twice(self):
        root_id = "sha256:" + "1" * 64
        child_id = "sha256:" + "2" * 64
        original = {"Id": root_id, "Os": "linux", "Architecture": "arm64", "RepoDigests": ["repo@" + root_id]}
        selected = dict(original, Id=child_id)
        with mock.patch.object(check, "run", side_effect=[json.dumps([original]), json.dumps([selected])]) as run:
            image = check.inspect_image("mutable:tag", "linux/arm64")
        self.assertEqual(image["image_id"], root_id)
        self.assertEqual(image["platform_image_id"], child_id)
        self.assertEqual(run.call_args_list[1].args[0][-1], root_id)

    def test_missing_inspect_identity_and_platform_are_reported_as_errors(self):
        for metadata in ([], [{}], [{"Id": "bad", "Os": "linux", "Architecture": "arm64"}]):
            with self.subTest(metadata=metadata), mock.patch.object(check, "run", return_value=json.dumps(metadata)), \
                    self.assertRaises(check.CheckError):
                check.inspect_image("image", None)

    def test_control_import_and_binary_failures_remain_in_report(self):
        checks, versions = check.parse_probe("CHECK\tfailed\tversion:ceph-mon\nCHECK\tfailed\tpython-bindings\n")
        self.assertEqual([item["status"] for item in checks], ["failed", "failed"])
        self.assertEqual(versions, {})

    def test_no_checks_or_version_and_inconsistent_versions_cannot_pass(self):
        for output in ("", "CHECK\tpassed\tshell\n",
                       "CHECK\tpassed\tshell\nVERSION\tceph\tunrecognized\n",
                       "CHECK\tpassed\tshell\nVERSION\tceph\tceph version 20.2.4 (abc)\n"
                       "VERSION\trbd\tceph version 19.2.3 (def)\n"):
            with self.subTest(output=output), self.assertRaises(check.CheckError):
                check.parse_probe(output)

    def test_timeout_removes_the_owned_probe_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            image = {"platform": "linux/arm64", "image_id": "sha256:resolved"}
            args = check.arguments(["--image", "all=mutable"])
            with mock.patch.object(check, "run", return_value="container") as run, \
                    mock.patch.object(check.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                        "docker start", 1, output=b"partial log")), self.assertRaises(check.CheckError):
                check.probe_image("all", image, args, output)
            calls = [call.args[0] for call in run.call_args_list]
            self.assertEqual(calls[0][-4:], ["sha256:resolved", "-s", "--", "all"])
            self.assertEqual(calls[1][:3], ["docker", "rm", "--force"])
            self.assertEqual(calls[1][3], calls[0][3])
            self.assertEqual((output / "all.log").read_text(), "partial log")
            self.assertEqual(image["status"], "failed")

    def test_incomplete_probe_with_exit_zero_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(check, "run", return_value="container"), \
                mock.patch.object(check.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "CHECK\tpassed\tshell\nVERSION\tceph\tceph version 20.2.4 (abc)\n", "")):
            image = {"platform": "linux/arm64", "image_id": "fixed"}
            check.probe_image("control", image, check.arguments(["--image", "control=image"]), Path(temporary))
        self.assertEqual(image["status"], "failed")
        self.assertIn("did not finish", image["error"])

    def test_main_uses_fixed_ids_and_preserves_runtime_failure(self):
        for runtime_fails in (False, True):
            with self.subTest(runtime_fails=runtime_fails), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                def inspect(reference, platform):
                    return {"image_id": "sha256:fixed", "platform": "linux/arm64", "reference": reference}
                def probe(role, image, args, output):
                    image.update({"status": "passed", "versions": {
                        "ceph": {"release": "20.2.4", "commit": "abc"}}})
                def run(images, scenarios, output, log):
                    self.assertEqual(set(images.values()), {"sha256:fixed"})
                    self.assertEqual(tuple(scenarios), check.suite.SCENARIOS)
                    return {name: {"status": "failed" if runtime_fails and name == "rgw-multisite" else "passed"}
                            for name in scenarios}
                with mock.patch.object(check.shutil, "which", return_value="installed"), \
                        mock.patch.object(check, "inspect_image", side_effect=inspect), \
                        mock.patch.object(check, "probe_image", side_effect=probe), \
                        mock.patch.object(check.suite, "run", side_effect=run), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = check.main(["--image", "all=mutable", "--full", "--output-dir", str(output)])
                report = json.loads((output / "check-report.json").read_text())
                expected = "failed" if runtime_fails else "passed"
                self.assertEqual(code, 1 if runtime_fails else 0)
                self.assertEqual(report["level"], "full")
                self.assertEqual(report["scenarios"], expected)
                self.assertEqual(report["functional"]["all"]["status"], expected)
                self.assertEqual(report["functional"]["all"]["failed"], ["rgw-multisite"] if runtime_fails else [])
                self.assertEqual(report["preflight"], "passed")

    def test_mixed_platforms_stop_before_starting_any_container(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(check.shutil, "which", return_value="docker"), \
                mock.patch.object(check, "probe_image") as probe, \
                mock.patch.object(check, "inspect_image", side_effect=[
                    {"platform": "linux/arm64"}, {"platform": "linux/amd64"}]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = check.main(["--image", "control=a", "--image", "osd=b", "--output-dir", temporary])
            report = json.loads((Path(temporary) / "check-report.json").read_text())
        probe.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(report["preflight"], "failed")
        self.assertEqual(report["level"], "quick")
        self.assertEqual(report["scenarios"], "not_requested")

    def test_existing_report_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "check-report.json"
            report.write_text("previous evidence")
            with mock.patch.object(check.shutil, "which", return_value="docker"), \
                    self.assertRaises(check.CheckError):
                check.main(["--image", "all=image", "--output-dir", temporary])
            self.assertEqual(report.read_text(), "previous evidence")

    def test_missing_and_loading_failures_remain_distinct(self):
        checks, _ = check.parse_probe(
            "CHECK\tfailed\tpath:cryptsetup\nFAILURE\tmissing_file\tpath:cryptsetup\n"
            "CHECK\tfailed\tclass-load:hello\nFAILURE\tloading_failure\tclass-load:hello\n")
        self.assertEqual([item["failure_kind"] for item in checks], ["missing_file", "loading_failure"])
        self.assertEqual(checks[1]["failure_stage"], "quick:class-load:hello")

    def test_missing_supported_dependency_fails_without_running_functional(self):
        with tempfile.TemporaryDirectory() as temporary:
            def probe(role, image, args, output):
                image.update(status="failed", checks=[{"name": "path:cryptsetup", "status": "failed",
                    "failure_kind": "missing_file"}], versions={})
            with mock.patch.object(check.shutil, "which", return_value="docker"), \
                    mock.patch.object(check, "inspect_image", return_value={"platform": "linux/arm64", "reference": "image"}), \
                    mock.patch.object(check, "probe_image", side_effect=probe), \
                    mock.patch.object(check.suite, "run") as functional, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = check.main(["--image", "all=image", "--full", "--scenario", "rbd-encryption",
                                   "--output-dir", temporary])
            functional.assert_not_called()
            report = json.loads((Path(temporary) / "check-report.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(report["level"], "functional-selected")
        self.assertEqual(report["scenarios"], "not_run")


if __name__ == "__main__":
    unittest.main()
