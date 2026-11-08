"""Contract utilities: declared per role, resolved in the source, kept in step with the quick check."""
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contract
import runtime


class UtilityContractTests(unittest.TestCase):
    def test_quick_check_lists_the_contract_utilities(self):
        script = (Path(__file__).resolve().parents[2] / "check-runtime.sh").read_text()
        listed = re.search(r'^cache_utilities="([^"]*)"$', script, re.M)
        self.assertIsNotNone(listed)
        self.assertEqual(set(listed.group(1).split()), contract.UTILITIES["rbd-aux"])

    def test_only_the_cache_role_declares_utilities(self):
        self.assertEqual(set(contract.UTILITIES), {"rbd-aux"})

    def test_utilities_resolve_to_source_paths(self):
        with mock.patch.object(runtime.shutil, "which", lambda name: "/usr/bin/" + name):
            paths = runtime.contract_utility_paths("rbd-aux")
        self.assertEqual(paths, sorted("/usr/bin/" + name for name in contract.UTILITIES["rbd-aux"]))
        self.assertEqual(runtime.contract_utility_paths("client"), [])

    def test_a_utility_the_source_lacks_fails_the_build(self):
        with mock.patch.object(runtime.shutil, "which", lambda name: None if name == "flock" else "/usr/bin/" + name):
            with self.assertRaisesRegex(RuntimeError, "rbd-aux requires utility flock"):
                runtime.contract_utility_paths("rbd-aux")


if __name__ == "__main__":
    unittest.main()
