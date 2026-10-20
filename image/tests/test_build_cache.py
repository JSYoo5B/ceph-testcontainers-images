"""Exercise Make's real build commands without an engine or registry."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
ROLES = ("control", "osd", "rgw", "mds", "all")


class RegistryCacheTests(unittest.TestCase):
    def run_make(self, target, reference="", write="false", platform="linux/amd64", fail_role=""):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            docker = directory / "docker"
            docker.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['BUILD_COMMANDS'], 'a') as log:
    log.write(json.dumps(sys.argv[1:]) + '\\n')
if '--target' in sys.argv and sys.argv[sys.argv.index('--target') + 1] == os.environ['FAIL_ROLE']:
    sys.exit(42)
''')
            docker.chmod(0o755)
            log = directory / "commands.jsonl"
            env = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                       BUILD_COMMANDS=str(log), FAIL_ROLE=fail_role)
            result = subprocess.run([
                "make", "--no-print-directory", "-s", target,
                "CEPH_RELEASE=20.2.4", "PLATFORM=" + platform, "IMAGE_REPOSITORY=cache-test",
                "BUILD_CACHE_REF=" + reference, "BUILD_CACHE_WRITE=" + write,
            ], cwd=ROOT, env=env, text=True, capture_output=True)
            commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return result, commands

    def cache_options(self, command):
        return [arg for arg in command if arg.startswith(("--cache-from=", "--cache-to="))]

    def test_no_registry_access_without_an_opt_in_reference(self):
        for target in ("debian-images", "ubuntu-images", "extension-images"):
            with self.subTest(target=target):
                result, commands = self.run_make(target)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(commands), 1 if target == "extension-images" else 5)
                for command in commands:
                    self.assertEqual(self.cache_options(command), [])
                    self.assertIn("--load", command)
                    self.assertNotIn("--push", command)

    def test_distribution_caches_are_distinct_per_role_and_readers_cannot_export(self):
        for distribution in ("debian", "ubuntu"):
            for architecture in ("amd64", "arm64"):
                for write in ("false", "true"):
                    with self.subTest(distribution=distribution, architecture=architecture, write=write):
                        prefix = "registry.example/cache:buildcache-%s-20.2.4-%s" % (distribution, architecture)
                        result, commands = self.run_make(distribution + "-images", prefix, write,
                                                         "linux/" + architecture)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(len(commands), 5)
                        for role, command in zip(ROLES, commands):
                            reference = prefix + "-" + role
                            expected = ["--cache-from=type=registry,ref=" + reference]
                            if write == "true":
                                expected += ["--cache-to=type=registry,ref=" + reference +
                                             ",mode=max,ignore-error=true"]
                            self.assertEqual(self.cache_options(command), expected)
                            self.assertEqual(command[command.index("--target") + 1], role)
                            self.assertEqual(command[command.index("--platform") + 1], "linux/" + architecture)
                            self.assertEqual(command[command.index("-t") + 1],
                                             "cache-test:%s-20.2.4-%s" % (distribution, role))
                            self.assertIn("--load", command)
                            self.assertNotIn("--push", command)

    def test_extension_cache_uses_its_own_reference(self):
        for write in ("false", "true"):
            with self.subTest(write=write):
                reference = "registry.example/cache:buildcache-netem-amd64"
                result, commands = self.run_make("extension-images", reference, write)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(commands), 1)
                expected = ["--cache-from=type=registry,ref=" + reference]
                if write == "true":
                    expected += ["--cache-to=type=registry,ref=" + reference + ",mode=max,ignore-error=true"]
                self.assertEqual(self.cache_options(commands[0]), expected)

    def test_optional_cache_export_never_suppresses_a_build_failure(self):
        result, commands = self.run_make("debian-images", "registry.example/cache:test", "true", fail_role="osd")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([command[command.index("--target") + 1] for command in commands], ["control", "osd"])


if __name__ == "__main__":
    unittest.main()
