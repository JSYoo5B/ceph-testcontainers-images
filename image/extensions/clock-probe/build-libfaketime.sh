#!/bin/sh
# Build libfaketime twice from one pinned commit and write the build manifest.
# Runs once during the image build. Needs git, a C compiler, make and python3,
# and the network for the single fetch of the pinned commit; the source tree
# is removed afterwards, so neither the network nor the tree is needed later.
set -eu
repository=https://github.com/wolfcw/libfaketime.git
: "${LIBFAKETIME_TAG:?}" "${LIBFAKETIME_COMMIT:?}"
root=/opt/ceph-testcontainers/libfaketime
source_tree=$(mktemp -d)
trap 'rm -rf "$source_tree"' EXIT

cd "$source_tree"
git init -q .
git fetch -q --depth 1 "$repository" "$LIBFAKETIME_COMMIT"
git checkout -q --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "$LIBFAKETIME_COMMIT"
tree=$(git rev-parse 'HEAD^{tree}')

# The two variants differ only in INTERCEPT_SYSCALL, which diverts glibc's
# syscall() for known syscalls (upstream Makefile). Only the single-threaded
# library is built; the wrapper program and the MT library are not part of the
# contract.
build() {
    name=$1
    flags=$2
    make -C src clean >/dev/null
    make -C src FAKETIME_COMPILE_CFLAGS="$flags" libfaketime.so.1
    install -D -m 0644 src/libfaketime.so.1 "$root/$name/libfaketime.so.1"
}
build default ''
build intercept-syscall '-DINTERCEPT_SYSCALL'

mkdir -p /usr/share/doc/libfaketime
cp COPYING /usr/share/doc/libfaketime/copyright

python3 - "$root" "$LIBFAKETIME_TAG" "$LIBFAKETIME_COMMIT" "$tree" <<'MANIFEST'
import hashlib, json, platform, subprocess, sys
root, tag, commit, tree = sys.argv[1:]
def first_line(*command):
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout.splitlines()[0]
variants = {}
for name, flags in (("default", ""), ("intercept-syscall", "-DINTERCEPT_SYSCALL")):
    path = root + "/" + name + "/libfaketime.so.1"
    with open(path, "rb") as library:
        digest = hashlib.sha256(library.read()).hexdigest()
    variants[name] = {"path": path, "sha256": digest, "intercept_syscall": bool(flags),
                      "compile_flags": flags}
manifest = {"schema": 1,
            "source": {"repository": "https://github.com/wolfcw/libfaketime", "tag": tag, "commit": commit,
                       "tree": tree},
            "toolchain": {"compiler": first_line("gcc", "--version"), "libc": first_line("ldd", "--version")},
            "architecture": platform.machine(),
            "variants": variants}
with open(root + "/build.json", "w") as output:
    json.dump(manifest, output, indent=2, sort_keys=True)
    output.write("\n")
MANIFEST
