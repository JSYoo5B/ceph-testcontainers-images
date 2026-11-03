import copy
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ci
import ci_command

def fixture(command='/ci run image-full on all'):
    repo={'full_name':'owner/repo'}
    event={'action':'created','repository':repo,'issue':{'number':7,'pull_request':{'url':'https://api.github.com/repos/owner/repo/pulls/7'}},'comment':{'id':123,'author_association':'OWNER','user':{'type':'User'},'body':command}}
    pull={'number':7,'state':'open','base':{'repo':repo},'head':{'sha':'a'*40,'repo':repo}}
    return event,pull

class ImageCommandTests(unittest.TestCase):
    def test_quick_is_one_native_default_cohort(self):
        result=ci_command.select(*fixture('/ci run image-quick'))
        self.assertEqual(result['sha'],'a'*40)
        self.assertEqual(result['matrix']['include'],[{'release':'20.2.4','variant':'official','architecture':'amd64','scope':'quick','runner':'ubuntu-24.04'}])

    def test_full_all_selects_every_available_catalogued_cohort(self):
        result=ci_command.select(*fixture())
        self.assertEqual(len(result['matrix']['include']),79)
        self.assertEqual(len({(c['release'],c['variant'],c['architecture']) for c in result['matrix']['include']}),79)
        self.assertEqual(len(ci_command.select(*fixture('/ci run image-full on 19.2.5'))['matrix']['include']),6)
        # The catalogue has no usable Ubuntu 19.2.6 arm64 input.
        cells=ci_command.select(*fixture('/ci run image-full on 19.2.6'))['matrix']['include']
        self.assertEqual(len(cells),5)
        self.assertNotIn(('ubuntu','arm64'),{(c['variant'],c['architecture']) for c in cells})
        self.assertEqual(len(ci_command.select(*fixture('/ci run image-quick on all'))['matrix']['include']),14)

    def test_unauthorized_forks_closed_and_bad_heads_are_refused(self):
        for kind in ('author','fork','closed','sha','number'):
            e,p=fixture()
            if kind=='author':e['comment']['author_association']='NONE'
            if kind=='fork':p['head']['repo']={'full_name':'fork/repo'}
            if kind=='closed':p['state']='closed'
            if kind=='sha':p['head']['sha']='main; bad'
            if kind=='number':p['number']=9
            with self.subTest(kind=kind),self.assertRaises(ValueError):ci_command.select(e,p)

    def test_unknown_partial_or_publication_requests_do_not_execute(self):
        for command in ('/ci run image-full on 21.0.0','/ci run publish','/ci run image-full --publish','/ci run image-quick on','/ci run image-full\n/ci run image-quick'):
            with self.subTest(command=command),self.assertRaises(ValueError):ci_command.select(*fixture(command))

    def test_plain_quoted_fenced_bot_and_edited_comments_are_ignored(self):
        for command in ('ordinary review','> /ci run image-full','```sh\n/ci run image-full\n```'):
            self.assertIsNone(ci_command.select(*fixture(command)))
        for kind in ('bot','edited','issue'):
            e,p=fixture()
            if kind=='bot':e['comment']['user']['type']='Bot'
            if kind=='edited':e['action']='edited'
            if kind=='issue':e['issue'].pop('pull_request')
            self.assertIsNone(ci_command.select(e,p))

    def test_checks_have_no_publication_command_and_full_preserves_both_layouts(self):
        quick,none=ci.commands('20.2.4','official','amd64','quick',Path('output'))
        gate,functional=ci.commands('20.2.4','official','amd64','full',Path('output'))
        self.assertEqual((len(quick),none),(1,{}))
        # The full scope gates on the build and the image requirement check.
        self.assertEqual(len(gate),2)
        self.assertEqual(gate[0],quick[0])
        self.assertNotIn('--full',gate[1])
        self.assertIn('linux/amd64',gate[1])
        self.assertIn(str(Path('output')/'quick'),gate[1])
        self.assertIn('all=ceph-testcontainers:official-20.2.4-all',gate[1])
        self.assertEqual(set(functional),{'full','rgw-only'})
        self.assertIn('all=ceph-testcontainers:official-20.2.4-all',functional['full'])
        self.assertIn('rgw-multisite',functional['rgw-only'])
        self.assertFalse(any('client=' in item for item in functional['rgw-only']))
        self.assertFalse(any('publish' in item for cmd in (*gate,*functional.values()) for item in cmd))


