#!/usr/bin/env python3
"""Audit retained capture publication artifacts, without Docker or network I/O.

Qualification means consistency of the supplied API records, ZIP bytes and OCI
blobs. It is not a signature verifier, a fresh registry check or a native run.
"""
import argparse
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile
import zlib

import publish

REPOSITORY = "JSYoo5B/ceph-testcontainers-images"
MAX_JSON = 8 * 1024 * 1024
MAX_ZIP = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_MEMBERS = 128
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
GUARD = "Keep raw captures out of the artifacts"
UPLOAD = "Run actions/upload-artifact@v4"


class Invalid(ValueError):
    pass


class Missing(Invalid):
    pass


def need(condition, message):
    if not condition:
        raise Invalid(message)


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def loads(data):
    need(len(data) <= MAX_JSON, "JSON input exceeds 8 MiB")
    return json.loads(data, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(Invalid("nonfinite JSON: " + value)))


def timestamp(value):
    need(isinstance(value, str), "missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    need(result.tzinfo is not None, "timestamp has no timezone")
    return result


def complete(value, label):
    if value.get("conclusion") in (None, "skipped", "cancelled") or value.get("status") != "completed":
        raise Missing(label + " did not execute successfully")
    need(value.get("conclusion") == "success", label + " failed")


class Audit:
    def __init__(self, directory, revision, run_id):
        need(re.fullmatch(r"[0-9a-f]{40}", revision), "expected revision must be a full SHA")
        need(re.fullmatch(r"[1-9][0-9]*-[1-9][0-9]*", run_id), "run identity must include run and attempt")
        self.directory = Path(directory)
        self.revision, self.run_id = revision, run_id
        self.number, self.attempt = map(int, run_id.split("-"))
        self.errors, self.missing, self.files, self.reports = [], [], {}, []
        self.run, self.jobs, self.artifacts = {}, [], []
        self.candidate_jobs, self.originals, self.promoted_at = {}, {}, None

    def attempt_check(self, label, check):
        try:
            return check()
        except Missing as error:
            self.missing.append((label + ": " + str(error))[:1024])
        except (Invalid, publish.PublishError, OSError, KeyError, TypeError, ValueError,
                AttributeError, RecursionError, zipfile.BadZipFile) as error:
            self.errors.append((label + ": " + str(error))[:1024])
        return None

    def read(self, name, limit=MAX_JSON):
        path = self.directory / name
        if not path.exists():
            raise Missing("missing " + name)
        need(path.is_file() and not path.is_symlink(), "input is not a regular file: " + name)
        need(path.stat().st_size <= limit, "oversized input: " + name)
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        need(len(data) <= limit, "oversized input: " + name)
        self.files[name] = sha(data)
        return data

    def metadata(self):
        self.run = loads(self.read("run.json"))
        jobs, artifacts = loads(self.read("jobs.json")), loads(self.read("artifacts.json"))
        self.jobs, self.artifacts = jobs["jobs"], artifacts["artifacts"]
        need(jobs["total_count"] == len(self.jobs), "jobs input is paginated/incomplete")
        need(artifacts["total_count"] == len(self.artifacts), "artifact input is paginated/incomplete")
        need(len(self.jobs) <= 128 and len(self.artifacts) <= 128, "too many metadata entries")
        need(self.run.get("id") == self.number and self.run.get("run_attempt") == self.attempt,
             "unexpected run or attempt")
        need(self.run.get("head_sha") == self.revision, "unexpected run source")
        need(self.run.get("repository", {}).get("full_name") == REPOSITORY, "unexpected repository")
        need(self.run.get("head_branch") == "main", "publication requires the default branch")
        if self.run.get("status") != "completed":
            raise Missing("workflow is not completed")
        need(self.run.get("conclusion") in ("success", "failure"), "workflow result is unavailable")
        if self.run.get("event") != "workflow_dispatch":
            raise Missing("run is not the manual publication invocation")

    def job(self, name, required_step=None, guard=False):
        matches = [value for value in self.jobs if value.get("name") == name]
        if not matches:
            raise Missing("missing job " + name)
        need(len(matches) == 1, "duplicate job " + name)
        job = matches[0]
        need(job.get("run_id") == self.number and job.get("run_attempt") == self.attempt
             and job.get("head_sha") == self.revision, "job has another source/run/attempt")
        complete(job, name)
        for step_name in ([required_step] if required_step else []) + ([GUARD] if guard else []):
            steps = [step for step in job.get("steps", []) if step.get("name") == step_name]
            need(len(steps) == 1, "missing/duplicate step " + step_name)
            complete(steps[0], step_name)
        uploads = [step for step in job.get("steps", []) if step.get("name") == UPLOAD
                   and step.get("status") == "completed" and step.get("conclusion") == "success"]
        need(uploads, "no successful artifact upload step")
        return job

    def archive(self, name, job):
        matches = [item for item in self.artifacts if item.get("name") == name]
        if not matches:
            raise Missing("missing artifact " + name)
        need(len(matches) == 1, "duplicate artifact " + name)
        meta = matches[0]
        need(type(meta.get("id")) is int and meta["id"] > 0, "invalid artifact ID")
        need(meta.get("expired") is False, "artifact is expired or expiration is unknown")
        origin = meta.get("workflow_run", {})
        need(origin.get("id") == self.number and origin.get("head_sha") == self.revision,
             "artifact belongs to another run/source")
        need(origin.get("repository_id") == self.run.get("repository", {}).get("id"), "artifact repository mismatch")
        created = timestamp(meta.get("created_at"))
        need(timestamp(job.get("started_at")) <= created <= timestamp(job.get("completed_at")),
             "artifact was not created during the selected producer attempt")
        data = self.read(str(meta["id"]) + ".zip", MAX_ZIP)
        need(DIGEST.fullmatch(meta.get("digest", "")) and sha(data) == meta["digest"], "artifact ZIP checksum mismatch")
        need(len(data) == meta.get("size_in_bytes"), "artifact ZIP size mismatch")
        files = {}
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            need(len(members) <= MAX_MEMBERS, "too many ZIP members")
            need(sum(info.file_size for info in members) <= MAX_EXPANDED, "expanded ZIP exceeds 64 MiB")
            for info in members:
                path = PurePosixPath(info.filename.rstrip("/"))
                need(str(path) == info.filename.rstrip("/") and not path.is_absolute()
                     and ".." not in path.parts and "\\" not in info.filename, "unsafe ZIP path")
                need(not stat.S_ISLNK(info.external_attr >> 16) and not info.flag_bits & 1, "linked/encrypted ZIP member")
                if info.is_dir():
                    continue
                need(info.filename not in files and info.file_size <= MAX_JSON, "duplicate/oversized ZIP member")
                try:
                    with archive.open(info) as member:
                        payload = member.read(MAX_JSON + 1)
                except (NotImplementedError, zlib.error) as error:
                    # Keep malformed input failures inside the per-artifact audit
                    # boundary without exposing decompressor/member input text.
                    raise Invalid("unsupported or malformed ZIP compression") from error
                need(len(payload) == info.file_size and len(payload) <= MAX_JSON, "oversized/incomplete ZIP member")
                need(".pcap" not in path.name.lower() and payload[:4] not in publish.CAPTURE_MAGICS,
                     "raw capture in retained artifact")
                files[info.filename] = payload
        return meta, files

    @staticmethod
    def member(files, name):
        values = [data for path, data in files.items() if PurePosixPath(path).name == name]
        need(len(values) == 1, "missing/duplicate artifact member " + name)
        return values[0]

    def report(self, artifact_name, job, architecture):
        meta, files = self.archive(artifact_name, job)
        data = self.member(files, "extension-report.json")
        report = loads(data)
        publish.validate_extension_report(report, "capture", architecture)
        need(set(report["targets"]) == publish.extension_targets("capture"), "unexpected publication target set")
        need(report["image"].get("checks") and all(check.get("status") == "passed" for check in report["image"]["checks"]),
             "quick checks are missing or did not pass")
        need(timestamp(job["started_at"]) <= timestamp(report.get("started_at"))
             <= timestamp(report.get("finished_at")) <= timestamp(job["completed_at"]), "report is outside producer attempt")
        need(DIGEST.fullmatch(report["image"].get("image_id", "")), "invalid tested helper ID")
        targets = {}
        for reference, result in report["targets"].items():
            need(DIGEST.fullmatch(result.get("image_id", "")), "missing immutable target image ID")
            targets[reference] = {"image_id": result["image_id"], "partial_observations": {
                kind: {"end": result["captures"][kind].get("end"), "cut_tail": result["captures"][kind].get("cut_tail")}
                for kind in ("abrupt", "target_loss", "restart")}}
        self.reports.append({"artifact": artifact_name, "artifact_id": meta["id"], "zip_sha256": meta["digest"],
                             "report_sha256": sha(data), "platform": "linux/" + architecture,
                             "helper_image_id": report["image"]["image_id"], "targets": targets})
        return report, data, files

    def candidate(self, arch, original):
        job = self.job("capture-extension (" + arch + ")", "Upload the exact tested helper", True)
        self.candidate_jobs[arch] = job
        _, files = self.archive("extension-candidate-capture-" + arch, job)
        candidate = loads(self.member(files, "extension-candidate.json"))
        need(candidate.get("schema") == 1 and candidate.get("status") == "passed"
             and candidate.get("extension") == "capture" and candidate.get("contract") == "1", "invalid capture candidate")
        need(candidate.get("architecture") == arch and candidate.get("revision") == self.revision
             and candidate.get("run_id") == self.run_id, "candidate source/run/attempt/platform mismatch")
        report, data, _ = original
        need(candidate.get("report_sha256") == hashlib.sha256(data).hexdigest(), "candidate report checksum mismatch")
        need(candidate.get("targets") == sorted(report["targets"]), "candidate target set mismatch")
        image = candidate["image"]
        need(image.get("extension") == "capture" and image.get("architecture") == arch
             and image.get("image_id") == report["image"]["image_id"], "candidate tested image mismatch")
        need(all(DIGEST.fullmatch(image.get(key, "")) for key in ("image_id", "digest", "config_digest")), "invalid candidate digest")
        need(image.get("rootfs_diff_ids") and all(DIGEST.fullmatch(value) for value in image["rootfs_diff_ids"]), "invalid candidate layers")
        return candidate

    def promotion(self, candidates):
        job = self.job("promote-capture-extension", "Overwrite the helper tags with the tested digests")
        _, files = self.archive("extension-capture-promotion", job)
        value = loads(self.member(files, "promotion.json"))
        need(value.get("status") == "passed" and value.get("extension") == "capture"
             and value.get("revision") == self.revision and value.get("run_id") == self.run_id, "promotion provenance mismatch")
        need(all(timestamp(producer["completed_at"]) <= timestamp(job["started_at"])
                 for producer in self.candidate_jobs.values()), "promotion preceded a candidate job")
        self.promoted_at = timestamp(job["completed_at"])
        expected = {arch: candidate["image"]["digest"] for arch, candidate in candidates.items()}
        need(value.get("images") == {"capture": expected}, "promotion platform digest map mismatch")
        for arch, candidate in candidates.items():
            need(value.get("platforms", {}).get(publish.extension_tag("capture", arch)) == candidate["image"], "promotion differs from candidate")
        index_data = self.read("index.json")
        index = loads(index_data)
        need(index.get("schemaVersion") == 2, "unexpected index schema")
        descriptor = value.get("indexes", {}).get(publish.extension_tag("capture"), {})
        need(descriptor.get("digest") == sha(index_data), "index bytes differ from promoted digest")
        observed = [(item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"), item.get("digest"))
                    for item in index.get("manifests", [])]
        need(sorted(observed) == sorted(("linux", arch, digest) for arch, digest in expected.items()), "published index platform mismatch")
        for arch, candidate in candidates.items():
            image = candidate["image"]
            raw_manifest, raw_config = self.read("manifest-" + arch + ".json"), self.read("config-" + arch + ".json")
            need(sha(raw_manifest) == image["digest"] and sha(raw_config) == image["config_digest"], "platform manifest/config digest mismatch")
            manifest, config = loads(raw_manifest), loads(raw_config)
            need(manifest.get("config", {}).get("digest") == image["config_digest"], "platform config link mismatch")
            need(config.get("os") == "linux" and config.get("architecture") == arch
                 and config.get("rootfs", {}).get("diff_ids") == image["rootfs_diff_ids"], "platform config/layer mismatch")
            need(config.get("config", {}).get("Labels", {}).get("io.ceph-testcontainers.extension") == "capture", "platform config extension mismatch")
            need(config.get("config", {}).get("Labels", {}).get("io.ceph-testcontainers.extension.contract") == "1", "platform config contract mismatch")
        return value

    def published(self, arch, candidate, promotion):
        job = self.job("verify-published-capture-extension / published-capture-extension (" + arch + ")",
                       "Pull the published digest and capture in Ceph targets' namespaces", True)
        report, _, files = self.report("published-capture-extension-" + arch + "-check", job, arch)
        need(self.promoted_at <= timestamp(job["started_at"]), "published recheck preceded promotion")
        need(loads(self.member(files, "published-extension-digests.json")) == promotion["images"], "published recheck digest map mismatch")
        image = candidate["image"]
        need(report["image"].get("reference") == publish.REGISTRY + "@" + image["digest"], "published recheck used a mutable/wrong reference")
        need(report["image"]["image_id"] in {image["image_id"], image["digest"], image["config_digest"]}, "published recheck helper identity mismatch")
        need({key: result["image_id"] for key, result in report["targets"].items()}
             == {key: result["image_id"] for key, result in self.originals[arch][0]["targets"].items()}, "published target identities changed")
        return {"platform": "linux/" + arch, "manifest_digest": image["digest"], "config_digest": image["config_digest"]}

    def run_audit(self):
        self.attempt_check("metadata", self.metadata)
        originals, candidates, platforms = {}, {}, []
        for arch in publish.ARCHITECTURES:
            originals[arch] = self.attempt_check("original " + arch, lambda a=arch: self.report(
                "extension-capture-" + a + "-check", self.job("capture-extension (" + a + ")", guard=True), a))
            self.originals = originals
            if originals[arch] is not None:
                candidate = self.attempt_check("candidate " + arch, lambda a=arch: self.candidate(a, originals[a]))
                if candidate is not None:
                    candidates[arch] = candidate
        promotion = self.attempt_check("promotion", lambda: self.promotion(candidates)) if len(candidates) == 2 else None
        for arch, candidate in candidates.items():
            if promotion is not None:
                result = self.attempt_check("published " + arch, lambda a=arch, c=candidate: self.published(a, c, promotion))
                if result is not None:
                    platforms.append(result)
        if len(platforms) != 2 and not self.errors and not self.missing:
            self.missing.append("publication chain lacks both platform rechecks")
        status = "failed" if self.errors else "incomplete" if self.missing else "qualified"
        return {"schema": "capture-publication-qualification/v1", "status": status,
                "evidence_class": "offline-artifact-consistency", "revision": self.revision, "run_id": self.run_id,
                "verified_reports": self.reports, "qualified_platforms": platforms,
                "inputs": self.files, "errors": self.errors, "missing": self.missing,
                "live_registry_checked": False, "consumer_protocol_claimed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-id", required=True, help="run ID and attempt, for example 123-1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = Audit(args.directory, args.revision, args.run_id).run_audit()
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0 if report["status"] == "qualified" else 1
    except (Invalid, OSError, ValueError) as error:
        print("capture qualification: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
