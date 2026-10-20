import contextlib
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check


class FakeEngine:
    """A Docker engine that models containers, networks, network namespaces
    and the root qdisc of each interface, for the commands the checker runs."""

    def __init__(self, kernel_netem=True, grants_net_admin=True, server_ready=True):
        self.kernel_netem = kernel_netem
        self.grants_net_admin = grants_net_admin
        self.server_ready = server_ready
        self.runs = []
        self.containers = {}
        self.networks = {}
        self.namespaces = {}
        self.ids = itertools.count(1)

    def namespace(self, bridge):
        identifier = next(self.ids)
        links = {"lo": {"ifindex": 1, "address": "00:00:00:00:00:00", "ip": "127.0.0.1", "qdisc": None}}
        if bridge:
            links["eth0"] = {"ifindex": 10 + identifier, "address": "02:42:ac:1e:00:%02x" % identifier,
                             "ip": "172.30.0.%d" % identifier, "qdisc": None}
        self.namespaces[identifier] = links
        return identifier

    def __call__(self, *command, timeout=120, check_exit=True):
        code, output = self.handle(list(command))
        if check_exit and code:
            raise extension_check.CheckError("Command failed: docker " + " ".join(command) + "\n" + output)
        return code, output

    def handle(self, command):
        verb = command[0]
        if verb == "network":
            return self.network(command[1:])
        if verb == "run":
            return self.run(command[1:])
        if verb == "container" and command[1] == "inspect":
            return self.inspect(command[2])
        if verb == "exec":
            return self.exec(command[1], command[2:])
        if verb == "rm":
            names = [name for name in command[1:] if not name.startswith("-")]
            for name in names:
                self.containers.pop(name, None)
            return 0, ""
        if verb == "ps":
            selector = command[command.index("--filter") + 1]
            if selector.startswith("name="):
                return 0, "\n".join(name for name in self.containers if "^" + name + "$" == selector[5:])
            return 0, "\n".join(self.labeled(self.containers, command))
        if verb == "restart":
            self.restart(command[-1])
            return 0, ""
        raise AssertionError("unexpected docker command: %r" % command)

    @staticmethod
    def labeled(items, command):
        label = command[command.index("--filter") + 1].split("=", 1)[1]
        return [name for name, labels in items.items() if label in (labels if isinstance(labels, set) else
                                                                     labels["labels"])]

    @staticmethod
    def option(command, name):
        for index, value in enumerate(command):
            if value == name:
                return command[index + 1]
            if value.startswith(name + "="):
                return value.split("=", 1)[1]
        return None

    def network(self, command):
        if command[0] == "create":
            self.networks[command[-1]] = {self.option(command, "--label")}
            return 0, ""
        if command[0] == "ls":
            return 0, "\n".join(self.labeled(self.networks, command))
        if command[0] == "rm":
            for name in command[1:]:
                self.networks.pop(name, None)
            return 0, ""
        raise AssertionError(command)

    def run(self, command):
        self.runs.append(command)
        name = self.option(command, "--name")
        network = self.option(command, "--network")
        if network.startswith("container:"):
            namespace = self.containers[network.split(":", 1)[1]]["namespace"]
        else:
            namespace = self.namespace(network != "none")
        self.containers[name] = {"labels": {self.option(command, "--label")}, "namespace": namespace,
                                 "network": network,
                                 "net_admin": "--cap-add=NET_ADMIN" in command and self.grants_net_admin}
        return 0, name

    def restart(self, name):
        """A new namespace for the target; Docker moves its endpoint there and
        joined containers stay in the old one."""
        target = self.containers[name]
        old = self.namespaces[target["namespace"]]
        identifier = next(self.ids)
        self.namespaces[identifier] = {"lo": dict(old["lo"], qdisc=None), "eth0": dict(old["eth0"], ifindex=100 + identifier, qdisc=None)}
        del old["eth0"]
        target["namespace"] = identifier

    def inspect(self, name):
        container = self.containers[name]
        eth0 = self.namespaces[container["namespace"]]["eth0"]
        settings = {"SandboxID": "sandbox-%d" % container["namespace"],
                    "Networks": {container["network"]: {"IPAddress": eth0["ip"], "MacAddress": eth0["address"]}}}
        return 0, json.dumps([{"NetworkSettings": settings}])

    def exec(self, name, command):
        container = self.containers[name]
        links = self.namespaces[container["namespace"]]
        if command == ["python3", "-c", extension_check.KERNEL]:
            return 0, "0.0.0-fake\n"
        if command == ["python3", "-c", extension_check.NAMESPACE]:
            return 0, "net:[%d]\n" % container["namespace"]
        if command[:2] == ["ip", "-j"]:
            return 0, json.dumps([{"ifname": ifname, "ifindex": link["ifindex"], "address": link["address"],
                                   "addr_info": [{"local": link["ip"]}]} for ifname, link in links.items()])
        if command[0] == "tc":
            return self.tc(container, links, command[1:])
        if command[:3] == ["python3", "-c", extension_check.READY]:
            return (0, "") if self.server_ready else (1, "ConnectionRefusedError: [Errno 111] Connection refused")
        if command[:3] == ["python3", "-c", extension_check.CLIENT]:
            target = next(link for links in self.namespaces.values() for link in links.values()
                          if link["ip"] == command[3])
            delay = 2 * target["qdisc"]["delay"] if target["qdisc"] else 0
            if target["qdisc"]:
                target["qdisc"]["packets"] += 18
            return 0, json.dumps({"count": 9, "median_ms": delay + 0.1, "min_ms": delay, "max_ms": delay + 1})
        raise AssertionError(command)

    def tc(self, container, links, command):
        if command[:2] == ["-j", "-s"]:
            link = links[command[-1]]
            qdisc = link["qdisc"]
            if qdisc is None:
                return 0, json.dumps([{"kind": "noqueue", "handle": "0:", "root": True, "options": {}}])
            return 0, json.dumps([{"kind": "netem", "handle": qdisc["handle"], "root": True,
                                   "options": {"delay": {"delay": qdisc["delay"] / 1000,
                                                         "jitter": qdisc["jitter"] / 1000}},
                                   "packets": qdisc["packets"], "bytes": qdisc["packets"] * 100, "drops": 0}])
        action, interface = command[1], command[3]
        if not container["net_admin"]:
            return 2, "RTNETLINK answers: Operation not permitted\n"
        if not self.kernel_netem:
            return 2, "Error: Specified qdisc kind is unknown.\n"
        link = links[interface]
        if action == "del":
            link["qdisc"] = None
            return 0, ""
        values = [int(value[:-2]) for value in command[command.index("delay") + 1:]]
        if action == "add" and link["qdisc"] is not None:
            return 2, "Error: Exclusivity flag on, cannot modify.\n"
        link["qdisc"] = {"handle": command[command.index("handle") + 1], "delay": values[0], "jitter": values[1] if len(values) > 1 else 0,
                         "packets": (link["qdisc"] or {}).get("packets", 0)}
        return 0, ""


