#!/bin/sh
# Quick check of the image requirements. No build metadata, package manager or
# Python is needed for non-control roles. Run only in a disposable container as root.
set -u
role=${1:?role required}
failed=0
work=/tmp/ceph-image-check-$$

check() {
    name=$1
    shift
    if "$@"; then
        printf 'CHECK\tpassed\t%s\n' "$name"
    else
        printf 'CHECK\tfailed\t%s\n' "$name"
        failed=1
    fi
}

# test/sleep/sh must also work through Docker exec, outside a shell.
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

for utility in sh mkdir cp cat rm test sleep hostname awk; do
    check "path:$utility" external_command "$utility"
done
check shell test -x /bin/sh
if ! mkdir -p "$work"; then
    printf 'CHECK\tfailed\twritable:/tmp\n'
    exit 1
fi
trap 'rm -rf "$work"' EXIT HUP INT TERM

writable() {
    directory=$1
    mkdir -p "$directory" &&
        printf 'ceph-image-check\n' > "$directory/.ceph-image-check-$$" &&
        cp "$directory/.ceph-image-check-$$" "$work/copied" &&
        test "$(cat "$work/copied")" = ceph-image-check &&
        rm "$directory/.ceph-image-check-$$" "$work/copied"
}
for directory in /etc/ceph /var/lib/ceph /var/run/ceph /var/log/ceph /tmp; do
    check "writable:$directory" writable "$directory"
done
# Modules copy their own files into new directories; the path is their choice.
new_directory() {
    writable "/ceph-image-check-$$" && rmdir "/ceph-image-check-$$"
}
check new-directory new_directory
check tmp-sticky test -k /tmp
check hostname-ip /bin/sh -c 'hostname -i | awk "{print \$1}" | { read -r address; test -n "$address"; }'
check sleep-infinity /bin/sh -c '
    sleep infinity >/dev/null 2>&1 &
    sleeper=$!
    sleep 1
    if ! kill -0 "$sleeper" 2>/dev/null; then
        wait "$sleeper" 2>/dev/null
        exit 1
    fi
    kill "$sleeper"
    wait "$sleeper" 2>/dev/null || true
'

control=false
osd=false
rgw=false
mds=false
case "$role" in
    control) control=true ;;
    osd) osd=true ;;
    rgw) rgw=true ;;
    mds) mds=true ;;
    all) control=true; osd=true; rgw=true; mds=true ;;
    *) exit 2 ;;
esac

version() {
    binary=$1
    external_command "$binary" && "$binary" --version > "$work/version" 2>&1 || {
        cat "$work/version" 2>/dev/null || true
        return 1
    }
    printf 'VERSION\t%s\t%s\n' "$binary" "$(cat "$work/version")"
}
if "$control"; then
    for binary in ceph-mon ceph-mgr ceph ceph-authtool monmaptool rados rbd; do
        check "version:$binary" version "$binary"
    done
    check path:python3 external_command python3
    check python-bindings python3 -c 'import rados, rbd, cephfs, ceph_argparse, ceph_daemon'
    mgr_modules() {
        module_path=$(ceph-mgr --show-config-value mgr_module_path) || return 1
        python3 - "$module_path" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
assert (root / "mgr_module.py").is_file(), "missing mgr_module.py"
for name in ("volumes", "rbd_support", "mirroring"):
    assert (root / name / "module.py").is_file(), "missing MGR module: " + name
PY
    }
    check mgr-modules mgr_modules
    check tmp-mode python3 -c 'import os, stat; assert stat.S_IMODE(os.stat("/tmp").st_mode) == 0o1777'
    check keyring ceph-authtool --create-keyring "$work/keyring" --gen-key -n client.admin
    check keyring-read /bin/sh -c 'ceph-authtool "$1" --print-key > /dev/null' check "$work/keyring"
    check monmap monmaptool --create --fsid 9df1f6ea-6047-4d0c-bb3d-408fbef094b4 \
        --addv a '[v2:127.0.0.1:3300,v1:127.0.0.1:6789]' "$work/monmap"
    for binary in rbd-mirror cephfs-mirror radosgw-admin; do
        check "version:$binary" version "$binary"
    done
fi

plugins() {
    directory=$1
    pattern=$2
    for library in "$directory/"$pattern; do
        test ! -f "$library" || return 0
    done
    return 1
}
if "$osd"; then
    check version:ceph-osd version ceph-osd
    class_dir=$(ceph-osd --show-config-value osd_class_dir) || failed=1
    plugin_dir=$(ceph-osd --show-config-value plugin_dir) || failed=1
    for class in rbd rgw cephfs; do
        check "object-class:$class" plugins "$class_dir" "libcls_$class.so*"
    done
    for group in compressor erasure-code; do
        check "plugins:$group" plugins "$plugin_dir/$group" '*.so*'
    done
fi
if "$rgw"; then
    check path:readlink external_command readlink
    check version:radosgw version radosgw
    check version:radosgw-admin version radosgw-admin
fi
if "$mds"; then
    check version:ceph-mds version ceph-mds
fi
printf 'COMPLETE\t%s\n' "$role"
exit "$failed"
