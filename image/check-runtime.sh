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
        case "$name" in
            path:*|object-class:*) kind=missing_file ;;
            library-load:*|class-load:*|version:*) kind=loading_failure ;;
            *) kind=quick_failure ;;
        esac
        printf 'FAILURE\t%s\t%s\n' "$kind" "$name"
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
    check path:cryptsetup external_command cryptsetup
    if external_command cryptsetup; then
        check library-load:cryptsetup-executable cryptsetup --version
    fi
    # Resolve by linker names, not package names or distribution-specific paths.
    python3 - <<'PY' || failed=1
import ctypes
import ctypes.util
import os
import subprocess
import sys
failed = False
def result(name, passed, kind):
    global failed
    print('CHECK\t%s\t%s' % ('passed' if passed else 'failed', name))
    if not passed:
        failed = True
        print('FAILURE\t%s\t%s' % (kind, name))
libraries = {
    'rbd': ('librbd.so.1', ('rbd_encryption_format', 'rbd_encryption_load')),
    'cryptsetup': ('libcryptsetup.so.12', ('crypt_init', 'crypt_load', 'crypt_keyslot_change_by_passphrase')),
    'radosstriper': ('libradosstriper.so.1', ('rados_striper_create', 'rados_striper_write',
                                         'rados_striper_read', 'rados_striper_remove')),
}
for name, (fallback, symbols) in libraries.items():
    # find_library may need ldconfig or a compiler, neither is a role
    # requirement. The loader can resolve the supported ABI directly too.
    soname = ctypes.util.find_library(name) or fallback
    try:
        library = ctypes.CDLL(soname, mode=os.RTLD_NOW)
    except OSError as error:
        print(str(error))
        missing = str(error).startswith(soname + ': cannot open shared object file')
        result('library-present:' + name, not missing, 'missing_file')
        if not missing:
            result('library-load:' + name, False, 'loading_failure')
        continue
    result('library-present:' + name, True, 'missing_file')
    try:
        for symbol in symbols:
            getattr(library, symbol)
    except AttributeError as error:
        print(str(error))
        result('library-load:' + name, False, 'loading_failure')
    else:
        result('library-load:' + name, True, 'loading_failure')
try:
    import rados, rbd
    assert callable(rbd.Image.encryption_format) and callable(rbd.Image.encryption_load)
    assert callable(rados.WriteOpCtx.execute) and callable(rados.Ioctx.lock_exclusive)
    assert callable(rados.Ioctx.unlock)
except (ImportError, AttributeError, AssertionError) as error:
    print(str(error))
    result('python-client-features', False, 'loading_failure')
else:
    result('python-client-features', True, 'loading_failure')
help_output = subprocess.run(['rados', '--help'], capture_output=True, text=True)
result('rados-striper-option', help_output.returncode == 0 and '--striper' in help_output.stdout,
       'unsupported_feature')
sys.exit(1 if failed else 0)
PY
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
    class_load() {
        for library in "$class_dir/libcls_$1.so"*; do
            if test -f "$library"; then
                # Object classes reference OSD exports: load in ceph-osd itself,
                # with eager ELF relocation, without Python or a host compiler.
                status=0
                LD_BIND_NOW=1 LD_PRELOAD="$library" ceph-osd --version \
                    > "$work/class-version" 2> "$work/class-error" || status=$?
                cat "$work/class-version" "$work/class-error"
                test "$status" -eq 0 || return "$status"
                # glibc can ignore an invalid/missing preload and still exit 0.
                # That is a loading failure, even if ceph-osd prints a version.
                awk 'BEGIN {failed=0}
                    /cannot be preloaded|cannot open shared object|wrong ELF|invalid ELF|undefined symbol|ERROR: ld.so|Error loading|Error relocating/ {failed=1}
                    END {exit failed}' "$work/class-error"
                return $?
            fi
        done
        return 1
    }
    for class in rbd rgw cephfs hello lock; do
        check "object-class:$class" plugins "$class_dir" "libcls_$class.so*"
        if plugins "$class_dir" "libcls_$class.so*"; then
            check "class-load:$class" class_load "$class"
        fi
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
