"""Disposable Ceph clusters driven through the Docker CLI.

Internal test harness: it bootstraps clusters from role images the same way a
testcontainers module would, but it is not a reusable library.
"""
import json
from pathlib import Path
import subprocess
import time
import uuid

LABEL = "io.ceph-testcontainers.functional"
OSD_BLOCK_BYTES = 1 << 30
RGW_PORT = 7480

MON_SCRIPT = r"""
set -eu
mkdir -p /etc/ceph /var/lib/ceph/mon/ceph-a /var/run/ceph /var/log/ceph
address=$(hostname -i | awk '{print $1}')
cat > /etc/ceph/ceph.conf <<EOF
[global]
fsid = ${TC_FSID}
mon host = [v2:${address}:3300,v1:${address}:6789]
mon initial members = a
auth cluster required = cephx
auth service required = cephx
auth client required = cephx
auth allow insecure global id reclaim = false
log to file = false
log to stderr = true
err to stderr = true
mon cluster log to file = false
osd pool default size = 2
osd pool default min size = 1
osd pool default pg num = 8
osd pool default pgp num = 0
osd pool default pg autoscale mode = off
mon allow pool size one = true
mon allow pool delete = true
# Host disk usage is not an image property; keep MON_DISK_LOW out of HEALTH_OK.
mon data avail warn = 5
ms bind ipv6 = false
[osd]
osd objectstore = bluestore
bluestore block create = true
bluestore block size = ${TC_OSD_BLOCK_BYTES}
bluestore block preallocate file = false
bluestore cache autotune = false
bluestore cache size = 67108864
osd memory target = 536870912
osd crush chooseleaf type = 0
EOF
ceph-authtool --create-keyring /etc/ceph/mon.keyring --gen-key -n mon. --cap mon 'allow *'
ceph-authtool --create-keyring /etc/ceph/ceph.client.admin.keyring --gen-key -n client.admin \
    --cap mon 'allow *' --cap osd 'allow *' --cap mgr 'allow *' --cap mds 'allow *'
ceph-authtool /etc/ceph/mon.keyring --import-keyring /etc/ceph/ceph.client.admin.keyring
monmaptool --create --fsid "$TC_FSID" --addv a "[v2:${address}:3300,v1:${address}:6789]" /tmp/monmap
ceph-mon --mkfs -i a --monmap /tmp/monmap --keyring /etc/ceph/mon.keyring
exec ceph-mon -f -i a
"""

# Every non-MON container receives the cluster configuration this way.
PRELUDE = r"""
set -eu
mkdir -p /etc/ceph /var/run/ceph /var/log/ceph
printf '%s\n' "$TC_CEPH_CONF" > /etc/ceph/ceph.conf
printf '%s\n' "$TC_ADMIN_KEYRING" > /etc/ceph/ceph.client.admin.keyring
"""

DAEMON_KEYRING = PRELUDE + r"""
mkdir -p "$TC_DATA_DIR"
printf '%s\n' "$TC_KEYRING" > "$TC_DATA_DIR/keyring"
"""

MGR_SCRIPT = DAEMON_KEYRING + 'exec ceph-mgr -f -i "$TC_ID"\n'

OSD_SCRIPT = DAEMON_KEYRING + r"""
if [ ! -f "$TC_DATA_DIR/ready" ]; then
    ceph-osd --mkfs -i "$TC_ID" --osd-uuid "$TC_OSD_UUID"
fi
exec ceph-osd -f -i "$TC_ID" --crush-location "root=default host=osd-$TC_ID"
"""

MDS_SCRIPT = DAEMON_KEYRING + 'exec ceph-mds -f -i "$TC_ID" --mds-cache-memory-limit 134217728\n'

RGW_SCRIPT = PRELUDE + r"""
set -- radosgw -f -n client.admin --keyring /etc/ceph/ceph.client.admin.keyring \
    --rgw-frontends "beast port=${TC_RGW_PORT}" --rgw-thread-pool-size 4
if [ -n "${TC_RGW_REALM:-}" ]; then
    set -- "$@" --rgw-realm "$TC_RGW_REALM" --rgw-zonegroup "$TC_RGW_ZONEGROUP" --rgw-zone "$TC_RGW_ZONE"
fi
exec "$@"
"""

