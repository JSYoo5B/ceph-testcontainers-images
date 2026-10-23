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

For the packet-capture helper, --functional runs a capture scenario with each
--target: a helper in the target container's namespace on a private bridge,
as the image's own user with NET_RAW alone, captures TCP between a peer and
that namespace with an explicit interface, filter and snaplen. It checks the
readiness line, the selected flow in both directions without an excluded
flow, a stop by SIGTERM with the file flushed and the statistics, a valid
empty capture, the packet bound, and that an invalid filter, a missing
interface and a missing NET_RAW are refused, each with its own kind.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid

import check

HERE = Path(__file__).resolve().parent
# Each extension's quick probe, the user that runs it and the capabilities it
# gets in a container without a network: the netem helper runs as root with
# none; the capture helper as the unprivileged user of its image with
# NET_RAW, which its tcpdump needs to start at all.
EXTENSIONS = {"netem": {"contract": "1", "probe": HERE / "extensions" / "netem" / "check-netem.sh", "user": "0:0",
                        "caps": (), "published": True},
              "capture": {"contract": "1", "probe": HERE / "extensions" / "capture" / "check-capture.sh",
                          "user": None, "caps": ("NET_RAW",), "published": True}}
# The helpers that image/publish.py publishes, each gated by its own
# functional check on both architectures.
PUBLISHED = tuple(name for name, spec in EXTENSIONS.items() if spec["published"])
# The checks that exist only for the netem helper.
NETEM_ONLY = ("selector", "rate", "packet_faults")
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
    if args.extension != "netem" and any(getattr(args, option) for option in NETEM_ONLY):
        parser.error("--extension " + args.extension + " has no --selector, --rate or --packet-faults; "
                     "they belong to netem")
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
        # The contract asks for sh on PATH, not for /bin/sh. Without a user,
        # the image's own user runs the probe.
        user = EXTENSIONS[extension]["user"]
        caps = ["--cap-add=" + cap for cap in EXTENSIONS[extension]["caps"]]
        docker("create", "--name", container, "--label", LABEL + "=quick", "--pull=never", "--network=none",
               "--cap-drop=ALL", *caps, *(["--user=" + user] if user else []), "--interactive", "--platform",
               image["platform"], "--entrypoint=sh", image["image_id"], "-s")
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


# What runc prints, as the container's own output, when the kernel refuses to
# execute a file whose effective file capability the bounding set lacks.
EXEC_REFUSED = re.compile(r"exec (/[^\s:]*/)?tcpdump: operation not permitted")


def exec_refusal(container, timeout=120):
    """Start a created container whose entrypoint is tcpdump, without NET_RAW.
    Only the runtime's refusal to execute tcpdump counts as refused: the
    container must have started (no Docker or runtime setup error) and exited
    with runc's status 255 and its exec message alone, not with an error of
    tcpdump itself. ran is false when Docker or the runtime never ran the
    container's process, which says nothing about the image."""
    code, text = docker("start", "--attach", container, timeout=timeout, check_exit=False)
    state = json.loads(docker("container", "inspect", "--format", "{{json .State}}", container,
                              timeout=timeout)[1])
    state = {key: state.get(key) for key in ("ExitCode", "Error", "StartedAt")}
    started = bool(state["StartedAt"]) and not state["StartedAt"].startswith("0001-")
    ran = started and not state["Error"] and code == state["ExitCode"]
    refused = ran and state["ExitCode"] == 255 and EXEC_REFUSED.fullmatch(text.strip()) is not None
    return {"exit_code": code, "output": text, "state": state, "ran": ran, "refused": refused}


def capture_refusal(image, output):
    """Without NET_RAW, the capture helper's tcpdump must not start at all:
    the kernel refuses to execute a file capability that the bounding set
    lacks. Only the refusal described by exec_refusal() passes."""
    container = "ceph-extension-check-" + uuid.uuid4().hex
    result = {"name": "refused-without-net_raw"}
    try:
        docker("create", "--name", container, "--label", LABEL + "=quick", "--pull=never", "--network=none",
               "--cap-drop=ALL", "--platform", image["platform"], "--entrypoint=tcpdump", image["image_id"],
               "--version")
        refusal = exec_refusal(container)
        (output / "refusal.log").write_text(refusal["output"])
        result.update(exit_code=refusal["exit_code"], state=refusal["state"])
        if not refusal["ran"]:
            raise CheckError("The refusal container did not run: docker start exited %s, state %s: %s"
                             % (refusal["exit_code"], json.dumps(refusal["state"]), refusal["output"].strip()[:500]))
        result["status"] = "passed" if refusal["refused"] else "failed"
        if not refusal["refused"]:
            result.update(failure_stage="quick:refused-without-net_raw", failure_kind="quick_failure")
    except check.CheckError as error:
        # A container that Docker could not create, start or inspect says
        # nothing about the image.
        result.update(status="failed", error=str(error), failure_stage="quick:refused-without-net_raw",
                      failure_kind="checker_failure")
    finally:
        result["cleanup"] = remove_container(container)
        if result["cleanup"]["status"] != "passed":
            result.update(status="failed", failure_kind="checker_failure")
    return result


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


