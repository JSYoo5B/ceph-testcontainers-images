import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check
from test_extension_check import HELPER, TARGET
from test_extension_rate import RateEngine

# Saved from the helper (iproute2 6.1.0):
# `tc -j qdisc show dev lo` after `netem delay 10ms reorder 25% 50% duplicate
# 3% 10% corrupt 2% 5%`, and after `netem duplicate 3% 10%`, which loses the
# duplicate's correlation.
NATIVE_FAULTS = ('[{"kind":"netem","handle":"c7e4:","root":true,"refcnt":2,"options":{"limit":1000,"delay":{"delay":0.01,'
                 '"jitter":0,"correlation":0},"duplicate":{"duplicate":0.03,"correlation":0.1},"reorder":{"reorder":0.25,'
                 '"correlation":0.5},"corrupt":{"corrupt":0.02,"correlation":0.05},"ecn":false,"gap":1}}]')
NATIVE_PLAIN_DUPLICATE = ('[{"kind":"netem","handle":"c7e4:","root":true,"refcnt":2,"options":{"limit":1000,'
                          '"duplicate":{"duplicate":0.03,"correlation":0},"ecn":false,"gap":0}}]')

USAGE = "Usage: ... netem [ limit PACKETS ]\n"
VALUE = re.compile(r"[0-9.]+(us|ms|%)")
# What each fault adds to the receiver's counters in one selected transfer;
# native transfers of 4 MB added 60 to 300.
EFFECTS = {"reorder": ("TcpExt", "TCPOFOQueue", 140), "duplicate": ("TcpExt", "TCPDSACKOldSent", 60),
           "corrupt": ("Tcp", "InCsumErrors", 90)}


