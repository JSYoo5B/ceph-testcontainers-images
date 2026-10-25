"""Synthetic artifact joins, separate from native publication evidence."""
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import capture_qualification as q


def encoded(value):
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def digest(value):
    return "sha256:" + ("%064x" % value)


def time(minute, second=0):
    return "2026-01-01T00:%02d:%02d+00:00" % (minute, second)


class CaptureQualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.revision, self.run_id = "a" * 40, "123-2"
        self.run = {"id": 123, "run_attempt": 2, "head_sha": self.revision, "head_branch": "main",
                    "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
                    "repository": {"id": 42, "full_name": q.REPOSITORY}}
        self.jobs, self.artifacts, self.payloads, self.candidates = [], [], {}, {}
        self.published_jobs, self.original_jobs = {}, {}
        self.index = {"schemaVersion": 2, "manifests": []}
        for number, arch in enumerate(q.publish.ARCHITECTURES):
            config = {"architecture": arch, "os": "linux", "rootfs": {"diff_ids": [digest(100 + number)]},
                      "config": {"Labels": {"io.ceph-testcontainers.extension": "capture",
                                            "io.ceph-testcontainers.extension.contract": "1"}}}
            config_data = encoded(config)
            manifest_data = encoded({"schemaVersion": 2, "config": {"digest": q.sha(config_data)}})
            (self.root / ("config-" + arch + ".json")).write_bytes(config_data)
            (self.root / ("manifest-" + arch + ".json")).write_bytes(manifest_data)
            image = {"extension": "capture", "architecture": arch, "image_id": q.sha(config_data),
                     "config_digest": q.sha(config_data), "digest": q.sha(manifest_data),
                     "rootfs_diff_ids": config["rootfs"]["diff_ids"]}
            report = self.report(arch, image["image_id"], 0)
            report_data = encoded(report)
            original = self.job("capture-extension (" + arch + ")", 0,
                                [q.GUARD, "Upload the exact tested helper", q.UPLOAD, q.UPLOAD])
            self.original_jobs[arch] = original
            self.artifact("extension-capture-" + arch + "-check", {"extension-report.json": report_data}, original)
            candidate = {"schema": 1, "status": "passed", "extension": "capture", "contract": "1",
                         "architecture": arch, "revision": self.revision, "run_id": self.run_id,
                         "report_sha256": hashlib.sha256(report_data).hexdigest(), "targets": sorted(report["targets"]), "image": image}
            self.candidates[arch] = candidate
            self.artifact("extension-candidate-capture-" + arch, {"extension-candidate.json": encoded(candidate)}, original)
            self.index["manifests"].append({"platform": {"os": "linux", "architecture": arch}, "digest": image["digest"]})
        index_data = encoded(self.index)
        (self.root / "index.json").write_bytes(index_data)
        self.promotion = {"status": "passed", "extension": "capture", "revision": self.revision, "run_id": self.run_id,
                          "images": {"capture": {arch: value["image"]["digest"] for arch, value in self.candidates.items()}},
                          "platforms": {q.publish.extension_tag("capture", arch): value["image"] for arch, value in self.candidates.items()},
                          "indexes": {q.publish.extension_tag("capture"): {"digest": q.sha(index_data)}}}
        promote = self.job("promote-capture-extension", 2, ["Overwrite the helper tags with the tested digests", q.UPLOAD])
        self.artifact("extension-capture-promotion", {"promotion.json": encoded(self.promotion)}, promote)
        for arch, candidate in self.candidates.items():
            job = self.job("verify-published-capture-extension / published-capture-extension (" + arch + ")", 4,
                           ["Pull the published digest and capture in Ceph targets' namespaces", q.GUARD, q.UPLOAD])
            self.published_jobs[arch] = job
            report = self.report(arch, candidate["image"]["image_id"], 4)
            report["image"]["reference"] = q.publish.REGISTRY + "@" + candidate["image"]["digest"]
            self.artifact("published-capture-extension-" + arch + "-check",
                          {"extension-report.json": encoded(report), "published-extension-digests.json": encoded(self.promotion["images"])}, job)
        self.write_metadata()

    def job(self, name, minute, steps):
        job = {"name": name, "run_id": 123, "run_attempt": 2, "head_sha": self.revision,
               "status": "completed", "conclusion": "success", "started_at": time(minute), "completed_at": time(minute, 40),
               "steps": [{"name": name, "status": "completed", "conclusion": "success"} for name in steps]}
        self.jobs.append(job)
        return job

    def report(self, arch, image_id, minute):
        targets = {}
        for number, reference in enumerate(sorted(q.publish.extension_targets("capture"))):
            targets[reference] = {"status": "passed", "image_id": digest(200 + number), "helper_image_id": image_id,
                                  "platform": "linux/" + arch, "cleanup": {"status": "passed", "leftovers": []},
                                  "captures": {kind: {"status": "passed", "problems": [], "end": "exited", "cut_tail": False}
                                               for kind in q.publish.extensions().CAPTURE_STOPS},
                                  "refusals": {name: {"status": "passed", "kind": value, "expected": value}
                                               for name, value in q.publish.extensions().CAPTURE_REFUSALS.items()}}
        return {"schema": 1, "extension": "capture", "contract": "1", "status": "passed", "level": "functional",
                "started_at": time(minute, 2), "finished_at": time(minute, 20),
                "checker_sha256": q.publish.extensions().checker_sha256(), "targets": targets,
                "image": {"status": "passed", "reference": "synthetic-helper", "image_id": image_id, "platform": "linux/" + arch,
                          "labels": {"io.ceph-testcontainers.extension": "capture", "io.ceph-testcontainers.extension.contract": "1"},
                          "cleanup": {"status": "passed"},
                          "checks": [{"name": "refused-without-net_raw", "status": "passed", "cleanup": {"status": "passed"}}]}}

    def artifact(self, name, files, job):
        number = len(self.artifacts) + 1
        meta = {"id": number, "name": name, "expired": False,
                "workflow_run": {"id": 123, "head_sha": self.revision, "repository_id": 42},
                "created_at": time(int(job["started_at"][14:16]), 30)}
        self.artifacts.append(meta)
        self.payloads[name] = files
        self.repack(name)

    def repack(self, name):
        meta = next(item for item in self.artifacts if item["name"] == name)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, data in self.payloads[name].items():
                archive.writestr(path, data)
        data = stream.getvalue()
        (self.root / (str(meta["id"]) + ".zip")).write_bytes(data)
        meta.update(digest=q.sha(data), size_in_bytes=len(data))

    def write_metadata(self):
        for name, value in (("run.json", self.run), ("jobs.json", {"total_count": len(self.jobs), "jobs": self.jobs}),
                            ("artifacts.json", {"total_count": len(self.artifacts), "artifacts": self.artifacts})):
            (self.root / name).write_bytes(encoded(value))

    def audit(self):
        self.write_metadata()
        return q.Audit(self.root, self.revision, self.run_id).run_audit()

    def reject(self):
        report = self.audit()
        self.assertNotEqual(report["status"], "qualified", report)
        return report

    def alter(self, artifact, member, edit):
        value = q.loads(self.payloads[artifact][member])
        edit(value)
        self.payloads[artifact][member] = encoded(value)
        self.repack(artifact)

    def test_complete_synthetic_chain(self):
        report = self.audit()
        self.assertEqual(report["status"], "qualified", report)
        self.assertEqual(len(report["verified_reports"]), 4)
        self.assertEqual(len(report["qualified_platforms"]), 2)
        self.assertFalse(report["live_registry_checked"])
        self.assertFalse(report["consumer_protocol_claimed"])

    def test_unrelated_job_failure_does_not_replace_capture_job_evidence(self):
        self.run["conclusion"] = "failure"
        self.assertEqual(self.audit()["status"], "qualified")

    def test_nonpublication_run_retains_only_partial_observations(self):
        self.run["event"] = "push"
        self.artifacts = [a for a in self.artifacts if a["name"].startswith("extension-capture-") and a["name"].endswith("-check")]
        for job in self.jobs:
            if not job["name"].startswith("capture-extension"):
                job["conclusion"] = "skipped"
        report = self.reject()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(len(report["verified_reports"]), 2)

    def test_run_source_attempt_and_repository(self):
        for key, value in (("head_sha", "b" * 40), ("run_attempt", 1), ("id", 124), ("head_branch", "other")):
            with self.subTest(key=key):
                saved = self.run[key]; self.run[key] = value; self.reject(); self.run[key] = saved
        self.run["repository"]["full_name"] = "another/repository"
        self.reject()

    def test_job_skipped_failed_old_source_and_attempt(self):
        job = self.published_jobs["amd64"]
        for key, value in (("conclusion", "skipped"), ("conclusion", "failure"), ("head_sha", "b" * 40), ("run_attempt", 1)):
            with self.subTest(key=key, value=value):
                saved = job[key]; job[key] = value; self.reject(); job[key] = saved

    def test_guard_and_upload_cannot_be_skipped(self):
        for wanted in (q.GUARD, q.UPLOAD):
            step = next(step for step in self.published_jobs["amd64"]["steps"] if step["name"] == wanted)
            step["conclusion"] = "skipped"; self.reject(); step["conclusion"] = "success"

    def test_candidate_checksum_source_and_attempt(self):
        for key, value in (("report_sha256", "0" * 64), ("revision", "b" * 40), ("run_id", "123-1"), ("architecture", "arm64")):
            with self.subTest(key=key):
                name = "extension-candidate-capture-amd64"
                saved = self.payloads[name]["extension-candidate.json"]
                self.alter(name, "extension-candidate.json", lambda candidate: candidate.update({key: value}))
                self.reject(); self.payloads[name]["extension-candidate.json"] = saved; self.repack(name)

    def test_missing_and_duplicate_artifact(self):
        saved = self.artifacts.pop(); self.reject(); self.artifacts.append(saved)
        self.artifacts.append(copy.deepcopy(saved)); self.reject()

    def test_expired_artifact_and_wrong_producer_time(self):
        meta = self.artifacts[0]
        meta["expired"] = True; self.reject(); meta["expired"] = False
        meta["created_at"] = time(9); self.reject()

    def test_zip_checksum(self):
        meta = self.artifacts[0]
        (self.root / (str(meta["id"]) + ".zip")).write_bytes(b"changed")
        self.reject()

    def test_raw_capture_name_magic_and_unsafe_paths(self):
        name = "extension-capture-amd64-check"
        for path, value in (("trace.PCAP", b"payload"), ("result.bin", q.publish.CAPTURE_MAGICS[0]),
                            ("../outside.json", b"{}"), ("/absolute.json", b"{}")):
            with self.subTest(path=path):
                self.payloads[name][path] = value; self.repack(name); self.reject()
                del self.payloads[name][path]; self.repack(name)

    def test_checker_cleanup_native_refusal_and_parent_report_times(self):
        name = "published-capture-extension-amd64-check"
        original = self.payloads[name]["extension-report.json"]
        for edit in (lambda r: r.update(checker_sha256="0" * 64),
                     lambda r: r["image"]["cleanup"].update(status="failed"),
                     lambda r: r.update(started_at=time(0)),
                     lambda r: next(iter(r["targets"].values()))["refusals"]["without_net_raw"].update(kind="other")):
            self.alter(name, "extension-report.json", edit); self.reject()
            self.payloads[name]["extension-report.json"] = original; self.repack(name)

    def test_promotion_and_published_digest_identity(self):
        name = "extension-capture-promotion"
        self.alter(name, "promotion.json", lambda value: value["images"]["capture"].update(amd64=digest(999)))
        self.reject()

    def test_index_manifest_config_checksum_and_platform(self):
        for name in ("index.json", "manifest-amd64.json", "config-arm64.json"):
            with self.subTest(name=name):
                path = self.root / name; original = path.read_bytes(); path.write_bytes(b"{}")
                self.reject(); path.write_bytes(original)

    def test_published_reference_and_target_identity(self):
        name = "published-capture-extension-amd64-check"
        original = self.payloads[name]["extension-report.json"]
        for edit in (lambda value: value["image"].update(reference=q.publish.REGISTRY + ":latest"),
                     lambda value: value["image"].update(image_id=digest(999)),
                     lambda value: next(iter(value["targets"].values())).update(image_id=digest(999))):
            self.alter(name, "extension-report.json", edit); self.reject()
            self.payloads[name]["extension-report.json"] = original; self.repack(name)

    def test_metadata_pagination_and_duplicate_json_key(self):
        self.write_metadata()
        path = self.root / "artifacts.json"
        value = q.loads(path.read_bytes()); value["total_count"] += 1; path.write_bytes(encoded(value))
        self.assertNotEqual(q.Audit(self.root, self.revision, self.run_id).run_audit()["status"], "qualified")
        with self.assertRaises(q.Invalid):
            q.loads(b'{"status":"passed","status":"failed"}')

    def test_byte_member_and_expanded_limits(self):
        for name, value in (("MAX_JSON", 64), ("MAX_ZIP", 64), ("MAX_EXPANDED", 64), ("MAX_MEMBERS", 0)):
            with self.subTest(limit=name), mock.patch.object(q, name, value):
                self.reject()

    def test_symlink_zip_member(self):
        meta = self.artifacts[0]
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name, payload in self.payloads[meta["name"]].items():
                archive.writestr(name, payload)
            linked = zipfile.ZipInfo("linked.json")
            linked.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(linked, "extension-report.json")
        data = stream.getvalue()
        (self.root / (str(meta["id"]) + ".zip")).write_bytes(data)
        meta.update(digest=q.sha(data), size_in_bytes=len(data))
        report = self.reject()
        self.assertTrue(any("linked/encrypted" in error for error in report["errors"]))

    def test_promotion_and_published_temporal_order(self):
        job = next(job for job in self.jobs if job["name"] == "promote-capture-extension")
        job["started_at"] = time(0)
        self.reject()

    def test_published_digest_map_requires_both_platforms(self):
        name = "published-capture-extension-amd64-check"
        self.alter(name, "published-extension-digests.json", lambda value: value["capture"].pop("arm64"))
        self.reject()


if __name__ == "__main__":
    unittest.main()