# Functional check of the packet-capture helper (docs/EXTENSIONS.md). A
# capturing helper joins a target container's namespace on a private bridge
# as the image's own user with NET_RAW alone; a peer on the bridge exchanges
# TCP with a server in that namespace on the selected and on an excluded port.
CAPTURE_PORT, CAPTURE_EXCLUDED_PORT = 7000, 7001
CAPTURE_SNAPLEN = 128
CAPTURE_PACKETS = 1000
CAPTURE_BOUND = 3
CAPTURE_RESPONSE = 1000
CAPTURE_READY_TIMEOUT = 15
CAPTURE_STOP_TIMEOUT = 15
CAPTURE_MISSING_INTERFACE = "ctc-missing0"
CAPTURE_INVALID_FILTER = "tcp portx %d" % CAPTURE_PORT
PCAP_HEADER = 24
PCAP_RECORD = 16


def capture_request(port):
    return b"capture-request-%d\n" % port


def capture_token(port):
    return b"capture-response-%d-" % port


# Answers each request line on every port given with argv[1] bytes of the
# port's own token; runs in the target's namespace without any capability.
CAPTURE_SERVER = """import socket, sys, threading
size = int(sys.argv[1])
def serve(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(16)
    token = b"capture-response-%d-" % port
    body = (token * (size // len(token) + 1))[:size]
    while True:
        c, _ = s.accept()
        try:
            data = b""
            while not data.endswith(b"\\n"):
                chunk = c.recv(256)
                if not chunk:
                    break
                data += chunk
            if data:
                c.sendall(body)
        except OSError:
            pass
        finally:
            c.close()
for port in sys.argv[2:]:
    threading.Thread(target=serve, args=(int(port),)).start()
"""

# One request and its whole answer per port given, from the peer.
CAPTURE_EXCHANGE = """import json, socket, sys
host, size = sys.argv[1], int(sys.argv[2])
results = []
for port in map(int, sys.argv[3:]):
    token = b"capture-response-%d-" % port
    with socket.create_connection((host, port), timeout=10) as c:
        c.sendall(b"capture-request-%d\\n" % port)
        data = b""
        while True:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
    if data != (token * (size // len(token) + 1))[:size]:
        raise SystemExit("port %d answered %d unexpected bytes" % (port, len(data)))
    results.append({"port": port, "received": len(data)})
print(json.dumps(results))
"""

# The interface of the namespace with the given MAC address, from sysfs.
CAPTURE_INTERFACE = """import json, os, sys
found = []
for name in sorted(os.listdir("/sys/class/net")):
    try:
        with open("/sys/class/net/%s/address" % name) as f:
            address = f.read().strip()
        with open("/sys/class/net/%s/ifindex" % name) as f:
            index = int(f.read())
    except OSError:
        continue
    if address == sys.argv[1].lower():
        found.append({"name": name, "index": index})
print(json.dumps(found))
"""

# The capture runs under sh, which records tcpdump's PID and, once it ends,
# its exit status; $0 is the base path of the capture's files.
CAPTURE_WRAPPER = 'tcpdump "$@" 2>"$0.stderr" & echo $! >"$0.pid"; wait $!; echo $? >"$0.status"'

# The capture's standard error, PID, exit status and output size.
CAPTURE_STATE = """import json, os, sys
base = sys.argv[1]
def read(suffix):
    try:
        with open(base + suffix) as f:
            return f.read()
    except OSError:
        return None
def number(text):
    return int(text) if text and text.endswith("\\n") and text.strip().isdigit() else None
size = os.path.getsize(base + ".pcap") if os.path.exists(base + ".pcap") else None
print(json.dumps({"stderr": read(".stderr") or "", "pid": number(read(".pid")),
                  "status": number(read(".status")), "size": size}))
"""

