"""Routing tests use distinct role references so all cannot hide a fallback."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check
import publish
import contract

class RoleSelectionTests(unittest.TestCase):
    def test_diagnostic_decoder_follows_source_availability_only_in_client(self):
        self.assertIn('ceph-dencoder',contract.executables('client',{'ceph-dencoder'}))
        self.assertNotIn('ceph-dencoder',contract.executables('client',set()))
        self.assertNotIn('ceph-dencoder',contract.executables('essential',{'ceph-dencoder'}))
        self.assertTrue(contract.permits_path('client','/usr/bin/ceph-dencoder',{'ceph-dencoder'}))
        self.assertFalse(contract.permits_path('essential','/usr/bin/ceph-dencoder',{'ceph-dencoder'}))

    def test_ordinary_rbd_has_no_auxiliary_or_rgw_requirement(self):
        images = {'essential':'core', 'client':'toolkit'}
        self.assertEqual(check.runtime_sets(images,['rbd'],'roles-v2'), {'mixed':images})
        with self.assertRaises(check.CheckError):
            check.runtime_sets({'essential':'core'},['rbd'],'roles-v2')

    def test_rgw_does_not_inherit_generic_client(self):
        images = {'essential':'core', 'rgw':'gateway-and-admin'}
        self.assertEqual(check.runtime_sets(images,['rgw-multisite'],'roles-v2'), {'mixed':images})

    def test_all_and_mixed_are_independent(self):
        images = {r:r for r in contract.ROLES}
        images['all'] = 'complete'
        sets = check.runtime_sets(images,None,'roles-v2')
        self.assertEqual(sets['mixed'],{r:r for r in contract.ROLES})
        self.assertEqual(sets['all'],{r:'complete' for r in contract.ROLES})
        with self.assertRaises(check.CheckError):
            check.runtime_sets({'essential':'core','control':'old'},['rbd'],'roles-v2')

    def test_every_existing_scenario_has_an_explicit_requirement_set(self):
        self.assertEqual(set(contract.SCENARIOS),set(check.suite.SCENARIOS))
        self.assertIn('rbd-aux',contract.required(['rbd-snapshot-mirror']))
        self.assertNotIn('rbd-aux',contract.required(['rbd-backup']))

    def test_v2_publication_cannot_overwrite_legacy_tags(self):
        with patch.object(publish,'CONTRACT','roles-v2'):
            self.assertEqual(publish.active_roles(),contract.ROLES+('all',))
            self.assertIn(':roles-v2-official-19.2.5-all',publish.release_tag('19.2.5','official','all'))
            with self.assertRaises(publish.PublishError):
                publish.validate_report({'contract':'legacy'},'amd64','19.2.5')

class VersionDiagnosticsTests(unittest.TestCase):
    def test_python_diagnostics_do_not_corrupt_version_records(self):
        import subprocess
        import tempfile
        script = (check.HERE/'check-runtime.sh').read_text()
        function = script[script.index('version() {'):script.index('# pgrep -f')]
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder)/'ceph'
            binary.write_text('#!/bin/sh\necho "SyntaxWarning: dependency diagnostic"\necho "ceph version 19.2.4 (abcdef) squid (stable)"\n')
            binary.chmod(0o755)
            result = subprocess.run(['sh','-c','work="$1"\nexternal_command() { return 0; }\n'+function+'\nversion "$2"\nprintf "CHECK\\tpassed\\tversion\\n"', 'probe',folder,str(binary)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        checks,versions = check.parse_probe(result.stdout)
        self.assertEqual([v['release'] for v in versions.values()],['19.2.4'])
        self.assertIn('SyntaxWarning',result.stdout)


class BuildIDPruningTests(unittest.TestCase):
    def test_excluded_binary_cannot_return_through_a_build_id_link(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            alias = Path(folder)/'build-id'
            alias.symlink_to('/usr/bin/radosgw-admin')
            self.assertFalse(contract.permits_path('client',str(alias),{'radosgw-admin'}))
            self.assertTrue(contract.permits_path('rgw',str(alias),{'radosgw-admin'}))
        self.assertFalse(contract.permits_path('essential','/usr/bin/rbd',{'rbd'}))
        self.assertTrue(contract.permits_path('client','/usr/bin/rbd',{'rbd'}))

class SavedConfigurationTests(unittest.TestCase):
    def test_index_id_resolves_only_the_exact_runnable_platform(self):
        import measure
        observed = {'Id':'sha256:index','Os':'linux','Architecture':'amd64','RootFS':{'Layers':['sha256:layer']}}
        record = {'Layers':['native/layer.tar']}
        config = {'os':'linux','architecture':'amd64','rootfs':{'diff_ids':['sha256:layer']}}
        other = dict(config,architecture='arm64')
        self.assertEqual(measure.select_configuration(observed,{'sha256:native':(record,config),'sha256:other':({},other)}),('sha256:native',record))
        with self.assertRaises(ValueError):
            measure.select_configuration(observed,{'sha256:other':({},other)})
        with self.assertRaises(ValueError):
            measure.select_configuration(observed,{'sha256:a':(record,config),'sha256:b':(record,config)})
