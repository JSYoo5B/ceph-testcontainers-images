from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import releases

ROOT = Path(__file__).resolve().parents[2]


class ReleaseTests(unittest.TestCase):
    def test_every_release_pins_immutable_inputs_for_its_own_version(self):
        self.assertIn(releases.DEFAULT, releases.RELEASES)
        for name, release in releases.RELEASES.items():
            self.assertRegex(release["official"], r"^quay\.io/ceph/ceph:v" + re.escape(name) + r"@sha256:[0-9a-f]{64}$")
            for distribution in releases.DISTRIBUTIONS:
                values = release[distribution]
                self.assertRegex(values["base"], r"^" + re.escape(values["base_name"]) + r"@sha256:[0-9a-f]{64}$")
                self.assertEqual(values["package"], name + "-1" + values["suite"])

    def test_build_args_select_the_release_repository(self):
        args = releases.build_args("19.2.5", "ubuntu")
        self.assertEqual((args["CEPH_RELEASE"], args["CEPH_SUITE"], args["CEPH_PACKAGE_VERSION"]),
                         ("19.2.5", "jammy", "19.2.5-1jammy"))
        with self.assertRaises(KeyError):
            releases.build_args("18.2.0", "debian")

    def test_dockerfile_defaults_match_the_default_release(self):
        for distribution in releases.DISTRIBUTIONS:
            dockerfile = (ROOT / "image" / distribution / "Dockerfile").read_text()
            for key, value in releases.build_args(releases.DEFAULT, distribution).items():
                self.assertIn("ARG " + key + "=" + value, dockerfile)


if __name__ == "__main__":
    unittest.main()
