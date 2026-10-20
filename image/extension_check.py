#!/usr/bin/env python3
"""Check an opt-in extension image against its contract (docs/EXTENSIONS.md).

The quick check (default) runs the extension's probe in the image without any
capability or network. The functional check (--functional) also checks this
Docker engine: the kernel feature with NET_ADMIN in a disposable namespace,
then a delay that the helper applies, changes and removes inside the network
namespace of a container from each --target image. Each --selector adds a
selector profile of the contract: a delay and a loss that only the selected
peers, families and ports of a target's egress get, and the lifecycle of the
owned qdisc and filter hierarchy. --rate adds the optional bandwidth profile:
whether the image's tc parses netem's rate and queue limit (without any
capability), and with --functional whether this engine's kernel keeps them and
a rate limits only the selected peers of each target's egress.
--packet-faults adds the optional packet-fault profile in the same stages:
netem's reorder, duplicate and corrupt with correlations, and with
--functional each fault's effect on the selected peers of each target's
egress, observed in the receiver's kernel counters.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

import check

HERE = Path(__file__).resolve().parent
EXTENSIONS = {"netem": {"contract": "1", "probe": HERE / "extensions" / "netem" / "check-netem.sh"}}
LABEL = "io.ceph-testcontainers.extension-check"
HANDLE = "c7e1:"
PORT = 7000
RESPONSE = 1024
SAMPLES = 9

# Peer-selective profiles of contract 1 (docs/EXTENSIONS.md). Each leaf selects
# the target's egress to one (family, peer, TCP destination port).
ROOT, CHILD, BAND = "7e00:", "7e10:", "7e00:3"
SELECTED_PORT, OTHER_PORT, REVERSE_PORT = 7000, 7001, 6999
PEERS = ("peer-b", "peer-c", "peer-d")
SELECTORS = {"ipv4-tcp-u32": [(4, "peer-b", SELECTED_PORT)],
             "ipv6-tcp-u32": [(6, "peer-b", SELECTED_PORT)],
             "multi-peer-u32": [(4, "peer-b", SELECTED_PORT), (4, "peer-c", OTHER_PORT), (6, "peer-b", SELECTED_PORT)]}
FOREIGN_PREF = 20
SELECTOR_SAMPLES, CONTROL_SAMPLES = 5, 3
LOSS_TIMEOUT = 0.8
DRAIN_BUDGET = 2.0
DRAIN_POLL = 0.05

# The optional bandwidth profile (docs/EXTENSIONS.md): netem's rate and queue
# limit in the owned child, for the selected peers of a target's egress. tc
# takes the rate in bit/s and prints it in bytes/s.
RATE_HANDLE = "c7e3:"
RATE_PROBE_BIT, RATE_PROBE_LIMIT = 1000000, 100
RATE_LEAVES = [(4, "peer-b", SELECTED_PORT), (6, "peer-b", SELECTED_PORT)]
RATE_CONTROL = "peer-c"
DEFAULT_QUEUE_LIMIT = 1000
QUEUE_PROBE_LIMIT = 5
RATE_DELAY_MS = 50
RATE_TRANSFER_SECONDS = 2
# Measured goodput bounds, as fractions of the configured rate: a rate counts
# Ethernet, IP and TCP headers, so goodput stays under it, and a path without
# the rate must be clearly faster.
RATE_CEILING, RATE_FLOOR, COMPOSED_FLOOR, UNSHAPED_FLOOR = 1.05, 0.5, 0.25, 4.0

# The optional packet-fault profile (docs/EXTENSIONS.md): netem's reorder,
# duplicate and corrupt, each with a correlation, in the owned child for the
# selected peers of a target's egress. The netem options are those that the
# Go module sends: a delay in microseconds, then the faults, then a rate. tc
# (iproute2 6.1) drops a duplicate's correlation silently without a delay, so
# a duplicate with a correlation always follows "delay <time> <jitter> 0%",
# which is "delay 0us 0us 0%" without a delay.
FAULT_HANDLE = "c7e4:"
FAULTS = ("reorder", "duplicate", "corrupt")


def fault_spec(netem, faults, delay_ms=0, rate_bit=0):
    """A requested netem child: its options, delay, rate, and each fault's
    [percent, correlation]. A reorder takes gap 1, as tc sets it."""
    return {"netem": netem, "delay_ms": delay_ms, "jitter_ms": 0, "rate_bit": rate_bit,
            "faults": {name: faults.get(name) for name in FAULTS}, "gap": 1 if "reorder" in faults else 0}


FAULT_PROBES = {
    "every-fault": fault_spec(["delay", "10000us", "0us", "0%", "reorder", "25%", "50%", "duplicate", "3%", "10%", "corrupt", "2%", "5%"],
                              {"reorder": [25, 50], "duplicate": [3, 10], "corrupt": [2, 5]}, 10),
    "zero-delay-duplicate": fault_spec(["delay", "0us", "0us", "0%", "duplicate", "3%", "10%"], {"duplicate": [3, 10]}),
}
# The encoding that loses the duplicate's correlation with iproute2 6.1; the
# kernel stage records what it installs and never relies on it.
PLAIN_DUPLICATE = ["duplicate", "3%", "10%"]
# tc refuses a reorder without a delay before it asks the kernel.
REORDER_WITHOUT_DELAY = ["reorder", "25%", "50%"]
# The target scenario's effects, each observed in the receiver's kernel
# counters: a reordered segment arrives ahead of the delayed ones and waits in
# the out-of-order queue; a duplicate segment is acknowledged with a D-SACK;
# a corrupted segment fails its checksum. A corrupted bit in an Ethernet or
# IPv6 header, or an IPv4 header that the bridge drops first, never reaches
# the receiver's counters, so corruption is checked at 20%.
FAULT_EFFECTS = {
    "reorder": dict(fault_spec(["delay", "10000us", "reorder", "25%", "25%"], {"reorder": [25, 25]}, 10),
                    counters=["ofo_queue"]),
    "duplicate": dict(fault_spec(["delay", "0us", "0us", "0%", "duplicate", "50%", "10%"], {"duplicate": [50, 10]}),
                      counters=["dsack_sent"]),
    "corrupt": dict(fault_spec(["corrupt", "20%", "10%"], {"corrupt": [20, 10]}), counters=["csum_errors"]),
    "composed": dict(fault_spec(["delay", "5000us", "0us", "0%", "duplicate", "50%", "10%", "corrupt", "20%", "10%",
                                 "rate", "100000000bit", "limit", "1000"],
                                {"duplicate": [50, 10], "corrupt": [20, 10]}, 5, 100000000),
                     counters=["dsack_sent", "csum_errors"]),
}
# Compositions that the profile does not offer, with the reason; a module
# refuses them before it changes anything.
FAULT_UNSUPPORTED = {
    "reorder without delay": "netem reorders by sending some packets ahead of the delayed ones; tc refuses it",
    "reorder with jitter": "a jitter reorders packets by itself, and the two cannot be told apart",
    "reorder with rate": "netem sends the reordered packets past the rate limit",
}
FAULT_LEAVES = [(4, "peer-b", SELECTED_PORT), (6, "peer-b", SELECTED_PORT)]
FAULT_CONTROL = "peer-c"
FAULT_TRANSFER_BYTES = 8000000
FAULT_TRANSFER_TIMEOUT = 60
# A selected transfer must raise its effect's counter by at least
# EFFECT_FLOOR and by EFFECT_MARGIN over its noise: the most that a baseline
# transfer raised it. A transfer without the fault may exceed the noise by
# CONTROL_ALLOWANCE. Natively, selected transfers of 8 MB raised their
# effect's counter by 113 or more, and transfers without the fault by at most
# 5; with 4 MB, a reorder once raised it by only 22.
EFFECT_FLOOR, EFFECT_MARGIN, CONTROL_ALLOWANCE = 20, 15, 10
# The receiver's raw counters, and the effect counters made of them.
RAW_COUNTERS = {"TcpExt": ("TCPOFOQueue", "TCPDSACKOldSent", "TCPDSACKOfoSent"), "IpExt": ("InCsumErrors",),
                "Tcp": ("InCsumErrors", "InErrs", "InSegs")}
EFFECT_COUNTERS = {"ofo_queue": ("TcpExt.TCPOFOQueue",),
                   "dsack_sent": ("TcpExt.TCPDSACKOldSent", "TcpExt.TCPDSACKOfoSent"),
                   "csum_errors": ("Tcp.InCsumErrors", "IpExt.InCsumErrors")}

# Answers every connection with RESPONSE bytes; runs in the target namespace.
SERVER = """import socket
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", %d))
s.listen(64)
while True:
    c, _ = s.accept()
    try:
        c.recv(64)
        c.sendall(b"x" * %d)
    finally:
        c.close()
""" % (PORT, RESPONSE)

# Answers every connection on each port given, over IPv4 and IPv6.
PEER_SERVER = """import socket, sys, threading
def serve(port):
    s = socket.socket(socket.AF_INET6)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", port))
    s.listen(64)
    while True:
        c, _ = s.accept()
        try:
            c.recv(64)
            c.sendall(b"x" * %d)
        finally:
            c.close()
threads = [threading.Thread(target=serve, args=(int(port),)) for port in sys.argv[1:]]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
""" % RESPONSE

# Times fresh requests and reports each outcome; a timeout is a result.
SELECT_CLIENT = """import json, socket, sys, time
host, port, count, size, timeout = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5])
outcomes, times = [], []
for _ in range(count):
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout) as c:
            c.sendall(b"ping\\n")
            data = b""
            while len(data) < size:
                chunk = c.recv(4096)
                if not chunk:
                    break
                data += chunk
        outcome = "answered" if len(data) == size else "short"
    except socket.timeout:
        outcome = "timeout"
    except OSError as error:
        outcome = "error: " + str(error)
    elapsed = (time.monotonic() - start) * 1000
    outcomes.append(outcome)
    if outcome == "answered":
        times.append(elapsed)