CAPTURE_SIGNAL = "import os, signal, sys; os.kill(int(sys.argv[1]), getattr(signal, sys.argv[2]))"
# The wrapper's status of a tcpdump ended by SIGKILL: 128 + 9.
CAPTURE_KILLED = 137
CAPTURE_UID = "import json, os; print(json.dumps([os.getuid(), os.geteuid()]))"

READY_LINE = re.compile(r"tcpdump: listening on (\S+), link-type (\S+) .*, snapshot length (\d+) bytes")
STATISTICS = {"captured": "captured", "received_by_filter": "received by filter",
              "dropped_by_kernel": "dropped by kernel", "dropped_by_interface": "dropped by interface"}
# tcpdump prints these three after a capture; the last is unknown when missing.
REQUIRED_STATISTICS = ("captured", "received_by_filter", "dropped_by_kernel")


def capture_ready(stderr, interface, snaplen):
    """Whether tcpdump announced the requested interface, Ethernet and snaplen."""
    for line in stderr.splitlines():
        match = READY_LINE.fullmatch(line.strip())
        if match and match.groups() == (interface, "EN10MB", str(snaplen)):
            return True
    return False


def capture_statistics(stderr):
    """tcpdump's closing statistics; one that it did not print is None (unknown), never zero."""
    statistics = dict.fromkeys(STATISTICS)
    for line in stderr.splitlines():
        for key, label in STATISTICS.items():
            match = re.fullmatch(r"(\d+) packets? " + label, line.strip())
            if match:
                statistics[key] = int(match.group(1))
    return statistics


def classify_capture(code, stderr):
    """The kind of a tcpdump that failed to capture."""
    text = stderr.lower()
    if "can't parse filter expression" in text:
        return "invalid_filter"
    if "no such device" in text or "doesn't exist" in text:
        return "unavailable_interface"
    if "operation not permitted" in text or "permission" in text:
        return "permission_denied"
    if "error while loading shared libraries" in text or "executable file not found" in text or code in (126, 127):
        return "loading_failure"
    return "capture_failure"


def parse_pcap(data, snaplen):
    """The records of a classic pcap file as (included bytes, original length),
    with the problems of its header and records."""
    problems, records = [], []
    if len(data) < PCAP_HEADER:
        return records, ["%d bytes, shorter than a pcap header" % len(data)]
    magic, major, minor, _, _, header_snaplen, link = struct.unpack_from("<IHHiIII", data)
    if magic != 0xa1b2c3d4:
        problems.append("magic %#x is not classic microsecond pcap" % magic)
    if (major, minor) != (2, 4):
        problems.append("version %d.%d" % (major, minor))
    if header_snaplen != snaplen:
        problems.append("snaplen %d, requested %d" % (header_snaplen, snaplen))
    if link != 1:
        problems.append("link type %d, not Ethernet" % link)
    if problems:
        return records, problems
    offset = PCAP_HEADER
    while offset < len(data):
        if offset + PCAP_RECORD > len(data):
            problems.append("a record header is cut at byte %d" % offset)
            break
        _, microseconds, included, original = struct.unpack_from("<IIII", data, offset)
        # A complete record header is checked even when its data is cut, so
        # that a cut tail never hides an invalid header.
        if microseconds >= 1000000 or included > snaplen or included > original:
            problems.append("record %d has %d of %d bytes at %d us" % (len(records), included, original,
                                                                       microseconds))
        frame = data[offset + PCAP_RECORD:offset + PCAP_RECORD + included]
        offset += PCAP_RECORD + included
        if len(frame) != included:
            problems.append("a record is cut at byte %d" % (offset - included))
            break
        records.append((frame, original))
    return records, problems


def tcp_segment(frame):
    """(source, destination, source port, destination port, flags, payload) of
    an Ethernet/IPv4/TCP frame, or None."""
    if len(frame) < 14 + 20 or frame[12:14] != b"\x08\x00" or frame[14] >> 4 != 4:
        return None
    header = (frame[14] & 0x0f) * 4
    if frame[14 + 9] != 6 or len(frame) < 14 + header + 20:
        return None
    tcp = 14 + header
    source, destination = (str(ipaddress.IPv4Address(frame[14 + 12:14 + 16])),
                           str(ipaddress.IPv4Address(frame[14 + 16:14 + 20])))
    sport, dport = struct.unpack_from(">HH", frame, tcp)
    flags = frame[tcp + 13]
    payload = frame[tcp + (frame[tcp + 12] >> 4) * 4:]
    return source, destination, sport, dport, flags, payload


