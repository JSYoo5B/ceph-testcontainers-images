import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import publish


def digest(number):
    return "sha256:" + ("%064x" % number)


def extension_report(architecture="arm64"):
    targets = {reference: {"status": "passed", "cleanup": {"status": "passed", "leftovers": []}}
               for reference in publish.extension_targets()}
    return {"status": "passed", "level": "functional", "extension": "netem", "contract": "1",
            "checker_sha256": publish.extensions().checker_sha256(),
            "image": {"image_id": digest(50), "status": "passed", "platform": "linux/" + architecture,
                      "cleanup": {"status": "passed"},
                      "labels": {"io.ceph-testcontainers.extension": "netem",
                                 "io.ceph-testcontainers.extension.contract": "1"}},
            "kernel": {"status": "passed", "kernel": "6.8.0", "cleanup": {"status": "passed"}},
            "environment": {"OperatingSystem": "Ubuntu 24.04"}, "targets": targets,
            "selectors": {profile: copy.deepcopy(targets) for profile in publish.extensions().SELECTORS},
            "rate": {"status": "passed", "scope": "functional", "targets": copy.deepcopy(targets)},
            "packet_faults": {"status": "passed", "scope": "functional", "targets": copy.deepcopy(targets)}}


def extension_candidates(directory, architectures=publish.ARCHITECTURES, run_id="123-1"):
    for n, architecture in enumerate(architectures):
        publish.save(directory / architecture / "extension-candidate.json", {
            "status": "passed", "extension": "netem", "architecture": architecture, "revision": "a" * 40,
            "run_id": run_id, "image": {"extension": "netem", "architecture": architecture, "image_id": digest(60 + n),
                                        "config_digest": digest(60 + n), "digest": digest(70 + n),
                                        "rootfs_diff_ids": [digest(100)]}})



