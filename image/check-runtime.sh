#!/bin/sh
# Quick check of the image requirements. No build metadata, package manager or
# Python is needed for non-control roles. Run only in a disposable container as root.
set -u
role=${1:?role required}
contract=${2:-legacy}
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

core=false
client=false
cache=false
fsmirror=false
control=false
osd=false
rgw=false
mds=false
case "$role" in
    essential) core=true; osd=true ;;
    client) client=true ;;
    cephfs) mds=true; fsmirror=true ;;
    rbd-aux) cache=true ;;
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
    # Python startup diagnostics can precede the version (notably on Alpine).
    # Preserve them in the log, but emit exactly one protocol VERSION record.
    cat "$work/version"
    release=$(awk '/^ceph version / {count++; value=$0} END {if (count != 1) exit 1; print value}' "$work/version") || return 1
    printf 'VERSION\t%s\t%s\n' "$binary" "$release"
}
# pgrep -f reports exactly a live process found by its command line, and
# nothing once that process has exited and been reaped.
pgrep_inspection() {
    sentinel=ceph-image-check-pgrep-$$
    rm -f "$work/pgrep-ready"
    python3 -c 'import sys, time; open(sys.argv[1], "w").close(); time.sleep(60)' \
        "$work/pgrep-ready" "$sentinel" &
    child=$!
    tries=0
    while ! test -e "$work/pgrep-ready" && test "$tries" -lt 10; do
        sleep 1
        tries=$((tries + 1))
    done
    found=$(pgrep -f -- "$sentinel")
    status=$?
    kill "$child" 2>/dev/null
    wait "$child" 2>/dev/null
    if test "$status" -ne 0 || test "$found" != "$child"; then
        printf 'pgrep -f found [%s] with exit %s while process %s ran\n' "$found" "$status" "$child"
        return 1
    fi
    found=$(pgrep -f -- "$sentinel")
    status=$?
    if test "$status" -ne 1 || test -n "$found"; then
        printf 'pgrep -f found [%s] with exit %s after process %s exited\n' "$found" "$status" "$child"
        return 1
    fi
}
if "$control" || "$core" || "$client" || "$cache" || "$fsmirror" || { "$rgw" && test "$contract" = roles-v2; }; then
    binaries="ceph"
    if "$control" || "$core"; then binaries="$binaries ceph-mon ceph-mgr ceph-authtool monmaptool"; fi
    if "$control" || "$client" || "$cache"; then binaries="$binaries rados rbd"; fi
    for binary in $binaries; do
        check "version:$binary" version "$binary"
    done
    check path:python3 external_command python3
    check python-cli python3 -c 'import rados, ceph_argparse, ceph_daemon'
    if "$control" || "$client"; then
        check python-bindings python3 -c 'import rados, rbd, cephfs, ceph_argparse, ceph_daemon'
    fi
    if "$cache"; then check python-rbd python3 -c 'import rados,rbd'; fi
    if { "$client" || "$control"; } && test "$contract" = roles-v2; then
    map_tools_roundtrip() {
        python3 - "$work" <<'MAPTOOLS'
from pathlib import Path
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(dir=sys.argv[1]) as directory:
    root = Path(directory)
    osdmap, exported, text, compiled, restored = (root/name for name in ('osdmap', 'exported', 'crush.txt', 'compiled', 'restored'))
    def run(*args):
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30).stdout
    run('osdmaptool', str(osdmap), '--createsimple', '2')
    printed = run('osdmaptool', str(osdmap), '--print')
    assert 'max_osd 2' in printed.splitlines(), 'synthetic OSD map lost its device limit: '+printed
    run('osdmaptool', str(osdmap), '--export-crush', str(exported))
    decoded = run('crushtool', '-d', str(exported))
    assert 'device 0 osd.0' in decoded and 'device 1 osd.1' in decoded, 'CRUSH map lost device identities'
    text.write_text(decoded)
    run('crushtool', '-c', str(text), '-o', str(compiled))
    assert run('crushtool', '-d', str(compiled)) == decoded, 'CRUSH decode/compile differs'
    run('osdmaptool', str(osdmap), '--import-crush', str(compiled))
    run('osdmaptool', str(osdmap), '--export-crush', str(restored))
    assert restored.read_bytes() == compiled.read_bytes(), 'OSD map CRUSH bytes differ'