def capture_flow(records, peer, target, snaplen):
    """Summary and problems of a capture that must hold only the TCP flow
    between the peer and the target's selected port, in both directions."""
    summary = {"records": len(records), "to_target": 0, "from_target": 0, "truncated": 0}
    problems = []
    seen = set()
    for index, (frame, original) in enumerate(records):
        segment = tcp_segment(frame)
        if segment is None:
            problems.append("record %d is not IPv4 TCP" % index)
            continue
        source, destination, sport, dport, flags, payload = segment
        if (source, destination, dport) == (peer, target, CAPTURE_PORT):
            summary["to_target"] += 1
            if flags & 0x12 == 0x02:
                seen.add("syn")
            if payload.startswith(capture_request(CAPTURE_PORT)):
                seen.add("request")
        elif (source, destination, sport) == (target, peer, CAPTURE_PORT):
            summary["from_target"] += 1
            if flags & 0x12 == 0x12:
                seen.add("syn_ack")
            if payload.startswith(capture_token(CAPTURE_PORT)):
                seen.add("response")
        else:
            problems.append("record %d is outside the filter: %s:%d > %s:%d" % (index, source, sport,
                                                                                 destination, dport))
        if len(frame) == snaplen < original:
            summary["truncated"] += 1
    summary["seen"] = sorted(seen)
    return summary, problems


def capture_problems(kind, state, data, peer, target, snaplen, packets):
    """Summary and problems of one finished capture: kind is "traffic" (the
    selected flow both ways and nothing else), "empty" (a valid file without
    records) or "bound" (exactly `packets` records of the selected flow)."""
    problems = []
    if state["status"] != 0:
        problems.append("tcpdump exited with %s" % state["status"])
    statistics = capture_statistics(state["stderr"])
    missing = [key for key in REQUIRED_STATISTICS if statistics[key] is None]
    if missing:
        problems.append("no statistics for " + ", ".join(missing))
    records, pcap_problems = parse_pcap(data, snaplen)
    problems += pcap_problems
    limit = PCAP_HEADER + packets * (PCAP_RECORD + snaplen)
    if len(data) > limit:
        problems.append("%d bytes, over the bound of %d" % (len(data), limit))
    summary, flow_problems = capture_flow(records, peer, target, snaplen)
    problems += flow_problems
    summary.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), statistics=statistics,
                   byte_bound=limit)
    if statistics["captured"] is not None and statistics["captured"] != len(records):
        problems.append("%d packets captured, %d records" % (statistics["captured"], len(records)))
    if kind == "traffic":
        absent = {"syn", "syn_ack", "request", "response"} - set(summary["seen"])
        if absent:
            problems.append("the selected flow lacks " + ", ".join(sorted(absent)))
        if not summary["truncated"]:
            problems.append("no record was cut at the snaplen")
    elif kind == "empty":
        if records or len(data) != PCAP_HEADER:
            problems.append("an empty capture holds %d records in %d bytes" % (len(records), len(data)))
    elif kind == "bound":
        if len(records) != packets:
            problems.append("%d records, bound %d" % (len(records), packets))
    return summary, problems


def cut_tail(problems):
    """Split a cut final record off the problems of parse_pcap(): an abrupt
    end may leave one, which is partial output, not a malformed file."""
    cut = [problem for problem in problems if problem.startswith(("a record header is cut", "a record is cut"))]
    return [problem for problem in problems if problem not in cut], bool(cut)


