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
CONTRACT = "roles"

def active_roles():
    return check.ROLES

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
    require(report.get("contract") == CONTRACT, "Image contract differs from publication namespace")
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
        require(candidate.get("contract") == CONTRACT, "Candidate contract differs")
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


def extension_targets(extension):
    """The targets every published helper must pass. The netem helper: the
    official image and the published essential role image of every release.
    The capture helper: the official image of every release, which is what its
    CI job checks."""
    if extension == "capture":
        return {value["official"] for value in releases.RELEASES.values()}
    return {reference for release, value in releases.RELEASES.items()
            for reference in (value["official"], releases.published_role(release, "official", "essential"))}


def validate_extension_report(report, extension, architecture):
    extension_check = extensions()
    require(extension in extension_check.PUBLISHED, "Unknown or unpublished extension " + repr(extension))
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
    targets = report.get("targets", {})
    require(extension_targets(extension) <= set(targets),
            "Every release target of the " + extension + " helper must be a checked target")
    for result in targets.values():
        require(result.get("status") == "passed" and result.get("cleanup", {}).get("status") == "passed"
                and not result["cleanup"].get("leftovers"), "Every target scenario must pass and clean up")
    {"netem": validate_netem_report, "capture": validate_capture_report}[extension](report, architecture)


def validate_netem_report(report, architecture):
    extension_check = extensions()
    kernel = report.get("kernel", {})
    require(kernel.get("status") == "passed" and kernel.get("cleanup", {}).get("status") == "passed",
            "The kernel check must pass and clean up")
    targets = extension_targets("netem")
    selectors = report.get("selectors", {})
    require(set(extension_check.SELECTORS) <= set(selectors), "Every selector profile of the contract must be checked")
    for profile in extension_check.SELECTORS:
        results = selectors[profile]
        require(targets <= set(results), "Each selector profile must be checked in every release target")
        for result in results.values():
            require(result.get("status") == "passed" and result.get("cleanup", {}).get("status") == "passed"
                    and not result["cleanup"].get("leftovers"), "Every selector scenario must pass and clean up")
    # The optional profiles that the published helper promises.
    for key, name in (("rate", "bandwidth"), ("packet_faults", "packet-fault")):
        profile = report.get(key, {})
        require(profile.get("status") == "passed" and profile.get("scope") == "functional",
                "The %s profile must pass functionally" % name)
        results = profile.get("targets", {})
        require(targets <= set(results), "The %s profile must be checked in every release target" % name)
        for result in results.values():
            require(result.get("status") == "passed" and result.get("cleanup", {}).get("status") == "passed"
                    and not result["cleanup"].get("leftovers"), "Every %s scenario must pass and clean up" % name)


def validate_capture_report(report, architecture):
    """The capture helper publishes only what its functional check showed on
    this platform: the quick refusal without NET_RAW, and in every release
    target, with this helper, every capture of CAPTURE_STOPS (including the
    IPv6 and the restart capture) and every negative control."""
    extension_check = extensions()
    image = report["image"]
    refusals = [entry for entry in image.get("checks", []) if entry.get("name") == "refused-without-net_raw"]
    require(len(refusals) == 1 and refusals[0].get("status") == "passed"
            and refusals[0].get("cleanup", {}).get("status") == "passed",
            "The quick refusal without NET_RAW must pass and clean up")
    for reference, result in report["targets"].items():
        require(result.get("helper_image_id") == image.get("image_id")
                and result.get("platform") == "linux/" + architecture,
                "Every capture scenario must use the checked helper on the requested platform: " + reference)
        captures = result.get("captures", {})
        require(set(captures) == set(extension_check.CAPTURE_STOPS)
                and all(capture.get("status") == "passed" and not capture.get("problems")
                        for capture in captures.values()),
                "Every capture must be recorded and pass: " + reference)
        controls = result.get("refusals", {})
        require(set(controls) == set(extension_check.CAPTURE_REFUSALS)
                and all(control.get("status") == "passed" and control.get("expected") == expected
                        and control.get("kind") == expected
                        for name, expected in extension_check.CAPTURE_REFUSALS.items()
                        for control in (controls[name],)),
                "Every negative control must be recorded and fail as required: " + reference)


# The leading bytes of classic pcap (both byte orders, micro- and nanosecond)
# and of pcapng.
CAPTURE_MAGICS = (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d",
                  b"\x0a\x0d\x0d\x0a")