MAPTOOLS
    }
    check path:crushtool external_command crushtool
    check path:osdmaptool external_command osdmaptool
    check map-tools-roundtrip map_tools_roundtrip
    fi
    if "$control" || "$client" || "$cache"; then
    if "$control" || "$client"; then
    check path:cryptsetup external_command cryptsetup
    if external_command cryptsetup; then
        check library-load:cryptsetup-executable cryptsetup --version
    fi
    fi
    check path:pgrep external_command pgrep
    if external_command pgrep; then
        check process-inspection:pgrep pgrep_inspection
    fi
    # Resolve by linker names, not package names or distribution-specific paths.
    python3 - "$role" <<'PY' || failed=1
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
if sys.argv[1] == 'rbd-aux':
    libraries = {}
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
# librbd loads a plugin by name from the plugin directory, so the parent
# cache client needs that file and its dependencies, not only librbd.
if sys.argv[1] in ('control','all','rbd-aux'):
    config_program = 'ceph-conf' if sys.argv[1] == 'rbd-aux' else 'ceph-mgr'
    plugin_dir = subprocess.run([config_program, '--show-config-value', 'plugin_dir'],
                                capture_output=True, text=True).stdout.strip()
    plugin = os.path.join(plugin_dir, 'librbd', 'libceph_librbd_parent_cache.so')
    present = bool(plugin_dir) and os.path.isfile(plugin)
    result('library-present:rbd-parent-cache', present, 'missing_file')
    if present:
        try:
            library = ctypes.CDLL(plugin, mode=os.RTLD_NOW)
            getattr(library, '__ceph_plugin_init')
            getattr(library, '__ceph_plugin_version')
        except (OSError, AttributeError) as error:
            print(str(error))
            result('library-load:rbd-parent-cache', False, 'loading_failure')
        else:
            result('library-load:rbd-parent-cache', True, 'loading_failure')
if sys.argv[1] != 'rbd-aux':
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
    fi
    if "$control" || "$core"; then
    if "$core"; then
        check path:pgrep external_command pgrep
        check process-inspection:pgrep pgrep_inspection
    fi
    mgr_modules() {
        module_path=$(ceph-mgr --show-config-value mgr_module_path) || return 1
        python3 - "$module_path" "$contract" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
assert (root / "mgr_module.py").is_file(), "missing mgr_module.py"
modules = ("volumes", "rbd_support", "mirroring")
if sys.argv[2] == "roles-v2":
    modules += ("snap_schedule",)
for name in modules:
    assert (root / name / "module.py").is_file(), "missing MGR module: " + name
PY
    }
    check mgr-modules mgr_modules
    check tmp-mode python3 -c 'import os, stat; assert stat.S_IMODE(os.stat("/tmp").st_mode) == 0o1777'
    check keyring ceph-authtool --create-keyring "$work/keyring" --gen-key -n client.admin
    check keyring-read /bin/sh -c 'ceph-authtool "$1" --print-key > /dev/null' check "$work/keyring"
    check monmap monmaptool --create --fsid 9df1f6ea-6047-4d0c-bb3d-408fbef094b4 \
        --addv a '[v2:127.0.0.1:3300,v1:127.0.0.1:6789]' "$work/monmap"
    fi
    binaries=""
    if "$control"; then binaries="rbd-mirror cephfs-mirror radosgw-admin ceph-immutable-object-cache"; fi
    if "$cache"; then binaries="rbd-mirror ceph-immutable-object-cache"; fi
    if "$fsmirror"; then binaries="cephfs-mirror"; fi
    for binary in $binaries; do
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