CLIENT_SCRIPT = PRELUDE + "exec sleep infinity\n"

# A named client user for mirror daemons; the keyring path follows the default.
MIRROR_SCRIPT = PRELUDE + r"""
printf '%s\n' "$TC_KEYRING" > "/etc/ceph/ceph.$TC_NAME.keyring"
exec "$TC_DAEMON" -f -n "$TC_NAME"
"""

HERE = Path(__file__).resolve().parent


class ClusterError(Exception):
    pass


class DockerError(ClusterError):
    def __init__(self, message, output, returncode):
        super().__init__(message)
        self.output = output
        self.returncode = returncode


class Docker:
    """Docker CLI calls labelled with one session, logged to a file."""

    def __init__(self, session, log_path):
        self.session = session
        self.log_path = log_path

    def __call__(self, *args, input=None, check=True, timeout=300):
        command = ["docker"] + [str(arg) for arg in args]
        started = time.monotonic()
        try:
            result = subprocess.run(command, input=input, capture_output=True, timeout=timeout,
                                    text=not isinstance(input, bytes))
        except subprocess.TimeoutExpired as error:
            self.log(command, None, "timeout after %ss" % timeout, timeout)
            raise ClusterError("Timed out: " + " ".join(command[:6])) from error
        output = result.stdout if isinstance(result.stdout, str) else result.stdout.decode(errors="replace")
        errors = result.stderr if isinstance(result.stderr, str) else result.stderr.decode(errors="replace")
        self.log(command, result.returncode, output + errors, time.monotonic() - started)
        if check and result.returncode:
            raise DockerError("Command failed (%d): %s\n%s" % (result.returncode, " ".join(command[:8]),
                                                               (output + errors)[-2000:]),
                              output + errors, result.returncode)
        return output

    def log(self, command, code, output, seconds):
        shown = [part if len(part) < 200 else part[:60] + "...<%d bytes>" % len(part) for part in command]
        with self.log_path.open("a") as stream:
            stream.write("%s $ %s\n[exit %s, %.1fs]\n%s\n" % (time.strftime("%H:%M:%S"), " ".join(shown), code,
                                                              seconds, output[-4000:]))

    def label(self):
        return LABEL + "=" + self.session

    def cleanup(self):
        """Remove every container and network this session created."""
        containers = self("ps", "-aq", "--filter", "label=" + self.label(), check=False).split()
        if containers:
            self("rm", "-f", "-v", *containers, check=False)
        networks = self("network", "ls", "-q", "--filter", "label=" + self.label(), check=False).split()
        for network in networks:
            self("network", "rm", network, check=False)

    def leftovers(self):
        containers = self("ps", "-aq", "--filter", "label=" + self.label(), check=False).split()
        networks = self("network", "ls", "-q", "--filter", "label=" + self.label(), check=False).split()
        return containers + networks


def wait(description, check, timeout=240, interval=2):
    """Poll check() until it returns a truthy value; keep the last error."""
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            value = check()
            if value:
                return value
        except ClusterError as error:
            last = error
        if time.monotonic() > deadline:
            raise ClusterError("Timed out waiting for " + description + (": " + str(last) if last else ""))
        time.sleep(interval)