class FaultEngine(RateEngine):
    """A Docker engine whose netem keeps reorder, duplicate and corrupt with
    correlations, as tc (iproute2 6.1) sends them, and whose receivers count
    each fault's effect in their kernel counters.

    tc_faults: whether the image's tc parses the faults; keeps_faults:
    whether the kernel keeps their correlations; keeps_gap: whether it keeps
    the reorder gap; effects: whether the faults affect the selected
    transfers; leaky: whether they also affect the unselected peer; counters:
    "ok", "missing" (no TCPOFOQueue) or "garbage"."""

    def __init__(self, tc_faults=True, keeps_faults=True, keeps_gap=True, effects=True, leaky=False, counters="ok",
                 **kwargs):
        super().__init__(**kwargs)
        self.tc_faults, self.keeps_faults, self.keeps_gap = tc_faults, keeps_faults, keeps_gap
        self.effects, self.leaky, self.counters = effects, leaky, counters
        self.interrupt_at = self.timeout_at = None
        self.fail_leaf = self.garbage_qdisc = self.network_rm_fails = self.rm_fails = False

    def namespace(self, bridge):
        identifier = super().namespace(bridge)
        for link in self.namespaces[identifier].values():
            link["counters"] = {"TcpExt": {"TCPOFOQueue": 0, "TCPDSACKOldSent": 0, "TCPDSACKOfoSent": 0},
                                "IpExt": {"InCsumErrors": 0}, "Tcp": {"InCsumErrors": 0, "InErrs": 0, "InSegs": 0}}
        return identifier

    def handle(self, command):
        if command[:2] == ["network", "rm"] and self.network_rm_fails:
            return 1, "Error response from daemon: error while removing network: network has active endpoints\n"
        if command[0] == "rm" and self.rm_fails:
            return 1, "Error response from daemon: cannot remove container\n"
        return super().handle(command)

    def exec(self, name, command):
        links = self.namespaces[self.containers[name]["namespace"]]
        if command == ["python3", "-c", extension_check.COUNTERS]:
            if self.counters == "garbage":
                return 0, "Traceback (most recent call last):\n"
            tables = json.loads(json.dumps(links["eth0"]["counters"]))
            if self.counters == "missing":
                del tables["TcpExt"]["TCPOFOQueue"]
            return 0, json.dumps(tables) + "\n"
        if command[:3] == ["python3", "-c", extension_check.OFFLOADS]:
            return 0, json.dumps({"rx_checksum": True, "tx_checksum": True, "tso": True, "gso": True, "gro": False})
        return super().exec(name, command)

    def send(self, links, arguments):
        self.sent = getattr(self, "sent", 0) + 1
        if self.sent == self.interrupt_at:
            raise KeyboardInterrupt
        if self.sent == self.timeout_at:
            raise extension_check.CheckError("Docker command timed out: docker exec", "timeout")
        source = links["eth0"]
        paths = [arguments[index:index + 3] for index in range(3, len(arguments), 3)]
        for host, port, _ in paths:
            selected = self.selected(source, host, int(port))
            child = source["child"]
            if child and self.effects and (selected or self.leaky):
                receiver = self.owner(host)["counters"]
                for fault, (table, counter, amount) in EFFECTS.items():
                    if child["faults"].get(fault):
                        receiver[table][counter] += amount
        return super().send(links, arguments)

    def tc(self, container, links, command):
        faults = [word for word in command if word in extension_check.FAULTS]
        if faults and not self.tc_faults:
            return 255, 'What is "%s"?\n%s' % (faults[0], USAGE)
        if "reorder" in command and "delay" not in command:
            return 1, "reordering not possible without specifying some delay\n" + USAGE
        if command[:2] == ["-j", "-s"] and self.garbage_qdisc and links[command[-1]]["qdisc"]:
            return 0, "qdisc netem c7e4: root refcnt 2"
        if command[:2] == ["filter", "add"] and self.fail_leaf and "ipv6" in command:
            return 2, "RTNETLINK answers: Invalid argument\n"
        return super().tc(container, links, command)

    def netem(self, options, previous=None):
        state = {"delay": 0, "jitter": 0, "rate": (previous or {}).get("rate", 0), "limit": 1000, "loss": None,
                 "faults": {}, "gap": 0}
        delayed = "delay" in options
        index = 0
        while index < len(options):
            word = options[index]
            if word == "delay":
                values = []
                while index + 1 < len(options) and len(values) < 3 and VALUE.fullmatch(options[index + 1]):
                    values.append(options[index + 1])
                    index += 1
                times = [float(value[:-2]) / (1000 if value.endswith("us") else 1) for value in values[:2]]
                state["delay"], state["jitter"] = times[0], times[1] if len(times) > 1 else 0
            elif word in extension_check.FAULTS:
                fault = [float(options[index + 1][:-1]), 0.0]
                index += 1
                if index + 1 < len(options) and options[index + 1].endswith("%"):
                    fault[1] = float(options[index + 1][:-1])
                    index += 1
                state["faults"][word] = fault
            elif word == "rate":
                state["rate"] = int(options[index + 1][:-3]) // 8 if self.keeps_rate else 0
                index += 1
            elif word == "limit":
                state["limit"] = int(options[index + 1])
                index += 1
            elif word == "loss":
                state["loss"] = 1
            index += 1
        # iproute2 6.1 drops a duplicate's correlation without a delay.
        if "duplicate" in state["faults"] and not delayed:
            state["faults"]["duplicate"][1] = 0.0
        if not self.keeps_faults:
            for fault in state["faults"].values():
                fault[1] = 0.0
        if "reorder" in state["faults"] and self.keeps_gap:
            state["gap"] = 1
        return state

    def netem_json(self, state):
        options = RateEngine.netem_json(state)
        for name, (percent, correlation) in state.get("faults", {}).items():
            options[name] = {name: percent / 100, "correlation": correlation / 100}
        options["gap"] = state.get("gap", 0)
        return options


