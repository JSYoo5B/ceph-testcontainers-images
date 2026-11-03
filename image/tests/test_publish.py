import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish

RELEASE = publish.releases.DEFAULT
# The catalogue has no Debian arm64 input for this release.
UNAVAILABLE_RELEASE = "19.2.0"
ALL_COHORTS = {(v, a) for v in publish.VARIANTS for a in publish.ARCHITECTURES}


def digest(number):
    return "sha256:" + ("%064x" % number)


def report(release=RELEASE):
    """A passing image requirement (quick) report: the publication gate."""
    images = {role: {"image_id": digest(n + 1), "status": "passed", "platform": "linux/arm64",
                     "cleanup": {"status": "passed"}, "versions": {"ceph": {"release": release}}}
              for n, role in enumerate(publish.check.ROLES)}
    return {"status": "passed", "level": "quick", "preflight": "passed", "scenarios": "not_requested",
            "contract": "roles", "images": images, "functional": {},
            "contract_sha256": hashlib.sha256((publish.check.HERE / "check-runtime.sh").read_bytes()).hexdigest(),
            "functional_sha256": publish.check.functional_sha256(),
            "checker_sha256": hashlib.sha256(Path(publish.check.__file__).read_bytes()).hexdigest()}


def functional_summary(value, status="failed"):
    """An auxiliary functional summary as image/ci.py writes it."""
    return {"schema": 1, "status": status, "gating": False, "runs": {
        "full": {"exit_status": 1 if status == "failed" else 0, "status": status, "outcome": status,
                 "report_sha256": "f" * 64, "topologies": {
                     "mixed": {"status": status, "images": {r: value["images"][r]["image_id"] for r in publish.check.ROLES[:-1]},
                               "scenarios": {"rgw-multisite": status}},
                     "all": {"status": status, "images": {r: value["images"]["all"]["image_id"] for r in publish.check.ROLES[:-1]},
                             "scenarios": {"rgw-multisite": status}}}}}}


def candidates(directory, release=RELEASE, cohorts=ALL_COHORTS):
    for variant, architecture in sorted(cohorts):
        value = {"status": "passed", "release": release, "variant": variant, "architecture": architecture,
                 "revision": "a" * 40, "run_id": "123-1", "contract": "roles", "images": {
                     role: {"role": role, "architecture": architecture, "image_id": digest(n + 1),
                            "config_digest": digest(n + 1), "digest": digest(n + 11),
                            "rootfs_diff_ids": [digest(100)]}
                     for n, role in enumerate(publish.check.ROLES)}}
        publish.save(directory / (variant + "-" + architecture) / "candidate.json", value)


