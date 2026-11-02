"""Restore only a verified Ceph COPYING omitted by a matching upstream build.

This supplements license metadata, never executables or package selection.
Both the host and the offline source container validate the exact build.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

CANDIDATES = ('/usr/share/doc/ceph/COPYING', '/usr/share/licenses/ceph/COPYING',
              '/usr/share/doc/ceph-common/copyright')


def load_record(release, version, directory):
    directory = Path(directory)
    mapping = json.loads((directory / 'ceph-licenses.json').read_text())
    if mapping.get('schema') != 1 or release not in mapping.get('releases', {}):
        raise ValueError('No verified Ceph license for release ' + release)
    record = mapping['releases'][release]
    commit = record.get('commit', '')
    if record.get('release') != release or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid Ceph license build identity')
    observed = re.match(r'^ceph version (\S+) \(([0-9a-f]{40})\)(?:\s|$)', version)
    if observed is None or observed.groups() != (release, commit):
        raise ValueError('Ceph license build differs from the observed release/commit')
    asset = PurePosixPath(record.get('asset', ''))
    if (asset.is_absolute() or '..' in asset.parts or len(asset.parts) != 2
            or asset.parts[0] != 'licenses'):
        raise ValueError('Ceph license asset must be a local licenses file')
    path = directory / str(asset)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Verified Ceph license asset is missing or indirect')
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != record.get('sha256'):
        raise ValueError('Ceph license asset hash differs from its verified record')
    if record.get('url') != 'https://raw.githubusercontent.com/ceph/ceph/' + commit + '/COPYING':
        raise ValueError('Ceph license source URL is not the immutable build source')
    return dict(record), content


def restore(release, version, directory, root=Path('/')):
    record, content = load_record(release, version, directory)
    root = Path(root)
    for candidate in CANDIDATES:
        if (root / candidate.lstrip('/')).is_file():
            return None
    target = root / CANDIDATES[0].lstrip('/')
    if target.is_symlink():
        raise ValueError('Refusing an indirect Ceph COPYING restoration target')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also refuses an unexpected concurrent license writer.
    with target.open('xb') as output:
        output.write(content)
    target.chmod(0o644)
    return {'schema': 1, 'operation': 'restore-missing-ceph-copying',
            'release': record['release'], 'commit': record['commit'],
            'sha256': record['sha256'], 'source_url': record['url'],
            'restored_path': CANDIDATES[0],
            'scope': 'License text only; no executable or package metadata changed'}
