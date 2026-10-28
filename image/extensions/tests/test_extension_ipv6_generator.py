import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check
import test_extension_capture as capture
from test_extension_capture import FakeCaptureEngine, ext_output, default_ext_output, PORT, TARGET6

HEADER = extension_check.EXT_HEADER.hex()
ADMITTED = json.loads(default_ext_output("net_raw_user"))
REFUSED = json.loads(default_ext_output("capability_free"))


def text(record):
    return json.dumps(record) + "\n"


def observe(record, variant="net_raw_user"):
    return extension_check.ext_observation(variant, text(record) if isinstance(record, dict) else record)


class HeaderTests(unittest.TestCase):
    def test_the_header_is_one_destination_options_header_before_tcp(self):
        header = extension_check.EXT_HEADER
        # Next header TCP and length 0 are the bytes at offsets 40 and 41 of the research filter's packets.
        self.assertEqual((len(header), header[0], header[1]), (8, 6, 0))
        self.assertEqual((header[1] + 1) * 8, len(header))
        # PadN (type 1) with four bytes of zero padding fills the rest.
        self.assertEqual((header[2], header[3], header[4:]), (1, 4, bytes(4)))
        self.assertEqual((extension_check.EXT_OPTION, extension_check.CAP_NET_RAW), (59, 13))


