"""Keep ordinary CI bounded and review-selected image execution read-only."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]

class ProductionWorkflowTests(unittest.TestCase):
    def test_default_checker_has_no_image_build_matrix(self):
        workflow = (ROOT/'.github/workflows/test.yml').read_text()
        self.assertIn('  unit:',workflow)
        self.assertIn('  netem-extension:',workflow)
        for job in ('official-image:', 'extracted-images:', 'distribution-images:', 'legacy_images:'):
            self.assertNotIn(job,workflow)

    def test_images_are_explicit_and_publication_keeps_full_checks(self):
        workflow=(ROOT/'.github/workflows/images.yml').read_text()
        self.assertIn('  workflow_dispatch:',workflow)
        self.assertNotIn('  pull_request:',workflow)
        self.assertNotIn('  push:',workflow)
        self.assertIn('--scope full',workflow)
        self.assertIn('image/publish.py stage',workflow)
        self.assertIn('needs: [unit, build]',workflow)
        self.assertIn("github.event_name == 'workflow_dispatch' && inputs.publish",workflow)

    def test_images_select_catalogued_releases_and_promote_each_independently(self):
        workflow=(ROOT/'.github/workflows/images.yml').read_text()
        inputs=workflow[workflow.index('    inputs:'):workflow.index('permissions:')]
        self.assertIn('      releases:\n        type: string\n',inputs)
        self.assertIn("default: '20.2.4'",inputs)
        for removed in ('      release:','      mode:','choice'):
            self.assertNotIn(removed,inputs)
        self.assertIn('image/releases.py plan --releases "$RELEASES"',workflow)
        self.assertIn('matrix: ${{ fromJSON(needs.unit.outputs.matrix) }}',workflow)
        promote=workflow[workflow.index('  promote:'):]
        # A failed cohort of one release must not stop the others' promotion.
        self.assertIn('if: always() &&',promote)
        self.assertIn("needs.unit.result == 'success'",promote)
        self.assertIn('for release in',promote)
        self.assertIn('image/publish.py promote --release "$release"',promote)
        self.assertIn('image/publish.py health --promotion',promote)
        self.assertIn('[ "${#incomplete[@]}" -eq 0 ]',promote)

    def test_no_workflow_hard_codes_a_release_list_or_a_separate_channel(self):
        for path in sorted((ROOT/'.github/workflows').glob('*.yml')):
            text=path.read_text()
            with self.subTest(workflow=path.name):
                self.assertNotIn('19.2.5',text)
                self.assertNotIn('discovery',text.lower())

    def test_comment_checks_cannot_publish_and_pin_the_requested_head(self):
        workflow=(ROOT/'.github/workflows/image-checks.yml').read_text()
        self.assertIn('ref: ${{ inputs.ref }}',workflow)
        self.assertIn('persist-credentials: false',workflow)
        self.assertNotIn('packages: write',workflow)
        self.assertNotIn('publish.py',workflow)
        command=(ROOT/'.github/workflows/ci-command.yml').read_text()
        self.assertIn('issue_comment:',command)
        self.assertIn('HEAD_SHA: ${{ needs.command.outputs.sha }}',command)
        self.assertNotIn('pull-requests: write',command)

    def test_health_and_extension_use_immutable_published_roles(self):
        for name in ('test.yml','published-extension.yml'):
            text=(ROOT/'.github/workflows'/name).read_text()
            self.assertIn('image/releases.py role',text)
            # Extensions target the releases with published role images only.
            self.assertIn('$(python3 image/releases.py published)',text)
            self.assertNotIn('releases.py list',text)
        self.assertIn('image/publish.py health',(ROOT/'.github/workflows/published-health.yml').read_text())