def partial_problems(kind, state, data, peer, target, snaplen, packets, loss=None):
    """Summary and problems of a capture that did not end through SIGTERM:
    "abrupt" (tcpdump killed with SIGKILL) or "target_loss" (the target
    container killed while tcpdump ran). Its evidence is partial: a valid
    header, complete records of the selected flow only, at most one cut final
    record, and statistics that stay unknown when tcpdump printed none."""
    problems = []
    statistics = capture_statistics(state["stderr"])
    records, pcap_problems = parse_pcap(data, snaplen)
    pcap_problems, cut = cut_tail(pcap_problems)
    problems += pcap_problems
    limit = PCAP_HEADER + packets * (PCAP_RECORD + snaplen)
    if len(data) > limit:
        problems.append("%d bytes, over the bound of %d" % (len(data), limit))
    summary, flow_problems = capture_flow(records, peer, target, snaplen)
    problems += flow_problems
    summary.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), statistics=statistics,
                   byte_bound=limit, cut_tail=cut, complete="partial")
    if not records:
        problems.append("no complete record of the traffic before the %s" % ("kill" if kind == "abrupt" else "loss"))
    if kind == "abrupt":
        if state["status"] != CAPTURE_KILLED:
            problems.append("tcpdump ended with %s, not %d after SIGKILL" % (state["status"], CAPTURE_KILLED))
        printed = [key for key, value in statistics.items() if value is not None]
        if printed:
            problems.append("a killed tcpdump printed statistics: " + ", ".join(printed))
    else:
        summary["loss"] = loss
        if loss["interfaces"]:
            problems.append("the target's interface is still in the namespace after its container was killed")
        if loss["end"] == "exited" and state["status"] == 0:
            problems.append("tcpdump ended with status 0 after it lost its interface")
    return summary, problems


