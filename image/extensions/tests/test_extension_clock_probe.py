import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check

DIRECTORY = Path(extension_check.HERE) / "extensions" / "clock-probe"
PROBE = Path(extension_check.EXTENSIONS["clock-probe"]["probe"])
HELPER = {"reference": "ceph-check:extension-clock-probe", "image_id": "sha256:" + "d" * 64,
          "platform": "linux/amd64"}
PROBE_OUTPUT = "".join("CHECK\tpassed\t%s\n" % name for name in (
    "path:sh", "path:sleep", "path:python3", "path:gcc", "path:nm", "path:clock-probe-build-info",
    "sleep-infinity", "version:gcc", "python-runtime", "writable:/tmp", "manifest", "symbol:syscall",
    "load:shift")) + "VERSION\tgcc\tgcc (Debian 12.2.0-14) 12.2.0\nCOMPLETE\tclock-probe\n"


class ClockProbeQuickCheckTests(unittest.TestCase):
    """The clock-probe helper's quick check, with Docker replaced by fakes."""

    def test_the_helper_has_only_the_quick_check_and_is_not_published(self):
        self.assertEqual(extension_check.arguments(["--extension", "clock-probe", "--image", "x"]).extension,
                         "clock-probe")
        self.assertNotIn("clock-probe", extension_check.PUBLISHED)
        self.assertEqual(extension_check.EXTENSIONS["clock-probe"]["contract"], "1")
        stderr = io.StringIO()
        for extra in (["--functional", "--target", "t"], ["--rate"], ["--packet-faults"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                extension_check.arguments(["--extension", "clock-probe", "--image", "x", *extra])
        self.assertIn("only the quick check", stderr.getvalue())

    def test_the_probe_runs_as_root_without_capabilities_or_network(self):
        created = []

        def docker(*command, timeout=120, check_exit=True):
            if command[0] == "create":
                created.append(list(command))
            return 0, ""

        result = mock.Mock(returncode=0, stdout=PROBE_OUTPUT, stderr="")
        image = dict(HELPER)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check.subprocess, "run", return_value=result), \
                mock.patch.object(extension_check, "remove_container", return_value={"status": "passed", "errors": []}):
            extension_check.probe("clock-probe", image, mock.Mock(probe_timeout=10), Path(directory))
        command = created[0]
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--network=none", command)
        self.assertIn("--user=0:0", command)
        self.assertFalse(any(arg.startswith("--cap-add") for arg in command))
        self.assertEqual(image["status"], "passed")
        self.assertEqual(image["versions"]["gcc"], "gcc (Debian 12.2.0-14) 12.2.0")

    def test_an_unfinished_or_failed_probe_fails_the_image(self):
        for name, stdout, code in (("failed check", PROBE_OUTPUT.replace("CHECK\tpassed\tmanifest", "CHECK\tfailed\tmanifest"), 1),
                                   ("not complete", PROBE_OUTPUT.replace("COMPLETE\tclock-probe\n", ""), 0)):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(extension_check, "docker", return_value=(0, "")), \
                    mock.patch.object(extension_check.subprocess, "run",
                                      return_value=mock.Mock(returncode=code, stdout=stdout, stderr="")), \
                    mock.patch.object(extension_check, "remove_container",
                                      return_value={"status": "passed", "errors": []}):
                image = dict(HELPER)
                extension_check.probe("clock-probe", image, mock.Mock(probe_timeout=10), Path(directory))
                self.assertEqual(image["status"], "failed")

    def test_the_probe_uses_only_the_contract_executables(self):
        # sh builtins plus sh, sleep, python3, gcc, nm and clock-probe-build-info.
        script = PROBE.read_text()
        shell = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        # Quoted Python bodies are not shell commands.
        shell = re.sub(r"'[^']*'", "''", shell)
        for tool in ("grep", "tr", "rm", "cat", "sed", "awk", "head", "tail", "seq", "id", "mkdir", "ls", "cut",
                     "mktemp", "env", "timeout", "sha256sum", "uname", "readelf", "objdump", "ldd", "file"):
            with self.subTest(tool=tool):
                self.assertIsNone(re.search(r"(^|[;&|(`]|\$\(|\bthen|\bdo|\belse)\s*%s\b" % tool, shell, re.M),
                                  tool + " is not a contract executable")

    def test_the_load_check_never_sets_faketime(self):
        # FAKETIME disables the timestamp file in libfaketime's initialization.
        script = PROBE.read_text()
        self.assertNotIn("FAKETIME=", script)
        self.assertIn('not key.startswith("FAKETIME")', script)
        self.assertIn("FAKETIME_TIMESTAMP_FILE=stamp", script)


def python_blocks(script):
    """The bodies of the quick probe's `python3 -c '...'` blocks."""
    return re.findall(r"python3 -c '\n(.*?)\n'\n", script, re.S)


class BuildManifestOracleTests(unittest.TestCase):
    """The manifest oracle of check-clock-probe.sh, run on synthetic manifests."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        blocks = [block for block in python_blocks(PROBE.read_text()) if "def fail(text)" in block]
        self.assertEqual(len(blocks), 1)
        self.code = blocks[0]
        import platform
        self.machine = platform.machine()

    def manifest(self):
        variants = {}
        for name, content in (("default", b"\x7fELF-default"), ("intercept-syscall", b"\x7fELF-intercept")):
            path = self.directory / (name + ".so")
            path.write_bytes(content)
            variants[name] = {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
                              "intercept_syscall": name == "intercept-syscall",
                              "compile_flags": "-DINTERCEPT_SYSCALL" if name == "intercept-syscall" else ""}
        return {"schema": 1,
                "source": {"repository": "https://github.com/wolfcw/libfaketime", "tag": "v0.9.12",
                           "commit": "3ccdd344aadf1e317156fa5fb7c881f2c4404778", "tree": "a" * 40},
                "toolchain": {"compiler": "gcc 12", "libc": "ldd 2.36"}, "architecture": self.machine,
                "variants": variants}

    def run_oracle(self, manifest):
        text = manifest if isinstance(manifest, str) else json.dumps(manifest)
        return subprocess.run([sys.executable, "-I", "-c", self.code], input=text, capture_output=True,
                              text=True).returncode

    def test_a_consistent_manifest_passes(self):
        self.assertEqual(self.run_oracle(self.manifest()), 0)

    def test_manifest_defects_fail(self):
        def mutate(change):
            value = self.manifest()
            change(value)
            return value

        def tamper(value):
            Path(value["variants"]["default"]["path"]).write_bytes(b"\x7fELF-changed")

        def same_file(value):
            value["variants"]["intercept-syscall"] = dict(value["variants"]["default"], intercept_syscall=True)

        cases = {
            "not json": lambda: "not json",
            "wrong schema": lambda: mutate(lambda v: v.update(schema=2)),
            "short commit": lambda: mutate(lambda v: v["source"].update(commit="3ccdd344")),
            "no tree": lambda: mutate(lambda v: v["source"].pop("tree")),
            "other architecture": lambda: mutate(lambda v: v.update(architecture="m68k")),
            "no compiler": lambda: mutate(lambda v: v["toolchain"].pop("compiler")),
            "missing variant": lambda: mutate(lambda v: v["variants"].pop("intercept-syscall")),
            "extra variant": lambda: mutate(lambda v: v["variants"].update(other=v["variants"]["default"])),
            "intercept flag swapped": lambda: mutate(lambda v: v["variants"]["default"].update(intercept_syscall=True)),
            "intercept flag as string": lambda: mutate(lambda v: v["variants"]["default"].update(intercept_syscall="false")),
            "relative path": lambda: mutate(lambda v: v["variants"]["default"].update(path="default.so")),
            "missing file": lambda: mutate(lambda v: v["variants"]["default"].update(path=str(self.directory / "none.so"))),
            "wrong hash": lambda: mutate(lambda v: v["variants"]["default"].update(sha256="0" * 64)),
            "file changed after the manifest": lambda: mutate(tamper),
            "identical files": lambda: mutate(same_file),
            "missing compile_flags": lambda: mutate(lambda v: v["variants"]["default"].pop("compile_flags")),
            "missing compile_flags of the intercept variant":
                lambda: mutate(lambda v: v["variants"]["intercept-syscall"].pop("compile_flags")),
            "null compile_flags": lambda: mutate(lambda v: v["variants"]["default"].update(compile_flags=None)),
            "non-string compile_flags": lambda: mutate(lambda v: v["variants"]["default"].update(compile_flags=[])),
            "default variant declaring -DINTERCEPT_SYSCALL":
                lambda: mutate(lambda v: v["variants"]["default"].update(compile_flags="-DINTERCEPT_SYSCALL")),
            "intercept variant without -DINTERCEPT_SYSCALL":
                lambda: mutate(lambda v: v["variants"]["intercept-syscall"].update(compile_flags="")),
            "intercept variant with other flags":
                lambda: mutate(lambda v: v["variants"]["intercept-syscall"].update(
                    compile_flags="-DINTERCEPT_SYSCALL -DFAKE_SETTIME")),
            "null sha256": lambda: mutate(lambda v: v["variants"]["default"].update(sha256=None)),
            "missing sha256": lambda: mutate(lambda v: v["variants"]["default"].pop("sha256")),
            "missing path": lambda: mutate(lambda v: v["variants"]["default"].pop("path")),
            "null path": lambda: mutate(lambda v: v["variants"]["default"].update(path=None)),
            "null intercept_syscall": lambda: mutate(lambda v: v["variants"]["default"].update(intercept_syscall=None)),
            "null architecture": lambda: mutate(lambda v: v.update(architecture=None)),
            "null source": lambda: mutate(lambda v: v.update(source=None)),
            "null tag": lambda: mutate(lambda v: v["source"].update(tag=None)),
            "empty repository": lambda: mutate(lambda v: v["source"].update(repository="")),
            "null commit": lambda: mutate(lambda v: v["source"].update(commit=None)),
            "uppercase tree": lambda: mutate(lambda v: v["source"].update(tree="A" * 40)),
            "null toolchain": lambda: mutate(lambda v: v.update(toolchain=None)),
            "null libc": lambda: mutate(lambda v: v["toolchain"].update(libc=None)),
            "null variants": lambda: mutate(lambda v: v.update(variants=None)),
            "null variant": lambda: mutate(lambda v: v["variants"].update(default=None)),
            "variant list": lambda: mutate(lambda v: v.update(variants=[])),
            "json null": lambda: "null",
        }
        for name, value in cases.items():
            with self.subTest(name):
                self.assertNotEqual(self.run_oracle(value()), 0)

    def test_a_file_that_is_not_an_elf_object_fails(self):
        value = self.manifest()
        path = Path(value["variants"]["default"]["path"])
        path.write_bytes(b"text")
        value["variants"]["default"]["sha256"] = hashlib.sha256(b"text").hexdigest()
        self.assertNotEqual(self.run_oracle(value), 0)


class ImageDefinitionTests(unittest.TestCase):
    """Static properties of the reference helper's build."""

    def test_the_source_is_one_pinned_commit_that_the_build_verifies(self):
        dockerfile = (DIRECTORY / "Dockerfile").read_text()
        build = (DIRECTORY / "build-libfaketime.sh").read_text()
        self.assertRegex(dockerfile, r"ARG LIBFAKETIME_TAG=v0\.9\.12\n")
        self.assertRegex(dockerfile, r"ARG LIBFAKETIME_COMMIT=[0-9a-f]{40}\n")
        self.assertIn('test "$(git rev-parse HEAD)" = "$LIBFAKETIME_COMMIT"', build)

    def test_both_variants_differ_only_in_intercept_syscall(self):
        build = (DIRECTORY / "build-libfaketime.sh").read_text()
        self.assertIn("build default ''", build)
        self.assertIn("build intercept-syscall '-DINTERCEPT_SYSCALL'", build)
        self.assertEqual(build.count("-DINTERCEPT_SYSCALL"), 2)  # the build call and the manifest

    def test_the_base_is_the_pinned_base_of_the_other_images(self):
        pin = re.search(r"ARG BASE_IMAGE=(\S+)", (DIRECTORY / "Dockerfile").read_text()).group(1)
        for other in ("debian/Dockerfile", "extensions/capture/Dockerfile", "extensions/netem/Dockerfile"):
            self.assertIn("ARG BASE_IMAGE=" + pin, (Path(extension_check.HERE) / other).read_text())

    def test_the_helper_is_labelled_and_idles(self):
        dockerfile = (DIRECTORY / "Dockerfile").read_text()
        self.assertIn('io.ceph-testcontainers.extension="clock-probe"', dockerfile)
        self.assertIn('io.ceph-testcontainers.extension.contract="1"', dockerfile)
        self.assertIn('CMD ["sleep", "infinity"]', dockerfile)


if __name__ == "__main__":
    unittest.main()
