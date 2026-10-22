"""Versioned role capabilities. Package names are backend implementation details."""
import os
from pathlib import Path
ROLES = ("essential", "client", "cephfs", "rbd-aux", "rgw")
CORE_CLI = {"ceph", "ceph-conf"}
EXECUTABLES = {
    "essential": CORE_CLI | {"ceph-mon", "ceph-mgr", "ceph-osd", "ceph-authtool", "monmaptool", "crushtool"},
    "client": CORE_CLI | {"rados", "rbd", "cryptsetup", "crushtool", "osdmaptool"},
    "cephfs": CORE_CLI | {"ceph-mds", "cephfs-mirror"},
    "rbd-aux": CORE_CLI | {"rados", "rbd", "rbd-mirror", "ceph-immutable-object-cache"},
    "rgw": CORE_CLI | {"radosgw", "radosgw-admin"},
}
# Diagnostic CLI shipped by supported RPM/deb ceph-common. Alpine's source
# package omits it; do not invent an upstream capability for that experiment.
OPTIONAL_EXECUTABLES = {"client": {"ceph-dencoder"}}

def executables(role, available):
    return EXECUTABLES[role] | (OPTIONAL_EXECUTABLES.get(role, set()) & set(available))
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
            if candidate.name in ceph_bins - executables(role, ceph_bins):
                return False
    return True