class OracleTests(unittest.TestCase):
    def test_the_capability_free_refusal_is_recorded_as_it_was(self):
        observation = observe(REFUSED, "capability_free")
        self.assertEqual(observation["outcome"], "refused")
        self.assertEqual(observation["set"], {"ok": False, "errno": 1, "name": "EPERM"})
        self.assertIs(observation["net_raw_effective"], False)
        self.assertEqual((observation["readback"], observation["exchange"]), (None, None))

    def test_an_admitted_generator_needs_the_exact_header_and_the_whole_answer(self):
        observation = observe(ADMITTED)
        self.assertEqual((observation["outcome"], observation["net_raw_effective"]), ("admitted", True))
        self.assertEqual(observation["readback"], HEADER)

    def test_other_results_keep_their_own_outcome(self):
        exchange = ADMITTED["exchange"]
        for name, change, outcome in (
                ("another errno", dict(set={"ok": False, "errno": 22, "name": "EINVAL"}, readback=None, exchange=None),
                 "set_failed"),
                ("empty readback", dict(readback=""), "readback_mismatch"),
                ("another header", dict(readback="0600010400000001"), "readback_mismatch"),
                ("longer readback", dict(readback=HEADER + "00"), "readback_mismatch"),
                ("the exchange failed", dict(exchange=dict(exchange, complete=False, received=0,
                                                           error="Connection reset by peer")),
                 "exchange_incomplete"),
                ("a short answer", dict(exchange=dict(exchange, complete=False, received=10)),
                 "exchange_incomplete")):
            with self.subTest(name=name):
                self.assertEqual(observe(dict(ADMITTED, **change))["outcome"], outcome)

    def test_a_set_failure_without_the_capability_but_not_eperm_is_not_a_refusal(self):
        record = dict(REFUSED, set={"ok": False, "errno": 13, "name": "EACCES"})
        self.assertEqual(observe(record, "capability_free")["outcome"], "set_failed")

    def test_unusable_output_stays_inconclusive(self):
        for name, value in (("empty", ""), ("not JSON", "Traceback (most recent call last):\n"), ("null", None),
                            ("bytes", b"{}"), ("a list", "[]\n"), ("a string", '"x"\n'), ("null record", "null\n")):
            with self.subTest(name=name):
                self.assertEqual(extension_check.ext_observation("net_raw_user", value)["outcome"], "inconclusive")

    def test_output_other_than_exactly_one_record_is_inconclusive(self):
        record, other = text(ADMITTED), text(REFUSED)
        for name, value in (("prefixed noise", "unexpected probe output\n" + record), ("prefixed null", "null\n" + record),
                            ("prefixed empty object", "{}\n" + record), ("two identical records", record + record),
                            ("two different records", other + record), ("trailing noise", record + "done\n"),
                            ("trailing null", record + "null\n"), ("two records on one line", record.strip() * 2),
                            ("a warning before it", "Warning: x\n\n" + record)):
            with self.subTest(name=name):
                self.assertEqual(extension_check.ext_observation("net_raw_user", value)["outcome"], "inconclusive")
        # Surrounding whitespace around the one record is fine.
        self.assertEqual(extension_check.ext_observation("net_raw_user", "\n  " + record + "\n\n")["outcome"],
                         "admitted")
        # A conclusive summary needs every variant to be one clean record.
        noisy = {"net_raw_user": "noise\n" + default_ext_output("net_raw_user")}
        observations = {variant: extension_check.ext_observation(variant, noisy.get(variant, default_ext_output(variant)))
                        for variant in extension_check.EXT_VARIANTS}
        self.assertEqual(extension_check.ext_summary(observations), {"capability_free_refused": None, "admitted": None})

    def test_every_missing_null_or_wrong_typed_field_is_inconclusive(self):
        nested = {"caps": ADMITTED["caps"], "set": ADMITTED["set"], "exchange": ADMITTED["exchange"]}
        wrong = (None, True, "x", 1.5, [], {}, -1)
        for key in ADMITTED:
            with self.subTest(missing=key):
                self.assertEqual(observe({k: v for k, v in ADMITTED.items() if k != key})["outcome"], "inconclusive")
            for value in wrong:
                if ADMITTED[key] == value and type(ADMITTED[key]) is type(value):
                    continue
                with self.subTest(key=key, value=value):
                    # A null readback or exchange after a successful set is also unusable.
                    self.assertEqual(observe(dict(ADMITTED, **{key: value}))["outcome"], "inconclusive")
        for key, record in nested.items():
            for inner in record:
                with self.subTest(missing=key + "." + inner):
                    changed = copy.deepcopy(ADMITTED)
                    del changed[key][inner]
                    self.assertEqual(observe(changed)["outcome"], "inconclusive")
                for value in wrong + ("zz", "0" * 15, "0" * 17, "G" * 16):
                    changed = copy.deepcopy(ADMITTED)
                    if changed[key][inner] == value and type(changed[key][inner]) is type(value):
                        continue
                    changed[key][inner] = value
                    with self.subTest(key=key + "." + inner, value=value):
                        # An errno or name of a successful set must be null; a received count is a whole number.
                        self.assertEqual(observe(changed)["outcome"], "inconclusive")
        with self.subTest(extra="key"):
            self.assertEqual(observe(dict(ADMITTED, extra=1))["outcome"], "inconclusive")

    def test_an_unreadable_readback_or_uid_never_decodes(self):
        for name, change in (("odd length hex", dict(readback="060")), ("mixed-case hex", dict(readback="060001040000000A")),
                             ("bool uid", dict(uid=True)), ("float euid", dict(euid=0.0)),
                             ("non-hex capability", dict(caps=dict(ADMITTED["caps"], CapEff="x" * 16))),
                             ("extra capability set", dict(caps=dict(ADMITTED["caps"], CapAmb="0" * 16)))):
            with self.subTest(name=name):
                self.assertEqual(observe(dict(ADMITTED, **change))["outcome"], "inconclusive")

    def test_a_record_that_contradicts_itself_is_inconclusive(self):
        no_cap = "0000000000000000"
        for name, record, variant in (
                ("EPERM although NET_RAW is effective", dict(REFUSED, caps=dict(REFUSED["caps"], CapEff="0000000000002000")),
                 "capability_free"),
                ("set although NET_RAW is not effective", dict(ADMITTED, caps=dict(ADMITTED["caps"], CapEff=no_cap)),
                 "net_raw_user"),
                ("readback after a refused set", dict(REFUSED, readback=HEADER), "capability_free"),
                ("exchange after a refused set", dict(REFUSED, exchange=ADMITTED["exchange"]), "capability_free"),
                ("complete with a short answer", dict(ADMITTED, exchange={"received": 10, "complete": True,
                                                                          "error": None}), "net_raw_user"),
                ("complete with an error", dict(ADMITTED, exchange={"received": 1000, "complete": True,
                                                                    "error": "x"}), "net_raw_user"),
                ("refusal with a zero errno", dict(REFUSED, set={"ok": False, "errno": 0, "name": None}),
                 "capability_free"),
                ("success with an errno", dict(ADMITTED, set={"ok": True, "errno": 1, "name": None}), "net_raw_user")):
            with self.subTest(name=name):
                self.assertEqual(observe(record, variant)["outcome"], "inconclusive")

    def test_the_summary_is_known_only_when_every_variant_is_conclusive(self):
        def summary(**variants):
            outputs = {variant: variants.get(variant, default_ext_output(variant)) for variant in
                       extension_check.EXT_VARIANTS}
            return extension_check.ext_summary({variant: extension_check.ext_observation(variant, output)
                                                for variant, output in outputs.items()})
        self.assertEqual(summary(), {"capability_free_refused": True, "admitted": ["net_raw_root", "net_raw_user"]})
        # An admitted user the kernel refused still has a known refusal for the capability-free sender.
        refused_user = ext_output(uid=1000)
        self.assertEqual(summary(net_raw_user=refused_user),
                         {"capability_free_refused": True, "admitted": ["net_raw_root"]})
        for name, variants in (("garbage capability-free", {"capability_free": "oops"}),
                               ("garbage admitted user", {"net_raw_user": "{}"}),
                               ("capability-free admitted", {"capability_free": default_ext_output("net_raw_user")})):
            with self.subTest(name=name):
                self.assertEqual(summary(**variants), {"capability_free_refused": None, "admitted": None})
        self.assertEqual(extension_check.ext_summary({}), {"capability_free_refused": None, "admitted": None})


