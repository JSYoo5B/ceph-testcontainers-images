#!/usr/bin/env python3
"""Publish the exact images that passed CI; promote only a complete release or
an extension helper checked on both platforms."""
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
CONTRACT = "legacy"

def active_roles():
    return check.contract.ROLES + ("all",) if CONTRACT == "roles-v2" else check.ROLES

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
    return REGISTRY + ":" + ("roles-v2-" if CONTRACT == "roles-v2" else "") + variant + "-" + release + "-" + role + (
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
    require(report.get("contract", "legacy") == CONTRACT, "Image contract differs from publication namespace")
    require(report.get("status") == "passed" and report.get("level") == "full",
            "Publishing requires a passing full check")
    require(report.get("preflight") == "passed" and report.get("scenarios") == "passed",
            "Quick and functional checks must both pass")
    require(set(report.get("images", {})) == set(active_roles()), "All contract images must be checked")
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
                    for role in active_roles()[:-1]}
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
    label, value = (("io.ceph-testcontainers.extension", entry["extension"]) if "extension" in entry
                    else ("io.ceph-testcontainers.role", entry["role"]))
    require(config.get("config", {}).get("Labels", {}).get(label) == value,
            "Uploaded image has the wrong role or extension")


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
              "report_sha256": hashlib.sha256(report_bytes).hexdigest(), "contract": CONTRACT, "images": {}}
    save(args.output, result)
    for role, entry in prepared.items():
        tag = REGISTRY + ":ci-" + ("roles-v2-" if CONTRACT == "roles-v2" else "") + args.run_id + "-" + args.variant + "-" + args.release + "-" + role + "-linux-" + args.architecture
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
        require(candidate.get("contract", "legacy") == CONTRACT, "Candidate contract differs")
        require(candidate.get("release") == release, "Candidates must be built for release " + release)
        require(set(candidate.get("images", {})) == set(active_roles()), "Incomplete candidate roles")
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
    tags = [release_tag(args.release, v, r, a) for v in VARIANTS for r in active_roles() for a in (*ARCHITECTURES, None)]
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
        for role in active_roles():
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


def extensions():
    """The extension checker, imported only for extension commands, so that a
    defect in it cannot stop the publication of release images."""
    import extension_check
    return extension_check


def extension_tag(extension, architecture=None):
    """The tag of an extension helper; the contract version is part of it."""
    return REGISTRY + ":extension-" + extension + "-v" + extensions().EXTENSIONS[extension]["contract"] + (
        "-linux-" + architecture if architecture else "")


def extension_targets():
    """The targets every published helper must pass: the official image and the
    published OSD role image of every release."""
    return {reference for release, value in releases.RELEASES.items()
            for reference in (value["official"], release_tag(release, "official", "osd"))}


def validate_extension_report(report, extension, architecture):
    extension_check = extensions()
    require(extension in extension_check.EXTENSIONS, "Unknown extension " + repr(extension))
    contract = extension_check.EXTENSIONS[extension]["contract"]
    require(report.get("status") == "passed" and report.get("level") == "functional",
            "Publishing an extension requires a passing functional check")
    require(report.get("extension") == extension and report.get("contract") == contract,
            "The report checked a different extension or contract")
    require(report.get("checker_sha256") == extension_check.checker_sha256(),
            "Extension report must come from the current checker and probe")
    image = report.get("image", {})
    require(image.get("status") == "passed" and image.get("platform") == "linux/" + architecture,
            "The helper must pass on the requested platform")
    require(image.get("cleanup", {}).get("status") == "passed", "Quick cleanup must pass")
    labels = image.get("labels", {})
    require(labels.get("io.ceph-testcontainers.extension") == extension and
            labels.get("io.ceph-testcontainers.extension.contract") == contract,
            "A published helper must declare its extension and contract")
    kernel = report.get("kernel", {})
    require(kernel.get("status") == "passed" and kernel.get("cleanup", {}).get("status") == "passed",
            "The kernel check must pass and clean up")
    targets = report.get("targets", {})
    require(extension_targets() <= set(targets),
            "The official and the published OSD role image of every release must be checked targets")
    for result in targets.values():
        require(result.get("status") == "passed" and result.get("cleanup", {}).get("status") == "passed"
                and not result["cleanup"].get("leftovers"), "Every target scenario must pass and clean up")
    selectors = report.get("selectors", {})
    require(set(extension_check.SELECTORS) <= set(selectors), "Every selector profile of the contract must be checked")
    for profile in extension_check.SELECTORS:
        results = selectors[profile]
        require(extension_targets() <= set(results), "Each selector profile must be checked in every release target")
        for result in results.values():
            require(result.get("status") == "passed" and result.get("cleanup", {}).get("status") == "passed"
                    and not result["cleanup"].get("leftovers"), "Every selector scenario must pass and clean up")
    # The optional profiles that the published helper promises.
    for key, name in (("rate", "bandwidth"), ("packet_faults", "packet-fault")):
        profile = report.get(key, {})
        require(profile.get("status") == "passed" and profile.get("scope") == "functional",
                "The %s profile must pass functionally" % name)
        results = profile.get("targets", {})
        require(extension_targets() <= set(results), "The %s profile must be checked in every release target" % name)
        for result in results.values():
            require(result.get("status") == "passed" and result.get("cleanup", {}).get("status") == "passed"
                    and not result["cleanup"].get("leftovers"), "Every %s scenario must pass and clean up" % name)


