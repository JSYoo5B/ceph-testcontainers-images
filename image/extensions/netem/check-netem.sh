#!/bin/sh
# Quick check of the netem helper contract (docs/EXTENSIONS.md). It needs no
# capability and no network: run it as root in a disposable container started
# with --network=none and --cap-drop=ALL. Besides shell builtins it uses only
# the contract's executables, and it changes nothing.
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

for utility in sh sleep tc ip python3; do
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

# iproute2 tools print "<tool> utility, iproute2-<version>...".
version() {
    output=$("$1" -V 2>&1) || return 1
    case "$output" in
        *iproute2*) printf 'VERSION\t%s\t%s\n' "$1" "$output" ;;
        *) return 1 ;;
    esac
}
check version:tc version tc
check version:ip version ip

# The module reads state only as JSON.
json() {
    output=$("$@" 2>/dev/null) || return 1
    printf '%s' "$output" | python3 -c 'import json, sys; json.load(sys.stdin)'
}
check json:tc-qdisc json tc -j -s qdisc show
check json:tc-filter json tc -j -s filter show dev lo
check json:ip-link json ip -j link show
check json:ip-address json ip -j address show
check json:ip-route json ip -j route show

# tc parses netem options before it asks the kernel. Without NET_ADMIN the
# kernel refuses; a tc without netem support fails while parsing instead.
netem_parser() {
    if output=$(tc qdisc add dev lo root handle c7e1: netem delay 100ms 10ms 2>&1); then
        return 1
    fi
    case "$output" in
        *'Operation not permitted'*) return 0 ;;
        *) return 1 ;;
    esac
}
check netem-parser netem_parser

# The selector profiles add a prio root and u32 filters with IPv4 and IPv6
# keys. tc parses them before it asks the kernel, as it does netem.
refused() {
    if output=$("$@" 2>&1); then
        return 1
    fi
    case "$output" in
        *'Operation not permitted'*) return 0 ;;
        *) return 1 ;;
    esac
}
check prio-parser refused tc qdisc add dev lo root handle 7e00: prio bands 3 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
check u32-parser:ip refused tc filter add dev lo parent 7e00: protocol ip pref 10 handle 800::1 u32 \
    match ip dst 192.0.2.1/32 match ip protocol 6 0xff match ip dport 7000 0xffff flowid 7e00:3
check u32-parser:ip6 refused tc filter add dev lo parent 7e00: protocol ipv6 pref 11 handle 801::1 u32 \
    match ip6 dst 2001:db8::1/128 match ip6 protocol 6 0xff match ip6 dport 7000 0xffff flowid 7e00:3

# The command supervisor's imports, and the subreaper call it makes.
check python-runtime python3 -c '
import base64, ctypes, json, os, select, signal, subprocess, sys, time
assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
'
# The supervisor keeps its markers below /tmp.
check writable:/tmp python3 -c '
import os, tempfile
descriptor, path = tempfile.mkstemp(dir="/tmp")
os.close(descriptor)
os.unlink(path)
'
printf 'COMPLETE\tnetem\n'
exit "$failed"
