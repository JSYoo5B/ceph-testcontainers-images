import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check
from test_extension_check import FakeEngine, HELPER, TARGET

# Saved from the helper (iproute2 6.1.0): `tc -j -s qdisc show dev lo` after `netem rate 1000000bit limit 100`.
# tc prints the rate in bytes per second, and no delay member without one.
NATIVE_RATE = ('[{"kind":"netem","handle":"c7e3:","root":true,"refcnt":2,"options":{"limit":100,"rate":{"rate":125000,'
               '"packetoverhead":0,"cellsize":0,"celloverhead":0},"ecn":false,"gap":0},"bytes":0,"packets":0,"drops":0,'
               '"overlimits":0,"requeues":0,"backlog":0,"qlen":0}]')

# What iproute2 prints for a netem option that it cannot parse.
NO_RATE_PARSER = 'What is "rate"?\nUsage: ... netem [ limit PACKETS ]\n'

FAST = 1.2e9


class RateEngine(FakeEngine):
    """A Docker engine with dual-stack bridge namespaces, each interface's
    owned prio root, netem child and u32 leaves, and transfers whose speed,
    family and counters follow that kernel state.

    tc_rate: whether the image's tc parses netem's rate; keeps_rate: whether
    the kernel keeps it; shapes: whether the kernel's netem limits the rate;
    leaves_shaper: whether removing a container fails silently for the
    shaping helper."""

    def __init__(self, tc_rate=True, keeps_rate=True, shapes=True, overflows=True, **kwargs):
        super().__init__(**kwargs)
        self.tc_rate, self.keeps_rate, self.shapes, self.overflows = tc_rate, keeps_rate, shapes, overflows
        self.leaves_shaper = False
        self.sends = []

    def namespace(self, bridge):
        identifier = super().namespace(bridge)
        links = self.namespaces[identifier]
        for link in links.values():
            link.update(qdisc=None, child=None, filters=[])
        if bridge:
            links["eth0"]["ip6"] = "fd00:1::%x" % identifier
        return identifier

    def restart(self, name):
        raise AssertionError("the rate scenario does not restart its target")

    def handle(self, command):
        if command[0] == "rm" and self.leaves_shaper:
            names = [name for name in command[1:] if not name.startswith("-") and not name.endswith("-shaper")]
            return super().handle(["rm", "--force"] + names)
        return super().handle(command)

    def inspect(self, name):
        container = self.containers[name]
        eth0 = self.namespaces[container["namespace"]]["eth0"]
        settings = {"SandboxID": "sandbox-%d" % container["namespace"],
                    "Networks": {container["network"]: {"IPAddress": eth0["ip"], "GlobalIPv6Address": eth0["ip6"],
                                                        "MacAddress": eth0["address"]}}}
        return 0, json.dumps([{"NetworkSettings": settings}])

    def owner(self, host):
        for identifier, links in self.namespaces.items():
            eth0 = links.get("eth0")
            if eth0 and host in (eth0["ip"], eth0.get("ip6")):
                return eth0
        raise AssertionError(host)

    def exec(self, name, command):
        container = self.containers[name]
        links = self.namespaces[container["namespace"]]
        if command[:3] == ["python3", "-c", extension_check.SENDER]:
            return self.send(links, command[3:])
        if command[:3] == ["python3", "-c", extension_check.READY]:
            return 0, ""
        if command[:2] == ["ip", "-j"]:
            return 0, json.dumps([{"ifname": ifname, "ifindex": link["ifindex"], "address": link["address"],
                                   "addr_info": [{"local": link["ip"]}] + ([{"local": link["ip6"]}] if "ip6" in link else [])}
                                  for ifname, link in links.items()])
        return super().exec(name, command)

    # Transfers: the selected paths take the child's rate, the others are fast.

    def selected(self, link, host, port):
        if not link["qdisc"] or link["qdisc"]["kind"] != "prio" or not link["child"]:
            return False
        return any(leaf["dst"] == host and leaf["port"] == port for leaf in link["filters"])

    def send(self, links, arguments):
        size, together = int(arguments[0]), arguments[2] == "together"
        paths = [arguments[index:index + 3] for index in range(3, len(arguments), 3)]
        source = links["eth0"]
        child = source["child"]
        shaped = [self.selected(source, host, int(port)) for host, port, _ in paths]
        rate = child["rate"] if child and child["rate"] and self.shapes else None
        results, wall = [], 0.0
        for (host, port, seed), selected in zip(paths, shaped):
            family = 6 if ":" in host else 4
            speed = FAST if not (selected and rate) else rate * 0.94 / (sum(shaped) if together else 1)
            delay = child["delay"] if selected else 0
            elapsed = size / speed + delay / 1000 * 2
            seen = source["ip6"] if family == 6 else "::ffff:" + source["ip"]
            results.append({"host": host, "port": int(port), "error": None, "connect_ms": delay + 0.1,
                            "family": family, "local": source["ip6"] if family == 6 else source["ip"],
                            "received": size, "received_sha256": "s" + seed, "seen_from": seen, "sent": size,
                            "sha256": "s" + seed, "elapsed_ms": elapsed * 1000, "goodput_Bps": size / elapsed})
            if selected:
                child["packets"] += size // 1448
                child["bytes"] += size
                if self.overflows and child["limit"] < 10:
                    child["drops"] += 7
            wall = max(wall, elapsed) if together else wall + elapsed
        self.sends.append((paths, together))
        return 0, json.dumps({"size": size, "together": together, "wall_ms": wall * 1000,
                              "aggregate_Bps": size * len(paths) / wall, "transfers": results})

    # tc: a root netem on the loopback, or the owned prio hierarchy.

    def tc(self, container, links, command):
        if "rate" in command and not self.tc_rate:
            return 255, NO_RATE_PARSER
        if command[:2] == ["-j", "-s"]:
            return 0, json.dumps(self.qdiscs(links[command[-1]]))
        if command[:3] == ["-j", "filter", "show"]:
            return 0, self.filters(links[command[-1]])
        if not container["net_admin"]:
            return 2, "RTNETLINK answers: Operation not permitted\n"
        if not self.kernel_netem:
            return 2, "Error: Specified qdisc kind is unknown.\n"
        link = links[command[3]]
        if command[0] == "qdisc":
            return self.qdisc(link, command)
        return self.filter(link, command)

    def netem(self, options, previous=None):
        # A change without a rate keeps the rate, as netem does.
        state = {"delay": 0, "jitter": 0, "rate": (previous or {}).get("rate", 0), "limit": 1000, "loss": None}
        for index, word in enumerate(options):
            if word == "delay":
                state["delay"] = int(options[index + 1][:-2])
                if options[index + 2:index + 3] and options[index + 2].endswith("ms"):
                    state["jitter"] = int(options[index + 2][:-2])
            elif word == "rate":
                state["rate"] = int(options[index + 1][:-3]) // 8 if self.keeps_rate else 0
            elif word == "limit":
                state["limit"] = int(options[index + 1])
            elif word == "loss":
                state["loss"] = 1
        return state

    def qdisc(self, link, command):
        action = command[1]
        handle = command[command.index("handle") + 1] if "handle" in command else None
        if action == "del":
            if "root" in command:
                link.update(qdisc=None, child=None, filters=[])
            return 0, ""
        if "prio" in command:
            if link["qdisc"]:
                return 2, "Error: Exclusivity flag on, cannot modify.\n"
            link["qdisc"] = {"kind": "prio", "handle": handle}
            return 0, ""
        options = command[command.index("netem") + 1:]
        counters = {"packets": 0, "bytes": 0, "drops": 0}
        if "root" in command:
            if action == "add" and link["qdisc"]:
                return 2, "Error: Exclusivity flag on, cannot modify.\n"
            previous = link["qdisc"] if action == "change" else None
            link["qdisc"] = dict(self.netem(options, previous), kind="netem", handle=handle, **counters)
            return 0, ""
        previous = link["child"] if action == "change" else None
        counters = {key: previous[key] for key in counters} if previous else counters
        link["child"] = dict(self.netem(options, previous), kind="netem", handle=handle,
                             parent=command[command.index("parent") + 1], **counters)
        return 0, ""

    @staticmethod
    def netem_json(state):
        options = {"limit": state["limit"], "delay": {"delay": state["delay"] / 1000, "jitter": state["jitter"] / 1000,
                                                "correlation": 0},
                   "ecn": False, "gap": 0}
        if state["rate"]:
            options["rate"] = {"rate": state["rate"], "packetoverhead": 0, "cellsize": 0, "celloverhead": 0}
        if state["loss"]:
            options["loss-random"] = {"loss": 1, "correlation": 0}
        return options

    def qdiscs(self, link):
        root = link["qdisc"]
        if root is None:
            return [{"kind": "noqueue", "handle": "0:", "root": True, "options": {}}]
        if root["kind"] == "netem":
            return [{"kind": "netem", "handle": root["handle"], "root": True, "options": self.netem_json(root),
                     "packets": root["packets"], "bytes": root["bytes"], "drops": 0}]
        entries = [{"kind": "prio", "handle": root["handle"], "root": True,
                    "options": {"bands": 3, "priomap": [0] * 16, "multiqueue": False}, "packets": 0, "drops": 0}]
        child = link["child"]
        if child:
            entries.append({"kind": "netem", "handle": child["handle"], "parent": child["parent"],
                            "options": self.netem_json(child), "packets": child["packets"], "bytes": child["bytes"],
                            "drops": child["drops"], "backlog": 0, "qlen": 0})
        return entries

    def filter(self, link, command):
        def value(name):
            return command[command.index(name) + 1]

        if command[1] == "add":
            dst = value("dst").split("/")[0]
            link["filters"].append({"protocol": value("protocol"), "pref": int(value("pref")), "handle": value("handle"),
                                    "dst": dst, "port": int(value("dport")), "family": 6 if ":" in dst else 4})
            return 0, ""
        pref = int(value("pref"))
        handle = value("handle") if "handle" in command else None
        link["filters"] = [leaf for leaf in link["filters"]
                           if not (leaf["pref"] == pref and handle in (None, leaf["handle"]))]
        return 0, ""

    @staticmethod
    def filters(link):
        """A native-shaped dump: a header and a table entry per preference,
        then each leaf with one "match" member per key."""
        entries = []
        groups = []
        for leaf in link["filters"]:
            if (leaf["protocol"], leaf["pref"]) not in groups:
                groups.append((leaf["protocol"], leaf["pref"]))
        for protocol, pref in groups:
            head = '{"parent":"7e00:","protocol":"%s","pref":%d,"kind":"u32","chain":0' % (protocol, pref)
            table = [leaf for leaf in link["filters"] if leaf["pref"] == pref][0]["handle"].split("::")[0] + ":"
            entries += [head + "}", head + ',"options":{"fh":"%s","ht_divisor":1}}' % table]
            for leaf in link["filters"]:
                if leaf["pref"] != pref:
                    continue
                keys = ",".join('"match":{"value":"%x","mask":"%x","offmask":"","off":%d}'
                                % (int(key["value"], 16), int(key["mask"], 16), key["off"])
                                for key in extension_check.u32_keys(leaf["family"], leaf["dst"], leaf["port"]))
                entries.append(head + ',"options":{"fh":"%s","order":1,"flowid":"7e00:3",%s}}' % (leaf["handle"], keys))
        return "[" + ",".join(entries) + "]"


