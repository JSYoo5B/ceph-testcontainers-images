#!/bin/sh
# Quick check of the image requirements. No build metadata, package manager or
# Python probes run only in roles that provide the interpreter. Run only in a disposable container as root.
set -u
role=${1:?role required}
shift
# The mgr-modules role takes the requested MGR module names after the role.
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
osd=false
rgw=false
mds=false
fstools=false
mgr=false
mgrbundle=false
case "$role" in
    essential) core=true; osd=true; mgr=true ;;
    mgr-modules) mgr=true; mgrbundle=true ;;
    client) client=true ;;
    cephfs) mds=true; fsmirror=true; fstools=true ;;
    rbd-aux) cache=true ;;
    rgw) rgw=true ;;
    all) core=true; client=true; cache=true; fsmirror=true; osd=true; rgw=true; mds=true; fstools=true; mgr=true ;;
    *) exit 2 ;;
esac
# Alpine packages the CephFS recovery tools in ceph19-utils, which the
# experimental Alpine source does not install; its contract omits them.
if "$fstools" && test -r /etc/os-release && (. /etc/os-release; test "${ID:-}" = alpine); then
    fstools=false
fi

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
if "$core" || "$client" || "$cache" || "$fsmirror" || "$rgw" || "$mgrbundle"; then
    binaries="ceph"
    if "$core"; then binaries="$binaries ceph-mon ceph-mgr ceph-authtool monmaptool"; fi
    if "$mgrbundle"; then binaries="$binaries ceph-mgr"; fi
    if "$client" || "$cache"; then binaries="$binaries rados rbd"; fi
    for binary in $binaries; do
        check "version:$binary" version "$binary"
    done
    check path:python3 external_command python3
    check python-cli python3 -c 'import rados, ceph_argparse, ceph_daemon'
    if "$client"; then
        check python-bindings python3 -c 'import rados, rbd, cephfs, ceph_argparse, ceph_daemon'
    fi
    if "$cache"; then check python-rbd python3 -c 'import rados,rbd'; fi
    if "$client"; then
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
    if "$client" || "$cache"; then
    if "$client"; then
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
if sys.argv[1] in ('all','rbd-aux'):
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
    if "$core"; then
    if "$core"; then
        check path:pgrep external_command pgrep
        check process-inspection:pgrep pgrep_inspection
    fi
    check tmp-mode python3 -c 'import os, stat; assert stat.S_IMODE(os.stat("/tmp").st_mode) == 0o1777'
    check keyring ceph-authtool --create-keyring "$work/keyring" --gen-key -n client.admin
    check keyring-read /bin/sh -c 'ceph-authtool "$1" --print-key > /dev/null' check "$work/keyring"
    check monmap monmaptool --create --fsid 9df1f6ea-6047-4d0c-bb3d-408fbef094b4 \
        --addv a '[v2:127.0.0.1:3300,v1:127.0.0.1:6789]' "$work/monmap"
    fi
    if "$mgr"; then
    # The release's always-on modules, volumes, rbd_support, mirroring and
    # snap_schedule, and any requested module, each with its import closure.
    # Modules are parsed, never imported: mgr_module needs the native bindings
    # that only the embedded MGR interpreter provides.
    mgr_module_path=$(ceph-mgr --show-config-value mgr_module_path 2>/dev/null)
    mgr_release=$(ceph-mgr --version 2>/dev/null | awk '/^ceph version / {print $3; exit}')
    python3 - "$mgr_module_path" "$mgr_release" "$@" <<'MGR' || failed=1
import ast
import operator
import os
from pathlib import Path
import re
import subprocess
import sys

# Observed with `ceph mgr module ls` on a MON of each catalogued release
# (Squid 19.2.5, Tentacle 20.2.4 and Umbrella 21.2.0), keyed by major release.
ALWAYS_ON = dict.fromkeys(('19', '20', '21'), (
    'balancer', 'crash', 'devicehealth', 'orchestrator', 'pg_autoscaler', 'progress',
    'rbd_support', 'status', 'telemetry', 'volumes'))
BASELINE = ('volumes', 'rbd_support', 'mirroring', 'snap_schedule')
# Files a module needs besides Python, checked only when it is selected.
ASSETS = {'dashboard': ('frontend/dist',)}
# Provided by the embedded MGR interpreter, not by an importable file.
EMBEDDED = {'ceph_module'}
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
failed = False


def result(name, passed, kind, detail=''):
    global failed
    if detail:
        print(detail)
    print('CHECK\t%s\t%s' % ('passed' if passed else 'failed', name))
    if not passed:
        failed = True
        print('FAILURE\t%s\t%s' % (kind, name))


