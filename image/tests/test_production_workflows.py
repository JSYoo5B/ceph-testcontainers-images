"""Check that ordinary production and extension CI use dependency roles."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ProductionWorkflowTests(unittest.TestCase):
    def test_old_role_builds_require_manual_opt_in(self):
        workflow = (ROOT / '.github/workflows/test.yml').read_text()
        self.assertIn('legacy_images:\n', workflow)
        self.assertIn("if: github.event_name == 'workflow_dispatch' && inputs.legacy_images", workflow)
        # All old image jobs depend on this gated release planner. Extensions
        # remain independent so they can run without an obsolete image build.
        for name in ('official-image', 'extracted-images', 'distribution-images'):
            self.assertIn('  ' + name + ':\n    needs: releases\n', workflow)
        self.assertIn('  netem-extension:\n    permissions:', workflow)

    def test_extension_checks_the_current_osd_runtime(self):
        for name in ('test.yml', 'published-extension.yml'):
            workflow = (ROOT / '.github/workflows' / name).read_text()
            self.assertIn('roles-v2-official-$release-essential', workflow)
            self.assertNotIn(':official-$release-osd', workflow)

    def test_health_and_production_use_the_dependency_contract(self):
        health = (ROOT / '.github/workflows/published-health.yml').read_text()
        self.assertIn('image/publish.py health --contract roles-v2', health)
        production = (ROOT / '.github/workflows/roles-v2.yml').read_text()
        self.assertIn('  pull_request:', production)
        self.assertIn('  push:', production)
        self.assertIn('variant: [official, debian, ubuntu]', production)
        self.assertIn('architecture: [amd64, arm64]', production)
        self.assertIn('image/check.py --contract roles-v2', production)
        self.assertIn('image/publish.py stage --contract roles-v2', production)
        self.assertIn('image/publish.py promote --contract roles-v2', production)
        self.assertIn('needs: [unit, build]', production)


if __name__ == '__main__':
    unittest.main()
