#!/usr/bin/env python3
"""Check an opt-in extension image against its contract (docs/EXTENSIONS.md).

The quick check (default) runs the extension's probe in the image without any
capability or network. The functional check (--functional) also checks this
Docker engine: the kernel feature with NET_ADMIN in a disposable namespace,
then a delay that the helper applies, changes and removes inside the network
namespace of a container from each --target image. Each --selector adds a
candidate selector profile, outside the contract: a delay and a loss that only
the selected peer, family and port of a target's egress get.
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

# Candidate selector profiles (docs/EXTENSIONS.md): not part of any contract.
SELECTORS = {"ipv4-tcp-u32": {"family": 4, "protocol": "ip", "pref": 10, "handle": "800::1"},
             "ipv6-tcp-u32": {"family": 6, "protocol": "ipv6", "pref": 10, "handle": "800::1"}}
ROOT, CHILD, BAND = "7e00:", "7e10:", "7e00:3"
SELECTED_PORT, OTHER_PORT, REVERSE_PORT = 7000, 7001, 6999
SELECTOR_SAMPLES, CONTROL_SAMPLES = 5, 3
LOSS_TIMEOUT = 0.8
DRAIN_BUDGET = 2.0
DRAIN_POLL = 0.05

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

# Helper-side reads that need only the contract's python3.
NAMESPACE = 'import os; print(os.readlink("/proc/self/ns/net"))'
KERNEL = "import os; print(os.uname().release)"

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
                        help="With --functional, also check a candidate selector profile in each target; repeat")
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
    if "Specified qdisc kind is unknown" in output:
        return "kernel_unsupported"
    if "Unknown qdisc" in output or "unparsable" in output:
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


def selector_leaf(raw, profile, peer, port):
    """The owned selector of a filter dump, checked against the requested
    one. The dump of the owned root may hold the leaf and its u32 table and
    header entries; anything else is foreign state."""
    settings = SELECTORS[profile]
    leaves = []
    for entry in json.loads(raw, object_pairs_hook=Pairs):
        if (entry.get("kind"), entry.get("protocol"), entry.get("pref"), entry.get("parent")) != \
                ("u32", settings["protocol"], settings["pref"], ROOT):
            raise CheckError("A filter that the check does not own: " + json.dumps(entry), "runtime_failure")
        options = entry.get("options")
        if options is None or options.get("fh", "").endswith(":"):
            continue
        leaves.append(options)
    if len(leaves) != 1:
        raise CheckError("Expected one selector leaf, found %d" % len(leaves), "runtime_failure")
    options = leaves[0]
    keys = []
    for match in options.every("match"):
        try:
            keys.append({"off": match.get("off"), "value": "%08x" % int(match.get("value"), 16),
                         "mask": "%08x" % int(match.get("mask"), 16), "offmask": match.get("offmask")})
        except (TypeError, ValueError) as error:
            raise CheckError("A u32 key that cannot be read: " + json.dumps(match), "runtime_failure") from error
    leaf = {"handle": options.get("fh"), "flowid": options.get("flowid"), "keys": keys}
    expected = u32_keys(settings["family"], peer, port)
    problems = []
    if leaf["handle"] != settings["handle"]:
        problems.append("handle %s" % leaf["handle"])
    if leaf["flowid"] != BAND:
        problems.append("flowid %s" % leaf["flowid"])
    if keys != expected:
        problems.append("keys %s, requested %s" % (json.dumps(keys), json.dumps(expected)))
    if problems:
        raise CheckError("The selector is not the requested one: " + "; ".join(problems), "runtime_failure")
    return leaf


def remaining_filters(entries, settings):
    """What the owned preference holds after its leaf was removed: nothing,
    or its u32 table entries. A leaf or another filter is foreign state."""
    kept = []
    for entry in entries:
        options = entry.get("options")
        owned = (entry.get("kind"), entry.get("protocol"), entry.get("pref"), entry.get("parent")) == \
            ("u32", settings["protocol"], settings["pref"], ROOT)
        if not owned or (options is not None and not options.get("fh", "").endswith(":")):
            raise CheckError("A filter remains after the leaf was removed: " + json.dumps(entry), "runtime_failure")
        kept.append("table" if options is None else options.get("fh"))
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
    delay = child.get("options", {}).get("delay", {})
    return {"delay_ms": round(delay.get("delay", 0) * 1000, 3), "loss": child.get("options", {}).get("loss-random"),
            "counters": {key: child.get(key) for key in ("packets", "bytes", "drops", "backlog", "qlen")}}


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


def selector_scenario(helper, target, run, profile, delay, output):
    """Delay, then drop, only the selected peer, family and port of the
    target's egress, and keep the other paths unaffected."""
    settings = SELECTORS[profile]
    family = settings["family"]
    label = LABEL + "=" + run
    network = "ceph-extension-check-" + run
    names = {role: network + "-" + role for role in ("target", "shaper", "driver", "peer-b", "peer-c")}
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
        for peer in ("peer-b", "peer-c"):
            docker("run", *common, "--name", names[peer], "--network", network, "--cap-drop=ALL", *servers,
                   str(SELECTED_PORT), str(OTHER_PORT))
        endpoints = {}
        for role in ("target", "peer-b", "peer-c"):
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
        interface = target_interface(addresses, endpoints["target"][family], endpoints["target"]["mac"])
        device = interface["name"]
        other = 6 if family == 4 else 4
        peer = endpoints["peer-b"][family]
        result.update(interface=interface, selected={"peer": peer, "port": SELECTED_PORT, "family": family})
        paths = {"selected": (names["driver"], peer, SELECTED_PORT),
                 "same_peer_other_family": (names["driver"], endpoints["peer-b"][other], SELECTED_PORT),
                 "same_peer_other_port": (names["driver"], peer, OTHER_PORT),
                 "third_peer": (names["driver"], endpoints["peer-c"][family], SELECTED_PORT),
                 "reverse": (names["peer-b"], endpoints["target"][family], REVERSE_PORT)}
        for name, (client, host, port) in paths.items():
            code, text = docker("exec", client, "python3", "-c", READY, host, str(port), check_exit=False)
            if code:
                raise CheckError("No answer on the %s path: %s" % (name, text.strip()), "runtime_failure")

        def measure_path(name, phase, count=CONTROL_SAMPLES, timeout=10):
            client, host, port = paths[name]
            return requests(client, host, port, count, timeout, phase + " " + name, output)

        def controls(phase):
            return {name: measure_path(name, phase) for name in paths if name != "selected"}

        result["root_before"] = root_qdisc(names["shaper"], device)
        result["baseline"] = measure_path("selected", "baseline", SELECTOR_SAMPLES)
        # The root and its child first: until the filter exists, everything
        # takes the unshaped band.
        tc(names["shaper"], "qdisc", "add", "dev", device, "root", "handle", ROOT, "prio", "bands", "3",
           "priomap", *["0"] * 16)
        tc(names["shaper"], "qdisc", "add", "dev", device, "parent", BAND, "handle", CHILD, "netem",
           "delay", "%dms" % delay)
        match = "ip" if family == 4 else "ip6"
        tc(names["shaper"], "filter", "add", "dev", device, "protocol", settings["protocol"], "parent", ROOT,
           "pref", str(settings["pref"]), "handle", settings["handle"], "u32",
           "match", match, "dst", "%s/%d" % (peer, 32 if family == 4 else 128),
           "match", match, "protocol", "6", "0xff", "match", match, "dport", str(SELECTED_PORT), "0xffff",
           "flowid", BAND)
        # Without a parent, tc prints each entry's parent, protocol and
        # preference, and every egress filter of the device.
        raw = tc(names["shaper"], "-j", "filter", "show", "dev", device)
        (output / "filter.json").write_text(raw)
        result["selector"] = selector_leaf(raw, profile, peer, SELECTED_PORT)
        result["applied"] = selector_qdiscs(tc(names["shaper"], "-j", "-s", "qdisc", "show", "dev", device))
        # Controls run before selected traffic, so the child must count none.
        result["controls_delayed"] = controls("delayed")
        result["after_controls"] = selector_qdiscs(tc(names["shaper"], "-j", "-s", "qdisc", "show", "dev", device))
        result["delayed"] = measure_path("selected", "delayed", SELECTOR_SAMPLES)
        result["after_delayed"] = selector_qdiscs(tc(names["shaper"], "-j", "-s", "qdisc", "show", "dev", device))
        result["drain"] = drained(names["shaper"], device)
        apply_loss(names["shaper"], device, result["drain"])
        result["lost"] = measure_path("selected", "loss", 1, LOSS_TIMEOUT)
        result["controls_lost"] = controls("loss")
        result["after_loss"] = selector_qdiscs(tc(names["shaper"], "-j", "-s", "qdisc", "show", "dev", device))
        # The selection stops first, then the owned qdiscs go. Removing the
        # last leaf can remove its whole filter chain; only a u32 table that
        # outlived the leaf is removed by preference.
        tc(names["shaper"], "filter", "del", "dev", device, "parent", ROOT, "protocol", settings["protocol"],
           "pref", str(settings["pref"]), "handle", settings["handle"], "u32")
        remaining = json.loads(tc(names["shaper"], "-j", "filter", "show", "dev", device), object_pairs_hook=Pairs)
        result["after_leaf_removal"] = remaining_filters(remaining, settings)
        if result["after_leaf_removal"]:
            tc(names["shaper"], "filter", "del", "dev", device, "parent", ROOT, "protocol", settings["protocol"],
               "pref", str(settings["pref"]))
        tc(names["shaper"], "qdisc", "del", "dev", device, "root", "handle", ROOT)
        result["root_after"] = root_qdisc(names["shaper"], device)
        result["filters_after"] = json.loads(tc(names["shaper"], "-j", "filter", "show", "dev", device))
        result["restored"] = measure_path("selected", "restored", SELECTOR_SAMPLES)
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

    for phase in ("baseline", "restored"):
        value = median(result[phase])
        if value is None or value >= fast:
            problems.append("selected path %s: %s" % (phase, json.dumps(result[phase])))
    for phase in ("controls_delayed", "controls_lost"):
        for name, observation in result[phase].items():
            value = median(observation)
            if value is None or value >= fast:
                problems.append("%s %s: %s" % (phase, name, json.dumps(observation)))
    if result["applied"]["delay_ms"] != delay:
        problems.append("applied delay %r" % result["applied"]["delay_ms"])
    if result["after_controls"]["counters"]["packets"] != 0:
        problems.append("the child counted %r packets of control traffic" % result["after_controls"]["counters"]["packets"])
    value = median(result["delayed"])
    if value is None or value < delay:
        problems.append("selected path delayed: %s" % json.dumps(result["delayed"]))
    if not result["after_delayed"]["counters"]["packets"]:
        problems.append("the child counted no selected packets")
    if result["lost"]["outcomes"] != ["timeout"]:
        problems.append("selected path under loss: %s" % json.dumps(result["lost"]))
    if not result["after_loss"]["counters"]["drops"]:
        problems.append("the child dropped nothing under loss")
    before, after = result["root_before"], result["root_after"]
    if (after.get("kind"), after.get("handle")) != (before.get("kind"), before.get("handle")):
        problems.append("root qdisc %s %s after removal, %s %s before" % (after.get("kind"), after.get("handle"),
                                                                         before.get("kind"), before.get("handle")))
    if result["filters_after"]:
        problems.append("filters remain: " + json.dumps(result["filters_after"]))
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
