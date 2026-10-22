#!/bin/sh
# Quick check of the packet-capture helper contract (docs/EXTENSIONS.md). It
# needs no network and NET_RAW alone: run it as the image's own user in a
# disposable container started with --network=none, --cap-drop=ALL and
# --cap-add=NET_RAW.
# Besides shell builtins it uses only the contract's executables, and it
# changes nothing.
# shellcheck disable=SC2329 # the checks run through check()
set -u
failed=0

check() {
    name=$1
    shift
    if "$@"; then
        printf 'CHECK\tpassed\t%s\n' "$name"
    else
        printf 'CHECK\tfailed\t%s\n' "$name"
        case "$name" in
            path:*) kind=missing_executable ;;
            version:*|python-runtime|sleep-infinity) kind=loading_failure ;;
            *) kind=quick_failure ;;
        esac
        printf 'FAILURE\t%s\t%s\n' "$kind" "$name"
        failed=1
    fi
}

# Modules run these through Docker exec, outside a shell.
external_command() {
    (IFS=:
    for directory in $PATH; do
        test -n "$directory" || directory=.
        if test -f "$directory/$1" && test -x "$directory/$1"; then
            return 0
        fi
    done
    return 1)
}

for utility in sh sleep tcpdump python3; do
    check "path:$utility" external_command "$utility"
done

# A helper idles in sleep infinity until the module removes it.
sleep_infinity() {
    sleep infinity &
    pid=$!
    sleep 1
    kill -0 "$pid" 2>/dev/null || return 1
    kill "$pid"
    { wait "$pid"; } 2>/dev/null
    return 0
}
check sleep-infinity sleep_infinity

# tcpdump --version prints the tcpdump and the libpcap version.
version() {
    output=$(tcpdump --version 2>&1) || return 1
    case "$output" in
        *'tcpdump version'*'libpcap version'*) ;;
        *) return 1 ;;
    esac
    printf 'VERSION\ttcpdump\t%s\n' "$(printf '%s' "$output" | python3 -c 'import sys; print(" ".join(sys.stdin.read().split()))')"
}
check version:tcpdump version

# The helper runs unprivileged: the module never runs it as root, so tcpdump
# neither drops privileges nor changes the ownership of its output.
check user:unprivileged python3 -c 'import os, sys; sys.exit(os.getuid() == 0 or os.geteuid() == 0)'

# tcpdump holds cap_net_raw (bit 13) as a permitted and effective file
# capability, so NET_RAW in the container's bounding set is all it needs.
check file-capability:net_raw python3 -c '
import os, shutil, struct, sys
data = os.getxattr(shutil.which("tcpdump"), "security.capability")
magic, permitted = struct.unpack_from("<II", data)
sys.exit(not (magic & 1 and permitted >> 13 & 1))
'

# The module compiles a filter before it starts a capture; tcpdump compiles
# one for a link type without opening a device or any capability.
compiles() {
    tcpdump -y EN10MB -d "$1" >/dev/null 2>&1
}
refuses_filter() {
    if output=$(tcpdump -y EN10MB -d "$1" 2>&1); then
        return 1
    fi
    case "$output" in
        *"can't parse filter expression"*) return 0 ;;
        *) return 1 ;;
    esac
}
check filter:compile compiles 'tcp port 7000 and host 192.0.2.1 and not (host 192.0.2.9)'
check filter:compile-ip6 compiles 'ip6 and tcp port 7000 and host 2001:db8::1'
check filter:invalid refuses_filter 'tcp portx 7000'

# A short capture on the loopback, with the options that the module uses: it
# needs NET_RAW and no network. Readiness is tcpdump's "listening on" line;
# a TERM signal ends it with exit status 0, a flushed classic pcap and its
# statistics. The filter must keep the datagram to UDP port 7000 and drop
# the one to port 7001; each carries its own payload.
remove_tree() {
    python3 -c 'import shutil, sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' "$1"
}
capture_lo() {
    directory=$(python3 -c 'import tempfile; print(tempfile.mkdtemp(dir="/tmp"))') || return 1
    tcpdump -p -i lo -s 128 -c 100 --immediate-mode -U -w "$directory/lo.pcap" 'udp port 7000' \
        2>"$directory/stderr" &
    pid=$!
    if ! python3 - "$directory/stderr" <<'READY'
import sys, time
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    try:
        with open(sys.argv[1]) as stderr:
            if any(line.startswith("tcpdump: listening on lo, link-type EN10MB") for line in stderr):
                sys.exit(0)
    except OSError:
        pass
    time.sleep(0.1)
sys.exit(1)
READY
    then
        kill "$pid" 2>/dev/null
        { wait "$pid"; } 2>/dev/null
        remove_tree "$directory"
        return 1
    fi
    python3 -c '
import socket
for port in (7000, 7001):
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"capture-check-%d" % port, ("127.0.0.1", port))
'
    sleep 0.5
    kill -TERM "$pid"
    wait "$pid"
    status=$?
    python3 - "$directory" "$status" <<'ORACLE'
import pathlib, struct, sys
directory, status = pathlib.Path(sys.argv[1]), int(sys.argv[2])
stderr = (directory / "stderr").read_text()
data = (directory / "lo.pcap").read_bytes()
print("CAPTURE\t%s" % " ".join(line.strip() for line in stderr.splitlines()))
if len(data) < 24 or status != 0 or "1 packet captured" not in stderr or "packets received by filter" not in stderr:
    sys.exit(1)
magic, major, minor, _, _, snaplen, link = struct.unpack_from("<IHHiIII", data)
if magic != 0xa1b2c3d4 or (major, minor) != (2, 4) or snaplen != 128 or link != 1:
    sys.exit(1)
# Every record as (UDP destination port, payload): Ethernet, IPv4, UDP.
datagrams, offset = [], 24
while offset < len(data):
    if offset + 16 > len(data):
        sys.exit(1)
    captured = struct.unpack_from("<I", data, offset + 8)[0]
    frame = data[offset + 16:offset + 16 + captured]
    offset += 16 + captured
    if len(frame) != captured or len(frame) < 14 + 20 + 8 or frame[12:14] != b"\x08\x00":
        sys.exit(1)
    header = (frame[14] & 0x0f) * 4
    if frame[14 + 9] != 17 or len(frame) < 14 + header + 8:
        sys.exit(1)
    port = struct.unpack_from(">H", frame, 14 + header + 2)[0]
    datagrams.append((port, frame[14 + header + 8:]))
sys.exit(datagrams != [(7000, b"capture-check-7000")])
ORACLE
    result=$?
    remove_tree "$directory"
    return "$result"
}
check capture:loopback capture_lo

# The command supervisor's imports, and the subreaper call it makes.
check python-runtime python3 -c '
import base64, ctypes, json, os, select, signal, subprocess, sys, time
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
'
# Captures and the supervisor's markers live below /tmp.
check writable:/tmp python3 -c '
import os, tempfile
descriptor, path = tempfile.mkstemp(dir="/tmp")
os.close(descriptor)
os.unlink(path)
'
printf 'COMPLETE\tcapture\n'
exit "$failed"
