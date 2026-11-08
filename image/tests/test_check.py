import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check


class ImageContractTests(unittest.TestCase):
    def test_bad_roles_references_and_duplicates_are_rejected(self):
        for values in (["control"], ["mon=image"], ["all="], ["all= "],
                       ["all=--help"], ["all=first", "all=second"]):
            with self.subTest(values=values), self.assertRaises(check.CheckError):
                check.image_arguments(values)
        self.assertEqual(check.image_arguments(["all=repo/ceph:v1@sha256:abc"]),
                         {"all": "repo/ceph:v1@sha256:abc"})

    def test_partial_roles_cannot_run_full_check(self):
        with self.assertRaises(check.CheckError):
            check.runtime_sets({"control": "control", "osd": "osd"})
        images = {role: role for role in check.ROLES}
        sets = check.runtime_sets(images)
        baseline = {role: role for role in check.contract.ROLES}
        self.assertEqual(sets["mixed"], baseline)
        self.assertEqual(set(sets["all"].values()), {"all"})
        # mgr-modules replaces only the MGR image of the mixed roles.
        self.assertEqual(sets["mgr-modules"], dict(baseline, **{"mgr-modules": "mgr-modules"}))
        self.assertEqual(set(sets), {"mixed", "all", "mgr-modules"})

    def test_topologies_select_and_require_their_images(self):
        images = {"essential": "e", "client": "c", "cephfs": "f", "mgr-modules": "m"}
        sets = check.runtime_sets(images, ["cephfs"], ["mgr-modules"])
        self.assertEqual(sets, {"mgr-modules": dict(images)})
        self.assertEqual(set(check.runtime_sets(images, ["cephfs"])), {"mixed", "mgr-modules"})
        for topologies in (["all"], ["mgr-modules", "all"]):
            with self.subTest(topologies=topologies), self.assertRaisesRegex(check.CheckError, "lack"):
                check.runtime_sets(images, ["cephfs"], topologies)
        # Without its baseline roles mgr-modules has no topology of its own.
        with self.assertRaises(check.CheckError):
            check.runtime_sets({"mgr-modules": "m"}, ["cephfs"])

    def test_requested_mgr_modules_are_validated_and_reach_only_the_mgr_probe(self):
        args = check.arguments(["--image", "mgr-modules=m", "--mgr-module", "volumes",
                                "--mgr-module", "my_custom", "--mgr-module", "volumes"])
        self.assertEqual(args.mgr_module, ["my_custom", "volumes"])
        for name in ("bad-name", "1st", "a.b", ""):
            with self.subTest(name=name), mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                check.arguments(["--image", "mgr-modules=m", "--mgr-module", name])
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            check.arguments(["--image", "all=a", "--topology", "all"])
        with self.assertRaisesRegex(check.CheckError, "mgr-modules=REF"):
            check.main(["--image", "essential=e", "--mgr-module", "dashboard"])
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return ""
        started = mock.Mock(stdout="COMPLETE\tmgr-modules\nCHECK\tpassed\tx\nVERSION\tceph-mgr\t"
                                   "ceph version 20.2.4 (abc) tentacle (stable)\n", stderr="", returncode=0)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(check, "run", side_effect=run), \
                mock.patch.object(check.subprocess, "run", return_value=started):
            for role in ("mgr-modules", "essential"):
                image = {"image_id": "sha256:" + "1" * 64, "platform": "linux/amd64"}
                check.probe_image(role, image, args, Path(folder))
                create = next(c for c in reversed(commands) if c[:2] == ["docker", "create"])
                tail = create[create.index("--") + 1:]
                if role == "mgr-modules":
                    self.assertEqual(tail, ["mgr-modules", "my_custom", "volumes"])
                    self.assertEqual(image["requested_mgr_modules"], ["my_custom", "volumes"])
                else:
                    self.assertEqual(tail, ["essential"])
                    self.assertNotIn("requested_mgr_modules", image)

    def test_multiarch_probe_uses_runnable_index_id_without_resolving_tag_twice(self):
        root_id = "sha256:" + "1" * 64
        child_id = "sha256:" + "2" * 64
        original = {"Id": root_id, "Os": "linux", "Architecture": "arm64", "RepoDigests": ["repo@" + root_id]}
        selected = dict(original, Id=child_id)
        with mock.patch.object(check, "run", side_effect=[json.dumps([original]), json.dumps([selected])]) as run:
            image = check.inspect_image("mutable:tag", "linux/arm64")
        self.assertEqual(image["image_id"], root_id)
        self.assertEqual(image["platform_image_id"], child_id)
        self.assertEqual(run.call_args_list[1].args[0][-1], root_id)

    def test_missing_inspect_identity_and_platform_are_reported_as_errors(self):
        for metadata in ([], [{}], [{"Id": "bad", "Os": "linux", "Architecture": "arm64"}]):
            with self.subTest(metadata=metadata), mock.patch.object(check, "run", return_value=json.dumps(metadata)), \
                    self.assertRaises(check.CheckError):
                check.inspect_image("image", None)

    def test_docker28_fallback_still_rejects_platform_mismatch(self):
        original = {"Id": "sha256:" + "1" * 64, "Os": "linux", "Architecture": "arm64"}
        for requested, succeeds in (("linux/arm64", True), ("linux/amd64", False)):
            with self.subTest(platform=requested), mock.patch.object(check, "run", side_effect=[
                    json.dumps([original]), check.CheckError("unknown flag: --platform")]):
                if succeeds:
                    self.assertEqual(check.inspect_image("image", requested)["platform"], requested)
                else:
                    with self.assertRaisesRegex(check.CheckError, "mismatched"):
                        check.inspect_image("image", requested)

    def test_platform_inspection_errors_other_than_unsupported_flag_are_not_hidden(self):
        original = {"Id": "sha256:" + "1" * 64, "Os": "linux", "Architecture": "arm64"}
        with mock.patch.object(check, "run", side_effect=[json.dumps([original]),
                check.CheckError("missing platform manifest")]), self.assertRaisesRegex(check.CheckError, "missing"):
            check.inspect_image("image", "linux/arm64")

    def test_control_import_and_binary_failures_remain_in_report(self):
        checks, versions = check.parse_probe("CHECK\tfailed\tversion:ceph-mon\nCHECK\tfailed\tpython-bindings\n")
        self.assertEqual([item["status"] for item in checks], ["failed", "failed"])
        self.assertEqual(versions, {})

    def test_no_checks_or_version_and_inconsistent_versions_cannot_pass(self):
        for output in ("", "CHECK\tpassed\tshell\n",
                       "CHECK\tpassed\tshell\nVERSION\tceph\tunrecognized\n",
                       "CHECK\tpassed\tshell\nVERSION\tceph\tceph version 20.2.4 (abc)\n"
                       "VERSION\trbd\tceph version 19.2.3 (def)\n"):
            with self.subTest(output=output), self.assertRaises(check.CheckError):
                check.parse_probe(output)

    def test_timeout_removes_the_owned_probe_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            image = {"platform": "linux/arm64", "image_id": "sha256:resolved"}
            args = check.arguments(["--image", "all=mutable"])
            with mock.patch.object(check, "run", return_value="container") as run, \
                    mock.patch.object(check.subprocess, "run", side_effect=subprocess.TimeoutExpired(
                        "docker start", 1, output=b"partial log")), self.assertRaises(check.CheckError):
                check.probe_image("all", image, args, output)
            calls = [call.args[0] for call in run.call_args_list]
            self.assertEqual(calls[0][-4:], ["sha256:resolved", "-s", "--", "all"])
            self.assertEqual(calls[1][:3], ["docker", "rm", "--force"])
            self.assertEqual(calls[1][3], calls[0][3])
            self.assertEqual((output / "all.log").read_text(), "partial log")
            self.assertEqual(image["status"], "failed")

    def test_incomplete_probe_with_exit_zero_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(check, "run", return_value="container"), \
                mock.patch.object(check.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "CHECK\tpassed\tshell\nVERSION\tceph\tceph version 20.2.4 (abc)\n", "")):
            image = {"platform": "linux/arm64", "image_id": "fixed"}
            check.probe_image("essential", image, check.arguments(["--image", "essential=image"]), Path(temporary))
        self.assertEqual(image["status"], "failed")
        self.assertIn("did not finish", image["error"])

    def test_main_uses_fixed_ids_and_preserves_runtime_failure(self):
        for runtime_fails in (False, True):
            with self.subTest(runtime_fails=runtime_fails), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                def inspect(reference, platform):
                    return {"image_id": "sha256:fixed", "platform": "linux/arm64", "reference": reference}
                def probe(role, image, args, output):
                    image.update({"status": "passed", "versions": {
                        "ceph": {"release": "20.2.4", "commit": "abc"}}})
                def run(images, scenarios, output, log):
                    self.assertEqual(set(images.values()), {"sha256:fixed"})
                    self.assertEqual(tuple(scenarios), check.suite.SCENARIOS)
                    return {name: {"status": "failed" if runtime_fails and name == "rgw-multisite" else "passed"}
                            for name in scenarios}
                with mock.patch.object(check.shutil, "which", return_value="installed"), \
                        mock.patch.object(check, "inspect_image", side_effect=inspect), \
                        mock.patch.object(check, "probe_image", side_effect=probe), \
                        mock.patch.object(check.suite, "run", side_effect=run), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    code = check.main(["--image", "all=mutable", "--full", "--output-dir", str(output)])
                report = json.loads((output / "check-report.json").read_text())
                expected = "failed" if runtime_fails else "passed"
                self.assertEqual(code, 1 if runtime_fails else 0)
                self.assertEqual(report["level"], "full")
                self.assertEqual(report["scenarios"], expected)
                self.assertEqual(report["functional"]["all"]["status"], expected)
                self.assertEqual(report["functional"]["all"]["failed"], ["rgw-multisite"] if runtime_fails else [])
                self.assertEqual(report["preflight"], "passed")

    def test_mixed_platforms_stop_before_starting_any_container(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(check.shutil, "which", return_value="docker"), \
                mock.patch.object(check, "probe_image") as probe, \
                mock.patch.object(check, "inspect_image", side_effect=[
                    {"platform": "linux/arm64"}, {"platform": "linux/amd64"}]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = check.main(["--image", "essential=a", "--image", "client=b", "--output-dir", temporary])
            report = json.loads((Path(temporary) / "check-report.json").read_text())
        probe.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(report["preflight"], "failed")
        self.assertEqual(report["level"], "quick")
        self.assertEqual(report["scenarios"], "not_requested")

    def test_existing_report_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "check-report.json"
            report.write_text("previous evidence")
            with mock.patch.object(check.shutil, "which", return_value="docker"), \
                    self.assertRaises(check.CheckError):
                check.main(["--image", "all=image", "--output-dir", temporary])
            self.assertEqual(report.read_text(), "previous evidence")

    def test_missing_and_loading_failures_remain_distinct(self):
        checks, _ = check.parse_probe(
            "CHECK\tfailed\tpath:cryptsetup\nFAILURE\tmissing_file\tpath:cryptsetup\n"
            "CHECK\tfailed\tclass-load:hello\nFAILURE\tloading_failure\tclass-load:hello\n")
        self.assertEqual([item["failure_kind"] for item in checks], ["missing_file", "loading_failure"])
        self.assertEqual(checks[1]["failure_stage"], "quick:class-load:hello")

    def test_missing_supported_dependency_fails_without_running_functional(self):
        with tempfile.TemporaryDirectory() as temporary:
            def probe(role, image, args, output):
                image.update(status="failed", checks=[{"name": "path:cryptsetup", "status": "failed",
                    "failure_kind": "missing_file"}], versions={})
            with mock.patch.object(check.shutil, "which", return_value="docker"), \
                    mock.patch.object(check, "inspect_image", return_value={"platform": "linux/arm64", "reference": "image"}), \
                    mock.patch.object(check, "probe_image", side_effect=probe), \
                    mock.patch.object(check.suite, "run") as functional, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = check.main(["--image", "all=image", "--full", "--scenario", "rbd-encryption",
                                   "--output-dir", temporary])
            functional.assert_not_called()
            report = json.loads((Path(temporary) / "check-report.json").read_text())
        self.assertEqual(code, 1)
        self.assertEqual(report["level"], "functional-selected")
        self.assertEqual(report["scenarios"], "not_run")


class ProcessInspectionProbeTests(unittest.TestCase):
    """Runs the probe's pgrep_inspection function against stub pgrep commands."""

    def run_inspection(self, stub):
        script = (check.HERE / "check-runtime.sh").read_text()
        start = script.index("pgrep_inspection() {")
        function = script[start:script.index("\n}\n", start) + 3]
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary) / "bin"
            bin_dir.mkdir()
            if stub is not None:
                pgrep = bin_dir / "pgrep"
                pgrep.write_text("#!/bin/sh\n" + stub)
                pgrep.chmod(0o755)
            environment = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                               STUB_STATE=temporary, NATIVE_PGREP=self.native_pgrep or "false")
            return subprocess.run(["/bin/sh", "-c", "work=$1\n" + function + "\npgrep_inspection", "sh", temporary],
                                  env=environment, capture_output=True, text=True, timeout=60)

    def setUp(self):
        self.native_pgrep = shutil.which("pgrep")

    def test_native_pgrep_finds_the_process_and_then_nothing(self):
        if self.native_pgrep is None:
            self.skipTest("the host has no pgrep")
        result = self.run_inspection(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wrong_answers_fail(self):
        stubs = {
            "never finds the process": ("exit 1\n", "while process"),
            "reports another process": ("echo 1\n", "while process"),
            "still reports the process after it exited": (
                'if test -e "$STUB_STATE/called"; then echo 4242; exit 0; fi\n'
                'touch "$STUB_STATE/called"\nexec "$NATIVE_PGREP" "$@"\n', "after process"),
        }
        for name, (stub, failure) in stubs.items():
            with self.subTest(name):
                if failure == "after process" and self.native_pgrep is None:
                    self.skipTest("the host has no pgrep")
                result = self.run_inspection(stub)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(failure, result.stdout)


class ParentCachePluginProbeTests(unittest.TestCase):
    """Runs the control probe's Python block against a stub plugin directory."""

    def run_probe(self, plugin, role="all"):
        script = (check.HERE / "check-runtime.sh").read_text()
        start = script.index("python3 - \"$role\" <<'PY' || failed=1\n") + len("python3 - \"$role\" <<'PY' || failed=1\n")
        block = script[start:script.index("\nPY\n", start)]
        with tempfile.TemporaryDirectory() as temporary:
            plugin_dir = Path(temporary) / "plugins"
            (plugin_dir / "librbd").mkdir(parents=True)
            if plugin is not None:
                plugin(plugin_dir / "librbd" / "libceph_librbd_parent_cache.so", Path(temporary))
            bin_dir = Path(temporary) / "bin"
            bin_dir.mkdir()
            stub = bin_dir / ("ceph-conf" if role == "rbd-aux" else "ceph-mgr")
            stub.write_text("#!/bin/sh\necho 'did not load config file' >&2\necho %s\n" % plugin_dir)
            stub.chmod(0o755)
            environment = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            result = subprocess.run([sys.executable, "-c", block, role], env=environment,
                                    capture_output=True, text=True, timeout=60)
        return [line for line in result.stdout.splitlines() if "rbd-parent-cache" in line]

    @staticmethod
    def compiled(source):
        def build(path, temporary):
            compiler = shutil.which("cc")
            if compiler is None:
                raise unittest.SkipTest("the host has no C compiler")
            (temporary / "plugin.c").write_text(source)
            subprocess.run([compiler, "-shared", "-fPIC", "-o", str(path), str(temporary / "plugin.c")], check=True)
        return build

    def test_loadable_plugin_passes(self):
        lines = self.run_probe(self.compiled(
            "const char *__ceph_plugin_version(void) { return \"x\"; }\n"
            "int __ceph_plugin_init(void *c, const char *t, const char *n) { return 0; }\n"))
        self.assertEqual(lines, ["CHECK\tpassed\tlibrary-present:rbd-parent-cache",
                                 "CHECK\tpassed\tlibrary-load:rbd-parent-cache"])

    def test_auxiliary_plugin_probe_does_not_need_a_manager_binary(self):
        lines = self.run_probe(None, role="rbd-aux")
        self.assertIn("CHECK\tfailed\tlibrary-present:rbd-parent-cache", lines)

    def test_missing_plugin_is_a_missing_file(self):
        lines = self.run_probe(None)
        self.assertEqual(lines, ["CHECK\tfailed\tlibrary-present:rbd-parent-cache",
                                 "FAILURE\tmissing_file\tlibrary-present:rbd-parent-cache"])

    def test_unloadable_or_incomplete_plugin_is_a_loading_failure(self):
        for name, plugin in {
            "not a library": lambda path, temporary: path.write_text("not a library"),
            "no entry points": self.compiled("int unrelated(void) { return 0; }\n"),
        }.items():
            with self.subTest(name):
                lines = self.run_probe(plugin)
                self.assertEqual(lines, ["CHECK\tpassed\tlibrary-present:rbd-parent-cache",
                                         "CHECK\tfailed\tlibrary-load:rbd-parent-cache",
                                         "FAILURE\tloading_failure\tlibrary-load:rbd-parent-cache"])

if __name__ == "__main__":
    unittest.main()


class CephfsRecoveryToolQuickCheckTests(unittest.TestCase):
    """Runs the role gating and the cephfs-journal-tool check with a recording check()."""

    def checks(self, role):
        script = (check.HERE / "check-runtime.sh").read_text()
        gating = script[script.index("core=false\n"):script.index("version() {")]
        tail = script[script.index('if "$fstools"; then\n    check version'):script.index("printf 'COMPLETE")]
        program = 'role=$1\ncheck() { echo "$1"; }\n' + gating + tail
        result = subprocess.run(["/bin/sh", "-c", program, "sh", role], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_only_cephfs_and_all_check_the_tool(self):
        for role in ("cephfs", "all"):
            self.assertIn("version:cephfs-journal-tool", self.checks(role), role)
        for role in ("essential", "client", "rbd-aux", "rgw"):
            self.assertNotIn("version:cephfs-journal-tool", self.checks(role), role)

    def test_check_failure_is_reported_as_a_loading_failure(self):
        script = (check.HERE / "check-runtime.sh").read_text()
        self.assertIn("library-load:*|class-load:*|version:*) kind=loading_failure", script)


class BlueStoreToolQuickCheckTests(unittest.TestCase):
    """Runs the role gating and the OSD version checks with a recording check()."""

    def checks(self, role):
        script = (check.HERE / "check-runtime.sh").read_text()
        gating = script[script.index("core=false\n"):script.index("version() {")]
        start = script.index('if "$osd"; then\n')
        end = script.index("\n", script.index("check version:ceph-bluestore-tool", start)) + 1
        program = 'role=$1\ncheck() { echo "$1"; }\n' + gating + script[start:end] + "fi\n"
        result = subprocess.run(["/bin/sh", "-c", program, "sh", role], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_only_essential_and_all_check_the_tool(self):
        for role in ("essential", "all"):
            self.assertIn("version:ceph-bluestore-tool", self.checks(role), role)
        for role in ("client", "cephfs", "rbd-aux", "rgw"):
            self.assertNotIn("version:ceph-bluestore-tool", self.checks(role), role)


class ImmutableCacheToolProbeTests(unittest.TestCase):
    """Runs the immutable cache tool checks of the quick check on this host's
    tools and on stubs that drop one form the Go library's lease uses."""

    FORMS = ("flock", "renameat2", "realpath", "stat", "find", "links", "rename", "text",
             "directories", "kill", "sleep", "mountinfo")

    @classmethod
    def setUpClass(cls):
        script = (check.HERE / "check-runtime.sh").read_text()
        definitions = script[script.index("check() {"):script.index("for utility in sh mkdir")]
        functions = script[script.index("cache_utilities="):script.index(
            'if "$core" || "$client" || "$cache" || "$fsmirror"')]
        start = script.index('    if "$cache"; then\n    for utility in $cache_utilities')
        block = script[start:script.index("\n    fi\n", start) + 8]
        gating = script[script.index("core=false\n"):script.index("version() {")]
        cls.program = ("set -u\nrole=$1\nfailed=0\nwork=$2\n" + definitions + gating + functions +
                       'if "$cache"; then\n' + block + "fi\nexit $failed\n")

    def run_checks(self, role="rbd-aux", stubs=None):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary) / "work"
            work.mkdir()
            bin_dir = Path(temporary) / "bin"
            bin_dir.mkdir()
            for name, body in (stubs or {}).items():
                stub = bin_dir / name
                stub.write_text("#!/bin/sh\n" + body)
                stub.chmod(0o755)
            environment = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["/bin/sh", "-c", self.program, "sh", role, str(work)],
                                    env=environment, capture_output=True, text=True, timeout=120)
        checks = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if fields[0] == "CHECK":
                checks[fields[2]] = fields[1]
        return result, checks

    def require_gnu_host(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("the forms are GNU coreutils/findutils/util-linux forms")
        for name in ("flock", "realpath", "find"):
            if shutil.which(name) is None:
                self.skipTest("the host has no " + name)

    def test_cache_roles_check_every_utility_and_form(self):
        for role in ("rbd-aux", "all"):
            with self.subTest(role):
                _, checks = self.run_checks(role)
                expected = {"immutable-cache:" + form for form in self.FORMS}
                expected |= {"path:" + name for name in ("flock", "realpath", "stat", "sha256sum", "find", "grep")}
                self.assertLessEqual(expected, set(checks))
        for role in ("essential", "client", "cephfs", "rgw"):
            with self.subTest(role):
                _, checks = self.run_checks(role)
                self.assertFalse([name for name in checks if name.startswith("immutable-cache:")])

    def test_host_gnu_tools_pass(self):
        self.require_gnu_host()
        result, checks = self.run_checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual({name: state for name, state in checks.items() if state != "passed"}, {})

    def test_a_wrapped_sleep_passes(self):
        # coreutils-single runs sleep through a shebang wrapper of the
        # multi-call binary, so the child's command line is not "sleep 30".
        self.require_gnu_host()
        sleep = shutil.which("sleep")
        result, checks = self.run_checks(stubs={"sleep": 'exec %s "$@"\n' % sleep})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(checks.get("immutable-cache:flock"), "passed")
        self.assertEqual(checks.get("immutable-cache:kill"), "passed")

    def test_each_missing_or_wrong_form_fails(self):
        self.require_gnu_host()
        real = {name: shutil.which(name) for name in ("flock", "realpath", "stat", "find", "mv", "ln", "sha256sum")}
        cases = {
            "flock that never waits": ({"flock": "exit 0\n"}, "immutable-cache:flock"),
            "flock that ignores -E": ({"flock": 'test "$1" = -E && shift 2\nexec %s "$@"\n' % real["flock"]},
                                      "immutable-cache:flock"),
            "missing flock": ({"flock": "exit 127\n"}, "immutable-cache:flock"),
            "no renameat2 in libc": ({"python3": "exit 1\n"}, "immutable-cache:renameat2"),
            "realpath without -m": ({"realpath": 'test "$1" = -m && exit 1\nexec %s "$@"\n' % real["realpath"]},
                                    "immutable-cache:realpath"),
            "stat without birth": ({"stat": 'test "$2" = "%%d:%%i:%%w:%%f" && { echo 1:2; exit 0; }\nexec %s "$@"\n'
                                            % real["stat"]}, "immutable-cache:stat"),
            "wrong sha256sum": ({"sha256sum": "cat > /dev/null\necho '0  -'\n"}, "immutable-cache:stat"),
            "find without -printf": ({"find": 'for a; do test "$a" = -printf && exit 1; done\nexec %s "$@"\n'
                                              % real["find"]}, "immutable-cache:find"),
            "ln that replaces": ({"ln": 'test "$1" = -sT && exec %s -sfT "$2" "$3"\nexec %s "$@"\n'
                                        % (real["ln"], real["ln"])}, "immutable-cache:links"),
            "ln that ignores -T": ({"ln": 'case "$1" in -T) shift ;; -sT) shift; set -- -s "$@" ;; esac\n'
                                          'exec %s "$@"\n' % real["ln"]}, "immutable-cache:links"),
            "flock on writable files only": ({"flock": 'test -d /proc/self/fd/9 && exit 1\n'
                                                       'exec %s "$@"\n' % real["flock"]}, "immutable-cache:flock"),
            "mv that replaces": ({"mv": 'test "$1" = -nT && { rm -rf "$3"; exec %s -T "$2" "$3"; }\nexec %s "$@"\n'
                                        % (real["mv"], real["mv"])}, "immutable-cache:rename"),
            "integer-only sleep": ({"sleep": 'case "$1" in *.*) exit 1 ;; esac\nexec /bin/sleep "$@"\n'},
                                   "immutable-cache:sleep"),
        }
        for name, (stubs, failed) in cases.items():
            with self.subTest(name):
                result, checks = self.run_checks(stubs=stubs)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(checks.get(failed), "failed", result.stdout + result.stderr)

    def test_a_missing_utility_is_a_missing_file(self):
        self.require_gnu_host()
        with tempfile.TemporaryDirectory() as temporary:
            shadow = Path(temporary)
            for directory in os.environ["PATH"].split(os.pathsep):
                for entry in Path(directory).glob("*") if Path(directory).is_dir() else ():
                    target = shadow / entry.name
                    if entry.name != "sha256sum" and not target.exists() and os.access(entry, os.X_OK):
                        target.symlink_to(entry)
            script = self.program
            result = subprocess.run(["/bin/sh", "-c", script, "sh", "rbd-aux", temporary + "/work"],
                                    env=dict(os.environ, PATH=str(shadow)), capture_output=True, text=True,
                                    timeout=120, cwd=temporary)
        self.assertIn("FAILURE\tmissing_file\tpath:sha256sum", result.stdout, result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 0)
