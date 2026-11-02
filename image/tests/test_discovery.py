"""Command/evidence refusal controls; the recorder substitutes external CLIs.

The fake only supplies the existing tools' report protocol. It does not model
Docker, image contents, package installation or native Ceph compatibility.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import signal
import subprocess
import tempfile
import unittest
from unittest import mock
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discovery


class Commands:
    def __init__(self, fail=(), publish_fail=False, preexisting=False, cpu=True, bad_checks=False):
        self.calls, self.local = [], set()
        self.fail, self.publish_fail, self.preexisting, self.cpu = fail, publish_fail, preexisting, cpu
        self.bad_checks = bad_checks

    def __call__(self, command, log, timeout, env=None, check_result=True):
        self.calls.append((command, env, timeout))
        log.write_text("")
        if command[0].startswith("/lib64/"):
            log.write_text("x86-64-v3 (supported, searched)" if self.cpu else "x86-64-v2 (supported, searched)")
        elif command[:3] == ["docker", "image", "ls"]:
            prefix = next(arg.split("=", 1)[1][:-1] for arg in command if arg.startswith("reference="))
            log.write_text("\n".join(tag for tag in self.local if tag.startswith(prefix)))
        elif command[:3] == ["docker", "image", "inspect"]:
            log.write_text("sha256:" + "1" * 64 if self.preexisting else "No such image")
            return 0 if self.preexisting else 1
        elif command[:2] == ["docker", "pull"]:
            if any(name in command[-1] for name in self.fail):
                raise discovery.CommandError("timeout", command)
        elif len(command) > 1 and command[1].endswith("/roles/build.py"):
            directory = Path(command[command.index("--output-dir") + 1])
            release = directory.parent.name
            prefix = command[command.index("--tag") + 1]
            roles = {role: "ceph-discovery:" + prefix + "-" + role for role in discovery.check.ROLES}
            self.local.update(roles.values())
            directory.mkdir(parents=True)
            (directory / "image-check").mkdir()
            architecture = command[command.index("--platform") + 1]
            (directory / "build-report.json").write_text(json.dumps({"status": "passed", "tags": roles,
                "ceph_version": "ceph version " + release + " (fixture)",
                "checks": {"smoke": "passed", "image_check": "passed"}}))
            images = {role: {"status": "passed", "platform": architecture,
                            "cleanup": {"status": "passed"}, "versions": {"ceph": {"release": release}}}
                      for role in roles}
            (directory / "image-check/check-report.json").write_text(json.dumps({
                "status": "failed" if self.bad_checks else "passed", "level": "quick",
                "preflight": "passed", "contract": "roles", "images": images}))
            if release in self.fail:
                raise discovery.CommandError("command_failed", command, 1)
        elif "stage-discovery" in command:
            if self.publish_fail:
                raise discovery.CommandError("command_failed", command, 1)
            fields = {field: command[command.index("--" + field.replace("_", "-")) + 1]
                      for field in ("release", "variant", "architecture", "revision", "run_id")}
            images = {role: {"candidate_tag": discovery.REGISTRY + ":ci-discovery-" + fields["run_id"] + "-"
                       + fields["variant"] + "-" + fields["release"] + "-" + role + "-linux-" + fields["architecture"]}
                      for role in discovery.check.ROLES}
            self.local.update(value["candidate_tag"] for value in images.values())
            Path(command[command.index("--output") + 1]).write_text(json.dumps({**fields, "status": "passed", "images": images}))
        elif command[:3] == ["docker", "image", "rm"]:
            self.local.discard(command[-1])
        return 0


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def args(self, *extra):
        return ["build", "--releases", "19.2.0", "--variant", "official", "--architecture", "amd64",
                "--revision", "a" * 40, "--run-id", "123-1", "--output", str(self.root / "run"), *extra]

    def execute(self, commands, *extra):
        return discovery.main(self.args(*extra), commands)

    def report(self, version="19.2.0"):
        return json.loads((self.root / "run" / version / "report.json").read_text())

    def test_plan_packs_fourteen_versions_into_six_native_jobs(self):
        output = self.root / "github-output"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(discovery.main(["plan", "--github-output", str(output)]), 0)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(len(json.loads(values["releases"])), 14)
        entries = json.loads(values["matrix"])["include"]
        self.assertEqual(len(entries), 6)
        self.assertEqual({row["runner"] for row in entries}, {"ubuntu-24.04", "ubuntu-24.04-arm"})

    def test_old_debian_backport_version_and_pinned_base_reach_build(self):
        prepare, assemble, roles, source = discovery.build_commands("19.2.0", "debian", "amd64", self.root)
        self.assertIn("CEPH_PACKAGE_VERSION=19.2.0-1~bpo12+1", prepare)
        self.assertTrue(any(arg.startswith("BASE_IMAGE=debian:bookworm-slim@sha256:") for arg in prepare))
        self.assertEqual(source, "ceph-discovery:discovery-debian-19.2.0-source")
        self.assertIn("deb", assemble)
        self.assertEqual(assemble[assemble.index("--check") + 1], "quick")
        self.assertNotIn("--keep-context", assemble)
        self.assertEqual(len(roles), 6)

    def test_absent_architecture_records_unavailable_without_docker(self):
        commands = Commands()
        self.assertEqual(self.execute(commands, "--variant", "debian", "--architecture", "arm64"), 0)
        self.assertEqual(commands.calls, [])
        self.assertEqual(self.report()["status"], "unavailable")
        self.assertIn("absent", self.report()["reason"])

    def test_success_does_not_publish_implicitly_and_keeps_unpublished_images(self):
        commands = Commands()
        self.assertEqual(self.execute(commands), 0)
        self.assertFalse(any("stage-discovery" in command for command, _, _ in commands.calls))
        self.assertFalse(any(command[:3] == ["docker", "image", "rm"] for command, _, _ in commands.calls))
        self.assertEqual(self.report()["publication"], "not_requested")
        self.assertEqual(self.report()["image_cleanup"], "retained_unpublished")
        self.assertFalse(self.report()["full_interoperability"])
        created = next(command for command, _, _ in commands.calls if command[:3] == ["docker", "buildx", "create"])
        builder = created[created.index("--name") + 1]
        self.assertNotIn("--use", created)
        self.assertIn(["docker", "buildx", "rm", builder], [command for command, _, _ in commands.calls])
        self.assertTrue(all(env["BUILDX_BUILDER"] == builder for _, env, _ in commands.calls))

    def test_publish_removes_only_exact_owned_local_tags_and_introduced_source(self):
        commands = Commands()
        self.assertEqual(self.execute(commands, "--publish"), 0)
        self.assertEqual(self.report()["publication"], "passed")
        removed = [command[-1] for command, _, _ in commands.calls if command[:3] == ["docker", "image", "rm"]]
        roles, _ = discovery.tags("official", "19.2.0")
        candidates = {discovery.REGISTRY + ":ci-discovery-123-1-official-19.2.0-" + role + "-linux-amd64" for role in roles}
        expected = set(roles.values()) | candidates | {discovery.patch_releases.release("19.2.0")["official"]["source"]}
        self.assertEqual(set(removed), expected)
        self.assertEqual(len(removed), len(expected))
        self.assertFalse(any("--force" in command or "prune" in command for command, _, _ in commands.calls))
        self.assertEqual(commands.local, set())

    def test_preexisting_official_source_is_never_removed(self):
        commands = Commands(preexisting=True)
        self.assertEqual(self.execute(commands, "--publish"), 0)
        removed = [command[-1] for command, _, _ in commands.calls if command[:3] == ["docker", "image", "rm"]]
        self.assertNotIn(discovery.patch_releases.release("19.2.0")["official"]["source"], removed)

    def test_failed_checks_clean_owned_tags_and_continue_later_versions(self):
        commands = Commands(fail=("19.2.0",))
        self.assertEqual(self.execute(commands, "--releases", "19.2.0,19.2.1", "--publish"), 1)
        self.assertEqual(self.report()["status"], "failed")
        self.assertEqual(self.report("19.2.1")["status"], "passed")
        self.assertEqual(self.report()["classification"], "timeout")
        self.assertTrue(any("stage-discovery" in command and "19.2.1" in command for command, _, _ in commands.calls))

    def test_uncertain_staging_retains_checked_images_and_removes_private_builder(self):
        commands = Commands(publish_fail=True)
        self.assertEqual(self.execute(commands, "--publish"), 1)
        self.assertEqual(self.report()["publication"], "failed")
        self.assertEqual(self.report()["image_cleanup"], "retained_unpublished")
        self.assertFalse(any(command[:3] == ["docker", "image", "rm"] for command, _, _ in commands.calls))
        self.assertTrue(any(command[:3] == ["docker", "buildx", "rm"] for command, _, _ in commands.calls))

    def test_failed_quick_evidence_cannot_publish_and_its_owned_images_are_removed(self):
        commands = Commands(bad_checks=True)
        self.assertEqual(self.execute(commands, "--publish"), 1)
        self.assertFalse(any("stage-discovery" in command for command, _, _ in commands.calls))
        self.assertEqual(self.report()["classification"], "invalid_or_incomplete_evidence")
        self.assertEqual(self.report()["image_cleanup"], "passed")
        self.assertEqual(commands.local, set())

    def test_cleanup_error_keeps_primary_timeout_visible(self):
        commands = Commands(fail=("19.2.0",))
        def fail_cleanup(command, log, timeout, env=None, check_result=True):
            if command[:3] == ["docker", "buildx", "rm"]:
                raise discovery.CommandError("command_failed", command, 1)
            return commands(command, log, timeout, env, check_result)
        self.assertEqual(self.execute(fail_cleanup, "--publish"), 1)
        self.assertEqual(self.report()["classification"], "timeout")
        self.assertTrue(self.report()["cleanup_errors"])

    def test_real_subprocess_timeout_is_reported_after_settlement(self):
        log = self.root / "timeout.log"
        with self.assertRaises(discovery.CommandError) as raised:
            discovery.run([sys.executable, "-c", "import time; time.sleep(5)"], log, 0.03)
        self.assertEqual(raised.exception.classification, "timeout")

    def test_timeout_settles_descendants_after_leader_exits(self):
        for ignore_term in (False, True):
            with self.subTest(ignore_term=ignore_term):
                pidfile = self.root / ("child-" + str(ignore_term))
                marker = pidfile.with_suffix(".marker")
                parentfile = pidfile.with_suffix(".parent")
                child = ("import os,signal,time; from pathlib import Path; "
                         "signal.signal(signal.SIGINT,signal.SIG_IGN); "
                         + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "")
                         + "Path(" + repr(str(pidfile)) + ").write_text(str(os.getpid())); "
                         "time.sleep(1.4); Path(" + repr(str(marker)) + ").write_text('late write'); time.sleep(10)")
                parent = ("import os,signal,subprocess,sys,time; from pathlib import Path; "
                          "signal.signal(signal.SIGINT,lambda *_:sys.exit(0)); "
                          "Path(" + repr(str(parentfile)) + ").write_text(str(os.getpid())); "
                          "subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(10)")
                started = time.monotonic()
                try:
                    with mock.patch.object(discovery, "STOP_STEPS", (
                            (signal.SIGINT, .12), (signal.SIGTERM, .12), (signal.SIGKILL, 1))):
                        with self.assertRaises(discovery.CommandError) as raised:
                            discovery.run([sys.executable, "-c", parent], pidfile.with_suffix(".log"), .5)
                    self.assertEqual(raised.exception.classification, "timeout")
                    self.assertTrue(pidfile.exists(), "descendant must install signal handlers before timeout")
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(int(parentfile.read_text()), 0)
                    time.sleep(max(0, 1.7 - (time.monotonic() - started)))
                    self.assertFalse(marker.exists(), "descendant wrote after timeout settlement")
                finally:
                    if parentfile.exists():
                        try:
                            os.killpg(int(parentfile.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_unconfirmed_group_stop_is_distinct_from_settled_timeout(self):
        # A separate reaper owns this real process when settlement is unavailable.
        real_popen = subprocess.Popen
        owned = []
        def capture(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            owned.append(process)
            return process
        try:
            with mock.patch.object(discovery.subprocess, "Popen", side_effect=capture), \
                    mock.patch.object(discovery, "settle_group", return_value=False):
                with self.assertRaises(discovery.CommandError) as raised:
                    discovery.run([sys.executable, "-c", "import time; time.sleep(10)"],
                                  self.root / "unconfirmed.log", .03)
            self.assertEqual(raised.exception.classification, "stop_unconfirmed")
        finally:
            for process in owned:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)

    def test_unconfirmed_stop_blocks_cleanup_and_remaining_patches(self):
        commands = Commands()
        def unconfirmed(command, *args, **kwargs):
            if command[:2] == ["docker", "pull"]:
                raise discovery.CommandError("stop_unconfirmed", command)
            return commands(command, *args, **kwargs)
        self.assertEqual(self.execute(unconfirmed, "--releases", "19.2.0,19.2.1"), 1)
        self.assertEqual(self.report()["process_stop"], "unconfirmed")
        self.assertEqual(self.report()["image_cleanup"], "not_attempted_unconfirmed_stop")
        self.assertFalse(any(c[:3] in (["docker", "image", "rm"], ["docker", "buildx", "rm"])
                             for c, _, _ in commands.calls))
        summary = json.loads((self.root / "run/summary.json").read_text())
        self.assertEqual(summary["not_started"], ["19.2.1"])
        self.assertFalse((self.root / "run/19.2.1").exists())

    def test_unconfirmed_cleanup_stop_blocks_next_cleanup_command(self):
        commands = Commands(fail=("19.2.0",))
        def unconfirmed(command, *args, **kwargs):
            if command[:3] == ["docker", "image", "rm"]:
                raise discovery.CommandError("stop_unconfirmed", command)
            return commands(command, *args, **kwargs)
        self.assertEqual(self.execute(unconfirmed, "--releases", "19.2.0,19.2.1"), 1)
        self.assertEqual(self.report()["classification"], "timeout")
        self.assertEqual(self.report()["process_stop"], "unconfirmed")
        self.assertFalse(any(c[:3] == ["docker", "buildx", "rm"] for c, _, _ in commands.calls))
        self.assertFalse((self.root / "run/19.2.1").exists())

    def test_ownership_collision_refuses_without_mutating_docker(self):
        commands = Commands()
        commands.local.add("ceph-discovery:discovery-official-19.2.0-client")
        self.assertEqual(self.execute(commands), 1)
        self.assertEqual(len(commands.calls), 1)
        self.assertEqual(self.report()["image_cleanup"], "not_owned")

    def test_umbrella_cpu_ineligible_is_not_missing_image(self):
        commands = Commands(cpu=False)
        self.assertEqual(self.execute(commands, "--releases", "21.2.0"), 1)
        self.assertEqual(self.report("21.2.0")["status"], "environment_ineligible")
        self.assertFalse(any(command[0] == "docker" for command, _, _ in commands.calls))

    def test_bad_selection_revision_or_nonempty_output_refuses_before_commands(self):
        for extra in (("--releases", "19.2.99"), ("--releases", "19.2.0,19.2.0"), ("--revision", "main")):
            with self.subTest(extra=extra):
                commands = Commands()
                with self.assertRaises(ValueError):
                    self.execute(commands, *extra)
                self.assertEqual(commands.calls, [])
        (self.root / "run").mkdir()
        (self.root / "run/earlier.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "earlier evidence"):
            self.execute(Commands())


if __name__ == "__main__":
    unittest.main()
