"""Build one native image cohort and run the requested checks."""
import argparse
from pathlib import Path
import subprocess
import sys

import releases


def commands(release, variant, architecture, scope, output):
    build = ['make', 'role-images', 'VARIANT='+variant, 'CEPH_RELEASE='+release,
             'PLATFORM=linux/'+architecture, 'ROLE_OUTPUT='+str(output/'build')]
    result = [build]
    if scope == 'full':
        tags = {role: f'ceph-testcontainers:{variant}-{release}-{role}'
                for role in ('essential', 'client', 'cephfs', 'rbd-aux', 'rgw', 'all')}
        full = [sys.executable, 'image/check.py', '--full', '--output-dir', str(output/'full')]
        for role, reference in tags.items():
            full += ['--image', role+'='+reference]
        result += [full, [sys.executable, 'image/check.py', '--full', '--scenario', 'rgw-multisite',
                         '--image', 'essential='+tags['essential'], '--image', 'rgw='+tags['rgw'],
                         '--output-dir', str(output/'rgw-only')]]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', choices=tuple(releases.RELEASES), required=True)
    parser.add_argument('--variant', choices=('official', 'debian', 'ubuntu'), required=True)
    parser.add_argument('--architecture', choices=('amd64', 'arm64'), required=True)
    parser.add_argument('--scope', choices=('quick', 'full'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for command in commands(args.release, args.variant, args.architecture, args.scope, args.output):
        subprocess.run(command, check=True)


if __name__ == '__main__':
    main()
