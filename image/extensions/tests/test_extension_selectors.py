import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check

# Saved from the helper (iproute2 6.1.0): `tc -j filter show dev eth0` in the namespace of a target
# whose peer B had 172.19.0.3 and fd62:93fe:2c9e::3, after the owned root,
# child and one selector of each family. tc repeats "match" for each key.
IPV4_LEAF = ('{"parent":"7e00:","protocol":"ip","pref":10,"kind":"u32","chain":0,"options":{"fh":"800::1","order":1,'
             '"key_ht":"800","bkt":"0","flowid":"7e00:3","not_in_hw":true,'
             '"match":{"value":"ac130003","mask":"ffffffff","offmask":"","off":16},'
             '"match":{"value":"60000","mask":"ff0000","offmask":"","off":8},'
             '"match":{"value":"1b58","mask":"ffff","offmask":"","off":20}}}')
IPV6_LEAF = ('{"parent":"7e00:","protocol":"ipv6","pref":10,"kind":"u32","chain":0,"options":{"fh":"800::1","order":1,'
             '"key_ht":"800","bkt":"0","flowid":"7e00:3","not_in_hw":true,'
             '"match":{"value":"fd6293fe","mask":"ffffffff","offmask":"","off":24},'
             '"match":{"value":"2c9e0000","mask":"ffffffff","offmask":"","off":28},'
             '"match":{"value":"0","mask":"ffffffff","offmask":"","off":32},'
             '"match":{"value":"3","mask":"ffffffff","offmask":"","off":36},'
             '"match":{"value":"600","mask":"ff00","offmask":"","off":4},'
             '"match":{"value":"1b58","mask":"ffff","offmask":"","off":40}}}')
FIRST_WORD = '"match":{"value":"fd6293fe","mask":"ffffffff","offmask":"","off":24}'
SECOND_WORD = '"match":{"value":"2c9e0000","mask":"ffffffff","offmask":"","off":28}'


def dump(protocol, leaf):
    """A filter dump with the u32 table and header entries before the leaf."""
    head = '{"parent":"7e00:","protocol":"%s","pref":10,"kind":"u32","chain":0' % protocol
    return "[" + head + "}," + head + ',"options":{"fh":"800:","ht_divisor":1}},' + leaf + "]"


IPV4_DUMP = dump("ip", IPV4_LEAF)
IPV6_DUMP = dump("ipv6", IPV6_LEAF)

# `tc -j -s qdisc show dev eth0` after the owned root and child.
QDISCS = ('[{"kind":"prio","handle":"7e00:","root":true,"refcnt":5,"options":{"bands":3,'
          '"priomap":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],"multiqueue":false},"bytes":0,"packets":0,"drops":0,'
          '"overlimits":0,"requeues":0,"backlog":0,"qlen":0},{"kind":"netem","handle":"7e10:","parent":"7e00:3",'
          '"options":{"limit":1000,"delay":{"delay":0.12,"jitter":0,"correlation":0},"ecn":false,"gap":0},'
          '"bytes":0,"packets":0,"drops":0,"overlimits":0,"requeues":0,"backlog":0,"qlen":0}]')


BUSY = QDISCS.replace('"backlog":0,"qlen":0}]', '"backlog":1514,"qlen":1}]')