def stage_extension(args):
    report_bytes = args.report.read_bytes()
    report = json.loads(report_bytes)
    validate_extension_report(report, args.extension, args.architecture)
    require(re.fullmatch(r"[0-9a-f]{40}", args.revision) is not None, "Revision must be a full commit SHA")
    require(re.fullmatch(r"[0-9]+-[0-9]+", args.run_id) is not None, "Run identity must include run and attempt")
    checked = report["image"]
    local = json.loads(run(["docker", "image", "inspect", checked["image_id"]]))[0]
    require(local["Id"] == checked["image_id"] and local["Os"] + "/" + local["Architecture"] == checked["platform"],
            "Local image identity/platform differs from the tested result")
    require(local.get("Config", {}).get("Labels", {}).get("io.ceph-testcontainers.extension") == args.extension,
            "Local image extension differs from the checked extension")
    config_digest = local.get("Descriptor", {}).get("annotations", {}).get("config.digest", local["Id"])
    require(DIGEST.fullmatch(config_digest) is not None, "Missing tested image config digest")
    entry = {"extension": args.extension, "architecture": args.architecture, "image_id": checked["image_id"],
             "config_digest": config_digest, "rootfs_diff_ids": local["RootFS"]["Layers"]}
    result = {"schema": 1, "extension": args.extension, "contract": report["contract"],
              "architecture": args.architecture, "revision": args.revision, "run_id": args.run_id,
              "status": "uploading", "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
              "targets": sorted(report["targets"]), "selectors": sorted(report["selectors"]),
              "environment": report.get("environment"),
              "kernel": report["kernel"].get("kernel")}
    save(args.output, result)
    tag = REGISTRY + ":ci-" + args.run_id + "-" + extension_tag(args.extension, args.architecture).split(":", 1)[1]
    run(["docker", "tag", entry["image_id"], tag])
    (args.output.parent / "push-extension.log").write_text(run(["docker", "push", tag]))
    entry.update(digest=manifest(tag)["digest"], candidate_tag=tag)
    require(DIGEST.fullmatch(entry["digest"]) is not None, "Invalid uploaded manifest digest")
    verify_image(entry)
    result.update(image=entry, status="passed")
    save(args.output, result)
    print("Uploaded tested helper: " + tag + "@" + entry["digest"], flush=True)


def collect_extension_candidates(directory, revision, run_id):
    result = {}
    for path in sorted(directory.rglob("extension-candidate.json")):
        candidate = json.loads(path.read_text())
        key = (candidate.get("extension"), candidate.get("architecture"))
        require(key not in result, "Duplicate extension candidate platform")
        require(candidate.get("status") == "passed" and candidate.get("revision") == revision
                and candidate.get("run_id") == run_id, "Candidates must pass in the same CI run and revision")
        entry = candidate.get("image", {})
        require(entry.get("extension") == key[0] and entry.get("architecture") == key[1],
                "Candidate extension/platform mismatch")
        require(all(isinstance(entry.get(field), str) and DIGEST.fullmatch(entry[field])
                    for field in ("digest", "config_digest", "image_id")), "Invalid candidate identity")
        result[key] = candidate
    require(set(result) == {(e, a) for e in extensions().EXTENSIONS for a in ARCHITECTURES},
            "Every extension must pass on both platforms")
    return result