class PublicationTests(unittest.TestCase):
    def test_only_a_passing_requirement_check_gates_publication(self):
        good = report()
        publish.validate_report(good, "arm64", RELEASE)
        bad = []
        for status in ("failed", "running"):
            value = copy.deepcopy(good); value["status"] = status; bad.append(value)
        # A full or scoped functional report is not the gate.
        for level in ("full", "functional-selected"):
            value = copy.deepcopy(good); value["level"] = level; bad.append(value)
        value = copy.deepcopy(good); value["preflight"] = "failed"; bad.append(value)
        value = copy.deepcopy(good); del value["images"]["rgw"]; bad.append(value)
        value = copy.deepcopy(good); value["images"]["client"]["status"] = "failed"; bad.append(value)
        value = copy.deepcopy(good); value["images"]["essential"]["cleanup"]["status"] = "failed"; bad.append(value)
        value = copy.deepcopy(good); value["contract"] = "other"; bad.append(value)
        for value in bad:
            with self.subTest(report=value), self.assertRaises(publish.PublishError):
                publish.validate_report(value, "arm64", RELEASE)

    def test_functional_results_are_not_required_by_the_gate(self):
        value = report()
        value["functional"] = {"mixed": {"status": "failed", "scenarios": {"rgw-multisite": {"status": "failed"}}}}
        publish.validate_report(value, "arm64", RELEASE)
        value = report(); value["functional_sha256"] = "old"
        publish.validate_report(value, "arm64", RELEASE)

    def test_wrong_platform_release_and_checker_block_publication(self):
        for mutation in (lambda v: v["images"]["essential"].update(platform="linux/amd64"),
                         lambda v: v["images"]["essential"]["versions"]["ceph"].update(release="old"),
                         lambda v: v.update(checker_sha256="old"),
                         lambda v: v.update(contract_sha256="old")):
            value = report(); mutation(value)
            with self.assertRaises(publish.PublishError):
                publish.validate_report(value, "arm64", RELEASE)
        # A report for the default release cannot publish another release.
        with self.assertRaises(publish.PublishError):
            publish.validate_report(report(), "arm64", "19.2.5")

    def test_stage_rejects_failed_report_before_any_docker_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); value = report(); value["status"] = "failed"
            publish.save(root / "check.json", value)
            args = SimpleNamespace(report=root / "check.json", variant="official", architecture="arm64", release=RELEASE)
            with mock.patch.object(publish, "run") as run, self.assertRaises(publish.PublishError):
                publish.stage(args)
            run.assert_not_called()
            # A missing gate report blocks staging, whatever the functional outcome.
            args = SimpleNamespace(report=root / "missing.json", variant="official", architecture="arm64",
                                   release=RELEASE, functional_summary=None)
            with mock.patch.object(publish, "run") as run, self.assertRaises(OSError):
                publish.stage(args)
            run.assert_not_called()
            # A cohort that the catalogue marks unavailable is refused even with a passing report.
            publish.save(root / "check.json", report(UNAVAILABLE_RELEASE))
            args = SimpleNamespace(report=root / "check.json", variant="debian", architecture="arm64",
                                   release=UNAVAILABLE_RELEASE)
            with mock.patch.object(publish, "run") as run, self.assertRaisesRegex(publish.PublishError, "unavailable"):
                publish.stage(args)
            run.assert_not_called()

    def stage(self, root, value, summary_path):
        local = {i["image_id"]: {"Id": i["image_id"], "Os": "linux", "Architecture": "arm64",
                 "Config": {"Labels": {"io.ceph-testcontainers.role": role}}, "RootFS": {"Layers": [digest(100)]}}
                 for role, i in value["images"].items()}
        calls = []
        def run(command):
            calls.append(command)
            if command[:3] == ["docker", "image", "inspect"]:
                return json.dumps([local[command[-1]]])
            return ""
        def manifest(tag):
            role = next(role for role in sorted(publish.check.ROLES,key=len,reverse=True) if tag.rsplit("-linux-",1)[0].endswith("-"+role))
            return {"digest": digest(publish.check.ROLES.index(role) + 11)}
        args = SimpleNamespace(report=root / "check.json", architecture="arm64", variant="official",
                               release=RELEASE, revision="a" * 40, run_id="123-1", output=root / "candidate.json",
                               functional_summary=summary_path)
        with mock.patch.object(publish, "run", side_effect=run), mock.patch.object(publish, "manifest", side_effect=manifest), \
                mock.patch.object(publish, "verify_image"), mock.patch("sys.stdout"):
            publish.stage(args)
        return calls, json.loads(args.output.read_text())

    def test_stage_tags_tested_ids_and_distinguishes_config_ids_from_manifest_digests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); value = report(); publish.save(root / "check.json", value)
            calls, saved = self.stage(root, value, None)
            tagged = [c[2] for c in calls if c[:2] == ["docker", "tag"]]
            self.assertEqual(tagged, [value["images"][r]["image_id"] for r in sorted(publish.check.ROLES)])
            self.assertEqual((saved["status"], saved["gate"], saved["functional"]), ("passed", "quick", {"status": "not_recorded"}))
            self.assertEqual(saved["report_sha256"], hashlib.sha256((root / "check.json").read_bytes()).hexdigest())
            for image in saved["images"].values():
                self.assertEqual(image["config_digest"], image["image_id"])
                self.assertNotEqual(image["digest"], image["image_id"])

    def test_a_failing_or_unreadable_functional_result_is_recorded_and_does_not_block_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); value = report(); publish.save(root / "check.json", value)
            publish.save(root / "functional-summary.json", functional_summary(value))
            _, saved = self.stage(root, value, root / "functional-summary.json")
            functional = saved["functional"]
            self.assertEqual((saved["status"], functional["status"], functional["images_match"]), ("passed", "failed", True))
            self.assertEqual(functional["summary_sha256"],
                             hashlib.sha256((root / "functional-summary.json").read_bytes()).hexdigest())
            self.assertEqual(functional["runs"]["full"]["topologies"]["all"]["scenarios"]["rgw-multisite"], "failed")
            self.assertEqual(functional["runs"]["full"]["report_sha256"], "f" * 64)
            # Functional runs of other images are recorded as such.
            other = functional_summary(value, "passed")
            other["runs"]["full"]["topologies"]["mixed"]["images"]["rgw"] = digest(99)
            publish.save(root / "functional-summary.json", other)
            _, saved = self.stage(root, value, root / "functional-summary.json")
            self.assertEqual((saved["functional"]["status"], saved["functional"]["images_match"]), ("passed", False))
            for content in (None, "not json", "[]"):
                path = root / "broken.json"
                path.unlink(missing_ok=True)
                if content is not None:
                    path.write_text(content)
                with self.subTest(content=content):
                    _, saved = self.stage(root, value, path)
                    self.assertEqual((saved["status"], saved["functional"]["status"]), ("passed", "unreadable"))

    def test_uploaded_config_platform_and_layers_must_match_tested_image(self):
        entry = {"digest": digest(11), "config_digest": digest(1), "architecture": "arm64",
                 "role": "essential", "rootfs_diff_ids": [digest(100)]}
        image = {"os": "linux", "architecture": "arm64", "rootfs": {"diff_ids": [digest(100)]},
                 "config": {"Labels": {"io.ceph-testcontainers.role": "essential"}}}
        with mock.patch.object(publish, "raw_manifest", return_value={"config": {"digest": digest(1)}}), \
                mock.patch.object(publish, "run", return_value=json.dumps(image)):
            publish.verify_image(entry)
        for field, replacement in (("architecture", "amd64"), ("rootfs", {"diff_ids": [digest(101)]})):
            wrong = dict(image, **{field: replacement})
            with mock.patch.object(publish, "raw_manifest", return_value={"config": {"digest": digest(1)}}), \
                    mock.patch.object(publish, "run", return_value=json.dumps(wrong)), self.assertRaises(publish.PublishError):
                publish.verify_image(entry)
        with mock.patch.object(publish, "raw_manifest", return_value={"config": {"digest": digest(2)}}), \
                mock.patch.object(publish, "run") as run, self.assertRaises(publish.PublishError):
            publish.verify_image(entry)
        run.assert_not_called()

    def test_incomplete_wrong_run_and_wrong_revision_candidates_cannot_promote(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); candidates(root)
            self.assertEqual(len(publish.collect_candidates(root, "a" * 40, "123-1", RELEASE)), 6)
            for revision, run_id, release in (("b" * 40, "123-1", RELEASE), ("a" * 40, "123-2", RELEASE),
                                              ("a" * 40, "123-1", "19.2.5")):
                with self.assertRaises(publish.PublishError):
                    publish.collect_candidates(root, revision, run_id, release)
            (root / "official-arm64/candidate.json").unlink()
            args = SimpleNamespace(candidates=root, revision="a" * 40, run_id="123-1", release=RELEASE)
            with mock.patch.object(publish, "run") as run, self.assertRaises(publish.PublishError):
                publish.promote(args)
            run.assert_not_called()

    def test_promotion_uses_only_verified_digests_for_all_role_tags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); candidates(root / "candidates")
            observed = {}; calls = []
            def run(command):
                calls.append(command)
                tag = command[command.index("--tag") + 1]
                references = command[command.index("--tag") + 2:]
                observed[tag] = references
                return ""
            def manifest(tag):
                references = observed.get(tag)
                return {"digest": references[0].split("@", 1)[1] if references and len(references) == 1 else digest(999)}
            def raw(tag):
                return {"manifests": [{"platform": {"os": "linux", "architecture": architecture}, "digest": reference.split("@", 1)[1]}
                                      for architecture, reference in zip(publish.ARCHITECTURES, observed[tag])]}
            for name, functional in (("ubuntu-arm64", {"status": "failed", "runs": {}}),
                                     ("official-amd64", {"status": "passed", "runs": {}}), ("debian-amd64", "garbage")):
                path = root / "candidates" / name / "candidate.json"
                value = json.loads(path.read_text()); value["functional"] = functional; publish.save(path, value)
            args = SimpleNamespace(candidates=root / "candidates", revision="a" * 40, run_id="123-1", release=RELEASE,
                                   output=root / "promotion.json", github_output=root / "outputs")
            with mock.patch.object(publish, "run", side_effect=run), mock.patch.object(publish, "manifest", side_effect=manifest), \
                    mock.patch.object(publish, "raw_manifest", side_effect=raw), mock.patch.object(publish, "verify_image") as verify, \
                    mock.patch("sys.stdout"):
                publish.promote(args)
            self.assertEqual(verify.call_count, 36)
            self.assertEqual(len(calls), 54)
            self.assertTrue(all(all('@sha256:' in ref for ref in refs) for refs in observed.values()))
            saved = json.loads(args.output.read_text())
            self.assertEqual((saved["status"], len(saved["platforms"]), len(saved["indexes"])), ("passed", 36, 18))
            # A failing functional outcome promotes and is carried into the receipt.
            self.assertEqual(saved["functional"]["ubuntu"]["arm64"]["status"], "failed")
            self.assertEqual(saved["functional"]["official"]["amd64"]["status"], "passed")
            self.assertEqual(saved["functional"]["debian"]["amd64"], {"status": "not_recorded"})
            self.assertTrue(args.github_output.read_text().startswith("images="))


    def test_first_publication_of_a_release_has_no_previous_tags(self):
        with mock.patch.object(publish, "manifest", side_effect=publish.PublishError("ERROR: ghcr.io/x: not found")):
            self.assertIsNone(publish.previous_manifest("ghcr.io/x"))
        with mock.patch.object(publish, "manifest", side_effect=publish.PublishError("unauthorized")), \
                self.assertRaises(publish.PublishError):
            publish.previous_manifest("ghcr.io/x")
        self.assertEqual(publish.release_tag("19.2.5", "ubuntu", "essential", "arm64"),
                         publish.REGISTRY + ":ubuntu-19.2.5-essential-linux-arm64")


    def test_health_reports_release_tags_whose_platform_images_were_deleted(self):
        present = {publish.REGISTRY + "@" + digest(n) for n in (1, 2)}
        indexes = {publish.releases.published_role(RELEASE,v,r) for v in publish.VARIANTS for r in publish.active_roles()}
        def raw(reference):
            if reference not in indexes:
                if reference not in present:
                    raise publish.PublishError("ERROR: " + reference + ": not found")
                return {"config": {}}
            return {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": digest(1)},
                                  {"platform": {"os": "linux", "architecture": "arm64"}, "digest": digest(2)}]}
        args = SimpleNamespace(releases=[RELEASE])
        with mock.patch.object(publish, "raw_manifest", side_effect=raw), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            publish.health(args)
        present.discard(publish.REGISTRY + "@" + digest(2))
        with mock.patch.object(publish, "raw_manifest", side_effect=raw), mock.patch("sys.stdout"), \
                mock.patch("sys.stderr") as stderr, self.assertRaises(publish.PublishError):
            publish.health(args)
        self.assertIn("linux/arm64 image", "".join(str(call) for call in stderr.write.call_args_list))

    def test_only_available_catalogued_cohorts_are_required_for_promotion(self):
        expected = publish.releases.cohorts(UNAVAILABLE_RELEASE)
        self.assertEqual(expected, ALL_COHORTS - {("debian", "arm64")})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); candidates(root, UNAVAILABLE_RELEASE)
            # A candidate for an unavailable cohort is foreign, not an extra child.
            with self.assertRaises(publish.PublishError):
                publish.collect_candidates(root, "a" * 40, "123-1", UNAVAILABLE_RELEASE)
            (root / "debian-arm64/candidate.json").unlink()
            self.assertEqual(set(publish.collect_candidates(root, "a" * 40, "123-1", UNAVAILABLE_RELEASE)), expected)
            # A failed available cohort cannot be omitted to make the release publish.
            (root / "official-arm64/candidate.json").unlink()
            with self.assertRaises(publish.PublishError):
                publish.collect_candidates(root, "a" * 40, "123-1", UNAVAILABLE_RELEASE)

    def test_promotion_without_an_unavailable_child_and_its_health(self):
        cohorts = publish.releases.cohorts(UNAVAILABLE_RELEASE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); candidates(root / "candidates", UNAVAILABLE_RELEASE, cohorts)
            observed = {}
            def run(command):
                tag = command[command.index("--tag") + 1]
                observed[tag] = command[command.index("--tag") + 2:]
                return ""
            def manifest(tag):
                refs = observed.get(tag)
                return {"digest": refs[0].split("@", 1)[1] if refs and len(refs) == 1 else digest(999)}
            def raw(tag):
                variant = next(v for v in publish.VARIANTS if ":" + v + "-" in tag)
                architectures = [a for a in publish.ARCHITECTURES if (variant, a) in cohorts]
                return {"manifests": [{"platform": {"os": "linux", "architecture": a}, "digest": ref.split("@", 1)[1]}
                                      for a, ref in zip(architectures, observed[tag])]}
            args = SimpleNamespace(candidates=root / "candidates", revision="a" * 40, run_id="123-1",
                                   release=UNAVAILABLE_RELEASE, output=root / "promotion.json", github_output=None)
            with mock.patch.object(publish, "run", side_effect=run), mock.patch.object(publish, "manifest", side_effect=manifest), \
                    mock.patch.object(publish, "raw_manifest", side_effect=raw), mock.patch.object(publish, "verify_image") as verify:
                publish.promote(args)
            self.assertEqual(verify.call_count, 30)
            debian = publish.release_tag(UNAVAILABLE_RELEASE, "debian", "essential")
            self.assertEqual(len(observed[debian]), 1)
            self.assertNotIn(publish.release_tag(UNAVAILABLE_RELEASE, "debian", "essential", "arm64"), observed)
            saved = json.loads(args.output.read_text())
            self.assertEqual((len(saved["platforms"]), len(saved["indexes"])), (30, 18))
            with mock.patch.object(publish, "check_tags") as checked:
                publish.health(SimpleNamespace(promotion=args.output))
            tags, architectures, identities = checked.call_args.args
            self.assertEqual(len(tags), 18)
            self.assertEqual(architectures[debian], ["amd64"])
            self.assertEqual(architectures[publish.release_tag(UNAVAILABLE_RELEASE, "ubuntu", "all")], ["amd64", "arm64"])
            self.assertEqual(identities[debian], {("linux", "amd64"): saved["images"]["debian"]["amd64"]["essential"]})
            # A receipt that claims the unavailable child is refused before any registry read.
            saved["images"]["debian"]["arm64"] = saved["images"]["debian"]["amd64"]
            publish.save(args.output, saved)
            with mock.patch.object(publish, "check_tags") as checked, self.assertRaises(publish.PublishError):
                publish.health(SimpleNamespace(promotion=args.output))
            checked.assert_not_called()

    def test_single_platform_health_still_refuses_extra_missing_or_foreign_children(self):
        tag = publish.release_tag(UNAVAILABLE_RELEASE, "debian", "all")
        child = publish.REGISTRY + "@" + digest(1)
        index = {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": digest(1)}]}
        def raw(reference):
            if reference == tag:
                return index
            if reference == child:
                return {"config": {}}
            raise publish.PublishError("manifest unknown")
        with mock.patch.object(publish, "raw_manifest", side_effect=raw), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            publish.check_tags([tag], {tag: ["amd64"]})
            with self.assertRaises(publish.PublishError):
                publish.check_tags([tag], {tag: ["amd64"]}, {tag: {("linux", "amd64"): digest(99)}})
            index["manifests"].append({"platform": {"os": "linux", "architecture": "arm64"}, "digest": digest(2)})
            with self.assertRaises(publish.PublishError):
                publish.check_tags([tag], {tag: ["amd64"]})
            index["manifests"] = [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": digest(2)}]
            with self.assertRaises(publish.PublishError):
                publish.check_tags([tag], {tag: ["amd64"]})