class Clock:
    """A monotonic clock that only Docker reads and sleeps advance."""

    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DrainTests(unittest.TestCase):
    """drained() against a controlled clock; each read is (seconds, output),
    or (seconds, None) for a read that Docker abandons at its timeout."""

    def run_drain(self, reads):
        clock, timeouts = Clock(), []
        reads = list(reads)

        def docker(*command, timeout=120, check_exit=True):
            timeouts.append(timeout)
            seconds, output = reads.pop(0)
            if output is None:
                clock.now += timeout
                raise extension_check.CheckError("Docker command timed out", "timeout")
            clock.now += seconds
            return 0, output

        with mock.patch.object(extension_check, "docker", docker), \
                mock.patch.object(extension_check.time, "monotonic", clock.monotonic), \
                mock.patch.object(extension_check.time, "sleep", clock.sleep):
            return extension_check.drained("shaper", "eth0"), timeouts

    def test_an_empty_queue_within_the_budget_is_drained(self):
        drain, timeouts = self.run_drain([(0.1, BUSY), (0.1, QDISCS)])
        self.assertEqual(drain["status"], "drained")
        self.assertEqual(drain["elapsed_ms"], 250.0)
        self.assertEqual(timeouts[0], 2.0)

    def test_an_empty_queue_observed_after_the_budget_is_not_drained(self):
        drain, _ = self.run_drain([(3.0, QDISCS)])
        self.assertEqual(drain["status"], "drain_pending")
        self.assertEqual(drain["reason"], "observed_after_deadline")
        self.assertEqual(drain["elapsed_ms"], 3000.0)

    def test_a_read_that_times_out_is_not_drained(self):
        drain, timeouts = self.run_drain([(0.1, BUSY), (0, None)])
        self.assertEqual(drain["status"], "drain_pending")
        self.assertEqual(drain["reason"], "read_timeout")
        self.assertEqual(drain["counters"]["backlog"], 1514)
        self.assertEqual([round(timeout, 3) for timeout in timeouts], [2.0, 1.85])
        self.assertEqual(drain["elapsed_ms"], 2000.0)

    def test_a_queue_busy_until_the_budget_is_not_drained(self):
        drain, timeouts = self.run_drain([(0.25, BUSY)] * 20)
        self.assertEqual(drain["status"], "drain_pending")
        self.assertEqual(drain["counters"]["qlen"], 1)
        self.assertLessEqual(drain["elapsed_ms"], 2050.0)
        # Each later read is bounded by what remains of the budget, and no
        # read starts after it.
        self.assertEqual([round(timeout, 3) for timeout in timeouts], [2.0, 1.7, 1.4, 1.1, 0.8, 0.5, 0.2])

    def test_loss_follows_only_a_drained_queue(self):
        calls = []
        with mock.patch.object(extension_check, "tc", lambda *command, **_: calls.append(command)):
            for drain in ({"status": "drain_pending", "reason": "read_timeout"},
                          {"status": "drain_pending", "reason": "observed_after_deadline"}):
                with self.subTest(drain["reason"]), self.assertRaises(extension_check.CheckError):
                    extension_check.apply_loss("shaper", "eth0", drain)
            self.assertEqual(calls, [])
            extension_check.apply_loss("shaper", "eth0", {"status": "drained"})
        self.assertEqual(calls, [("shaper", "qdisc", "change", "dev", "eth0", "parent", "7e00:3", "handle", "7e10:",
                                  "netem", "loss", "100%")])


def leaves(profile, **peers):
    """The layout of a profile with the peers' addresses filled in."""
    addresses = {("peer-b", 4): "172.19.0.3", ("peer-b", 6): "fd62:93fe:2c9e::3", ("peer-c", 4): "172.19.0.4"}
    addresses.update(peers)
    layout = extension_check.layout(profile)
    for leaf in layout:
        leaf["peer"] = addresses[(leaf["role"], leaf["family"])]
    return layout


# The multi-peer-u32 profile's dump: a second IPv4 leaf for peer C on port
# 7001 in the same table, and the IPv6 leaf in preference 11 and table 801:.
IPV4_SECOND_LEAF = ('{"parent":"7e00:","protocol":"ip","pref":10,"kind":"u32","chain":0,"options":{"fh":"800::2",'
                    '"order":2,"key_ht":"800","bkt":"0","flowid":"7e00:3","not_in_hw":true,'
                    '"match":{"value":"ac130004","mask":"ffffffff","offmask":"","off":16},'
                    '"match":{"value":"60000","mask":"ff0000","offmask":"","off":8},'
                    '"match":{"value":"1b59","mask":"ffff","offmask":"","off":20}}}')
MULTI_DUMP = (IPV4_DUMP[:-1] + "," + IPV4_SECOND_LEAF + "," +
              dump("ipv6", IPV6_LEAF)[1:].replace('"pref":10', '"pref":11').replace('"fh":"800:"', '"fh":"801:"')
              .replace('"fh":"800::1"', '"fh":"801::1"').replace('"key_ht":"800"', '"key_ht":"801"'))


