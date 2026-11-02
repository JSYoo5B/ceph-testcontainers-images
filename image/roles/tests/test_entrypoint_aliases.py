"""Preserve real generated source links; no native daemon execution is claimed."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import assemble
import contract
import package_roles
import runtime


class EntrypointAliasTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source'
        self.bin = self.source / 'usr/bin'
        self.alternatives = self.source / 'etc/alternatives'
        self.bin.mkdir(parents=True)
        self.alternatives.mkdir(parents=True)
        self.entry = self.bin / 'ceph-osd'
        self.middle = self.alternatives / 'ceph-osd'
        self.target = self.bin / 'ceph-osd-classic'
        self.target.write_bytes(b'#!/bin/sh\nexit 0\n')
        self.target.chmod(0o751)
        self.entry.symlink_to('../../etc/alternatives/ceph-osd')
        self.middle.symlink_to('../../usr/bin/ceph-osd-classic')

    def aliases(self):
        return runtime.required_entrypoint_aliases({'essential': (str(self.entry),)})

    def test_generated_alias_is_required_even_when_only_target_is_package_owned(self):
        self.assertEqual(self.aliases(), {str(self.entry)})
        self.assertEqual(runtime.required_entrypoint_aliases({'essential': (str(self.target),)}), set())
        materialized = Path(self.temporary.name) / 'materialized'
        # These globals select a real temporary assembly root; no copy/stat or
        # symlink semantics are substituted. Host-system ancestors are already
        # visited so their privileged metadata is outside this fixture. The one package-owned file does
        # not include the update-alternatives-generated entrypoint.
        with mock.patch.object(assemble, 'ROOT', materialized), \
                mock.patch.object(assemble, 'copied', {str(p) for p in self.source.parents}), \
                mock.patch.object(assemble, 'hardlinks', {}), \
                mock.patch.object(assemble, 'license_files', set()), \
                mock.patch.object(package_roles, 'REQUIRED_EXECUTABLES',
                                  {'essential': (str(self.entry),)}):
            assemble.copy_path(str(self.target))
            owned = {str(self.target)}
            members = {p: {'essential'} for p in package_roles.plan_source_paths(owned, set(), materialized)}
            with self.assertRaisesRegex(RuntimeError, 'does not include required executable'):
                package_roles.validate_executables(materialized, members)
            for alias in self.aliases():
                assemble.copy_path(alias)
            members = {p: {'essential'} for p in package_roles.plan_source_paths(
                owned | self.aliases(), set(), materialized)}
            package_roles.validate_executables(materialized, members)
            copied = assemble.destination(str(self.target))
            self.assertEqual(copied.read_bytes(), self.target.read_bytes())
            self.assertEqual(copied.stat().st_mode & 0o777, 0o751)
            self.assertEqual(os.readlink(assemble.destination(str(self.entry))), os.readlink(self.entry))
            self.assertEqual(os.readlink(assemble.destination(str(self.middle))), os.readlink(self.middle))

    def test_missing_nonexecutable_and_cyclic_targets_are_refused(self):
        self.target.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'missing/non-executable'):
            self.aliases()
        self.target.unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing/non-executable'):
            self.aliases()
        self.target.symlink_to('ceph-osd')
        with self.assertRaisesRegex(RuntimeError, 'cyclic'):
            self.aliases()

    def test_only_the_essential_implementation_is_allowed_not_other_ceph_commands(self):
        bins = {'ceph-osd-classic', 'radosgw-admin', 'ceph-mds', 'rbd-mirror'}
        self.assertEqual(contract.implementation_names('essential', bins), {'ceph-osd-classic'})
        self.assertNotIn('ceph-osd-classic', contract.EXECUTABLES['essential'])
        for role in contract.ROLES:
            self.assertEqual(contract.permits_path(role, '/usr/bin/ceph-osd-classic', bins),
                             role == 'essential')
        for command in ('radosgw-admin', 'ceph-mds', 'rbd-mirror'):
            self.assertFalse(contract.permits_path('essential', '/usr/bin/' + command, bins))
        # Match the source's Linux target path without writing /usr/bin on the
        # test host. realpath resolves this actual temporary link to that path.
        linux_target_alias = self.source / 'build-id'
        linux_target_alias.symlink_to('/usr/bin/ceph-osd-classic')
        for role in contract.ROLES:
            self.assertEqual(contract.permits_path(role, str(linux_target_alias), bins),
                             role == 'essential')


if __name__ == '__main__':
    unittest.main()