RATE_BIT = 16000000


class RateTests(unittest.TestCase):
    def setUp(self):
        self.output = Path(tempfile.mkdtemp())

    def engine(self, **kwargs):
        engine = RateEngine(**kwargs)
        patch = mock.patch.object(extension_check, "docker", engine)
        patch.start()
        self.addCleanup(patch.stop)
        return engine

    def scenario(self, engine, run="rate1"):
        result = extension_check.rate_scenario(HELPER, TARGET, run, RATE_BIT, self.output / run)
        self.assertEqual((engine.containers, engine.networks), ({}, {}))
        return result

    # The parser: the image alone, without any capability.

    def test_a_tc_that_parses_rate_is_refused_by_the_kernel_without_net_admin(self):
        engine = self.engine()
        result = extension_check.rate_parser(HELPER, self.output)
        self.assertEqual(result["status"], "passed", result)
        self.assertIn("Operation not permitted", result["output"])
        self.assertEqual(engine.containers, {})
        run = engine.runs[-1]
        self.assertIn("--cap-drop=ALL", run)
        self.assertNotIn("--cap-add=NET_ADMIN", run)
        self.assertIn("--network=none", run)

    def test_a_tc_that_cannot_parse_rate_is_image_unsupported(self):
        engine = self.engine(tc_rate=False)
        result = extension_check.rate_parser(HELPER, self.output)
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "image_unsupported"))
        self.assertEqual(engine.containers, {})

    def test_option_parse_errors_are_image_failures(self):
        self.assertEqual(extension_check.classify(NO_RATE_PARSER), "image_unsupported")
        self.assertEqual(extension_check.classify('Illegal "rate"\n'), "image_unsupported")

    # The kernel: a disposable namespace with NET_ADMIN.

    def test_the_kernel_keeps_rate_limit_and_delay_and_the_handle_goes(self):
        engine = self.engine()
        result = extension_check.rate_kernel(HELPER, self.output)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["added"], {"rate_bit": 1000000, "limit": 100, "delay_ms": 0})
        self.assertEqual(result["composed"], {"rate_bit": 1000000, "limit": 100, "delay_ms": 10})
        self.assertEqual((result["before"], result["removed"]), ("noqueue", "noqueue"))
        self.assertEqual(engine.containers, {})

    def test_kernel_failures_keep_their_kind(self):
        for kwargs, kind in (({"keeps_rate": False}, "kernel_unsupported"),
                             ({"kernel_netem": False}, "kernel_unsupported"),
                             ({"grants_net_admin": False}, "permission_denied")):
            with self.subTest(kind=kind, **kwargs):
                engine = self.engine(**kwargs)
                result = extension_check.rate_kernel(HELPER, self.output)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", kind))
                self.assertEqual(engine.containers, {})

    def test_native_rate_json_is_bytes_per_second(self):
        qdisc = json.loads(NATIVE_RATE)[0]
        self.assertEqual(extension_check.netem_rate_state(qdisc, "c7e3:"),
                         {"rate_bit": 1000000, "limit": 100, "delay_ms": 0})
        # A foreign tbf prints a plain rate; it is not netem's.
        self.assertEqual(extension_check.netem_rate({"rate": 125000}), 0)
        self.assertEqual(extension_check.netem_rate({}), 0)
        with self.assertRaises(extension_check.CheckError):
            extension_check.netem_rate_state(dict(qdisc, handle="8001:"), "c7e3:")

    # The target scenario.

    def test_the_rate_limits_only_the_selected_paths_and_restores(self):
        engine = self.engine()
        result = self.scenario(engine)
        self.assertEqual(result["status"], "passed", result.get("error"))
        self.assertEqual(sorted(result["selected"]), ["peer-b ipv4 7000", "peer-b ipv6 7000"])
        self.assertEqual(sorted(result["controls"]), ["peer-c ipv4 7000", "peer-c ipv6 7000"])
        self.assertEqual([(leaf["protocol"], leaf["pref"], leaf["handle"]) for leaf in result["selectors"]],
                         [("ip", 10, "800::1"), ("ipv6", 11, "801::1")])
        self.assertEqual((result["applied"]["rate_bit"], result["applied"]["limit"]), (RATE_BIT, 1000))
        self.assertEqual(result["composed_applied"]["delay_ms"], 50)
        self.assertEqual(result["queue_applied"]["limit"], 5)
        self.assertGreater(result["after_queue"]["counters"]["drops"], 0)
        self.assertEqual(result["after_controls"]["counters"]["packets"], 0)
        self.assertEqual((result["root_after"]["kind"], result["observed_root"]["kind"]), ("noqueue", "noqueue"))
        self.assertEqual((result["filters_after"], result["shaper_left"]), ([], []))
        self.assertEqual(result["size"], 4000000)
        # Each phase's transfers, in order; only the shared one runs together.
        self.assertEqual([together for _, together in engine.sends], [False, False, False, True, False, False, False])

    def test_a_kernel_that_does_not_shape_fails_the_scenario(self):
        result = self.scenario(self.engine(shapes=False))
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "runtime_failure"))
        self.assertIn("rated peer-b ipv4 7000", result["error"])

    def test_a_queue_that_never_overflows_fails_the_scenario(self):
        result = self.scenario(self.engine(overflows=False))
        self.assertEqual(result["status"], "failed")
        self.assertIn("dropped nothing with a queue of 5 packets", result["error"])

    def test_a_shaping_helper_left_behind_fails_the_scenario(self):
        engine = self.engine()
        engine.leaves_shaper = True
        result = extension_check.rate_scenario(HELPER, TARGET, "rate2", RATE_BIT, self.output / "rate2")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["shaper_left"])

    def test_scenario_failures_report_their_kind_and_leave_nothing(self):
        for kwargs, kind in (({"grants_net_admin": False}, "permission_denied"),
                             ({"kernel_netem": False}, "kernel_unsupported")):
            with self.subTest(kind=kind):
                result = self.scenario(self.engine(**kwargs), "rate-" + kind)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", kind))

    def passing(self):
        engine = self.engine()
        result = self.scenario(engine)
        self.assertEqual(result["status"], "passed", result.get("error"))
        return result

    def test_rate_receipts_need_every_observation(self):
        base = self.passing()
        self.assertEqual(extension_check.rate_problems(base, RATE_BIT), [])
        v4, v6, c4 = "peer-b ipv4 7000", "peer-b ipv6 7000", "peer-c ipv4 7000"
        changes = {
            "a short transfer": lambda r: r["rated"]["transfers"][v4].update(received=3999999),
            "another digest": lambda r: r["baseline"]["transfers"][v6].update(received_sha256="other"),
            "a failed transfer": lambda r: r["restored"]["transfers"][v4].update(error="ConnectionResetError"),
            "IPv4 on the IPv6 path": lambda r: r["rated"]["transfers"][v6].update(family=4),
            "seen from another address": lambda r: r["rated"]["transfers"][v4].update(seen_from="::ffff:172.30.0.99"),
            "an unreadable source": lambda r: r["rated"]["transfers"][v4].update(seen_from="nowhere"),
            "a rate exceeded": lambda r: r["rated"]["transfers"][v6].update(goodput_Bps=2200000),
            "a rate far under": lambda r: r["rated"]["transfers"][v4].update(goodput_Bps=900000),
            "a slow baseline": lambda r: r["baseline"]["transfers"][c4].update(goodput_Bps=4000000),
            "a shaped control": lambda r: r["controls_rated"]["transfers"][c4].update(goodput_Bps=1880000),
            "a slow restore": lambda r: r["restored"]["transfers"][v6].update(goodput_Bps=1900000),
            "no sharing": lambda r: r["shared"].update(aggregate_Bps=3760000),
            "an undelayed composition": lambda r: r["composed"]["transfers"][v4].update(connect_ms=0.2),
            "control packets in the child": lambda r: r["after_controls"]["counters"].update(packets=3),
            "nothing counted": lambda r: r["after_rated"]["counters"].update(packets=0),
            "no queue drops": lambda r: r["after_queue"]["counters"].update(drops=0),
            "another applied rate": lambda r: r["applied"].update(rate_bit=8000000),
            "another default limit": lambda r: r["applied"].update(limit=100),
            "the delay lost in composition": lambda r: r["composed_applied"].update(delay_ms=0),
            "another queue limit": lambda r: r["queue_applied"].update(limit=1000),
            "another root after restore": lambda r: r.update(root_after={"kind": "prio", "handle": "7e00:"}),
            "another root for a new helper": lambda r: r.update(observed_root={"kind": "netem", "handle": "c7e1:"}),
            "a filter left": lambda r: r.update(filters_after=[{"kind": "u32"}]),
            "the shaper left": lambda r: r.update(shaper_left=["abc"]),
        }
        for name, change in changes.items():
            result = json.loads(json.dumps(base))
            change(result)
            with self.subTest(name):
                self.assertTrue(extension_check.rate_problems(result, RATE_BIT))

    # The command line and the report.

    def test_rate_arguments(self):
        args = extension_check.arguments(["--extension", "netem", "--image", "x", "--rate"])
        self.assertEqual((args.rate, args.rate_kbit), (True, 16000))
        self.assertFalse(extension_check.arguments(["--extension", "netem", "--image", "x"]).rate)
        for value in ("63", "10000001", "1.5", "fast"):
            with self.subTest(value), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                extension_check.arguments(["--extension", "netem", "--image", "x", "--rate", "--rate-kbit", value])

    def run_main(self, engine, *extra):
        image = dict(HELPER, labels={})

        def probe(extension, checked, args, output):
            checked.update(status="passed", checks=[], cleanup={"status": "passed", "errors": []})

        output = self.output / ("main-" + str(len(list(self.output.iterdir()))))
        original = engine.handle

        def handle(command):
            if command[:2] == ["image", "inspect"]:
                return 0, json.dumps([{"Config": {"Labels": {}}}])
            if command[0] == "info":
                return 0, json.dumps({"ServerVersion": "0.0.0-fake", "KernelVersion": "0.0.0-fake"})
            return original(command)

        engine.handle = handle
        with mock.patch.object(extension_check.check, "inspect_image", side_effect=lambda reference, _: dict(
                image if reference == "helper" else TARGET, reference=reference)), \
                mock.patch.object(extension_check, "probe", side_effect=probe), \
                mock.patch.object(extension_check, "delay_scenario", return_value={"status": "passed"}), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = extension_check.main(["--extension", "netem", "--image", "helper", "--output-dir", str(output),
                                         *extra])
        return code, json.loads((output / "extension-report.json").read_text())

    def test_without_rate_the_report_has_no_rate_profile(self):
        code, report = self.run_main(self.engine(tc_rate=False), "--functional", "--target", "t")
        self.assertEqual((code, report["status"]), (0, "passed"))
        self.assertNotIn("rate", report)

    def test_a_missing_parser_is_its_own_outcome_and_the_baseline_still_runs(self):
        code, report = self.run_main(self.engine(tc_rate=False), "--functional", "--target", "t", "--rate")
        self.assertEqual((code, report["failure_kind"]), (1, "image_unsupported"))
        rate = report["rate"]
        self.assertEqual((rate["status"], rate["stage"], rate["failure_kind"]), ("failed", "parser", "image_unsupported"))
        self.assertNotIn("kernel", rate)
        self.assertEqual(report["kernel"]["status"], "passed")
        self.assertEqual(report["targets"]["t"]["status"], "passed")

    def test_a_kernel_that_drops_the_rate_stops_before_the_targets(self):
        code, report = self.run_main(self.engine(keeps_rate=False), "--functional", "--target", "t", "--rate")
        self.assertEqual((code, report["failure_kind"]), (1, "kernel_unsupported"))
        self.assertEqual((report["rate"]["stage"], report["rate"]["parser"]["status"]), ("kernel", "passed"))
        self.assertNotIn("targets", report["rate"])

    def test_the_quick_rate_profile_is_the_parser_alone(self):
        engine = self.engine()
        code, report = self.run_main(engine, "--rate")
        self.assertEqual((code, report["rate"]["status"], report["rate"]["scope"]), (0, "passed", "parser"))
        self.assertTrue(all("--cap-add=NET_ADMIN" not in run for run in engine.runs))

    def test_the_functional_rate_profile_passes_every_stage(self):
        code, report = self.run_main(self.engine(), "--functional", "--target", "t", "--rate", "--rate-kbit", "16000")
        self.assertEqual(code, 0, report.get("error"))
        rate = report["rate"]
        self.assertEqual((rate["status"], rate["scope"], rate["rate_bit"], rate["queue_limit"]),
                         ("passed", "functional", 16000000, 1000))
        self.assertEqual((rate["parser"]["status"], rate["kernel"]["status"], rate["targets"]["t"]["status"]),
                         ("passed", "passed", "passed"))


if __name__ == "__main__":
    unittest.main()