class ProbeScriptTests(unittest.TestCase):
    """The script a generator runs, against a scripted socket instead of Linux."""

    def run_script(self, set_error=None, readback=HEADER, answer=None, connect_error=None, caps="0" * 16):
        size = 1000
        token = b"capture-response-%d-" % PORT
        answer = (token * (size // len(token) + 1))[:size] if answer is None else answer
        calls = []

        class Socket:
            def __init__(self, family):
                calls.append(("socket", family))

            def setsockopt(self, level, option, value):
                calls.append(("setsockopt", level, option, value))
                if set_error:
                    raise OSError(set_error, "refused")

            def getsockopt(self, level, option, length):
                calls.append(("getsockopt", level, option, length))
                return bytes.fromhex(readback)

            def settimeout(self, seconds):
                pass

            def connect(self, address):
                calls.append(("connect", address))
                if connect_error:
                    raise OSError(connect_error, "reset")

            def sendall(self, data):
                calls.append(("sendall", data))

            def recv(self, size):
                chunk, self.pending = getattr(self, "pending", answer)[:size], getattr(self, "pending", answer)[size:]
                return chunk

            def close(self):
                pass

        fake = mock.Mock(AF_INET6=10, IPPROTO_IPV6=41, IPV6_DSTOPTS=59, socket=Socket)
        status = "Name:\tpython3\nCapPrm:\t%s\nCapEff:\t%s\nCapBnd:\t0000000000002000\n" % (caps, caps)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"socket": fake}), \
                mock.patch.object(sys, "argv", ["-c", TARGET6, str(PORT), str(size), HEADER]), \
                mock.patch("builtins.open", mock.mock_open(read_data=status)), \
                mock.patch("os.getuid", return_value=1000), mock.patch("os.geteuid", return_value=1000), \
                contextlib.redirect_stdout(out):
            exec(compile(extension_check.EXT_PROBE, "<probe>", "exec"), {})
        return out.getvalue(), calls

    def test_a_refused_set_never_connects(self):
        output, calls = self.run_script(set_error=1)
        self.assertEqual([call[0] for call in calls], ["socket", "setsockopt"])
        self.assertEqual(calls[1][1:], (41, 59, extension_check.EXT_HEADER))
        observation = extension_check.ext_observation("capability_free", output)
        self.assertEqual((observation["outcome"], observation["set"]["name"]), ("refused", "EPERM"))

    def test_an_admitted_set_reads_back_and_completes_the_exchange(self):
        output, calls = self.run_script(caps="0000000000002000")
        self.assertEqual([call[0] for call in calls],
                         ["socket", "setsockopt", "getsockopt", "connect", "sendall"])
        self.assertEqual(calls[3][1], (TARGET6, PORT))
        self.assertEqual(extension_check.ext_observation("net_raw_user", output)["outcome"], "admitted")

    def test_a_wrong_readback_a_short_answer_and_a_failed_connect_are_told_apart(self):
        caps = "0000000000002000"
        for kwargs, outcome in ((dict(readback=""), "readback_mismatch"), (dict(answer=b"short"), "exchange_incomplete"),
                                (dict(connect_error=111), "exchange_incomplete")):
            with self.subTest(kwargs=kwargs):
                output = self.run_script(caps=caps, **kwargs)[0]
                self.assertEqual(extension_check.ext_observation("net_raw_user", output)["outcome"], outcome)


