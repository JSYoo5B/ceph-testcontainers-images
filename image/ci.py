"""Build one native image cohort and run the requested checks.

The cohort must be available in the release catalogue, and the host must run
its source executables: a cohort that needs a newer x86-64 level than the CPU
offers is refused as environment_ineligible (exit 3) before any build,
distinct from a build or check failure. preflight.json records the decision.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import releases

INELIGIBLE = 3
LOADER = '/lib64/ld-linux-x86-64.so.2'


class Ineligible(RuntimeError):
    pass


def loader_levels():
    """The glibc loader lists the x86-64 levels this CPU supports."""
    return subprocess.run([LOADER, '--help'], capture_output=True, text=True, check=True, timeout=60).stdout


def preflight(release, variant, architecture, probe=loader_levels):
    values = releases.release(release)[variant]
    if architecture not in values['architectures']:
        raise ValueError('The catalogue marks this cohort unavailable: ' + values['unavailable'][architecture])
    level = releases.cpu_baseline(release, variant, architecture)
    if level:
        try:
            supported = level + ' (supported, searched)' in probe()
        except (OSError, subprocess.SubprocessError) as error:
            raise Ineligible('Cannot establish ' + level + ' CPU support: ' + str(error)) from error
        if not supported:
            raise Ineligible(variant + ' ' + release + ' ' + architecture + ' requires an ' + level + ' CPU')


def commands(release, variant, architecture, scope, output):
    build = ['make', 'role-images', 'VARIANT='+variant, 'CEPH_RELEASE='+release,
             'PLATFORM=linux/'+architecture, 'ROLE_OUTPUT='+str(output/'build')]
    result = [build]
    if scope == 'full':
        tags = {role: f'ceph-testcontainers:{variant}-{release}-{role}' for role in releases.ROLES}
        full = [sys.executable, 'image/check.py', '--full', '--output-dir', str(output/'full')]
        for role, reference in tags.items():
            full += ['--image', role+'='+reference]
        result += [full, [sys.executable, 'image/check.py', '--full', '--scenario', 'rgw-multisite',
                         '--image', 'essential='+tags['essential'], '--image', 'rgw='+tags['rgw'],
                         '--output-dir', str(output/'rgw-only')]]
    return result


def main(argv=None, probe=loader_levels, execute=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', choices=tuple(releases.RELEASES), required=True)
    parser.add_argument('--variant', choices=releases.VARIANTS, required=True)
    parser.add_argument('--architecture', choices=releases.ARCHITECTURES, required=True)
    parser.add_argument('--scope', choices=('quick', 'full'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    record = {'release': args.release, 'variant': args.variant, 'architecture': args.architecture}
    try:
        preflight(args.release, args.variant, args.architecture, probe)
    except Ineligible as error:
        record.update(status='environment_ineligible', reason=str(error))
        (args.output/'preflight.json').write_text(json.dumps(record, indent=2) + '\n')
        print('Environment ineligible: ' + str(error), file=sys.stderr)
        return INELIGIBLE
    except ValueError as error:
        record.update(status='unavailable', reason=str(error))
        (args.output/'preflight.json').write_text(json.dumps(record, indent=2) + '\n')
        print(error, file=sys.stderr)
        return 2
    record['status'] = 'passed'
    (args.output/'preflight.json').write_text(json.dumps(record, indent=2) + '\n')
    for command in commands(args.release, args.variant, args.architecture, args.scope, args.output):
        execute(command, check=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
