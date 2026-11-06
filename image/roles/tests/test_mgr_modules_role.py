"""The optional mgr-modules role: inventory, package roots, executables and image targets."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backend
import build
import contract

MGR = "/usr/share/ceph/mgr"


def rpm_inventory():
    """An RPM-shaped inventory with the module ownership of the official image."""
    files = {
        "ceph-mgr": ["/usr/bin/ceph-mgr", MGR + "/mgr_module.py"],
        "ceph-mgr-modules-core": [MGR + "/volumes/module.py", MGR + "/rbd_support/module.py",
                                  MGR + "/mirroring/module.py", MGR + "/snap_schedule/module.py",
                                  MGR + "/volumes/fs/__init__.py", MGR + "/object_format.py"],
        "ceph-mgr-dashboard": [MGR + "/dashboard/module.py", MGR + "/dashboard/frontend/dist/index.html"],
        "ceph-mgr-cephadm": [MGR + "/cephadm/module.py"],
        "cephadm": ["/usr/sbin/cephadm"],
        "ceph-common": ["/usr/bin/ceph", "/usr/bin/ceph-conf", "/usr/bin/rados"],
        "ceph-mon": ["/usr/bin/ceph-mon"],
        "ceph-osd": ["/usr/bin/ceph-osd"],
        "ceph-radosgw": ["/usr/bin/radosgw"],
        "ceph-mds": ["/usr/bin/ceph-mds"],
        "libradosstriper1": [], "cryptsetup": [], "ceph-base": [], "python3-cephfs": [],
        "rbd-mirror": [], "cephfs-mirror": [], "ceph-immutable-object-cache": [],
    }
    return {name: {"files": paths} for name, paths in files.items()}


class InventoryTests(unittest.TestCase):
    def test_modules_are_directories_with_module_py_by_owning_package(self):
        root, owners = backend.mgr_modules(rpm_inventory())
        self.assertEqual(root, MGR)
        self.assertEqual(owners, {"volumes": "ceph-mgr-modules-core", "rbd_support": "ceph-mgr-modules-core",
                                  "mirroring": "ceph-mgr-modules-core", "snap_schedule": "ceph-mgr-modules-core",
                                  "dashboard": "ceph-mgr-dashboard", "cephadm": "ceph-mgr-cephadm"})

    def test_no_or_ambiguous_module_path(self):
        self.assertEqual(backend.mgr_modules({"ceph-common": {"files": ["/usr/bin/ceph"]}}), (None, {}))
        inventory = rpm_inventory()
        inventory["other"] = {"files": ["/opt/mgr/mgr_module.py"]}
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            backend.mgr_modules(inventory)
        inventory = rpm_inventory()
        inventory["other"] = {"files": [MGR + "/dashboard/module.py"]}
        with self.assertRaisesRegex(RuntimeError, "two packages"):
            backend.mgr_modules(inventory)

    def test_rpm_roots_bundle_every_module_package_and_no_daemon_roots(self):
        roots = backend.roots("rpm", rpm_inventory())
        self.assertEqual(roots["mgr-modules"], ("ceph-mgr", "ceph-common", "ceph-mgr-cephadm",
                                                "ceph-mgr-dashboard", "ceph-mgr-modules-core"))
        for daemon in ("ceph-mon", "ceph-osd", "ceph-radosgw", "ceph-mds", "rbd-mirror", "cephfs-mirror"):
            self.assertNotIn(daemon, roots["mgr-modules"])
        # The baseline roots are unchanged by the optional role.
        self.assertEqual(roots["essential"], ("ceph-mon", "ceph-mgr", "ceph-osd", "ceph-common"))

    def test_only_the_official_rpm_backend_builds_the_role(self):
        self.assertEqual(contract.roles("rpm"), contract.ROLES + ("mgr-modules",))
        for kind in ("deb", "apk"):
            self.assertEqual(contract.roles(kind), contract.ROLES)
        self.assertEqual(contract.roles("rpm", optional=False), contract.ROLES)
        inventory = rpm_inventory()
        self.assertNotIn("mgr-modules", backend.roots("deb", inventory))
        self.assertNotIn("mgr-modules", backend.roots("rpm", {"ceph-mgr": {"files": ["/usr/bin/ceph-mgr"]}}))


class ExecutableTests(unittest.TestCase):
    bins = {"ceph", "ceph-conf", "ceph-mgr", "ceph-mon", "ceph-osd", "ceph-bluestore-tool", "ceph-mds",
            "radosgw", "rbd-mirror", "cephfs-mirror", "rados", "cephadm", "ceph-authtool"}

    def test_the_replacement_mgr_keeps_only_mgr_and_its_local_cli(self):
        self.assertEqual(contract.executables("mgr-modules", set()), {"ceph", "ceph-conf", "ceph-mgr"})
        self.assertEqual(contract.executables("mgr-modules", self.bins),
                         {"ceph", "ceph-conf", "ceph-mgr", "cephadm"})
        for name in ("ceph-mon", "ceph-osd", "ceph-bluestore-tool", "ceph-mds", "radosgw", "rbd-mirror",
                     "cephfs-mirror", "rados", "ceph-authtool"):
            with self.subTest(name=name):
                self.assertFalse(contract.permits_path("mgr-modules", "/usr/bin/" + name, self.bins))
        self.assertTrue(contract.permits_path("mgr-modules", "/usr/sbin/cephadm", self.bins))
        # The cephadm executable stays out of every baseline role.
        for role in contract.ROLES:
            self.assertFalse(contract.permits_path(role, "/usr/sbin/cephadm", self.bins))


def plan(roles, modules=("dashboard", "volumes")):
    targets = tuple(roles) + ("all",)
    value = {
        "source_image": "source@sha256:pinned", "oci_architecture": "amd64",
        "ceph_version": "ceph version 20.2.4 test",
        "ordered_groups": ["common"],
        "groups": {"common": {"archive": "groups/common.tar", "members": list(roles)}},
        "roles": {role: {"groups": ["common"], "manifest_archive": "manifests/" + role + ".tar",
                         "logical_regular_file_bytes": 1} for role in targets},
    }
    if "mgr-modules" in roles:
        value["roles"]["mgr-modules"]["mgr_modules"] = list(modules)
    return value


class ImageTargetTests(unittest.TestCase):
    def test_targets_follow_the_plan_and_label_the_inventory(self):
        official = plan(contract.ROLES + ("mgr-modules",))
        self.assertEqual(build.plan_roles(official), contract.ROLES + ("mgr-modules", "all"))
        generated = build.dockerfile(official)
        self.assertIn("FROM common AS mgr-modules", generated)
        self.assertEqual(generated.count(build.MGR_MODULES_LABEL), 1)
        self.assertIn('LABEL io.ceph-testcontainers.mgr-modules="dashboard volumes"', generated)
        distribution = plan(contract.ROLES)
        self.assertEqual(build.plan_roles(distribution), build.ROLES)
        self.assertNotIn("mgr-modules", build.dockerfile(distribution))

    def test_plans_with_unknown_or_missing_roles_or_bad_inventories_are_refused(self):
        missing = plan(contract.ROLES + ("mgr-modules",))
        del missing["roles"]["rgw"]
        unknown = plan(contract.ROLES)
        unknown["roles"]["mon-mgr"] = dict(unknown["roles"]["essential"])
        for value in (missing, unknown):
            with self.assertRaises(build.BuildError):
                build.plan_roles(value)
        for modules in ((), ("../dashboard",), ("dash board",)):
            with self.subTest(modules=modules), self.assertRaises(build.BuildError):
                build.dockerfile(plan(contract.ROLES + ("mgr-modules",), modules))

    def test_the_check_requires_the_packaged_inventory(self):
        tags = {role: "repo:20.2.4-" + role for role in build.plan_roles(plan(contract.ROLES + ("mgr-modules",)))}
        command = build.check_command(tags, "quick", Path("/out"), ["dashboard", "volumes"])
        self.assertIn("mgr-modules=repo:20.2.4-mgr-modules", command)
        self.assertEqual(command[-4:], ["--mgr-module", "dashboard", "--mgr-module", "volumes"])


if __name__ == "__main__":
    unittest.main()