def promote_extension(args):
    candidates = collect_extension_candidates(args.candidates, args.revision, args.run_id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(verify_image, [candidate["image"] for candidate in candidates.values()]))
    names = extensions().EXTENSIONS
    tags = [extension_tag(e, a) for e in names for a in (*ARCHITECTURES, None)]
    result = {"status": "promoting", "revision": args.revision, "run_id": args.run_id,
              "previous": {tag: previous_manifest(tag) for tag in tags}, "platforms": {}, "indexes": {}}
    save(args.output, result)
    digests = {extension: {} for extension in names}
    for (extension, architecture), candidate in sorted(candidates.items()):
        tag = extension_tag(extension, architecture)
        run(["docker", "buildx", "imagetools", "create", "--prefer-index=false", "--tag", tag,
             REGISTRY + "@" + candidate["image"]["digest"]])
        require(manifest(tag)["digest"] == candidate["image"]["digest"], "Platform tag differs from tested candidate")
        digests[extension][architecture] = candidate["image"]["digest"]
        result["platforms"][tag] = candidate["image"]
        save(args.output, result)
    for extension in names:
        tag = extension_tag(extension)
        expected = {("linux", a): digests[extension][a] for a in ARCHITECTURES}
        run(["docker", "buildx", "imagetools", "create", "--tag", tag] +
            [REGISTRY + "@" + digest for digest in expected.values()])
        raw = raw_manifest(tag)
        observed = {(m["platform"]["os"], m["platform"]["architecture"]): m["digest"] for m in raw.get("manifests", [])}
        require(len(raw.get("manifests", [])) == 2 and observed == expected, "Index differs from tested platforms")
        result["indexes"][tag] = manifest(tag)
        save(args.output, result)
        print("Promoted tested helper platforms: " + tag, flush=True)
    result.update(status="passed", images=digests, finished_at=datetime.now(timezone.utc).isoformat())
    save(args.output, result)
    if args.github_output:
        with args.github_output.open("a") as output:
            output.write("images=" + json.dumps(digests, separators=(",", ":")) + "\n")


def health(args):
    """Every release tag must list both platforms, and each platform image must
    still exist; deleting a platform tag leaves a release tag that cannot pull."""
    check_tags([release_tag(release, variant, role)
                for release in args.releases for variant in VARIANTS for role in active_roles()])


def extension_health(args):
    """The same for every extension helper tag."""
    check_tags([extension_tag(extension) for extension in extensions().EXTENSIONS])


def check_tags(tags):
    problems = []
    checked = 0
    for tag in tags:
        try:
            index = raw_manifest(tag)
        except PublishError as error:
            problems.append(tag + ": tag unreadable: " + str(error).splitlines()[0])
            continue
        platforms = {(m.get("platform", {}).get("os"), m.get("platform", {}).get("architecture")): m.get("digest")
                     for m in index.get("manifests", [])}
        if set(platforms) != {("linux", a) for a in ARCHITECTURES}:
            problems.append(tag + ": tag does not list exactly linux/amd64 and linux/arm64")
            continue
        for (_, architecture), digest in sorted(platforms.items()):
            checked += 1
            try:
                raw_manifest(REGISTRY + "@" + digest)
            except PublishError:
                problems.append(tag + ": linux/" + architecture + " image " + digest + " is missing")
    for problem in problems:
        print(problem, file=sys.stderr)
    print("Published platform images: %d present, %d problems" % (checked - sum("is missing" in p for p in problems), len(problems)))
    require(not problems, "Published tags are incomplete")


def main():
    global CONTRACT
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
        command.add_argument("--contract", choices=("legacy", "roles-v2"), default="legacy")
        command.add_argument("--release", choices=tuple(releases.RELEASES), default=releases.DEFAULT)
        command.add_argument("--revision", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--output", type=Path, required=True)
    stage_helper = commands.add_parser("stage-extension", help="Upload a functionally checked extension helper")
    stage_helper.add_argument("--report", type=Path, required=True)
    stage_helper.add_argument("--extension", required=True)
    stage_helper.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    promote_helper = commands.add_parser("promote-extension", help="Promote every extension from both platforms")
    promote_helper.add_argument("--candidates", type=Path, required=True)
    promote_helper.add_argument("--github-output", type=Path)
    for command in (stage_helper, promote_helper):
        command.add_argument("--revision", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--output", type=Path, required=True)
    health_parser = commands.add_parser("health", help="Check that every published platform image still exists")
    health_parser.add_argument("--contract", choices=("legacy", "roles-v2"), default="legacy")
    health_parser.add_argument("--release", dest="releases", action="append", choices=tuple(releases.RELEASES))
    commands.add_parser("extension-health", help="Check that every published helper platform image still exists")
    args = parser.parse_args()
    CONTRACT = getattr(args, "contract", "legacy")
    if args.command == "health":
        args.releases = args.releases or list(releases.RELEASES)
        health(args)
        return
    if args.command == "extension-health":
        extension_health(args)
        return
    {"stage": stage, "promote": promote, "stage-extension": stage_extension,
     "promote-extension": promote_extension}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (PublishError, OSError, ValueError, KeyError) as error:
        print("Image publication failed: " + str(error), file=sys.stderr)
        sys.exit(1)
