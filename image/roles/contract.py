"""Role capabilities. Package names are backend implementation details."""
import os
from pathlib import Path
# The baseline roles. Their union is the aggregate all image.
ROLES = ("essential", "client", "cephfs", "rbd-aux", "rgw")
# Optional roles, built only from the package backends listed here and never
# part of all. mgr-modules replaces only the MGR process image: the baseline
# MGR of essential plus every MGR module the source ships, with dependencies.
OPTIONAL_ROLES = {"mgr-modules": ("rpm",)}
CORE_CLI = {"ceph", "ceph-conf"}
EXECUTABLES = {
    "essential": CORE_CLI | {"ceph-mon", "ceph-mgr", "ceph-osd", "ceph-bluestore-tool", "ceph-authtool", "monmaptool", "crushtool"},
    "client": CORE_CLI | {"rados", "rbd", "cryptsetup", "crushtool", "osdmaptool"},
    "cephfs": CORE_CLI | {"ceph-mds", "cephfs-mirror", "cephfs-journal-tool"},
    "rbd-aux": CORE_CLI | {"rados", "rbd", "rbd-mirror", "ceph-immutable-object-cache"},
    "rgw": CORE_CLI | {"radosgw", "radosgw-admin"},
    "mgr-modules": CORE_CLI | {"ceph-mgr"},
}
# Diagnostic CLI shipped by supported RPM/deb ceph-common. Alpine's source
# package omits it; do not invent an upstream capability for that experiment.
# The cephadm MGR module reads the cephadm executable it would deploy
# (its cephadm_path option), so the bundle keeps it when the source ships it.
OPTIONAL_EXECUTABLES = {"client": {"ceph-dencoder"}, "mgr-modules": {"cephadm"}}

# Executables a package backend's source does not ship. Alpine packages the
# CephFS recovery tools in ceph19-utils, which the experiment does not install
# (installing it would also add ceph-dencoder and its denc plugins).
SOURCE_LACKS = {"apk": {"cephfs-journal-tool"}}

def roles(backend, optional=True):
    """The packaging roles of one build: the baseline roles, then the optional
    roles this backend builds. optional=False leaves the optional roles out,
    for a source that ships no MGR module inventory."""
    return ROLES + tuple(role for role, backends in OPTIONAL_ROLES.items()
                         if optional and backend in backends)


def executables(role, available, backend=None):
    names = EXECUTABLES[role] | (OPTIONAL_EXECUTABLES.get(role, set()) & set(available))
    return names - SOURCE_LACKS.get(backend, set())


def implementation_names(role, available):
    """Umbrella's Debian ceph-osd entrypoint selects this packaged implementation.

    Keep the public contract at ceph-osd. Its classic implementation is payload
    of the essential role only, not an additional command for every role.
    """
    return {'ceph-osd-classic'} if 'ceph-osd' in EXECUTABLES[role] and 'ceph-osd-classic' in available else set()
SCENARIOS = {
    "cluster-lifecycle": ("essential", "client"),
    "rbd": ("essential", "client"),
    "rbd-encryption": ("essential", "client"),
    "rados-object-class": ("essential", "client"),
    "rados-striper": ("essential", "client"),
    "rbd-backup": ("essential", "client"),
    "cephfs": ("essential", "client", "cephfs"),
    "rbd-snapshot-mirror": ("essential", "client", "rbd-aux"),
    "cephfs-snapshot-mirror": ("essential", "client", "cephfs"),
    "rgw-s3": ("essential", "rgw"),
    "rgw-multisite": ("essential", "rgw"),
}

def required(scenarios):
    return set().union(*(set(SCENARIOS[name]) for name in scenarios))

def permits_path(role, path, ceph_bins):
    """RPM build-id/debug symlinks must not reintroduce excluded executables."""
    for candidate in (path, os.path.realpath(path)):
        candidate = Path(candidate)
        if str(candidate.parent) in ('/usr/bin','/usr/sbin','/bin','/sbin'):
            if candidate.name in ceph_bins - (executables(role, ceph_bins) | implementation_names(role, ceph_bins)):
                return False
    return True
