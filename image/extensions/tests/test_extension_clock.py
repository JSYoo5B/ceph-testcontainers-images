"""The clock helper's quick check, with Docker replaced by fakes and the
interposer replaced by synthetic ELF objects."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check

DIRECTORY = Path(extension_check.HERE) / "extensions" / "clock"
LOAD = Path(extension_check.EXTENSIONS["clock"]["probe"])
HELPER = {"reference": "ceph-check:extension-clock", "image_id": "sha256:" + "c" * 64, "platform": "linux/amd64"}
TARGET = {"reference": "quay.io/ceph/ceph:v20.2.4", "image_id": "sha256:" + "e" * 64, "platform": "linux/amd64"}
NEEDS = (("libc.so.6", ("GLIBC_2.2.5", "GLIBC_2.3", "GLIBC_2.34")),)


def elf(symbols=(("clock_gettime", True), ("time", True), ("dlsym", False)), needs=NEEDS, machine=62, kind=3):
    """A little-endian ELF64 object with a dynamic symbol table and version
    needs, as far as elf_dynamic() reads one."""
    strings = bytearray(b"\0")

    def add(text):
        offset = len(strings)
        strings.extend(text.encode() + b"\0")
        return offset

    symbol_table = bytearray(24)
    for name, defined in symbols:
        symbol_table += struct.pack("<IBBHQQ", add(name), (1 << 4) | 2, 0, 12 if defined else 0, 0, 0)
    version_needs = bytearray()
    for number, (library, versions) in enumerate(needs):
        following = 16 + 16 * len(versions) if number < len(needs) - 1 else 0
        version_needs += struct.pack("<HHIII", 1, len(versions), add(library), 16, following)
        for position, version in enumerate(versions):
            version_needs += struct.pack("<IHHII", 0, 0, 0, add(version), 16 if position < len(versions) - 1 else 0)
    offsets = [64, 64 + len(strings), 64 + len(strings) + len(symbol_table)]
    header_offset = offsets[2] + len(version_needs)
    sections = [bytes(64),
                struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, offsets[0], len(strings), 0, 0, 1, 0),
                struct.pack("<IIQQQQIIQQ", 0, 11, 0, 0, offsets[1], len(symbol_table), 1, 1, 8, 24),
                struct.pack("<IIQQQQIIQQ", 0, 0x6ffffffe, 0, 0, offsets[2], len(version_needs), 1, len(needs), 8, 0)]
    header = b"\x7fELF" + bytes([2, 1, 1, 0]) + bytes(8) + struct.pack(
        "<HHIQQQIHHHHHH", kind, machine, 1, 0, 0, header_offset, 0, 64, 0, 0, 64, len(sections), 0)
    return header + bytes(strings) + bytes(symbol_table) + bytes(version_needs) + b"".join(sections)


def manifest(data, **interposer):
    value = {"schema": 1,
             "source": {"repository": "https://github.com/wolfcw/libfaketime", "tag": "v0.9.12",
                        "commit": "3ccdd344aadf1e317156fa5fb7c881f2c4404778", "tree": "a" * 40},
             "toolchain": {"compiler": "gcc (Debian 12.2.0-14) 12.2.0", "libc": "ldd (Debian GLIBC 2.36-9) 2.36"},
             "architecture": "x86_64",
             "interposer": {"build": "default", "path": extension_check.CLOCK_INTERPOSER,
                            "sha256": hashlib.sha256(data).hexdigest(), "intercept_syscall": False,
                            "compile_flags": ""}}
    value["interposer"].update(interposer)
    return value


def filesystem(data=None, value=None, extra=()):
    """The members of an exported helper filesystem."""
    data = elf() if data is None else data
    value = manifest(data) if value is None else value
    members = {"/opt": {"type": "directory", "mode": 0o755},
               "/opt/ceph-testcontainers/clock": {"type": "directory", "mode": 0o755},
               extension_check.CLOCK_MANIFEST: {"type": "file", "mode": 0o644, "data": json.dumps(value).encode()},
               extension_check.CLOCK_INTERPOSER: {"type": "file", "mode": 0o644, "data": data},
               "/usr/share/doc/libfaketime/copyright": {"type": "file", "mode": 0o644},
               "/usr/share/ceph-testcontainers/SOURCES.txt": {"type": "file", "mode": 0o644},
               # The engine's own files of every container.
               "/.dockerenv": {"type": "file", "mode": 0o755},
               "/etc/hosts": {"type": "file", "mode": 0o644},
               "/dev/console": {"type": "file", "mode": 0o755}}
    for path, entry in extra:
        members[path] = entry
    return members


def failed(members, platform="linux/amd64"):
    checks, _, interposer = extension_check.clock_checks(members, platform)
    return sorted(entry["name"] for entry in checks if entry["status"] != "passed"), interposer


class ClockContractTests(unittest.TestCase):

    def test_the_helper_is_published_and_gated_by_its_quick_check(self):
        self.assertIn("clock", extension_check.PUBLISHED)
        self.assertEqual(extension_check.EXTENSIONS["clock"]["gate"], "quick")
        self.assertEqual(extension_check.EXTENSIONS["clock"]["contract"], "1")
        args = extension_check.arguments(["--extension", "clock", "--image", "x", "--target", "t"])
        self.assertEqual((args.extension, args.target, args.functional), ("clock", ["t"], False))
        self.assertEqual(extension_check.arguments(["--extension", "clock", "--image", "x"]).target, [])
        for extra in (["--functional", "--target", "t"], ["--rate"], ["--packet-faults"],
                      ["--selector", "ipv4-tcp-u32", "--functional", "--target", "t"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                extension_check.arguments(["--extension", "clock", "--image", "x", *extra])
        # The other quick-only helpers still refuse a target.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            extension_check.arguments(["--extension", "blockio", "--image", "x", "--target", "t"])

    def test_the_checks_are_exactly_the_contract_checks(self):
        checks, versions, interposer = extension_check.clock_checks(filesystem(), "linux/amd64")
        self.assertEqual([entry["name"] for entry in checks], list(extension_check.QUICK_CHECKS["clock"]))
        self.assertTrue(all(entry["status"] == "passed" for entry in checks), checks)
        self.assertEqual(interposer, elf())
        self.assertEqual(versions, {"libfaketime": "v0.9.12 3ccdd344aadf1e317156fa5fb7c881f2c4404778",
                                    "interposer_sha256": hashlib.sha256(elf()).hexdigest(),
                                    "glibc_required": "GLIBC_2.34"})
        # Every failure also fails the same named checks, never fewer names.
        checks, _, _ = extension_check.clock_checks({}, "linux/amd64")
        self.assertEqual([entry["name"] for entry in checks], list(extension_check.QUICK_CHECKS["clock"]))

    def test_the_arm64_interposer_is_judged_by_its_own_machine(self):
        data = elf(machine=183, needs=(("libc.so.6", ("GLIBC_2.17", "GLIBC_2.34")),))
        value = manifest(data)
        value["architecture"] = "aarch64"
        self.assertEqual(failed(filesystem(data, value), "linux/arm64"), ([], data))
        self.assertEqual(set(failed(filesystem(data, value), "linux/amd64")[0]),
                         {"manifest", "manifest:default-build", "hash", "elf", "symbol:syscall", "glibc-bound"})

    def test_a_helper_that_carries_the_intercept_syscall_build_fails(self):
        # The build that exports syscall(), whatever its manifest says.
        intercept = elf(symbols=(("clock_gettime", True), ("syscall", True)))
        self.assertEqual(failed(filesystem(intercept)), (["symbol:syscall"], None))
        # A manifest that declares it.
        for change in ({"build": "intercept-syscall"}, {"intercept_syscall": True}, {"intercept_syscall": None},
                       {"compile_flags": "-DINTERCEPT_SYSCALL"}, {"compile_flags": None}):
            with self.subTest(change=change):
                self.assertEqual(failed(filesystem(value=manifest(elf(), **change)))[0], ["manifest:default-build"])
        # The intercept-syscall build carried next to the default build.
        for path in (extension_check.CLOCK_ROOT + "/intercept-syscall/libfaketime.so.1",
                     "/usr/lib/libfaketime.so.1", extension_check.CLOCK_ROOT + "/libfaketimeMT.so.1"):
            with self.subTest(path=path):
                extra = ((path, {"type": "file", "mode": 0o644}),)
                self.assertEqual(failed(filesystem(extra=extra))[0], ["interposer:single"])
        # A manifest of the clock-probe kind, with two variants.
        value = manifest(elf())
        value["variants"] = {"default": value["interposer"], "intercept-syscall": value["interposer"]}
        self.assertEqual(failed(filesystem(value=value))[0], ["hash", "manifest", "manifest:default-build"])

    def test_a_manifest_hash_that_does_not_match_the_file_fails(self):
        self.assertEqual(failed(filesystem(value=manifest(elf(), sha256="0" * 64))), (["hash"], None))
        # The file changed after the manifest was written.
        self.assertEqual(failed(filesystem(data=elf(symbols=(("time", True),)), value=manifest(elf()))),
                         (["hash"], None))

    def test_an_interposer_that_needs_a_newer_glibc_fails(self):
        for versions, newest in ((("GLIBC_2.2.5", "GLIBC_2.35"), "GLIBC_2.35"), (("GLIBC_2.38",), "GLIBC_2.38"),
                                 (("GLIBC_2.34", "GLIBC_3.0"), "GLIBC_3.0")):
            with self.subTest(versions=versions):
                data = elf(needs=(("libc.so.6", versions),))
                checks, recorded, interposer = extension_check.clock_checks(filesystem(data), "linux/amd64")
                self.assertEqual([entry["name"] for entry in checks if entry["status"] != "passed"], ["glibc-bound"])
                self.assertIsNone(interposer)
                self.assertEqual(recorded["glibc_required"], newest)
        # A version need beyond glibc's own versions, or of a second library.
        for needs in ((("libc.so.6", ("GLIBC_PRIVATE",)),),
                      (("libc.so.6", ("GLIBC_2.2.5",)), ("libm.so.6", ("GLIBC_2.35",))),
                      (("libstdc++.so.6", ("GLIBCXX_3.4",)),)):
            with self.subTest(needs=needs):
                self.assertEqual(failed(filesystem(elf(needs=needs)))[0], ["glibc-bound"])
        self.assertEqual(extension_check.glibc_problem([("libc.so.6", "GLIBC_2.34")]), (None, "GLIBC_2.34"))
        self.assertEqual(extension_check.glibc_problem([("libc.so.6", "GLIBC_2.2.5")]), (None, "GLIBC_2.2.5"))
        self.assertEqual(extension_check.glibc_problem([]), (None, None))

    def test_files_manifest_and_object_defects_fail(self):
        cases = {
            "no manifest": (filesystem(extra=((extension_check.CLOCK_MANIFEST, {"type": "directory", "mode": 0o755}),)),
                            {"file:manifest", "manifest", "manifest:default-build", "hash"}),
            "symlinked interposer": (filesystem(extra=((extension_check.CLOCK_INTERPOSER,
                                                        {"type": "symlink", "mode": 0o777}),)),
                                     {"file:interposer", "hash", "elf", "symbol:syscall", "glibc-bound"}),
            "an executable file": (filesystem(extra=(("/bin/sh", {"type": "file", "mode": 0o755}),)),
                                   {"executables:none"}),
            "an executable interposer": (filesystem(extra=((extension_check.CLOCK_INTERPOSER,
                                                            {"type": "file", "mode": 0o755, "data": elf()}),)),
                                         {"executables:none"}),
            "a manifest that is not JSON": (filesystem(extra=((extension_check.CLOCK_MANIFEST,
                                                              {"type": "file", "mode": 0o644, "data": b"{"}),)),
                                            {"manifest", "manifest:default-build", "hash"}),
            "not an ELF object": (filesystem(b"text", manifest(b"text")), {"elf", "symbol:syscall", "glibc-bound"}),
            "a truncated ELF object": (filesystem(elf()[:200], manifest(elf()[:200])),
                                       {"elf", "symbol:syscall", "glibc-bound"}),
            "an executable, not a shared object": (filesystem(elf(kind=2), manifest(elf(kind=2))), {"elf",
                                                   "symbol:syscall", "glibc-bound"}),
            "another machine": (filesystem(elf(machine=183), manifest(elf(machine=183))),
                                {"elf", "symbol:syscall", "glibc-bound"}),
        }
        for mutation in (lambda v: v.update(schema=2), lambda v: v.update(architecture="aarch64"),
                         lambda v: v["source"].update(commit="3ccdd344"), lambda v: v["source"].pop("tree"),
                         lambda v: v["toolchain"].update(libc=""), lambda v: v.update(toolchain=None),
                         lambda v: v["interposer"].update(path="/libfaketime.so.1"),
                         lambda v: v["interposer"].update(sha256=None), lambda v: v.update(interposer=[]),
                         lambda v: v.update(extra=1), lambda v: v.pop("source")):
            value = manifest(elf())
            mutation(value)
            cases["manifest " + json.dumps(value, sort_keys=True)[:80]] = (
                filesystem(value=value), {"manifest", "manifest:default-build", "hash"})
        cases["json null"] = (filesystem(extra=((extension_check.CLOCK_MANIFEST,
                                                 {"type": "file", "mode": 0o644, "data": b"null"}),)),
                              {"manifest", "manifest:default-build", "hash"})
        for name, (members, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(set(failed(members)[0]), expected)
                self.assertIsNone(failed(members)[1])

    def test_the_parser_reads_a_real_shared_object(self):
        # The C library of the running Python on Linux exports both.
        if not Path("/proc/self/maps").exists():
            self.skipTest("needs Linux")
        paths = {line.split()[-1] for line in Path("/proc/self/maps").read_text().splitlines()
                 if re.search(r"/libc[.-][^/]*so", line.split()[-1])}
        self.assertTrue(paths)
        parsed = extension_check.elf_dynamic(Path(sorted(paths)[0]).read_bytes())
        self.assertEqual(parsed["type"], 3)
        self.assertIn("clock_gettime", parsed["symbols"])
        self.assertIn("syscall", parsed["symbols"])

    def test_the_members_of_an_export_are_read_with_the_two_files(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "export.tar"
            with tarfile.open(archive, "w") as tar:
                for name, data, mode in (("opt/ceph-testcontainers/clock/build.json", b"{}", 0o644),
                                         ("opt/ceph-testcontainers/clock/libfaketime.so.1", b"\x7fELF", 0o644),
                                         ("usr/bin/env", b"#!", 0o755), ("./.dockerenv", b"", 0o755)):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), mode
                    tar.addfile(info, io.BytesIO(data))
                link = tarfile.TarInfo("lib")
                link.type, link.linkname = tarfile.SYMTYPE, "usr/lib"
                tar.addfile(link)
            members = extension_check.clock_members(archive)
        self.assertEqual(members[extension_check.CLOCK_MANIFEST]["data"], b"{}")
        self.assertEqual(members[extension_check.CLOCK_INTERPOSER]["data"], b"\x7fELF")
        self.assertEqual(members["/usr/bin/env"], {"type": "file", "mode": 0o755})
        self.assertEqual(members["/lib"]["type"], "symlink")
        self.assertEqual(members["/.dockerenv"], {"type": "file", "mode": 0o755})


def samples(**changes):
    """Samples of check-clock-load.py from a working interposer."""
    def clocks(realtime, monotonic):
        return {"realtime": realtime, "monotonic": monotonic}
    measured = {"interposer": extension_check.CLOCK_LOAD_PATH, "python": "3.9.21", "samples": []}
    for number, (name, stamp, offset) in enumerate(extension_check.CLOCK_SAMPLES):
        start = 1000.0 + number
        measured["samples"].append({"name": name, "stamp": stamp, "exit_code": 0, "stderr": "",
                                    "before": clocks(1.7e9 + start, start), "after": clocks(1.7e9 + start + 0.2, start + 0.2),
                                    "child": dict(clocks(1.7e9 + start + 0.1 + (offset or 0), start + 0.1),
                                                  loaded=offset is not None)})
    for name, change in changes.items():
        sample = next(entry for entry in measured["samples"] if entry["name"] == name)
        change(sample)
    return measured


class LoadJudgementTests(unittest.TestCase):

    def test_samples_of_a_working_interposer_pass(self):
        self.assertEqual(extension_check.clock_load_problems(samples()), [])

    def test_load_defects_fail(self):
        cases = {
            "not mapped": samples(**{"+3600": lambda s: s["child"].update(loaded=False)}),
            "mapped without LD_PRELOAD": samples(reference=lambda s: s["child"].update(loaded=True)),
            "no shift": samples(**{"+3600": lambda s: s["child"].update(realtime=s["before"]["realtime"] + 0.1)}),
            "shift without a file": samples(reference=lambda s: s["child"].update(realtime=s["child"]["realtime"] + 60)),
            "shift at +0": samples(**{"+0": lambda s: s["child"].update(realtime=s["child"]["realtime"] - 10)}),
            "faked monotonic": samples(**{"+3600": lambda s: s["child"].update(monotonic=s["child"]["monotonic"] + 3600)}),
            "monotonic before the reference read": samples(**{"+0": lambda s: s["child"].update(
                monotonic=s["before"]["monotonic"] - 0.01)}),
            "a hanging child": samples(**{"+0": lambda s: (s.pop("child"), s.update(error="timeout"))}),
            "a failing child": samples(**{"+0": lambda s: s.update(exit_code=127)}),
            "a boolean clock": samples(**{"+0": lambda s: s["child"].update(realtime=True)}),
            "a missing clock": samples(**{"+0": lambda s: s["after"].pop("monotonic")}),
            "another stamp": samples(**{"+3600": lambda s: s.update(stamp="+60")}),
        }
        missing = samples()
        missing["samples"].pop()
        cases["a missing sample"] = missing
        other = samples()
        other["interposer"] = "/usr/lib/libfaketime.so.1"
        cases["another path"] = other
        cases["no measurement"] = None
        for name, measured in cases.items():
            with self.subTest(name):
                self.assertTrue(extension_check.clock_load_problems(measured))

    def test_the_load_script_measures_a_child_with_and_without_the_file(self):
        # Without any interposer, the script still measures: the reference
        # passes and the instrumented samples report that nothing is mapped.
        if not Path("/proc/self/maps").exists():
            self.skipTest("needs Linux")
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "libfaketime.so.1")
            result = subprocess.run([sys.executable, "-I", "-", missing], input=LOAD.read_text(), capture_output=True,
                                    text=True, timeout=120, env={"PATH": os.environ.get("PATH", ""),
                                                                 "FAKETIME": "+1d", "LD_PRELOAD": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        measured = json.loads(result.stdout.splitlines()[-1])
        problems = extension_check.clock_load_problems(measured, missing)
        self.assertEqual([problem for problem in problems if problem.startswith("reference")], [])
        self.assertIn("+3600: the interposer is not mapped", problems)

    def test_the_load_script_never_sets_faketime(self):
        script = LOAD.read_text()
        self.assertNotIn("FAKETIME=", script)
        self.assertIn('not key.startswith("FAKETIME")', script)
        self.assertIn('FAKETIME_DONT_FAKE_MONOTONIC="1"', script)
        self.assertIn("FAKETIME_TIMESTAMP_FILE=stamp", script)
        self.assertEqual(subprocess.run([sys.executable, "-m", "py_compile", str(LOAD)]).returncode, 0)


class OrchestrationTests(unittest.TestCase):
    """The Docker commands of the quick and the load check, with fakes."""

    def test_the_quick_check_creates_and_exports_without_starting_a_container(self):
        commands = []

        def docker(*command, timeout=120, check_exit=True):
            commands.append(command)
            return 0, ""

        image = dict(HELPER)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check, "clock_members", return_value=filesystem()), \
                mock.patch.object(extension_check, "remove_container",
                                  return_value={"status": "passed", "errors": []}) as remove:
            interposer = extension_check.clock_quick(image, mock.Mock(probe_timeout=10), Path(directory))
            log = (Path(directory) / "quick.log").read_text()
        self.assertEqual(interposer, elf())
        self.assertEqual([command[0] for command in commands], ["create", "export"])
        self.assertIn("--network=none", commands[0])
        self.assertEqual(remove.call_count, 1)
        self.assertEqual(image["status"], "passed")
        self.assertIn("CHECK\tpassed\tglibc-bound\n", log)

    def test_a_failed_quick_check_returns_no_interposer(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(extension_check, "docker", return_value=(0, "")), \
                mock.patch.object(extension_check, "clock_members",
                                  return_value=filesystem(elf(symbols=(("syscall", True),)))), \
                mock.patch.object(extension_check, "remove_container", return_value={"status": "passed", "errors": []}):
            image = dict(HELPER)
            self.assertIsNone(extension_check.clock_quick(image, mock.Mock(probe_timeout=10), Path(directory)))
            self.assertEqual(image["status"], "failed")
            self.assertIn("CHECK\tfailed\tsymbol:syscall\texports syscall\n", (Path(directory) / "quick.log").read_text())

    def run_load(self, stdout, returncode=0):
        commands, started = [], []

        def docker(*command, timeout=120, check_exit=True):
            commands.append(command)
            if command[0] == "cp":
                self.assertEqual(Path(command[1]).read_bytes(), elf())
            return 0, ""

        def run(command, **kwargs):
            started.append((command, kwargs))
            return mock.Mock(returncode=returncode, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check.subprocess, "run", side_effect=run), \
                mock.patch.object(extension_check, "cleanup",
                                  return_value={"status": "passed", "leftovers": [], "errors": []}) as cleanup:
            result = extension_check.clock_load(HELPER, TARGET, "abc123", elf(), 10, Path(directory) / "load")
            left = sorted(path.name for path in (Path(directory) / "load").iterdir())
        cleanup.assert_called_once()
        self.assertEqual(cleanup.call_args.args[0], extension_check.LABEL + "=abc123")
        return result, commands, started, left

    def test_the_load_check_copies_the_interposer_into_an_isolated_target_container(self):
        result, commands, started, left = self.run_load(json.dumps(samples()) + "\n")
        self.assertEqual([command[0] for command in commands], ["create", "cp"])
        create = commands[0]
        for option in ("--network=none", "--cap-drop=ALL", "--user=0:0", "--pull=never", "--entrypoint=python3"):
            self.assertIn(option, create)
        self.assertFalse(any(arg.startswith(("--cap-add", "--privileged", "--volume", "--mount")) for arg in create))
        self.assertEqual(create[create.index(TARGET["image_id"]) + 1:], ("-I", "-", extension_check.CLOCK_LOAD_PATH))
        self.assertEqual(commands[1][2], "ceph-extension-check-abc123:" + extension_check.CLOCK_LOAD_PATH)
        self.assertEqual(started[0][1]["input"], LOAD.read_text())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["interposer_sha256"], hashlib.sha256(elf()).hexdigest())
        self.assertEqual(result["helper_image_id"], HELPER["image_id"])
        # The copy of the interposer does not stay in the report directory.
        self.assertEqual(left, ["load.log"])

    def test_a_failed_load_fails_with_its_problems(self):
        result, _, _, _ = self.run_load(json.dumps(samples(**{"+3600": lambda s: s["child"].update(loaded=False)})))
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "load_failure"))
        self.assertIn("+3600: the interposer is not mapped", result["problems"])
        for stdout, code in (("not json", 0), ("", 0), (json.dumps(samples()), 1)):
            with self.subTest(stdout=stdout, code=code):
                self.assertEqual(self.run_load(stdout, code)[0]["status"], "failed")

    def test_a_failed_quick_check_loads_nothing(self):
        report = {"targets": {}}
        args = mock.Mock(target=[TARGET["reference"]], platform="linux/amd64", probe_timeout=10)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(extension_check, "clock_quick", return_value=None), \
                mock.patch.object(extension_check, "clock_load") as load, self.assertRaises(extension_check.CheckError):
            image = dict(HELPER, cleanup={"status": "passed", "errors": []})
            extension_check.clock_contract(args, image, report, Path(directory))
        load.assert_not_called()


class ImageDefinitionTests(unittest.TestCase):
    """Static properties of the reference helper's build."""

    def test_the_helper_pins_the_libfaketime_commit_of_the_clock_probe_helper(self):
        pins = []
        for helper in ("clock", "clock-probe"):
            dockerfile = (DIRECTORY.parent / helper / "Dockerfile").read_text()
            pins.append((re.search(r"ARG LIBFAKETIME_TAG=(\S+)\n", dockerfile).group(1),
                         re.search(r"ARG LIBFAKETIME_COMMIT=([0-9a-f]{40})\n", dockerfile).group(1)))
        self.assertEqual(pins[0], pins[1])
        build = (DIRECTORY / "build-interposer.sh").read_text()
        self.assertIn('test "$(git rev-parse HEAD)" = "$LIBFAKETIME_COMMIT"', build)

    def test_only_the_default_build_is_installed_without_an_execute_bit(self):
        build = (DIRECTORY / "build-interposer.sh").read_text()
        code = "\n".join(line for line in build.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("INTERCEPT_SYSCALL", code.replace('"intercept_syscall": False', ""))
        self.assertIn("make -C src FAKETIME_COMPILE_CFLAGS='' libfaketime.so.1", code)
        self.assertEqual(code.count("make -C src"), 1)
        self.assertIn('install -D -m 0644 src/libfaketime.so.1 "$root/libfaketime.so.1"', code)
        self.assertIn('path = "%s"' % extension_check.CLOCK_INTERPOSER, code)
        self.assertIn("root=$OUTPUT" + extension_check.CLOCK_ROOT + "\n", code)

    def test_the_published_stage_has_no_base_and_no_entry(self):
        dockerfile = (DIRECTORY / "Dockerfile").read_text()
        stages = re.findall(r"^FROM (\S+)", dockerfile, re.M)
        self.assertEqual(stages, ["${BASE_IMAGE}", "scratch"])
        final = dockerfile[dockerfile.index("FROM scratch"):]
        self.assertEqual(re.findall(r"^(COPY|RUN|CMD|ENTRYPOINT|USER)\b.*", final, re.M), ["COPY"])
        self.assertIn("COPY --from=build /out/ /", final)
        self.assertIn('io.ceph-testcontainers.extension="clock"', final)
        self.assertIn('io.ceph-testcontainers.extension.contract="1"', final)

    def test_the_build_stage_uses_the_pinned_base_of_the_other_images(self):
        pin = re.search(r"ARG BASE_IMAGE=(\S+)", (DIRECTORY / "Dockerfile").read_text()).group(1)
        for other in ("debian/Dockerfile", "extensions/clock-probe/Dockerfile", "extensions/blockio/Dockerfile"):
            self.assertIn("ARG BASE_IMAGE=" + pin, (Path(extension_check.HERE) / other).read_text())


if __name__ == "__main__":
    unittest.main()
