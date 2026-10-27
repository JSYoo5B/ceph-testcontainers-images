"""Routing tests use distinct role references so all cannot hide a fallback."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check
import publish
import contract
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'roles'))

class RoleSelectionTests(unittest.TestCase):
    def test_diagnostic_decoder_follows_source_availability_only_in_client(self):
        self.assertIn('ceph-dencoder',contract.executables('client',{'ceph-dencoder'}))
        self.assertNotIn('ceph-dencoder',contract.executables('client',set()))
        self.assertNotIn('ceph-dencoder',contract.executables('essential',{'ceph-dencoder'}))
        self.assertTrue(contract.permits_path('client','/usr/bin/ceph-dencoder',{'ceph-dencoder'}))
        self.assertFalse(contract.permits_path('essential','/usr/bin/ceph-dencoder',{'ceph-dencoder'}))

    def test_client_map_diagnostics_preserve_executable_boundaries(self):
        tools = {'crushtool', 'osdmaptool'}
        self.assertTrue(tools <= contract.executables('client', set()))
        self.assertIn('crushtool', contract.executables('essential', set()))
        for role in ('cephfs', 'rbd-aux', 'rgw'):
            self.assertTrue(tools.isdisjoint(contract.executables(role, tools)))
        for name in tools:
            self.assertTrue(contract.permits_path('client', '/usr/bin/'+name, tools))
        for name in ('ceph-osd', 'ceph-mon', 'ceph-mgr', 'radosgw-admin'):
            self.assertFalse(contract.permits_path('client', '/usr/bin/'+name, {name}))

    def test_ordinary_rbd_has_no_auxiliary_or_rgw_requirement(self):
        images = {'essential':'core', 'client':'toolkit'}
        self.assertEqual(check.runtime_sets(images,['rbd']), {'mixed':images})
        with self.assertRaises(check.CheckError):
            check.runtime_sets({'essential':'core'},['rbd'])

    def test_rgw_does_not_inherit_generic_client(self):
        images = {'essential':'core', 'rgw':'gateway-and-admin'}
        self.assertEqual(check.runtime_sets(images,['rgw-multisite']), {'mixed':images})

    def test_all_and_mixed_are_independent(self):
        images = {r:r for r in contract.ROLES}
        images['all'] = 'complete'
        sets = check.runtime_sets(images,None)
        self.assertEqual(sets['mixed'],{r:r for r in contract.ROLES})
        self.assertEqual(sets['all'],{r:'complete' for r in contract.ROLES})
        with self.assertRaises(check.CheckError):
            check.runtime_sets({'essential':'core','control':'old'},['rbd'])

    def test_every_existing_scenario_has_an_explicit_requirement_set(self):
        self.assertEqual(set(contract.SCENARIOS),set(check.suite.SCENARIOS))
        self.assertIn('rbd-aux',contract.required(['rbd-snapshot-mirror']))
        self.assertNotIn('rbd-aux',contract.required(['rbd-backup']))

    def test_publication_uses_the_default_role_tags(self):
        self.assertEqual(publish.active_roles(),contract.ROLES+('all',))
        self.assertEqual(publish.release_tag('19.2.5','official','all'),publish.REGISTRY+':official-19.2.5-all')

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


JOURNAL_TOOL = 'cephfs-journal-tool'
# Upstream v20.2.4 src/tools/cephfs/CMakeLists.txt links cephfs-journal-tool
# against librados, the static mds/osdc/global libraries and libblkid; the
# static libraries fold into the executable and libceph-common.so comes from
# the global target. This is fixture data, not a substitute for the loader
# check that runs in the built image (see check-runtime.sh version checks).
JOURNAL_TOOL_LIBRARIES = {
    'libceph-common.so.2': 'ceph-common',
    'librados.so.2': 'librados2',
    'libblkid.so.1': 'libblkid1',
}


def fixture_inventory():
    """A deb-shaped package inventory with the ownership the contract relies on."""
    owned = {
        'ceph-common': ['/usr/bin/ceph', '/usr/bin/ceph-conf', '/usr/bin/rados', '/usr/bin/rbd',
                        '/usr/bin/ceph-dencoder', '/usr/bin/cephfs-journal-tool',
                        '/usr/bin/cephfs-table-tool', '/usr/bin/cephfs-data-scan',
                        '/usr/lib/x86_64-linux-gnu/ceph/libceph-common.so.2'],
        'librados2': ['/usr/lib/x86_64-linux-gnu/librados.so.2'],
        'libblkid1': ['/lib/x86_64-linux-gnu/libblkid.so.1'],
        'python3-cephfs': ['/usr/lib/python3/dist-packages/cephfs.so'],
        'ceph-mds': ['/usr/bin/ceph-mds'],
        'cephfs-mirror': ['/usr/bin/cephfs-mirror'],
        'ceph-mon': ['/usr/bin/ceph-mon', '/usr/bin/ceph-authtool', '/usr/bin/monmaptool', '/usr/bin/crushtool',
                     '/usr/bin/osdmaptool'],
        'ceph-mgr': ['/usr/bin/ceph-mgr'],
        'ceph-osd': ['/usr/bin/ceph-osd'],
        'ceph-base': [],
        'rbd-mirror': ['/usr/bin/rbd-mirror'],
        'ceph-immutable-object-cache': ['/usr/bin/ceph-immutable-object-cache'],
        'ceph-radosgw': ['/usr/bin/radosgw', '/usr/bin/radosgw-admin'],
        'radosgw': ['/usr/bin/radosgw', '/usr/bin/radosgw-admin'],
        'libradosstriper1': [],
        'cryptsetup': ['/usr/sbin/cryptsetup'],
        'cryptsetup-bin': ['/usr/sbin/cryptsetup'],
    }
    depends = {
        'ceph-common': ['librados2', 'libblkid1'],
        'ceph-mds': ['ceph-base'], 'ceph-base': ['ceph-common'],
        'cephfs-mirror': ['ceph-common'], 'python3-cephfs': ['librados2'],
        'ceph-mon': ['ceph-base'], 'ceph-mgr': ['ceph-base'], 'ceph-osd': ['ceph-base'],
        'rbd-mirror': ['ceph-common'], 'ceph-immutable-object-cache': ['ceph-common'],
        'ceph-radosgw': ['ceph-common'], 'radosgw': ['ceph-common'], 'cryptsetup-bin': [], 'libradosstriper1': ['librados2'], 'cryptsetup': [],
        'librados2': [], 'libblkid1': [],
    }
    inventory = {name: {'files': files, 'licenses': set()} for name, files in owned.items()}
    def closure(names):
        selected, todo = set(), list(names)
        while todo:
            name = todo.pop()
            if name not in selected:
                selected.add(name)
                todo.extend(depends[name])
        return selected
    return inventory, closure


class CephfsRecoveryToolTests(unittest.TestCase):
    """cephfs-journal-tool belongs to the cephfs role only (Refs ceph-testcontainers-go#67)."""

    def test_selected_for_cephfs_and_the_all_union(self):
        self.assertIn(JOURNAL_TOOL, contract.executables('cephfs', set()))
        union = set().union(*(contract.executables(role, set()) for role in contract.ROLES))
        self.assertIn(JOURNAL_TOOL, union)

    def test_not_selected_for_other_roles(self):
        available = {JOURNAL_TOOL}
        for role in ('essential', 'client', 'rbd-aux', 'rgw'):
            with self.subTest(role=role):
                self.assertNotIn(JOURNAL_TOOL, contract.executables(role, available))
                self.assertFalse(contract.permits_path(role, '/usr/bin/'+JOURNAL_TOOL, available))
        self.assertTrue(contract.permits_path('cephfs', '/usr/bin/'+JOURNAL_TOOL, available))

    def test_only_the_needed_recovery_tool_is_selected(self):
        for name in ('cephfs-table-tool', 'cephfs-data-scan'):
            self.assertNotIn(name, contract.executables('cephfs', {name}))
            self.assertFalse(contract.permits_path('cephfs', '/usr/bin/'+name, {name}))

    def test_experimental_alpine_source_does_not_claim_the_tool(self):
        self.assertNotIn(JOURNAL_TOOL, contract.executables('cephfs', set(), 'apk'))
        for backend in ('rpm', 'deb'):
            self.assertIn(JOURNAL_TOOL, contract.executables('cephfs', set(), backend))

    def role_file_lists(self, backend='deb'):
        """Runs the production file-selection step with runtime.configure's path filter."""
        import backend as backends
        import package_roles
        inventory, closure = fixture_inventory()
        roots = backends.roots(backend)
        ceph_bins = {Path(p).name for name, pkg in inventory.items() for p in pkg['files']
                     if str(Path(p).parent) in ('/usr/bin', '/usr/sbin', '/bin', '/sbin')}
        executables = {role: contract.executables(role, {n for n in contract.OPTIONAL_EXECUTABLES.get(role, ())}, backend)
                       for role in contract.ROLES}
        saved = package_roles.include_path
        package_roles.include_path = lambda role, path: contract.permits_path(role, path, ceph_bins)
        try:
            return {role: package_roles.role_files(role, inventory, closure(roots[role]), set())
                    for role in contract.ROLES}, executables, inventory, closure, roots
        finally:
            package_roles.include_path = saved

    def test_role_file_lists_contain_the_tool_only_in_cephfs(self):
        for backend in ('deb', 'rpm'):
            files, *_ = self.role_file_lists(backend)
            with self.subTest(backend=backend):
                self.assertIn('/usr/bin/'+JOURNAL_TOOL, files['cephfs'])
                for role in ('essential', 'client', 'rbd-aux', 'rgw'):
                    self.assertNotIn('/usr/bin/'+JOURNAL_TOOL, files[role])
                for name in ('cephfs-table-tool', 'cephfs-data-scan'):
                    self.assertNotIn('/usr/bin/'+name, files['cephfs'])

    def test_tool_library_closure_is_covered_like_other_executables(self):
        """Every library the tool needs is in the cephfs list, as for ceph-mds and cephfs-mirror."""
        files, executables, inventory, closure, roots = self.role_file_lists('deb')
        owners = {path: name for name, pkg in inventory.items() for path in pkg['files']}
        listed = {Path(p).name: p for p in files['cephfs']}
        for library, package in JOURNAL_TOOL_LIBRARIES.items():
            with self.subTest(library=library):
                self.assertIn(library, listed)
                self.assertEqual(owners[listed[library]], package)
                self.assertIn(package, closure(roots['cephfs']))
        for name in executables['cephfs']:
            if name not in ('ceph', 'ceph-conf'):
                self.assertIn('/usr/bin/'+name, files['cephfs'])
        # The tool's own owning package is already a cephfs root dependency.
        self.assertEqual(owners['/usr/bin/'+JOURNAL_TOOL], 'ceph-common')
        self.assertIn('ceph-common', closure(roots['cephfs']))

    def test_other_role_file_lists_are_not_enlarged(self):
        """The tool's absence from other roles is the only difference introduced by the selection."""
        files, *_ = self.role_file_lists('deb')
        extra = {'/usr/bin/'+JOURNAL_TOOL}
        for role in ('essential', 'client', 'rbd-aux', 'rgw'):
            self.assertTrue(extra.isdisjoint(files[role]))
