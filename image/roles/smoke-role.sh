#!/bin/sh
set -eu
python3 - "$1" <<'PY'
import json
import os
from pathlib import Path
import shutil
import stat
import sys

root = Path('/usr/share/ceph-testcontainers')
m = json.loads((root/'image-manifest.json').read_text())
assert m['role'] == sys.argv[1]
assert m['contract'] == 'roles'
assert m['source_image'] in (root/'SOURCES.txt').read_text()
assert 'source-packages.txt' in (root/'SOURCES.txt').read_text()
assert (root/'runtime-packages.txt').read_text().splitlines()[1:] == m['package_versions']
assert len([s for s in (root/'source-packages.txt').read_text().splitlines() if s and not s.startswith('#')]) == len(m['package_versions'])
for executable in m['excluded_executables']:
    assert shutil.which(executable) is None, 'excluded executable reintroduced: '+executable
for executable in m['required_executables']:
    assert os.access(executable, os.X_OK), executable
for path, expected in m['metadata_probes'].items():
    info = os.lstat(path)
    assert (info.st_uid, info.st_gid, '%04o' % stat.S_IMODE(info.st_mode)) == (expected['uid'],expected['gid'],expected['mode']), path
    if stat.S_ISLNK(info.st_mode):
        assert os.readlink(path) == expected['link_target'], path
print('roles source, executable and runtime metadata checks passed')
PY
