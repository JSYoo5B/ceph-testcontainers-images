"""Source pointers must remain accurate across EL9 and EL10 source images."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package_roles


class SourceNoticeTest(unittest.TestCase):
    def test_notice_does_not_assign_all_official_sources_to_el9(self):
        image = "quay.io/ceph/ceph@sha256:" + "a" * 64
        commit = "b" * 40
        notice = package_roles.source_notice(image, "ceph version 21.2.0 (" + commit + ") umbrella (stable)")
        self.assertIn(image, notice)
        self.assertIn("https://download.ceph.com/rpm-21.2.0/", notice)
        self.assertIn("https://github.com/ceph/ceph/tree/" + commit, notice)
        self.assertIn("Rocky Linux", notice)
        self.assertNotIn("/el9/", notice)
        self.assertNotIn("/9-stream/", notice)


if __name__ == "__main__":
    unittest.main()
