"""The MGR module requirement probe of check-runtime.sh, run on module trees."""
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check

START = "<<'MGR' || failed=1\n"
ALWAYS_ON = ('balancer', 'crash', 'devicehealth', 'orchestrator', 'pg_autoscaler', 'progress',
             'rbd_support', 'status', 'telemetry', 'volumes')


def probe_source():
    script = (check.HERE / "check-runtime.sh").read_text()
    return script.split(START, 1)[1].split("\nMGR\n", 1)[0]


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def module_tree(root):
    """A release-shaped module path: the shared files and the baseline modules."""
    write(root / "mgr_module.py", """
        import os
        import ceph_module
        from mgr_util import helper
        class MgrModule:
            pass
        """)
    write(root / "mgr_util.py", """
        import os
        if 'UNITTEST' in os.environ:
            import tests
        def helper():
            pass
        """)
    for name in set(ALWAYS_ON) | {"mirroring", "snap_schedule"}:
        write(root / name / "__init__.py", "from .module import Module\n")
        write(root / name / "module.py", """
            import json
            from mgr_module import MgrModule
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                import only_for_type_checkers
            try:
                import optional_accelerator
            except ImportError:
                optional_accelerator = None
            class Module(MgrModule):
                pass
            """)
        # A module's own test suite is not part of its runtime closure.
        write(root / name / "tests" / "test_module.py", "import pytest_only_in_tests\n")


