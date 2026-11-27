#!/bin/sh
# Build the libfaketime default interposer from one pinned commit and write the
# build manifest. Runs once in the build stage of the clock helper. Needs git, a
# C compiler, make and python3, and the network for the single fetch of the
# pinned commit. None of them is part of the published image.
set -eu
repository=https://github.com/wolfcw/libfaketime.git
: "${LIBFAKETIME_TAG:?}" "${LIBFAKETIME_COMMIT:?}" "${OUTPUT:?}"
root=$OUTPUT/opt/ceph-testcontainers/clock
source_tree=$(mktemp -d)
trap 'rm -rf "$source_tree"' EXIT

cd "$source_tree"
git init -q .
git fetch -q --depth 1 "$repository" "$LIBFAKETIME_COMMIT"
git checkout -q --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "$LIBFAKETIME_COMMIT"
tree=$(git rev-parse 'HEAD^{tree}')

# Only the single-threaded library of the upstream default build: no
# INTERCEPT_SYSCALL, which hangs every process that loads tcmalloc, and so
# every Ceph daemon of the role images.
make -C src FAKETIME_COMPILE_CFLAGS='' libfaketime.so.1
install -D -m 0644 src/libfaketime.so.1 "$root/libfaketime.so.1"
install -D -m 0644 COPYING "$OUTPUT/usr/share/doc/libfaketime/copyright"

python3 - "$root" "$LIBFAKETIME_TAG" "$LIBFAKETIME_COMMIT" "$tree" <<'MANIFEST'
import hashlib, json, platform, subprocess, sys
root, tag, commit, tree = sys.argv[1:]
def first_line(*command):
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.splitlines()[0]
path = "/opt/ceph-testcontainers/clock/libfaketime.so.1"
with open(root + "/libfaketime.so.1", "rb") as library:
    digest = hashlib.sha256(library.read()).hexdigest()
manifest = {"schema": 1,
            "source": {"repository": "https://github.com/wolfcw/libfaketime", "tag": tag, "commit": commit,
                       "tree": tree},
            "toolchain": {"compiler": first_line("gcc", "--version"), "libc": first_line("ldd", "--version")},
            "architecture": platform.machine(),
            "interposer": {"build": "default", "path": path, "sha256": digest, "intercept_syscall": False,
                           "compile_flags": ""}}
with open(root + "/build.json", "w") as output:
    json.dump(manifest, output, indent=2, sort_keys=True)
    output.write("\n")
MANIFEST
