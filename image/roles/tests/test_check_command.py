from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build


class ImageCheckTests(unittest.TestCase):
    tags = {role: "repo:20.2.4-" + role for role in build.ROLES}

    def test_builds_need_no_external_checkout(self):
        args = build.arguments(["--source-image", "source"])
        self.assertIsNone(args.check)
        self.assertFalse(hasattr(args, "go_module_dir"))

    def test_quick_check_passes_every_role_image(self):
        command = build.check_command(self.tags, "quick", Path("/out"))
        self.assertTrue(command[1].endswith("image/check.py"))
        for role in build.ROLES:
            self.assertIn("--image", command)
            self.assertIn(role + "=repo:20.2.4-" + role, command)
        self.assertNotIn("--full", command)

    def test_full_check_runs_functional_scenarios(self):
        self.assertEqual(build.check_command(self.tags, "full", Path("/out"))[-1], "--full")

    def test_unknown_check_level_is_rejected(self):
        with self.assertRaises(SystemExit):
            build.arguments(["--source-image", "source", "--check", "integration"])


if __name__ == "__main__":
    unittest.main()
