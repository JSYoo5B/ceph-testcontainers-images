import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import releases

ROOT = Path(__file__).resolve().parents[2]
ALL_COHORTS = {(v, a) for v in releases.VARIANTS for a in releases.ARCHITECTURES}


class ReleaseTests(unittest.TestCase):
    def test_every_release_pins_immutable_inputs_for_its_own_version(self):
        self.assertEqual(releases.names()[0], releases.DEFAULT)
        for name, release in releases.RELEASES.items():
            self.assertRegex(releases.official(name), r"^quay\.io/ceph/ceph:v" + re.escape(name) + r"@sha256:[0-9a-f]{64}$")
            for architecture in release["official"]["architectures"]:
                self.assertEqual(releases.official(name, architecture),
                                 "quay.io/ceph/ceph:v" + name + "@" + release["official"]["children"][architecture])
            for distribution in releases.DISTRIBUTIONS:
                values = release[distribution]
                self.assertRegex(values["base"], r"^" + re.escape(values["base_name"]) + r"@sha256:[0-9a-f]{64}$")
                self.assertRegex(values["package"], r"^" + re.escape(name) + r"-1(?:" + values["suite"] + r"|~bpo12\+1)$")

    def test_catalogue_cohorts_and_unavailable_inputs(self):
        self.assertEqual(len(releases.RELEASES), 14)
        self.assertEqual(sum(len(releases.cohorts(name)) for name in releases.RELEASES), 79)
        self.assertEqual(releases.cohorts("20.2.4"), ALL_COHORTS)
        self.assertEqual(releases.cohorts("21.2.0"), ALL_COHORTS)
        for name in ("19.2.0", "19.2.1", "19.2.2", "19.2.3"):
            self.assertEqual(releases.cohorts(name), ALL_COHORTS - {("debian", "arm64")})
            self.assertIn("arm64", releases.release(name)["debian"]["unavailable"])
            self.assertEqual(releases.build_args(name, "debian")["CEPH_PACKAGE_VERSION"], name + "-1~bpo12+1")
        self.assertEqual(releases.cohorts("19.2.6"), ALL_COHORTS - {("ubuntu", "arm64")})
        evidence = releases.release("19.2.6")["ubuntu"]["unavailable_evidence"]["arm64"]
        self.assertEqual(evidence["classification"], "upstream_metadata_artifact_mismatch")
        self.assertNotEqual(evidence["expected"]["sha256"], evidence["observed"]["sha256"])
        self.assertNotEqual(evidence["expected"]["size"], evidence["observed"]["size"])
        with self.assertRaises(KeyError):
            releases.official("21.2.0", "s390x")

    def test_build_args_select_the_release_repository(self):
        args = releases.build_args("19.2.5", "ubuntu")
        self.assertEqual((args["CEPH_RELEASE"], args["CEPH_SUITE"], args["CEPH_PACKAGE_VERSION"], args["CEPH_MGR_PACKAGES"]),
                         ("19.2.5", "jammy", "19.2.5-1jammy", "python3-distutils"))
        for name, suite in (("19.2.0", "jammy"), ("20.2.0", "jammy"),
                            ("20.2.3", "jammy"), ("20.2.4", "noble"), ("21.2.0", "noble")):
            args = releases.build_args(name, "ubuntu")
            self.assertEqual((args["CEPH_RELEASE"], args["CEPH_SUITE"], args["CEPH_PACKAGE_VERSION"]),
                             (name, suite, name + "-1" + suite))
        for variant in releases.DISTRIBUTIONS:
            args = releases.build_args("21.2.0", variant)
            version = args["CEPH_PACKAGE_VERSION"]
            self.assertEqual(args["CEPH_MGR_PACKAGES"],
                             "ceph-mgr-mirroring=" + version + " ceph-mgr-snap-schedule=" + version)
            # Every pinned split MGR package has its pinned archive metadata.
            metadata = releases.release("21.2.0")[variant]["metadata"]["additional_packages"]
            for extra in args["CEPH_MGR_PACKAGES"].split():
                package, _, pinned = extra.partition("=")
                self.assertEqual(metadata[package]["version"], pinned)
        with self.assertRaises(KeyError):
            releases.build_args("18.2.0", "debian")

    def test_build_args_command_prints_shell_words(self):
        output = subprocess.run([sys.executable, str(ROOT / "image/releases.py"), "build-args", "21.2.0", "ubuntu"],
                                capture_output=True, text=True, check=True).stdout
        words = subprocess.run(["sh", "-c", 'eval "set -- $1"; printf "%s\\n" "$@"', "sh", output],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertIn("CEPH_MGR_PACKAGES=ceph-mgr-mirroring=21.2.0-1noble ceph-mgr-snap-schedule=21.2.0-1noble", words)
        self.assertEqual(words.count("--build-arg"), 6)

    def test_dockerfile_defaults_match_the_default_release(self):
        for distribution in releases.DISTRIBUTIONS:
            dockerfile = (ROOT / "image" / distribution / "Dockerfile").read_text()
            for key, value in releases.build_args(releases.DEFAULT, distribution).items():
                self.assertIn("ARG " + key + "=" + value, dockerfile)

    def test_selection_and_plan_list_only_available_cohorts(self):
        self.assertEqual(releases.selected("all"), list(releases.RELEASES))
        self.assertEqual(releases.selected("21.2.0,19.2.0"), ["21.2.0", "19.2.0"])
        for bad in ("", "19.2.8", "19.2.0,19.2.0", "21.2.0;true", "latest"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                releases.selected(bad)
        value = releases.plan("all")
        self.assertEqual(value["releases"], list(releases.RELEASES))
        cells = value["matrix"]["include"]
        self.assertEqual(len(cells), 79)
        self.assertEqual(len({(c["release"], c["variant"], c["architecture"]) for c in cells}), 79)
        self.assertNotIn({"release": "19.2.6", "variant": "ubuntu", "architecture": "arm64",
                          "runner": "ubuntu-24.04-arm"}, cells)
        self.assertTrue(all(c["runner"] == releases.RUNNERS[c["architecture"]] for c in cells))
        self.assertEqual(len(releases.plan(releases.DEFAULT)["matrix"]["include"]), 6)

    def test_published_releases_are_those_of_the_digest_inventory(self):
        self.assertEqual(releases.published()[0], releases.DEFAULT)
        self.assertEqual(set(releases.published()), set(releases.inventory()))
        self.assertTrue(set(releases.published()) <= set(releases.RELEASES))

    def test_catalogue_rejects_unpinned_mixed_or_silently_missing_inputs(self):
        baseline = {"schema": 1, "releases": {name: copy.deepcopy(releases.release(name))
                                              for name in (releases.DEFAULT, "19.2.0")}}
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
            path = Path(directory) / "releases.json"
            path.write_text(json.dumps(baseline))
            self.assertEqual(set(releases.load(path)), {releases.DEFAULT, "19.2.0"})
            for edit in edits:
                data = copy.deepcopy(baseline)
                edit(data["releases"]["19.2.0"])
                path.write_text(json.dumps(data))
                with self.subTest(data=data), self.assertRaises(ValueError):
                    releases.load(path)
            # The default release must be catalogued.
            path.write_text(json.dumps({"schema": 1, "releases": {"19.2.0": baseline["releases"]["19.2.0"]}}))
            with self.assertRaises(ValueError):
                releases.load(path)


if __name__ == "__main__":
    unittest.main()