# The exceptions a handler must name to catch a failed import: a missing
# module raises ModuleNotFoundError, a missing name ImportError, and an error
# while a found module runs only Exception and BaseException catch.
CATCHES = {'ModuleNotFoundError': {'ModuleNotFoundError', 'ImportError', 'Exception', 'BaseException'},
           'ImportError': {'ImportError', 'Exception', 'BaseException'}}
TRY = tuple(t for t in (ast.Try, getattr(ast, 'TryStar', None)) if t)


def caught(handler):
    """The exception names a handler catches; a bare except catches any."""
    if handler.type is None:
        return {'BaseException'}
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {getattr(t, 'id', getattr(t, 'attr', '')) for t in types}


def environ(node):
    """Whether an expression reads the environment (os.environ, os.getenv, or
    the names imported from os)."""
    return any((isinstance(n, ast.Attribute) and n.attr in ('environ', 'getenv')
                and isinstance(n.value, ast.Name) and n.value.id == 'os')
               or (isinstance(n, ast.Name) and n.id in ('environ', 'getenv')) for n in ast.walk(node))


COMPARE = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
           ast.Eq: operator.eq, ast.NotEq: operator.ne}


def version_gate(test):
    """The value of a `sys.version_info <op> (x, y)` test for this
    interpreter, which is the MGR's, or None for any other test."""
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and type(test.ops[0]) in COMPARE):
        return None
    left = test.left
    if not ((isinstance(left, ast.Attribute) and left.attr == 'version_info'
             and isinstance(left.value, ast.Name) and left.value.id == 'sys')
            or (isinstance(left, ast.Name) and left.id == 'version_info')):
        return None
    try:
        bound = ast.literal_eval(test.comparators[0])
    except ValueError:
        return None
    if not (isinstance(bound, tuple) and all(isinstance(part, int) for part in bound)):
        return None
    return COMPARE[type(test.ops[0])](tuple(sys.version_info), bound)


def gated(node):
    """Which branches of an if are type-checking or environment-gated hooks
    (UNITTEST, COVERAGE_ENABLED), or Python-version branches this interpreter
    never takes: (body, orelse)."""
    test = node.test
    taken = version_gate(test)
    if taken is not None:
        return not taken, taken
    if (getattr(test, 'id', None) or getattr(test, 'attr', None)) == 'TYPE_CHECKING':
        return True, False
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and environ(test.comparators[0]):
        if isinstance(test.ops[0], ast.In):
            return True, False
        if isinstance(test.ops[0], ast.NotIn):
            return False, True
    if environ(test):
        return True, False
    return False, False