def raw_captures(directory):
    """Files below a report directory that are, or are named as, raw captures.
    The capture checker keeps them out of its report directory, which CI
    uploads; a capture can hold payloads that no redaction sanitizes."""
    found = []
    for path in sorted(Path(directory).rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            head = handle.read(4)
        if ".pcap" in path.name or head in CAPTURE_MAGICS:
            found.append(str(path.relative_to(directory)))
    return found


def published_reference(extension, architecture, images):
    """The digest reference of one published helper platform, from a digest
    map (extension -> architecture -> sha256 digest) as promotion writes it."""
    images = json.loads(images)
    names = extensions().PUBLISHED
    require(isinstance(images, dict) and extension in names and extension in images,
            "Provide a digest map for the published extension " + repr(extension))
    require(set(images) <= set(names), "The digest map names an unpublished extension")
    for name, platforms in images.items():
        require(isinstance(platforms, dict) and set(platforms) == set(ARCHITECTURES),
                "Provide both architectures for " + name)
        require(all(isinstance(digest, str) and DIGEST.fullmatch(digest) for digest in platforms.values()),
                "Image references must be immutable sha256 digests")
    return REGISTRY + "@" + images[extension][architecture]


def check_artifacts(args):
    found = raw_captures(args.directory)
    for path in found:
        print("Raw capture in the artifact directory: " + path, file=sys.stderr)
    require(not found, "Raw captures must not be uploaded")


def stage_extension(args):
    report_bytes = args.report.read_bytes()
    report = json.loads(report_bytes)
    validate_extension_report(report, args.extension, args.architecture)
    if args.extension == "capture":
        require(not raw_captures(args.report.parent), "The report directory holds raw captures")
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
              "targets": sorted(report["targets"]), "environment": report.get("environment")}
    if args.extension == "netem":
        result.update(selectors=sorted(report["selectors"]), kernel=report["kernel"].get("kernel"))
    else:
        result["versions"] = checked.get("versions")
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


def collect_extension_candidates(directory, revision, run_id, extension):
    """The candidates of one extension: one per architecture, each uploaded by
    the job whose own functional check passed on that architecture."""
    result = {}
    for path in sorted(directory.rglob("extension-candidate.json")):
        candidate = json.loads(path.read_text())
        key = (candidate.get("extension"), candidate.get("architecture"))
        require(key not in result, "Duplicate extension candidate platform")
        require(candidate.get("status") == "passed" and candidate.get("revision") == revision
                and candidate.get("run_id") == run_id, "Candidates must pass in the same CI run and revision")
        entry = candidate.get("image", {})
        require(key[0] == extension, "Candidate of another extension: " + repr(key[0]))
        require(entry.get("extension") == key[0] and entry.get("architecture") == key[1],
                "Candidate extension/platform mismatch")
        require(all(isinstance(entry.get(field), str) and DIGEST.fullmatch(entry[field])
                    for field in ("digest", "config_digest", "image_id")), "Invalid candidate identity")
        result[key] = candidate
    require(set(result) == {(extension, a) for a in ARCHITECTURES}, "The extension must pass on both platforms")
    return result


def promote_extension(args):
    require(args.extension in extensions().PUBLISHED, "Unknown or unpublished extension " + repr(args.extension))
    candidates = collect_extension_candidates(args.candidates, args.revision, args.run_id, args.extension)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(verify_image, [candidate["image"] for candidate in candidates.values()]))
    names = (args.extension,)
    tags = [extension_tag(e, a) for e in names for a in (*ARCHITECTURES, None)]
    result = {"status": "promoting", "extension": args.extension, "revision": args.revision, "run_id": args.run_id,
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
    """Check both native platforms of the selected published role indexes."""
    promotion = getattr(args, 'promotion', None)
    if promotion:
        receipt = json.loads(promotion.read_text())
        require(receipt.get('status') == 'passed', 'Promotion must have completed')
        tags = list(receipt['indexes'])
    else:
        tags = [releases.published_role(release, variant, role)
                for release in args.releases for variant in VARIANTS for role in active_roles()]
    check_tags(tags)


def extension_health(args):
    """The same for every extension helper tag."""
    check_tags([extension_tag(extension) for extension in extensions().PUBLISHED])


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
    stage_helper = commands.add_parser("stage-extension", help="Upload a functionally checked extension helper")
    stage_helper.add_argument("--report", type=Path, required=True)
    stage_helper.add_argument("--extension", required=True)
    stage_helper.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    promote_helper = commands.add_parser("promote-extension", help="Promote one extension from both platforms")
    promote_helper.add_argument("--extension", required=True)
    promote_helper.add_argument("--candidates", type=Path, required=True)
    promote_helper.add_argument("--github-output", type=Path)
    for command in (stage_helper, promote_helper):
        command.add_argument("--revision", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--output", type=Path, required=True)
    health_parser = commands.add_parser("health", help="Check that every published platform image still exists")
    health_parser.add_argument("--promotion", type=Path, help="Check the tags of this completed promotion")
    health_parser.add_argument("--release", dest="releases", action="append", choices=tuple(releases.RELEASES))
    commands.add_parser("extension-health", help="Check that every published helper platform image still exists")
    reference = commands.add_parser("published-reference", help="Print one platform of a promoted helper by digest")
    reference.add_argument("--extension", required=True)
    reference.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    reference.add_argument("--images", required=True)
    artifacts = commands.add_parser("check-artifacts", help="Fail when a report directory holds raw captures")
    artifacts.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "health":
        args.releases = args.releases or list(releases.RELEASES)
        health(args)
        return
    if args.command == "extension-health":
        extension_health(args)
        return
    if args.command == "published-reference":
        print(published_reference(args.extension, args.architecture, args.images))
        return
    if args.command == "check-artifacts":
        check_artifacts(args)
        return
    {"stage": stage, "promote": promote, "stage-extension": stage_extension,
     "promote-extension": promote_extension}[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except (PublishError, OSError, ValueError, KeyError) as error:
        print("Image publication failed: " + str(error), file=sys.stderr)
        sys.exit(1)