times.sort()
print(json.dumps({"count": count, "answered": len(times), "outcomes": sorted(set(outcomes)),
                  "median_ms": times[len(times) // 2] if times else None, "elapsed_ms": elapsed}))
"""

# Receives whole transfers on each port given, over IPv4 and IPv6, and
# answers each with the byte count, the SHA-256 and the address it came from.
SINK = """import hashlib, json, socket, sys, threading
def receive(c, address):
    try:
        digest, count = hashlib.sha256(), 0
        while True:
            chunk = c.recv(65536)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
        c.sendall(json.dumps({"bytes": count, "sha256": digest.hexdigest(), "from": address[0]}).encode())
    except OSError:
        pass
    finally:
        c.close()
def serve(port):
    s = socket.socket(socket.AF_INET6)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    s.bind(("::", port))
    s.listen(64)
    while True:
        c, address = s.accept()
        threading.Thread(target=receive, args=(c, address), daemon=True).start()
threads = [threading.Thread(target=serve, args=(int(port),)) for port in sys.argv[1:]]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
"""

# Sends a deterministic payload to each host and port given, one after the
# other or all at once, and reports what the sink received. Runs in the
# target's namespace, so the payload leaves through the target's interface.
SENDER = """import hashlib, json, socket, sys, threading, time
size, timeout, together = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3] == "together"
paths = [sys.argv[index:index + 3] for index in range(4, len(sys.argv), 3)]
def send(host, port, seed, result):
    block = hashlib.sha256(seed.encode()).digest() * 2048
    digest, sent = hashlib.sha256(), 0
    result.update(host=host, port=int(port), error=None)
    start = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as c:
            result["connect_ms"] = (time.monotonic() - start) * 1000
            result["family"] = 6 if c.family == socket.AF_INET6 else 4
            result["local"] = c.getsockname()[0]
            while sent < size:
                chunk = block[:size - sent]
                c.sendall(chunk)
                digest.update(chunk)
                sent += len(chunk)
            c.shutdown(socket.SHUT_WR)
            answer = b""
            while True:
                chunk = c.recv(4096)
                if not chunk:
                    break
                answer += chunk
        receipt = json.loads(answer.decode())
        result.update(received=receipt["bytes"], received_sha256=receipt["sha256"], seen_from=receipt["from"])
    except (OSError, ValueError, KeyError) as error:
        result["error"] = type(error).__name__ + ": " + str(error)
    elapsed = time.monotonic() - start
    result.update(sent=sent, sha256=digest.hexdigest(), elapsed_ms=elapsed * 1000,
                  goodput_Bps=sent / elapsed if elapsed > 0 else None)
results = [{} for _ in paths]
start = time.monotonic()
threads = [threading.Thread(target=send, args=tuple(path) + (result,)) for path, result in zip(paths, results)]
for thread in threads:
    thread.start()
    if not together:
        thread.join()
for thread in threads:
    thread.join()
wall = time.monotonic() - start
print(json.dumps({"size": size, "together": together, "wall_ms": wall * 1000,
                  "aggregate_Bps": sum(r["sent"] for r in results) / wall if wall > 0 else None,
                  "transfers": results}))
"""

# Helper-side reads that need only the contract's python3.
NAMESPACE = 'import os; print(os.readlink("/proc/self/ns/net"))'
KERNEL = "import os; print(os.uname().release)"

# The kernel's TCP and IP counters of the namespace it runs in, as JSON
# tables; /proc/net/netstat and /proc/net/snmp list each table as a line of
# names and a line of values.
COUNTERS = """import json
tables = {}
for path in ("/proc/net/netstat", "/proc/net/snmp"):
    lines = open(path).read().splitlines()
    for names, values in zip(lines[::2], lines[1::2]):
        table, names = names.split(":", 1)
        tables[table] = dict(zip(names.split(), (int(value) for value in values.split(":", 1)[1].split())))
print(json.dumps(tables))
"""

# The checksum and offload settings of an interface, through the legacy
# ethtool ioctls, which need no capability. Context only: an image without
# fcntl or an interface without the ioctl reports why.
OFFLOADS = """import ctypes, fcntl, json, socket, struct, sys
out = {}
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for name, command in (("rx_checksum", 0x14), ("tx_checksum", 0x16), ("scatter_gather", 0x18), ("tso", 0x1e),
                      ("gso", 0x23), ("gro", 0x2b)):
    value = ctypes.create_string_buffer(struct.pack("II", command, 0), 8)
    try:
        fcntl.ioctl(s.fileno(), 0x8946, struct.pack("16sP16x", sys.argv[1].encode(), ctypes.addressof(value)))
        out[name] = bool(struct.unpack("II", value.raw[:8])[1])
    except OSError as error:
        out[name] = "unavailable: " + str(error)
print(json.dumps(out))
"""

# Waits until the server in the target's namespace accepts connections.
READY = """import socket, sys, time
deadline = time.monotonic() + 30
while True:
    try:
        socket.create_connection((sys.argv[1], int(sys.argv[2])), 2).close()
        break
    except OSError:
        if time.monotonic() > deadline:
            raise
        time.sleep(0.2)
"""

# Times whole requests from another container: connect, send, read the answer.
CLIENT = """import json, socket, sys, time
host, port, count, size = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
times = []
for _ in range(count):
    start = time.monotonic()
    with socket.create_connection((host, port), timeout=10) as c:
        c.sendall(b"ping\\n")
        data = b""
        while len(data) < size:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
    if len(data) != size:
        raise SystemExit("short answer: %d bytes" % len(data))
    times.append((time.monotonic() - start) * 1000)
times.sort()
print(json.dumps({"count": count, "median_ms": times[count // 2], "min_ms": times[0], "max_ms": times[-1]}))
"""


class CheckError(check.CheckError):
    """A failed check, with the kind of failure for the report."""

    def __init__(self, message, kind="checker_failure"):
        super().__init__(message)
        self.kind = kind


def rate_kbit(value):
    """A rate in kbit/s from 64 kbit/s to 10 Gbit/s; whole kbit/s are whole bytes per second."""
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a whole number of kbit/s") from error
    if not 64 <= number <= 10000000:
        raise argparse.ArgumentTypeError("must be from 64 to 10000000 kbit/s")
    return number


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extension", choices=tuple(EXTENSIONS), required=True)
    parser.add_argument("--image", required=True, metavar="REF", help="Extension helper image")
    parser.add_argument("--platform", choices=("linux/arm64", "linux/amd64"),
                        help="Reject other platforms; otherwise record each local image's platform")
    parser.add_argument("--functional", action="store_true",
                        help="Also check the kernel feature and a delay inside each target's namespace")
    parser.add_argument("--target", action="append", default=[], metavar="REF",
                        help="With --functional, a Ceph image whose container namespace the helper joins; repeat")
    parser.add_argument("--selector", action="append", default=[], choices=tuple(SELECTORS),
                        help="With --functional, also check a selector profile in each target; repeat")
    parser.add_argument("--rate", action="store_true",
                        help="Also check the optional bandwidth profile: tc's netem rate and queue limit, and with "
                             "--functional the kernel and a rate in each target")
    parser.add_argument("--rate-kbit", type=rate_kbit, default=16000, metavar="KBIT",
                        help="With --rate and --functional, the rate of the target scenario in kbit/s (default 16000)")
    parser.add_argument("--packet-faults", action="store_true",
                        help="Also check the optional packet-fault profile: tc's netem reorder, duplicate and corrupt "
                             "with correlations, and with --functional the kernel and each fault in each target")
    parser.add_argument("--delay-ms", type=check.positive_number, default=200, metavar="MS")
    parser.add_argument("--output-dir", type=Path, help="Fresh report directory (default: artifacts/extension-check-UUID)")
    parser.add_argument("--probe-timeout", type=check.positive_number, default=120, metavar="SECONDS")
    args = parser.parse_args(argv)
    if args.target and not args.functional:
        parser.error("--target requires --functional")
    if args.functional and not args.target:
        parser.error("--functional requires at least one --target")
    if args.selector and not args.functional:
        parser.error("--selector requires --functional")
    if args.image.startswith("-") or any(target.startswith("-") for target in args.target):
        parser.error("image references cannot start with -")
    return args


def docker(*command, timeout=120, check_exit=True):
    """Run a Docker command; return (exit code, combined output)."""
    try:
        result = subprocess.run(["docker", *command], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise CheckError("Docker command timed out: docker " + " ".join(command), "timeout") from error
    output = result.stdout + result.stderr
    if check_exit and result.returncode:
        raise CheckError("Command failed: docker " + " ".join(command) + "\n" + output)
    return result.returncode, output


def parse_probe(output, name):
    """CHECK, FAILURE and VERSION lines of an extension probe."""
    checks, versions, failures = [], {}, {}
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        if fields[0] == "CHECK":
            checks.append({"name": fields[2], "status": fields[1]})
        elif fields[0] == "FAILURE":
            failures[fields[2]] = fields[1]
        elif fields[0] == "VERSION":
            versions[fields[1]] = fields[2]
    if not checks:
        raise CheckError("The probe returned no checks")
    for item in checks:
        if item["status"] != "passed":
            item.update(failure_stage="quick:" + item["name"],
                        failure_kind=failures.get(item["name"], "quick_failure"))
    complete = "COMPLETE\t" + name in output.splitlines()
    return checks, versions, complete


def probe(extension, image, args, output):
    """Run the probe with no capabilities and no network."""
    container = "ceph-extension-check-" + uuid.uuid4().hex
    log = output / "quick.log"
    try:
        # The contract asks for sh on PATH, not for /bin/sh.
        docker("create", "--name", container, "--label", LABEL + "=quick", "--pull=never", "--network=none",
               "--cap-drop=ALL", "--user=0:0", "--interactive", "--platform", image["platform"],
               "--entrypoint=sh", image["image_id"], "-s")
        try:
            result = subprocess.run(["docker", "start", "--attach", "--interactive", container],
                                    input=EXTENSIONS[extension]["probe"].read_text(), capture_output=True,
                                    text=True, timeout=args.probe_timeout)
        except subprocess.TimeoutExpired as error:
            raise CheckError("The probe timed out", "timeout") from error
        log.write_text(result.stdout + result.stderr)
        checks, versions, complete = parse_probe(result.stdout, extension)
        passed = result.returncode == 0 and complete and all(c["status"] == "passed" for c in checks)
        image.update(checks=checks, versions=versions, log=log.name, exit_code=result.returncode,
                     status="passed" if passed else "failed")
        if not complete:
            image["error"] = "The probe did not finish all checks"
    finally:
        image["cleanup"] = remove_container(container)


def classify(output):
    """The kind of a failed tc mutation: permissions, kernel or image."""
    if "Operation not permitted" in output:
        return "permission_denied"
    if "Specified qdisc kind is unknown" in output or "TC classifier not found" in output:
        return "kernel_unsupported"
    # tc reports an option that it cannot parse as 'What is "<option>"?' or
    # 'Illegal "<option>"', before it asks the kernel.
    if "Unknown qdisc" in output or "Unknown filter" in output or "unparsable" in output or \
            'What is "' in output or 'Illegal "' in output:
        return "image_unsupported"
    return "runtime_failure"


def tc(container, *command, timeout=120):
    code, output = docker("exec", container, "tc", *command, timeout=timeout, check_exit=False)
    if code:
        raise CheckError("tc " + " ".join(command) + " failed: " + output.strip(), classify(output))
    return output


def root_qdisc(container, interface):
    """The root qdisc of an interface, from tc's JSON output."""
    qdiscs = json.loads(tc(container, "-j", "-s", "qdisc", "show", "dev", interface))
    roots = [qdisc for qdisc in qdiscs if qdisc.get("root")]
    if len(roots) != 1:
        raise CheckError("Expected one root qdisc on " + interface + ", got " + json.dumps(qdiscs))
    return roots[0]


def netem_state(qdisc):
    """The delay and jitter of an owned netem root, in milliseconds."""
    if qdisc.get("kind") != "netem" or qdisc.get("handle") != HANDLE:
        raise CheckError("The root qdisc is not the owned netem qdisc: " + json.dumps(qdisc))
    delay = qdisc.get("options", {}).get("delay", {})
    return round(delay.get("delay", 0) * 1000, 3), round(delay.get("jitter", 0) * 1000, 3)


def target_interface(addresses, address, mac):
    """The interface that carries the target's endpoint address and MAC."""
    matches = [link for link in addresses
               if link.get("address") == mac and any(a.get("local") == address for a in link.get("addr_info", []))]
    if len(matches) != 1:
        raise CheckError("Expected one interface with %s and %s, found %d" % (address, mac, len(matches)))
    return {"name": matches[0]["ifname"], "index": matches[0]["ifindex"], "mac": mac, "address": address}


def kernel(image, output):
    """Add, change and remove netem on the loopback of a disposable namespace."""
    container = "ceph-extension-check-" + uuid.uuid4().hex
    result = {"container": container}
    try:
        docker("run", "--detach", "--name", container, "--label", LABEL + "=kernel", "--pull=never", "--user=0:0",
               "--network=none", "--cap-drop=ALL", "--cap-add=NET_ADMIN", "--platform", image["platform"],
               "--entrypoint=sleep", image["image_id"], "infinity")
        result["kernel"] = docker("exec", container, "python3", "-c", KERNEL)[1].strip()
        tc(container, "qdisc", "add", "dev", "lo", "root", "handle", HANDLE, "netem", "delay", "10ms", "2ms")
        result["added"] = netem_state(root_qdisc(container, "lo"))
        tc(container, "qdisc", "change", "dev", "lo", "root", "handle", HANDLE, "netem", "delay", "20ms")
        result["changed"] = netem_state(root_qdisc(container, "lo"))
        tc(container, "qdisc", "del", "dev", "lo", "root", "handle", HANDLE)
        result["removed"] = root_qdisc(container, "lo").get("kind")
        if result["added"] != (10, 2) or result["changed"] != (20, 0) or result["removed"] == "netem":
            raise CheckError("netem did not apply as requested: " + json.dumps(result), "runtime_failure")
        result["status"] = "passed"
    except check.CheckError as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    finally:
        result["cleanup"] = remove_container(container)
        if result["cleanup"]["status"] != "passed":
            result.update(status="failed", error=result.get("error", "Could not remove " + container),
                          failure_kind=result.get("failure_kind", "checker_failure"))
        (output / "kernel.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def measure(client, address, label, output):
    code, text = docker("exec", client, "python3", "-c", CLIENT, address, str(PORT), str(SAMPLES), str(RESPONSE),
                        check_exit=False)
    with (output / "timings.log").open("a") as log:
        log.write(label + ": " + text)
    if code:
        raise CheckError("Requests to the target failed (" + label + "): " + text.strip(), "runtime_failure")
    return json.loads(text.strip().splitlines()[-1])


def delay_scenario(helper, target, run, delay, output):
    """Delay, change and restore the target's egress from a helper in its namespace."""
    label = LABEL + "=" + run
    network = "ceph-extension-check-" + run
    names = {role: network + "-" + role for role in ("target", "server", "shaper", "observer", "client")}
    names["helper_image"] = helper["image_id"]
    result = {"reference": target["reference"], "image_id": target["image_id"], "status": "running"}
    output.mkdir()
    # Helpers run as root, as the contract says, whatever the image's USER.
    common = ["--detach", "--label", label, "--pull=never", "--user=0:0", "--platform", helper["platform"]]
    try:
        docker("network", "create", "--internal", "--label", label, network)
        docker("run", "--detach", "--label", label, "--pull=never", "--platform", target["platform"], "--name", names["target"],
               "--network", network, "--entrypoint=sleep", target["image_id"], "infinity")
        inspect = json.loads(docker("container", "inspect", names["target"])[1])[0]
        endpoint = inspect["NetworkSettings"]["Networks"][network]
        result["sandbox_id"] = inspect["NetworkSettings"]["SandboxID"]
        shared = "container:" + names["target"]
        docker("run", *common, "--name", names["server"], "--network", shared, "--cap-drop=ALL",
               "--entrypoint=python3", helper["image_id"], "-c", SERVER)
        docker("run", *common, "--name", names["shaper"], "--network", shared, "--cap-drop=ALL",
               "--cap-add=NET_ADMIN", "--entrypoint=sleep", helper["image_id"], "infinity")
        docker("run", *common, "--name", names["client"], "--network", network, "--cap-drop=ALL",
               "--entrypoint=sleep", helper["image_id"], "infinity")
        namespaces = {docker("exec", names[role], "python3", "-c", NAMESPACE)[1].strip()
                      for role in ("server", "shaper")}
        if len(namespaces) != 1:
            raise CheckError("The helpers did not join one namespace: " + ", ".join(sorted(namespaces)))
        addresses = json.loads(docker("exec", names["shaper"], "ip", "-j", "address", "show")[1])
        interface = target_interface(addresses, endpoint["IPAddress"], endpoint["MacAddress"])
        result["interface"] = interface
        result["before"] = root_qdisc(names["shaper"], interface["name"]).get("kind")
        if result["before"] == "netem":
            raise CheckError("The new target already has a netem root qdisc")
        code, text = docker("exec", names["client"], "python3", "-c", READY, interface["address"], str(PORT),
                            check_exit=False)
        if code:
            raise CheckError("The server in the target's namespace did not answer: " + text.strip(), "runtime_failure")
        result["baseline"] = measure(names["client"], interface["address"], "baseline", output)
        tc(names["shaper"], "qdisc", "add", "dev", interface["name"], "root", "handle", HANDLE,
           "netem", "delay", "%dms" % delay)
        result["applied"] = netem_state(root_qdisc(names["shaper"], interface["name"]))
        result["delayed"] = measure(names["client"], interface["address"], "delayed", output)
        counters = root_qdisc(names["shaper"], interface["name"])
        result["counters"] = {key: counters.get(key) for key in ("packets", "bytes", "drops")}
        tc(names["shaper"], "qdisc", "change", "dev", interface["name"], "root", "handle", HANDLE,
           "netem", "delay", "%dms" % (delay // 2), "%dms" % (delay // 10))
        result["changed"] = netem_state(root_qdisc(names["shaper"], interface["name"]))
        # Removing the helper does not remove the qdisc from the live namespace.
        docker("rm", "--force", names["shaper"])
        docker("run", *common, "--name", names["observer"], "--network", shared, "--cap-drop=ALL",
               "--cap-add=NET_ADMIN", "--entrypoint=sleep", helper["image_id"], "infinity")
        result["after_helper_exit"] = netem_state(root_qdisc(names["observer"], interface["name"]))
        tc(names["observer"], "qdisc", "del", "dev", interface["name"], "root", "handle", HANDLE)
        result["after_restore"] = root_qdisc(names["observer"], interface["name"]).get("kind")
        result["restored"] = measure(names["client"], interface["address"], "restored", output)
        result["restart"] = restart(names, interface, delay, common)
        problems = []
        if result["applied"] != (delay, 0):
            problems.append("applied delay %r" % (result["applied"],))
        if result["delayed"]["median_ms"] < delay:
            problems.append("median %.1f ms under a %d ms delay" % (result["delayed"]["median_ms"], delay))
        if not result["counters"]["packets"]:
            problems.append("no packets counted by netem")
        if result["changed"] != (delay // 2, delay // 10):
            problems.append("changed delay %r" % (result["changed"],))
        if result["after_helper_exit"] != result["changed"]:
            problems.append("the qdisc changed when the helper exited")
        if result["after_restore"] != result["before"]:
            problems.append("root qdisc %s after removal, %s before" % (result["after_restore"], result["before"]))
        if result["restored"]["median_ms"] >= delay / 4:
            problems.append("median %.1f ms after removal" % result["restored"]["median_ms"])
        restarted = result["restart"]
        if restarted["sandbox_id"] == result["sandbox_id"]:
            problems.append("the restarted target kept its network sandbox")
        if restarted["old_namespace_interfaces"]:
            problems.append("the old namespace still has the target's interface after the restart")
        if restarted["new_root"] != result["before"]:
            problems.append("root qdisc %s in the restarted target, %s before" % (restarted["new_root"], result["before"]))
        if problems:
            raise CheckError("; ".join(problems), "runtime_failure")
        result["status"] = "passed"
    except (check.CheckError, KeyError, ValueError) as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    finally:
        result["cleanup"] = cleanup(label, output)
        if result["cleanup"]["status"] != "passed":
            result["status"] = "failed"
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def restart(names, interface, delay, common):
    """Restart the delayed target: Docker gives it a new namespace without the
    delay, and a helper that stays in the old one loses the interface."""
    tc(names["observer"], "qdisc", "add", "dev", interface["name"], "root", "handle", HANDLE,
       "netem", "delay", "%dms" % delay)
    docker("restart", "-t", "1", names["target"])
    inspect = json.loads(docker("container", "inspect", names["target"])[1])[0]
    old = json.loads(docker("exec", names["observer"], "ip", "-j", "address", "show")[1])
    fresh = names["target"] + "-fresh"
    docker("run", *common, "--name", fresh, "--network", "container:" + names["target"], "--cap-drop=ALL",
           "--entrypoint=sleep", names["helper_image"], "infinity")
    return {"sandbox_id": inspect["NetworkSettings"]["SandboxID"],
            "old_namespace_interfaces": [link["ifname"] for link in old
                                         if link.get("address") == interface["mac"]],
            "new_root": root_qdisc(fresh, interface["name"]).get("kind")}


class Pairs(list):
    """A JSON object as its (key, value) pairs in order. tc prints one u32
    key per repeated "match" member, which a dict would reduce to the last."""

    def get(self, key, default=None):
        for name, value in self:
            if name == key:
                return value
        return default

    def every(self, key):
        return [value for name, value in self if name == key]


def u32_keys(family, peer, port):
    """The keys that tc prints for a destination, TCP and destination port
    selector, in order: (offset, value, mask, offmask). tc packs each match
    into a word-aligned key, so an IPv6 next header and port appear at
    offsets 4 and 40, not 6 and 42."""
    if family == 4:
        keys = [(16, int(ipaddress.IPv4Address(peer)), 0xffffffff), (8, 0x00060000, 0x00ff0000)]
        port_offset = 20
    else:
        address = int(ipaddress.IPv6Address(peer))
        keys = [(24 + 4 * index, (address >> (96 - 32 * index)) & 0xffffffff, 0xffffffff) for index in range(4)]
        keys.append((4, 0x00000600, 0x0000ff00))
        port_offset = 40
    keys.append((port_offset, port, 0x0000ffff))
    return [{"off": off, "value": "%08x" % value, "mask": "%08x" % mask, "offmask": ""} for off, value, mask in keys]


def layout(profile):
    """The owned filters of a profile (a name, or its (family, role, port)
    leaves), laid out as modules lay them out: one
    preference per family, IPv4 first, from preference 10 and u32 table 800:,
    and one key node per leaf in the profile's order. A u32 filter has one
    protocol, so the families cannot share a preference, and the kernel
    numbers the u32 tables of one qdisc in the order that their preferences
    are created."""
    leaves, group = [], 0
    spec = SELECTORS[profile] if isinstance(profile, str) else profile
    for family, protocol in ((4, "ip"), (6, "ipv6")):
        selected = [(role, port) for leaf_family, role, port in spec if leaf_family == family]
        for node, (role, port) in enumerate(selected, 1):
            table = "%x:" % (0x800 + group)
            leaves.append({"family": family, "role": role, "port": port, "protocol": protocol,
                           "pref": 10 + group, "table": table, "handle": "%s:%x" % (table, node)})
        if selected:
            group += 1
    return leaves


def path_name(role, family, port):
    return "%s ipv%d %d" % (role, family, port)


def selector_leaves(raw, leaves):
    """The owned selectors of a filter dump, checked against the requested
    leaves, each with its peer address. Each owned preference may also hold
    its chain and u32 table entries; anything else is foreign state."""
    tables = {(leaf["protocol"], leaf["pref"]): leaf["table"] for leaf in leaves}
    found = {}
    for entry in json.loads(raw, object_pairs_hook=Pairs):
        group = (entry.get("protocol"), entry.get("pref"))
        if entry.get("kind") != "u32" or entry.get("parent") != ROOT or group not in tables:
            raise CheckError("A filter that the check does not own: " + json.dumps(entry), "runtime_failure")
        options = entry.get("options")
        if options is None:
            continue
        handle = options.get("fh", "")
        if handle.endswith(":"):
            if handle != tables[group]:
                raise CheckError("Preference %s has the u32 table %s, not %s" % (group[1], handle, tables[group]),
                                 "runtime_failure")
            continue
        matching = [leaf for leaf in leaves if (leaf["protocol"], leaf["pref"], leaf["handle"]) == group + (handle,)]
        if not matching:
            raise CheckError("A selector leaf that the check did not add: " + json.dumps(entry), "runtime_failure")
        if handle in found:
            raise CheckError("The selector leaf %s appears twice" % handle, "runtime_failure")
        leaf = matching[0]
        keys = []
        for match in options.every("match"):
            try:
                keys.append({"off": match.get("off"), "value": "%08x" % int(match.get("value"), 16),
                             "mask": "%08x" % int(match.get("mask"), 16), "offmask": match.get("offmask")})
            except (TypeError, ValueError) as error:
                raise CheckError("A u32 key that cannot be read: " + json.dumps(match), "runtime_failure") from error
        expected = u32_keys(leaf["family"], leaf["peer"], leaf["port"])
        problems = []
        if options.get("flowid") != BAND:
            problems.append("flowid %s" % options.get("flowid"))
        if keys != expected:
            problems.append("keys %s, requested %s" % (json.dumps(keys), json.dumps(expected)))
        if problems:
            raise CheckError("The selector %s is not the requested one: %s" % (handle, "; ".join(problems)),
                             "runtime_failure")
        found[handle] = {"handle": handle, "protocol": group[0], "pref": group[1], "flowid": options.get("flowid"),
                         "keys": keys}
    missing = [leaf["handle"] for leaf in leaves if leaf["handle"] not in found]
    if missing:
        raise CheckError("Expected %d selector leaves, missing %s" % (len(leaves), ", ".join(missing)),
                         "runtime_failure")
    return [found[leaf["handle"]] for leaf in leaves]


def remaining_filters(entries, leaves):
    """What the owned preferences hold after their leaves were removed:
    nothing, or chain and u32 table entries. A leaf or another filter is
    foreign state. Returns each preference that still has entries."""
    owned = {(leaf["protocol"], leaf["pref"]) for leaf in leaves}
    kept = []
    for entry in entries:
        options = entry.get("options")
        group = (entry.get("protocol"), entry.get("pref"))
        if entry.get("kind") != "u32" or entry.get("parent") != ROOT or group not in owned or \
                (options is not None and not options.get("fh", "").endswith(":")):
            raise CheckError("A filter remains after the leaves were removed: " + json.dumps(entry), "runtime_failure")
        if group not in kept:
            kept.append(group)
    return kept


def selector_qdiscs(raw):
    """The owned prio root and netem child of a qdisc dump, with counters."""
    root = child = None
    for qdisc in json.loads(raw):
        if qdisc.get("root"):
            root = qdisc
        elif qdisc.get("handle") == CHILD:
            child = qdisc
    options = (root or {}).get("options", {})
    if (root or {}).get("kind") != "prio" or root.get("handle") != ROOT or options.get("bands") != 3 \
            or options.get("priomap") != [0] * 16:
        raise CheckError("The root qdisc is not the owned prio qdisc: " + json.dumps(root), "runtime_failure")
    if (child or {}).get("kind") != "netem" or child.get("parent") != BAND:
        raise CheckError("The owned netem child is missing: " + json.dumps(child), "runtime_failure")
    options = child.get("options", {})
    delay = options.get("delay", {})
    faults, gap = netem_faults(options)
    return {"delay_ms": round(delay.get("delay", 0) * 1000, 3), "jitter_ms": round(delay.get("jitter", 0) * 1000, 3),
            "loss": options.get("loss-random"), "rate_bit": netem_rate(options), "limit": options.get("limit"),
            "faults": faults, "gap": gap, "requeues": child.get("requeues"),
            "counters": {key: child.get(key) for key in ("packets", "bytes", "drops", "backlog", "qlen")}}


def netem_faults(options):
    """netem's reorder, duplicate and corrupt members in percent, and its
    reorder gap. tc prints each member as an object with the probability
    under the member's own name and a correlation, both fractions, and no
    member without the fault. A member that is not such an object is not
    netem's and reads as no fault; a missing or unreadable correlation reads
    as None, which no request matches."""
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    faults = {}
    for name in FAULTS:
        member = options.get(name)
        if not isinstance(member, dict) or not number(member.get(name)):
            faults[name] = None
            continue
        correlation = member.get("correlation")
        faults[name] = {"percent": round(member[name] * 100, 6),
                        "correlation": round(correlation * 100, 6) if number(correlation) else None}
    gap = options.get("gap")
    return faults, gap if isinstance(gap, int) and not isinstance(gap, bool) else None


def netem_rate(options):
    """netem's rate in bit/s: tc prints it as an object with bytes per
    second. Other qdiscs, such as tbf, print a plain number under the same
    key; it is not netem's rate, and reads as zero, as does no rate."""
    rate = options.get("rate")
    if not isinstance(rate, dict) or not isinstance(rate.get("rate"), int):
        return 0
    return rate["rate"] * 8


def requests(container, host, port, count, timeout, label, output):
    code, text = docker("exec", container, "python3", "-c", SELECT_CLIENT, host, str(port), str(count), str(RESPONSE),
                        str(timeout), check_exit=False, timeout=count * timeout + 60)
    with (output / "timings.log").open("a") as log:
        log.write(label + ": " + text)
    if code:
        raise CheckError("The request client failed (" + label + "): " + text.strip(), "runtime_failure")
    return json.loads(text.strip().splitlines()[-1])


def drained(shaper, interface):
    """Waits until the owned child holds no packet, within DRAIN_BUDGET. A
    completed read does not show that the qdisc is empty, and an empty queue
    observed after the deadline does not show that it drained in time. Every
    read gets only what remains of the budget."""
    started = time.monotonic()
    deadline = started + DRAIN_BUDGET
    counters, reason = None, "budget_exhausted"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = tc(shaper, "-j", "-s", "qdisc", "show", "dev", interface, timeout=remaining)
        except CheckError as error:
            if error.kind != "timeout":
                raise
            reason = "read_timeout"
            break
        observed = time.monotonic()
        counters = selector_qdiscs(raw)["counters"]
        if observed > deadline:
            reason = "observed_after_deadline"
            break
        if counters["backlog"] == 0 and counters["qlen"] == 0:
            return {"status": "drained", "elapsed_ms": round((observed - started) * 1000, 1), "counters": counters}
        time.sleep(max(0.0, min(DRAIN_POLL, deadline - time.monotonic())))
    # The last counters read, if any; an unobserved queue is not empty.
    return {"status": "drain_pending", "reason": reason,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1), "counters": counters}


def apply_loss(shaper, device, drain):
    """Changes the owned child to 100% loss, only after it drained in time."""
    if drain["status"] != "drained":
        raise CheckError("The owned child did not drain within %.1f s (%s)" % (DRAIN_BUDGET, drain.get("reason")),
                         "runtime_failure")
    tc(shaper, "qdisc", "change", "dev", device, "parent", BAND, "handle", CHILD, "netem", "loss", "100%")


def install_hierarchy(shaper, device, leaves, delay, netem=None):
    """The owned root and child, then each leaf. Until a leaf exists, every
    packet takes the unshaped band."""
    install_root(shaper, device, delay, netem)
    install_leaves(shaper, device, leaves)


def install_root(shaper, device, delay, netem=None):
    """The owned prio root and its netem child, with a delay, or with the
    netem options given."""
    tc(shaper, "qdisc", "add", "dev", device, "root", "handle", ROOT, "prio", "bands", "3", "priomap", *["0"] * 16)
    tc(shaper, "qdisc", "add", "dev", device, "parent", BAND, "handle", CHILD, "netem",
       *(netem if netem is not None else ["delay", "%dms" % delay]))


def install_leaves(shaper, device, leaves):
    for leaf in leaves:
        match = "ip" if leaf["family"] == 4 else "ip6"
        tc(shaper, "filter", "add", "dev", device, "protocol", leaf["protocol"], "parent", ROOT,
           "pref", str(leaf["pref"]), "handle", leaf["handle"], "u32",
           "match", match, "dst", "%s/%d" % (leaf["peer"], 32 if leaf["family"] == 4 else 128),
           "match", match, "protocol", "6", "0xff", "match", match, "dport", str(leaf["port"]), "0xffff",
           "flowid", BAND)


def remove_hierarchy(shaper, device, leaves):
    """The selection stops first, leaf by leaf in reverse order, then the
    owned qdiscs go. Removing the last leaf of a preference can remove its
    whole chain; only a preference whose table outlived its leaves is
    removed as a whole. Returns those preferences."""
    for leaf in reversed(leaves):
        tc(shaper, "filter", "del", "dev", device, "parent", ROOT, "protocol", leaf["protocol"],
           "pref", str(leaf["pref"]), "handle", leaf["handle"], "u32")
    remaining = remaining_filters(json.loads(tc(shaper, "-j", "filter", "show", "dev", device),
                                             object_pairs_hook=Pairs), leaves)
    for protocol, pref in remaining:
        tc(shaper, "filter", "del", "dev", device, "parent", ROOT, "protocol", protocol, "pref", str(pref))
    tc(shaper, "qdisc", "del", "dev", device, "root", "handle", ROOT)
    return [pref for _, pref in remaining]


def selector_scenario(helper, target, run, profile, delay, output):
    """Delay, then drop, only the selected peers, families and ports of the
    target's egress, and keep every other path unaffected. Then check that
    the owned hierarchy delays nothing before its leaves, outlives a helper
    that exits, is refused with foreign state under it, is removed leaf
    first, and ends with the target's namespace."""
    leaves = layout(profile)
    label = LABEL + "=" + run
    network = "ceph-extension-check-" + run
    roles = ("target", "shaper", "driver", "observer", "fresh") + PEERS
    names = {role: network + "-" + role for role in roles}
    result = {"profile": profile, "reference": target["reference"], "image_id": target["image_id"], "status": "running"}
    output.mkdir()
    common = ["--detach", "--label", label, "--pull=never", "--user=0:0", "--platform", helper["platform"]]
    servers = ["--entrypoint=python3", helper["image_id"], "-c", PEER_SERVER]
    try:
        docker("network", "create", "--internal", "--ipv6", "--label", label, network)
        docker("run", "--detach", "--label", label, "--pull=never", "--platform", target["platform"], "--name", names["target"],
               "--network", network, "--entrypoint=sleep", target["image_id"], "infinity")
        shared = "container:" + names["target"]
        docker("run", *common, "--name", names["shaper"], "--network", shared, "--cap-drop=ALL",
               "--cap-add=NET_ADMIN", "--entrypoint=sleep", helper["image_id"], "infinity")
        docker("run", *common, "--name", names["driver"], "--network", shared, "--cap-drop=ALL", *servers, str(REVERSE_PORT))
        for peer in PEERS:
            docker("run", *common, "--name", names[peer], "--network", network, "--cap-drop=ALL", *servers,
                   str(SELECTED_PORT), str(OTHER_PORT))
        endpoints = {}
        for role in ("target",) + PEERS:
            inspect = json.loads(docker("container", "inspect", names[role])[1])[0]
            endpoint = inspect["NetworkSettings"]["Networks"][network]
            endpoints[role] = {4: endpoint["IPAddress"], 6: endpoint["GlobalIPv6Address"], "mac": endpoint["MacAddress"],
                               "sandbox_id": inspect["NetworkSettings"]["SandboxID"]}
            if not endpoints[role][4] or not endpoints[role][6]:
                raise CheckError("The network gave %s no IPv4 or no IPv6 address" % role, "runtime_failure")
        result["sandbox_id"] = endpoints["target"]["sandbox_id"]
        namespaces = {docker("exec", names[role], "python3", "-c", NAMESPACE)[1].strip() for role in ("shaper", "driver")}
        if len(namespaces) != 1:
            raise CheckError("The helpers did not join one namespace: " + ", ".join(sorted(namespaces)))
        addresses = json.loads(docker("exec", names["shaper"], "ip", "-j", "address", "show")[1])
        interface = target_interface(addresses, endpoints["target"][leaves[0]["family"]], endpoints["target"]["mac"])
        device = interface["name"]
        for leaf in leaves:
            leaf["peer"] = endpoints[leaf["role"]][leaf["family"]]
        result.update(interface=interface, selected=[{key: leaf[key] for key in ("family", "role", "peer", "port",
                                                                                  "protocol", "pref", "handle")}
                                                     for leaf in leaves])
        selected = {path_name(leaf["role"], leaf["family"], leaf["port"]): (names["driver"], leaf["peer"], leaf["port"])
                    for leaf in leaves}
        # Every other peer, family and port, and a reverse connection of each
        # family from peer B to the target, are controls.
        controls = {path_name(role, family, port): (names["driver"], endpoints[role][family], port)
                    for role in PEERS for family in (4, 6) for port in (SELECTED_PORT, OTHER_PORT)
                    if path_name(role, family, port) not in selected}
        for family in (4, 6):
            controls["reverse ipv%d" % family] = (names["peer-b"], endpoints["target"][family], REVERSE_PORT)
        for name, (client, host, port) in list(selected.items()) + list(controls.items()):
            code, text = docker("exec", client, "python3", "-c", READY, host, str(port), check_exit=False)
            if code:
                raise CheckError("No answer on the %s path: %s" % (name, text.strip()), "runtime_failure")

        def measure_paths(paths, phase, count=CONTROL_SAMPLES, timeout=10):
            return {name: requests(client, host, port, count, timeout, phase + " " + name, output)
                    for name, (client, host, port) in paths.items()}

        def qdiscs(helper_name):
            return selector_qdiscs(tc(names[helper_name], "-j", "-s", "qdisc", "show", "dev", device))

        result["root_before"] = root_qdisc(names["shaper"], device)
        result["baseline"] = measure_paths(selected, "baseline", SELECTOR_SAMPLES)
        # The root and its child alone select nothing: an apply that stops
        # before its leaves delays no packet.
        install_root(names["shaper"], device, delay)
        result["partial"] = measure_paths(selected, "partial")
        result["after_partial"] = qdiscs("shaper")
        install_leaves(names["shaper"], device, leaves)
        # Without a parent, tc prints each entry's parent, protocol and
        # preference, and every egress filter of the device.
        raw = tc(names["shaper"], "-j", "filter", "show", "dev", device)
        (output / "filter.json").write_text(raw)
        result["selectors"] = selector_leaves(raw, leaves)
        result["applied"] = qdiscs("shaper")
        # Controls run before selected traffic, so the child must count none.
        result["controls_delayed"] = measure_paths(controls, "delayed")
        result["after_controls"] = qdiscs("shaper")
        result["delayed"] = measure_paths(selected, "delayed", SELECTOR_SAMPLES)
        result["after_delayed"] = qdiscs("shaper")
        result["drain"] = drained(names["shaper"], device)
        apply_loss(names["shaper"], device, result["drain"])
        result["lost"] = measure_paths(selected, "loss", 1, LOSS_TIMEOUT)
        result["controls_lost"] = measure_paths(controls, "loss")
        result["after_loss"] = qdiscs("shaper")
        # A filter that another owner adds under the root is foreign state:
        # the readback refuses it, and accepts the hierarchy again once that
        # owner removed it.
        tc(names["shaper"], "filter", "add", "dev", device, "protocol", "ip", "parent", ROOT, "pref", str(FOREIGN_PREF),
           "u32", "match", "ip", "dst", "192.0.2.1/32", "flowid", ROOT + "1")
        result["foreign"] = {"refused": None}
        try:
            selector_leaves(tc(names["shaper"], "-j", "filter", "show", "dev", device), leaves)
        except CheckError as error:
            result["foreign"]["refused"] = str(error)
        tc(names["shaper"], "filter", "del", "dev", device, "parent", ROOT, "protocol", "ip", "pref", str(FOREIGN_PREF))
        result["foreign"]["after_removal"] = len(selector_leaves(tc(names["shaper"], "-j", "filter", "show", "dev",
                                                                    device), leaves))
        # Removing the helper does not remove the hierarchy from the live
        # namespace: a new helper finds every leaf and the child.
        docker("rm", "--force", names["shaper"])
        docker("run", *common, "--name", names["observer"], "--network", shared, "--cap-drop=ALL",
               "--cap-add=NET_ADMIN", "--entrypoint=sleep", helper["image_id"], "infinity")
        result["after_helper_exit"] = {
            "selectors": len(selector_leaves(tc(names["observer"], "-j", "filter", "show", "dev", device), leaves)),
            "loss": qdiscs("observer")["loss"]}
        result["removed_preferences"] = remove_hierarchy(names["observer"], device, leaves)
        result["root_after"] = root_qdisc(names["observer"], device)
        result["filters_after"] = json.loads(tc(names["observer"], "-j", "filter", "show", "dev", device))
        result["restored"] = measure_paths(selected, "restored", SELECTOR_SAMPLES)
        # The whole hierarchy again, then a restart: Docker gives the target
        # a new namespace without it, and the old helper loses the interface.
        install_hierarchy(names["observer"], device, leaves, delay)
        selector_leaves(tc(names["observer"], "-j", "filter", "show", "dev", device), leaves)
        docker("restart", "-t", "1", names["target"])
        inspect = json.loads(docker("container", "inspect", names["target"])[1])[0]
        old = json.loads(docker("exec", names["observer"], "ip", "-j", "address", "show")[1])
        docker("run", *common, "--name", names["fresh"], "--network", shared, "--cap-drop=ALL",
               "--entrypoint=sleep", helper["image_id"], "infinity")
        result["restart"] = {
            "sandbox_id": inspect["NetworkSettings"]["SandboxID"],
            "old_namespace_interfaces": [link["ifname"] for link in old if link.get("address") == interface["mac"]],
            "new_root": root_qdisc(names["fresh"], device).get("kind"),
            "new_filters": json.loads(tc(names["fresh"], "-j", "filter", "show", "dev", device))}
        problems = selector_problems(result, delay)
        if problems:
            raise CheckError("; ".join(problems), "runtime_failure")
        result["status"] = "passed"
    except (check.CheckError, KeyError, ValueError) as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    finally:
        result["cleanup"] = cleanup(label, output)
        if result["cleanup"]["status"] != "passed":
            result["status"] = "failed"
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def selector_problems(result, delay):
    """What a selector scenario's observations do not show."""
    problems = []
    fast = delay / 4

    def median(observation):
        return observation["median_ms"] if observation["answered"] == observation["count"] else None

    for phase in ("baseline", "partial", "restored", "controls_delayed", "controls_lost"):
        for name, observation in result[phase].items():
            value = median(observation)
            if value is None or value >= fast:
                problems.append("%s %s: %s" % (phase, name, json.dumps(observation)))
    if result["after_partial"]["counters"]["packets"] != 0:
        problems.append("the child counted %r packets before any leaf" % result["after_partial"]["counters"]["packets"])
    if result["applied"]["delay_ms"] != delay:
        problems.append("applied delay %r" % result["applied"]["delay_ms"])
    if result["after_controls"]["counters"]["packets"] != 0:
        problems.append("the child counted %r packets of control traffic" % result["after_controls"]["counters"]["packets"])
    for name, observation in result["delayed"].items():
        value = median(observation)
        if value is None or value < delay:
            problems.append("selected path %s delayed: %s" % (name, json.dumps(observation)))
    if not result["after_delayed"]["counters"]["packets"]:
        problems.append("the child counted no selected packets")
    for name, observation in result["lost"].items():
        if observation["outcomes"] != ["timeout"]:
            problems.append("selected path %s under loss: %s" % (name, json.dumps(observation)))
    if not result["after_loss"]["counters"]["drops"]:
        problems.append("the child dropped nothing under loss")
    foreign = result["foreign"]
    if not foreign.get("refused") or foreign.get("after_removal") != len(result["selected"]):
        problems.append("foreign filter state: " + json.dumps(foreign))
    after_exit = result["after_helper_exit"]
    if after_exit["selectors"] != len(result["selected"]) or (after_exit["loss"] or {}).get("loss") != 1:
        problems.append("the hierarchy changed when the helper exited: " + json.dumps(after_exit))
    before, after = result["root_before"], result["root_after"]
    if (after.get("kind"), after.get("handle")) != (before.get("kind"), before.get("handle")):
        problems.append("root qdisc %s %s after removal, %s %s before" % (after.get("kind"), after.get("handle"),
                                                                         before.get("kind"), before.get("handle")))
    if result["filters_after"]:
        problems.append("filters remain: " + json.dumps(result["filters_after"]))
    restarted = result["restart"]
    if restarted["sandbox_id"] == result["sandbox_id"]:
        problems.append("the restarted target kept its network sandbox")
    if restarted["old_namespace_interfaces"]:
        problems.append("the old namespace still has the target's interface after the restart")
    if restarted["new_root"] != before.get("kind") or restarted["new_filters"]:
        problems.append("the restarted target has root %s and filters %s" % (restarted["new_root"],
                                                                            json.dumps(restarted["new_filters"])))
    return problems


def rate_options(rate_bit, limit, delay_ms=0):
    """netem's options for a rate in bit/s and a queue limit in packets."""
    options = ["delay", "%dms" % delay_ms] if delay_ms else []
    return options + ["rate", "%dbit" % rate_bit, "limit", str(limit)]


RATE_PROBE = ("qdisc", "add", "dev", "lo", "root", "handle", RATE_HANDLE, "netem") + \
    tuple(rate_options(RATE_PROBE_BIT, RATE_PROBE_LIMIT))


def rate_parser(image, output):
    """Whether the image's tc parses netem's rate and limit: with no
    capability, the kernel refuses the request, and a tc that cannot parse
    them fails before it asks. This depends on the image alone."""
    container = "ceph-extension-check-" + uuid.uuid4().hex
    result = {"container": container, "command": ["tc", *RATE_PROBE]}
    try:
        docker("run", "--detach", "--name", container, "--label", LABEL + "=rate-parser", "--pull=never",
               "--user=0:0", "--network=none", "--cap-drop=ALL", "--platform", image["platform"],
               "--entrypoint=sleep", image["image_id"], "infinity")
        code, text = docker("exec", container, "tc", *RATE_PROBE, check_exit=False)
        result.update(exit_code=code, output=text.strip())
        if code == 0:
            raise CheckError("tc changed the loopback without NET_ADMIN", "checker_failure")
        if "Operation not permitted" not in text:
            kind = classify(text)
            raise CheckError("tc did not parse netem's rate and limit: " + text.strip(),
                             "image_unsupported" if kind == "image_unsupported" else "runtime_failure")
        result["status"] = "passed"
    except check.CheckError as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    finally:
        result["cleanup"] = remove_container(container)
        if result["cleanup"]["status"] != "passed":
            result.update(status="failed", error=result.get("error", "Could not remove " + container),
                          failure_kind=result.get("failure_kind", "checker_failure"))
        (output / "rate-parser.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def netem_rate_state(qdisc, handle):
    """The rate, queue limit and delay of an owned netem root."""
    if qdisc.get("kind") != "netem" or qdisc.get("handle") != handle:
        raise CheckError("The root qdisc is not the owned netem qdisc: " + json.dumps(qdisc), "runtime_failure")
    options = qdisc.get("options", {})
    return {"rate_bit": netem_rate(options), "limit": options.get("limit"),
            "delay_ms": round(options.get("delay", {}).get("delay", 0) * 1000, 3)}


def rate_kernel(image, output):
    """Add netem's rate and limit on the loopback of a disposable namespace,
    compose them with a delay, read both back from tc's JSON and remove the
    owned handle. tc parsed them already, so a kernel that does not keep them
    is kernel_unsupported."""
    container = "ceph-extension-check-" + uuid.uuid4().hex
    result = {"container": container}
    expected = {"rate_bit": RATE_PROBE_BIT, "limit": RATE_PROBE_LIMIT}
    try:
        docker("run", "--detach", "--name", container, "--label", LABEL + "=rate-kernel", "--pull=never",
               "--user=0:0", "--network=none", "--cap-drop=ALL", "--cap-add=NET_ADMIN", "--platform", image["platform"],
               "--entrypoint=sleep", image["image_id"], "infinity")
        result["before"] = root_qdisc(container, "lo").get("kind")
        tc(container, *RATE_PROBE)
        result["added"] = netem_rate_state(root_qdisc(container, "lo"), RATE_HANDLE)
        tc(container, "qdisc", "change", "dev", "lo", "root", "handle", RATE_HANDLE, "netem",
           *rate_options(RATE_PROBE_BIT, RATE_PROBE_LIMIT, 10))
        result["composed"] = netem_rate_state(root_qdisc(container, "lo"), RATE_HANDLE)
        tc(container, "qdisc", "del", "dev", "lo", "root", "handle", RATE_HANDLE)
        result["removed"] = root_qdisc(container, "lo").get("kind")
        if result["added"] != dict(expected, delay_ms=0) or result["composed"] != dict(expected, delay_ms=10):
            raise CheckError("netem did not keep the rate and limit: " + json.dumps(result), "kernel_unsupported")
        if result["removed"] != result["before"]:
            raise CheckError("The loopback kept %s after the owned qdisc was removed" % result["removed"],
                             "runtime_failure")
        result["status"] = "passed"
    except (check.CheckError, ValueError) as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    finally:
        result["cleanup"] = remove_container(container)
        if result["cleanup"]["status"] != "passed":
            result.update(status="failed", error=result.get("error", "Could not remove " + container),
                          failure_kind=result.get("failure_kind", "checker_failure"))
        (output / "rate-kernel.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def transfers(driver, paths, size, timeout, phase, output, together=False):
    """Sends size bytes on each path from the target's namespace; returns
    the sender's report with each transfer under its path name."""
    command = [str(size), str(timeout), "together" if together else "one-by-one"]
    for name, spec in paths.items():
        command += [spec["host"], str(spec["port"]), phase + " " + name]
    limit = timeout * (len(paths) + 1) + 60
    code, text = docker("exec", driver, "python3", "-c", SENDER, *command, check_exit=False, timeout=limit)
    with (output / "transfers.log").open("a") as log:
        log.write(phase + ": " + text)
    if code:
        raise CheckError("The sender failed (" + phase + "): " + text.strip(), "runtime_failure")
    report = json.loads(text.strip().splitlines()[-1])
    report["transfers"] = dict(zip(paths, report["transfers"]))
    return report


def seen_address(text):
    """The family and address that a dual-stack sink saw a sender come from."""
    address = ipaddress.ip_address(text)
    if address.version == 6 and address.ipv4_mapped:
        return 4, str(address.ipv4_mapped)
    return address.version, str(address)


def transfer_problems(phase, name, transfer, spec, size):
    """Whether a transfer carried exactly its bytes, over its path's family,
    from the target's address of that family."""
    problems = []
    if transfer.get("error"):
        return ["%s %s: %s" % (phase, name, transfer["error"])]
    if transfer.get("sent") != size or transfer.get("received") != size:
        problems.append("%s %s: sent %s and received %s of %d bytes" % (phase, name, transfer.get("sent"),
                                                                        transfer.get("received"), size))
    if transfer.get("sha256") != transfer.get("received_sha256"):
        problems.append("%s %s: the received SHA-256 differs" % (phase, name))
    try:
        seen = seen_address(transfer.get("seen_from", ""))
    except ValueError:
        seen = None
    if transfer.get("family") != spec["family"] or seen != (spec["family"], spec["source"]):
        problems.append("%s %s: sent over IPv%s, seen from %s; expected IPv%d from %s" % (
            phase, name, transfer.get("family"), transfer.get("seen_from"), spec["family"], spec["source"]))
    return problems


def start_transfers(helper, target, names, label, network, common, peers, leaves):
    """The private dual-stack network of a transfer scenario: the target, a
    shaping helper with NET_ADMIN and a sender in the target's namespace, and
    a sink for each peer. Returns each endpoint's addresses and the target's
    interface, and gives each leaf its peer's address."""
    docker("network", "create", "--internal", "--ipv6", "--label", label, network)
    docker("run", "--detach", "--label", label, "--pull=never", "--platform", target["platform"], "--name", names["target"],
           "--network", network, "--entrypoint=sleep", target["image_id"], "infinity")
    shared = "container:" + names["target"]
    docker("run", *common, "--name", names["shaper"], "--network", shared, "--cap-drop=ALL",
           "--cap-add=NET_ADMIN", "--entrypoint=sleep", helper["image_id"], "infinity")
    docker("run", *common, "--name", names["driver"], "--network", shared, "--cap-drop=ALL",
           "--entrypoint=sleep", helper["image_id"], "infinity")
    for peer in peers:
        docker("run", *common, "--name", names[peer], "--network", network, "--cap-drop=ALL",
               "--entrypoint=python3", helper["image_id"], "-c", SINK, str(SELECTED_PORT))
    endpoints = {}
    for role in ("target",) + tuple(peers):
        inspect = json.loads(docker("container", "inspect", names[role])[1])[0]
        endpoint = inspect["NetworkSettings"]["Networks"][network]
        endpoints[role] = {4: endpoint["IPAddress"], 6: endpoint["GlobalIPv6Address"], "mac": endpoint["MacAddress"]}
        if not endpoints[role][4] or not endpoints[role][6]:
            raise CheckError("The network gave %s no IPv4 or no IPv6 address" % role, "runtime_failure")
    namespaces = {docker("exec", names[role], "python3", "-c", NAMESPACE)[1].strip() for role in ("shaper", "driver")}
    if len(namespaces) != 1:
        raise CheckError("The helpers did not join one namespace: " + ", ".join(sorted(namespaces)))
    addresses = json.loads(docker("exec", names["shaper"], "ip", "-j", "address", "show")[1])
    interface = target_interface(addresses, endpoints["target"][4], endpoints["target"]["mac"])
    for leaf in leaves:
        leaf["peer"] = endpoints[leaf["role"]][leaf["family"]]
    return endpoints, interface


def transfer_path(endpoints, role, family):
    """A transfer from the target to a peer over one family, with the
    target's address that the peer must see."""
    return {"host": endpoints[role][family], "port": SELECTED_PORT, "family": family,
            "source": str(ipaddress.ip_address(endpoints["target"][family]))}


def wait_for_sinks(driver, paths):
    for name, spec in paths.items():
        code, text = docker("exec", driver, "python3", "-c", READY, spec["host"], str(spec["port"]), check_exit=False)
        if code:
            raise CheckError("No answer on the %s path: %s" % (name, text.strip()), "runtime_failure")


def rate_scenario(helper, target, run, rate_bit, output):
    """Limit the selected peers of the target's egress to a rate: baseline,
    rate, rate with a delay, a short queue, then restore. Every transfer must
    carry exactly its bytes over the selected family, the selected paths must
    stay under the rate and share it, an unselected peer must stay fast, and
    the restore must leave the original root qdisc, no filter and no helper."""
    leaves = layout(RATE_LEAVES)
    label = LABEL + "=" + run
    network = "ceph-extension-check-" + run
    roles = ("target", "shaper", "driver", "observer", "peer-b", RATE_CONTROL)
    names = {role: network + "-" + role for role in roles}
    rate_Bps = rate_bit // 8
    size = rate_Bps * RATE_TRANSFER_SECONDS
    timeout = RATE_TRANSFER_SECONDS * 15 + 30
    result = {"reference": target["reference"], "image_id": target["image_id"], "rate_bit": rate_bit,
              "queue_limit": DEFAULT_QUEUE_LIMIT, "size": size, "status": "running"}
    output.mkdir()
    common = ["--detach", "--label", label, "--pull=never", "--user=0:0", "--platform", helper["platform"]]
    try:
        endpoints, interface = start_transfers(helper, target, names, label, network, common, ("peer-b", RATE_CONTROL),
                                               leaves)
        device = interface["name"]
        shared = "container:" + names["target"]
        selected = {path_name(leaf["role"], leaf["family"], leaf["port"]): transfer_path(endpoints, leaf["role"],
                                                                                          leaf["family"])
                    for leaf in leaves}
        controls = {path_name(RATE_CONTROL, family, SELECTED_PORT): transfer_path(endpoints, RATE_CONTROL, family)
                    for family in (4, 6)}
        result.update(interface=interface, selected=selected, controls=controls)
        wait_for_sinks(names["driver"], dict(selected, **controls))

        def send(paths, phase, together=False):
            return transfers(names["driver"], paths, size, timeout, phase, output, together)

        def qdiscs(helper_name):
            return selector_qdiscs(tc(names[helper_name], "-j", "-s", "qdisc", "show", "dev", device))

        def change(netem):
            tc(names["shaper"], "qdisc", "change", "dev", device, "parent", BAND, "handle", CHILD, "netem", *netem)

        result["root_before"] = root_qdisc(names["shaper"], device)
        result["baseline"] = send(dict(selected, **controls), "baseline")
        install_hierarchy(names["shaper"], device, leaves, 0, rate_options(rate_bit, DEFAULT_QUEUE_LIMIT))
        raw = tc(names["shaper"], "-j", "filter", "show", "dev", device)
        (output / "filter.json").write_text(raw)
        result["selectors"] = selector_leaves(raw, leaves)
        result["applied"] = qdiscs("shaper")
        # The unselected peer runs first, so the child must count none of it.
        result["controls_rated"] = send(controls, "rated")
        result["after_controls"] = qdiscs("shaper")
        result["rated"] = send(selected, "rated")
        result["after_rated"] = qdiscs("shaper")
        # Both selected paths at once share the one rate.
        result["shared"] = send(selected, "shared", together=True)
        result["after_shared"] = qdiscs("shaper")
        # The rate composes with a delay in the same netem qdisc.
        change(rate_options(rate_bit, DEFAULT_QUEUE_LIMIT, RATE_DELAY_MS))
        result["composed_applied"] = qdiscs("shaper")
        first = dict([next(iter(selected.items()))])
        result["composed"] = send(first, "composed")
        # A short queue overflows: netem drops and counts what finds it full,
        # and TCP still delivers every byte.
        change(rate_options(rate_bit, QUEUE_PROBE_LIMIT))
        result["queue_applied"] = qdiscs("shaper")
        result["queue"] = send(first, "queue")
        result["after_queue"] = qdiscs("shaper")
        result["removed_preferences"] = remove_hierarchy(names["shaper"], device, leaves)
        result["root_after"] = root_qdisc(names["shaper"], device)
        result["filters_after"] = json.loads(tc(names["shaper"], "-j", "filter", "show", "dev", device))
        # No helper stays behind, and a new one without NET_ADMIN finds the
        # original root qdisc and no filter.
        docker("rm", "--force", names["shaper"])
        result["shaper_left"] = docker("ps", "--all", "--quiet", "--filter", "name=^" + names["shaper"] + "$")[1].split()
        docker("run", *common, "--name", names["observer"], "--network", shared, "--cap-drop=ALL",
               "--entrypoint=sleep", helper["image_id"], "infinity")
        result["observed_root"] = root_qdisc(names["observer"], device)
        result["observed_filters"] = json.loads(tc(names["observer"], "-j", "filter", "show", "dev", device))
        result["restored"] = send(selected, "restored")
        problems = rate_problems(result, rate_bit)
        if problems:
            raise CheckError("; ".join(problems), "runtime_failure")
        result["status"] = "passed"
    except (check.CheckError, KeyError, ValueError) as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    finally:
        result["cleanup"] = cleanup(label, output)
        if result["cleanup"]["status"] != "passed":
            result["status"] = "failed"
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def rate_problems(result, rate_bit):
    """What a rate scenario's observations do not show. The configured state
    comes from tc; the throughput is what the transfers achieved."""
    problems = []
    rate_Bps, size = rate_bit / 8, result["size"]
    specs = dict(result["selected"], **result["controls"])

    def goodput(phase, name, transfer, low, high=None):
        value = transfer.get("goodput_Bps")
        if value is None or value < low * rate_Bps or (high is not None and value > high * rate_Bps):
            problems.append("%s %s: %s B/s, expected %s%s times the rate of %d B/s" % (
                phase, name, value, low, " to %s" % high if high is not None else " or more", rate_Bps))

    for phase in ("baseline", "controls_rated", "rated", "shared", "composed", "queue", "restored"):
        for name, transfer in result[phase]["transfers"].items():
            problems += transfer_problems(phase, name, transfer, specs[name], size)
    for phase in ("baseline", "controls_rated", "restored"):
        for name, transfer in result[phase]["transfers"].items():
            goodput(phase, name, transfer, UNSHAPED_FLOOR)
    for name, transfer in result["rated"]["transfers"].items():
        goodput("rated", name, transfer, RATE_FLOOR, RATE_CEILING)
    shared = result["shared"]["aggregate_Bps"]
    if shared is None or not RATE_FLOOR * rate_Bps <= shared <= RATE_CEILING * rate_Bps:
        problems.append("shared: %s B/s together, expected %s to %s times the rate" % (shared, RATE_FLOOR, RATE_CEILING))
    for name, transfer in result["composed"]["transfers"].items():
        goodput("composed", name, transfer, COMPOSED_FLOOR, RATE_CEILING)
        if (transfer.get("connect_ms") or 0) < RATE_DELAY_MS:
            problems.append("composed %s: connected in %s ms under a %d ms delay" % (name, transfer.get("connect_ms"),
                                                                                    RATE_DELAY_MS))
    for name, transfer in result["queue"]["transfers"].items():
        goodput("queue", name, transfer, 0, RATE_CEILING)
    for phase, delay, limit in (("applied", 0, DEFAULT_QUEUE_LIMIT), ("composed_applied", RATE_DELAY_MS, DEFAULT_QUEUE_LIMIT),
                                ("queue_applied", 0, QUEUE_PROBE_LIMIT)):
        state = result[phase]
        if (state["rate_bit"], state["limit"], state["delay_ms"]) != (rate_bit, limit, delay):
            problems.append("%s: tc reports rate %s bit/s, limit %s, delay %s ms; requested %d, %d, %d" % (
                phase, state["rate_bit"], state["limit"], state["delay_ms"], rate_bit, limit, delay))
    if result["after_controls"]["counters"]["packets"] != 0:
        problems.append("the child counted %r packets of the unselected peer" % result["after_controls"]["counters"]["packets"])
    if not result["after_rated"]["counters"]["packets"]:
        problems.append("the child counted no selected packets")
    if not result["after_queue"]["counters"]["drops"] > result["queue_applied"]["counters"]["drops"]:
        problems.append("the child dropped nothing with a queue of %d packets: %s"
                        % (QUEUE_PROBE_LIMIT, json.dumps(result["after_queue"]["counters"])))
    before = result["root_before"]
    for phase in ("root_after", "observed_root"):
        after = result[phase]
        if (after.get("kind"), after.get("handle")) != (before.get("kind"), before.get("handle")):
            problems.append("%s: root qdisc %s %s, %s %s before" % (phase, after.get("kind"), after.get("handle"),
                                                                    before.get("kind"), before.get("handle")))
    if result["filters_after"] or result["observed_filters"]:
        problems.append("filters remain: " + json.dumps(result["filters_after"] or result["observed_filters"]))
    if result["shaper_left"]:
        problems.append("the shaping helper remains: " + ", ".join(result["shaper_left"]))
    return problems


def fault_command(netem):
    return ("qdisc", "add", "dev", "lo", "root", "handle", FAULT_HANDLE, "netem") + tuple(netem)


def faults_parser(image, output):
    """Whether the image's tc parses every fault with a correlation and the
    zero-delay duplicate encoding: with no capability, the kernel refuses
    each request, and a tc that cannot parse one fails before it asks. This
    depends on the image alone. A reorder without a delay is recorded: tc
    refuses it itself."""
    container = "ceph-extension-check-" + uuid.uuid4().hex
    result = {"container": container, "probes": {}}
    try:
        docker("run", "--detach", "--name", container, "--label", LABEL + "=faults-parser", "--pull=never",
               "--user=0:0", "--network=none", "--cap-drop=ALL", "--platform", image["platform"],
               "--entrypoint=sleep", image["image_id"], "infinity")
        for name, spec in FAULT_PROBES.items():
            command = fault_command(spec["netem"])
            code, text = docker("exec", container, "tc", *command, check_exit=False)
            result["probes"][name] = {"command": ["tc", *command], "exit_code": code, "output": text.strip()}
            if code == 0:
                raise CheckError("tc changed the loopback without NET_ADMIN", "checker_failure")
            if "Operation not permitted" not in text:
                kind = classify(text)
                raise CheckError("tc did not parse the %s probe: %s" % (name, text.strip()),
                                 "image_unsupported" if kind == "image_unsupported" else "runtime_failure")
        command = fault_command(REORDER_WITHOUT_DELAY)
        code, text = docker("exec", container, "tc", *command, check_exit=False)
        result["reorder_without_delay"] = {
            "command": ["tc", *command], "exit_code": code, "output": text.strip(),
            "outcome": "refused_by_tc" if "reordering not possible" in text else
                       "reached_kernel" if "Operation not permitted" in text else "other"}
        result["status"] = "passed"
    except check.CheckError as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    finally:
        result["cleanup"] = remove_container(container)
        if result["cleanup"]["status"] != "passed":
            result.update(status="failed", error=result.get("error", "Could not remove " + container),
                          failure_kind=result.get("failure_kind", "checker_failure"))
        (output / "faults-parser.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def read_json(text, what):
    """tc's or a counter reader's JSON; anything else is a runtime failure,
    not a pass."""
    try:
        return json.loads(text)
    except ValueError as error:
        raise CheckError("%s is not JSON: %s" % (what, text.strip()[:200]), "runtime_failure") from error


def fault_root_state(qdisc):
    """The delay, jitter, rate, faults and gap of the owned netem root."""
    if qdisc.get("kind") != "netem" or qdisc.get("handle") != FAULT_HANDLE:
        raise CheckError("The root qdisc is not the owned netem qdisc: " + json.dumps(qdisc), "runtime_failure")
    options = qdisc.get("options") or {}
    delay = options.get("delay") or {}
    faults, gap = netem_faults(options)
    return {"delay_ms": round(delay.get("delay", 0) * 1000, 3), "jitter_ms": round(delay.get("jitter", 0) * 1000, 3),
            "rate_bit": netem_rate(options), "faults": faults, "gap": gap}


def fault_mismatches(spec, state):
    """How an installed netem state differs from a requested one: each
    fault's percent and correlation, the gap, the delay, jitter and rate."""
    problems = []
    for name in FAULTS:
        wanted, got = spec["faults"][name], state["faults"][name]
        if wanted is None:
            if got is not None:
                problems.append("%s %s is installed but was not requested" % (name, json.dumps(got)))
        elif got is None:
            problems.append("%s is missing" % name)
        elif got["correlation"] is None:
            problems.append("%s has no correlation" % name)
        elif abs(got["percent"] - wanted[0]) > 1e-4 or abs(got["correlation"] - wanted[1]) > 1e-4:
            problems.append("%s is %s%% with correlation %s%%, requested %s%% and %s%%" % (
                name, got["percent"], got["correlation"], wanted[0], wanted[1]))
    if state["gap"] != spec["gap"]:
        problems.append("gap %s, expected %s" % (state["gap"], spec["gap"]))
    for key in ("delay_ms", "jitter_ms", "rate_bit"):
        if state[key] != spec[key]:
            problems.append("%s %s, requested %s" % (key, state[key], spec[key]))
    return problems


def faults_kernel(image, output):
    """Install each fault probe on the loopback of a disposable namespace,
    read every probability, correlation and the gap back from tc's JSON, and
    remove the owned handle. tc parsed them already, so a kernel that does
    not keep one is kernel_unsupported. The plain duplicate encoding is
    recorded, not relied on."""
    container = "ceph-extension-check-" + uuid.uuid4().hex
    result = {"container": container, "probes": {}}
    try:
        docker("run", "--detach", "--name", container, "--label", LABEL + "=faults-kernel", "--pull=never",
               "--user=0:0", "--network=none", "--cap-drop=ALL", "--cap-add=NET_ADMIN", "--platform", image["platform"],
               "--entrypoint=sleep", image["image_id"], "infinity")
        result["kernel"] = docker("exec", container, "python3", "-c", KERNEL)[1].strip()
        result["before"] = root_qdisc(container, "lo").get("kind")

        def install(netem):
            tc(container, *fault_command(netem))
            try:
                return fault_root_state(root_qdisc(container, "lo"))
            finally:
                tc(container, "qdisc", "del", "dev", "lo", "root", "handle", FAULT_HANDLE)

        problems, unremoved = [], []
        for name, spec in FAULT_PROBES.items():
            probe = result["probes"][name] = {"command": ["tc", *fault_command(spec["netem"])],
                                              "requested": {key: spec[key] for key in ("faults", "gap", "delay_ms")}}
            probe["installed"] = install(spec["netem"])
            probe["removed"] = root_qdisc(container, "lo").get("kind")
            problems += [name + ": " + problem for problem in fault_mismatches(spec, probe["installed"])]
            if probe["removed"] != result["before"]:
                unremoved.append(name)
        plain = result["plain_duplicate"] = {"command": ["tc", *fault_command(PLAIN_DUPLICATE)]}
        try:
            plain["installed"] = install(PLAIN_DUPLICATE)
            plain["correlation_kept"] = (plain["installed"]["faults"]["duplicate"] or {}).get("correlation") == 10
        except CheckError as error:
            plain["error"] = str(error)
        result["removed"] = root_qdisc(container, "lo").get("kind")
        if unremoved or result["removed"] != result["before"]:
            raise CheckError("The loopback kept another root qdisc after the owned one was removed: " +
                             ", ".join(unremoved or ["plain duplicate"]), "runtime_failure")
        if problems:
            raise CheckError("netem did not keep the packet faults: " + "; ".join(problems), "kernel_unsupported")
        result["status"] = "passed"
    except check.CheckError as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    except ValueError as error:
        result.update(status="failed", error="tc printed JSON that cannot be read: " + str(error),
                      failure_kind="runtime_failure")
    finally:
        result["cleanup"] = remove_container(container)
        if result["cleanup"]["status"] != "passed":
            result.update(status="failed", error=result.get("error", "Could not remove " + container),
                          failure_kind=result.get("failure_kind", "checker_failure"))
        (output / "faults-kernel.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def receiver_counters(container):
    """The raw TCP and IP counters of a receiver's namespace that the effects
    are read from. A missing or unreadable counter fails the scenario: an
    effect that cannot be observed is not a pass."""
    code, text = docker("exec", container, "python3", "-c", COUNTERS, check_exit=False)
    if code:
        raise CheckError("The receiver's counters could not be read: " + text.strip(), "runtime_failure")
    tables = read_json(text.strip().splitlines()[-1] if text.strip() else "", "The receiver's counters")
    counters = {}
    for table, names in RAW_COUNTERS.items():
        for name in names:
            value = (tables.get(table) or {}).get(name) if isinstance(tables, dict) else None
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CheckError("The receiver reports no %s.%s counter: %s" % (table, name, json.dumps(value)),
                                 "runtime_failure")
            counters[table + "." + name] = value
    return counters


def counter_delta(before, after):
    """The effect counters that one transfer raised; a counter that went
    back is a broken observation."""
    raw = {key: after[key] - before[key] for key in before}
    if any(value < 0 for value in raw.values()):
        raise CheckError("A receiver counter went back: " + json.dumps(raw), "runtime_failure")
    effects = {name: sum(raw[key] for key in keys) for name, keys in EFFECT_COUNTERS.items()}
    return dict(effects, raw=raw)


def offloads(container, interface):
    code, text = docker("exec", container, "python3", "-c", OFFLOADS, interface, check_exit=False)
    if code:
        return {"error": text.strip()[-300:]}
    try:
        return json.loads(text.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": text.strip()[-300:]}


def fault_scenario(helper, target, run, output):
    """Each packet fault on the selected peers of the target's egress, one
    hierarchy per effect: baseline, then for each effect the unselected peer
    first and the selected peer over IPv4 and IPv6, each transfer observed in
    the receiver's kernel counters, then the hierarchy removed. Every
    transfer must carry exactly its bytes; the restore must leave the
    original root qdisc, no filter and no helper, and the counters at their
    noise."""
    leaves = layout(FAULT_LEAVES)
    label = LABEL + "=" + run
    network = "ceph-extension-check-" + run
    roles = ("target", "shaper", "driver", "observer", "peer-b", FAULT_CONTROL)
    names = {role: network + "-" + role for role in roles}
    size = FAULT_TRANSFER_BYTES
    result = {"reference": target["reference"], "image_id": target["image_id"], "helper_image_id": helper["image_id"],
              "size": size, "status": "running", "effects": {}}
    output.mkdir()
    common = ["--detach", "--label", label, "--pull=never", "--user=0:0", "--platform", helper["platform"]]
    try:
        endpoints, interface = start_transfers(helper, target, names, label, network, common, ("peer-b", FAULT_CONTROL),
                                               leaves)
        device = interface["name"]
        shared = "container:" + names["target"]
        selected = {path_name(leaf["role"], leaf["family"], leaf["port"]): transfer_path(endpoints, leaf["role"],
                                                                                          leaf["family"])
                    for leaf in leaves}
        controls = {path_name(FAULT_CONTROL, family, SELECTED_PORT): transfer_path(endpoints, FAULT_CONTROL, family)
                    for family in (4, 6)}
        receivers = {name: names[name.split()[0]] for name in list(selected) + list(controls)}
        result.update(interface=interface, selected=selected, controls=controls)
        wait_for_sinks(names["driver"], dict(selected, **controls))
        peer_addresses = json.loads(docker("exec", names["peer-b"], "ip", "-j", "address", "show")[1])
        peer_interface = target_interface(peer_addresses, endpoints["peer-b"][4], endpoints["peer-b"]["mac"])
        result["offloads"] = {"sender": offloads(names["driver"], device),
                              "receiver": offloads(names["peer-b"], peer_interface["name"])}

        def observe(paths, phase):
            """Each path's transfer alone, between two reads of its
            receiver's counters."""
            observed = {}
            for name, spec in paths.items():
                before = receiver_counters(receivers[name])
                transfer = transfers(names["driver"], {name: spec}, size, FAULT_TRANSFER_TIMEOUT, phase, output)
                after = receiver_counters(receivers[name])
                observed[name] = {"transfer": transfer["transfers"][name], "counters": counter_delta(before, after)}
            return observed

        def qdiscs():
            return selector_qdiscs(tc(names["shaper"], "-j", "-s", "qdisc", "show", "dev", device))

        result["root_before"] = root_qdisc(names["shaper"], device)
        result["baseline"] = observe(dict(selected, **controls), "baseline")
        for effect, spec in FAULT_EFFECTS.items():
            phase = result["effects"][effect] = {
                "requested": {key: spec[key] for key in ("netem", "faults", "gap", "delay_ms", "rate_bit", "counters")}}
            install_hierarchy(names["shaper"], device, leaves, 0, spec["netem"])
            raw = tc(names["shaper"], "-j", "filter", "show", "dev", device)
            (output / ("filter-" + effect + ".json")).write_text(raw)
            phase["selectors"] = selector_leaves(raw, leaves)
            phase["applied"] = qdiscs()
            # The unselected peer runs first, so the child must count none of it.
            phase["controls"] = observe(controls, effect + " control")
            phase["after_controls"] = qdiscs()
            phase["selected"] = observe(selected, effect)
            phase["after_selected"] = qdiscs()
            phase["removed_preferences"] = remove_hierarchy(names["shaper"], device, leaves)
            phase["root_after"] = root_qdisc(names["shaper"], device)
            phase["filters_after"] = json.loads(tc(names["shaper"], "-j", "filter", "show", "dev", device))
        # No helper stays behind, and a new one without NET_ADMIN finds the
        # original root qdisc and no filter.
        docker("rm", "--force", names["shaper"])
        result["shaper_left"] = docker("ps", "--all", "--quiet", "--filter", "name=^" + names["shaper"] + "$")[1].split()
        docker("run", *common, "--name", names["observer"], "--network", shared, "--cap-drop=ALL",
               "--entrypoint=sleep", helper["image_id"], "infinity")
        result["observed_root"] = root_qdisc(names["observer"], device)
        result["observed_filters"] = json.loads(tc(names["observer"], "-j", "filter", "show", "dev", device))
        result["restored"] = observe(selected, "restored")
        problems = fault_problems(result)
        if problems:
            raise CheckError("; ".join(problems), "runtime_failure")
        result["status"] = "passed"
    except (check.CheckError, KeyError, ValueError) as error:
        result.update(status="failed", error=str(error), failure_kind=getattr(error, "kind", "checker_failure"))
    except KeyboardInterrupt:
        result.update(status="failed", error="cancelled", failure_kind="checker_failure", cancelled=True)
        raise
    finally:
        result["cleanup"] = cleanup(label, output)
        if result["cleanup"]["status"] != "passed":
            result["status"] = "failed"
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def fault_problems(result):
    """What a packet-fault scenario's observations do not show. The
    installed state comes from tc, the effects from the receiver's kernel
    counters, and the application outcome from the transfers' bytes and
    digests; each is checked on its own. The noise of a counter is the most
    that any transfer without a fault raised it."""
    problems = []
    size = result["size"]
    specs = dict(result["selected"], **result["controls"])
    baseline = list(result["baseline"].values())
    noise = {name: max(observation["counters"][name] for observation in baseline) for name in EFFECT_COUNTERS}
    result["noise"] = noise

    def transfers_of(phase, observations):
        for name, observation in observations.items():
            problems.extend(transfer_problems(phase, name, observation["transfer"], specs[name], size))

    transfers_of("baseline", result["baseline"])
    transfers_of("restored", result["restored"])
    for effect, phase in result["effects"].items():
        spec = FAULT_EFFECTS[effect]
        transfers_of(effect + " control", phase["controls"])
        transfers_of(effect, phase["selected"])
        problems.extend("%s applied: %s" % (effect, problem) for problem in fault_mismatches(spec, phase["applied"]))
        if phase["after_controls"]["counters"]["packets"] != 0:
            problems.append("%s: the child counted %r packets of the unselected peer"
                            % (effect, phase["after_controls"]["counters"]["packets"]))
        if not phase["after_selected"]["counters"]["packets"]:
            problems.append("%s: the child counted no selected packets" % effect)
        for counter in spec["counters"]:
            floor = max(EFFECT_FLOOR, noise[counter] + EFFECT_MARGIN)
            for name, observation in phase["selected"].items():
                value = observation["counters"][counter]
                if value < floor:
                    problems.append("%s %s: %s rose by %d, expected at least %d" % (effect, name, counter, value, floor))
            for name, observation in phase["controls"].items():
                value = observation["counters"][counter]
                if value > noise[counter] + CONTROL_ALLOWANCE:
                    problems.append("%s control %s: %s rose by %d, noise %d" % (effect, name, counter, value,
                                                                              noise[counter]))
        root = phase["root_after"]
        if (root.get("kind"), root.get("handle")) != (result["root_before"].get("kind"), result["root_before"].get("handle")):
            problems.append("%s: root qdisc %s %s after removal" % (effect, root.get("kind"), root.get("handle")))
        if phase["filters_after"]:
            problems.append("%s: filters remain: %s" % (effect, json.dumps(phase["filters_after"])))
    for name, observation in result["restored"].items():
        for counter in EFFECT_COUNTERS:
            if observation["counters"][counter] > noise[counter] + CONTROL_ALLOWANCE:
                problems.append("restored %s: %s rose by %d, noise %d" % (name, counter, observation["counters"][counter],
                                                                          noise[counter]))
    before, after = result["root_before"], result["observed_root"]
    if (after.get("kind"), after.get("handle")) != (before.get("kind"), before.get("handle")):
        problems.append("observed_root: root qdisc %s %s, %s %s before" % (after.get("kind"), after.get("handle"),
                                                                           before.get("kind"), before.get("handle")))
    if result["observed_filters"]:
        problems.append("filters remain: " + json.dumps(result["observed_filters"]))
    if result["shaper_left"]:
        problems.append("the shaping helper remains: " + ", ".join(result["shaper_left"]))
    return problems


def run_cleanup(errors, *command):
    """One cleanup command; its failure is recorded, not raised."""
    try:
        code, text = docker(*command, check_exit=False)
    except check.CheckError as error:
        errors.append(str(error))
        return None
    if code:
        errors.append(text.strip() or "docker " + " ".join(command) + " failed")
        return None
    return text


def remove_container(container):
    errors = []
    run_cleanup(errors, "rm", "--force", container)
    remaining = run_cleanup(errors, "ps", "--all", "--quiet", "--filter", "name=^" + container + "$")
    passed = remaining is not None and not remaining.split()
    return {"status": "passed" if passed else "failed", "container": container, "errors": errors}


def cleanup(label, output):
    """Remove every container and network of one scenario; report leftovers.
    Cleanup passes only when the final inventories succeed and are empty."""
    errors = []
    containers = run_cleanup(errors, "ps", "--all", "--quiet", "--filter", "label=" + label)
    if containers and containers.split():
        run_cleanup(errors, "rm", "--force", *containers.split())
    networks = run_cleanup(errors, "network", "ls", "--quiet", "--filter", "label=" + label)
    if networks and networks.split():
        run_cleanup(errors, "network", "rm", *networks.split())
    containers = run_cleanup(errors, "ps", "--all", "--quiet", "--filter", "label=" + label)
    networks = run_cleanup(errors, "network", "ls", "--quiet", "--filter", "label=" + label)
    leftovers = (containers or "").split() + (networks or "").split()
    passed = containers is not None and networks is not None and not leftovers
    return {"status": "passed" if passed else "failed", "leftovers": leftovers, "errors": errors}


def environment():
    info = json.loads(docker("info", "--format", "{{json .}}")[1])
    return {key: info.get(key) for key in ("ServerVersion", "OperatingSystem", "OSType", "Architecture",
                                            "KernelVersion")}


def checker_sha256():
    digest = hashlib.sha256(Path(__file__).read_bytes())
    for extension in sorted(EXTENSIONS):
        digest.update(EXTENSIONS[extension]["probe"].read_bytes())
    return digest.hexdigest()


def save(output, report):
    (output / "extension-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def rate_profile(args, image, report, output):
    """The optional bandwidth profile, after the baseline contract passed:
    the parser's outcome, then the kernel and each target's scenario. Its
    failure fails the check with its own kind, and records the stage."""
    rate = report["rate"]
    rate.update(rate_bit=args.rate_kbit * 1000, queue_limit=DEFAULT_QUEUE_LIMIT)
    stage = "parser"
    if rate["parser"]["status"] == "passed" and args.functional:
        print("Checking netem's rate and queue limit on this engine's kernel", flush=True)
        rate["kernel"] = rate_kernel(image, output)
        save(output, report)
        stage = "kernel"
        if rate["kernel"]["status"] == "passed":
            stage = "targets"
            rate["targets"] = {}
            for reference in args.target:
                target = check.inspect_image(reference, args.platform)
                print("Limiting the selected peers of a container of %s to %d kbit/s" % (reference, args.rate_kbit),
                      flush=True)
                run = uuid.uuid4().hex[:12]
                rate["targets"][reference] = rate_scenario(image, target, run, rate["rate_bit"], output / ("rate-" + run))
                save(output, report)
    if stage == "targets":
        failed = [reference for reference, result in rate["targets"].items() if result["status"] != "passed"]
        if failed:
            rate.update(status="failed", stage=stage, failure_kind="runtime_failure",
                        error="The rate scenario failed for: " + ", ".join(failed))
        else:
            rate["status"] = "passed"
    elif rate[stage]["status"] != "passed":
        rate.update(status="failed", stage=stage, failure_kind=rate[stage]["failure_kind"], error=rate[stage]["error"])
    else:
        rate["status"] = "passed"
    rate["scope"] = "functional" if args.functional else "parser"
    save(output, report)
    if rate["status"] != "passed":
        return CheckError("The rate profile failed at the %s stage: %s" % (rate["stage"], rate["error"]),
                          rate["failure_kind"])
    return None


def faults_profile(args, image, report, output):
    """The optional packet-fault profile, after the baseline contract
    passed: the parser's outcome, then the kernel and each target's
    scenario. Returns its failure, with its own kind and the stage."""
    faults = report["packet_faults"]
    faults.update(effects={name: {key: spec[key] for key in ("netem", "faults", "gap", "delay_ms", "rate_bit",
                                                              "counters")}
                           for name, spec in FAULT_EFFECTS.items()},
                  unsupported=dict(FAULT_UNSUPPORTED))
    stage = "parser"
    if faults["parser"]["status"] == "passed" and args.functional:
        print("Checking netem's packet faults on this engine's kernel", flush=True)
        faults["kernel"] = faults_kernel(image, output)
        save(output, report)
        stage = "kernel"
        if faults["kernel"]["status"] == "passed":
            stage = "targets"
            faults["targets"] = {}
            for reference in args.target:
                target = check.inspect_image(reference, args.platform)
                print("Reordering, duplicating and corrupting the selected peers of a container of " + reference,
                      flush=True)
                run = uuid.uuid4().hex[:12]
                faults["targets"][reference] = fault_scenario(image, target, run, output / ("faults-" + run))
                save(output, report)
    if stage == "targets":
        failed = [reference for reference, result in faults["targets"].items() if result["status"] != "passed"]
        if failed:
            kinds = {faults["targets"][reference].get("failure_kind", "runtime_failure") for reference in failed}
            faults.update(status="failed", stage=stage,
                          failure_kind=kinds.pop() if len(kinds) == 1 else "runtime_failure",
                          error="The packet-fault scenario failed for: " + ", ".join(failed))
        else:
            faults["status"] = "passed"
    elif faults[stage]["status"] != "passed":
        faults.update(status="failed", stage=stage, failure_kind=faults[stage]["failure_kind"],
                      error=faults[stage]["error"])
    else:
        faults["status"] = "passed"
    faults["scope"] = "functional" if args.functional else "parser"
    save(output, report)
    if faults["status"] != "passed":
        return CheckError("The packet-fault profile failed at the %s stage: %s" % (faults["stage"], faults["error"]),
                          faults["failure_kind"])
    return None


def main(argv=None):
    args = arguments(argv)
    output = (args.output_dir or Path("artifacts") / ("extension-check-" + uuid.uuid4().hex)).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise CheckError("Output directory must be empty: " + str(output))
    report = {"schema": 1, "extension": args.extension, "contract": EXTENSIONS[args.extension]["contract"],
              "level": "functional" if args.functional else "quick", "status": "running",
              "started_at": datetime.now(timezone.utc).isoformat(), "checker_sha256": checker_sha256(),
              "delay_ms": args.delay_ms, "targets": {}}
    if args.rate:
        report["rate"] = {"status": "pending"}
    if args.packet_faults:
        report["packet_faults"] = {"status": "pending"}
    save(output, report)
    try:
        image = check.inspect_image(args.image, args.platform)
        metadata = json.loads(docker("image", "inspect", image["image_id"])[1])[0]
        labels = (metadata.get("Config") or {}).get("Labels") or {}
        image["labels"] = {key: value for key, value in labels.items() if key.startswith("io.ceph-testcontainers.")}
        report["image"] = image
        print("Checking the " + args.extension + " contract: " + args.image, flush=True)
        probe(args.extension, image, args, output)
        save(output, report)
        if image["status"] != "passed":
            raise CheckError("The image does not meet the " + args.extension + " contract; see quick.log",
                             "image_unsupported")
        if image["cleanup"]["status"] != "passed":
            raise CheckError("Could not remove the quick probe container: " + "; ".join(image["cleanup"]["errors"]),
                             "checker_failure")
        if args.rate:
            # The image's parser alone decides this; the kernel comes later,
            # and only after the parser passed.
            print("Checking that tc parses netem's rate and queue limit", flush=True)
            report["rate"] = {"status": "running", "parser": rate_parser(image, output)}
            save(output, report)
        if args.packet_faults:
            print("Checking that tc parses netem's reorder, duplicate and corrupt with correlations", flush=True)
            report["packet_faults"] = {"status": "running", "parser": faults_parser(image, output)}
            save(output, report)
        if args.functional:
            report["environment"] = environment()
            print("Checking netem on this engine's kernel", flush=True)
            report["kernel"] = kernel(image, output)
            save(output, report)
            if report["kernel"]["status"] != "passed":
                raise CheckError("netem does not work on this engine: " + report["kernel"]["error"],
                                 report["kernel"]["failure_kind"])
            for reference in args.target:
                target = check.inspect_image(reference, args.platform)
                if target["platform"] != image["platform"]:
                    raise CheckError("The target and the helper must use one platform: " + reference)
                print("Delaying a container of " + reference, flush=True)
                run = uuid.uuid4().hex[:12]
                report["targets"][reference] = delay_scenario(image, target, run, args.delay_ms,
                                                              output / ("target-" + run))
                save(output, report)
            failed = [reference for reference, result in report["targets"].items() if result["status"] != "passed"]
            if failed:
                raise CheckError("The delay scenario failed for: " + ", ".join(failed), "runtime_failure")
            for profile in args.selector:
                report.setdefault("selectors", {})[profile] = {}
                for reference in args.target:
                    target = check.inspect_image(reference, args.platform)
                    print("Checking the " + profile + " selector in a container of " + reference, flush=True)
                    run = uuid.uuid4().hex[:12]
                    report["selectors"][profile][reference] = selector_scenario(
                        image, target, run, profile, args.delay_ms, output / ("selector-" + run))
                    save(output, report)
            failed = [profile + " " + reference for profile, results in report.get("selectors", {}).items()
                      for reference, result in results.items() if result["status"] != "passed"]
            if failed:
                raise CheckError("The selector scenario failed for: " + ", ".join(failed), "runtime_failure")
        # Each optional profile runs and is reported on its own; the first
        # failure fails the check.
        failures = [profile(args, image, report, output) for selected, profile in
                    ((args.rate, rate_profile), (args.packet_faults, faults_profile)) if selected]
        failures = [failure for failure in failures if failure]
        if failures:
            raise failures[0]
        report["status"] = "passed"
    except (check.CheckError, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        report.update(status="failed", error=str(error) or type(error).__name__,
                      failure_kind=getattr(error, "kind", "checker_failure"))
        print(report["error"], file=sys.stderr)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(output, report)
        print("Extension check: " + report["status"], flush=True)
        print("Report: " + str(output / "extension-report.json"), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (check.CheckError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