def receipts(result):
    return json.loads(json.dumps(result))


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.output = Path(tempfile.mkdtemp())

    def engine(self, **kwargs):
        engine = FaultEngine(**kwargs)
        patch = mock.patch.object(extension_check, "docker", engine)
        patch.start()
        self.addCleanup(patch.stop)
        return engine

    def scenario(self, engine, run="faults1", empty=True):
        result = extension_check.fault_scenario(HELPER, TARGET, run, self.output / run)
        if empty:
            self.assertEqual((engine.containers, engine.networks), ({}, {}))
        return result

    # The parser: the image alone, without any capability.

    def test_a_tc_that_parses_every_fault_is_refused_by_the_kernel_without_net_admin(self):
        engine = self.engine()
        result = extension_check.faults_parser(HELPER, self.output)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(sorted(result["probes"]), ["every-fault", "zero-delay-duplicate"])
        for probe in result["probes"].values():
            self.assertIn("Operation not permitted", probe["output"])
        self.assertEqual(result["probes"]["zero-delay-duplicate"]["command"][-7:],
                         ["delay", "0us", "0us", "0%", "duplicate", "3%", "10%"])
        self.assertEqual(result["reorder_without_delay"]["outcome"], "refused_by_tc")
        self.assertEqual(engine.containers, {})
        run = engine.runs[-1]
        self.assertIn("--cap-drop=ALL", run)
        self.assertNotIn("--cap-add=NET_ADMIN", run)
        self.assertIn("--network=none", run)

    def test_a_tc_without_the_fault_grammar_is_image_unsupported(self):
        engine = self.engine(tc_faults=False)
        result = extension_check.faults_parser(HELPER, self.output)
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "image_unsupported"))
        self.assertIn('What is "reorder"?', result["error"])
        self.assertEqual(engine.containers, {})

    def test_a_parser_container_that_cannot_be_removed_fails_the_stage(self):
        engine = self.engine()
        engine.rm_fails = True
        result = extension_check.faults_parser(HELPER, self.output)
        self.assertEqual((result["status"], result["cleanup"]["status"]), ("failed", "failed"))

    # The kernel: a disposable namespace with NET_ADMIN.

    def test_the_kernel_keeps_every_fault_correlation_and_gap_and_the_handle_goes(self):
        engine = self.engine()
        result = extension_check.faults_kernel(HELPER, self.output)
        self.assertEqual(result["status"], "passed", result)
        every = result["probes"]["every-fault"]["installed"]
        self.assertEqual(every["faults"], {"reorder": {"percent": 25, "correlation": 50},
                                           "duplicate": {"percent": 3, "correlation": 10},
                                           "corrupt": {"percent": 2, "correlation": 5}})
        self.assertEqual((every["gap"], every["delay_ms"]), (1, 10))
        zero = result["probes"]["zero-delay-duplicate"]["installed"]
        self.assertEqual((zero["faults"]["duplicate"], zero["gap"], zero["delay_ms"]),
                         ({"percent": 3, "correlation": 10}, 0, 0))
        # The plain encoding loses the correlation with iproute2 6.1; it is
        # recorded and not relied on.
        self.assertFalse(result["plain_duplicate"]["correlation_kept"])
        self.assertEqual((result["before"], result["removed"]), ("noqueue", "noqueue"))
        self.assertEqual(engine.containers, {})

    def test_a_kernel_that_drops_a_correlation_or_the_gap_is_kernel_unsupported(self):
        for kwargs, words in (({"keeps_faults": False}, "correlation"), ({"keeps_gap": False}, "gap 0")):
            with self.subTest(**kwargs):
                engine = self.engine(**kwargs)
                result = extension_check.faults_kernel(HELPER, self.output)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", "kernel_unsupported"))
                self.assertIn(words, result["error"])
                self.assertEqual(engine.containers, {})

    def test_kernel_failures_keep_their_kind(self):
        for kwargs, kind in (({"kernel_netem": False}, "kernel_unsupported"),
                             ({"grants_net_admin": False}, "permission_denied")):
            with self.subTest(kind=kind):
                engine = self.engine(**kwargs)
                result = extension_check.faults_kernel(HELPER, self.output)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", kind))
                self.assertEqual(engine.containers, {})

    def test_an_unreadable_kernel_report_is_a_runtime_failure(self):
        engine = self.engine()
        engine.garbage_qdisc = True
        result = extension_check.faults_kernel(HELPER, self.output)
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "runtime_failure"))
        self.assertEqual(engine.containers, {})

    def test_a_kernel_container_that_cannot_be_removed_fails_the_stage(self):
        engine = self.engine()
        engine.rm_fails = True
        result = extension_check.faults_kernel(HELPER, self.output)
        self.assertEqual((result["status"], result["cleanup"]["status"]), ("failed", "failed"))

    def test_native_fault_json_is_read_in_percent(self):
        state = extension_check.fault_root_state(json.loads(NATIVE_FAULTS)[0])
        self.assertEqual(extension_check.fault_mismatches(extension_check.FAULT_PROBES["every-fault"], state), [])
        plain = extension_check.fault_root_state(json.loads(NATIVE_PLAIN_DUPLICATE)[0])
        self.assertEqual(plain["faults"]["duplicate"], {"percent": 3, "correlation": 0})
        self.assertTrue(extension_check.fault_mismatches(extension_check.FAULT_PROBES["zero-delay-duplicate"], plain))
        with self.assertRaises(extension_check.CheckError):
            extension_check.fault_root_state(dict(json.loads(NATIVE_FAULTS)[0], handle="8001:"))

    def test_a_missing_or_malformed_fault_member_never_matches(self):
        spec = extension_check.FAULT_PROBES["every-fault"]
        base = json.loads(NATIVE_FAULTS)[0]
        changes = {
            "no correlation": lambda o: o["reorder"].pop("correlation"),
            "a text correlation": lambda o: o["corrupt"].update(correlation="5%"),
            "no member": lambda o: o.pop("duplicate"),
            "a plain number": lambda o: o.update(corrupt=0.02),
            "no gap": lambda o: o.pop("gap"),
            "gap 5": lambda o: o.update(gap=5),
            "another correlation": lambda o: o["duplicate"].update(correlation=0.2),
            "an extra delay": lambda o: o["delay"].update(jitter=0.002),
        }
        for name, change in changes.items():
            qdisc = json.loads(json.dumps(base))
            change(qdisc["options"])
            with self.subTest(name):
                self.assertTrue(extension_check.fault_mismatches(spec, extension_check.fault_root_state(qdisc)))
        extra = extension_check.fault_root_state(base)
        self.assertTrue(extension_check.fault_mismatches(extension_check.FAULT_PROBES["zero-delay-duplicate"], extra))

    # The target scenario.

    def test_each_fault_affects_only_the_selected_paths_and_restores(self):
        engine = self.engine()
        result = self.scenario(engine)
        self.assertEqual(result["status"], "passed", result.get("error"))
        self.assertEqual(sorted(result["selected"]), ["peer-b ipv4 7000", "peer-b ipv6 7000"])
        self.assertEqual(sorted(result["controls"]), ["peer-c ipv4 7000", "peer-c ipv6 7000"])
        self.assertEqual(list(result["effects"]), ["reorder", "duplicate", "corrupt", "composed"])
        reorder = result["effects"]["reorder"]
        self.assertEqual((reorder["applied"]["faults"]["reorder"], reorder["applied"]["gap"]),
                         ({"percent": 25, "correlation": 25}, 1))
        duplicate = result["effects"]["duplicate"]
        self.assertEqual((duplicate["applied"]["faults"]["duplicate"], duplicate["applied"]["delay_ms"]),
                         ({"percent": 50, "correlation": 10}, 0))
        composed = result["effects"]["composed"]["applied"]
        self.assertEqual((composed["rate_bit"], composed["delay_ms"], composed["gap"]), (100000000, 5, 0))
        for effect, phase in result["effects"].items():
            with self.subTest(effect):
                self.assertEqual(phase["after_controls"]["counters"]["packets"], 0)
                self.assertEqual((phase["root_after"]["kind"], phase["filters_after"]), ("noqueue", []))
                for counter in extension_check.FAULT_EFFECTS[effect]["counters"]:
                    for observation in phase["selected"].values():
                        self.assertGreaterEqual(observation["counters"][counter], 60)
                    for observation in phase["controls"].values():
                        self.assertEqual(observation["counters"][counter], 0)
        for observation in result["restored"].values():
            self.assertEqual({key: observation["counters"][key] for key in extension_check.EFFECT_COUNTERS},
                             {"ofo_queue": 0, "dsack_sent": 0, "csum_errors": 0})
        self.assertEqual((result["observed_root"]["kind"], result["observed_filters"], result["shaper_left"]),
                         ("noqueue", [], []))
        self.assertEqual(result["offloads"]["sender"]["tx_checksum"], True)
        # Every transfer alone: 4 baseline, 4 for each of the 4 effects, 2 restored.
        self.assertEqual(len(engine.sends), 22)
        self.assertFalse(any(together for _, together in engine.sends))

    def test_an_absent_effect_never_passes(self):
        result = self.scenario(self.engine(effects=False))
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "runtime_failure"))
        for effect, counter in (("reorder", "ofo_queue"), ("duplicate", "dsack_sent"), ("corrupt", "csum_errors"),
                                ("composed", "csum_errors")):
            self.assertIn("%s peer-b ipv6 7000: %s rose by 0, expected at least 20" % (effect, counter), result["error"])

    def test_an_effect_on_the_unselected_peer_fails(self):
        result = self.scenario(self.engine(leaky=True))
        self.assertEqual(result["status"], "failed")
        self.assertIn("reorder control peer-c ipv4 7000: ofo_queue rose by 140", result["error"])

    def test_a_kernel_that_drops_the_correlation_fails_the_scenario(self):
        result = self.scenario(self.engine(keeps_faults=False))
        self.assertEqual(result["status"], "failed")
        self.assertIn("reorder applied: reorder is 25.0% with correlation 0.0%", result["error"])

    def test_missing_or_unreadable_receiver_counters_fail_without_a_pass(self):
        for counters, words in (("missing", "no TcpExt.TCPOFOQueue counter"), ("garbage", "is not JSON")):
            with self.subTest(counters):
                result = self.scenario(self.engine(counters=counters), "faults-" + counters)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", "runtime_failure"))
                self.assertIn(words, result["error"])

    def test_a_partial_apply_fails_and_leaves_nothing(self):
        engine = self.engine()
        engine.fail_leaf = True
        result = self.scenario(engine)
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "runtime_failure"))
        self.assertIn("Invalid argument", result["error"])
        self.assertNotIn("reorder", {key for key, value in result["effects"].items() if "applied" in value})

    def test_a_cancelled_scenario_cleans_up_and_records_it(self):
        engine = self.engine()
        engine.interrupt_at = 6
        with self.assertRaises(KeyboardInterrupt):
            extension_check.fault_scenario(HELPER, TARGET, "faults-cancel", self.output / "faults-cancel")
        self.assertEqual((engine.containers, engine.networks), ({}, {}))
        saved = json.loads((self.output / "faults-cancel" / "result.json").read_text())
        self.assertEqual((saved["status"], saved["cancelled"], saved["cleanup"]["status"]), ("failed", True, "passed"))

    def test_a_transfer_that_times_out_keeps_its_kind(self):
        engine = self.engine()
        engine.timeout_at = 7
        result = self.scenario(engine)
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "timeout"))

    def test_a_cleanup_failure_fails_a_passing_scenario(self):
        engine = self.engine()
        engine.network_rm_fails = True
        result = self.scenario(engine, empty=False)
        self.assertEqual((result["status"], result["cleanup"]["status"]), ("failed", "failed"))
        self.assertTrue(result["cleanup"]["leftovers"])

    def test_scenario_failures_report_their_kind_and_leave_nothing(self):
        for kwargs, kind in (({"grants_net_admin": False}, "permission_denied"),
                             ({"kernel_netem": False}, "kernel_unsupported")):
            with self.subTest(kind=kind):
                result = self.scenario(self.engine(**kwargs), "faults-" + kind)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", kind))

    def test_fault_receipts_need_every_observation(self):
        base = self.scenario(self.engine())
        self.assertEqual(base["status"], "passed", base.get("error"))
        self.assertEqual(extension_check.fault_problems(receipts(base)), [])
        v4, v6, c4 = "peer-b ipv4 7000", "peer-b ipv6 7000", "peer-c ipv4 7000"
        changes = {
            "a short transfer": lambda r: r["effects"]["corrupt"]["selected"][v4]["transfer"].update(received=3999999),
            "another digest": lambda r: r["effects"]["duplicate"]["selected"][v6]["transfer"].update(
                received_sha256="other"),
            "a failed control": lambda r: r["effects"]["reorder"]["controls"][c4]["transfer"].update(error="reset"),
            "IPv4 on the IPv6 path": lambda r: r["effects"]["reorder"]["selected"][v6]["transfer"].update(family=4),
            "a failed restore": lambda r: r["restored"][v4]["transfer"].update(error="ConnectionResetError"),
            "a missing correlation": lambda r: r["effects"]["duplicate"]["applied"]["faults"]["duplicate"].update(
                correlation=None),
            "another percent": lambda r: r["effects"]["corrupt"]["applied"]["faults"]["corrupt"].update(percent=5),
            "no gap": lambda r: r["effects"]["reorder"]["applied"].update(gap=0),
            "a stale reorder": lambda r: r["effects"]["duplicate"]["applied"]["faults"].update(
                reorder={"percent": 25, "correlation": 25}),
            "the rate lost": lambda r: r["effects"]["composed"]["applied"].update(rate_bit=0),
            "control packets in the child": lambda r: r["effects"]["corrupt"]["after_controls"]["counters"].update(
                packets=2),
            "nothing selected": lambda r: r["effects"]["reorder"]["after_selected"]["counters"].update(packets=0),
            "an effect under the floor": lambda r: r["effects"]["reorder"]["selected"][v4]["counters"].update(
                ofo_queue=19),
            "noise as large as the effect": lambda r: r["baseline"][c4]["counters"].update(dsack_sent=55),
            "a composed effect missing": lambda r: r["effects"]["composed"]["selected"][v6]["counters"].update(
                csum_errors=0),
            "an effect at the control": lambda r: r["effects"]["corrupt"]["controls"][c4]["counters"].update(
                csum_errors=11),
            "an effect after restore": lambda r: r["restored"][v6]["counters"].update(ofo_queue=11),
            "a root left by an effect": lambda r: r["effects"]["duplicate"].update(
                root_after={"kind": "prio", "handle": "7e00:"}),
            "a filter left by an effect": lambda r: r["effects"]["corrupt"].update(filters_after=[{"kind": "u32"}]),
            "another root for a new helper": lambda r: r.update(observed_root={"kind": "netem", "handle": "c7e4:"}),
            "a filter for a new helper": lambda r: r.update(observed_filters=[{"kind": "u32"}]),
            "the shaper left": lambda r: r.update(shaper_left=["abc"]),
        }
        for name, change in changes.items():
            result = receipts(base)
            change(result)
            with self.subTest(name):
                self.assertTrue(extension_check.fault_problems(result))

    def test_counter_deltas_are_effects_of_raw_counters(self):
        before = {"TcpExt.TCPOFOQueue": 1, "TcpExt.TCPDSACKOldSent": 2, "TcpExt.TCPDSACKOfoSent": 3,
                  "IpExt.InCsumErrors": 4, "Tcp.InCsumErrors": 5, "Tcp.InErrs": 6, "Tcp.InSegs": 7}
        after = dict(before, **{"TcpExt.TCPDSACKOfoSent": 5, "IpExt.InCsumErrors": 5, "Tcp.InCsumErrors": 8})
        delta = extension_check.counter_delta(before, after)
        self.assertEqual((delta["ofo_queue"], delta["dsack_sent"], delta["csum_errors"]), (0, 2, 4))
        with self.assertRaises(extension_check.CheckError):
            extension_check.counter_delta(after, before)

    # The command line and the report.

    def test_packet_fault_arguments(self):
        self.assertTrue(extension_check.arguments(["--extension", "netem", "--image", "x", "--packet-faults"]).packet_faults)
        self.assertFalse(extension_check.arguments(["--extension", "netem", "--image", "x"]).packet_faults)

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

    def test_without_packet_faults_the_report_has_no_packet_fault_profile(self):
        code, report = self.run_main(self.engine(tc_faults=False), "--functional", "--target", "t", "--rate")
        self.assertEqual((code, report["status"]), (0, "passed"))
        self.assertNotIn("packet_faults", report)

    def test_a_missing_parser_is_its_own_outcome_and_the_rate_and_baseline_still_run(self):
        code, report = self.run_main(self.engine(tc_faults=False), "--functional", "--target", "t", "--rate",
                                     "--packet-faults")
        self.assertEqual((code, report["failure_kind"]), (1, "image_unsupported"))
        faults = report["packet_faults"]
        self.assertEqual((faults["status"], faults["stage"], faults["failure_kind"]),
                         ("failed", "parser", "image_unsupported"))
        self.assertNotIn("kernel", faults)
        self.assertEqual((report["kernel"]["status"], report["targets"]["t"]["status"]), ("passed", "passed"))
        self.assertEqual(report["rate"]["status"], "passed")

    def test_a_kernel_that_drops_correlations_stops_before_the_targets(self):
        code, report = self.run_main(self.engine(keeps_faults=False), "--functional", "--target", "t",
                                     "--packet-faults")
        self.assertEqual((code, report["failure_kind"]), (1, "kernel_unsupported"))
        self.assertEqual((report["packet_faults"]["stage"], report["packet_faults"]["parser"]["status"]),
                         ("kernel", "passed"))
        self.assertNotIn("targets", report["packet_faults"])

    def test_a_failed_target_scenario_fails_the_profile_with_its_kind(self):
        code, report = self.run_main(self.engine(effects=False), "--functional", "--target", "t", "--packet-faults")
        self.assertEqual((code, report["failure_kind"]), (1, "runtime_failure"))
        self.assertEqual((report["packet_faults"]["stage"], report["packet_faults"]["targets"]["t"]["status"]),
                         ("targets", "failed"))

    def test_the_quick_packet_fault_profile_is_the_parser_alone(self):
        engine = self.engine()
        code, report = self.run_main(engine, "--packet-faults")
        self.assertEqual((code, report["packet_faults"]["status"], report["packet_faults"]["scope"]),
                         (0, "passed", "parser"))
        self.assertTrue(all("--cap-add=NET_ADMIN" not in run for run in engine.runs))

    def test_the_functional_packet_fault_profile_passes_every_stage(self):
        code, report = self.run_main(self.engine(), "--functional", "--target", "t", "--packet-faults")
        self.assertEqual(code, 0, report.get("error"))
        faults = report["packet_faults"]
        self.assertEqual((faults["status"], faults["scope"]), ("passed", "functional"))
        self.assertEqual((faults["parser"]["status"], faults["kernel"]["status"], faults["targets"]["t"]["status"]),
                         ("passed", "passed", "passed"))
        self.assertEqual(sorted(faults["unsupported"]), ["reorder with jitter", "reorder with rate",
                                                         "reorder without delay"])
        self.assertEqual(faults["effects"]["duplicate"]["netem"], ["delay", "0us", "0us", "0%", "duplicate", "50%", "10%"])


if __name__ == "__main__":
    unittest.main()