class ScenarioTests(unittest.TestCase):
    setUp = capture.CaptureScenarioTests.setUp
    scenario = capture.CaptureScenarioTests.scenario

    def probe(self, **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        engine = FakeCaptureEngine(self, **options)
        result = self.scenario(engine)
        return engine, result

    def test_the_probe_records_the_refusal_and_the_admitted_generators_without_gating(self):
        engine, result = self.probe()
        self.assertEqual(result["status"], "passed", result.get("error"))
        # The captures and their gate are the ones from before.
        self.assertEqual(set(result["captures"]),
                         {"traffic", "empty", "bound", "abrupt", "target_loss", "ipv6", "restart"})
        probe = result["ipv6_extension_probe"]
        self.assertEqual((probe["status"], probe["gating"], probe["header"], probe["address"], probe["port"]),
                         ("observed", False, HEADER, TARGET6, PORT))
        self.assertEqual({name: item["outcome"] for name, item in probe["variants"].items()},
                         {"capability_free": "refused", "net_raw_user": "admitted", "net_raw_root": "admitted"})
        self.assertEqual(probe["summary"], {"capability_free_refused": True, "admitted": ["net_raw_root", "net_raw_user"]})
        self.assertIn("emission on the wire is not observed", probe["scope"])

    def test_the_generators_are_owned_containers_admitted_with_net_raw_alone(self):
        engine, _ = self.probe()
        runs = {call[call.index("--name") + 1].rsplit("-", 1)[-1]: call for call in engine.calls if call[0] == "run"}
        for role, user in (("netrawuser6", False), ("netrawroot6", True)):
            call = runs[role]
            self.assertEqual([arg for arg in call if arg.startswith("--cap")], ["--cap-drop=ALL", "--cap-add=NET_RAW"])
            self.assertEqual([arg for arg in call if arg.startswith("--user")], ["--user=0:0"] if user else [])
            self.assertNotIn("--privileged", call)
            self.assertEqual(call[call.index("--network") + 1], "ceph-extension-check-abcdef012345-dual")
            self.assertIn("io.ceph-testcontainers.extension-check=abcdef012345", call)
        # The target, the server and the capturer are not given the capability for it.
        for role in ("target6", "server6", "peer6"):
            self.assertFalse(any(arg == "--cap-add=NET_RAW" for arg in runs[role]), role)
        probes = [call for call in engine.calls if call[0] == "exec" and extension_check.EXT_PROBE in call]
        self.assertEqual([call[1].rsplit("-", 1)[-1] for call in probes], ["peer6", "netrawuser6", "netrawroot6"])

    def test_the_probe_runs_before_the_restart_and_changes_no_capture(self):
        engine, result = self.probe()
        positions = [index for index, call in enumerate(engine.calls) if call[0] == "exec" and
                     extension_check.EXT_PROBE in call]
        restart = next(index for index, call in enumerate(engine.calls) if call[0] == "restart")
        self.assertTrue(all(position < restart for position in positions))
        self.assertEqual(result["captures"]["ipv6"]["status"], "passed")

    def test_a_generator_that_cannot_start_or_a_probe_that_fails_is_an_unknown_not_a_failure(self):
        for name, options in (("start fails", dict(generator_run_fails=True)),
                              ("garbage output", dict(generator={"capability_free": "Traceback\n"})),
                              ("null admitted fields", dict(generator={
                                  "net_raw_user": text(dict(ADMITTED, readback=None, exchange=None))}))):
            with self.subTest(name=name):
                engine, result = self.probe(**options)
                self.assertEqual(result["status"], "passed", result.get("error"))
                probe = result["ipv6_extension_probe"]
                self.assertFalse(probe["gating"])
                if name == "start fails":
                    self.assertIn("error", probe)
                    self.assertEqual(probe["status"], "inconclusive")
                self.assertIsNone(probe["summary"]["admitted"] if name != "start fails" else None)

    def test_an_unrefused_capability_free_sender_is_recorded_as_inconclusive_not_as_evidence(self):
        engine, result = self.probe(generator={"capability_free": default_ext_output("net_raw_user")})
        probe = result["ipv6_extension_probe"]
        self.assertEqual(probe["variants"]["capability_free"]["outcome"], "inconclusive")
        self.assertEqual((probe["status"], probe["summary"]["capability_free_refused"]), ("inconclusive", None))
        self.assertEqual(result["status"], "passed")

    def test_the_publication_gate_and_the_coverage_note_are_unchanged(self):
        import publish
        keys = set(extension_check.CAPTURE_STOPS)
        self.assertEqual(keys, {"traffic", "empty", "bound", "abrupt", "target_loss", "ipv6", "restart"})
        docs = (Path(__file__).resolve().parents[3] / "docs" / "EXTENSIONS.md").read_text()
        self.assertIn("What the functional check does not cover: byte limits other than the packet bound, "
                      "and IPv6 with extension headers.", docs)
        self.assertTrue(callable(publish.extension_targets))


if __name__ == "__main__":
    unittest.main()