class SelectorTests(unittest.TestCase):
    def test_a_dict_keeps_only_the_last_match(self):
        # The reason for Pairs: plain decoding sees the port key alone.
        options = json.loads(IPV6_LEAF)["options"]
        self.assertEqual(options["match"]["off"], 40)
        pairs = json.loads(IPV6_LEAF, object_pairs_hook=extension_check.Pairs).get("options")
        self.assertEqual([match.get("off") for match in pairs.every("match")], [24, 28, 32, 36, 4, 40])

    def test_requested_keys_are_word_aligned(self):
        self.assertEqual([(key["off"], key["value"], key["mask"]) for key in
                          extension_check.u32_keys(6, "fd62:93fe:2c9e::3", 7000)],
                         [(24, "fd6293fe", "ffffffff"), (28, "2c9e0000", "ffffffff"), (32, "00000000", "ffffffff"),
                          (36, "00000003", "ffffffff"), (4, "00000600", "0000ff00"), (40, "00001b58", "0000ffff")])
        self.assertEqual([(key["off"], key["value"], key["mask"]) for key in
                          extension_check.u32_keys(4, "172.19.0.3", 7000)],
                         [(16, "ac130003", "ffffffff"), (8, "00060000", "00ff0000"), (20, "00001b58", "0000ffff")])

    def test_layouts_put_each_family_in_its_own_preference_and_table(self):
        def summary(profile):
            return [(leaf["protocol"], leaf["pref"], leaf["table"], leaf["handle"], leaf["role"], leaf["port"])
                    for leaf in extension_check.layout(profile)]
        self.assertEqual(summary("ipv4-tcp-u32"), [("ip", 10, "800:", "800::1", "peer-b", 7000)])
        # A family alone starts at preference 10 and table 800:.
        self.assertEqual(summary("ipv6-tcp-u32"), [("ipv6", 10, "800:", "800::1", "peer-b", 7000)])
        self.assertEqual(summary("multi-peer-u32"), [("ip", 10, "800:", "800::1", "peer-b", 7000),
                                                     ("ip", 10, "800:", "800::2", "peer-c", 7001),
                                                     ("ipv6", 11, "801:", "801::1", "peer-b", 7000)])

    def test_native_selectors_are_the_requested_ones(self):
        for profile, raw in (("ipv4-tcp-u32", IPV4_DUMP), ("ipv6-tcp-u32", IPV6_DUMP), ("multi-peer-u32", MULTI_DUMP)):
            with self.subTest(profile):
                requested = leaves(profile)
                found = extension_check.selector_leaves(raw, requested)
                self.assertEqual([(leaf["handle"], leaf["flowid"]) for leaf in found],
                                 [(leaf["handle"], "7e00:3") for leaf in requested])
                for leaf, receipt in zip(requested, found):
                    self.assertEqual(receipt["keys"], extension_check.u32_keys(leaf["family"], leaf["peer"], leaf["port"]))

    def test_a_selector_that_differs_anywhere_is_refused(self):
        changes = {
            "missing address word": IPV6_LEAF.replace('"match":{"value":"0","mask":"ffffffff","offmask":"","off":32},', ""),
            "another address": IPV6_LEAF.replace('"value":"3"', '"value":"4"'),
            "another protocol": IPV6_LEAF.replace('"value":"600"', '"value":"1100"'),
            "wide protocol mask": IPV6_LEAF.replace('"mask":"ff00"', '"mask":"ffff"'),
            "another port": IPV6_LEAF.replace('"value":"1b58"', '"value":"1b59"'),
            "port mask": IPV6_LEAF.replace('"value":"1b58","mask":"ffff"', '"value":"1b58","mask":"ff"'),
            "literal port offset": IPV6_LEAF.replace('"off":40', '"off":42'),
            "dynamic offset": IPV6_LEAF.replace('"offmask":"","off":40', '"offmask":"0f00","off":40'),
            "other order": IPV6_LEAF.replace(FIRST_WORD + "," + SECOND_WORD, SECOND_WORD + "," + FIRST_WORD),
            "other flowid": IPV6_LEAF.replace('"flowid":"7e00:3"', '"flowid":"7e00:1"'),
            "other leaf": IPV6_LEAF.replace('"fh":"800::1"', '"fh":"800::2"'),
            "unreadable value": IPV6_LEAF.replace('"value":"3"', '"value":"x3"'),
        }
        for name, leaf in changes.items():
            with self.subTest(name), self.assertRaises(extension_check.CheckError) as raised:
                extension_check.selector_leaves(dump("ipv6", leaf), leaves("ipv6-tcp-u32"))
            self.assertEqual(raised.exception.kind, "runtime_failure")

    def test_foreign_or_missing_filter_state_is_refused(self):
        foreign = IPV4_DUMP[:-1] + ',' + IPV4_LEAF.replace('"pref":10', '"pref":20') + "]"
        cases = {"foreign preference": (foreign, "ipv4-tcp-u32"),
                 "other family": (IPV4_DUMP, "ipv6-tcp-u32"),
                 "other parent": (IPV4_DUMP.replace('"parent":"7e00:"', '"parent":"1:"'), "ipv4-tcp-u32"),
                 "header only": (dump("ip", "")[:-2] + "]", "ipv4-tcp-u32"),
                 "two leaves": (IPV4_DUMP[:-1] + "," + IPV4_LEAF + "]", "ipv4-tcp-u32"),
                 "no filter": ("[]", "ipv4-tcp-u32"),
                 "another table": (IPV4_DUMP.replace('"fh":"800:"', '"fh":"802:"'), "ipv4-tcp-u32"),
                 # Of several leaves, a missing one, or one in another
                 # preference or table, is not the requested hierarchy.
                 "a missing second leaf": (MULTI_DUMP.replace(IPV4_SECOND_LEAF + ",", ""), "multi-peer-u32"),
                 "the IPv6 leaf in preference 10": (IPV4_DUMP[:-1] + "," + IPV4_SECOND_LEAF + "," + IPV6_LEAF + "]",
                                                    "multi-peer-u32"),
                 "the IPv6 leaf in table 800:": (MULTI_DUMP.replace('"fh":"801::1"', '"fh":"800::3"'), "multi-peer-u32"),
                 "swapped IPv4 leaves": (MULTI_DUMP.replace('"value":"1b59"', '"value":"1b58"'), "multi-peer-u32")}
        for name, (raw, profile) in cases.items():
            with self.subTest(name), self.assertRaises(extension_check.CheckError):
                extension_check.selector_leaves(raw, leaves(profile))

    def test_only_the_owned_tables_may_outlive_their_leaves(self):
        def entries(raw):
            return json.loads(raw, object_pairs_hook=extension_check.Pairs)

        single, multi = leaves("ipv4-tcp-u32"), leaves("multi-peer-u32")
        self.assertEqual(extension_check.remaining_filters(entries("[]"), single), [])
        table = dump("ip", "")[:-2] + "]"
        self.assertEqual(extension_check.remaining_filters(entries(table), single), [("ip", 10)])
        tables = table[:-1] + "," + table[1:].replace('"protocol":"ip","pref":10', '"protocol":"ipv6","pref":11')
        self.assertEqual(extension_check.remaining_filters(entries(tables), multi), [("ip", 10), ("ipv6", 11)])
        for name, raw in {"the leaf": IPV4_DUMP, "another family": dump("ipv6", "")[:-2] + "]",
                          "another preference": table.replace('"pref":10', '"pref":20')}.items():
            with self.subTest(name), self.assertRaises(extension_check.CheckError):
                extension_check.remaining_filters(entries(raw), single)

    def test_qdiscs_must_be_the_owned_prio_root_and_netem_child(self):
        state = extension_check.selector_qdiscs(QDISCS)
        self.assertEqual(state["delay_ms"], 120)
        self.assertEqual(state["counters"], {"packets": 0, "bytes": 0, "drops": 0, "backlog": 0, "qlen": 0})
        for name, raw in {"noqueue root": '[{"kind":"noqueue","handle":"0:","root":true,"options":{}}]',
                          "other root handle": QDISCS.replace('"handle":"7e00:"', '"handle":"1:"'),
                          "other priomap": QDISCS.replace('"priomap":[0,0', '"priomap":[1,0'),
                          "no child": QDISCS.replace('"handle":"7e10:"', '"handle":"7e11:"'),
                          "child elsewhere": QDISCS.replace('"parent":"7e00:3"', '"parent":"7e00:1"')}.items():
            with self.subTest(name), self.assertRaises(extension_check.CheckError):
                extension_check.selector_qdiscs(raw)

    def test_hierarchy_commands_add_leaves_last_and_remove_them_first(self):
        calls = []

        def tc(container, *command, **_):
            calls.append(command)
            return "[]"

        layout = leaves("multi-peer-u32")
        with mock.patch.object(extension_check, "tc", tc):
            extension_check.install_hierarchy("shaper", "eth0", layout, 120)
            installed, calls[:] = list(calls), []
            self.assertEqual(extension_check.remove_hierarchy("shaper", "eth0", layout), [])
        self.assertEqual([command[:2] for command in installed], [("qdisc", "add"), ("qdisc", "add")] + [("filter", "add")] * 3)
        self.assertEqual([command[command.index("handle") + 1] for command in installed[2:]], ["800::1", "800::2", "801::1"])
        self.assertEqual(installed[4][installed[4].index("protocol") + 1], "ipv6")
        self.assertEqual([command[command.index("handle") + 1] for command in calls if command[:2] == ("filter", "del")],
                         ["801::1", "800::2", "800::1"])
        self.assertEqual(calls[-1], ("qdisc", "del", "dev", "eth0", "root", "handle", "7e00:"))

    def passing(self, delay=120, profile="multi-peer-u32"):
        fast = {"count": 3, "answered": 3, "outcomes": ["answered"], "median_ms": 0.07}
        layout = leaves(profile)
        selected = [(extension_check.reverse_name if leaf["match"] == "sport" else extension_check.path_name)(
            leaf["role"], leaf["family"], leaf["port"]) for leaf in layout]
        setup = [extension_check.path_name(leaf["role"], leaf["family"], leaf["port"])
                 for leaf in layout if leaf["match"] == "setup"]
        controls = {name: dict(fast) for name in ("peer-d ipv4 7000", "peer-c ipv6 7001",
                                                  extension_check.reverse_name("peer-b", 4, extension_check.REVERSE_PORT))}
        state = {"delay_ms": delay, "counters": {"packets": 0, "drops": 0}}
        return {"selected": [dict(handle="800::%d" % n, match=leaf["match"], role=leaf["role"], family=leaf["family"],
                                  port=leaf["port"]) for n, leaf in enumerate(layout)], "sandbox_id": "a",
                "established_baseline": {name: dict(fast, count=5, answered=5) for name in setup},
                "established_delayed": {name: dict(fast, count=5, answered=5) for name in setup},
                "established_lost": {name: dict(fast) for name in setup},
                "baseline": {name: dict(fast, count=5, answered=5) for name in selected},
                "partial": {name: dict(fast) for name in selected},
                "restored": {name: dict(fast, count=5, answered=5) for name in selected},
                "controls_delayed": controls, "controls_lost": json.loads(json.dumps(controls)),
                "after_partial": state, "applied": state, "after_controls": state,
                "delayed": {name: {"count": 5, "answered": 5, "outcomes": ["answered"], "median_ms": 250.0} for name in selected},
                "after_delayed": {"counters": {"packets": 23, "drops": 0}},
                "lost": {name: {"count": 1, "answered": 0, "outcomes": ["timeout"], "median_ms": None} for name in selected},
                "after_loss": {"counters": {"packets": 25, "drops": 1}},
                "foreign": {"refused": "A filter that the check does not own", "after_removal": len(selected)},
                "after_helper_exit": {"selectors": len(selected), "loss": {"loss": 1, "correlation": 0}},
                "root_before": {"kind": "noqueue", "handle": "0:"}, "root_after": {"kind": "noqueue", "handle": "0:"},
                "filters_after": [],
                "restart": {"sandbox_id": "b", "old_namespace_interfaces": [], "new_root": "noqueue", "new_filters": []}}

    def test_selector_receipts_need_every_observation(self):
        self.assertEqual(extension_check.selector_problems(self.passing(), 120), [])
        changes = {
            "a slow control": lambda r: r["controls_delayed"]["peer-d ipv4 7000"].update(median_ms=125.0),
            "a control that failed under loss": lambda r: r["controls_lost"]["peer-b to target ipv4 6999"].update(answered=2),
            "control traffic in the child": lambda r: r.update(after_controls={"delay_ms": 120, "counters": {"packets": 2}}),
            "a partial hierarchy that delays": lambda r: r["partial"]["peer-c ipv4 7001"].update(median_ms=121.0),
            "a partial hierarchy that counts": lambda r: r.update(after_partial={"delay_ms": 120, "counters": {"packets": 1}}),
            "one undelayed selected path": lambda r: r["delayed"]["peer-c ipv4 7001"].update(median_ms=0.08),
            "a selected request lost under delay": lambda r: r["delayed"]["peer-b ipv6 7000"].update(answered=4),
            "nothing counted": lambda r: r["after_delayed"]["counters"].update(packets=0),
            "one answer under loss": lambda r: r["lost"]["peer-b ipv4 7000"].update(outcomes=["answered"], answered=1),
            "no drops": lambda r: r["after_loss"]["counters"].update(drops=0),
            "foreign state accepted": lambda r: r["foreign"].update(refused=None),
            "a leaf lost with the foreign filter": lambda r: r["foreign"].update(after_removal=2),
            "a leaf gone with the helper": lambda r: r["after_helper_exit"].update(selectors=2),
            "loss gone with the helper": lambda r: r["after_helper_exit"].update(loss=None),
            "another root after restore": lambda r: r.update(root_after={"kind": "prio", "handle": "7e00:"}),
            "a filter left": lambda r: r.update(filters_after=[{"kind": "u32"}]),
            "a slow restore": lambda r: r["restored"]["peer-b ipv6 7000"].update(median_ms=119.0),
            "another applied delay": lambda r: r.update(applied={"delay_ms": 60, "counters": {"packets": 0}}),
            "the same sandbox after restart": lambda r: r["restart"].update(sandbox_id="a"),
            "the old namespace keeps the interface": lambda r: r["restart"].update(old_namespace_interfaces=["eth0"]),
            "filters in the new namespace": lambda r: r["restart"].update(new_filters=[{"kind": "u32"}]),
            "another root in the new namespace": lambda r: r["restart"].update(new_root="prio"),
        }
        for name, change in changes.items():
            result = self.passing()
            change(result)
            with self.subTest(name):
                self.assertTrue(extension_check.selector_problems(result, 120))

    def test_setup_receipts_need_established_connections_that_stay_fast(self):
        for profile in ("multi-peer-u32", "tcp-sport-u32", "tcp-setup-u32"):
            with self.subTest(profile):
                self.assertEqual(extension_check.selector_problems(self.passing(profile=profile), 120), [])
        path = "peer-b ipv6 7000"
        changes = {
            "an established connection delayed with the setup": lambda r: r["established_delayed"][path].update(median_ms=125.0),
            "an established connection that lost a reply under loss": lambda r: r["established_lost"][path].update(
                answered=2, outcomes=["answered", "timeout"]),
            "an established connection closed under loss": lambda r: r["established_lost"][path].update(
                answered=0, outcomes=["closed", "timeout"], median_ms=None),
            "an established connection slow before the leaves": lambda r: r["established_baseline"][path].update(median_ms=40.0),
            "an established path missing under loss": lambda r: r["established_lost"].pop(path),
            "no established observations under delay": lambda r: r.pop("established_delayed"),
            "established observations that are not a mapping": lambda r: r.update(established_baseline=[]),
        }
        for name, change in changes.items():
            result = self.passing(profile="tcp-setup-u32")
            change(result)
            with self.subTest(name):
                self.assertTrue(extension_check.selector_problems(result, 120))
        # A profile without setup leaves needs no established connection.
        result = self.passing(profile="tcp-sport-u32")
        for phase in ("established_baseline", "established_delayed", "established_lost"):
            result.pop(phase)
        self.assertEqual(extension_check.selector_problems(result, 120), [])

    def test_source_and_setup_keys_are_the_words_tc_prints(self):
        sport = extension_check.u32_keys(4, "172.19.0.3", 7000, "sport")
        self.assertEqual(sport[-1], {"off": 20, "value": "1b580000", "mask": "ffff0000", "offmask": ""})
        sport6 = extension_check.u32_keys(6, "fd62:93fe:2c9e::3", 7000, "sport")
        self.assertEqual(sport6[-1], {"off": 40, "value": "1b580000", "mask": "ffff0000", "offmask": ""})
        setup = extension_check.u32_keys(4, "172.19.0.3", 7000, "setup")
        self.assertEqual(setup[-2:], [{"off": 20, "value": "00001b58", "mask": "0000ffff", "offmask": ""},
                                      {"off": 32, "value": "00020000", "mask": "00020000", "offmask": ""}])
        setup6 = extension_check.u32_keys(6, "fd62:93fe:2c9e::3", 7000, "setup")
        self.assertEqual(setup6[-1], {"off": 52, "value": "00020000", "mask": "00020000", "offmask": ""})
        with self.assertRaises(ValueError):
            extension_check.layout([(4, "peer-b", 7000, "flags")])

    def test_selectors_require_functional_checks(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            extension_check.arguments(["--extension", "netem", "--image", "helper", "--selector", "ipv4-tcp-u32"])
        args = extension_check.arguments(["--extension", "netem", "--image", "helper", "--functional", "--target", "t",
                                          "--selector", "ipv4-tcp-u32", "--selector", "multi-peer-u32"])
        self.assertEqual(args.selector, ["ipv4-tcp-u32", "multi-peer-u32"])

    def test_classifier_failures_are_classified(self):
        self.assertEqual(extension_check.classify("Error: TC classifier not found.\nWe have an error talking to the kernel"),
                         "kernel_unsupported")
        self.assertEqual(extension_check.classify('Unknown filter "u32", hence option "match" is unparsable'),
                         "image_unsupported")


if __name__ == "__main__":
    unittest.main()
