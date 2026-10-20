"""Functional scenarios that prove role images form working Ceph clusters."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import traceback
import uuid

from cluster import RGW_PORT, Cluster, ClusterError, Docker, DockerError, wait

ROLES = ("control", "osd", "rgw", "mds")
CLIENT_CAPABILITIES = ("rbd-encryption", "rados-object-class", "rados-striper")
SINGLE_CLUSTER = ("cluster-lifecycle", "rbd", "cephfs", "rgw-s3") + CLIENT_CAPABILITIES
MULTI_CLUSTER = ("rbd-backup", "rbd-snapshot-mirror", "cephfs-snapshot-mirror", "rgw-multisite")
SCENARIOS = SINGLE_CLUSTER + MULTI_CLUSTER
HERE = Path(__file__).resolve().parent


def keys():
    """Random S3 credentials; RGW access keys must be alphanumeric."""
    return uuid.uuid4().hex[:20].upper(), uuid.uuid4().hex + uuid.uuid4().hex[:8]


def sha(cluster, pool, image, snapshot=None):
    args = [pool, image] + ([snapshot] if snapshot else [])
    return cluster.python("rbd_io.py", "sha", *args).strip()


# Single cluster ----------------------------------------------------------

def cluster_lifecycle(cluster):
    cluster.client("ceph", "osd", "pool", "create", "tc-rados")
    cluster.client("ceph", "osd", "pool", "application", "enable", "tc-rados", "rados")
    data = hashlib.sha256(b"rados").hexdigest() * 512
    cluster.write_file("/tmp/rados-object", data)
    cluster.client("rados", "-p", "tc-rados", "put", "object", "/tmp/rados-object")

    def same():
        cluster.client("rados", "-p", "tc-rados", "get", "object", "/tmp/rados-read")
        if cluster.read_file("/tmp/rados-read") != data:
            raise ClusterError("RADOS object bytes differ")
        return True
    same()
    first = min(cluster.osds)
    cluster.restart_osd(first)
    same()
    added = cluster.add_osd()
    cluster.wait_osds()
    cluster.wait_clean()
    cluster.remove_osd(added)
    cluster.wait_osds()
    cluster.wait_clean()
    same()
    cluster.wait_health_ok()


def rbd(cluster):
    cluster.client("ceph", "osd", "pool", "create", "rbd")
    cluster.client("rbd", "pool", "init", "rbd")
    cluster.python("rbd_io.py", "create", "rbd", "parent", str(32 << 20))
    cluster.python("rbd_io.py", "write", "rbd", "parent", "0", "parent-1", str(4 << 20))
    snapshot = sha(cluster, "rbd", "parent")
    cluster.client("rbd", "snap", "create", "rbd/parent@base")
    cluster.client("rbd", "snap", "protect", "rbd/parent@base")
    cluster.client("rbd", "clone", "rbd/parent@base", "rbd/child")
    cluster.python("rbd_io.py", "write", "rbd", "parent", str(8 << 20), "parent-2", str(1 << 20))
    if sha(cluster, "rbd", "child") != snapshot:
        raise ClusterError("clone does not match its parent snapshot")
    cluster.client("rbd", "flatten", "rbd/child")
    cluster.client("rbd", "snap", "unprotect", "rbd/parent@base")
    cluster.client("rbd", "snap", "rm", "rbd/parent@base")
    if sha(cluster, "rbd", "child") != snapshot:
        raise ClusterError("flattened clone lost data")
    cluster.client("rbd", "rm", "rbd/parent")
    if sha(cluster, "rbd", "child") != snapshot:
        raise ClusterError("flattened clone depends on its removed parent")


def cephfs(cluster):
    cluster.start_mds()
    cluster.python("cephfs_io.py", "basic", "cephfs")
    cluster.client("ceph", "fs", "subvolume", "create", "cephfs", "tc-subvolume")
    path = cluster.client("ceph", "fs", "subvolume", "getpath", "cephfs", "tc-subvolume").strip()
    cluster.python("cephfs_io.py", "isdir", "cephfs", path)
    cluster.python("cephfs_io.py", "write", "cephfs", path + "/file", "subvolume", str(1 << 16))
    cluster.python("cephfs_io.py", "read", "cephfs", path + "/file", "subvolume", str(1 << 16))


def rgw_s3(cluster):
    cluster.start_rgw()
    access, secret = keys()
    cluster.rgw_admin("user", "create", "--uid", "tc", "--display-name", "tc",
                      "--access-key", access, "--secret", secret)
    cluster.python("s3.py", "basic", "rgw", str(RGW_PORT), access, secret)


# Multiple clusters -------------------------------------------------------

def rbd_pools(*clusters):
    for cluster in clusters:
        cluster.client("ceph", "osd", "pool", "create", "rbd")
        cluster.client("rbd", "pool", "init", "rbd")


def transfer(source, target, path, workdir):
    local = workdir / Path(path).name
    source.docker("cp", source.prefix + "-client:" + path, str(local))
    target.docker("cp", str(local), target.prefix + "-client:" + path)


def rbd_backup(primary, backup, workdir):
    rbd_pools(primary, backup)
    primary.python("rbd_io.py", "create", "rbd", "volume", str(32 << 20))
    primary.python("rbd_io.py", "write", "rbd", "volume", "0", "backup-1", str(4 << 20))
    primary.client("rbd", "snap", "create", "rbd/volume@one")
    primary.python("rbd_io.py", "write", "rbd", "volume", str(16 << 20), "backup-2", str(2 << 20))
    primary.client("rbd", "snap", "create", "rbd/volume@two")
    primary.client("rbd", "export-diff", "rbd/volume@one", "/tmp/one.diff")
    primary.client("rbd", "export-diff", "--from-snap", "one", "rbd/volume@two", "/tmp/two.diff")
    for name in ("one.diff", "two.diff"):
        transfer(primary, backup, "/tmp/" + name, workdir)
    backup.client("rbd", "create", "--size", "32M", "rbd/volume")
    backup.client("rbd", "import-diff", "/tmp/one.diff", "rbd/volume")
    if sha(backup, "rbd", "volume", "one") != sha(primary, "rbd", "volume", "one"):
        raise ClusterError("full backup differs")
    backup.client("rbd", "import-diff", "/tmp/two.diff", "rbd/volume")
    if sha(backup, "rbd", "volume", "two") != sha(primary, "rbd", "volume", "two"):
        raise ClusterError("incremental backup differs")


def rbd_snapshot_mirror(primary, secondary, workdir):
    rbd_pools(primary, secondary)
    primary.client("rbd", "mirror", "pool", "enable", "--site-name", "site-a", "rbd", "image")
    secondary.client("rbd", "mirror", "pool", "enable", "--site-name", "site-b", "rbd", "image")
    token = primary.client("rbd", "mirror", "pool", "peer", "bootstrap", "create", "--site-name", "site-a", "rbd")
    secondary.write_file("/tmp/peer-token", token.strip())
    secondary.connect("client", primary.network)  # Import contacts the primary cluster.
    secondary.client("rbd", "mirror", "pool", "peer", "bootstrap", "import", "--site-name", "site-b",
                     "--direction", "rx-only", "rbd", "/tmp/peer-token")
    keyring = secondary.auth("client.rbd-mirror.b", "mon", "profile rbd-mirror", "osd", "profile rbd")
    secondary.start_mirror("rbd-mirror", "client.rbd-mirror.b", keyring, [(primary.network, None)])
    primary.python("rbd_io.py", "create", "rbd", "volume", str(32 << 20))
    primary.client("rbd", "mirror", "image", "enable", "rbd/volume", "snapshot")
    for round_number, offset in ((1, 0), (2, 12 << 20)):
        primary.python("rbd_io.py", "write", "rbd", "volume", str(offset), "mirror-%d" % round_number,
                       str(2 << 20))
        primary.client("rbd", "mirror", "image", "snapshot", "rbd/volume")
        expected = sha(primary, "rbd", "volume")

        wait("RBD mirror round %d" % round_number,
             lambda expected=expected: sha(secondary, "rbd", "volume") == expected, timeout=300, interval=5)
    status = json.loads(secondary.client("rbd", "mirror", "image", "status", "rbd/volume", "--format", "json"))
    if not status.get("state", "").startswith("up+"):
        raise ClusterError("rbd-mirror image state is " + status.get("state", "unknown"))


def cephfs_snapshot_mirror(primary, secondary, workdir):
    for cluster in (primary, secondary):
        cluster.start_mds()
        cluster.client("ceph", "mgr", "module", "enable", "mirroring")
    wait("mirroring module", lambda: primary.client("ceph", "fs", "snapshot", "mirror", "enable", "cephfs"),
         timeout=120, interval=5)
    token = wait("peer bootstrap token", lambda: secondary.ceph(
        "fs", "snapshot", "mirror", "peer_bootstrap", "create", "cephfs", "client.mirror_remote", "site-b"),
        timeout=120, interval=5)["token"]
    primary.connect("mgr-x", secondary.network)  # The mirroring module contacts the peer cluster.
    primary.client("ceph", "fs", "snapshot", "mirror", "peer_bootstrap", "import", "cephfs", token)
    keyring = primary.auth("client.cephfs-mirror", "mon", "profile cephfs-mirror", "mds", "allow r",
                           "osd", "allow rw tag cephfs metadata=*, allow r tag cephfs data=*", "mgr", "allow r")
    primary.start_mirror("cephfs-mirror", "client.cephfs-mirror", keyring, [(secondary.network, None)])
    primary.python("cephfs_io.py", "write", "cephfs", "/mirrored/file", "mirror", str(1 << 20))
    primary.client("ceph", "fs", "snapshot", "mirror", "add", "cephfs", "/mirrored")
    primary.python("cephfs_io.py", "mkdir", "cephfs", "/mirrored/.snap/first")
    wait("CephFS mirrored snapshot", lambda: secondary.python(
        "cephfs_io.py", "read", "cephfs", "/mirrored/.snap/first/file", "mirror", str(1 << 20)),
        timeout=300, interval=5)


def rgw_multisite(primary, secondary, workdir):
    peer = primary.prefix.rsplit("-", 1)[0] + "-peer"
    primary.docker("network", "create", "--label", primary.docker.label(), peer)
    system_access, system_secret = keys()
    realm = ["--rgw-realm", "tc", "--rgw-zonegroup", "tc-zg"]
    admin_a = primary.prefix + "-client"
    admin_b = secondary.prefix + "-client"
    primary.docker("network", "connect", peer, admin_a)
    secondary.docker("network", "connect", peer, admin_b)
    primary.rgw_admin("realm", "create", "--rgw-realm", "tc", "--default", container=admin_a)
    primary.rgw_admin("zonegroup", "create", *realm, "--endpoints", "http://rgw-a:%d" % RGW_PORT,
                      "--master", "--default", container=admin_a)
    primary.rgw_admin("zone", "create", *realm, "--rgw-zone", "tc-a", "--endpoints", "http://rgw-a:%d" % RGW_PORT,
                      "--master", "--default", "--access-key", system_access, "--secret", system_secret,
                      container=admin_a)
    primary.rgw_admin("user", "create", *realm, "--rgw-zone", "tc-a", "--uid", "tc-sync", "--display-name",
                      "sync", "--access-key", system_access, "--secret", system_secret, "--system",
                      container=admin_a)
    primary.rgw_admin("period", "update", "--commit", *realm, "--rgw-zone", "tc-a", container=admin_a)
    primary.start_rgw("tc", "tc-zg", "tc-a", networks=[(peer, "rgw-a")])
    secondary.rgw_admin("realm", "pull", "--rgw-realm", "tc", "--url", "http://rgw-a:%d" % RGW_PORT,
                        "--access-key", system_access, "--secret", system_secret, "--default", container=admin_b)
    secondary.rgw_admin("zone", "create", *realm, "--rgw-zone", "tc-b", "--endpoints",
                        "http://rgw-b:%d" % RGW_PORT, "--access-key", system_access, "--secret", system_secret,
                        "--default", container=admin_b)
    secondary.rgw_admin("period", "update", "--commit", *realm, "--rgw-zone", "tc-b", container=admin_b)
    secondary.start_rgw("tc", "tc-zg", "tc-b", networks=[(peer, "rgw-b")])
    access, secret = keys()
    primary.rgw_admin("user", "create", *realm, "--rgw-zone", "tc-a", "--uid", "tc", "--display-name", "tc",
                      "--access-key", access, "--secret", secret, container=admin_a)
    primary.python("s3.py", "create-bucket", "rgw", str(RGW_PORT), access, secret, "tc-replicated")
    primary.python("s3.py", "put", "rgw", str(RGW_PORT), access, secret, "tc-replicated", "object",
                   "multisite", str(1 << 20))
    wait("RGW object replicated to the secondary zone", lambda: secondary.python(
        "s3.py", "get", "rgw", str(RGW_PORT), access, secret, "tc-replicated", "object", "multisite",
        str(1 << 20)), timeout=420, interval=10)


class ProbeFailure(ClusterError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic["failure_stage"] + ": " + diagnostic["error_type"])


def client_capability(cluster, name):
    pool = "tc-" + name
    cluster.client("ceph", "osd", "pool", "create", pool)
    cluster.client("ceph", "osd", "pool", "application", "enable", pool,
                   "rbd" if name == "rbd-encryption" else "rados")
    if name == "rbd-encryption":
        cluster.client("rbd", "pool", "init", pool)
    try:
        output = cluster.python("client_capabilities.py", name, pool, timeout=420)
    except DockerError as error:
        for line in error.output.splitlines():
            if line.startswith("PROBE_FAILURE\t"):
                raise ProbeFailure(json.loads(line.partition("\t")[2])) from error
        raise
    for line in output.splitlines():
        if line.startswith("PROBE_RESULT\t"):
            return json.loads(line.partition("\t")[2])
    raise ClusterError("client capability probe returned no proof")


SINGLE_FUNCTIONS = {"cluster-lifecycle": cluster_lifecycle, "rbd": rbd, "cephfs": cephfs, "rgw-s3": rgw_s3,
                   **{name: (lambda cluster, name=name: client_capability(cluster, name))
                      for name in CLIENT_CAPABILITIES}}
MULTI_FUNCTIONS = {"rbd-backup": rbd_backup, "rbd-snapshot-mirror": rbd_snapshot_mirror,
                   "cephfs-snapshot-mirror": cephfs_snapshot_mirror, "rgw-multisite": rgw_multisite}


# Runner ------------------------------------------------------------------

def run(images, scenarios, output, log=print):
    """Run scenarios with images {role: image}; return {scenario: result}.

    Single-cluster scenarios share one cluster. Each multi-cluster scenario
    gets its own pair. Every container and network is removed afterwards.
    """
    missing = [role for role in ROLES if role not in images]
    if missing:
        raise ValueError("Missing role images: " + ", ".join(missing))
    unknown = [name for name in scenarios if name not in SCENARIOS]
    if unknown:
        raise ValueError("Unknown scenarios: " + ", ".join(unknown))
    output.mkdir(parents=True, exist_ok=True)
    docker = Docker(uuid.uuid4().hex, output / "docker.log")
    results = {}
    cleanup_errors = []

    def cleanup():
        try:
            docker.cleanup()
        except Exception as error:
            cleanup_errors.append(str(error)[-2000:])

    def attempt(name, clusters, body):
        log("Scenario " + name)
        started = time.monotonic()
        try:
            evidence = body()
            results[name] = {"status": "passed"}
            if evidence is not None:
                results[name]["proof"] = evidence
        except Exception as error:  # Record and continue with the next scenario.
            results[name] = {"status": "failed", "error": str(error)[-3000:]}
            results[name].update(failure_stage="scenario", failure_kind="functional_failure")
            if isinstance(error, ProbeFailure):
                results[name].update(error.diagnostic)
            (output / (name + ".trace")).write_text(traceback.format_exc())
            directory = output / (name + "-logs")
            directory.mkdir(exist_ok=True)
            for cluster in clusters:
                try:
                    cluster.logs(directory)
                except Exception as log_error:
                    results[name].setdefault("log_errors", []).append(str(log_error)[-1000:])
        results[name]["seconds"] = round(time.monotonic() - started, 1)
        log("  " + results[name]["status"] + " (%ss)" % results[name]["seconds"])

    try:
        single = [name for name in SINGLE_CLUSTER if name in scenarios]
        if single:
            cluster = Cluster(docker, "single", images)
            try:
                cluster.start()
            except Exception as error:
                for name in single:
                    results[name] = {"status": "failed", "error": "bootstrap: " + str(error)[-3000:],
                                     "failure_stage": "bootstrap", "failure_kind": "functional_failure"}
                try:
                    cluster.logs(output)
                except Exception as log_error:
                    for name in single:
                        results[name]["log_errors"] = [str(log_error)[-1000:]]
            else:
                for name in single:
                    attempt(name, [cluster], lambda name=name: SINGLE_FUNCTIONS[name](cluster))
            cleanup()
        with tempfile.TemporaryDirectory(prefix="ceph-functional-") as temporary:
            for name in (name for name in MULTI_CLUSTER if name in scenarios):
                pair = [Cluster(docker, name.split("-")[0] + "-a", images),
                        Cluster(docker, name.split("-")[0] + "-b", images)]

                def body(name=name, pair=pair):
                    for cluster in pair:
                        cluster.start()
                    MULTI_FUNCTIONS[name](pair[0], pair[1], Path(temporary))
                attempt(name, pair, body)
                cleanup()
    finally:
        cleanup()
        try:
            leftovers = docker.leftovers()
        except Exception as error:
            leftovers = []
            cleanup_errors.append("Cannot verify leftovers: " + str(error)[-2000:])
        if leftovers or cleanup_errors:
            results["cleanup"] = {"status": "failed", "leftovers": leftovers, "errors": cleanup_errors,
                                  "failure_stage": "cleanup", "failure_kind": "cleanup_failure"}
        else:
            results["cleanup"] = {"status": "passed", "session": docker.session, "leftovers": []}
    return results
