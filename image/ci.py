"""Build one native image cohort and run the requested checks.

The cohort must be available in the release catalogue, and the host must run
its source executables: a cohort that needs a newer x86-64 level than the CPU
offers is refused as environment_ineligible (exit 3) before any build,
distinct from a build or check failure. preflight.json records the decision.

The build and the image requirement (quick) check are the gate: a failure in
either fails the command. The full scope then runs the functional scenarios as
an auxiliary indicator. Their outcome is written to functional-summary.json and
never fails the command.
"""
import argparse
import hashlib
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


# Single-cluster scenarios that exercise the baseline MGR modules (cluster
# health, rbd_support and volumes) with the MGRs run from mgr-modules.
MGR_MODULES_SCENARIOS = ('cluster-lifecycle', 'rbd', 'cephfs')


def packaged_mgr_modules(output):
    """The MGR module inventory of the built mgr-modules image, from its plan."""
    plan = json.loads((output/'build'/'plan.json').read_text())
    modules = plan['roles'].get('mgr-modules', {}).get('mgr_modules')
    if not modules:
        raise RuntimeError('The build recorded no mgr-modules inventory')
    return modules


def commands(release, variant, architecture, scope, output, modules=()):
    """The gate commands, which must succeed, and the functional commands by
    name, which are only recorded. modules is the packaged MGR inventory that
    the requirement check of the mgr-modules image must require."""
    build = ['make', 'role-images', 'VARIANT='+variant, 'CEPH_RELEASE='+release,
             'PLATFORM=linux/'+architecture, 'ROLE_OUTPUT='+str(output/'build')]
    if scope != 'full':
        return [build], {}
    roles = releases.roles(variant)
    tags = {role: f'ceph-testcontainers:{variant}-{release}-{role}' for role in roles}
    images = [item for role, reference in tags.items() for item in ('--image', role+'='+reference)]
    requested = [item for module in modules for item in ('--mgr-module', module)]
    quick = [sys.executable, 'image/check.py', '--platform', 'linux/'+architecture,
             '--output-dir', str(output/'quick')] + images + requested
    baseline = [item for role in releases.ROLES for item in ('--image', role+'='+tags[role])]
    functional = {
        'full': [sys.executable, 'image/check.py', '--full', '--output-dir', str(output/'full')] + baseline,
        'rgw-only': [sys.executable, 'image/check.py', '--full', '--scenario', 'rgw-multisite',
                     '--image', 'essential='+tags['essential'], '--image', 'rgw='+tags['rgw'],
                     '--output-dir', str(output/'rgw-only')]}
    if 'mgr-modules' in tags:
        functional['mgr-modules'] = [
            sys.executable, 'image/check.py', '--full', '--topology', 'mgr-modules',
            *[item for name in MGR_MODULES_SCENARIOS for item in ('--scenario', name)],
            *[item for role in ('essential', 'client', 'cephfs', 'mgr-modules')
              for item in ('--image', role+'='+tags[role])],
            '--output-dir', str(output/'mgr-modules')]
    return [build, quick], functional


def functional_run(name, command, returncode, output):
    """One functional run as recorded: its exit status and, when its report
    exists, the report's digest and per-topology and per-scenario statuses."""
    record = {'command': command, 'exit_status': returncode, 'report': name+'/check-report.json'}
    path = output/name/'check-report.json'
    try:
        data = path.read_bytes()
        report = json.loads(data)
        record.update(report_sha256=hashlib.sha256(data).hexdigest(), status=report.get('status'),
                      level=report.get('level'), scenarios=report.get('scenarios'), topologies={
                          topology: {'status': result.get('status'), 'images': result.get('images'),
                                     'scenarios': {scenario: value.get('status')
                                                   for scenario, value in result.get('scenarios', {}).items()}}
                          for topology, result in report.get('functional', {}).items()})
        if report.get('error'):
            record['error'] = report['error']
    except (OSError, ValueError, AttributeError, TypeError) as error:
        record.update(status='no_report', error=str(error))
    record['outcome'] = 'passed' if returncode == 0 and record.get('status') == 'passed' else 'failed'
    return record


def run_functional(functional, output, execute):
    runs = {}
    for name, command in functional.items():
        try:
            returncode = execute(command, check=False).returncode
        except OSError as error:
            print('Functional run ' + name + ' could not start: ' + str(error), file=sys.stderr)
            returncode = None
        runs[name] = functional_run(name, command, returncode, output)
    failed = [name for name, run in runs.items() if run['outcome'] != 'passed']
    summary = {'schema': 1, 'status': 'failed' if failed else 'passed', 'gating': False, 'runs': runs}
    (output/'functional-summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True) + '\n')
    if failed:
        # An annotation in GitHub Actions; the result does not block publication.
        print('::warning::Auxiliary functional scenarios failed (not a publication gate): ' + ', '.join(failed))
    return summary


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
    gate, functional = commands(args.release, args.variant, args.architecture, args.scope, args.output)
    execute(gate[0], check=True)
    if len(gate) > 1:
        # The requirement check needs the inventory that the build packaged.
        modules = packaged_mgr_modules(args.output) if 'mgr-modules' in releases.roles(args.variant) else ()
        gate, functional = commands(args.release, args.variant, args.architecture, args.scope, args.output, modules)
        for command in gate[1:]:
            execute(command, check=True)
    if functional:
        run_functional(functional, args.output, execute)
    return 0


if __name__ == '__main__':
    sys.exit(main())
