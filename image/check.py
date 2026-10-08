#!/usr/bin/env python3
"""Check whether supplied Ceph images meet the image requirements.

The quick check (default) inspects each image for the required components.
The full check also runs every functional scenario on real clusters.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "functional"))
import suite  # noqa: E402

ROLES = ("control", "osd", "rgw", "mds", "all")
VERSION = re.compile(r"ceph version (\S+) \(([^)]+)\)")


class CheckError(Exception):
    pass


def positive_number(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", action="append", required=True, metavar="ROLE=REF",
                        help="Repeat for any of control, osd, rgw, mds, all; extra daemons are allowed")
    parser.add_argument("--platform", choices=("linux/arm64", "linux/amd64"),
                        help="Reject other platforms; otherwise record each local image's platform")
    parser.add_argument("--output-dir", type=Path, help="Fresh report/log directory (default: artifacts/image-check-UUID)")
    parser.add_argument("--full", action="store_true",
                        help="Full check: also run every functional scenario on real clusters")
    parser.add_argument("--scenario", action="append", choices=suite.SCENARIOS,
                        help="With --full, run only selected scenarios; records a scoped functional check")
    parser.add_argument("--probe-timeout", type=positive_number, default=120, metavar="SECONDS")
    args = parser.parse_args(argv)
    if args.scenario and not args.full:
        parser.error("--scenario requires --full")
    return args


def image_arguments(values):
    images = {}
    for value in values:
        role, separator, reference = value.partition("=")
        if not separator or role not in ROLES or not reference.strip() or reference.startswith("-"):
            raise CheckError("Expected ROLE=REF with role in " + ", ".join(ROLES))
        if role in images:
            raise CheckError("Duplicate image role: " + role)
        images[role] = reference
    return images


def runtime_sets(images):
    result = {}
    mixed = set(ROLES[:-1])
    if mixed.issubset(images):
        result["mixed"] = {role: images[role] for role in ROLES[:-1]}
    if "all" in images:
        result["all"] = {role: images["all"] for role in ROLES[:-1]}
    if not result:
        raise CheckError("--full requires all=REF or all four control/osd/rgw/mds images")
    return result


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, **kwargs)
    if result.returncode:
        raise CheckError("Command failed: " + " ".join(command) + "\n" + result.stdout + result.stderr)
    return result.stdout


def image_metadata(command):
    data = json.loads(run(command))
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise CheckError("Docker inspection returned no image metadata")
    metadata = data[0]
    if any(not isinstance(metadata.get(key), str) for key in ("Id", "Os", "Architecture")):
        raise CheckError("Docker inspection lacks image identity/platform")
    return metadata


def inspect_image(reference, platform):
    # Resolve the tag once to a runnable local ID. Containerd's platform inspect
    # may return a child manifest ID that Docker cannot create by ID directly.
    command = ["docker", "image", "inspect", reference]
    original = image_metadata(command)
    identity = original.get("Id", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
        raise CheckError("Docker inspection did not return an immutable image ID")
    metadata = original
    if platform:
        try:
            metadata = image_metadata(["docker", "image", "inspect", "--platform", platform, identity])
        except CheckError as error:
            # Docker 28's classic image store exposes only the loaded platform
            # and has no inspect --platform option. Still enforce its platform.
            if "unknown flag: --platform" not in str(error):
                raise
    actual = metadata["Os"] + "/" + metadata["Architecture"]
    if actual not in ("linux/arm64", "linux/amd64") or (platform and actual != platform):
        raise CheckError("Unsupported or mismatched image platform: " + actual)
    return {"reference": reference, "image_id": identity, "platform_image_id": metadata["Id"], "platform": actual,
            "default_platform": original["Os"] + "/" + original["Architecture"],
            "repo_digests": metadata.get("RepoDigests", []),
            "runtime_env": (metadata.get("Config") or {}).get("Env", [])}


def parse_probe(output):
    checks = []
    versions = {}
    failures = {}
    for line in output.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        if fields[0] == "CHECK":
            checks.append({"name": fields[2], "status": fields[1]})
        elif fields[0] == "FAILURE":
            failures[fields[2]] = fields[1]
        elif fields[0] == "VERSION":
            match = VERSION.search(fields[2])
            if not match:
                raise CheckError("Unrecognized Ceph version: " + line)
            versions[fields[1]] = {"release": match[1], "commit": match[2], "output": fields[2]}
    if not checks or (not versions and all(check["status"] == "passed" for check in checks)):
        raise CheckError("Runtime probe returned no checks or versions")
    if len({(version["release"], version["commit"]) for version in versions.values()}) > 1:
        raise CheckError("Ceph binaries within one image have different release/commit values")
    for item in checks:
        if item["status"] != "passed":
            item.update(failure_stage="quick:" + item["name"],
                        failure_kind=failures.get(item["name"], "quick_failure"))
    return checks, versions


def probe_image(role, image, args, output):
    container = "ceph-image-check-" + uuid.uuid4().hex
    try:
        run(["docker", "create", "--name", container, "--pull=never", "--network=none", "--user=0:0", "--interactive",
             "--hostname=ceph-image-check", "--add-host=ceph-image-check:127.0.0.1",
             "--platform", image["platform"], "--entrypoint=/bin/sh", image["image_id"], "-s", "--", role])
        result = subprocess.run(["docker", "start", "--attach", "--interactive", container],
                                input=(HERE / "check-runtime.sh").read_text(), capture_output=True,
                                text=True, timeout=args.probe_timeout)
        log = output / (role + ".log")
        log.write_text(result.stdout + result.stderr)
        # Preserve all failed check names even if no binary could report a version.
        checks, versions = parse_probe(result.stdout)
        image.update({"checks": checks, "versions": versions, "log": log.name,
                      "status": "passed" if result.returncode == 0 and
                      all(check["status"] == "passed" for check in checks) else "failed"})
        image["exit_code"] = result.returncode
        if "COMPLETE\t" + role not in result.stdout.splitlines():
            image.update({"status": "failed", "error": "Runtime probe did not finish all checks"})
    except subprocess.TimeoutExpired as error:
        image.update({"status": "failed", "error": "Runtime probe timed out",
                      "failure_stage": "quick-probe", "failure_kind": "timeout"})
        (output / (role + ".log")).write_bytes((error.stdout or b"") + (error.stderr or b""))
        raise CheckError("Runtime probe timed out: " + role) from error
    except CheckError as error:
        image.update({"status": "failed", "error": str(error), "log": role + ".log",
                      "failure_stage": "quick-probe", "failure_kind": "checker_failure"})
        raise
    finally:
        # No mounts or shared state. Remove even after a killed Docker client.
        try:
            run(["docker", "rm", "--force", container])
            image["cleanup"] = {"status": "passed", "container": container}
        except CheckError as error:
            if "No such container" not in str(error):
                image["cleanup"] = {"status": "failed", "container": container, "error": str(error)}
                image["status"] = "failed"
                raise
            image["cleanup"] = {"status": "passed", "container": container, "already_absent": True}


def functional_sha256():
    digest = hashlib.sha256()
    for path in sorted((HERE / "functional").rglob("*.py")):
        if "tests" not in path.parts:
            digest.update(path.relative_to(HERE).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def scenarios(name, images, output, selected=None):
    results = suite.run(images, selected or suite.SCENARIOS, output / ("scenarios-" + name),
                        log=lambda line: print("  " + line, flush=True))
    failed = [scenario for scenario, result in results.items() if result["status"] != "passed"]
    status = "failed" if failed else "passed"
    return {"status": status, "images": images, "scenarios": results, "failed": failed}


def save(output, report):
    (output / "check-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    args = arguments(argv)
    references = image_arguments(args.image)
    sets = runtime_sets(references) if args.full else {}
    if not shutil.which("docker"):
        raise CheckError("Docker CLI must be installed")
    output = (args.output_dir or Path("artifacts") / ("image-check-" + uuid.uuid4().hex)).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise CheckError("Output directory must be empty: " + str(output))
    report = {"schema": 4, "level": ("functional-selected" if args.scenario else "full") if args.full else "quick",
              "requested_scenarios": args.scenario or (list(suite.SCENARIOS) if args.full else []),
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
              "preflight": "running", "scenarios": "pending" if sets else "not_requested",
              "images": {}, "functional": {},
              "contract_sha256": hashlib.sha256((HERE / "check-runtime.sh").read_bytes()).hexdigest(),
              "functional_sha256": functional_sha256(),
              "checker_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save(output, report)
    try:
        # Resolve every input before starting any checks, so tags cannot drift.
        resolved = {}
        for role, reference in references.items():
            if reference not in resolved:
                resolved[reference] = inspect_image(reference, args.platform)
            report["images"][role] = dict(resolved[reference])
        platforms = {image["platform"] for image in report["images"].values()}
        if len(platforms) != 1:
            raise CheckError("All supplied images must target the same platform")
        if sets and any(image.get("default_platform", image["platform"]) != image["platform"]
                        for image in report["images"].values()):
            raise CheckError("Containers use the local default platform; prepare a single-platform image for --full")
        for role, image in report["images"].items():
            print("Checking " + role + ": " + image["reference"], flush=True)
            probe_image(role, image, args, output)
            save(output, report)
        if any(image["status"] != "passed" for image in report["images"].values()):
            raise CheckError("Image requirements failed; see per-role checks and logs")
        releases = {version["release"] for image in report["images"].values() for version in image["versions"].values()}
        if len(releases) != 1:
            raise CheckError("Supplied images have different Ceph releases")
        report["preflight"] = "passed"
        for name, selected in sets.items():
            identities = {role: report["images"]["all" if name == "all" else role]["image_id"] for role in selected}
            report["scenarios"] = "running"
            report["functional"][name] = {"status": "running"}
            save(output, report)
            print("Running functional scenarios: " + name, flush=True)
            report["functional"][name] = scenarios(name, identities, output, args.scenario)
            save(output, report)
        if sets:
            failed = [name for name, result in report["functional"].items() if result["status"] != "passed"]
            if failed:
                report["scenarios"] = "failed"
                raise CheckError("Functional scenarios failed for: " + ", ".join(failed))
            report["scenarios"] = "passed"
        report["status"] = "passed"
    except (CheckError, suite.ClusterError, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        report["status"] = "failed"
        report["error"] = str(error) or type(error).__name__
        if report["preflight"] == "running":
            report["preflight"] = "failed"
        if report["scenarios"] in ("pending", "running"):
            report["scenarios"] = "failed" if report["scenarios"] == "running" else "not_run"
        for result in report["functional"].values():
            if result["status"] == "running":
                result["status"] = "failed"
        print(report["error"], file=sys.stderr)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(output, report)
        print("Preflight: " + report["preflight"] + "; scenarios: " + report["scenarios"], flush=True)
        print("Report: " + str(output / "check-report.json"), flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (CheckError, OSError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
