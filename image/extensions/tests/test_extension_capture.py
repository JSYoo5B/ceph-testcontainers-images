import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import extension_check

HELPER = {"reference": "ceph-check:extension-capture", "image_id": "sha256:" + "c" * 64, "platform": "linux/amd64"}
PROBE_OUTPUT = "".join("CHECK\tpassed\t%s\n" % name for name in (
    "path:sh", "path:sleep", "path:tcpdump", "path:python3", "sleep-infinity", "version:tcpdump",
    "user:unprivileged", "file-capability:net_raw", "filter:compile", "filter:compile-ip6", "filter:invalid",
    "capture:loopback", "python-runtime", "writable:/tmp")) + \
    "VERSION\ttcpdump\ttcpdump version 4.99.3 libpcap version 1.10.3\nCOMPLETE\tcapture\n"


class CaptureQuickCheckTests(unittest.TestCase):
    """The capture helper's quick check, with Docker replaced by fakes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)

    def test_capture_offers_the_functional_check_but_no_netem_profile(self):
        self.assertEqual(extension_check.arguments(["--extension", "capture", "--image", "x"]).extension, "capture")
        args = extension_check.arguments(["--extension", "capture", "--image", "x", "--functional", "--target", "t"])
        self.assertEqual((args.functional, args.target), (True, ["t"]))
        stderr = io.StringIO()
        for extra in (["--functional"], ["--functional", "--target", "t", "--selector", "ipv4-tcp-u32"],
                      ["--rate"], ["--packet-faults"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
                extension_check.arguments(["--extension", "capture", "--image", "x", *extra])
        self.assertIn("they belong to netem", stderr.getvalue())

    def run_probe(self, extension):
        """The docker create arguments of a probe, which then passes."""
        created = []

        def docker(*command, timeout=120, check_exit=True):
            if command[0] == "create":
                created.append(list(command))
            return 0, ""

        result = mock.Mock(returncode=0, stdout=PROBE_OUTPUT.replace("capture", extension) if extension == "netem"
                           else PROBE_OUTPUT, stderr="")
        image = dict(HELPER)
        args = mock.Mock(probe_timeout=10)
        with mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check.subprocess, "run", return_value=result), \
                mock.patch.object(extension_check, "remove_container", return_value={"status": "passed", "errors": []}):
            extension_check.probe(extension, image, args, self.output)
        return created[0], image

    def test_capture_probe_runs_as_the_image_user_with_net_raw_alone(self):
        command, image = self.run_probe("capture")
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--cap-add=NET_RAW", command)
        self.assertIn("--network=none", command)
        self.assertFalse(any(arg.startswith("--user") for arg in command))
        self.assertEqual(image["status"], "passed")
        self.assertEqual(image["versions"]["tcpdump"], "tcpdump version 4.99.3 libpcap version 1.10.3")
        netem, _ = self.run_probe("netem")
        self.assertIn("--user=0:0", netem)
        self.assertFalse(any(arg.startswith("--cap-add") for arg in netem))

    STARTED = {"ExitCode": 255, "Error": "", "StartedAt": "2026-10-22T22:27:18Z"}

    def refusal(self, code, text, state=None, create_error=None):
        calls = []

        def docker(*command, timeout=120, check_exit=True):
            calls.append(list(command))
            if command[0] == "create":
                if create_error:
                    raise extension_check.CheckError(create_error)
                return 0, ""
            if command[0] == "start":
                return code, text
            return 0, json.dumps(self.STARTED if state is None else state)

        with mock.patch.object(extension_check, "docker", side_effect=docker), \
                mock.patch.object(extension_check, "remove_container", return_value={"status": "passed", "errors": []}):
            result = extension_check.capture_refusal(dict(HELPER), self.output)
        return result, calls[0]

    def test_tcpdump_must_not_start_without_net_raw(self):
        result, command = self.refusal(255, "exec /usr/bin/tcpdump: operation not permitted\n")
        self.assertEqual(result["status"], "passed")
        self.assertIn("--cap-drop=ALL", command)
        self.assertFalse(any(arg.startswith("--cap-add") for arg in command))
        self.assertEqual(command[command.index("--entrypoint=tcpdump") + 2], "--version")

    NEVER_STARTED = {"ExitCode": 0, "Error": "", "StartedAt": "0001-01-01T00:00:00Z"}
    # Docker or the runtime never ran the process: these say nothing about the image.
    SETUP_FAILURES = (
        (125, "permission denied while trying to connect to the Docker daemon socket\n", NEVER_STARTED),
        (126, "OCI runtime create failed: unable to start container process\n",
         dict(NEVER_STARTED, ExitCode=126, Error="OCI runtime create failed")),
        (255, "exec /usr/bin/tcpdump: operation not permitted\n", dict(NEVER_STARTED, ExitCode=255)),
        (1, "Error response from daemon: container is marked for removal\n", STARTED))

    def test_only_the_runtime_exec_refusal_passes(self):
        for code, text, state in (
                (0, "tcpdump version 4.99.3\n", dict(self.STARTED, ExitCode=0)),
                # tcpdump itself ran and failed: its capability was granted.
                (1, "tcpdump: Operation not permitted\n", dict(self.STARTED, ExitCode=1)),
                (255, "tcpdump: lo: You don't have permission to perform this capture\n", None),
                (255, "exec /usr/bin/tcpdump: operation not permitted\nmore\n", None)):
            with self.subTest(code=code, text=text):
                result, _ = self.refusal(code, text, state)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", "quick_failure"))
        result, _ = self.refusal(0, "", create_error="permission denied while trying to connect to the Docker daemon")
        self.assertEqual((result["status"], result["failure_kind"]), ("failed", "checker_failure"))

    def test_docker_and_runtime_setup_failures_are_checker_failures(self):
        for code, text, state in self.SETUP_FAILURES:
            with self.subTest(code=code, text=text):
                result, _ = self.refusal(code, text, state)
                self.assertEqual((result["status"], result["failure_kind"]), ("failed", "checker_failure"))
                self.assertIn("did not run", result["error"])
                image = dict(HELPER, labels={})

                def probe(extension, checked, args, output):
                    checked.update(status="passed", checks=[], cleanup={"status": "passed", "errors": []})

                docker_calls = {"create": (0, ""), "start": (code, text), "container": (0, json.dumps(state))}

                def docker(*command, timeout=120, check_exit=True):
                    if command[0] == "image":
                        return 0, json.dumps([{"Config": {"Labels": {}}}])
                    return docker_calls[command[0]]

                output = self.output / ("main-%d-%d" % (code, len(text)))
                with mock.patch.object(extension_check.check, "inspect_image", return_value=dict(image)), \
                        mock.patch.object(extension_check, "docker", side_effect=docker), \
                        mock.patch.object(extension_check, "probe", side_effect=probe), \
                        mock.patch.object(extension_check, "remove_container",
                                          return_value={"status": "passed", "errors": []}), \
                        mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    self.assertEqual(extension_check.main(["--extension", "capture", "--image", "x", "--output-dir",
                                                           str(output)]), 1)
                report = json.loads((output / "extension-report.json").read_text())
                self.assertEqual((report["status"], report["failure_kind"]), ("failed", "checker_failure"))

    def test_a_tcpdump_that_starts_without_net_raw_fails_the_image(self):
        image = dict(HELPER, labels={})

        def probe(extension, checked, args, output):
            checked.update(status="passed", checks=[{"name": "path:sh", "status": "passed"}],
                           cleanup={"status": "passed", "errors": []})

        refusal = {"name": "refused-without-net_raw", "status": "failed", "failure_kind": "quick_failure",
                   "cleanup": {"status": "passed", "errors": []}}
        with mock.patch.object(extension_check.check, "inspect_image", return_value=dict(image)), \
                mock.patch.object(extension_check, "docker", return_value=(0, json.dumps([{"Config": {"Labels": {}}}]))), \
                mock.patch.object(extension_check, "probe", side_effect=probe), \
                mock.patch.object(extension_check, "capture_refusal", return_value=refusal), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = extension_check.main(["--extension", "capture", "--image", "x", "--output-dir",
                                         str(self.output / "quick")])
        report = json.loads((self.output / "quick" / "extension-report.json").read_text())
        self.assertEqual((code, report["status"], report["failure_kind"]), (1, "failed", "image_unsupported"))
        self.assertEqual(report["contract"], "1")
        self.assertEqual(report["image"]["checks"][-1]["name"], "refused-without-net_raw")


PROBE = Path(extension_check.EXTENSIONS["capture"]["probe"])


def udp_frame(port, payload):
    """An Ethernet/IPv4/UDP frame to 127.0.0.1:port."""
    udp = struct.pack(">HHHH", 40000, port, 8 + len(payload), 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, bytes([127, 0, 0, 1]),
                     bytes([127, 0, 0, 1]))
    return b"\x00" * 12 + b"\x08\x00" + ip + udp


def pcap(*frames, snaplen=128, link=1):
    data = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, snaplen, link)
    for frame in frames:
        data += struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame
    return data


class CaptureOracleTests(unittest.TestCase):
    """The loopback oracle embedded in check-capture.sh, run on synthetic captures."""

    def oracle(self, data, status=0, stderr=None):
        script = PROBE.read_text()
        code = script[script.index("<<'ORACLE'\n") + len("<<'ORACLE'\n"):script.index("\nORACLE\n")]
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "lo.pcap").write_bytes(data)
            Path(directory, "stderr").write_text(stderr if stderr is not None else (
                "tcpdump: listening on lo, link-type EN10MB (Ethernet), snapshot length 128 bytes\n"
                "1 packet captured\n2 packets received by filter\n0 packets dropped by kernel\n"))
            return subprocess.run([sys.executable, "-c", code, directory, str(status)], capture_output=True).returncode

    def test_only_the_selected_datagram_passes(self):
        selected, excluded = udp_frame(7000, b"capture-check-7000"), udp_frame(7001, b"capture-check-7001")
        self.assertEqual(self.oracle(pcap(selected)), 0)
        for name, data in (("excluded only", pcap(excluded)), ("both", pcap(selected, excluded)),
                           ("selected port, other payload", pcap(udp_frame(7000, b"capture-check-7001"))),
                           ("empty", pcap()), ("cut record", pcap(selected)[:-3]),
                           ("other snaplen", pcap(selected, snaplen=262144)), ("other link type", pcap(selected, link=113)),
                           ("no header", b"")):
            with self.subTest(name=name):
                self.assertNotEqual(self.oracle(data), 0)
        self.assertNotEqual(self.oracle(pcap(selected), status=1), 0)
        self.assertNotEqual(self.oracle(pcap(selected), stderr="0 packets captured\n"), 0)

    def test_the_probe_uses_only_the_contract_executables(self):
        # sh builtins plus sh, sleep, tcpdump and python3; no grep, tr, rm and the like.
        script = PROBE.read_text()
        shell = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
        for heredoc in ("READY", "ORACLE"):
            start = shell.index("<<'%s'\n" % heredoc)
            shell = shell[:start] + shell[shell.index("\n%s\n" % heredoc, start) + len(heredoc) + 2:]
        for tool in ("grep", "tr", "rm", "cat", "sed", "awk", "head", "tail", "seq", "id", "mkdir", "ls", "cut",
                     "mktemp", "getcap", "timeout"):
            with self.subTest(tool=tool):
                self.assertIsNone(re.search(r"(^|[;&|(`]|\$\(|\bthen|\bdo|\belse)\s*%s\b" % tool, shell, re.M),
                                  tool + " is not a contract executable")


PEER, TARGET = "172.30.0.3", "172.30.0.2"
SNAPLEN = extension_check.CAPTURE_SNAPLEN
READY_LINE = "tcpdump: listening on eth0, link-type EN10MB (Ethernet), snapshot length %d bytes\n" % SNAPLEN


def tcp_frame(source, destination, sport, dport, flags, payload=b""):
    """An Ethernet/IPv4/TCP frame without options."""
    tcp = struct.pack(">HHIIBBHHH", sport, dport, 1, 1, 5 << 4, flags, 65535, 0, 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0,
                     extension_check.ipaddress.IPv4Address(source).packed,
                     extension_check.ipaddress.IPv4Address(destination).packed)
    return b"\x00" * 12 + b"\x08\x00" + ip + tcp


def tcp6_frame(source, destination, sport, dport, flags, payload=b"", next_header=6):
    """An Ethernet/IPv6/TCP frame without options or extension headers."""
    tcp = struct.pack(">HHIIBBHHH", sport, dport, 1, 1, 5 << 4, flags, 65535, 0, 0) + payload
    ip = struct.pack(">IHBB16s16s", 6 << 28, len(tcp), next_header, 64,
                     extension_check.ipaddress.IPv6Address(source).packed,
                     extension_check.ipaddress.IPv6Address(destination).packed)
    return b"\x00" * 12 + b"\x86\xdd" + ip + tcp


def capture_file(*frames, snaplen=SNAPLEN):
    """A classic pcap that cuts every frame at the snaplen, as tcpdump does."""
    data = struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, snaplen, 1)
    for frame in frames:
        included = frame[:snaplen]
        data += struct.pack("<IIII", 1, 0, len(included), len(frame)) + included
    return data


PORT, EXCLUDED = extension_check.CAPTURE_PORT, extension_check.CAPTURE_EXCLUDED_PORT
RESPONSE = (extension_check.capture_token(PORT) * 60)[:extension_check.CAPTURE_RESPONSE]


def flow(peer, target, peer_port=40000, frame=tcp_frame):
    """One whole connection from the peer's port to the target's selected port."""
    return (frame(peer, target, peer_port, PORT, 0x02), frame(target, peer, PORT, peer_port, 0x12),
            frame(peer, target, peer_port, PORT, 0x10),
            frame(peer, target, peer_port, PORT, 0x18, extension_check.capture_request(PORT)),
            frame(target, peer, PORT, peer_port, 0x18, RESPONSE), frame(peer, target, peer_port, PORT, 0x11),
            frame(target, peer, PORT, peer_port, 0x11))


FLOW = flow(PEER, TARGET)
SYN, SYN_ACK, ACK, REQUEST, ANSWER = FLOW[:5]
OTHER = tcp_frame(PEER, TARGET, 40001, EXCLUDED, 0x02)
# The dual-stack bridge: its IPv6 flow, and the restart capture's connections
# from before and after the restart, over IPv4.
PEER6, TARGET6 = "fd00:c0de::3", "fd00:c0de::2"
PEER4, TARGET4 = "172.31.0.3", "172.31.0.2"
FLOW6 = flow(PEER6, TARGET6, frame=tcp6_frame)
OTHER6 = tcp6_frame(PEER6, TARGET6, 40001, EXCLUDED, 0x02)
BEFORE, AFTER = extension_check.CAPTURE_RESTART_PORTS
RESTART_FLOW = flow(PEER4, TARGET4, BEFORE)


def statistics(captured, received=None):
    return "%d packet%s captured\n%d packets received by filter\n0 packets dropped by kernel\n" % (
        captured, "" if captured == 1 else "s", captured if received is None else received)


def finished(captured, status=0, stderr=None):
    return {"status": status, "stderr": READY_LINE + (statistics(captured) if stderr is None else stderr),
            "pid": 42, "size": 24}


class CaptureEvidenceTests(unittest.TestCase):
    """The functional oracle on synthetic captures: each control below fails
    the unchanged evidence that a real capture must provide."""

    def problems(self, kind, data, state=None, packets=extension_check.CAPTURE_PACKETS):
        records = len(extension_check.parse_pcap(data, SNAPLEN)[0])
        return extension_check.capture_problems(kind, state or finished(records), data, PEER, TARGET, SNAPLEN,
                                                packets)

    def test_the_selected_flow_in_both_directions_passes(self):
        summary, problems = self.problems("traffic", capture_file(*FLOW))
        self.assertEqual(problems, [])
        self.assertEqual((summary["records"], summary["to_target"], summary["from_target"], summary["truncated"]),
                         (7, 4, 3, 1))
        self.assertEqual(summary["seen"], ["request", "response", "syn", "syn_ack"])
        self.assertIsNone(summary["statistics"]["dropped_by_interface"])

    def test_traffic_controls_fail(self):
        no_answer = [frame for frame in FLOW if frame[34:36] != struct.pack(">H", PORT)]
        for name, data in (
                ("excluded flow kept", capture_file(*FLOW, OTHER)),
                ("other peer kept", capture_file(*FLOW, tcp_frame("172.30.0.9", TARGET, 40002, PORT, 0x02))),
                ("not TCP", capture_file(*FLOW, b"\xff" * 12 + b"\x08\x06" + b"\x00" * 28)),
                ("one direction", capture_file(*no_answer)),
                ("no SYN-ACK", capture_file(*(frame for frame in FLOW if frame != SYN_ACK))),
                ("no request payload", capture_file(*(frame for frame in FLOW if frame != REQUEST))),
                ("nothing cut at the snaplen", capture_file(*(frame for frame in FLOW if frame != ANSWER),
                                                            tcp_frame(TARGET, PEER, PORT, 40000, 0x18,
                                                                      RESPONSE[:40]))),
                ("other header snaplen", capture_file(*FLOW, snaplen=SNAPLEN * 2)),
                ("cut record", capture_file(*FLOW)[:-5]),
                ("pcapng", b"\x0a\x0d\x0d\x0a" + capture_file(*FLOW)[4:]),
                ("empty", capture_file())):
            with self.subTest(name=name):
                self.assertNotEqual(self.problems("traffic", data)[1], [])

    def test_stop_and_statistics_controls_fail(self):
        data = capture_file(*FLOW)
        for name, state in (("killed", finished(7, status=143)), ("no statistics", finished(7, stderr="")),
                            ("count differs", finished(6)),
                            ("no kernel drops", finished(7, stderr="7 packets captured\n7 packets received by filter\n"))):
            with self.subTest(name=name):
                self.assertNotEqual(self.problems("traffic", data, state)[1], [])

    def test_an_empty_capture_is_a_header_alone(self):
        self.assertEqual(self.problems("empty", capture_file())[1], [])
        for name, data in (("a record", capture_file(OTHER)), ("no file content", b""),
                           ("other link type", capture_file()[:20] + struct.pack("<I", 113))):
            with self.subTest(name=name):
                self.assertNotEqual(self.problems("empty", data)[1], [])

    def test_the_packet_bound_holds_exactly_its_packets(self):
        bound = extension_check.CAPTURE_BOUND
        self.assertEqual(self.problems("bound", capture_file(*FLOW[:bound]), packets=bound)[1], [])
        self.assertNotEqual(self.problems("bound", capture_file(*FLOW[:bound + 1]), packets=bound)[1], [])
        self.assertNotEqual(self.problems("bound", capture_file(*FLOW[:bound - 1]), packets=bound)[1], [])

    def test_readiness_needs_the_requested_interface_link_type_and_snaplen(self):
        self.assertTrue(extension_check.capture_ready(READY_LINE, "eth0", SNAPLEN))
        for line in (READY_LINE.replace("eth0", "lo"), READY_LINE.replace("EN10MB (Ethernet)", "LINUX_SLL2 (Linux)"),
                     READY_LINE.replace(str(SNAPLEN), "262144"), "tcpdump: verbose output suppressed\n", ""):
            with self.subTest(line=line):
                self.assertFalse(extension_check.capture_ready(line, "eth0", SNAPLEN))

    def test_missing_statistics_are_unknown_not_zero(self):
        self.assertEqual(extension_check.capture_statistics("1 packet captured\n3 packets received by filter\n"),
                         {"captured": 1, "received_by_filter": 3, "dropped_by_kernel": None,
                          "dropped_by_interface": None})

    def test_capture_failures_are_classified(self):
        for code, text, kind in (
                (1, "tcpdump: can't parse filter expression: syntax error\n", "invalid_filter"),
                (1, "tcpdump: ctc-missing0: No such device exists\n(SIOCGIFHWADDR: No such device)\n",
                 "unavailable_interface"),
                (1, "tcpdump: eth0: You don't have permission to perform this capture on that device\n",
                 "permission_denied"),
                (255, "exec /usr/bin/tcpdump: operation not permitted\n", "permission_denied"),
                (127, "tcpdump: error while loading shared libraries: libpcap.so.0.8\n", "loading_failure"),
                (1, "tcpdump: pcap_loop: The interface disappeared\n", "capture_failure")):
            with self.subTest(text=text):
                self.assertEqual(extension_check.classify_capture(code, text), kind)

    def ipv6_problems(self, data, peer=PEER6, target=TARGET6):
        records = len(extension_check.parse_pcap(data, SNAPLEN)[0])
        return extension_check.capture_problems("ipv6", finished(records), data, peer, target, SNAPLEN,
                                                extension_check.CAPTURE_PACKETS)

    def test_the_ipv6_flow_passes_without_its_ipv4_counterpart(self):
        summary, problems = self.ipv6_problems(capture_file(*FLOW6))
        self.assertEqual(problems, [])
        self.assertEqual((summary["records"], summary["to_target"], summary["from_target"], summary["truncated"],
                          summary["ipv4_records"]), (7, 4, 3, 1, 0))
        self.assertEqual(summary["seen"], ["request", "response", "syn", "syn_ack"])
        # The engine may report an address in another text form.
        self.assertEqual(self.ipv6_problems(capture_file(*FLOW6), peer="fd00:c0de:0:0:0:0:0:3")[1], [])

    def test_ipv6_controls_fail(self):
        for name, data in (
                ("IPv4 flow kept", capture_file(*FLOW6, *flow(PEER4, TARGET4))),
                ("one IPv4 record", capture_file(*FLOW6, flow(PEER4, TARGET4)[0])),
                ("excluded port kept", capture_file(*FLOW6, OTHER6)),
                ("other IPv6 peer", capture_file(*FLOW6, tcp6_frame("fd00:c0de::9", TARGET6, 40002, PORT, 0x02))),
                ("extension header", capture_file(*FLOW6, tcp6_frame(PEER6, TARGET6, 40000, PORT, 0x10,
                                                                     next_header=0))),
                ("no SYN-ACK", capture_file(*(frame for frame in FLOW6 if frame != FLOW6[1]))),
                ("no request payload", capture_file(*(frame for frame in FLOW6 if frame != FLOW6[3]))),
                ("only IPv4", capture_file(*flow(PEER4, TARGET4))),
                ("empty", capture_file())):
            with self.subTest(name=name):
                self.assertNotEqual(self.ipv6_problems(data)[1], [])
        # The IPv4 oracle does not take the IPv6 flow either.
        self.assertNotEqual(self.problems("traffic", capture_file(*FLOW6))[1], [])


# A final record whose header promises 60 bytes, of which 10 were written.
CUT_RECORD = struct.pack("<IIII", 1, 0, 60, 60) + b"\x00" * 10


class PartialCaptureTests(unittest.TestCase):
    """Partial evidence: tcpdump killed with SIGKILL, or the target lost while it ran."""

    def problems(self, kind, data, status, stderr=READY_LINE, loss=None):
        state = {"status": status, "stderr": stderr, "pid": 42, "size": len(data)}
        return extension_check.partial_problems(kind, state, data, PEER, TARGET, SNAPLEN,
                                                extension_check.CAPTURE_PACKETS, loss)

    def test_a_killed_capture_keeps_its_complete_records(self):
        summary, problems = self.problems("abrupt", capture_file(*FLOW), 137)
        self.assertEqual(problems, [])
        self.assertEqual((summary["records"], summary["cut_tail"], summary["complete"]), (len(FLOW), False, "partial"))
        self.assertEqual(set(summary["statistics"].values()), {None}, "unprinted statistics stay unknown")
        summary, problems = self.problems("abrupt", capture_file(*FLOW) + CUT_RECORD, 137)
        self.assertEqual((problems, summary["cut_tail"]), ([], True))

    def test_a_cut_tail_never_hides_an_invalid_record_header(self):
        for name, microseconds, included, original in (("caplen over snaplen", 0, SNAPLEN + 1, SNAPLEN + 1),
                                                       ("caplen over original", 0, 60, 59),
                                                       ("microseconds out of range", 1000000, 60, 60)):
            with self.subTest(name=name):
                cut = struct.pack("<IIII", 1, microseconds, included, original) + b"\x00" * 10
                summary, problems = self.problems("abrupt", capture_file(*FLOW) + cut, 137)
                self.assertTrue(summary["cut_tail"])
                self.assertNotEqual(problems, [])

    def test_abrupt_controls_fail(self):
        for name, data, status, stderr in (
                ("status 0", capture_file(*FLOW), 0, READY_LINE),
                ("statistics after SIGKILL", capture_file(*FLOW), 137, READY_LINE + statistics(len(FLOW))),
                ("no record", capture_file(), 137, READY_LINE),
                ("excluded flow kept", capture_file(*FLOW, OTHER), 137, READY_LINE),
                ("bad header", b"\x0a\x0d\x0d\x0a" + capture_file(*FLOW)[4:], 137, READY_LINE)):
            with self.subTest(name=name):
                self.assertNotEqual(self.problems("abrupt", data, status, stderr)[1], [])

    def test_a_lost_target_ends_the_evidence(self):
        lost = {"end": "exited", "interfaces": []}
        summary, problems = self.problems("target_loss", capture_file(*FLOW), 1,
                                          READY_LINE + "tcpdump: pcap_loop: The interface disappeared\n", lost)
        self.assertEqual(problems, [])
        self.assertEqual(summary["loss"], lost)
        running = {"end": "kept running", "interfaces": []}
        self.assertEqual(self.problems("target_loss", capture_file(*FLOW), 0, READY_LINE + statistics(len(FLOW)),
                                       running)[1], [])
        for name, status, loss in (("interface still there", 1, {"end": "exited", "interfaces": [{"name": "eth0"}]}),
                                   ("clean exit after the loss", 0, lost)):
            with self.subTest(name=name):
                self.assertNotEqual(self.problems("target_loss", capture_file(*FLOW), status, READY_LINE, loss)[1], [])

    def restart_problems(self, data, status=1, stderr=READY_LINE + "tcpdump: pcap_loop: The interface disappeared\n",
                         **restart):
        restart = dict({"end": "exited", "interfaces": [], "followed": False, "before_port": BEFORE,
                        "after_port": AFTER, "new_address": TARGET4, "status_after_restart": 1}, **restart)
        state = {"status": status, "stderr": stderr, "pid": 42, "size": len(data)}
        return extension_check.partial_problems("restart", state, data, PEER4, TARGET4, SNAPLEN,
                                                extension_check.CAPTURE_PACKETS, restart)

    def test_a_restart_keeps_only_the_connection_from_before(self):
        summary, problems = self.restart_problems(capture_file(*RESTART_FLOW))
        self.assertEqual(problems, [])
        self.assertEqual((summary["records"], summary["complete"], summary["restart"]["end"]),
                         (len(RESTART_FLOW), "partial", "exited"))
        self.assertNotIn("loss", summary)
        # A tcpdump that kept running, stopped with SIGTERM afterwards.
        self.assertEqual(self.restart_problems(capture_file(*RESTART_FLOW), 0,
                                               READY_LINE + statistics(len(RESTART_FLOW)), end="kept running")[1], [])
        # A cut final record of the connection before the restart is partial output.
        summary, problems = self.restart_problems(capture_file(*RESTART_FLOW) + CUT_RECORD)
        self.assertEqual((problems, summary["cut_tail"]), ([], True))

    def test_any_record_from_after_the_restart_fails(self):
        moved = "172.31.0.9"
        for name, data, restart in (
                ("the connection after the restart", capture_file(*RESTART_FLOW, *flow(PEER4, TARGET4, AFTER)), {}),
                ("one record after the restart", capture_file(*RESTART_FLOW, flow(PEER4, TARGET4, AFTER)[1]), {}),
                ("only the connection after the restart", capture_file(*flow(PEER4, TARGET4, AFTER)), {}),
                ("after the restart at a new address", capture_file(*RESTART_FLOW, *flow(PEER4, moved, AFTER)),
                 {"new_address": moved}),
                ("another connection", capture_file(*RESTART_FLOW, *flow(PEER4, TARGET4, 40000)), {}),
                ("no record", capture_file(), {}),
                ("the helper followed the target", capture_file(*RESTART_FLOW), {"followed": True}),
                ("an interface in the helper's namespace", capture_file(*RESTART_FLOW),
                 {"interfaces": [{"name": "eth0", "index": 5}]})):
            with self.subTest(name=name):
                self.assertNotEqual(self.restart_problems(data, **restart)[1], [])
        problems = self.restart_problems(capture_file(*RESTART_FLOW, flow(PEER4, TARGET4, AFTER)[1]))[1]
        self.assertEqual(problems, ["record 7 is from after the restart: peer port %d" % AFTER])
        # A clean exit once the interface is gone fails as after a loss.
        self.assertNotEqual(self.restart_problems(capture_file(*RESTART_FLOW), 0, READY_LINE)[1], [])


def ext_output(caps="0000000000000000", uid=1000, set_error=(1, "EPERM"), readback=None, exchange=None):
    """One generator's output as the probe prints it: the set refused with
    set_error, or (set_error None) the readback and exchange given."""
    option = {"ok": True, "errno": None, "name": None} if set_error is None else \
        {"ok": False, "errno": set_error[0], "name": set_error[1]}
    return json.dumps({"uid": uid, "euid": uid, "caps": {"CapEff": caps, "CapPrm": caps, "CapBnd": "0000000000002000"},
                       "set": option, "readback": readback, "exchange": exchange}) + "\n"


def default_ext_output(variant):
    """What Linux does: refuse without an effective CAP_NET_RAW, admit with it."""
    if variant == "capability_free":
        return ext_output()
    return ext_output("0000000000002000", 0 if variant == "net_raw_root" else 1000, None,
                      extension_check.EXT_HEADER.hex(), {"received": 1000, "complete": True, "error": None})


class FakeCaptureEngine:
    """A Docker stand-in for one capture scenario; each option breaks one step."""

    def __init__(self, test, uid=1000, early_exit=None, invalid_filter_refused=True, net_raw_refused=True,
                 keeps_excluded=False, unprivileged=None, clock=None, ready_delay=0, exit_delay=0, block=0,
                 killed_status=137, killed_statistics=False, cut_tail=False, loss_end="exited", loss_status=1,
                 interface_survives=False, keeps_ipv4=False, no_ipv6=False, restart_end="exited", restart_status=1,
                 restart_leaks=False, follows=False, interface_survives_restart=False, same_sandbox=False,
                 renewed_address=TARGET4, generator=None, generator_run_fails=False):
        self.test, self.uid, self.early_exit = test, uid, early_exit
        # The generator-admission probe's output by variant (None: the
        # kernel's behavior, see ext_output), and whether starting an
        # admitted generator fails.
        self.generator, self.generator_run_fails = generator or {}, generator_run_fails
        self.invalid_filter_refused, self.net_raw_refused = invalid_filter_refused, net_raw_refused
        self.keeps_excluded = keeps_excluded
        # (exit code, output, state) of the control without NET_RAW.
        self.unprivileged = unprivileged
        # A synthetic clock: a readiness or exit read takes its delay, and a
        # state read that blocks for block seconds times out at its bound.
        self.clock, self.ready_delay, self.exit_delay, self.block = clock, ready_delay, exit_delay, block
        self.captures, self.calls, self.state_timeouts, self.exchanges = {}, [], [], []
        # How an abrupt end and a lost target look: the killed tcpdump's
        # status and statistics, a cut final record, and whether tcpdump
        # exits or keeps running once the target's interface is gone.
        self.killed_status, self.killed_statistics, self.cut_tail = killed_status, killed_statistics, cut_tail
        self.loss_end, self.loss_status, self.interface_survives = loss_end, loss_status, interface_survives
        self.target_killed = False
        # The dual-stack bridge: an IPv6 capture that keeps the IPv4 flow, a
        # network without IPv6 addresses, and how the target's restart looks:
        # tcpdump exits or keeps running, its file holds records from after
        # the restart, the helper joins the new namespace, an interface stays
        # in the helper's namespace, or the sandbox does not change.
        self.keeps_ipv4, self.no_ipv6 = keeps_ipv4, no_ipv6
        self.restart_end, self.restart_status, self.restart_leaks = restart_end, restart_status, restart_leaks
        self.follows, self.interface_survives_restart = follows, interface_survives_restart
        self.same_sandbox, self.renewed_address = same_sandbox, renewed_address
        self.target_restarted = False

    def __call__(self, *command, timeout=120, check_exit=True):
        self.calls.append(list(command))
        name = next((arg for arg in command if isinstance(arg, str) and arg.startswith("ceph-extension-check-")
                     and arg.count("-") >= 4), "")
        role = name.rsplit("-", 1)[-1]
        if command[:2] == ("container", "inspect") and "--format" not in command:
            network = name.rsplit("-", 1)[0]
            if role.endswith("6"):
                network += "-dual"
                address = {"target6": self.renewed_address if self.target_restarted else TARGET4,
                           "peer6": PEER4}[role]
                address6 = "" if self.no_ipv6 else {"target6": TARGET6, "peer6": PEER6}[role]
            else:
                address, address6 = {"target": TARGET, "peer": PEER}[role], ""
            sandbox = "sandbox-%s-%d" % (role, self.target_restarted and not self.same_sandbox)
            return 0, json.dumps([{"State": {"Running": True}, "NetworkSettings": {"SandboxID": sandbox, "Networks": {
                network: {"IPAddress": address, "GlobalIPv6Address": address6,
                          "MacAddress": "02:42:ac:1e:00:02"}}}}])
        if command[:2] == ("container", "inspect"):
            if self.unprivileged:
                return 0, json.dumps(self.unprivileged[2])
            return 0, json.dumps({"ExitCode": 255 if self.net_raw_refused else 1, "Error": "",
                                  "StartedAt": "2026-10-23T00:00:00Z"})
        if command[0] == "start":
            if self.unprivileged:
                return self.unprivileged[:2]
            if self.net_raw_refused:
                return 255, "exec /usr/bin/tcpdump: operation not permitted\n"
            return 1, "tcpdump: can't parse filter expression\n"
        if command[0] == "kill":
            self.test.assertTrue(command[1].endswith("-target"))
            self.target_killed = True
            return 0, ""
        if command[0] == "restart":
            self.test.assertEqual(command[1:3], ("-t", "0"))
            self.test.assertTrue(command[3].endswith("-target6"))
            self.target_restarted = True
            return 0, ""
        if command[0] == "cp":
            base = command[1].split(":", 1)[1][:-len(".pcap")]
            Path(command[2]).write_bytes(self.captures[base]["data"])
            return 0, ""
        if command[0] == "run" and role.startswith("netraw") and self.generator_run_fails:
            raise extension_check.CheckError("Command failed: docker run: cannot start")
        if command[0] != "exec":
            return 0, ""
        if command[1] == "--detach":
            base = command[6]
            self.test.assertTrue(command[2].endswith(("-capturer", "-capturer6")))
            self.test.assertEqual(command[3:6], ("sh", "-c", extension_check.CAPTURE_WRAPPER))
            self.captures[base] = {"args": command[7:], "stopped": False, "killed": False, "exchanged": False}
            self.latest = base
            return 0, ""
        script = command[command.index("-c") + 1] if "-c" in command and command[2] == "python3" else None
        if script == extension_check.CAPTURE_UID:
            return 0, json.dumps([self.uid, self.uid])
        if script == extension_check.NAMESPACE:
            namespace = {"peer": 2, "server6": 4, "capturer6": 4, "peer6": 5,
                         "renewed6": 4 if self.follows else 6}.get(role, 1)
            return 0, "net:[%d]\n" % namespace
        if script == extension_check.CAPTURE_INTERFACE:
            gone = (self.target_restarted and not self.interface_survives_restart) if role == "capturer6" else (
                self.target_killed and not self.interface_survives)
            return 0, "[]" if gone else json.dumps([{"name": "eth0", "index": 5}])
        if script == extension_check.READY:
            return 0, ""
        if script == extension_check.EXT_PROBE:
            variant = {"peer6": "capability_free", "netrawuser6": "net_raw_user", "netrawroot6": "net_raw_root"}[role]
            self.test.assertEqual(command[5:], (TARGET6, str(PORT), "1000", extension_check.EXT_HEADER.hex()))
            return 0, self.generator.get(variant, default_ext_output(variant))
        if script == extension_check.CAPTURE_EXCHANGE:
            self.captures[self.latest]["exchanged"] = True
            self.exchanges.append((self.latest, command[5], int(command[7]), self.target_restarted))
            return 0, json.dumps([{"address": command[5], "port": int(port), "source_port": int(command[7]) or 40000,
                                   "received": 1000} for port in command[8:]])
        if script == extension_check.CAPTURE_SIGNAL:
            self.test.assertIn(command[-1], ("SIGTERM", "SIGKILL"))
            self.captures[self.latest]["killed" if command[-1] == "SIGKILL" else "stopped"] = True
            return 0, ""
        if script == extension_check.CAPTURE_STATE:
            self.state_timeouts.append(timeout)
            if self.block:
                if timeout < self.block:
                    self.clock.now += timeout
                    raise extension_check.CheckError("Docker command timed out", "timeout")
                self.clock.now += self.block
            state = self.state(command[-1])
            if self.clock:
                self.clock.now += self.exit_delay if state["status"] is not None else (
                    self.ready_delay if not self.captures[command[-1]]["exchanged"] else 0)
            return 0, json.dumps(state)
        if command[2] == "tcpdump" and "-d" in command:
            return 0, "(000) ret #0\n"
        if command[2] == "tcpdump":
            if extension_check.CAPTURE_INVALID_FILTER in command:
                return (1, "tcpdump: can't parse filter expression: syntax error\n") if self.invalid_filter_refused \
                    else (0, READY_LINE + statistics(1))
            return 1, "tcpdump: ctc-missing0: No such device exists\n(SIOCGIFHWADDR: No such device)\n"
        raise AssertionError("unexpected docker call: %r" % (command,))

    def state(self, base):
        capture = self.captures[base]
        kind = base.rsplit("-", 1)[-1]
        if self.early_exit:
            return {"stderr": self.early_exit, "pid": 42, "status": 1, "size": None}
        if kind == "bound":
            frames = FLOW[:extension_check.CAPTURE_BOUND]
        elif kind == "traffic":
            frames = FLOW + ((OTHER,) if self.keeps_excluded else ())
        elif kind == "ipv6":
            frames = FLOW6 + (flow(PEER4, TARGET4) if self.keeps_ipv4 else ())
        else:
            frames = ()
        if kind == "restart" and self.target_restarted:
            capture["data"] = capture_file(*RESTART_FLOW, *(flow(PEER4, self.renewed_address, AFTER)
                                                            if self.restart_leaks else ()))
            if self.restart_end == "exited":
                return {"stderr": READY_LINE + "tcpdump: pcap_loop: The interface disappeared\n", "pid": 42,
                        "status": self.restart_status, "size": len(capture["data"])}
            if capture["stopped"]:
                return {"stderr": READY_LINE + statistics(len(RESTART_FLOW)), "pid": 42, "status": 0,
                        "size": len(capture["data"])}
            return {"stderr": READY_LINE, "pid": 42, "status": None, "size": len(capture["data"])}
        if kind == "abrupt" and capture["killed"]:
            capture["data"] = capture_file(*FLOW) + (CUT_RECORD if self.cut_tail else b"")
            return {"stderr": READY_LINE + (statistics(len(FLOW)) if self.killed_statistics else ""), "pid": 42,
                    "status": self.killed_status, "size": len(capture["data"])}
        if kind == "target_loss" and self.target_killed:
            capture["data"] = capture_file(*FLOW)
            if self.loss_end == "exited":
                return {"stderr": READY_LINE + "tcpdump: pcap_loop: The interface disappeared\n", "pid": 42,
                        "status": self.loss_status, "size": len(capture["data"])}
            if capture["stopped"]:
                return {"stderr": READY_LINE + statistics(len(FLOW)), "pid": 42, "status": 0,
                        "size": len(capture["data"])}
            return {"stderr": READY_LINE, "pid": 42, "status": None, "size": len(capture["data"])}
        if not (capture["stopped"] or kind == "bound" and capture["exchanged"]):
            return {"stderr": READY_LINE, "pid": 42, "status": None, "size": 24}
        capture["data"] = capture_file(*frames)
        return {"stderr": READY_LINE + statistics(len(frames)), "pid": 42, "status": 0,
                "size": len(capture["data"])}


def outcome(result):
    return result["status"], result.get("failure_stage"), result.get("failure_kind")


class Clock:
    """time.monotonic and time.sleep on a synthetic timeline."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class CaptureScenarioTests(unittest.TestCase):
    """The capture scenario's steps and failure kinds, with Docker replaced by a fake engine."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)

    def scenario(self, engine, clock=None):
        target = {"reference": "quay.io/ceph/ceph:v20.2.4", "image_id": "sha256:" + "d" * 64,
                  "platform": "linux/amd64"}
        cleanup = {"status": "passed", "leftovers": [], "errors": []}
        timing = (mock.patch.object(extension_check.time, "monotonic", side_effect=clock.monotonic),
                  mock.patch.object(extension_check.time, "sleep", side_effect=clock.sleep)) if clock else \
            (mock.patch.multiple(extension_check, CAPTURE_READY_TIMEOUT=1, CAPTURE_STOP_TIMEOUT=1),
             mock.patch.object(extension_check.time, "sleep"))
        with mock.patch.object(extension_check, "docker", side_effect=engine), \
                mock.patch.object(extension_check, "cleanup", return_value=cleanup) as removed, \
                timing[0], timing[1]:
            result = extension_check.capture_scenario(dict(HELPER), target, "abcdef012345", self.output / "run")
        removed.assert_called_once()
        return result

    def test_a_scenario_that_meets_the_contract_passes(self):
        engine = FakeCaptureEngine(self)
        result = self.scenario(engine)
        self.assertEqual(result["status"], "passed", result.get("error"))
        self.assertEqual(set(result["captures"]),
                         {"traffic", "empty", "bound", "abrupt", "target_loss", "ipv6", "restart"})
        self.assertEqual({name: control["status"] for name, control in result["refusals"].items()},
                         dict.fromkeys(("invalid_filter", "unavailable_interface", "without_net_raw"), "passed"))
        runs = [call for call in engine.calls if call[0] == "run"]
        capturer = next(call for call in runs if call[call.index("--name") + 1].endswith("-capturer"))
        self.assertEqual([arg for arg in capturer if arg.startswith("--cap")], ["--cap-drop=ALL", "--cap-add=NET_RAW"])
        self.assertFalse(any(arg.startswith("--user") or arg == "--privileged" for arg in capturer))
        self.assertEqual(capturer[capturer.index("--network") + 1], "container:ceph-extension-check-abcdef012345-target")
        self.assertIn("--internal", next(call for call in engine.calls if call[:2] == ["network", "create"]))
        traffic = engine.captures["/tmp/capture-traffic"]["args"]
        self.assertEqual(traffic[traffic.index("-i") + 1], "eth0")
        self.assertEqual(traffic[traffic.index("-s") + 1], str(SNAPLEN))
        self.assertEqual(traffic[-1], "tcp port %d and host %s" % (PORT, PEER))
        self.assertFalse(list(self.output.rglob("*.pcap")), "raw captures stay out of the report directory")

    def test_the_dual_stack_bridge_selects_ipv6_and_restarts_its_own_target(self):
        engine = FakeCaptureEngine(self)
        result = self.scenario(engine)
        self.assertEqual(result["status"], "passed", result.get("error"))
        networks = [call for call in engine.calls if call[:2] == ["network", "create"]]
        # The IPv4 bridge stays as it was; the second one is dual-stack.
        self.assertEqual([("--ipv6" in call, "--internal" in call, call[-1]) for call in networks],
                         [(False, True, "ceph-extension-check-abcdef012345"),
                          (True, True, "ceph-extension-check-abcdef012345-dual")])
        runs = [call for call in engine.calls if call[0] == "run"]
        capturer = next(call for call in runs if call[call.index("--name") + 1].endswith("-capturer6"))
        self.assertEqual([arg for arg in capturer if arg.startswith("--cap")], ["--cap-drop=ALL", "--cap-add=NET_RAW"])
        self.assertEqual(capturer[capturer.index("--network") + 1],
                         "container:ceph-extension-check-abcdef012345-target6")
        ipv6 = engine.captures["/tmp/capture-ipv6"]["args"]
        self.assertEqual(ipv6[-1], "ip6 and tcp port %d and host %s" % (PORT, PEER6))
        self.assertEqual(engine.captures["/tmp/capture-traffic"]["args"][-1], "tcp port %d and host %s" % (PORT, PEER))
        # The IPv6 capture sees the selected and the excluded port over IPv6
        # and the selected port over IPv4.
        self.assertEqual([(item["address"], item["port"]) for item in result["captures"]["ipv6"]["exchanges"]],
                         [(TARGET6, PORT), (TARGET6, EXCLUDED), (TARGET4, PORT)])
        self.assertEqual(result["captures"]["ipv6"]["summary"]["ipv4_records"], 0)
        # The restart comes last, between the connection from BEFORE and the one from AFTER.
        commands = [call[0] for call in engine.calls]
        self.assertGreater(commands.index("restart"), commands.index("kill"))
        restart_exchanges = [(source, restarted) for base, _, source, restarted in engine.exchanges
                             if base == "/tmp/capture-restart"]
        self.assertEqual(restart_exchanges, [(BEFORE, False), (AFTER, True)])
        renewed = next(call for call in runs if call[call.index("--name") + 1].endswith("-renewed6"))
        self.assertGreater(engine.calls.index(renewed), engine.calls.index(["restart", "-t", "0",
                                                                            "ceph-extension-check-abcdef012345-target6"]))
        self.assertEqual(renewed[renewed.index("--network") + 1], "container:ceph-extension-check-abcdef012345-target6")
        restart = result["captures"]["restart"]["summary"]["restart"]
        self.assertEqual({key: restart[key] for key in ("end", "followed", "interfaces", "status_after_restart")},
                         {"end": "exited", "followed": False, "interfaces": [], "status_after_restart": 1})
        self.assertEqual(result["captures"]["restart"]["summary"]["complete"], "partial")
        self.assertEqual(result["dual_stack"]["addresses"]["target"], {"ipv4": TARGET4, "ipv6": TARGET6})

    def test_a_restart_that_keeps_running_passes_on_the_synthetic_clock(self):
        clock = Clock()
        result = self.scenario(FakeCaptureEngine(self, clock=clock, restart_end="kept running"), clock)
        self.assertEqual(result["status"], "passed", result.get("error"))
        self.assertEqual(result["captures"]["restart"]["summary"]["restart"]["end"], "kept running")
        self.assertEqual(result["captures"]["restart"]["exit_code"], 0)

    def test_ipv6_and_restart_failures_keep_their_stage_and_kind(self):
        for name, engine, stage, kind in (
                ("IPv4 flow kept", FakeCaptureEngine(self, keeps_ipv4=True), "capture:ipv6", "capture_failure"),
                ("no IPv6 address", FakeCaptureEngine(self, no_ipv6=True), "setup:dual_stack", "checker_failure"),
                ("records after the restart", FakeCaptureEngine(self, restart_leaks=True), "capture:restart",
                 "capture_failure"),
                ("records after the restart at a new address",
                 FakeCaptureEngine(self, restart_leaks=True, renewed_address="172.31.0.9"), "capture:restart",
                 "capture_failure"),
                ("the helper follows the target", FakeCaptureEngine(self, follows=True), "capture:restart",
                 "capture_failure"),
                ("an interface stays", FakeCaptureEngine(self, interface_survives_restart=True), "capture:restart",
                 "capture_failure"),
                ("clean exit after the restart", FakeCaptureEngine(self, restart_status=0), "capture:restart",
                 "capture_failure"),
                ("the same sandbox", FakeCaptureEngine(self, same_sandbox=True), "capture:restart",
                 "checker_failure")):
            with self.subTest(name=name):
                self.addCleanup(shutil.rmtree, self.output / "run", ignore_errors=True)
                result = self.scenario(engine)
                shutil.rmtree(self.output / "run")
                self.assertEqual(outcome(result), ("failed", stage, kind))

    def test_failures_keep_their_kind(self):
        for name, engine, stage, kind in (
                ("root helper", FakeCaptureEngine(self, uid=0), "setup", "image_unsupported"),
                ("invalid filter", FakeCaptureEngine(self, early_exit="tcpdump: can't parse filter expression\n"),
                 "capture:traffic", "invalid_filter"),
                ("missing interface", FakeCaptureEngine(self, early_exit="tcpdump: eth0: No such device exists\n"),
                 "capture:traffic", "unavailable_interface"),
                ("excluded flow kept", FakeCaptureEngine(self, keeps_excluded=True), "capture:traffic",
                 "capture_failure"),
                ("invalid filter accepted", FakeCaptureEngine(self, invalid_filter_refused=False), "refusals",
                 "capture_failure"),
                ("starts without NET_RAW", FakeCaptureEngine(self, net_raw_refused=False), "refusals",
                 "capture_failure")):
            with self.subTest(name=name):
                result = self.scenario(engine)
                self.assertEqual((result["status"], result["failure_stage"], result["failure_kind"]),
                                 ("failed", stage, kind))
                shutil.rmtree(self.output / "run")

    def test_the_target_is_lost_only_after_every_other_capture_and_control(self):
        engine = FakeCaptureEngine(self)
        result = self.scenario(engine)
        self.assertEqual(result["status"], "passed", result.get("error"))
        commands = [call[0] for call in engine.calls]
        self.assertGreater(commands.index("kill"), max(i for i, command in enumerate(commands) if command == "start"))
        self.assertEqual(result["captures"]["abrupt"]["stop"], "SIGKILL")
        self.assertEqual(result["captures"]["target_loss"]["summary"]["loss"], {"end": "exited", "interfaces": []})
        shutil.rmtree(self.output / "run")
        for end in ("exited", "kept running"):
            with self.subTest(end=end):
                self.addCleanup(shutil.rmtree, self.output / "run", ignore_errors=True)
                result = self.scenario(FakeCaptureEngine(self, loss_end=end))
                shutil.rmtree(self.output / "run")
                self.assertEqual(result["status"], "passed", result.get("error"))
                self.assertEqual(result["captures"]["target_loss"]["summary"]["loss"]["end"], end)

    def test_a_target_loss_that_keeps_running_passes_on_the_synthetic_clock(self):
        # tcpdump keeps running past the whole observation window; the last
        # running state is kept and the SIGTERM branch ends it.
        clock = Clock()
        result = self.scenario(FakeCaptureEngine(self, clock=clock, loss_end="kept running"), clock)
        self.assertEqual(result["status"], "passed", result.get("error"))
        self.assertEqual(result["captures"]["target_loss"]["summary"]["loss"]["end"], "kept running")

    def test_partial_capture_failures_keep_their_stage(self):
        for name, engine, stage in (
                ("killed tcpdump exits 0", FakeCaptureEngine(self, killed_status=0), "capture:abrupt"),
                ("killed tcpdump prints statistics", FakeCaptureEngine(self, killed_statistics=True), "capture:abrupt"),
                ("interface survives", FakeCaptureEngine(self, interface_survives=True), "capture:target_loss"),
                ("clean exit after the loss", FakeCaptureEngine(self, loss_status=0), "capture:target_loss")):
            with self.subTest(name=name):
                self.addCleanup(shutil.rmtree, self.output / "run", ignore_errors=True)
                result = self.scenario(engine)
                shutil.rmtree(self.output / "run")
                self.assertEqual(outcome(result), ("failed", stage, "capture_failure"))

    def test_only_the_runtime_exec_refusal_passes_the_control_without_net_raw(self):
        started = {"ExitCode": 1, "Error": "", "StartedAt": "2026-10-23T00:00:00Z"}
        unstarted = {"ExitCode": 255, "Error": "", "StartedAt": "0001-01-01T00:00:00Z"}
        for name, unprivileged, kind in (
                # tcpdump started without NET_RAW and reported its own permission error.
                ("tcpdump's own permission error", (1, "tcpdump: eth0: You don't have permission to perform this "
                                                       "capture on that device\n", started), "capture_failure"),
                # The expected exit and message from a container that never started.
                ("unstarted container", (255, "exec /usr/bin/tcpdump: operation not permitted\n", unstarted),
                 "checker_failure"),
                ("runtime error", (255, "exec /usr/bin/tcpdump: operation not permitted\n",
                                   dict(unstarted, StartedAt="2026-10-23T00:00:00Z", Error="OCI runtime error")),
                 "checker_failure")):
            with self.subTest(name=name):
                self.addCleanup(shutil.rmtree, self.output / "run", ignore_errors=True)
                result = self.scenario(FakeCaptureEngine(self, unprivileged=unprivileged))
                shutil.rmtree(self.output / "run")
                self.assertEqual(outcome(result), ("failed", "refusals", kind))

    def test_waits_are_judged_on_the_synthetic_clock(self):
        # Within the 15-second budgets, the scenario passes on the synthetic clock too.
        clock = Clock()
        result = self.scenario(FakeCaptureEngine(self, clock=clock, ready_delay=5, exit_delay=5), clock)
        self.assertEqual(result["status"], "passed", result.get("error"))

    def test_late_readiness_or_exit_is_not_accepted(self):
        for name, options, stage in (("readiness at 20s", {"ready_delay": 20}, "capture:traffic"),
                                     ("exit at 20s", {"exit_delay": 20}, "capture:traffic")):
            with self.subTest(name=name):
                self.addCleanup(shutil.rmtree, self.output / "run", ignore_errors=True)
                clock = Clock()
                result = self.scenario(FakeCaptureEngine(self, clock=clock, **options), clock)
                shutil.rmtree(self.output / "run")
                self.assertEqual(outcome(result), ("failed", stage, "capture_failure"))
                self.assertIn("within 15s", result["error"])

    def test_a_blocked_state_read_is_bounded_by_the_time_left(self):
        clock = Clock()
        start = clock.now
        engine = FakeCaptureEngine(self, clock=clock, block=120)
        result = self.scenario(engine, clock)
        self.assertEqual(outcome(result), ("failed", "capture:traffic", "timeout"))
        self.assertTrue(engine.state_timeouts)
        self.assertTrue(all(timeout <= extension_check.CAPTURE_READY_TIMEOUT for timeout in engine.state_timeouts),
                        engine.state_timeouts)
        self.assertLessEqual(clock.now - start, extension_check.CAPTURE_READY_TIMEOUT)

    def test_a_docker_failure_is_a_checker_failure(self):
        def engine(*command, timeout=120, check_exit=True):
            raise extension_check.CheckError("Command failed: docker network create")

        result = self.scenario(engine)
        self.assertEqual((result["status"], result["failure_stage"], result["failure_kind"]),
                         ("failed", "setup", "checker_failure"))

    def run_main(self, results):
        image = dict(HELPER, labels={})

        def probe(extension, checked, args, output):
            checked.update(status="passed", checks=[], cleanup={"status": "passed", "errors": []})

        refusal = {"name": "refused-without-net_raw", "status": "passed", "cleanup": {"status": "passed"}}
        output = self.output / ("main-%d" % len(list(self.output.iterdir())))
        with mock.patch.object(extension_check.check, "inspect_image", return_value=dict(image)), \
                mock.patch.object(extension_check, "docker", return_value=(0, json.dumps([{"Config": {}}]))), \
                mock.patch.object(extension_check, "environment", return_value={}), \
                mock.patch.object(extension_check, "probe", side_effect=probe), \
                mock.patch.object(extension_check, "capture_refusal", return_value=refusal), \
                mock.patch.object(extension_check, "capture_scenario", side_effect=results) as scenario, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = extension_check.main(["--extension", "capture", "--image", "x", "--functional", "--target", "t",
                                         "--output-dir", str(output)])
        scenario.assert_called_once()
        return code, json.loads((output / "extension-report.json").read_text())

    def test_the_report_carries_the_scenario_outcome(self):
        passed = {"status": "passed", "cleanup": {"status": "passed", "leftovers": []}}
        code, report = self.run_main([passed])
        self.assertEqual((code, report["status"], report["level"]), (0, "passed", "functional"))
        for kind in ("invalid_filter", "permission_denied", "capture_failure", "checker_failure"):
            with self.subTest(kind=kind):
                code, report = self.run_main([{"status": "failed", "failure_kind": kind}])
                self.assertEqual((code, report["status"], report["failure_kind"]), (1, "failed", kind))


WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
PUBLISH_GATE = "inputs.publish && github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"


def section(text, start, end=None):
    begin = text.index(start)
    return text[begin:text.index(end, begin)] if end else text[begin:]


def step_eligible(condition, guard_id, outcome, job_failed):
    """Whether a step with this `if:` runs, for the conditions these
    workflows use: always(), success(), steps.<id>.outcome == '<value>' and
    &&. An unsupported condition fails the test instead of guessing."""
    terms = [term.strip() for term in condition.split("&&")]
    result = True
    has_status = False
    for term in terms:
        if term == "always()":
            has_status = True
        elif term == "success()":
            has_status = True
            result = result and not job_failed
        else:
            match = re.fullmatch(r"steps\.([\w-]+)\.outcome == '(\w+)'", term)
            if not match:
                raise AssertionError("unsupported workflow condition: " + condition)
            result = result and (match.group(1) == guard_id and outcome == match.group(2))
    # Without a status function, GitHub adds success().
    return result and (has_status or not job_failed)


class CapturePublicationTests(unittest.TestCase):
    """What a capture report must show before its helper can be published,
    with the scenario results of the actual checker on a fake engine."""

    setUp = CaptureScenarioTests.setUp
    scenario = CaptureScenarioTests.scenario

    def report(self):
        import publish
        result = self.scenario(FakeCaptureEngine(self))
        self.assertEqual(result["status"], "passed", result.get("error"))
        targets = {}
        for reference in sorted(publish.extension_targets("capture")):
            targets[reference] = json.loads(json.dumps(result))
            targets[reference]["reference"] = reference
        checks = [{"name": "path:tcpdump", "status": "passed"},
                  {"name": "refused-without-net_raw", "status": "passed", "cleanup": {"status": "passed"}}]
        image = dict(HELPER, status="passed", checks=checks, cleanup={"status": "passed"},
                     versions={"tcpdump": "tcpdump version 4.99.3 libpcap version 1.10.3"},
                     labels={"io.ceph-testcontainers.extension": "capture",
                             "io.ceph-testcontainers.extension.contract": "1"})
        return {"status": "passed", "level": "functional", "extension": "capture", "contract": "1",
                "checker_sha256": extension_check.checker_sha256(), "image": image,
                "environment": {"OperatingSystem": "Ubuntu 24.04"}, "targets": targets}

    def test_the_capture_helper_is_published(self):
        self.assertEqual(extension_check.PUBLISHED, ("netem", "capture"))
        import publish
        self.assertEqual(publish.extension_tag("capture"), publish.REGISTRY + ":extension-capture-v1")
        self.assertEqual(publish.extension_tag("capture", "arm64"),
                         publish.REGISTRY + ":extension-capture-v1-linux-arm64")

    def test_the_checkers_negative_controls_are_the_ones_publication_requires(self):
        result = self.scenario(FakeCaptureEngine(self))
        self.assertEqual({name: control["expected"] for name, control in result["refusals"].items()},
                         extension_check.CAPTURE_REFUSALS)
        self.assertEqual(set(result["captures"]), set(extension_check.CAPTURE_STOPS))

    def test_a_passing_functional_report_on_its_platform_can_be_published(self):
        import publish
        report = self.report()
        # The capture helper has no netem kernel check, selector or optional profile.
        self.assertFalse({"kernel", "selectors", "rate", "packet_faults"} & set(report))
        publish.validate_extension_report(report, "capture", "amd64")
        for architecture, extension in (("arm64", "capture"), ("amd64", "netem")):
            with self.subTest(architecture=architecture, extension=extension), \
                    self.assertRaises(publish.PublishError):
                publish.validate_extension_report(report, extension, architecture)

    def test_publication_requires_every_capture_control_and_target(self):
        import publish
        good = self.report()
        official = publish.releases.RELEASES["19.2.5"]["official"]

        def change(edit):
            value = json.loads(json.dumps(good))
            edit(value)
            return value

        bad = {
            "quick check level": change(lambda r: r.update(level="quick")),
            "another checker": change(lambda r: r.update(checker_sha256="0" * 64)),
            "no labels": change(lambda r: r["image"].update(labels={})),
            "no quick refusal": change(lambda r: r["image"].update(checks=r["image"]["checks"][:1])),
            "failed quick refusal": change(lambda r: r["image"]["checks"][1].update(status="failed")),
            "quick refusal cleanup": change(lambda r: r["image"]["checks"][1]["cleanup"].update(status="failed")),
            "missing release target": change(lambda r: r["targets"].pop(official)),
            "failed target": change(lambda r: r["targets"][official].update(status="failed")),
            "target leftovers": change(lambda r: r["targets"][official]["cleanup"].update(leftovers=["x"])),
            "another helper": change(lambda r: r["targets"][official].update(helper_image_id="sha256:" + "e" * 64)),
            "another platform": change(lambda r: r["targets"][official].update(platform="linux/arm64")),
            "missing target loss": change(lambda r: r["targets"][official]["captures"].pop("target_loss")),
            "missing abrupt": change(lambda r: r["targets"][official]["captures"].pop("abrupt")),
            "missing restart": change(lambda r: r["targets"][official]["captures"].pop("restart")),
            "missing ipv6": change(lambda r: r["targets"][official]["captures"].pop("ipv6")),
            "failed restart": change(lambda r: r["targets"][official]["captures"]["restart"].update(status="failed")),
            "ipv6 with problems": change(lambda r: r["targets"][official]["captures"]["ipv6"].update(problems=["x"])),
            "failed bound": change(lambda r: r["targets"][official]["captures"]["bound"].update(status="failed")),
            "empty with problems": change(lambda r: r["targets"][official]["captures"]["empty"].update(
                problems=["x"])),
            "no refusals": change(lambda r: r["targets"][official].pop("refusals")),
            "missing control": change(lambda r: r["targets"][official]["refusals"].pop("without_net_raw")),
            "failed control": change(lambda r: r["targets"][official]["refusals"]["invalid_filter"].update(
                status="failed")),
            "control of another kind": change(lambda r: r["targets"][official]["refusals"]["without_net_raw"].update(
                kind="not_refused")),
            "control expecting another kind": change(
                lambda r: [control.update(expected="capture_failure", kind="capture_failure")
                           for control in [r["targets"][official]["refusals"]["unavailable_interface"]]]),
        }
        for name, value in bad.items():
            with self.subTest(name=name), self.assertRaises(publish.PublishError):
                publish.validate_extension_report(value, "capture", "amd64")

    def test_the_capture_job_publishes_only_from_a_manual_run_on_the_default_branch(self):
        workflow = (WORKFLOWS / "test.yml").read_text()
        job = section(workflow, "  capture-extension:", "  promote-extension:")
        self.assertIn("make extension-test", job)
        self.assertIn("make capture-image", job)
        self.assertIn("--extension capture", job)
        self.assertIn("packages: write", job)
        steps = job.split("      - ")
        login = next(step for step in steps if "docker/login-action" in step)
        stage = next(step for step in steps if "publish.py stage-extension" in step)
        candidate = next(step for step in steps if "extension-candidate-capture-" in step)
        for step in (login, stage, candidate):
            self.assertIn("if: " + PUBLISH_GATE + "\n", step)
        self.assertIn("--extension capture --architecture ${{ matrix.architecture }}", stage)
        # The candidate comes from this job's own report, after its own check.
        self.assertIn('--report "$RUNNER_TEMP/capture/extension-report.json"', stage)
        self.assertLess(job.index("--functional"), job.index("publish.py stage-extension"))

    def test_raw_captures_are_refused_before_any_artifact_upload(self):
        job = section((WORKFLOWS / "test.yml").read_text(), "  capture-extension:", "  promote-extension:")
        published = section((WORKFLOWS / "published-extension.yml").read_text(), "  published-capture-extension:")
        for name, steps, directory in ((("test.yml", job.split("      - "), "$RUNNER_TEMP/capture/")),
                                       ("published-extension.yml", published.split("      - "),
                                        "$RUNNER_TEMP/published-extension/")):
            with self.subTest(workflow=name):
                guard = next(index for index, step in enumerate(steps) if "publish.py check-artifacts" in step)
                self.assertIn("if: always()\n", steps[guard])
                guard_id = re.search(r"^\s*id: (\S+)$", steps[guard], re.M).group(1)
                uploads = [step for step in steps if "actions/upload-artifact" in step
                           and directory.replace("$RUNNER_TEMP", "${{ runner.temp }}") in step]
                self.assertTrue(uploads)
                for upload in uploads:
                    self.assertGreater(steps.index(upload), guard)
                    condition = re.search(r"^\s*if: (.+)$", upload, re.M).group(1)
                    # Safe reports still upload after an earlier checker
                    # failure; a failed or skipped guard makes the upload
                    # ineligible.
                    self.assertTrue(step_eligible(condition, guard_id, "success", job_failed=True))
                    for outcome in ("failure", "skipped", "cancelled"):
                        self.assertFalse(step_eligible(condition, guard_id, outcome, job_failed=True), outcome)
        # The previous condition alone uploads after a failed guard.
        self.assertTrue(step_eligible("always()", "raw-captures", "failure", job_failed=True))

    def test_promotion_takes_only_both_capture_candidates_and_checks_the_published_digests(self):
        workflow = (WORKFLOWS / "test.yml").read_text()
        promote = section(workflow, "  promote-capture-extension:", "  verify-published-capture-extension:")
        self.assertIn("if: " + PUBLISH_GATE + "\n", promote)
        self.assertIn("needs: capture-extension\n", promote)
        self.assertIn("pattern: extension-candidate-capture-*\n", promote)
        self.assertIn("promote-extension --extension capture", promote)
        verify = section(workflow, "  verify-published-capture-extension:")
        self.assertIn("if: " + PUBLISH_GATE + "\n", verify)
        self.assertIn("needs: promote-capture-extension\n", verify)
        self.assertIn("extension: capture\n", verify)
        self.assertIn("images: ${{ needs.promote-capture-extension.outputs.images }}", verify)
        # netem's promotion no longer picks up the capture candidates.
        netem = section(workflow, "  promote-extension:", "  verify-published-extension:")
        self.assertIn("pattern: extension-candidate-netem-*\n", netem)
        self.assertIn("promote-extension --extension netem", netem)

    def test_the_published_capture_helper_is_checked_by_digest_as_in_ci(self):
        published = (WORKFLOWS / "published-extension.yml").read_text()
        netem = section(published, "  published-extension:", "  published-capture-extension:")
        capture = section(published, "  published-capture-extension:")
        self.assertIn("if: inputs.extension == 'netem'", netem)
        self.assertIn("if: inputs.extension == 'capture'", capture)
        self.assertIn("published-reference --extension \"$IMAGE_EXTENSION\"", capture)
        self.assertIn("--extension capture --image \"$reference\"", capture)
        self.assertIn("--functional", capture)
        # The same targets as the capture job, and no netem profile.
        self.assertIn('python3 image/releases.py official "$release"', capture)
        self.assertNotIn("releases.py role", capture)
        for option in ("--selector", "--rate", "--packet-faults"):
            self.assertNotIn(option, capture)


if __name__ == "__main__":
    unittest.main()