class FunctionalAuxiliaryTests(unittest.TestCase):
    """Functional scenarios are recorded, never a gate."""

    def run_full(self, failing=(), raising=(), unstartable=()):
        import json, tempfile
        from types import SimpleNamespace
        calls=[]
        def execute(command, check):
            calls.append((command, check))
            output=Path(command[command.index('--output-dir')+1]) if '--output-dir' in command else None
            name=output.name if output else 'build'
            if name in raising:
                raise ci.subprocess.CalledProcessError(1, command)
            if name in unstartable:
                raise OSError('cannot start')
            if output and '--full' in command:
                output.mkdir(parents=True)
                status='failed' if name in failing else 'passed'
                report={'status':status,'level':'full','scenarios':status,'functional':{
                    'mixed':{'status':status,'images':{'essential':'sha256:1'},
                             'scenarios':{'rgw-multisite':{'status':status},'cleanup':{'status':'passed'}}}}}
                (output/'check-report.json').write_text(json.dumps(report))
            return SimpleNamespace(returncode=1 if name in failing else 0)
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()),mock.patch('sys.stderr'):
                code=ci.main(['--release','20.2.4','--variant','official','--architecture','amd64',
                              '--scope','full','--output',directory],probe=lambda:'',execute=execute)
            summary_path=Path(directory)/'functional-summary.json'
            summary=json.loads(summary_path.read_text()) if summary_path.exists() else None
        return code,summary,calls

    def test_full_scope_records_functional_failures_without_failing(self):
        code,summary,calls=self.run_full(failing=('rgw-only',))
        self.assertEqual(code,0)
        self.assertEqual([check for _,check in calls],[True,True,False,False])
        self.assertEqual((summary['status'],summary['gating']),('failed',False))
        rgw=summary['runs']['rgw-only']
        self.assertEqual((rgw['exit_status'],rgw['status'],rgw['outcome']),(1,'failed','failed'))
        self.assertEqual(rgw['topologies']['mixed']['scenarios']['rgw-multisite'],'failed')
        self.assertEqual(len(rgw['report_sha256']),64)
        self.assertEqual(summary['runs']['full']['outcome'],'passed')
        code,summary,_=self.run_full()
        self.assertEqual((code,summary['status']),(0,'passed'))

    def test_a_functional_run_without_a_report_is_recorded(self):
        code,summary,_=self.run_full(unstartable=('full',))
        self.assertEqual((code,summary['status']),(0,'failed'))
        record=summary['runs']['full']
        self.assertEqual((record['status'],record['outcome'],record['exit_status']),('no_report','failed',None))

    def test_build_and_quick_check_failures_still_fail(self):
        for gate in ('build','quick'):
            with self.subTest(gate=gate),self.assertRaises(ci.subprocess.CalledProcessError):
                self.run_full(raising=(gate,))


class CohortPreflightTests(unittest.TestCase):
    def run_ci(self, release, variant, architecture, levels):
        import json, tempfile
        from types import SimpleNamespace
        calls = []
        def probe():
            if isinstance(levels, Exception):
                raise levels
            return levels
        with tempfile.TemporaryDirectory() as directory:
            code = ci.main(['--release', release, '--variant', variant, '--architecture', architecture,
                            '--scope', 'quick', '--output', directory],
                           probe=probe, execute=lambda command, check: calls.append(command) or SimpleNamespace(returncode=0))
            record = json.loads((Path(directory) / 'preflight.json').read_text())
        return code, record, calls

    def test_umbrella_official_amd64_needs_x86_64_v3_before_any_build(self):
        v2 = '  x86-64-v2 (supported, searched)\n  x86-64-v3\n'
        v3 = '  x86-64-v3 (supported, searched)\n  x86-64-v2 (supported, searched)\n'
        for levels in (v2, OSError('no loader')):
            with self.subTest(levels=levels):
                code, record, calls = self.run_ci('21.2.0', 'official', 'amd64', levels)
                self.assertEqual((code, record['status'], calls), (ci.INELIGIBLE, 'environment_ineligible', []))
        code, record, calls = self.run_ci('21.2.0', 'official', 'amd64', v3)
        self.assertEqual((code, record['status'], len(calls)), (0, 'passed', 1))
        # Other cohorts never consult the CPU probe.
        for release, variant, architecture in (('21.2.0', 'official', 'arm64'), ('21.2.0', 'debian', 'amd64'),
                                               ('20.2.4', 'official', 'amd64')):
            code, record, calls = self.run_ci(release, variant, architecture, OSError('not consulted'))
            self.assertEqual((code, record['status'], len(calls)), (0, 'passed', 1))

    def test_unavailable_cohorts_are_refused_before_any_build(self):
        for release, variant in (('19.2.0', 'debian'), ('19.2.6', 'ubuntu')):
            with self.subTest(release=release):
                code, record, calls = self.run_ci(release, variant, 'arm64', '')
                self.assertEqual((code, record['status'], calls), (2, 'unavailable', []))
                self.assertIn('unavailable', record['reason'])
