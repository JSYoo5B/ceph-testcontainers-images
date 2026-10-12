#!/usr/bin/env python3
"""Check an opt-in extension image against its contract (docs/EXTENSIONS.md).

The quick check (default) runs the extension's probe in the image without any
capability or network. The functional check (--functional) also checks this
Docker engine: the kernel feature with NET_ADMIN in a disposable namespace,
then a delay that the helper applies, changes and removes inside the network
namespace of a container from each --target image.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

import check

HERE = Path(__file__).resolve().parent
EXTENSIONS = {"netem": {"contract": "1", "probe": HERE / "extensions" / "netem" / "check-netem.sh"}}
LABEL = "io.ceph-testcontainers.extension-check"
HANDLE = "c7e1:"
PORT = 7000
RESPONSE = 1024
SAMPLES = 9

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
    parser.add_argument("--delay-ms", type=check.positive_number, default=200, metavar="MS")
    parser.add_argument("--output-dir", type=Path, help="Fresh report directory (default: artifacts/extension-check-UUID)")
    parser.add_argument("--probe-timeout", type=check.positive_number, default=120, metavar="SECONDS")
    args = parser.parse_args(argv)
    if args.target and not args.functional:
        parser.error("--target requires --functional")
    if args.functional and not args.target:
        parser.error("--functional requires at least one --target")
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


def tc(container, *command):
    code, output = docker("exec", container, "tc", *command, check_exit=False)
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