class MGRModuleProbeTests(unittest.TestCase):
    def run_probe(self, root, release="20.2.4", requested=()):
        result = subprocess.run([sys.executable, "-", str(root), release, *requested], input=probe_source(),
                                capture_output=True, text=True, timeout=120)
        checks = {}
        kinds = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if fields[0] == "CHECK":
                checks[fields[2]] = fields[1]
            elif fields[0] == "FAILURE":
                kinds[fields[2]] = fields[1]
        return result, checks, kinds

    def test_the_baseline_is_the_always_on_list_plus_the_four_fixture_modules(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            result, checks, _ = self.run_probe(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            expected = set(ALWAYS_ON) | {"volumes", "rbd_support", "mirroring", "snap_schedule"}
            self.assertEqual({name.split(":", 1)[1] for name in checks if name.startswith("mgr-module:")}, expected)
            self.assertTrue(all(status == "passed" for status in checks.values()), checks)
            for release in ("19.2.5", "21.2.0"):
                self.assertEqual(self.run_probe(root, release)[0].returncode, 0)

    def test_a_missing_baseline_module_is_a_missing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            (root / "snap_schedule" / "module.py").unlink()
            result, checks, kinds = self.run_probe(root)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(checks["mgr-module:snap_schedule"], "failed")
            self.assertEqual(kinds["mgr-module:snap_schedule"], "missing_file")
            self.assertEqual(checks["mgr-module:volumes"], "passed")

    def test_an_unrecorded_release_or_module_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            result, checks, kinds = self.run_probe(root, "22.2.0")
            self.assertEqual((result.returncode, checks["mgr-baseline-list"]), (1, "failed"))
            self.assertIn("22.2.0", result.stdout)
            result, checks, kinds = self.run_probe(root / "absent")
            self.assertEqual((result.returncode, kinds["mgr-module-path"]), (1, "missing_file"))

    def test_a_consumer_module_of_any_valid_name_is_checked_with_its_closure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            write(root / "tc_consumer" / "module.py", """
                import json
                import tc_shared_helper
                from mgr_module import MgrModule
                from . import local_part
                """)
            write(root / "tc_consumer" / "local_part.py", "import hashlib\n")
            write(root / "tc_shared_helper.py", "import sqlite3\n")
            result, checks, _ = self.run_probe(root, requested=["tc_consumer"])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual((checks["mgr-module:tc_consumer"], checks["mgr-module-load:tc_consumer"]),
                             ("passed", "passed"))

    def test_a_missing_dependency_is_attributed_to_its_module(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            write(root / "tc_broken" / "module.py", "import tc_shared_helper\n")
            # The missing package is a dependency of a shared internal file.
            write(root / "tc_shared_helper.py", "def f():\n    import tc_absent_dependency_package\n")
            result, checks, kinds = self.run_probe(root, requested=["tc_broken", "absent_module"])
            self.assertEqual(result.returncode, 1)
            self.assertEqual(checks["mgr-module:tc_broken"], "passed")
            self.assertEqual(kinds["mgr-module-load:tc_broken"], "loading_failure")
            self.assertIn("MGR module tc_broken dependency tc_absent_dependency_package: ModuleNotFoundError",
                          result.stdout)
            self.assertEqual(kinds["mgr-module:absent_module"], "missing_file")
            # The failure stays with the requested module; the baseline passes.
            self.assertEqual(checks["mgr-module-load:volumes"], "passed")
            parsed, _ = check.parse_probe(result.stdout + "VERSION\tceph-mgr\tceph version 20.2.4 (abc) tentacle\n")
            failed = {item["name"]: item["failure_kind"] for item in parsed if item["status"] != "passed"}
            self.assertEqual(failed, {"mgr-module-load:tc_broken": "loading_failure",
                                      "mgr-module:absent_module": "missing_file"})

    def dotted_probe(self, root, body):
        write(root / "tc_dotted" / "module.py", body)
        result, checks, kinds = self.run_probe(root, requested=["tc_dotted"])
        return result, checks["mgr-module-load:tc_dotted"]

    def test_a_dotted_import_needs_its_submodule_not_only_the_parent_package(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            # Positive controls: present submodules of a present package.
            result, status = self.dotted_probe(root, """
                import email.mime.text
                import xml.etree.ElementTree as ElementTree
                import os.path
                """)
            self.assertEqual((result.returncode, status), (0, "passed"), result.stdout)
            # The parent package email exists; its submodule does not.
            result, status = self.dotted_probe(root, "import email.tc_missing_submodule\n")
            self.assertEqual((result.returncode, status), (1, "failed"))
            self.assertIn("MGR module tc_dotted dependency email.tc_missing_submodule: ModuleNotFoundError",
                          result.stdout)

    def test_a_from_import_needs_its_module_path_and_each_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            # Positive controls: an attribute, a submodule that is not yet an
            # attribute of its package, a dotted module path, a star import,
            # a built-in module's attribute and the embedded MGR module.
            result, status = self.dotted_probe(root, """
                from json import dumps
                from email import mime
                from email.mime import text
                from email.mime.text import MIMEText
                from os.path import *
                from sys import argv
                from ceph_module import BaseMgrModule
                """)
            self.assertEqual((result.returncode, status), (0, "passed"), result.stdout)
            for body, shown in (
                    # A missing submodule in the dotted module path.
                    ("from email.tc_missing_submodule import anything\n",
                     "from email.tc_missing_submodule import anything: ModuleNotFoundError"),
                    # A present module without that attribute or submodule.
                    ("from email import tc_missing_name\n",
                     "from email import tc_missing_name: ImportError"),
                    ("from email.mime.text import tc_missing_name\n",
                     "from email.mime.text import tc_missing_name: ImportError"),
                    ("from sys import tc_missing_name\n",
                     "from sys import tc_missing_name: ImportError")):
                result, status = self.dotted_probe(root, body)
                self.assertEqual((result.returncode, status), (1, "failed"), body)
                self.assertIn("MGR module tc_dotted dependency " + shown, result.stdout)

    def test_a_python_version_branch_not_taken_is_not_required(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            # The shapes of the release modules (mgr_util, smb): the branch
            # for another Python version imports names this one lacks.
            result, status = self.dotted_probe(root, """
                import sys
                from sys import version_info
                if sys.version_info >= (3, 3):
                    from threading import Timer
                else:
                    from threading import _Timer as Timer
                if sys.version_info < (3, 0):
                    from tc_absent_python2 import name
                if version_info >= (99, 0):
                    from typing import tc_absent_future_name
                elif sys.version_info >= (3, 0):
                    import json
                """)
            self.assertEqual((result.returncode, status), (0, "passed"), result.stdout)
            # The branch this interpreter takes stays required.
            result, status = self.dotted_probe(root, """
                import sys
                if sys.version_info >= (3, 0):
                    from threading import tc_absent_name
                else:
                    import json
                """)
            self.assertEqual((result.returncode, status), (1, "failed"))
            self.assertIn("from threading import tc_absent_name", result.stdout)

    def test_a_dotted_internal_import_needs_its_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            write(root / "tc_internal" / "__init__.py")
            write(root / "tc_internal" / "part.py", "import json\n")
            write(root / "tc_internal" / "sub" / "__init__.py", "anything = None\n")
            result, status = self.dotted_probe(root, """
                import tc_internal.part
                from tc_internal.sub import anything
                from mgr_util import helper
                """)
            self.assertEqual((result.returncode, status), (0, "passed"), result.stdout)
            result, status = self.dotted_probe(root, "from tc_internal.tc_missing import anything\n")
            self.assertEqual((result.returncode, status), (1, "failed"))
            self.assertIn("MGR module tc_dotted dependency from tc_internal.tc_missing import anything: "
                          "internal module tc_internal.tc_missing not found", result.stdout)
            result, status = self.dotted_probe(root, "import mgr_util.tc_missing\n")
            self.assertEqual((result.returncode, status), (1, "failed"))
            # A present internal module without the imported name.
            for body in ("from mgr_util import tc_missing_name\n", "from tc_internal.sub import tc_missing_name\n",
                         "from tc_internal import tc_missing_name\n"):
                result, status = self.dotted_probe(root, body)
                self.assertEqual((result.returncode, status), (1, "failed"), body)
                self.assertIn(".tc_missing_name not found", result.stdout)

    def test_environment_gated_hooks_and_import_fallbacks_are_optional(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            write(root / "tc_hooks" / "module.py", """
                import os
                if 'COVERAGE_ENABLED' in os.environ:
                    import tc_absent_coverage
                if 'UNITTEST' not in os.environ:
                    import json
                else:
                    from tc_absent_tests import mock
                if os.environ.get('TC_DEBUG'):
                    import tc_absent_debugger
                from os import environ
                if 'SNAP_SCHED_UNITTEST' in environ:
                    import tc_absent_tests
                elif 'UNITTEST' in environ:
                    import tc_absent_tests
                try:
                    import queue
                except ImportError:
                    import tc_absent_python2_queue
                """)
            result, checks, _ = self.run_probe(root, requested=["tc_hooks"])
            self.assertEqual(result.returncode, 0, result.stdout)
            # The required branch of a negated environment test stays required.
            write(root / "tc_hooks" / "module.py", """
                import os
                if 'UNITTEST' not in os.environ:
                    import tc_absent_runtime_dependency
                """)
            result, checks, _ = self.run_probe(root, requested=["tc_hooks"])
            self.assertEqual(checks["mgr-module-load:tc_hooks"], "failed")

    def test_an_import_fallback_needs_one_branch_that_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            for body in (
                    # An optional accelerator without another required import.
                    "try:\n    import tc_fast\nexcept ImportError:\n    tc_fast = None\n",
                    "try:\n    import tc_fast\nexcept ImportError:\n    pass\n",
                    "try:\n    import tc_fast\nexcept ModuleNotFoundError:\n    tc_fast = None\n",
                    "try:\n    import tc_fast\nexcept Exception:\n    tc_fast = None\n",
                    "try:\n    import tc_fast\nexcept:\n    tc_fast = None\n",
                    # Either branch of a fallback may be the one that loads.
                    "try:\n    import tc_missing_first\nexcept ImportError:\n    import json\n",
                    "try:\n    import json\nexcept ImportError:\n    import tc_missing_fallback\n",
                    # The first handler that catches the failure runs.
                    "try:\n    import tc_missing\nexcept KeyError:\n    import tc_never\n"
                    "except (AttributeError, ImportError):\n    import json\n",
                    # A nested fallback that loads satisfies the outer try.
                    "try:\n    try:\n        import tc_a\n    except ImportError:\n        import json\n"
                    "except ImportError:\n    import tc_c\n",
                    # else runs only after the body loaded.
                    "try:\n    import tc_missing\nexcept ImportError:\n    tc_missing = None\n"
                    "else:\n    import tc_never_run\n",
                    # A version branch not taken inside a fallback.
                    "import sys\ntry:\n    if sys.version_info < (3, 0):\n        import tc_python2\n"
                    "    else:\n        import json\nexcept ImportError:\n    import tc_never\n"):
                result, status = self.dotted_probe(root, body)
                self.assertEqual((result.returncode, status), (0, "passed"), body + result.stdout)
            for body, shown in (
                    # Neither branch of the fallback loads.
                    ("try:\n    import tc_missing_first\nexcept ImportError:\n    import tc_missing_fallback\n",
                     ("tc_missing_first: ModuleNotFoundError", "tc_missing_fallback: ModuleNotFoundError")),
                    # The handler that runs fails, though a later one would load.
                    ("try:\n    import tc_missing\nexcept ModuleNotFoundError:\n    import tc_missing_fallback\n"
                     "except ImportError:\n    import json\n", ("tc_missing_fallback: ModuleNotFoundError",)),
                    # Nested fallbacks that all fail.
                    ("try:\n    try:\n        import tc_a\n    except ImportError:\n        import tc_b\n"
                     "except ImportError:\n    import tc_c\n", ("tc_c: ModuleNotFoundError",)),
                    # else runs after the body loaded; finally always runs.
                    ("try:\n    import json\nexcept ImportError:\n    json = None\nelse:\n    import tc_missing_after\n",
                     ("tc_missing_after: ModuleNotFoundError",)),
                    ("try:\n    import tc_fast\nexcept ImportError:\n    tc_fast = None\n"
                     "finally:\n    import tc_missing_final\n", ("tc_missing_final: ModuleNotFoundError",)),
                    # A handler that does not catch the failure.
                    ("try:\n    import tc_missing\nexcept KeyError:\n    pass\n", ("tc_missing: ModuleNotFoundError",)),
                    ("try:\n    from json import tc_missing_name\nexcept ModuleNotFoundError:\n    tc_missing_name = None\n",
                     ("from json import tc_missing_name: ImportError",)),
                    # The version branch taken inside a fallback stays one branch.
                    ("import sys\ntry:\n    if sys.version_info >= (3, 0):\n        import tc_missing_a\n"
                     "    else:\n        import json\nexcept ImportError:\n    import tc_missing_b\n",
                     ("tc_missing_a: ModuleNotFoundError", "tc_missing_b: ModuleNotFoundError"))):
                result, status = self.dotted_probe(root, body)
                self.assertEqual((result.returncode, status), (1, "failed"), body + result.stdout)
                for line in shown:
                    self.assertIn("MGR module tc_dotted dependency " + line, result.stdout, body)

    def relative_probe(self, root, body):
        write(root / "tc_rel" / "module.py", body + "class Module:\n    pass\n")
        result, checks, kinds = self.run_probe(root, requested=["tc_rel"])
        return result, checks["mgr-module-load:tc_rel"]

    def test_an_explicit_relative_import_needs_its_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            write(root / "tc_rel" / "__init__.py", "from .module import Module\n")
            write(root / "tc_rel" / "helper.py", "import json\ndef read():\n    pass\n")
            write(root / "tc_rel" / "sub" / "__init__.py", "from .. import helper\nVALUE = 1\n")
            write(root / "tc_rel" / "sub" / "leaf.py", "from ..helper import read\n")
            # Positive controls: a sibling module and its name, the package's
            # submodules, a name of a subpackage and a parent-relative import.
            result, status = self.relative_probe(root, """\
                from .helper import read
                from . import helper, sub
                from .sub import VALUE, leaf
                from .sub.leaf import read as leaf_read
                """.replace("                ", ""))
            self.assertEqual((result.returncode, status), (0, "passed"), result.stdout)
            for body, shown in (
                    ("from .missing_helper import read\n",
                     "from .missing_helper import read: internal module tc_rel.missing_helper not found"),
                    ("from . import missing_helper\n",
                     "from . import missing_helper: internal name tc_rel.missing_helper not found"),
                    ("from .helper import tc_missing_name\n",
                     "from .helper import tc_missing_name: internal name tc_rel.helper.tc_missing_name not found"),
                    ("from .sub.missing import x\n",
                     "from .sub.missing import x: internal module tc_rel.sub.missing not found"),
                    ("from ..pkg import y\n",
                     "from ..pkg import y: relative import beyond the top-level package")):
                result, status = self.relative_probe(root, body)
                self.assertEqual((result.returncode, status), (1, "failed"), body + result.stdout)
                self.assertIn("MGR module tc_rel dependency " + shown, result.stdout, body)
            # A module outside a package has no parent for a relative import.
            write(root / "tc_shared_relative.py", "from . import anything\n")
            result, status = self.relative_probe(root, "import tc_shared_relative\n")
            self.assertEqual((result.returncode, status), (1, "failed"))
            self.assertIn("from . import anything: relative import with no parent package", result.stdout)

    def test_selected_module_assets_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            module_tree(root)
            write(root / "dashboard" / "module.py", "import json\n")
            result, checks, kinds = self.run_probe(root, requested=["dashboard"])
            self.assertEqual(kinds["mgr-module-assets:dashboard"], "missing_file")
            write(root / "dashboard" / "frontend" / "dist" / "index.html", "<html></html>\n")
            result, checks, _ = self.run_probe(root, requested=["dashboard"])
            self.assertEqual((result.returncode, checks["mgr-module-assets:dashboard"]), (0, "passed"))
            # The baseline alone never asks for dashboard assets.
            self.assertNotIn("mgr-module-assets:dashboard", self.run_probe(root)[1])
            result, checks, _ = self.run_probe(root, requested=["../etc"])
            self.assertEqual((result.returncode, checks["mgr-module-names"]), (1, "failed"))


if __name__ == "__main__":
    unittest.main()
