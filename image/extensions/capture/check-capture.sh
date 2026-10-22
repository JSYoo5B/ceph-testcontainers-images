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
    printf 'VERSION\ttcpdump\t%s\n' "$(printf '%s' "$output" | tr '\n' ' ')"
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
# statistics. The filter keeps the UDP datagram to port 7000 and drops the
# one to port 7001.
capture_lo() {
    directory=$(python3 -c 'import tempfile; print(tempfile.mkdtemp(dir="/tmp"))') || return 1
    tcpdump -p -i lo -s 128 -c 100 --immediate-mode -U -w "$directory/lo.pcap" 'udp port 7000' \
        2>"$directory/stderr" &
    pid=$!
    ready=1
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25; do
        if grep -q '^tcpdump: listening on lo, link-type EN10MB' "$directory/stderr" 2>/dev/null; then
            ready=0
            break
        fi
        sleep 0.2
    done
    if test "$ready" -ne 0; then
        kill "$pid" 2>/dev/null
        { wait "$pid"; } 2>/dev/null
        rm -rf "$directory"
        return 1
    fi
    python3 -c '
import socket
for port in (7000, 7001):
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"capture-check", ("127.0.0.1", port))
'
    sleep 0.5
    kill -TERM "$pid"
    wait "$pid"
    status=$?
    python3 - "$directory" "$status" <<'PY'
import pathlib, struct, sys
directory, status = pathlib.Path(sys.argv[1]), int(sys.argv[2])
stderr = (directory / "stderr").read_text()
data = (directory / "lo.pcap").read_bytes()
magic, major, minor, _, _, snaplen, link = struct.unpack_from("<IHHiIII", data)
captured, original = struct.unpack_from("<II", data, 24 + 8)
payload = data[24 + 16:24 + 16 + captured]
ok = (status == 0 and "1 packet captured" in stderr and "packets received by filter" in stderr
      and magic == 0xa1b2c3d4 and (major, minor) == (2, 4) and snaplen == 128 and link == 1
      and len(data) == 24 + 16 + captured and payload.endswith(b"capture-check"))
print("CAPTURE\t%s" % " ".join(line.strip() for line in stderr.splitlines()))
sys.exit(not ok)
PY
    result=$?
    rm -rf "$directory"
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
