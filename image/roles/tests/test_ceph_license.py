"""Exact-build license restoration on a real temporary filesystem, not Docker."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ceph_license


class LicenseRestorationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'records'
        self.root = Path(self.temporary.name) / 'root'
        (self.directory / 'licenses').mkdir(parents=True)
        self.text = b'Verified Ceph license fixture\n'
        self.record = {'release': '19.2.0', 'commit': 'a' * 40,
                       'asset': 'licenses/COPYING.txt',
                       'sha256': hashlib.sha256(self.text).hexdigest(),
                       'url': 'https://raw.githubusercontent.com/ceph/ceph/' + 'a' * 40 + '/COPYING'}
        (self.directory / self.record['asset']).write_bytes(self.text)
        self.version = 'ceph version 19.2.0 (' + 'a' * 40 + ') squid (stable)'
        self.save()

    def save(self):
        (self.directory / 'ceph-licenses.json').write_text(json.dumps(
            {'schema': 1, 'releases': {'19.2.0': self.record}}))

    def restore(self, version=None):
        return ceph_license.restore('19.2.0', version or self.version, self.directory, self.root)

    def test_actual_restoration_preserves_bytes_and_records_exact_source(self):
        record = self.restore()
        target = self.root / ceph_license.CANDIDATES[0].lstrip('/')
        self.assertEqual(target.read_bytes(), self.text)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(record['sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(record['commit'], 'a' * 40)
        self.assertEqual(record['source_url'], self.record['url'])
        self.assertEqual(record['operation'], 'restore-missing-ceph-copying')
        self.assertEqual(record['restored_path'], '/usr/share/doc/ceph/COPYING')
        self.assertIsNone(self.restore(), 'a second call never replaces an existing license')

    def test_each_existing_license_path_is_left_unchanged(self):
        for candidate in ceph_license.CANDIDATES:
            with self.subTest(candidate=candidate):
                target = self.root / candidate.lstrip('/')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'existing distribution license')
                self.assertIsNone(self.restore())
                self.assertEqual(target.read_bytes(), b'existing distribution license')
                target.unlink()
                canonical = self.root / ceph_license.CANDIDATES[0].lstrip('/')
                self.assertFalse(canonical.exists())

    def test_wrong_release_or_commit_refuses_before_writing(self):
        for version in ('ceph version 19.2.1 (' + 'a' * 40 + ') squid (stable)',
                        'ceph version 19.2.0 (' + 'b' * 40 + ') squid (stable)',
                        'ceph version 19.2.0 (unknown)'):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, 'differs'):
                self.restore(version)
        self.assertFalse(self.root.exists())

    def test_wrong_hash_missing_asset_and_indirect_asset_refuse(self):
        asset = self.directory / self.record['asset']
        asset.write_bytes(b'unverified license text')
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.restore()
        asset.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.restore()
        outside = Path(self.temporary.name) / 'outside'
        outside.write_bytes(self.text)
        asset.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'indirect'):
            self.restore()
        self.assertFalse(self.root.exists())

    def test_unknown_release_escape_and_mutable_url_refuse(self):
        with self.assertRaisesRegex(ValueError, 'No verified'):
            ceph_license.load_record('21.2.0', self.version, self.directory)
        self.record['asset'] = '../outside'
        self.save()
        with self.assertRaisesRegex(ValueError, 'local licenses'):
            self.restore()
        self.record['asset'] = 'licenses/COPYING.txt'
        self.record['url'] = 'https://raw.githubusercontent.com/ceph/ceph/main/COPYING'
        self.save()
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.restore()

    def test_vendored_records_bind_each_patch_to_its_immutable_asset(self):
        directory = Path(__file__).resolve().parents[1]
        records = json.loads((directory / 'ceph-licenses.json').read_text())['releases']
        self.assertEqual(set(records), {f'19.2.{p}' for p in range(8)} | {f'20.2.{p}' for p in range(5)})
        for release, record in records.items():
            version = 'ceph version ' + release + ' (' + record['commit'] + ') release (stable)'
            loaded, content = ceph_license.load_record(release, version, directory)
            self.assertEqual(loaded, record)
            self.assertTrue(content)


if __name__ == '__main__':
    unittest.main()
