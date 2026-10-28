"""Launcher and oracle of the capture file-size-limit experiment.

This is the experiment supervisor's host side (docs/EXTENSIONS.md, "Capture
file-size-limit experiment"). It builds the `docker exec` command that runs
image/capture_experiment_supervisor.py inside the capture helper, and it
judges the supervisor's output together with the pcap that the experiment
left. It starts nothing itself, is wired into no workflow and changes neither
the helper, the pcap contract nor the publication gate.

The oracle never turns a missing or malformed observation into a result: it
answers "observed" only when every rule holds, and otherwise "inconclusive"
with the reasons. Each branch is its own outcome: how tcpdump ended
(`termination`) and whether the file ends in a cut record header, a cut record
body or neither (`cut_record`) are reported separately and never stand in for
each other or for the SIGKILL of the abrupt capture.
"""
import hashlib
import json
from pathlib import Path
import re

import extension_check

SUPERVISOR_SOURCE = (Path(__file__).resolve().parent / "capture_experiment_supervisor.py").read_text()
SCHEMA = "capture-experiment-supervisor/v1"
SIGXFSZ_NUMBER = 25
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def tcpdump_argv(base, interface, expression, packets):
    """The tcpdump of the functional check's capture, as a list for the supervisor."""
    return ["tcpdump", "-p", "-i", interface, "-s", str(extension_check.CAPTURE_SNAPLEN), "-c", str(packets),
            "--immediate-mode", "-U", "-w", base + ".pcap", expression]


def supervisor_config(argv, pcap, sigxfsz):
    """The supervisor's configuration. The child's SIGXFSZ disposition is
    always named: "default" restores it (the child dies of the signal) and
    "ignore" keeps it ignored (the write fails with EFBIG)."""
    if sigxfsz not in ("default", "ignore"):
        raise ValueError("sigxfsz is default or ignore, not %r" % (sigxfsz,))
    return {"argv": list(argv), "pcap": pcap, "sigxfsz": sigxfsz}


def supervisor_command(container, config):
    """The `docker exec` arguments that run the supervisor, which reads its
    commands from the standard input of the call."""
    return ["exec", "-i", container, "python3", "-c", SUPERVISOR_SOURCE, json.dumps(config)]


def limit_command(prefix, extra):
    """Lower the child's limit to prefix + extra, only while the file is exactly prefix bytes."""
    return {"op": "limit", "prefix": prefix, "extra": extra}


def read_receipt(text):
    """The supervisor's last output line as the receipt, or None when the
    output is empty, is not JSON lines or does not end in a receipt."""
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        items = [json.loads(line) for line in lines]
    except ValueError:
        return None
    if not items or any(not isinstance(item, dict) for item in items) or items[-1].get("schema") != SCHEMA:
        return None
    return items[-1]


def is_int(value):
    return type(value) is int


def is_limit(value):
    return isinstance(value, list) and len(value) == 2 and all(is_int(item) for item in value)


def structure_problems(receipt):
    """Why a receipt cannot be read at all: missing, null or wrong-typed fields."""
    if not isinstance(receipt, dict) or receipt.get("schema") != SCHEMA:
        return ["no supervisor receipt"]
    problems = []
    launcher, child, events = receipt.get("launcher"), receipt.get("child"), receipt.get("events")
    ended, stderr = receipt.get("exit"), receipt.get("stderr")
    if not isinstance(launcher, dict) or launcher.get("sigxfsz") not in ("default", "ignore") \
            or launcher.get("restore_signals") is not (launcher.get("sigxfsz") == "default") \
            or not isinstance(launcher.get("python"), str) or not isinstance(launcher.get("own_sigxfsz"), str):
        problems.append("the launcher's SIGXFSZ setting is not recorded")
    if not isinstance(child, dict) or not is_int(child.get("pid")) or child["pid"] <= 0:
        problems.append("the child's PID is not recorded")
    if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
        problems.append("the events are not recorded")
    if not isinstance(ended, dict) or not is_int(ended.get("returncode")) \
            or not isinstance(ended.get("running"), bool) or not isinstance(ended.get("reaped_with_sigkill"), bool) \
            or not isinstance(ended.get("sent"), list) or not all(isinstance(name, str) for name in ended["sent"]) \
            or not (ended.get("signal") is None or isinstance(ended.get("signal"), str)):
        problems.append("the child's end is not recorded")
    if not isinstance(stderr, dict) or not all(isinstance(stderr.get(key), str) for key in ("head", "tail")) \
            or not is_int(stderr.get("total")) or not is_int(stderr.get("retained")) \
            or not isinstance(stderr.get("overflow"), bool):
        problems.append("the retained standard error is not recorded")
    elif stderr["retained"] > stderr["total"] or stderr["overflow"] is not (stderr["total"] > stderr["retained"]):
        problems.append("the standard error's counts contradict its overflow flag")
    return problems


