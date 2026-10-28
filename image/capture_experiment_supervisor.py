"""Experiment supervisor for the capture file-size-limit diagnostic.

This file runs inside the capture helper, so it uses only the standard
library of python3, which contract 1 promises; the `resource` module is not
promised and its absence is a recorded result, not a crash. It is passed to
`python3 -c` by image/capture_experiment.py and never imported by the helper.

The supervisor owns one tcpdump child. The child's standard error is a pipe
read here, so the experimental RLIMIT_FSIZE, which Linux applies to every
regular file the limited task writes, can never cut the diagnostic stream.
Retention is bounded (a head and a tail); anything dropped is counted and
reported as overflow, which the oracle treats as inconclusive. The limit is
lowered on the child alone through resource.prlimit, after the caller has
established a verified prefix, and the supervisor's own limit is read before
and after to show that it did not change.

Commands arrive as JSON lines on standard input; each event leaves as one JSON
line on standard output, and the last line is the receipt.
"""
import hashlib
import json
import signal
import subprocess
import sys
import threading
import time

try:
    import resource
except ImportError:  # contract 1 does not promise the module
    resource = None

SCHEMA = "capture-experiment-supervisor/v1"
HEAD_BYTES = 4096
TAIL_BYTES = 4096
FINAL_WAIT = 5.0
READER_WAIT = 5.0
SIGNALS = ("SIGTERM", "SIGINT", "SIGKILL")
DISPOSITIONS = {"default": True, "ignore": False}  # restore_signals of subprocess


class Retention:
    """Bounded copy of a byte stream: the first and the last bytes, plus the
    count of everything that passed."""

    def __init__(self, head=HEAD_BYTES, tail=TAIL_BYTES):
        self.limits = (head, tail)
        self.head, self.tail, self.total = bytearray(), bytearray(), 0
        self.lock = threading.Lock()

    def feed(self, chunk):
        with self.lock:
            self.total += len(chunk)
            room = self.limits[0] - len(self.head)
            self.head += chunk[:room]
            self.tail += chunk[room:]
            del self.tail[:max(len(self.tail) - self.limits[1], 0)]

    def text(self):
        with self.lock:
            return bytes(self.head + self.tail).decode("utf-8", "replace")

    def snapshot(self):
        with self.lock:
            retained = len(self.head) + len(self.tail)
            return {"head": bytes(self.head).decode("utf-8", "replace"),
                    "tail": bytes(self.tail).decode("utf-8", "replace"),
                    "total": self.total, "retained": retained, "overflow": self.total > retained}


def drain(stream, retention):
    for chunk in iter(lambda: stream.read1(4096), b""):
        retention.feed(chunk)


def signal_disposition(pid, signum, proc="/proc"):
    """How the process handles a signal, from the masks in /proc/PID/status:
    "ignored", "caught", "default" or "unknown" when the masks cannot be read."""
    try:
        with open("%s/%s/status" % (proc, pid)) as f:
            lines = f.read().splitlines()
        masks = {key: int(value.strip(), 16) for key, _, value in (line.partition(":") for line in lines)
                 if key in ("SigIgn", "SigCgt")}
    except (OSError, ValueError):
        return "unknown"
    bit = 1 << (signum - 1)
    if "SigIgn" not in masks or "SigCgt" not in masks:
        return "unknown"
    if masks["SigIgn"] & bit:
        return "ignored"
    return "caught" if masks["SigCgt"] & bit else "default"