class ExtensionPublicationTests(unittest.TestCase):

    def test_extension_reports_need_functional_contract_labels_and_every_release_target(self):
        good = extension_report()
        publish.validate_extension_report(good, "netem", "arm64")
        bad = []
        value = copy.deepcopy(good); value["level"] = "quick"; bad.append(value)
        value = copy.deepcopy(good); value["contract"] = "2"; bad.append(value)
        value = copy.deepcopy(good); value["checker_sha256"] = "0" * 64; bad.append(value)
        value = copy.deepcopy(good); value["image"]["labels"] = {}; bad.append(value)
        value = copy.deepcopy(good); value["kernel"]["status"] = "failed"; bad.append(value)
        value = copy.deepcopy(good); value["kernel"]["cleanup"]["status"] = "failed"; bad.append(value)
        value = copy.deepcopy(good); del value["targets"][publish.releases.RELEASES["19.2.5"]["official"]]; bad.append(value)
        value = copy.deepcopy(good); del value["targets"][publish.releases.published_role("20.2.4", "official", "essential")]; bad.append(value)
        value = copy.deepcopy(good)
        next(iter(value["targets"].values()))["cleanup"]["leftovers"] = ["ceph-extension-check-x"]; bad.append(value)
        # Every selector profile of the contract, in every release target.
        value = copy.deepcopy(good); del value["selectors"]; bad.append(value)
        value = copy.deepcopy(good); del value["selectors"]["multi-peer-u32"]; bad.append(value)
        value = copy.deepcopy(good); del value["selectors"]["ipv6-tcp-u32"][publish.releases.published_role("19.2.5", "official", "essential")]
        bad.append(value)
        value = copy.deepcopy(good)
        next(iter(value["selectors"]["ipv4-tcp-u32"].values()))["status"] = "failed"; bad.append(value)
        value = copy.deepcopy(good)
        next(iter(value["selectors"]["multi-peer-u32"].values()))["cleanup"]["leftovers"] = ["x"]; bad.append(value)
        # The bandwidth and packet-fault profiles, functionally, in every release target.
        for key in ("rate", "packet_faults"):
            value = copy.deepcopy(good); del value[key]; bad.append(value)
            value = copy.deepcopy(good); value[key]["scope"] = "parser"; bad.append(value)
            value = copy.deepcopy(good); value[key]["status"] = "failed"; bad.append(value)
            value = copy.deepcopy(good); del value[key]["targets"][publish.releases.published_role("20.2.4", "official", "essential")]
            bad.append(value)
            value = copy.deepcopy(good)
            next(iter(value[key]["targets"].values()))["cleanup"]["leftovers"] = ["x"]; bad.append(value)
        for value in bad:
            with self.assertRaises(publish.PublishError):
                publish.validate_extension_report(value, "netem", "arm64")
        with self.assertRaises(publish.PublishError):
            publish.validate_extension_report(good, "netem", "amd64")

    def test_stage_extension_rejects_failed_report_before_any_docker_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); value = extension_report(); value["status"] = "failed"
            publish.save(root / "report.json", value)
            args = SimpleNamespace(report=root / "report.json", extension="netem", architecture="arm64")
            with mock.patch.object(publish, "run") as run, self.assertRaises(publish.PublishError):
                publish.stage_extension(args)
            run.assert_not_called()

    def test_stage_extension_uploads_the_tested_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); publish.save(root / "report.json", extension_report())
            local = {"Id": digest(50), "Os": "linux", "Architecture": "arm64", "RootFS": {"Layers": [digest(100)]},
                     "Config": {"Labels": {"io.ceph-testcontainers.extension": "netem"}}}
            calls = []
            def run(command):
                calls.append(command)
                return json.dumps([local]) if command[:3] == ["docker", "image", "inspect"] else ""
            args = SimpleNamespace(report=root / "report.json", extension="netem", architecture="arm64",
                                   revision="a" * 40, run_id="123-1", output=root / "extension-candidate.json")
            with mock.patch.object(publish, "run", side_effect=run), \
                    mock.patch.object(publish, "manifest", return_value={"digest": digest(70)}), \
                    mock.patch.object(publish, "verify_image") as verify:
                publish.stage_extension(args)
            self.assertIn(["docker", "tag", digest(50), publish.REGISTRY + ":ci-123-1-extension-netem-v1-linux-arm64"], calls)
            saved = json.loads(args.output.read_text())
            self.assertEqual((saved["status"], saved["image"]["digest"]), ("passed", digest(70)))
            self.assertEqual(verify.call_args.args[0]["extension"], "netem")

    def test_uploaded_helper_must_carry_its_extension_label(self):
        entry = {"digest": digest(70), "config_digest": digest(60), "architecture": "arm64",
                 "extension": "netem", "rootfs_diff_ids": [digest(100)]}
        image = {"os": "linux", "architecture": "arm64", "rootfs": {"diff_ids": [digest(100)]},
                 "config": {"Labels": {"io.ceph-testcontainers.extension": "netem"}}}
        with mock.patch.object(publish, "raw_manifest", return_value={"config": {"digest": digest(60)}}), \
                mock.patch.object(publish, "run", return_value=json.dumps(image)):
            publish.verify_image(entry)
        image["config"]["Labels"] = {"io.ceph-testcontainers.role": "essential"}
        with mock.patch.object(publish, "raw_manifest", return_value={"config": {"digest": digest(60)}}), \
                mock.patch.object(publish, "run", return_value=json.dumps(image)), self.assertRaises(publish.PublishError):
            publish.verify_image(entry)

    def test_extension_promotion_needs_both_platforms_from_one_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            extension_candidates(root / "one", architectures=("arm64",))
            extension_candidates(root / "two")
            with self.assertRaises(publish.PublishError):
                publish.collect_extension_candidates(root / "one", "a" * 40, "123-1")
            self.assertEqual(len(publish.collect_extension_candidates(root / "two", "a" * 40, "123-1")), 2)
            with self.assertRaises(publish.PublishError):
                publish.collect_extension_candidates(root / "two", "a" * 40, "123-2")

    def test_extension_promotion_uses_only_verified_digests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); extension_candidates(root / "candidates")
            observed = {}
            def run(command):
                observed[command[command.index("--tag") + 1]] = command[command.index("--tag") + 2:]
                return ""
            def manifest(tag):
                references = observed.get(tag)
                return {"digest": references[0].split("@", 1)[1] if references and len(references) == 1 else digest(999)}
            def raw(tag):
                return {"manifests": [{"platform": {"os": "linux", "architecture": architecture},
                                       "digest": reference.split("@", 1)[1]}
                                      for architecture, reference in zip(publish.ARCHITECTURES, observed[tag])]}
            args = SimpleNamespace(candidates=root / "candidates", revision="a" * 40, run_id="123-1",
                                   output=root / "promotion.json", github_output=root / "outputs")
            with mock.patch.object(publish, "run", side_effect=run), mock.patch.object(publish, "manifest", side_effect=manifest), \
                    mock.patch.object(publish, "raw_manifest", side_effect=raw), \
                    mock.patch.object(publish, "previous_manifest", return_value=None), \
                    mock.patch.object(publish, "verify_image") as verify:
                publish.promote_extension(args)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(set(observed), {publish.extension_tag("netem"), publish.extension_tag("netem", "amd64"),
                                             publish.extension_tag("netem", "arm64")})
            self.assertEqual(observed[publish.extension_tag("netem")],
                             [publish.REGISTRY + "@" + digest(70), publish.REGISTRY + "@" + digest(71)])
            self.assertEqual(json.loads(args.output.read_text())["images"], {"netem": {"amd64": digest(70), "arm64": digest(71)}})
            self.assertEqual(publish.extension_tag("netem"), publish.REGISTRY + ":extension-netem-v1")

    def test_extension_health_reports_a_helper_tag_without_both_platforms(self):
        def raw(reference):
            if "@" in reference:
                return {"config": {}}
            return {"manifests": [{"platform": {"os": "linux", "architecture": "arm64"}, "digest": digest(2)}]}
        with mock.patch.object(publish, "raw_manifest", side_effect=raw), mock.patch("sys.stdout"), \
                mock.patch("sys.stderr") as stderr, self.assertRaises(publish.PublishError):
            publish.extension_health(SimpleNamespace())
        self.assertIn("extension-netem-v1", "".join(str(call) for call in stderr.write.call_args_list))

    def test_release_health_does_not_import_the_extension_checker(self):
        script = """import sys
sys.path.insert(0, sys.argv[1])
from types import SimpleNamespace
import publish
indexes = {publish.releases.published_role(r,v,k) for r in publish.releases.RELEASES for v in publish.VARIANTS for k in publish.active_roles()}
publish.raw_manifest = lambda reference: {"config": {}} if reference not in indexes else {"manifests": [
    {"platform": {"os": "linux", "architecture": a}, "digest": "sha256:" + "1" * 64} for a in ("amd64", "arm64")]}
publish.health(SimpleNamespace(releases=list(publish.releases.RELEASES)))
print("extension_check" in sys.modules)
"""
        loaded = subprocess.run([sys.executable, "-c", script, str(Path(publish.__file__).parent)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(loaded.stdout.strip().splitlines()[-1], "False")

    def test_release_publication_does_not_import_the_extension_checker(self):
        loaded = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import publish; "
                                 "print('extension_check' in sys.modules)", str(Path(publish.__file__).parent)],
                                capture_output=True, text=True, check=True)
        self.assertEqual(loaded.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
