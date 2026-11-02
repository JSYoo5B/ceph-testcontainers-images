"""Discovery namespace controls; Docker doubles observe publication commands only."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish
from test_publish import candidates, digest, report


RELEASE = "19.2.0"
ALL_COHORTS = {(v, a) for v in publish.VARIANTS for a in publish.ARCHITECTURES}


def quick_report():
    value = report()
    value.update(schema=4, level="quick", scenarios="not_requested", functional={}, requested_scenarios=[])
    for image in value["images"].values():
        image["versions"] = {"ceph": {"release": RELEASE, "commit": "b" * 40}}
        image["checks"] = [{"name": "native-contract", "status": "passed"}]
    return value


def discovery_candidates(root, cohorts=ALL_COHORTS):
    candidates(root, RELEASE)
    for path in root.rglob("candidate.json"):
        value = json.loads(path.read_text())
        if (value["variant"], value["architecture"]) not in cohorts:
            path.unlink()
            continue
        value.update(schema=1, channel=publish.DISCOVERY, qualification=publish.DISCOVERY_QUALIFICATION)
        for entry in value["images"].values():
            entry["native_versions"] = {"ceph": {"release": RELEASE, "commit": "b" * 40}}
        publish.save(path, value)


class DiscoveryPublicationTests(unittest.TestCase):
    def setUp(self):
        # Only the committed inventory seam is substituted. Registry commands
        # below are separate explicit doubles; no fixture claims native validity.
        self.cohorts = ALL_COHORTS
        inventory = SimpleNamespace(release=self.release, expected_cohorts=lambda _: self.cohorts)
        patch = mock.patch.dict(sys.modules, patch_releases=inventory)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def release(name):
        if name != RELEASE:
            raise KeyError("not in inventory")
        return {}

    def test_quick_qualification_does_not_relax_production(self):
        value = quick_report()
        publish.validate_discovery_report(value, "arm64", RELEASE)
        with self.assertRaises(publish.PublishError):
            publish.validate_report(value, "arm64", RELEASE)
        with self.assertRaises(argparse.ArgumentTypeError):
            publish.discovery_release("21.2.99")

    def test_discovery_rejects_missing_failed_skipped_or_foreign_evidence(self):
        good = quick_report()
        edits = [lambda v: v.update(schema=3), lambda v: v.update(level="full"),
                 lambda v: v.update(status="failed"), lambda v: v.update(preflight="failed"),
                 lambda v: v.update(functional={"mixed": {}}), lambda v: v.update(scenarios="passed"),
                 lambda v: v.update(requested_scenarios=["rados"]),
                 lambda v: v["images"].pop("rgw"), lambda v: v.update(checker_sha256="old"),
                 lambda v: v.update(contract_sha256="old"), lambda v: v.update(functional_sha256="old")]
        for field, value in (("platform", "linux/amd64"), ("image_id", "mutable"),
                             ("checks", []), ("checks", [{"status": "skipped"}]),
                             ("cleanup", {"status": "failed"}), ("versions", {}),
                             ("versions", {"ceph": {"release": "19.2.1", "commit": "b" * 40}}),
                             ("versions", {"ceph": {"release": RELEASE, "commit": "unknown"}}),
                             ("versions", {"ceph": {"release": RELEASE, "commit": "c" * 40}})):
            edits.append(lambda v, f=field, x=value: v["images"]["essential"].update({f: x}))
        for edit in edits:
            value = copy.deepcopy(good)
            edit(value)
            with self.subTest(report=value), self.assertRaises(publish.PublishError):
                publish.validate_discovery_report(value, "arm64", RELEASE)

    def test_ineligible_or_failed_stage_is_refused_before_any_registry_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = quick_report()
            value["status"] = "failed"
            publish.save(root / "report.json", value)
            args = SimpleNamespace(release=RELEASE, variant="official", architecture="arm64", report=root / "report.json")
            with mock.patch.object(publish, "run") as run, self.assertRaises(publish.PublishError):
                publish.stage_discovery(args)
            run.assert_not_called()
            self.cohorts = {("official", "amd64")}
            with mock.patch.object(publish, "run") as run, self.assertRaises(publish.PublishError):
                publish.stage_discovery(args)
            run.assert_not_called()

    def test_stage_uploads_only_tested_ids_to_discovery_candidate_tags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = quick_report()
            publish.save(root / "report.json", value)
            local = {image["image_id"]: {"Id": image["image_id"], "Os": "linux", "Architecture": "arm64",
                     "Config": {"Labels": {"io.ceph-testcontainers.role": role}}, "RootFS": {"Layers": [digest(100)]}}
                     for role, image in value["images"].items()}
            calls = []
            def run(command):
                calls.append(command)
                return json.dumps([local[command[-1]]]) if command[:3] == ["docker", "image", "inspect"] else ""
            args = SimpleNamespace(report=root / "report.json", architecture="arm64", variant="official",
                                   release=RELEASE, revision="a" * 40, run_id="123-1", output=root / "candidate.json")
            with mock.patch.object(publish, "run", side_effect=run), \
                    mock.patch.object(publish, "manifest", return_value={"digest": digest(50)}), \
                    mock.patch.object(publish, "verify_image"):
                publish.stage_discovery(args)
            tagged = [command for command in calls if command[:2] == ["docker", "tag"]]
            self.assertEqual(len(tagged), 6)
            self.assertTrue(all(command[2] in local and ":ci-discovery-" in command[3] for command in tagged))
            saved = json.loads(args.output.read_text())
            self.assertEqual((saved["channel"], saved["qualification"]), ("discovery", "runtime-contract-only"))
            self.assertTrue(all(entry["native_versions"] for entry in saved["images"].values()))

    def test_channel_missing_platform_or_mixed_native_build_cannot_promote(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            discovery_candidates(root)
            self.assertEqual(len(publish.collect_candidates(root, "a" * 40, "123-1", RELEASE, publish.DISCOVERY)), 6)
            with self.assertRaises(publish.PublishError):
                publish.collect_candidates(root, "a" * 40, "123-1", RELEASE)
            path = root / "official-arm64/candidate.json"
            original = json.loads(path.read_text())
            for edit in (lambda v: v.pop("channel"), lambda v: v.update(qualification="fully-qualified"),
                         lambda v: v.update(revision="c" * 40), lambda v: v.update(run_id="123-2"),
                         lambda v: v["images"]["essential"].pop("native_versions"),
                         lambda v: v["images"]["essential"]["native_versions"]["ceph"].update(commit="c" * 40)):
                value = copy.deepcopy(original)
                edit(value)
                publish.save(path, value)
                with self.assertRaises(publish.PublishError):
                    publish.collect_candidates(root, "a" * 40, "123-1", RELEASE, publish.DISCOVERY)
            path.unlink()
            with self.assertRaises(publish.PublishError):
                publish.collect_candidates(root, "a" * 40, "123-1", RELEASE, publish.DISCOVERY)

    def test_committed_eligibility_controls_partial_indexes_not_a_missing_candidate(self):
        self.cohorts = ALL_COHORTS - {("debian", "arm64")}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            discovery_candidates(root / "candidates", self.cohorts)
            observed = {}
            def run(command):
                tag = command[command.index("--tag") + 1]
                observed[tag] = command[command.index("--tag") + 2:]
                return ""
            def manifest(tag):
                refs = observed.get(tag)
                return {"digest": refs[0].split("@", 1)[1] if refs and len(refs) == 1 else digest(999)}
            def raw(tag):
                architectures = [a for a in publish.ARCHITECTURES
                                 if (next(v for v in publish.VARIANTS if ":discovery-" + v + "-" in tag), a) in self.cohorts]
                return {"manifests": [{"platform": {"os": "linux", "architecture": a}, "digest": ref.split("@", 1)[1]}
                                      for a, ref in zip(architectures, observed[tag])]}
            args = SimpleNamespace(candidates=root / "candidates", revision="a" * 40, run_id="123-1", release=RELEASE,
                                   output=root / "promotion.json", github_output=None)
            with mock.patch.object(publish, "run", side_effect=run), mock.patch.object(publish, "manifest", side_effect=manifest), \
                    mock.patch.object(publish, "raw_manifest", side_effect=raw), mock.patch.object(publish, "verify_image") as verify:
                publish.promote_discovery(args)
            self.assertEqual(verify.call_count, 30)
            self.assertTrue(all(":discovery-" in tag for tag in observed))
            saved = json.loads(args.output.read_text())
            self.assertEqual((len(saved["platforms"]), len(saved["indexes"])), (30, 18))
            with mock.patch.object(publish, "check_tags") as checked:
                publish.discovery_health(SimpleNamespace(promotion=args.output))
            mapping = checked.call_args.args[1]
            self.assertEqual(mapping[publish.discovery_tag(RELEASE, "debian", "essential")], ["amd64"])
            identities = checked.call_args.args[2]
            self.assertEqual(identities[publish.discovery_tag(RELEASE, "debian", "essential")],
                             {("linux", "amd64"): saved["images"]["debian"]["amd64"]["essential"]})
            saved["cohorts"].append({"variant": "debian", "architecture": "arm64"})
            publish.save(args.output, saved)
            with mock.patch.object(publish, "check_tags") as checked, self.assertRaises(publish.PublishError):
                publish.discovery_health(SimpleNamespace(promotion=args.output))
            checked.assert_not_called()


class CommittedDiscoveryInventoryTests(unittest.TestCase):
    def test_an_unavailable_platform_is_neither_required_nor_fabricated(self):
        import patch_releases
        expected = patch_releases.expected_cohorts(RELEASE)
        self.assertEqual(expected, ALL_COHORTS - {("debian", "arm64")})
        self.assertEqual(patch_releases.expected_cohorts("20.2.4"), ALL_COHORTS)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            discovery_candidates(root)
            with self.assertRaises(publish.PublishError):
                publish.collect_candidates(root, "a" * 40, "123-1", RELEASE, publish.DISCOVERY)
            (root / "debian-arm64/candidate.json").unlink()
            self.assertEqual(set(publish.collect_candidates(root, "a" * 40, "123-1", RELEASE,
                                                           publish.DISCOVERY)), expected)
            (root / "official-arm64/candidate.json").unlink()
            with self.assertRaises(publish.PublishError):
                publish.collect_candidates(root, "a" * 40, "123-1", RELEASE, publish.DISCOVERY)

    def test_single_platform_registry_health_still_refuses_extra_or_missing_children(self):
        tag = publish.discovery_tag(RELEASE, "debian", "all")
        child = publish.REGISTRY + "@" + digest(1)
        index = {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": digest(1)}]}
        def raw(reference):
            if reference == tag:
                return index
            if reference == child:
                return {"config": {}}
            raise publish.PublishError("manifest unknown")
        with mock.patch.object(publish, "raw_manifest", side_effect=raw):
            publish.check_tags([tag], {tag: ["amd64"]})
            with self.assertRaises(publish.PublishError):
                publish.check_tags([tag], {tag: ["amd64"]}, {tag: {("linux", "amd64"): digest(99)}})
            index["manifests"].append({"platform": {"os": "linux", "architecture": "arm64"}, "digest": digest(2)})
            with self.assertRaises(publish.PublishError):
                publish.check_tags([tag], {tag: ["amd64"]})
            index["manifests"] = [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": digest(2)}]
            with self.assertRaises(publish.PublishError):
                publish.check_tags([tag], {tag: ["amd64"]})


if __name__ == "__main__":
    unittest.main()
