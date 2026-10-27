"""Configure the shared, metadata-preserving assembler for roles."""
import os
from pathlib import Path
import shutil

import analyze
import assemble
import backend
import contract
import package_roles as packaging

def configure(kind):
    inventory, closure, base = backend.load(kind, analyze)
    packaging.ROLES = contract.ROLES
    packaging.ROOT_PACKAGES = backend.roots(kind)
    executables = {role:contract.executables(role,
        {name for name in contract.OPTIONAL_EXECUTABLES.get(role,set()) if shutil.which(name)}, kind)
        for role in contract.ROLES}
    packaging.REQUIRED_EXECUTABLES = {
        role: tuple('/usr/sbin/cryptsetup' if name=='cryptsetup' else '/usr/bin/'+name
                    for name in sorted(names)) for role,names in executables.items()
    }
    # Resolve executable paths in the source rather than impose a distro layout.
    packaging.REQUIRED_EXECUTABLES = {
        role: tuple(shutil.which(Path(p).name) or p for p in paths)
        for role,paths in packaging.REQUIRED_EXECUTABLES.items()
    }
    ceph_bins = {Path(p).name for name,pkg in inventory.items() if any(x in name for x in ('ceph','rados','rbd'))
                 for p in pkg['files'] if str(Path(p).parent) in ('/usr/bin','/usr/sbin','/bin','/sbin')}
    packaging.include_path = lambda role,p: contract.permits_path(role,p,ceph_bins)
    packaging.EXCLUDED_EXECUTABLES = {role:sorted(ceph_bins-executables[role]) for role in contract.ROLES}
    analyze.BASE = base
    analyze.installed_inventory = lambda: inventory
    analyze.closure = closure
    candidates = ('/usr/share/doc/ceph/COPYING','/usr/share/licenses/ceph/COPYING','/usr/share/doc/ceph-common/copyright')
    packaging.COPYING = next(p for p in candidates if Path(p).is_file())
    original_extras = analyze.extra_paths
    utility_paths = {shutil.which(name) for name in ('sh','mkdir','cp','cat','rm','test','sleep','hostname','awk')}
    utility_paths.discard(None)
    analyze.extra_paths = lambda: original_extras() | utility_paths
    if kind == 'rpm':
        return
    source_listing = packaging.source_listing
    packaging.source_listing = lambda inventory,packages: source_listing(inventory,packages).replace('source-rpm\tvendor', 'corresponding-source\torigin', 1)
    # Preserve distribution licenses and the corresponding-source notice from
    # the fully installed source, including Alpine's documented ELF adjustment.
    extras = analyze.extra_paths
    analyze.extra_paths = lambda: extras() | {packaging.COPYING}
    packaging.role_notices = {p.name:p.read_bytes() for p in Path('/usr/share/ceph-testcontainers').glob('modifications.json')}
    packaging.source_notice = lambda source,version: (
        'roles runtime; see source-packages.txt for selected source packages. Extracted from '+source+'\n'+version+'\n'
        +Path('/usr/share/ceph-testcontainers/SOURCES.txt').read_text())
    def materialize():
        selected = closure(assemble.PACKAGES)
        assemble.ROOT.mkdir(parents=True, exist_ok=True)
        assemble.license_files.update(set().union(*(inventory[p]['licenses'] for p in selected)))
        files = analyze.extra_paths().union(*(set(inventory[p]['files']) for p in selected))
        for p in sorted(files):
            assemble.copy_path(p)
        for p in assemble.WRITABLE_DIRS:
            target = assemble.destination(p)
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o1777 if p=='/tmp' else 0o755)
        (assemble.ROOT/packaging.MANIFEST_DIRECTORY).mkdir(parents=True, exist_ok=True)
    assemble.main = materialize
    metadata_probes = packaging.metadata_probes
    packaging.metadata_probes = lambda root: metadata_probes(root,[p for p in packaging.METADATA_PROBE_PATHS if os.path.lexists(root/p)])

if __name__ == '__main__':
    configure(os.environ.get('PACKAGE_BACKEND','rpm'))
    packaging.main()