HELPER = {"reference": "helper", "image_id": "sha256:" + "a" * 64, "platform": "linux/arm64"}
TARGET = {"reference": "quay.io/ceph/ceph:v20.2.4", "image_id": "sha256:" + "b" * 64, "platform": "linux/arm64"}


class ExtensionCheckTests(unittest.TestCase):
    def setUp(self):
        self.output = Path(tempfile.mkdtemp())

    def engine(self, **kwargs):
        engine = FakeEngine(**kwargs)
        patch = mock.patch.object(extension_check, "docker", engine)
        patch.start()
        self.addCleanup(patch.stop)
        return engine

    def test_kernel_check_applies_changes_and_removes_netem_and_its_container(self):
        engine = self.engine()
        result = extension_check.kernel(HELPER, self.output)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual((result["added"], result["changed"], result["removed"]), ((10, 2), (20, 0), "noqueue"))
        self.assertEqual(result["kernel"], "0.0.0-fake")
        self.assertEqual(engine.containers, {})
        self.assertEqual(result["cleanup"]["status"], "passed")

    def test_kernel_and_permission_failures_are_told_apart_and_cleaned_up(self):
        for kwargs, kind in (({"kernel_netem": False}, "kernel_unsupported"),
                             ({"grants_net_admin": False}, "permission_denied")):
            with self.subTest(kind=kind):
                engine = self.engine(**kwargs)
                result = extension_check.kernel(HELPER, self.output)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", kind))
                self.assertEqual(engine.containers, {})

    def test_delay_scenario_delays_changes_survives_helper_exit_and_restores(self):
        engine = self.engine()
        result = extension_check.delay_scenario(HELPER, TARGET, "run1", 200, self.output / "target")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["interface"]["name"], "eth0")
        self.assertEqual(result["applied"], (200, 0))
        self.assertEqual(result["changed"], (100, 20))
        self.assertEqual(result["after_helper_exit"], (100, 20))
        self.assertEqual((result["before"], result["after_restore"]), ("noqueue", "noqueue"))
        self.assertGreater(result["counters"]["packets"], 0)
        restart = result["restart"]
        self.assertNotEqual(restart["sandbox_id"], result["sandbox_id"])
        self.assertEqual((restart["old_namespace_interfaces"], restart["new_root"]), ([], "noqueue"))
        self.assertEqual((engine.containers, engine.networks), ({}, {}))
        # Every helper runs as root; only the target keeps its image's user.
        helpers = [run for run in engine.runs if HELPER["image_id"] in run]
        self.assertEqual(len(helpers), 5)  # server, shaper, client, observer, fresh
        self.assertTrue(all("--user=0:0" in run for run in helpers))

    def test_a_server_that_never_answers_fails_before_the_baseline(self):
        engine = self.engine(server_ready=False)
        result = extension_check.delay_scenario(HELPER, TARGET, "run6", 200, self.output / "target")
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "runtime_failure"))
        self.assertNotIn("baseline", result)
        self.assertEqual((engine.containers, engine.networks), ({}, {}))

    def test_quick_cleanup_failure_is_not_an_image_failure(self):
        image = dict(HELPER, labels={})

        def probe(extension, checked, args, output):
            checked.update(status="passed", checks=[], cleanup={"status": "failed", "errors": ["timed out"]})

        with mock.patch.object(extension_check.check, "inspect_image", return_value=dict(image)), \
                mock.patch.object(extension_check, "docker", return_value=(0, json.dumps([{"Config": {"Labels": {}}}]))), \
                mock.patch.object(extension_check, "probe", side_effect=probe), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(extension_check.main(["--extension", "netem", "--image", "x", "--output-dir",
                                                   str(self.output / "quick")]), 1)
        report = json.loads((self.output / "quick" / "extension-report.json").read_text())
        self.assertEqual(report["failure_kind"], "checker_failure")

    def test_delay_scenario_failures_report_kind_and_leave_nothing(self):
        for kwargs, kind in (({"grants_net_admin": False}, "permission_denied"),
                             ({"kernel_netem": False}, "kernel_unsupported")):
            with self.subTest(kind=kind):
                engine = self.engine(**kwargs)
                result = extension_check.delay_scenario(HELPER, TARGET, "run2", 200, self.output / kind)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", kind))
                self.assertEqual((engine.containers, engine.networks), ({}, {}))

    def test_delay_scenario_rejects_a_delay_that_does_not_show_in_requests(self):
        engine = self.engine()
        original = engine.exec

        def no_effect(name, command):
            if command[:2] == ["python3", "-c"]:
                return 0, json.dumps({"count": 9, "median_ms": 0.2, "min_ms": 0.1, "max_ms": 0.3})
            return original(name, command)

        engine.exec = no_effect
        result = extension_check.delay_scenario(HELPER, TARGET, "run3", 200, self.output / "target")
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "runtime_failure"))
        self.assertIn("under a 200 ms delay", result["error"])
        self.assertEqual((engine.containers, engine.networks), ({}, {}))

    def test_leftovers_fail_the_scenario(self):
        engine = self.engine()
        original = engine.handle

        def stuck(command):
            if command[0] == "rm" and any(name.endswith("-target") for name in command):
                return 1, "Error response from daemon: removal of container is already in progress"
            return original(command)

        engine.handle = stuck
        result = extension_check.delay_scenario(HELPER, TARGET, "run4", 200, self.output / "target")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertTrue(result["cleanup"]["leftovers"])

    def test_a_restart_that_keeps_the_namespace_fails_the_scenario(self):
        engine = self.engine()
        engine.restart = lambda name: None
        result = extension_check.delay_scenario(HELPER, TARGET, "run5", 200, self.output / "target")
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "runtime_failure"))
        self.assertIn("kept its network sandbox", result["error"])
        self.assertEqual((engine.containers, engine.networks), ({}, {}))

    def test_cleanup_survives_timeouts_and_failed_inventories(self):
        engine = self.engine()
        engine.containers["c1"] = {"labels": {"l"}, "namespace": 1, "network": "n", "net_admin": False}
        engine.networks["n1"] = {"l"}
        original = engine.handle
        calls = []

        def flaky(command):
            calls.append(command[0])
            if command[0] == "rm" and calls.count("rm") == 1:
                raise extension_check.CheckError("Docker command timed out", "timeout")
            return original(command)

        with mock.patch.object(extension_check, "docker", lambda *c, timeout=120, check_exit=True: flaky(list(c))):
            result = extension_check.cleanup("l", self.output)
        # The network was still removed, and the container stayed as a leftover.
        self.assertEqual(engine.networks, {})
        self.assertEqual((result["status"], result["leftovers"]), ("failed", ["c1"]))
        self.assertIn("timed out", result["errors"][0])

        def blind(command):
            if command[0] in ("ps", "network") and "ls" in command or command[0] == "ps":
                return 1, ""
            return original(command)

        with mock.patch.object(extension_check, "docker", lambda *c, timeout=120, check_exit=True: blind(list(c))):
            result = extension_check.cleanup("l", self.output)
        self.assertEqual((result["status"], result["leftovers"]), ("failed", []))

    def test_interface_must_match_address_and_mac(self):
        links = [{"ifname": "eth0", "ifindex": 5, "address": "02:00:00:00:00:01", "addr_info": [{"local": "10.0.0.2"}]},
                 {"ifname": "eth1", "ifindex": 6, "address": "02:00:00:00:00:02", "addr_info": [{"local": "10.0.1.2"}]}]
        self.assertEqual(extension_check.target_interface(links, "10.0.1.2", "02:00:00:00:00:02")["name"], "eth1")
        for address, mac in (("10.0.1.2", "02:00:00:00:00:01"), ("10.0.9.9", "02:00:00:00:00:02")):
            with self.subTest(address=address, mac=mac), self.assertRaises(extension_check.CheckError):
                extension_check.target_interface(links, address, mac)

    def test_netem_state_requires_the_owned_handle(self):
        owned = {"kind": "netem", "handle": "c7e1:", "options": {"delay": {"delay": 0.2, "jitter": 0.01}}}
        self.assertEqual(extension_check.netem_state(owned), (200, 10))
        for qdisc in (dict(owned, handle="8001:"), dict(owned, kind="tbf"), {"kind": "noqueue", "handle": "0:"}):
            with self.subTest(qdisc=qdisc), self.assertRaises(extension_check.CheckError):
                extension_check.netem_state(qdisc)

    def test_tc_errors_are_classified(self):
        self.assertEqual(extension_check.classify("RTNETLINK answers: Operation not permitted"), "permission_denied")
        self.assertEqual(extension_check.classify("Error: Specified qdisc kind is unknown."), "kernel_unsupported")
        self.assertEqual(extension_check.classify('Unknown qdisc "netem", hence option "delay" is unparsable'),
                         "image_unsupported")
        self.assertEqual(extension_check.classify("Cannot find device \"eth9\""), "runtime_failure")

    def test_probe_output_keeps_failures_and_versions(self):
        output = ("CHECK\tpassed\tpath:sh\nCHECK\tfailed\tpath:tc\nFAILURE\tmissing_executable\tpath:tc\n"
                  "VERSION\tip\tip utility, iproute2-6.1.0\nCOMPLETE\tnetem\n")
        checks, versions, complete = extension_check.parse_probe(output, "netem")
        self.assertTrue(complete)
        self.assertEqual(versions, {"ip": "ip utility, iproute2-6.1.0"})
        self.assertEqual(checks[1]["failure_kind"], "missing_executable")
        self.assertFalse(extension_check.parse_probe("CHECK\tpassed\tpath:sh\n", "netem")[2])
        with self.assertRaises(extension_check.CheckError):
            extension_check.parse_probe("garbage\n", "netem")

    def test_arguments_require_targets_only_with_functional(self):
        stderr = io.StringIO()
        for argv in (["--extension", "netem", "--image", "x", "--target", "t"],
                     ["--extension", "netem", "--image", "x", "--functional"],
                     ["--extension", "netem", "--image=-x"],
                     ["--extension", "nft", "--image", "x"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                extension_check.arguments(argv)
        args = extension_check.arguments(["--extension", "netem", "--image", "x", "--functional", "--target", "t"])
        self.assertEqual(args.target, ["t"])


if __name__ == "__main__":
    unittest.main()
