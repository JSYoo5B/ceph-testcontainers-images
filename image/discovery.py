#!/usr/bin/env python3
"""Build opt-in patch discovery images without changing production support.

Each native variant/architecture job packs its selected versions sequentially.
Only the existing role smoke/quick checks are used; publication is explicit.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import check
import patch_releases

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "ceph-discovery"
REGISTRY = "ghcr.io/jsyoo5b/ceph-testcontainers-images"
COHORT_SECONDS = 6 * 60 * 60 - 300


class CommandError(RuntimeError):
    def __init__(self, classification, command, code=None):
        super().__init__(classification + ": " + " ".join(command))
        self.classification, self.code = classification, code


class EnvironmentIneligible(ValueError):
    classification = "environment_ineligible"


def run(command, log, timeout, env=None, check_result=True):
    """Own the process group so timeout settlement also reaches build children."""
    with log.open("w") as output:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            for action, allowance in ((signal.SIGINT, 30), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                try:
                    os.killpg(process.pid, action)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=allowance)
                    break
                except subprocess.TimeoutExpired:
                    continue
            raise CommandError("timeout", command)
    if check_result and code:
        raise CommandError("command_failed", command, code)
    return code


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def plan(names):
    return {"releases": names, "matrix": {"include": [
        {"variant": variant, "architecture": architecture,
         "runner": "ubuntu-24.04" if architecture == "amd64" else "ubuntu-24.04-arm"}
        for variant in patch_releases.VARIANTS for architecture in patch_releases.ARCHITECTURES]}}


def tags(variant, release):
    prefix = REPOSITORY + ":discovery-" + variant + "-" + release + "-"
    return {role: prefix + role for role in check.ROLES}, prefix + "source"


def build_commands(release, variant, architecture, directory):
    values = patch_releases.release(release)[variant]
    role_tags, local_source = tags(variant, release)
    platform = "linux/" + architecture
    if variant == "official":
        source = values["source"]
        preparation = ["docker", "pull", "--platform", platform, source]
    else:
        source = local_source
        preparation = ["docker", "buildx", "build", "--load", "--platform", platform,
                       "--provenance=false", "--target", "all"]
        for key, value in patch_releases.build_args(release, variant).items():
            preparation += ["--build-arg", key + "=" + value]
        preparation += ["-t", source, str(ROOT / "image" / variant)]
    assemble = [sys.executable, str(ROOT / "image/roles/build.py"), "--source-runtime-env",
                "--package-backend", "rpm" if variant == "official" else "deb",
                "--source-image", source, "--skip-pull", "--platform", platform,
                "--tag", "discovery-" + variant + "-" + release, "--repository", REPOSITORY,
                "--check", "quick", "--output-dir", str(directory / "build")]
    return preparation, assemble, role_tags, source


def check_reports(directory, release, architecture, role_tags):
    build = json.loads((directory / "build/build-report.json").read_text())
    quick = json.loads((directory / "build/image-check/check-report.json").read_text())
    if (build.get("status") != "passed" or build.get("tags") != role_tags
            or build.get("checks", {}).get("smoke") != "passed"
            or build.get("checks", {}).get("image_check") != "passed"
            or not re.match(r"ceph version " + re.escape(release) + r"(?:\s|$)", build.get("ceph_version", ""))
            or quick.get("status") != "passed" or quick.get("level") != "quick"
            or quick.get("preflight") != "passed" or quick.get("contract") != "roles"
            or set(quick.get("images", {})) != set(check.ROLES)):
        raise ValueError("Incomplete or mismatched role smoke/quick reports")
    for image in quick["images"].values():
        versions = image.get("versions", {})
        if (image.get("status") != "passed" or image.get("platform") != "linux/" + architecture
                or image.get("cleanup", {}).get("status") != "passed" or not versions
                or any(value.get("release") != release for value in versions.values())):
            raise ValueError("Quick report lacks exact release/platform/cleanup evidence")


def candidate_tags(path, args, release):
    value = json.loads(path.read_text())
    if (value.get("status") != "passed" or value.get("release") != release
            or value.get("variant") != args.variant or value.get("architecture") != args.architecture
            or value.get("revision") != args.revision or value.get("run_id") != args.run_id
            or set(value.get("images", {})) != set(check.ROLES)):
        raise ValueError("Candidate is not a complete verified publication of this cohort")
    expected = [REGISTRY + ":ci-discovery-" + args.run_id + "-" + args.variant + "-" + release
                + "-" + role + "-linux-" + args.architecture for role in check.ROLES]
    if sorted(entry.get("candidate_tag", "") for entry in value["images"].values()) != sorted(expected):
        raise ValueError("Candidate local tags are not owned by this publication")
    return expected


def one(args, release, deadline, invoke=run):
    directory = args.output / release
    directory.mkdir()
    values = patch_releases.release(release)[args.variant]
    result = {"release": release, "variant": args.variant, "architecture": args.architecture,
              "revision": args.revision, "run_id": args.run_id, "publication": "not_requested",
              "qualification": "role-smoke-and-quick-only", "full_interoperability": False}
    if args.architecture not in values["architectures"]:
        result.update(status="unavailable", reason=values["unavailable"][args.architecture])
        save(directory / "report.json", result)
        return result
    builder = "discovery-" + args.run_id + "-" + args.variant + "-" + args.architecture + "-" + uuid.uuid4().hex[:12]
    env = dict(os.environ, BUILDX_BUILDER=builder)
    owned, cleanup_tags, introduced_source = False, [], None
    builder_dispatched, checked, published = False, False, False
    preparation, assemble, role_tags, source = build_commands(release, args.variant, args.architecture, directory)
    prefix = REPOSITORY + ":discovery-" + args.variant + "-" + release + "-*"

    def command(argv, name, limit, check_result=True, cleanup=False):
        remaining = 120 if cleanup else deadline - time.monotonic()
        if remaining <= 0:
            raise CommandError("cohort_timeout", argv)
        return invoke(argv, directory / (name + ".log"), min(limit, remaining), env, check_result)

    def local_tags(name, cleanup=False):
        command(["docker", "image", "ls", "--filter", "reference=" + prefix,
                 "--format", "{{.Repository}}:{{.Tag}}"], name, 60, cleanup=cleanup)
        return [line.strip() for line in (directory / (name + ".log")).read_text().splitlines() if line.strip()]

    try:
        if release == "21.2.0" and args.variant == "official" and args.architecture == "amd64":
            try:
                command(["/lib64/ld-linux-x86-64.so.2", "--help"], "cpu-preflight", 60)
                if "x86-64-v3 (supported, searched)" not in (directory / "cpu-preflight.log").read_text():
                    raise EnvironmentIneligible("Umbrella official AMD64 requires x86-64-v3")
            except (OSError, CommandError) as error:
                raise EnvironmentIneligible("Cannot qualify Umbrella x86-64-v3 CPU: " + str(error)) from error
        if local_tags("ownership-before"):
            raise ValueError("Refuse to overwrite pre-existing local discovery tags")
        owned = True
        if args.variant == "official":
            code = command(["docker", "image", "inspect", "--format", "{{.Id}}", source], "source-before", 60, False)
            if code:
                if "No such image" not in (directory / "source-before.log").read_text():
                    raise ValueError("Cannot establish whether source image already exists")
                introduced_source = source
        builder_dispatched = True
        command(["docker", "buildx", "create", "--name", builder, "--driver", "docker-container"], "builder-create", 120)
        command(preparation, "source", 2700)
        command(assemble, "roles", 2700)
        check_reports(directory, release, args.architecture, role_tags)
        checked = True
        if args.publish:
            result["publication"] = "running"
            candidate = directory / "candidate.json"
            command([sys.executable, str(ROOT / "image/publish.py"), "stage-discovery", "--release", release,
                     "--variant", args.variant, "--architecture", args.architecture, "--revision", args.revision,
                     "--run-id", args.run_id, "--report", str(directory / "build/image-check/check-report.json"),
                     "--output", str(candidate)], "publish", 1800)
            cleanup_tags = candidate_tags(candidate, args, release)
            published = True
            result["publication"] = "passed"
        result["status"] = "passed"
    except (CommandError, ValueError, OSError, KeyError) as error:
        result.update(status="failed", classification=getattr(error, "classification", "invalid_or_incomplete_evidence"), error=str(error))
        if isinstance(error, EnvironmentIneligible):
            result["status"] = "environment_ineligible"
        if result["publication"] == "running":
            result["publication"] = "failed"
    finally:
        errors = []
        if owned and (published or not checked):
            try:
                observed = local_tags("ownership-after", cleanup=True)
                allowed = set(role_tags.values()) | {tags(args.variant, release)[1]}
                if not set(observed) <= allowed:
                    raise ValueError("Unexpected local tag; cleanup refuses unowned images")
                for number, tag in enumerate(observed + cleanup_tags + ([introduced_source] if introduced_source else [])):
                    command(["docker", "image", "rm", tag], "image-cleanup-" + str(number), 120, cleanup=True)
                result["image_cleanup"] = "passed"
            except (CommandError, ValueError, OSError) as error:
                errors.append(str(error))
        else:
            result["image_cleanup"] = "retained_unpublished" if checked else "not_owned"
        if builder_dispatched:
            try:
                command(["docker", "buildx", "rm", builder], "builder-cleanup", 120, cleanup=True)
            except (CommandError, OSError) as error:
                errors.append(str(error))
        if errors:
            result.update(status="failed", cleanup_errors=errors)
        save(directory / "report.json", result)
    return result


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "build"):
        child = commands.add_parser(name)
        child.add_argument("--releases", default="all")
        if name == "plan":
            child.add_argument("--github-output", type=Path)
        else:
            child.add_argument("--variant", required=True, choices=patch_releases.VARIANTS)
            child.add_argument("--architecture", required=True, choices=patch_releases.ARCHITECTURES)
            child.add_argument("--output", required=True, type=Path)
            child.add_argument("--revision", required=True)
            child.add_argument("--run-id", required=True)
            child.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    args.names = patch_releases.selected(args.releases)
    if args.command == "build":
        if not re.fullmatch(r"[0-9a-f]{40}", args.revision) or not re.fullmatch(r"[0-9]{1,24}-[0-9]{1,8}", args.run_id):
            raise ValueError("Use an exact revision and run-attempt identity")
        args.output = args.output.resolve()
        if args.output.exists() and any(args.output.iterdir()):
            raise ValueError("Discovery output must be empty; do not overwrite earlier evidence")
    return args


def main(argv=None, invoke=run):
    args = arguments(argv)
    if args.command == "plan":
        value = plan(args.names)
        if args.github_output:
            with args.github_output.open("a") as output:
                for field in ("releases", "matrix"):
                    output.write(field + "=" + json.dumps(value[field], separators=(",", ":")) + "\n")
        print(json.dumps(value))
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + COHORT_SECONDS
    results = [one(args, name, deadline, invoke) for name in args.names]
    failed = any(result["status"] not in ("passed", "unavailable") for result in results)
    save(args.output / "summary.json", {"status": "failed" if failed else "complete", "results": results,
                                       "finished_at": datetime.now(timezone.utc).isoformat()})
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print("Discovery build refused: " + str(error), file=sys.stderr)
        sys.exit(2)
