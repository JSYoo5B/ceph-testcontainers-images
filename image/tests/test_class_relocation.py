"""Real Linux ELF fixtures for the object-class relocation contract."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "linux" and shutil.which("cc"), "Linux ELF compiler required")
class ClassRelocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        source = Path(__file__).resolve().parents[1] / "check-runtime.sh"
        script = source.read_text()
        self.functions = script[script.index("    class_libc() {"):script.index("    class_load() {")]
        self.compile("ceph-osd", "int osd_data=7; int osd_export(void){return 3;} int main(void){return 0;}", shared=False)
        libc = self.shell("class_libc")
        if libc.returncode or libc.stdout.strip() != "glibc":
            self.skipTest("glibc fixtures require a recognized glibc interpreter")

    def compile(self, name, source, shared=True, flags=()):
        path = self.directory / name
        cfile = self.directory / (name + ".c")
        cfile.write_text(source)
        options = ["-shared", "-fPIC"] if shared else ["-Wl,--export-dynamic"]
        subprocess.run(["cc", *options, str(cfile), *flags, "-o", str(path)], check=True, capture_output=True)
        return path

    def shell(self, command, library=None, prefix=""):
        env = os.environ.copy()
        env["PATH"] = str(self.directory) + os.pathsep + env["PATH"]
        program = 'ulimit -c 0\nwork=$1\n' + self.functions + '\n' + prefix + '\n' + command
        return subprocess.run(["/bin/sh", "-c", program, "sh", str(self.directory), str(library or "")],
                              env=env, capture_output=True, text=True, timeout=20)

    def probe(self, library, prefix=""):
        return self.shell('class_relocate "$2"', library, prefix)

    def native(self, library):
        return self.shell('LD_BIND_NOW=1 LD_PRELOAD="$2" ceph-osd --version', library)

    def test_osd_function_and_data_exports_resolve(self):
        library = self.compile("valid.so", "extern int osd_data; extern int osd_export(void); int plugin(void){return osd_data+osd_export();}")
        self.assertEqual(self.native(library).returncode, 0)
        result = self.probe(library)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_strong_unresolved_function_and_data_fail(self):
        for name, source in {
            "function": "extern int absent(void); int plugin(void){return absent();}",
            "data": "extern int absent; int plugin(void){return absent;}",
        }.items():
            with self.subTest(name=name):
                library = self.compile(name + ".so", source)
                self.assertNotEqual(self.native(library).returncode, 0)
                raw = self.shell('LD_TRACE_LOADED_OBJECTS=1 LD_WARN=1 LD_BIND_NOW=1 LD_PRELOAD="$2" ceph-osd', library)
                self.assertEqual(raw.returncode, 0, raw.stderr)
                self.assertIn("undefined symbol", raw.stderr)
                self.assertNotEqual(self.probe(library).returncode, 0)

    def test_missing_needed_dependency_fails(self):
        dependency = self.compile("libfixturedep.so", "int dependency(void){return 1;}")
        library = self.compile("needed.so", "extern int dependency(void); int plugin(void){return dependency();}",
                               flags=("-L" + str(self.directory), "-lfixturedep", "-Wl,-rpath," + str(self.directory)))
        dependency.unlink()
        self.assertNotEqual(self.probe(library).returncode, 0)

    def test_missing_and_invalid_preloads_fail(self):
        invalid = self.directory / "invalid.so"
        invalid.write_text("not an ELF library\n")
        wrong = self.compile("wrong-class.so", "int plugin(void){return 1;}")
        contents = bytearray(wrong.read_bytes())
        contents[4] = 1 if contents[4] == 2 else 2
        wrong.write_bytes(contents)
        for library in (invalid, wrong, self.directory / "absent.so"):
            with self.subTest(library=library.name):
                self.assertNotEqual(self.probe(library).returncode, 0)

    def test_constructor_and_ifunc_are_intentionally_not_executed(self):
        fixtures = {
            "constructor": "__attribute__((constructor)) static void init(void){__builtin_trap();} int plugin(void){return 1;}",
            "ifunc": 'static void *resolve(void){__builtin_trap();} int target(void) __attribute__((ifunc("resolve"))); int plugin(void){return target();}',
        }
        for name, source in fixtures.items():
            with self.subTest(name=name):
                library = self.compile(name + ".so", source)
                self.assertNotEqual(self.native(library).returncode, 0)
                result = self.probe(library)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unknown_libc_and_missing_trace_fail_closed(self):
        library = self.compile("valid.so", "int plugin(void){return 1;}")
        self.assertNotEqual(self.probe(library, 'class_libc() { return 1; }').returncode, 0)
        # A successful command that ignored LD_TRACE must not pass.
        result = self.probe(library, "ceph-osd() { printf 'ceph version fixture\\n'; }")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_trace_errors_in_either_stream_fail(self):
        library = self.compile("valid.so", "int plugin(void){return 1;}")
        for destination in ("", ">&2"):
            with self.subTest(destination=destination):
                prefix = """ceph-osd() { printf '%s (0x1234)\\n' "$LD_PRELOAD"; printf 'undefined symbol: absent\\n' """ + destination + "; }"
                self.assertNotEqual(self.probe(library, prefix).returncode, 0)

    def test_musl_branch_preserves_native_constructor_execution(self):
        library = self.compile("ctor.so", "__attribute__((constructor)) static void init(void){__builtin_trap();}")
        result = self.probe(library, 'class_libc() { printf "musl\\n"; }')
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