def budget(deadline, what):
    """The seconds left before deadline; none left fails the wait."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise CheckError(what, "capture_failure")
    return left


def capture_state(capturer, base, deadline, what):
    return json.loads(docker("exec", capturer, "python3", "-c", CAPTURE_STATE, base,
                             timeout=budget(deadline, what))[1])


def start_capture(capturer, base, interface, expression, packets):
    """Start tcpdump in the background and wait for its readiness line and
    output file; a tcpdump that exits first fails with its own kind. Every
    Docker call is bounded by the time left, and readiness counts only when
    it was observed before the deadline."""
    deadline = time.monotonic() + CAPTURE_READY_TIMEOUT
    late = "tcpdump was not ready within %ds" % CAPTURE_READY_TIMEOUT
    docker("exec", "--detach", capturer, "sh", "-c", CAPTURE_WRAPPER, base, "-p", "-i", interface, "-s",
           str(CAPTURE_SNAPLEN), "-c", str(packets), "--immediate-mode", "-U", "-w", base + ".pcap", expression,
           timeout=budget(deadline, late))
    while True:
        state = capture_state(capturer, base, deadline, late)
        observed = time.monotonic()
        if state["status"] is not None:
            raise CheckError("tcpdump exited with %d before it was ready: %s" % (state["status"],
                                                                                 state["stderr"].strip()[:500]),
                             classify_capture(state["status"], state["stderr"]))
        if observed > deadline:
            raise CheckError(late + ": " + state["stderr"].strip()[:500], "capture_failure")
        if capture_ready(state["stderr"], interface, CAPTURE_SNAPLEN) and state["size"] is not None and state["pid"]:
            return state
        time.sleep(min(0.2, max(deadline - time.monotonic(), 0)))


def finish_capture(capturer, base, stop):
    """Send SIGTERM when stop is set, then wait for tcpdump's exit status.
    The first read, the signal and every later read are bounded by the time
    left, and an exit counts only when it was observed before the deadline."""
    deadline = time.monotonic() + CAPTURE_STOP_TIMEOUT
    late = "tcpdump did not exit within %ds%s" % (CAPTURE_STOP_TIMEOUT, " of SIGTERM" if stop else "")
    state = capture_state(capturer, base, deadline, late)
    if stop:
        docker("exec", capturer, "python3", "-c", CAPTURE_SIGNAL, str(state["pid"]), "SIGTERM",
               timeout=budget(deadline, late))
    while True:
        if time.monotonic() > deadline:
            raise CheckError(late, "capture_failure")
        if state["status"] is not None:
            return state
        time.sleep(min(0.2, max(deadline - time.monotonic(), 0)))
        state = capture_state(capturer, base, deadline, late)


def wait_status(capturer, base, timeout):
    """The capture's state once tcpdump has an exit status, or its last
    state, still running, when timeout has passed."""
    deadline = time.monotonic() + timeout
    late = "the capture's state was not read within %ds" % timeout
    state = capture_state(capturer, base, deadline, late)
    while state["status"] is None:
        # Expiry is tested before another poll, which could not start once
        # no time is left; a failed or blocked read still raises.
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(0.2, left))
        if time.monotonic() >= deadline:
            break
        state = capture_state(capturer, base, deadline, late)
    return state


def retrieve(capturer, base, directory):
    """The finished capture file. It stays out of the report directory: only
    its size, hash and decoded summary are reported."""
    local = Path(directory) / (Path(base).name + ".pcap")
    docker("cp", capturer + ":" + base + ".pcap", str(local))
    return local.read_bytes()


def exchange(peer, target_address, ports, output):
    code, text = docker("exec", peer, "python3", "-c", CAPTURE_EXCHANGE, target_address, str(CAPTURE_RESPONSE),
                        *map(str, ports), check_exit=False)
    with (output / "exchanges.log").open("a") as log:
        log.write("%s: %s" % (",".join(map(str, ports)), text))
    if code:
        raise CheckError("The peer's exchange failed: " + text.strip()[:500], "checker_failure")
    return json.loads(text.strip().splitlines()[-1])


CAPTURE_STOPS = {"traffic": "SIGTERM", "empty": "SIGTERM", "bound": "packet bound", "abrupt": "SIGKILL",
                 "target_loss": "target container killed"}
# The negative controls of refused_capture(), each with the kind it must fail with.
CAPTURE_REFUSALS = {"invalid_filter": "invalid_filter", "unavailable_interface": "unavailable_interface",
                    "without_net_raw": "permission_denied"}


def run_capture(names, kind, interface, peer, target, ports, packets, directory, output, mac=None):
    """One capture in the target's namespace, from readiness to the decoded file."""
    base = "/tmp/capture-" + kind
    expression = "tcp port %d and host %s" % (CAPTURE_PORT, peer)
    result = {"interface": interface, "filter": expression, "snaplen": CAPTURE_SNAPLEN, "packets": packets,
              "stop": CAPTURE_STOPS[kind]}
    capturer = names["capturer"]
    ready = start_capture(capturer, base, interface, expression, packets)
    result["ready"] = [line for line in ready["stderr"].splitlines() if READY_LINE.fullmatch(line.strip())]
    result["exchanges"] = exchange(names["peer"], target, ports, output)
    time.sleep(0.5)
    loss = None
    if kind == "abrupt":
        docker("exec", capturer, "python3", "-c", CAPTURE_SIGNAL, str(ready["pid"]), "SIGKILL",
               timeout=CAPTURE_STOP_TIMEOUT)
        state = finish_capture(capturer, base, stop=False)
    elif kind == "target_loss":
        # The target's own process ends, which takes its endpoint out of the
        # namespace that the helper joined; tcpdump keeps its file open.
        docker("kill", names["target"], timeout=CAPTURE_STOP_TIMEOUT)
        state = wait_status(capturer, base, CAPTURE_STOP_TIMEOUT)
        loss = {"end": "exited" if state["status"] is not None else "kept running"}
        loss["interfaces"] = json.loads(docker("exec", capturer, "python3", "-c", CAPTURE_INTERFACE, mac,
                                               timeout=CAPTURE_STOP_TIMEOUT)[1])
        if state["status"] is None:
            # A tcpdump that kept running is stopped as usual; its file then
            # still ends at the loss.
            state = finish_capture(capturer, base, stop=True)
    else:
        state = finish_capture(capturer, base, stop=kind != "bound")
    result.update(exit_code=state["status"], stderr=[line.strip() for line in state["stderr"].splitlines()])
    data = retrieve(capturer, base, directory)
    if kind in ("abrupt", "target_loss"):
        summary, problems = partial_problems(kind, state, data, peer, target, CAPTURE_SNAPLEN, packets, loss)
    else:
        summary, problems = capture_problems(kind, state, data, peer, target, CAPTURE_SNAPLEN, packets)
    result.update(summary=summary, problems=problems, status="failed" if problems else "passed")
    return result


