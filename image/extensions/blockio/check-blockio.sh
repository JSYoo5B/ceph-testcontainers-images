#!/bin/sh
# Image-only probe: no network, capabilities, host devices or kernel queries.
# shellcheck disable=SC2329
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
            cli:*|version:*|python-runtime|sleep-infinity) kind=loading_failure ;;
            *) kind=quick_failure ;;
        esac
        printf 'FAILURE\t%s\t%s\n' "$kind" "$name"
        failed=1
    fi
}

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
for utility in sh sleep python3 dmsetup losetup blockdev; do
    check "path:$utility" external_command "$utility"
done

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

# Help loads each executable and its shared-library closure without opening
# device-mapper control, allocating a loop device or querying any block device.
# In particular, dmsetup version and targets are not image-only checks.
cli_help() {
    output=$("$1" --help 2>&1) || return 1
    shift
    for required in "$@"; do
        case "$output" in
            *"$required"*) ;;
            *) return 1 ;;
        esac
    done
}
check cli:dmsetup cli_help dmsetup create remove suspend resume load clear table info --verifyudev --noudevsync --uuid
check cli:losetup cli_help losetup --find --show --detach --associated --list --json --output
check cli:blockdev cli_help blockdev --getsize64

version() {
    output=$("$1" --version 2>&1) || return 1
    case "$output" in
        *util-linux*) printf 'VERSION\t%s\t%s\n' "$1" "$output" ;;
        *) return 1 ;;
    esac
}
check version:losetup version losetup
check version:blockdev version blockdev
check user:root python3 -c 'import os; assert os.geteuid() == 0'
check python-runtime python3 -c '
import base64, ctypes, errno, json, os, select, signal, struct, subprocess, sys, tempfile, time
assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
'
check writable:/tmp python3 -c '
import os, tempfile
fd, path = tempfile.mkstemp(dir="/tmp")
os.close(fd)
os.unlink(path)
'
printf 'COMPLETE\tblockio\n'
exit "$failed"