def requirements(path):
    """What a file needs to load, as a tree in statement order: an import
    is an ('external', spec), ('internal', parts, name, shown) or
    ('failure', exception name, report) leaf, a block is ('all', items),
    and a try is ('try', body, handlers, orelse), each handler a
    (caught names, block) pair. finally always runs, so its imports join
    the enclosing block."""
    # The package a relative import starts from: the file's directory, as
    # the MGR imports each module directory as a package.
    package = path.relative_to(root).parts[:-1]

    def block(nodes):
        items = []
        for node in nodes:
            visit(node, items)
        return ('all', items)

    def visit(node, out):
        if isinstance(node, TRY):
            out.append(('try', block(node.body), [(caught(h), block(h.body)) for h in node.handlers],
                        block(node.orelse)))
            out.extend(block(node.finalbody)[1])
            return
        if isinstance(node, ast.If):
            body, orelse = gated(node)
            visit(node.test, out)
            if not body:
                out.extend(block(node.body)[1])
            if not orelse:
                out.extend(block(node.orelse)[1])
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.extend(dependency(alias.name, None, 'import ' + alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                out.extend(dependency(node.module, None if alias.name == '*' else alias.name,
                                      shown(node.module, alias.name)))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out.append(relative(package, node.level, node.module,
                                    None if alias.name == '*' else alias.name))
        for child in ast.iter_child_nodes(node):
            visit(child, out)

    return block(ast.parse(path.read_bytes(), str(path)).body)


def sources(root, name):
    """The Python files of an internal name; module test suites are not run."""
    if (root / (name + '.py')).is_file():
        return [root / (name + '.py')]
    def test_file(path):
        parts = path.relative_to(root / name).parts
        return (any(part in ('tests', 'test') for part in parts[:-1])
                or parts[-1].startswith('test_') or parts[-1] == 'conftest.py')
    return sorted(p for p in (root / name).rglob('*.py') if not test_file(p))


def spec(module, name):
    """The probe argument and the reported form of an import."""
    return module + ':' + name if name else module


def internal(top):
    return ((root / (top + '.py')).is_file() or (root / top / '__init__.py').is_file()
            or (root / top / 'module.py').is_file())


def located(parts):
    """Whether an internal dotted path has a file. Internal files cannot be
    imported outside the MGR, so the path is checked on disk (a directory is
    a namespace package)."""
    path = root.joinpath(*parts)
    return (path.with_name(path.name + '.py').is_file() or path.is_dir()
            or any(path.parent.glob(path.name + '.*.so')))


def shown(module, name):
    return 'from %s import %s' % (module, name) if name else 'import ' + module


def relative(package, level, module, name):
    """The leaf of `from .module import name` (level dots) in package,
    resolved as the import statement does. Relative imports stay within
    their top-level name, so the target is internal."""
    written = shown('.' * level + (module or ''), name or '*')
    if not package:
        return ('failure', 'ImportError', '%s: relative import with no parent package' % written)
    if level > len(package):
        return ('failure', 'ImportError', '%s: relative import beyond the top-level package' % written)
    parts = package[:len(package) - level + 1] + tuple(module.split('.') if module else ())
    return ('internal', parts, name, written)


# Names every module has without binding them.
MODULE_NAMES = {'__name__', '__file__', '__doc__', '__package__', '__spec__', '__loader__',
                '__path__', '__builtins__', '__cached__'}


def bound(path, cache={}):
    """The names a module file binds at module level, or None when it may
    bind any name (a star import, a module __getattr__ or globals())."""
    if path in cache:
        return cache[path]
    names = set(MODULE_NAMES)

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            # Only a global statement binds module names from a nested scope.
            for inner in ast.walk(node):
                if isinstance(inner, ast.Global):
                    names.update(inner.names)
                elif isinstance(inner, ast.Name) and inner.id == 'globals':
                    names.add(None)
            return
        if isinstance(node, ast.Lambda):
            return
        if isinstance(node, ast.Import):
            names.update(alias.asname or alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(None if alias.name == '*' else alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Name) and (isinstance(node.ctx, ast.Store) or node.id == 'globals'):
            names.add(node.id if isinstance(node.ctx, ast.Store) else None)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(path.read_bytes(), str(path)))
    cache[path] = None if None in names or '__getattr__' in names else names
    return cache[path]


def provides(parts, name):
    """Whether `from <parts> import name` finds name: a submodule, or a name
    the module binds. A regular package is its __init__.py, a namespace
    package has only submodules, and an extension module is not parsed."""
    if located(parts + (name,)):
        return True
    path = root.joinpath(*parts)
    if (path / '__init__.py').is_file():
        names = bound(path / '__init__.py')
    elif path.with_name(path.name + '.py').is_file():
        names = bound(path.with_name(path.name + '.py'))
    elif path.is_dir():
        return False
    else:
        return True
    return names is None or name in names


def dependency(module, name, written):
    """The leaves of `import module` or `from module import *` (name None)
    or of `from module import name`, written as in the source.
    Dotted imports keep their full path: a present parent package does not
    stand in for a missing submodule."""
    top = module.split('.')[0]
    if top in EMBEDDED:
        return []
    if top in sys.builtin_module_names:
        # A built-in module always loads; its names still count.
        return [('external', spec(module, name))] if module != top or name else []
    if internal(top):
        return [('internal', tuple(module.split('.')), name, written)]
    return [('external', spec(module, name))]


def leaves(node):
    if node[0] == 'all':
        return [leaf for item in node[1] for leaf in leaves(item)]
    if node[0] == 'try':
        return leaves(node[1]) + [leaf for _, h in node[2] for leaf in leaves(h)] + leaves(node[3])
    return [node]


def load(names):
    """Import external dependencies in a clean interpreter, as the MGR does,
    and return each failure by spec as (exception name, report), with the
    report of a crashed probe. `from module import name` passes when name is
    an attribute of module or an importable submodule, as the import
    statement resolves it, and otherwise raises ImportError as it does."""
    if not names:
        return {}, None
    program = ('import importlib, sys\n'
               'for n in sys.argv[1:]:\n'
               '    module, _, name = n.partition(":")\n'
               '    try:\n'
               '        loaded = importlib.import_module(module)\n'
               '        if name and not hasattr(loaded, name):\n'
               '            try:\n'
               '                importlib.import_module(module + "." + name)\n'
               '            except ModuleNotFoundError as e:\n'
               '                if e.name != module + "." + name:\n'
               '                    raise\n'
               '                raise ImportError("cannot import name %r from %r" % (name, module)) from None\n'
               '    except Exception as e:\n'
               '        shown = "from %s import %s" % (module, name) if name else module\n'
               '        print("%s\\t%s\\t%s: %s: %s" % (n, type(e).__name__, shown, type(e).__name__,\n'
               '                                       str(e).splitlines()[0] if str(e) else ""))\n')
    output = subprocess.run([sys.executable, '-I', '-c', program] + sorted(names), cwd='/',
                            capture_output=True, text=True, timeout=120)
    failures = {}
    for line in output.stdout.splitlines():
        name, kind, report = line.split('\t', 2)
        failures[name] = (kind, report)
    crash = None
    if output.returncode:
        crash = 'dependency probe exited %d: %s' % (output.returncode, output.stderr.strip()[-500:])
    return failures, crash


def unique(items):
    seen = set()
    return [item for item in items if not (item in seen or seen.add(item))]


class Closure:
    """The load failures of internal names with their import closures,
    each an ordered list of (exception name, report): the first is the one
    the import raises. A try loads when its body loads and then its else,
    or when the first handler that catches the body's failure loads, so a
    fallback needs one branch that loads, and a handler that only records a
    missing optional import needs nothing."""

    def __init__(self, names):
        self.trees, todo, external = {}, list(names), set()
        while todo:
            top = todo.pop()
            if top in self.trees:
                continue
            self.trees[top] = [requirements(path) for path in sources(root, top)]
            for leaf in (leaf for tree in self.trees[top] for leaf in leaves(tree)):
                if leaf[0] == 'external':
                    external.add(leaf[1])
                elif leaf[0] == 'internal':
                    todo.append(leaf[1][0])
        self.loaded, self.crash = load(external)
        # Internal names import each other in cycles: iterate to a fixed point.
        self.failures = dict.fromkeys(self.trees, [])
        for _ in range(len(self.trees) + 1):
            failures = {top: unique(problem for tree in trees for problem in self.evaluate(tree))
                        for top, trees in self.trees.items()}
            if failures == self.failures:
                break
            self.failures = failures

    def evaluate(self, node):
        if node[0] == 'all':
            return [problem for item in node[1] for problem in self.evaluate(item)]
        if node[0] == 'try':
            _, body, handlers, orelse = node
            failed = self.evaluate(body)
            if not failed:
                return self.evaluate(orelse)
            catching = CATCHES.get(failed[0][0], {'Exception', 'BaseException'})
            for names, handler in handlers:
                if names & catching:
                    fallback = self.evaluate(handler)
                    return fallback + failed if fallback else []
            return failed
        if node[0] == 'external':
            return [self.loaded[node[1]]] if node[1] in self.loaded else []
        if node[0] == 'failure':
            return [node[1:]]
        _, parts, name, written = node
        # The top-level internal name runs first, then its submodule is found.
        problems = list(self.failures.get(parts[0], ()))
        dotted = '.'.join(parts)
        if not located(parts):
            problems.append(('ModuleNotFoundError', '%s: internal module %s not found under %s'
                             % (written, dotted, root)))
        elif name and not provides(parts, name):
            problems.append(('ImportError', '%s: internal name %s.%s not found under %s'
                             % (written, dotted, name, root)))
        return problems

    def problems(self, name):
        found = sorted(report for _, report in self.failures[name])
        return found + [self.crash] if self.crash else found


root, release, requested = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
present = bool(sys.argv[1]) and (root / 'mgr_module.py').is_file()
result('mgr-module-path', present, 'missing_file',
       '' if present else 'mgr_module_path %r has no mgr_module.py' % sys.argv[1])
always_on = ALWAYS_ON.get(release.split('.')[0], None)
result('mgr-baseline-list', always_on is not None, 'quick_failure',
       '' if always_on is not None else 'no recorded always-on MGR modules for release %r' % release)
invalid = [name for name in requested if not NAME.match(name)]
result('mgr-module-names', not invalid, 'quick_failure',
       '' if not invalid else 'invalid MGR module names: ' + ', '.join(invalid))
if present:
    selected = sorted(set(always_on or ()) | set(BASELINE) | {n for n in requested if NAME.match(n)})
    closure = Closure(['mgr_module'] + [name for name in selected if (root / name / 'module.py').is_file()])
    problems = closure.problems('mgr_module')
    result('mgr-module-load:mgr_module', not problems, 'loading_failure', '\n'.join(problems))
    for name in selected:
        exists = (root / name / 'module.py').is_file()
        result('mgr-module:' + name, exists, 'missing_file',
               '' if exists else 'MGR module %s has no %s' % (name, root / name / 'module.py'))
        if not exists:
            continue
        problems = closure.problems(name)
        result('mgr-module-load:' + name, not problems, 'loading_failure',
               '\n'.join('MGR module %s dependency %s' % (name, line) for line in problems))
        for asset in ASSETS.get(name, ()):
            path = root / name / asset
            ok = path.is_dir() and any(path.iterdir())
            result('mgr-module-assets:' + name, ok, 'missing_file',
                   '' if ok else 'MGR module %s has no %s' % (name, path))
    if 'cephadm' in requested:
        # The cephadm module reads the executable it deploys at load time.
        option = subprocess.run(['ceph-mgr', '--show-config-value', 'cephadm_path'],
                                capture_output=True, text=True).stdout.strip()
        ok = bool(option) and os.path.isfile(option) and os.access(option, os.R_OK)
        result('mgr-module-assets:cephadm', ok, 'missing_file',
               '' if ok else 'cephadm_path %r is not a readable file' % option)
sys.exit(1 if failed else 0)
MGR
    fi
    binaries=""
    if "$cache"; then binaries="$binaries rbd-mirror ceph-immutable-object-cache"; fi
    if "$fsmirror"; then binaries="$binaries cephfs-mirror"; fi
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
    check version:ceph-bluestore-tool version ceph-bluestore-tool
    class_dir=$(ceph-osd --show-config-value osd_class_dir) || failed=1
    plugin_dir=$(ceph-osd --show-config-value plugin_dir) || failed=1
    class_libc() {
        # Minimal roles need not contain ldd or getconf. Ask the installed ELF
        # interpreter itself; an unknown or ambiguous libc must fail closed.
        libc=
        for loader in /lib/ld-musl-*.so.1 /lib64/ld-musl-*.so.1 \
            /lib/ld-linux*.so.* /lib64/ld-linux*.so.*; do
            test -x "$loader" || continue
            LC_ALL=C "$loader" --version > "$work/libc-version" 2>&1 || :
            detected=$(awk '
                /GNU C Library|GNU libc|GLIBC/ {glibc=1}
                /musl libc/ {musl=1}
                END {if (glibc && !musl) print "glibc";
                     else if (musl && !glibc) print "musl"}' "$work/libc-version")
            test -n "$detected" || return 1
            test -z "$libc" || test "$libc" = "$detected" || return 1
            libc=$detected
        done
        test -n "$libc" || return 1
        printf '%s\n' "$libc"
    }
    class_relocate() {
        library=$1
        case "$library" in /*) ;; *) return 1 ;; esac
        libc=$(class_libc) || return 1
        status=0
        if test "$libc" = glibc; then
            # Like glibc ldd -r: check eager data/function ELF relocation in
            # the OSD export scope, without pre-main C++ constructors. This
            # intentionally does not execute constructors or IFUNC resolvers;
            # actual class methods belong to the functional scenarios.
            LC_ALL=C LD_TRACE_LOADED_OBJECTS=1 LD_WARN=1 LD_BIND_NOW=1 \
                LD_DEBUG= LD_PROFILE= LD_AUDIT= LD_BIND_NOT= \
                LD_PRELOAD="$library" ceph-osd --version \
                > "$work/class-version" 2> "$work/class-error" || status=$?
        elif test "$libc" = musl; then
            # Preserve musl's existing native preload check.
            LC_ALL=C LD_BIND_NOW=1 LD_PRELOAD="$library" ceph-osd --version \
                > "$work/class-version" 2> "$work/class-error" || status=$?
        else
            return 1
        fi
        cat "$work/class-version" "$work/class-error"
        test "$status" -eq 0 || return "$status"
        # Trace warnings (including unresolved symbols) can still exit zero.
        # Require the requested object in the trace: ignored/unsupported trace
        # mode must never turn a successful --version into a passing check.
        awk -v library="$library" -v libc="$libc" '
            BEGIN {failed=0; loaded=0}
            $1 == library && $2 ~ /^\(0x[[:xdigit:]]+\)$/ {loaded=1}
            /cannot be preloaded|cannot open shared object|wrong ELF|invalid ELF|undefined symbol|ERROR: ld.so|Error loading|Error relocating|not found|symbol lookup error|relocation error|unsupported relocation|different size in shared object/ {failed=1}
            END {exit (failed || (libc == "glibc" && !loaded))}
            ' "$work/class-version" "$work/class-error"
    }
    class_load() {
        for library in "$class_dir/libcls_$1.so"*; do
            if test -f "$library"; then
                class_relocate "$library"
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
if "$fstools"; then
    check version:cephfs-journal-tool version cephfs-journal-tool
fi
printf 'COMPLETE\t%s\n' "$role"
exit "$failed"
