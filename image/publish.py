#!/usr/bin/env python3
"""Publish the exact images that passed CI; promote only a complete release."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import check
import releases

REGISTRY = "ghcr.io/jsyoo5b/ceph-testcontainers-images"
VARIANTS = ("official", "debian", "ubuntu")
ARCHITECTURES = ("amd64", "arm64")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class PublishError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PublishError(message)


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise PublishError("Command failed: " + " ".join(command) + "\n" + result.stderr[-3000:])
    return result.stdout


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def manifest(reference):
    return json.loads(run(["docker", "buildx", "imagetools", "inspect", reference,
                           "--format", "{{json .Manifest}}"] ))


def raw_manifest(reference):
    return json.loads(run(["docker", "buildx", "imagetools", "inspect", "--raw", reference]))


def release_tag(release, variant, role, architecture=None):
    return REGISTRY + ":" + variant + "-" + release + "-" + role + (
        "-linux-" + architecture if architecture else "")


def previous_manifest(tag):
    """The manifest a release tag points to, or None before its first publication."""
    try:
        return manifest(tag)
    except PublishError as error:
        if "not found" in str(error) or "manifest unknown" in str(error):
            return None
        raise


def validate_report(report, architecture, release):
    require(report.get("status") == "passed" and report.get("level") == "full",
            "Publishing requires a passing full check")
    require(report.get("preflight") == "passed" and report.get("scenarios") == "passed",
            "Quick and functional checks must both pass")
    require(set(report.get("images", {})) == set(check.ROLES), "All five images must be checked")
    require(set(report.get("functional", {})) == {"mixed", "all"}, "Check mixed roles and all separately")
    for image in report["images"].values():
        require(image.get("status") == "passed" and image.get("platform") == "linux/" + architecture,
                "Every image must pass on the requested platform")
        require(image.get("cleanup", {}).get("status") == "passed", "Quick cleanup must pass")
        require(image.get("versions") and all(v["release"] == release for v in image["versions"].values()),
                "Every image must use the published Ceph release " + release)
    for topology, result in report["functional"].items():
        require(result.get("status") == "passed", "Functional topology failed: " + topology)
        scenarios = result.get("scenarios", {})
        require(set(scenarios) == set(check.suite.SCENARIOS) | {"cleanup"}, "Every scenario must be recorded")
        require(all(s.get("status") == "passed" for s in scenarios.values()), "Failures and skips cannot publish")
        require(not scenarios["cleanup"].get("leftovers") and not scenarios["cleanup"].get("errors"),
                "Functional cleanup must leave no resources")
        expected = {role: report["images"]["all" if topology == "all" else role]["image_id"]
                    for role in check.ROLES[:-1]}
        require(result.get("images") == expected, "Functional checks must use the checked immutable image IDs")
    require(report.get("contract_sha256") == hashlib.sha256((check.HERE / "check-runtime.sh").read_bytes()).hexdigest()
            and report.get("functional_sha256") == check.functional_sha256()
            and report.get("checker_sha256") == hashlib.sha256(Path(check.__file__).read_bytes()).hexdigest(),
            "Check report must come from the current checker and scenarios")


def verify_image(entry):
    reference = REGISTRY + "@" + entry["digest"]
    metadata = raw_manifest(reference)
    require(metadata.get("config", {}).get("digest") == entry["config_digest"],
            "Uploaded config differs from the tested image: " + reference)
    config = json.loads(run(["docker", "buildx", "imagetools", "inspect", reference,
                             "--format", "{{json .Image}}"] ))
    require(config.get("os") == "linux" and config.get("architecture") == entry["architecture"],
            "Uploaded image platform differs from the tested image")
    require(config.get("rootfs", {}).get("diff_ids") == entry["rootfs_diff_ids"],
            "Uploaded layers differ from the tested image")
    require(config.get("config", {}).get("Labels", {}).get("io.ceph-testcontainers.role") == entry["role"],
            "Uploaded image has the wrong role")


def stage(args):
    report_bytes = args.report.read_bytes()
    report = json.loads(report_bytes)
    validate_report(report, args.architecture, args.release)
    require(re.fullmatch(r"[0-9a-f]{40}", args.revision) is not None, "Revision must be a full commit SHA")
    require(re.fullmatch(r"[0-9]+-[0-9]+", args.run_id) is not None, "Run identity must include run and attempt")
    prepared = {}
    # Check every immutable local identity before uploading the first image.
    for role, checked in report["images"].items():
        local = json.loads(run(["docker", "image", "inspect", checked["image_id"]]))[0]
        require(local["Id"] == checked["image_id"] and local["Os"] + "/" + local["Architecture"] == checked["platform"],
                "Local image identity/platform differs from the tested result")
        require(local.get("Config", {}).get("Labels", {}).get("io.ceph-testcontainers.role") == role,
                "Local image role differs from the checked role")
        config_digest = local.get("Descriptor", {}).get("annotations", {}).get("config.digest", local["Id"])
        require(DIGEST.fullmatch(config_digest) is not None, "Missing tested image config digest")
        prepared[role] = {"role": role, "architecture": args.architecture,
                          "image_id": checked["image_id"], "config_digest": config_digest,
                          "rootfs_diff_ids": local["RootFS"]["Layers"]}
    result = {"schema": 1, "release": args.release, "variant": args.variant, "architecture": args.architecture,
              "revision": args.revision, "run_id": args.run_id, "status": "uploading",
              "report_sha256": hashlib.sha256(report_bytes).hexdigest(), "images": {}}
    save(args.output, result)
    for role, entry in prepared.items():
        tag = REGISTRY + ":ci-" + args.run_id + "-" + args.variant + "-" + args.release + "-" + role + "-linux-" + args.architecture
        run(["docker", "tag", entry["image_id"], tag])
        (args.output.parent / ("push-" + role + ".log")).write_text(run(["docker", "push", tag]))
        entry.update(digest=manifest(tag)["digest"], candidate_tag=tag)
        require(DIGEST.fullmatch(entry["digest"]) is not None, "Invalid uploaded manifest digest")
        verify_image(entry)
        result["images"][role] = entry
        save(args.output, result)
        print("Uploaded tested image: " + tag + "@" + entry["digest"], flush=True)
    result["status"] = "passed"
    save(args.output, result)


def collect_candidates(directory, revision, run_id, release):
    result = {}
    for path in sorted(directory.rglob("candidate.json")):
        candidate = json.loads(path.read_text())
        key = (candidate.get("variant"), candidate.get("architecture"))
        require(key not in result, "Duplicate candidate platform")
        require(candidate.get("status") == "passed" and candidate.get("revision") == revision
                and candidate.get("run_id") == run_id, "Candidates must pass in the same CI run and revision")
        require(candidate.get("release") == release, "Candidates must be built for release " + release)
        require(set(candidate.get("images", {})) == set(check.ROLES), "Incomplete candidate roles")
        for role, entry in candidate["images"].items():
            require(entry.get("role") == role and entry.get("architecture") == key[1], "Candidate role/platform mismatch")
            require(all(isinstance(entry.get(field), str) and DIGEST.fullmatch(entry[field])
                        for field in ("digest", "config_digest", "image_id")), "Invalid candidate identity")
        result[key] = candidate
    require(set(result) == {(v, a) for v in VARIANTS for a in ARCHITECTURES}, "All six candidate platforms must pass")
    return result


def promote(args):
    candidates = collect_candidates(args.candidates, args.revision, args.run_id, args.release)
    entries = [entry for candidate in candidates.values() for entry in candidate["images"].values()]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(verify_image, entries))
    tags = [release_tag(args.release, v, r, a) for v in VARIANTS for r in check.ROLES for a in (*ARCHITECTURES, None)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        previous = dict(zip(tags, pool.map(previous_manifest, tags)))
    result = {"status": "promoting", "release": args.release, "revision": args.revision, "run_id": args.run_id,
              "previous": previous, "platforms": {}, "indexes": {}}
    save(args.output, result)
    digests = {v: {a: {} for a in ARCHITECTURES} for v in VARIANTS}
    for (variant, architecture), candidate in candidates.items():
        for role, entry in candidate["images"].items():
            tag = release_tag(args.release, variant, role, architecture)
            run(["docker", "buildx", "imagetools", "create", "--prefer-index=false", "--tag", tag,
                 REGISTRY + "@" + entry["digest"]])
            require(manifest(tag)["digest"] == entry["digest"], "Platform tag differs from tested candidate")
            digests[variant][architecture][role] = entry["digest"]
            result["platforms"][tag] = entry
            save(args.output, result)
    for variant in VARIANTS:
        for role in check.ROLES:
            tag = release_tag(args.release, variant, role)
            expected = {("linux", a): digests[variant][a][role] for a in ARCHITECTURES}
            run(["docker", "buildx", "imagetools", "create", "--tag", tag] +
                [REGISTRY + "@" + digest for digest in expected.values()])
            raw = raw_manifest(tag)
            observed = {(m["platform"]["os"], m["platform"]["architecture"]): m["digest"] for m in raw.get("manifests", [])}
            require(len(raw.get("manifests", [])) == 2 and observed == expected, "Index differs from tested platforms")
            result["indexes"][tag] = manifest(tag)
            save(args.output, result)
            print("Promoted tested platforms: " + tag, flush=True)
    result.update(status="passed", images=digests, finished_at=datetime.now(timezone.utc).isoformat())
    save(args.output, result)
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write("images=" + json.dumps(digests, separators=(",", ":")) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("stage")
    upload.add_argument("--report", type=Path, required=True)
    upload.add_argument("--variant", choices=VARIANTS, required=True)
    upload.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    promote_parser = commands.add_parser("promote")
    promote_parser.add_argument("--candidates", type=Path, required=True)
    promote_parser.add_argument("--github-output", type=Path)
    for command in (upload, promote_parser):
        command.add_argument("--release", choices=tuple(releases.RELEASES), default=releases.DEFAULT)
        command.add_argument("--revision", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    (stage if args.command == "stage" else promote)(args)


if __name__ == "__main__":
    try:
        main()
    except (PublishError, OSError, ValueError, KeyError) as error:
        print("Image publication failed: " + str(error), file=sys.stderr)
        sys.exit(1)