def file_prefix(path, size):
    """(file size, SHA-256 of the first `size` bytes) or (None, None)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None, None
    return len(data), hashlib.sha256(data[:size]).hexdigest()


def apply_limit(command, child, pcap, module, proc="/proc"):
    """Lower the child's RLIMIT_FSIZE to prefix + extra bytes, only while the
    pcap is exactly `prefix` bytes long. Every refusal and error is a result."""
    event = {"op": "limit", "prefix": command.get("prefix"), "extra": command.get("extra"), "status": "refused"}
    prefix, extra = event["prefix"], event["extra"]
    if any(type(value) is not int or value < 0 for value in (prefix, extra)):
        event["error"] = "prefix and extra must be non-negative integers"
        return event
    if module is None or not hasattr(module, "prlimit"):
        event.update(status="unavailable", error="resource.prlimit is not available")
        return event
    kind = module.RLIMIT_FSIZE
    event["bytes"] = prefix + extra
    size, digest = file_prefix(pcap, prefix)
    event.update(size_before=size, prefix_sha256=digest)
    if size != prefix:
        event["error"] = "the file is not exactly the prefix"
        return event
    try:
        event["own_before"] = list(module.getrlimit(kind))
        event["before"] = list(module.prlimit(child.pid, kind))
        module.prlimit(child.pid, kind, (prefix + extra, prefix + extra))
        event["after"] = list(module.prlimit(child.pid, kind))
        event["own_after"] = list(module.getrlimit(kind))
    except (OSError, ValueError, OverflowError) as error:
        event.update(status="error", error="%s: %s" % (type(error).__name__, str(error)[:200]))
        return event
    event["size_after"] = file_prefix(pcap, prefix)[0]
    event["sigxfsz"] = signal_disposition(child.pid, signal.SIGXFSZ, proc)
    event["status"] = "applied"
    return event


def wait_stderr(command, retention):
    deadline = time.monotonic() + float(command.get("timeout", 15))
    needle = command.get("contains")
    while True:
        seen = isinstance(needle, str) and needle in retention.text()
        if seen or time.monotonic() >= deadline:
            return {"op": "wait_stderr", "contains": needle, "seen": seen}
        time.sleep(0.05)


def wait_exit(child, timeout):
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def signal_name(number):
    try:
        return signal.Signals(number).name
    except ValueError:
        return "SIG%d" % number


def run(config, commands, emit, popen=subprocess.Popen, module=resource, proc="/proc"):
    """Run the child under the commands and return the receipt."""
    sigxfsz = config["sigxfsz"]
    child = popen(config["argv"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                  restore_signals=DISPOSITIONS[sigxfsz], close_fds=True)
    retention = Retention(config.get("head", HEAD_BYTES), config.get("tail", TAIL_BYTES))
    reader = threading.Thread(target=drain, args=(child.stderr, retention), daemon=True)
    reader.start()
    events, sent = [], []
    receipt = {"schema": SCHEMA,
               "launcher": {"restore_signals": DISPOSITIONS[sigxfsz], "sigxfsz": sigxfsz,
                            "python": sys.version.split()[0],
                            "own_sigxfsz": signal_disposition("self", signal.SIGXFSZ, proc)},
               "child": {"pid": child.pid, "argv": list(config["argv"])}, "events": events}

    def record(event):
        events.append(event)
        emit(event)

    for command in commands:
        name = command.get("op") if isinstance(command, dict) else None
        try:
            if name == "wait_stderr":
                record(wait_stderr(command, retention))
            elif name == "limit":
                record(apply_limit(command, child, config.get("pcap"), module, proc))
            elif name == "signal" and command.get("name") in SIGNALS:
                alive = child.poll() is None
                if alive:
                    child.send_signal(getattr(signal, command["name"]))
                    sent.append(command["name"])
                record({"op": "signal", "name": command["name"], "sent": alive})
            elif name == "wait_exit":
                wait_exit(child, float(command.get("timeout", 5)))
                record({"op": "wait_exit", "exited": child.poll() is not None})
            else:
                record({"op": "invalid"})
        except (TypeError, ValueError):
            record({"op": "invalid"})
    wait_exit(child, FINAL_WAIT)
    running = child.poll() is None
    if running:
        child.kill()
        child.wait()
    reader.join(READER_WAIT)
    if not reader.is_alive():
        child.stderr.close()
    code = child.returncode
    receipt["exit"] = {"returncode": code, "signal": signal_name(-code) if code < 0 else None,
                       "running": running, "reaped_with_sigkill": running, "sent": sent}
    receipt["stderr"] = retention.snapshot()
    return receipt


def main(argv, stdin, stdout):
    config = json.loads(argv[1])
    argv_ok = isinstance(config.get("argv"), list) and all(isinstance(item, str) for item in config["argv"])
    if not argv_ok or config["argv"] == [] or config.get("sigxfsz") not in DISPOSITIONS:
        raise SystemExit("config needs argv (a list of strings) and sigxfsz (default or ignore)")

    def emit(event):
        stdout.write(json.dumps(event) + "\n")
        stdout.flush()

    def commands():
        for line in stdin:
            try:
                yield json.loads(line)
            except ValueError:
                yield None

    emit(run(config, commands(), emit))


if __name__ == "__main__":
    main(sys.argv, sys.stdin, sys.stdout)
