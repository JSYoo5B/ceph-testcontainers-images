#!/bin/sh
# Quick check of the clock-probe helper contract (docs/EXTENSIONS.md). It needs
# no network and no capability: run it as root in a disposable container started
# with --network=none and --cap-drop=ALL.
# Besides shell builtins it uses only the contract's executables, and it
# changes nothing outside its own temporary directory.
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
            version:*|python-runtime|sleep-infinity|compile:*|load:*) kind=loading_failure ;;
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

for utility in sh sleep python3 gcc nm clock-probe-build-info; do
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

# The compiler's version and the Python runtime that the oracles below use.
version() {
    output=$(gcc --version 2>&1) || return 1
    printf 'VERSION\tgcc\t%s\n' "$(printf '%s' "$output" | python3 -c 'import sys; print(sys.stdin.read().splitlines()[0])')"
}
check version:gcc version
check python-runtime python3 -c '
import base64, ctypes, hashlib, json, os, platform, select, signal, subprocess, sys, tempfile, time
'
check writable:/tmp python3 -c '
import os, tempfile
descriptor, path = tempfile.mkstemp(dir="/tmp")
os.close(descriptor)
os.unlink(path)
'

# The manifest names the pinned source and both variants. Each variant's file
# must exist with the recorded SHA-256 and INTERCEPT_SYSCALL setting, the two
# files must differ, and the architecture must be the one that runs.
manifest() {
    clock-probe-build-info | python3 -c '
import hashlib, json, platform, re, sys
try:
    manifest = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
def fail(text):
    print("MANIFEST\t" + text)
    sys.exit(1)
if not isinstance(manifest, dict) or manifest.get("schema") != 1:
    fail("schema")
source = manifest.get("source")
if not isinstance(source, dict) or not all(isinstance(source.get(key), str) and source[key]
                                           for key in ("repository", "tag", "commit", "tree")):
    fail("source")
if not re.fullmatch("[0-9a-f]{40}", source["commit"]) or not re.fullmatch("[0-9a-f]{40}", source["tree"]):
    fail("source identity")
if manifest.get("architecture") != platform.machine():
    fail("architecture")
toolchain = manifest.get("toolchain")
if not isinstance(toolchain, dict) or not all(isinstance(toolchain.get(key), str) and toolchain[key]
                                              for key in ("compiler", "libc")):
    fail("toolchain")
variants = manifest.get("variants")
if not isinstance(variants, dict) or set(variants) != {"default", "intercept-syscall"}:
    fail("variants")
digests = set()
for name, variant in variants.items():
    if not isinstance(variant, dict) or variant.get("intercept_syscall") is not (name == "intercept-syscall"):
        fail("intercept_syscall of " + name)
    path = variant.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        fail("path of " + name)
    try:
        with open(path, "rb") as library:
            data = library.read()
    except OSError:
        fail("missing file of " + name)
    digest = hashlib.sha256(data).hexdigest()
    if variant.get("sha256") != digest or not data.startswith(b"\x7fELF"):
        fail("hash of " + name)
    digests.add(digest)
if len(digests) != 2:
    fail("identical variants")
print("MANIFEST\t" + source["tag"] + " " + source["commit"][:12] + " " + manifest["architecture"])
'
}
check manifest manifest

# INTERCEPT_SYSCALL is a compile-time switch: only the variant built with it
# exports syscall(). This is what route 2 of a probe needs to be able to tell.
exports_syscall() {
    clock-probe-build-info | python3 -c '
import json, subprocess, sys
variants = json.load(sys.stdin)["variants"]
for name, variant in variants.items():
    symbols = subprocess.run(["nm", "-D", "--defined-only", variant["path"]], capture_output=True, text=True)
    if symbols.returncode != 0:
        sys.exit(1)
    exported = any(line.split()[-2:] == ["T", "syscall"] for line in symbols.stdout.splitlines())
    if exported is not variant["intercept_syscall"]:
        sys.exit(1)
'
}
check symbol:syscall exports_syscall

# The compiler builds and runs a program, and each variant loads into it
# through LD_PRELOAD and a timestamp file that this check owns. The program is
# not instrumented unless preloaded, and FAKETIME is never set: libfaketime
# disables its timestamp file when FAKETIME is present. A +0 file leaves the
# public realtime clock where an uninstrumented reader sees it, and a +3600
# file moves it by that offset, within a bound far below the offset. This
# qualifies that the build loads and shifts one path of one process. It does
# not measure other routes, restoration or any Ceph daemon.
shift_oracle() {
    clock-probe-build-info | python3 -c '
import json, os, subprocess, sys, tempfile, time
variants = json.load(sys.stdin)["variants"]
program = """#include <stdio.h>
#include <time.h>
int main(void) {
    struct timespec realtime;
    if (clock_gettime(CLOCK_REALTIME, &realtime) != 0)
        return 2;
    printf("%lld\\n", (long long)realtime.tv_sec);
    return 0;
}
"""
BOUND = 5
with tempfile.TemporaryDirectory(dir="/tmp") as directory:
    source = os.path.join(directory, "now.c")
    binary = os.path.join(directory, "now")
    with open(source, "w") as output:
        output.write(program)
    build = subprocess.run(["gcc", "-O0", "-o", binary, source], capture_output=True, text=True)
    if build.returncode != 0:
        print("COMPILE\t" + build.stderr.strip()[:300])
        sys.exit(3)
    def run(variant, content):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("FAKETIME") and key != "LD_PRELOAD"}
        if variant is not None:
            stamp = os.path.join(directory, "offset")
            with open(stamp, "w") as output:
                output.write(content + "\n")
            environment.update(LD_PRELOAD=variants[variant]["path"], FAKETIME_TIMESTAMP_FILE=stamp)
        before = time.time()
        result = subprocess.run([binary], capture_output=True, text=True, env=environment, timeout=30)
        after = time.time()
        if result.returncode != 0 or not result.stdout.strip().isdigit():
            print("RUN\t" + str(variant) + " " + str(result.returncode) + " " + result.stderr.strip()[:200])
            sys.exit(1)
        observed = int(result.stdout)
        return observed - after, observed - before
    low, high = run(None, "")
    if not (low > -BOUND and high < BOUND):
        sys.exit(1)
    for name in sorted(variants):
        for content, offset in (("+0", 0), ("+3600", 3600)):
            low, high = run(name, content)
            print("SHIFT\t%s %s %.0f..%.0f" % (name, content, low, high))
            if low < offset - BOUND or high > offset + BOUND:
                sys.exit(1)
'
}
check load:shift shift_oracle

printf 'COMPLETE\tclock-probe\n'
exit "$failed"
