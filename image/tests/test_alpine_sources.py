"""Verify Alpine source provenance from installed APK database records."""
import os
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "alpine" / "write-sources.sh"
DOCKERFILE = SCRIPT.with_name("Dockerfile")


class AlpineStackTests(unittest.TestCase):
    def patch(self, directory, data):
        binary = directory / "mirror"
        binary.write_bytes(data)
        binary.chmod(0o755)
        script = DOCKERFILE.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        result = subprocess.run([sys.executable, "-", str(binary), str(directory / "notice")],
                                input=script, text=True, capture_output=True)
        return result, binary

    def elf(self, machine, stack=True):
        data = bytearray((i % 251 for i in range(256)))
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", data, 18, machine)
        struct.pack_into("<Q", data, 32, 64)
        struct.pack_into("<HH", data, 54, 56, 2)
        struct.pack_into("<I", data, 64, 1)
        struct.pack_into("<II", data, 120, 0x6474e551 if stack else 1, 6)
        struct.pack_into("<Q", data, 160, 0)
        return bytes(data)

    def test_changes_only_stack_size_and_preserves_non_executable_stack_and_file_mode(self):
        for machine in (62, 183):
            with self.subTest(machine=machine), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                original = self.elf(machine)
                result, binary = self.patch(root, original)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = bytearray(original)
                struct.pack_into("<Q", expected, 160, 8 * 1024 * 1024)
                self.assertEqual(binary.read_bytes(), expected)
                self.assertEqual(binary.stat().st_mode & 0o777, 0o755)
                self.assertEqual(struct.unpack_from("<I", binary.read_bytes(), 124)[0], 6)
                notice = json.loads((root / "notice/modifications.json").read_text())
                self.assertEqual((notice["before_bytes"], notice["after_bytes"]), (0, 8 * 1024 * 1024))

    def test_missing_stack_header_or_unsupported_architecture_never_modifies_binary(self):
        for original in (self.elf(62, stack=False), self.elf(3)):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result, binary = self.patch(root, original)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(binary.read_bytes(), original)
                self.assertFalse((root / "notice/modifications.json").exists())


class AlpineSourcesTests(unittest.TestCase):
    def test_subpackages_keep_origin_license_commit_and_complete_project_url(self):
        database = """P:py3-rados19
V:19.2.4-r1
o:ceph19
L:LGPL-2.1-only AND BSD-3-Clause
c:0123456789abcdef
U:https://ceph.io/en/

P:libc-utils
V:1.2.5-r1
o:musl
L:MIT
c:fedcba9876543210
U:https://musl.libc.org
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "installed"
            installed.write_text(database)
            output = root / "notice"
            env = dict(os.environ, APK_INSTALLED_DB=str(installed), SOURCE_NOTICE_DIR=str(output),
                       ALPINE_BRANCH="v3.24", CEPH_RELEASE="19.2.4")
            subprocess.run(["sh", str(SCRIPT)], env=env, check=True)
            rows = [line.split("\t") for line in (output / "source-packages.txt").read_text().splitlines()[1:]]
            self.assertEqual(rows, [
                ["libc-utils", "1.2.5-r1", "musl", "MIT", "fedcba9876543210", "https://musl.libc.org"],
                ["py3-rados19", "19.2.4-r1", "ceph19", "LGPL-2.1-only AND BSD-3-Clause",
                 "0123456789abcdef", "https://ceph.io/en/"],
            ])
            notice = (output / "SOURCES.txt").read_text()
            self.assertIn("Alpine v3.24", notice)
            self.assertIn("https://github.com/ceph/ceph/tree/v19.2.4", notice)
            self.assertIn("<aports-commit>", notice)


if __name__ == "__main__":
    unittest.main()
