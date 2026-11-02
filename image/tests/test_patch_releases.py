"""Freeze discovery inputs without changing production acceptance releases."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import patch_releases as patches
import releases


class PatchReleasesTest(unittest.TestCase):
    def test_inventory_and_production_are_distinct(self):
        self.assertEqual(len(patches.PATCHES), 14)
        self.assertEqual(sum(len(patches.expected_cohorts(name)) for name in patches.PATCHES), 79)
        self.assertEqual(set(releases.RELEASES), {"19.2.5", "20.2.4"})
        for name in ("19.2.0", "19.2.1", "19.2.2", "19.2.3"):
            self.assertNotIn(("debian", "arm64"), patches.expected_cohorts(name))
            self.assertIn("arm64", patches.release(name)["debian"]["unavailable"])
            self.assertEqual(patches.build_args(name, "debian")["CEPH_PACKAGE_VERSION"], name + "-1~bpo12+1")
        self.assertEqual(len(patches.expected_cohorts("21.2.0")), 6)
        self.assertNotIn(("ubuntu", "arm64"), patches.expected_cohorts("19.2.6"))
        evidence = patches.release("19.2.6")["ubuntu"]["unavailable_evidence"]["arm64"]
        self.assertEqual(evidence["classification"], "upstream_metadata_artifact_mismatch")
        self.assertNotEqual(evidence["expected"]["sha256"], evidence["observed"]["sha256"])
        self.assertNotEqual(evidence["expected"]["size"], evidence["observed"]["size"])

    def test_exact_package_repository_selection(self):
        for name, suite in (("19.2.0", "jammy"), ("20.2.0", "jammy"),
                            ("20.2.3", "jammy"), ("20.2.4", "noble"), ("21.2.0", "noble")):
            args = patches.build_args(name, "ubuntu")
            self.assertEqual(args["CEPH_PACKAGE_VERSION"], name + "-1" + suite)
            self.assertEqual(args["CEPH_RELEASE"], name)
            self.assertEqual(args["CEPH_SUITE"], suite)
        for variant in ("debian", "ubuntu"):
            args = patches.build_args("21.2.0", variant)
            self.assertEqual(args["CEPH_MGR_PACKAGES"],
                             "ceph-mgr-mirroring=" + args["CEPH_PACKAGE_VERSION"])

    def test_selection_is_bounded_and_duplicate_free(self):
        self.assertEqual(patches.selected("all"), list(patches.PATCHES))
        self.assertEqual(patches.selected("21.2.0,19.2.0"), ["21.2.0", "19.2.0"])
        for bad in ("", "19.2.8", "19.2.0,19.2.0", "21.2.0;true", "latest"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                patches.selected(bad)

    def test_manifest_rejects_unpinned_mixed_or_silently_missing_inputs(self):
        baseline = {"schema": 1, "releases": {"19.2.0": copy.deepcopy(patches.release("19.2.0"))}}
        edits = (
            lambda e: e["official"].update(source="quay.io/ceph/ceph:v19.2.0"),
            lambda e: e["official"].update(source=e["official"]["source"].replace("v19.2.0@", "v20.2.4@")),
            lambda e: e["official"].update(children={}),
            lambda e: e["debian"].update(package="20.2.4-1bookworm"),
            lambda e: e["debian"].update(unavailable={}),
            lambda e: e["ubuntu"].update(architectures=["amd64", "amd64"]),
            lambda e: e["ubuntu"].update(mgr_packages="ceph-mgr-mirroring"),
            lambda e: e["ubuntu"].update(mgr_packages="ceph-mgr-mirroring=21.2.0-1noble"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inputs.json"
            for edit in edits:
                data = copy.deepcopy(baseline)
                edit(data["releases"]["19.2.0"])
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    patches.load(path)


if __name__ == "__main__":
    unittest.main()