class Cluster:
    """One cluster: MON, MGR, OSDs and a client container on a private network."""

    def __init__(self, docker, name, images, osds=2):
        self.docker = docker
        self.name = name
        self.images = images
        self.osd_count = osds
        self.prefix = "tc-" + docker.session[:8] + "-" + name
        self.network = self.prefix
        self.fsid = str(uuid.uuid4())
        self.conf = None
        self.admin_keyring = None
        self.osds = {}

    # Containers --------------------------------------------------------

    def container(self, suffix, image, script, env=(), aliases=(), networks=()):
        """Create, attach and start one container running script under /bin/sh."""
        name = self.prefix + "-" + suffix
        args = ["create", "--name", name, "--hostname", suffix, "--label", self.docker.label(),
                "--pull=never", "--user=0:0", "--network", self.network, "--network-alias", suffix]
        for alias in aliases:
            args += ["--network-alias", alias]
        for key, value in env:
            args += ["-e", key + "=" + str(value)]
        args += ["--entrypoint", "/bin/sh", image, "-c", script]
        self.docker(*args)
        for network, alias in networks:
            extra = ["--alias", alias] if alias else []
            self.docker("network", "connect", *extra, network, name)
        self.docker("start", name)
        return name

    def connect(self, suffix, network):
        """Attach a running container to another cluster's network."""
        self.docker("network", "connect", network, self.prefix + "-" + suffix)

    def shared_env(self):
        return [("TC_CEPH_CONF", self.conf), ("TC_ADMIN_KEYRING", self.admin_keyring)]

    def exec(self, container, *args, input=None, check=True, timeout=300):
        interactive = ["-i"] if input is not None else []
        return self.docker("exec", *interactive, container, *args, input=input, check=check, timeout=timeout)

    def client(self, *args, **kwargs):
        return self.exec(self.prefix + "-client", *args, **kwargs)

    def ceph(self, *args, timeout=120):
        output = self.client("ceph", "--connect-timeout", "15", *args, "--format", "json", timeout=timeout)
        return json.loads(output) if output.strip() else None

    def python(self, script, *args, timeout=300):
        """Run a probe from probes/ in the client container's python3."""
        source = (HERE / "probes" / script).read_text()
        return self.client("python3", "-", *args, input=source, timeout=timeout)

    def write_file(self, path, data, container=None):
        self.exec(container or self.prefix + "-client", "sh", "-c", 'cat > "$1"', "write", path, input=data)

    def read_file(self, path, container=None):
        return self.exec(container or self.prefix + "-client", "cat", path)

    def logs(self, directory):
        names = self.docker("ps", "-a", "--format", "{{.Names}}", "--filter", "name=" + self.prefix, check=False)
        for name in names.split():
            output = self.docker("logs", "--tail", "400", name, check=False, timeout=60)
            (directory / (name + ".log")).write_text(output)

    # Bootstrap ---------------------------------------------------------

    def start(self, extra_networks=()):
        self.docker("network", "create", "--label", self.docker.label(), self.network)
        mon = self.container("mon", self.images["control"], MON_SCRIPT,
                             env=[("TC_FSID", self.fsid), ("TC_OSD_BLOCK_BYTES", OSD_BLOCK_BYTES)])
        wait("MON " + self.name, lambda: self.exec(mon, "ceph", "--connect-timeout", "5", "mon", "stat",
                                                   timeout=30), timeout=120)
        self.conf = self.exec(mon, "cat", "/etc/ceph/ceph.conf")
        self.admin_keyring = self.exec(mon, "cat", "/etc/ceph/ceph.client.admin.keyring")
        self.container("client", self.images["control"], CLIENT_SCRIPT, env=self.shared_env(),
                       networks=extra_networks)
        self.start_mgr()
        for _ in range(self.osd_count):
            self.add_osd()
        self.wait_osds()
        self.wait_clean()
        return self

    def auth(self, entity, *caps):
        return self.client("ceph", "auth", "get-or-create", entity, *caps)

    def start_mgr(self, name="x"):
        keyring = self.auth("mgr." + name, "mon", "allow profile mgr", "osd", "allow *", "mds", "allow *")
        self.container("mgr-" + name, self.images["control"], MGR_SCRIPT, env=self.shared_env() + [
            ("TC_ID", name), ("TC_KEYRING", keyring), ("TC_DATA_DIR", "/var/lib/ceph/mgr/ceph-" + name)])
        wait("MGR " + self.name, lambda: self.ceph("mgr", "dump")["available"], timeout=180)

    def add_osd(self):
        osd_uuid = str(uuid.uuid4())
        secret = self.client("ceph-authtool", "--gen-print-key").strip()
        self.write_file("/tmp/osd-" + osd_uuid + ".json", json.dumps({"cephx_secret": secret}))
        osd_id = int(self.client("ceph", "osd", "new", osd_uuid, "-i", "/tmp/osd-" + osd_uuid + ".json").strip())
        keyring = "[osd.%d]\n\tkey = %s\n" % (osd_id, secret)
        name = self.container("osd-%d" % osd_id, self.images["osd"], OSD_SCRIPT, env=self.shared_env() + [
            ("TC_ID", osd_id), ("TC_OSD_UUID", osd_uuid), ("TC_KEYRING", keyring),
            ("TC_DATA_DIR", "/var/lib/ceph/osd/ceph-%d" % osd_id)])
        self.osds[osd_id] = name
        return osd_id

    def wait_osds(self):
        def ready():
            stat = self.ceph("osd", "stat")
            return stat["num_up_osds"] == len(self.osds) and stat["num_in_osds"] == len(self.osds)
        wait("OSDs up in " + self.name, ready, timeout=240)

    def wait_clean(self, timeout=300):
        def clean():
            pgmap = self.ceph("status")["pgmap"]
            states = pgmap.get("pgs_by_state", [])
            total = sum(state["count"] for state in states)
            return total == pgmap["num_pgs"] and all(state["state_name"] == "active+clean" for state in states)
        wait("clean placement groups in " + self.name, clean, timeout=timeout)

    def wait_health_ok(self, timeout=240):
        def healthy():
            health = self.ceph("health", "detail")
            if health["status"] != "HEALTH_OK":
                raise ClusterError(json.dumps(health.get("checks", {}))[:1500])
            return True
        wait("HEALTH_OK in " + self.name, healthy, timeout=timeout)

    def remove_osd(self, osd_id):
        self.client("ceph", "osd", "out", str(osd_id))
        self.wait_clean()
        self.docker("stop", self.osds.pop(osd_id))
        self.client("ceph", "osd", "purge", str(osd_id), "--yes-i-really-mean-it")

    def restart_osd(self, osd_id):
        self.docker("restart", self.osds[osd_id])
        self.wait_osds()
        self.wait_clean()

    # Services ----------------------------------------------------------

    def start_mds(self, filesystem="cephfs", name="a"):
        self.client("ceph", "osd", "pool", "create", filesystem + "_metadata")
        self.client("ceph", "osd", "pool", "create", filesystem + "_data")
        self.client("ceph", "fs", "new", filesystem, filesystem + "_metadata", filesystem + "_data")
        keyring = self.auth("mds." + name, "mon", "allow profile mds", "osd", "allow rwx", "mds", "allow",
                            "mgr", "allow profile mds")
        self.container("mds-" + name, self.images["mds"], MDS_SCRIPT, env=self.shared_env() + [
            ("TC_ID", name), ("TC_KEYRING", keyring), ("TC_DATA_DIR", "/var/lib/ceph/mds/ceph-" + name)])

        def active():
            for item in self.ceph("fs", "dump")["filesystems"]:
                if item["mdsmap"]["fs_name"] == filesystem:
                    return any(info["state"] == "up:active" for info in item["mdsmap"]["info"].values())
            return False
        wait("active MDS in " + self.name, active, timeout=180)
        self.wait_clean()

    def start_rgw(self, realm=None, zonegroup=None, zone=None, networks=()):
        env = self.shared_env() + [("TC_RGW_PORT", RGW_PORT)]
        if realm:
            env += [("TC_RGW_REALM", realm), ("TC_RGW_ZONEGROUP", zonegroup), ("TC_RGW_ZONE", zone)]
        self.container("rgw", self.images["rgw"], RGW_SCRIPT, env=env, networks=networks)
        wait("RGW in " + self.name, lambda: self.python("s3.py", "ping", "rgw", str(RGW_PORT), timeout=30),
             timeout=240)

    def rgw_admin(self, *args, container=None):
        return self.exec(container or self.prefix + "-rgw", "radosgw-admin", *args)

    def start_mirror(self, daemon, name, keyring, networks):
        return self.container(daemon, self.images["control"], MIRROR_SCRIPT, env=self.shared_env() + [
            ("TC_DAEMON", daemon), ("TC_NAME", name), ("TC_KEYRING", keyring)], networks=networks)