def refused_capture(names, label, interface, helper):
    """Negative controls in the target's namespace: an invalid filter, an
    interface that does not exist, and a helper without NET_RAW. Each must
    fail before readiness with its own kind."""
    common = ["-p", "-s", str(CAPTURE_SNAPLEN), "-c", "1", "-w", "/tmp/refused.pcap"]
    expression = "tcp port %d" % CAPTURE_PORT
    controls = {}
    for name, arguments, expected in (
            ("invalid_filter", ["-i", interface, *common, CAPTURE_INVALID_FILTER], "invalid_filter"),
            ("unavailable_interface", ["-i", CAPTURE_MISSING_INTERFACE, *common, expression],
             "unavailable_interface")):
        code, text = docker("exec", names["capturer"], "tcpdump", *arguments, timeout=30, check_exit=False)
        controls[name] = {"expected": expected, "exit_code": code, "output": text.strip()[:500],
                          "kind": classify_capture(code, text) if code else None}
    # The capability is missing from the bounding set: the runtime refuses to
    # execute tcpdump, as in the quick check, but inside the target's namespace.
    docker("create", "--name", names["unprivileged"], "--label", label, "--pull=never",
           "--network", "container:" + names["target"], "--cap-drop=ALL", "--platform", helper["platform"],
           "--entrypoint=tcpdump", helper["image_id"], "-i", interface, *common, expression)
    refusal = exec_refusal(names["unprivileged"], timeout=30)
    if not refusal["ran"]:
        raise CheckError("The control without NET_RAW did not run: docker start exited %s, state %s: %s"
                         % (refusal["exit_code"], json.dumps(refusal["state"]), refusal["output"].strip()[:500]))
    # Only the runtime's exec refusal passes; a tcpdump that started and
    # reported a permission error of its own did run without NET_RAW.
    controls["without_net_raw"] = {"expected": "permission_denied", "exit_code": refusal["exit_code"],
                                   "output": refusal["output"].strip()[:500], "state": refusal["state"],
                                   "kind": "permission_denied" if refusal["refused"] else "not_refused"}
    for control in controls.values():
        control["status"] = "passed" if control["kind"] == control["expected"] and \
            "listening on" not in control["output"] else "failed"
    return controls