def limit_problems(limit, launcher):
    """Why a limit event does not prove a lowered limit on a verified prefix."""
    if limit.get("status") != "applied":
        return ["the limit was not applied: %s %s" % (limit.get("status"), limit.get("error", ""))]
    problems = []
    prefix, extra, bytes_ = limit.get("prefix"), limit.get("extra"), limit.get("bytes")
    if not (is_int(prefix) and is_int(extra) and is_int(bytes_) and prefix > 0 and extra > 0
            and bytes_ == prefix + extra):
        return ["the limit's prefix, extra and bytes are not consistent integers"]
    for key in ("before", "after", "own_before", "own_after"):
        if not is_limit(limit.get(key)):
            problems.append("the %s limit was not read back" % key.replace("_", " "))
    if problems:
        return problems
    if limit["after"] != [bytes_, bytes_]:
        problems.append("the child's limit reads %s after lowering it to %d" % (limit["after"], bytes_))
    if limit["before"][0] != -1 and limit["before"][0] <= bytes_:
        problems.append("the child's limit was already %d, not above %d" % (limit["before"][0], bytes_))
    if limit["own_before"] != limit["own_after"]:
        problems.append("the supervisor's own limit changed")
    if limit.get("size_before") != prefix or limit.get("size_after") != prefix:
        problems.append("the file was not stable at the prefix of %d bytes" % prefix)
    if not isinstance(limit.get("prefix_sha256"), str) or not HEX_DIGEST.fullmatch(limit["prefix_sha256"]):
        problems.append("the prefix has no SHA-256")
    if limit.get("sigxfsz") != launcher["sigxfsz"]:
        problems.append("the child's SIGXFSZ is %s, requested %s" % (limit.get("sigxfsz"), launcher["sigxfsz"]))
    return problems


def file_problems(limit, data, snaplen):
    """Problems of the pcap against the limit and its prefix, and the cut
    record: "header", "body" or "none"."""
    prefix, problems = limit["prefix"], []
    head_records, head_problems = extension_check.parse_pcap(data[:prefix], snaplen)
    if head_problems or not head_records:
        problems.append("the prefix is not a whole number of complete records: %s"
                        % "; ".join(head_problems or ["no record"]))
    if len(data) < prefix or hashlib.sha256(data[:prefix]).hexdigest() != limit["prefix_sha256"]:
        problems.append("the file does not begin with the recorded prefix")
    if len(data) > limit["bytes"]:
        problems.append("the file holds %d bytes, over its limit of %d" % (len(data), limit["bytes"]))
    records, parsed = extension_check.parse_pcap(data, snaplen)
    others, cut = extension_check.cut_tail(parsed)
    problems += others
    kind = "none"
    if cut:
        kind = "header" if any(item.startswith("a record header is cut") for item in parsed) else "body"
        start = extension_check.PCAP_HEADER + sum(extension_check.PCAP_RECORD + len(frame) for frame, _ in records)
        tail = len(data) - start
        if (kind == "header") is not (0 < tail < extension_check.PCAP_RECORD):
            problems.append("a cut %s of %d bytes is inconsistent" % (kind, tail))
    return problems, kind


def termination(receipt):
    """How the child ended, as a label, and why that is not a result when it is not."""
    ended, launcher = receipt["exit"], receipt["launcher"]
    code, sent = ended["returncode"], ended["sent"]
    if ended["running"]:
        return "still_running", [] if ended["reaped_with_sigkill"] else ["a running child was not reaped"]
    if ended["reaped_with_sigkill"]:
        return "inconsistent", ["the child ended but is recorded as reaped with SIGKILL"]
    if code == -SIGXFSZ_NUMBER:
        if launcher["sigxfsz"] != "default":
            return "killed_by_SIGXFSZ", ["SIGXFSZ ended a child that was told to ignore it"]
        return "killed_by_SIGXFSZ", []
    if code == -9:
        # Never relabelled: the abrupt capture's SIGKILL is a different branch.
        return "killed_by_SIGKILL", ["SIGKILL %s" % ("was sent by the supervisor" if "SIGKILL" in sent
                                                      else "ended the child unrequested")]
    if code < 0:
        return "killed_by_signal_%d" % -code, []
    if code == 0:
        return "exited_0_after_SIGTERM" if "SIGTERM" in sent else "exited_0_unrequested", []
    return "exited_%d" % code, []


def assess(receipt, data, snaplen=extension_check.CAPTURE_SNAPLEN):
    """The experiment's result from the supervisor's receipt and the pcap bytes."""
    result = {"verdict": "inconclusive", "reasons": [], "termination": None, "cut_record": None,
              "statistics": None, "limit": None, "file_bytes": len(data) if isinstance(data, bytes) else None}
    result["reasons"] += structure_problems(receipt)
    if not isinstance(data, bytes):
        result["reasons"].append("the pcap is not bytes")
    if result["reasons"]:
        return result
    result["termination"], reasons = termination(receipt)
    result["reasons"] += reasons
    stderr = receipt["stderr"]
    if stderr["overflow"]:
        # Overflow is kept as such: the statistics are unknown, never zero or absent.
        result["reasons"].append("the standard error overflowed its retention: %d of %d bytes kept"
                                 % (stderr["retained"], stderr["total"]))
    else:
        result["statistics"] = extension_check.capture_statistics(stderr["head"] + stderr["tail"])
    limits = [event for event in receipt["events"] if event.get("op") == "limit"]
    if len(limits) != 1:
        result["reasons"].append("%d limit events, not one" % len(limits))
        return result
    reasons = limit_problems(limits[0], receipt["launcher"])
    result["reasons"] += reasons
    if reasons:
        return result
    result["limit"] = {key: limits[0][key] for key in ("prefix", "extra", "bytes", "sigxfsz")}
    reasons, result["cut_record"] = file_problems(limits[0], data, snaplen)
    result["reasons"] += reasons
    if not result["reasons"]:
        result["verdict"] = "observed"
    return result