def capture_scenario(helper, target, run, output):
    """Capture between a peer and a container of the target image on a
    private bridge, from a helper in the target's namespace; then the
    negative controls; then remove every owned container and the network."""
    label = LABEL + "=" + run
    network = "ceph-extension-check-" + run
    names = {role: network + "-" + role for role in ("target", "server", "capturer", "peer", "unprivileged")}
    result = {"reference": target["reference"], "image_id": target["image_id"], "helper_image_id": helper["image_id"],
              "platform": helper["platform"], "status": "running", "stage": "setup"}
    output.mkdir()
    directory = tempfile.mkdtemp(prefix="capture-")
    common = ["--detach", "--label", label, "--pull=never", "--platform", helper["platform"]]
    try:
        docker("network", "create", "--internal", "--label", label, network)
        docker("run", "--detach", "--label", label, "--pull=never", "--platform", target["platform"],
               "--name", names["target"], "--network", network, "--entrypoint=sleep", target["image_id"], "infinity")
        endpoint = json.loads(docker("container", "inspect", names["target"])[1])[0]["NetworkSettings"]["Networks"][network]
        shared = "container:" + names["target"]
        docker("run", *common, "--name", names["server"], "--network", shared, "--cap-drop=ALL",
               "--entrypoint=python3", helper["image_id"], "-c", CAPTURE_SERVER, str(CAPTURE_RESPONSE),
               str(CAPTURE_PORT), str(CAPTURE_EXCLUDED_PORT))
        docker("run", *common, "--name", names["peer"], "--network", network, "--cap-drop=ALL",
               "--entrypoint=sleep", helper["image_id"], "infinity")
        # The image's own user, with NET_RAW alone and nothing else.
        docker("run", *common, "--name", names["capturer"], "--network", shared, "--cap-drop=ALL",
               "--cap-add=NET_RAW", "--entrypoint=sleep", helper["image_id"], "infinity")
        uid, euid = json.loads(docker("exec", names["capturer"], "python3", "-c", CAPTURE_UID)[1])
        result["capturer"] = {"uid": uid, "euid": euid, "caps": ["NET_RAW"]}
        if 0 in (uid, euid):
            raise CheckError("The capturing helper runs as root", "image_unsupported")
        namespaces = {role: docker("exec", names[role], "python3", "-c", NAMESPACE)[1].strip()
                      for role in ("server", "capturer", "peer")}
        if namespaces["server"] != namespaces["capturer"] or namespaces["peer"] == namespaces["capturer"]:
            raise CheckError("The helpers are not in the expected namespaces: " + json.dumps(namespaces))
        found = json.loads(docker("exec", names["capturer"], "python3", "-c", CAPTURE_INTERFACE,
                                  endpoint["MacAddress"])[1])
        if len(found) != 1:
            raise CheckError("Expected one interface with %s, found %s" % (endpoint["MacAddress"], json.dumps(found)))
        interface = found[0]["name"]
        target_address = endpoint["IPAddress"]
        peer_address = json.loads(docker("container", "inspect", names["peer"])[1])[0][
            "NetworkSettings"]["Networks"][network]["IPAddress"]
        result.update(interface=dict(found[0], address=target_address), peer_address=peer_address)
        for port in (CAPTURE_PORT, CAPTURE_EXCLUDED_PORT):
            code, text = docker("exec", names["peer"], "python3", "-c", READY, target_address, str(port),
                                check_exit=False)
            if code:
                raise CheckError("The server in the target's namespace did not answer: " + text.strip())
        expression = "tcp port %d and host %s" % (CAPTURE_PORT, peer_address)
        code, text = docker("exec", names["capturer"], "tcpdump", "-y", "EN10MB", "-d", expression, check_exit=False)
        if code:
            raise CheckError("The filter did not compile: " + text.strip()[:500], classify_capture(code, text))
        result["captures"] = {}
        for kind, ports, packets in (("traffic", (CAPTURE_PORT, CAPTURE_EXCLUDED_PORT), CAPTURE_PACKETS),
                                     ("empty", (CAPTURE_EXCLUDED_PORT,), CAPTURE_PACKETS),
                                     ("bound", (CAPTURE_PORT,), CAPTURE_BOUND),
                                     ("abrupt", (CAPTURE_PORT,), CAPTURE_PACKETS)):
            result["stage"] = "capture:" + kind
            result["captures"][kind] = run_capture(names, kind, interface, peer_address, target_address, ports,
                                                   packets, directory, output)
            if result["captures"][kind]["status"] != "passed":
                raise CheckError("The %s capture failed: %s" % (kind, "; ".join(result["captures"][kind]["problems"])),
                                 "capture_failure")
        result["stage"] = "refusals"
        result["refusals"] = refused_capture(names, label, interface, helper)
        failed = [name for name, control in result["refusals"].items() if control["status"] != "passed"]
        if failed:
            raise CheckError("Negative controls did not fail as expected: " + ", ".join(failed), "capture_failure")
        # Last, because it ends the target: the capture loses its interface
        # while tcpdump runs.
        result["stage"] = "capture:target_loss"
        result["captures"]["target_loss"] = run_capture(names, "target_loss", interface, peer_address, target_address,
                                                        (CAPTURE_PORT,), CAPTURE_PACKETS, directory, output,
                                                        endpoint["MacAddress"])
        if result["captures"]["target_loss"]["status"] != "passed":
            raise CheckError("The target_loss capture failed: "
                             + "; ".join(result["captures"]["target_loss"]["problems"]), "capture_failure")
        del result["stage"]
        result["status"] = "passed"
    except (check.CheckError, KeyError, ValueError, TypeError) as error:
        result.update(status="failed", error=str(error), failure_stage=result["stage"],
                      failure_kind=getattr(error, "kind", "checker_failure"))
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        result["cleanup"] = cleanup(label, output)
        if result["cleanup"]["status"] != "passed":
            result["status"] = "failed"
            result.setdefault("failure_kind", "checker_failure")
            result.setdefault("failure_stage", "cleanup")
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


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
        if args.extension == "capture" and image["status"] == "passed":
            refusal = capture_refusal(image, output)
            image["checks"].append(refusal)
            save(output, report)
            if refusal.get("failure_kind") == "checker_failure":
                raise CheckError("Could not run the refusal check: " + refusal.get("error", "cleanup failed"),
                                 "checker_failure")
            if refusal["status"] != "passed":
                image["status"] = "failed"
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
        if args.functional and args.extension == "capture":
            report["environment"] = environment()
            for reference in args.target:
                target = check.inspect_image(reference, args.platform)
                if target["platform"] != image["platform"]:
                    raise CheckError("The target and the helper must use one platform: " + reference)
                print("Capturing in the namespace of a container of " + reference, flush=True)
                run = uuid.uuid4().hex[:12]
                report["targets"][reference] = capture_scenario(image, target, run, output / ("capture-" + run))
                save(output, report)
            failed = [reference for reference, result in report["targets"].items() if result["status"] != "passed"]
            if failed:
                kinds = {report["targets"][reference].get("failure_kind", "capture_failure") for reference in failed}
                raise CheckError("The capture scenario failed for: " + ", ".join(failed),
                                 kinds.pop() if len(kinds) == 1 else "capture_failure")
        elif args.functional:
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
