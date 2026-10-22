import copy
from pathlib import Path
import sys
import unittest
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

    def test_full_all_retains_twelve_cohorts(self):
        result=ci_command.select(*fixture())
        self.assertEqual(len(result['matrix']['include']),12)
        self.assertEqual(len({(c['release'],c['variant'],c['architecture']) for c in result['matrix']['include']}),12)
        self.assertEqual(len(ci_command.select(*fixture('/ci run image-full on 19.2.5'))['matrix']['include']),6)

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
        quick=ci.commands('20.2.4','official','amd64','quick',Path('output'))
        full=ci.commands('20.2.4','official','amd64','full',Path('output'))
        self.assertEqual(len(quick),1)
        self.assertEqual(len(full),3)
        self.assertEqual(full[0],quick[0])
        self.assertIn('all=ceph-testcontainers:official-20.2.4-all',full[1])
        self.assertIn('rgw-multisite',full[2])
        self.assertFalse(any('client=' in item for item in full[2]))
        self.assertFalse(any('publish' in item for cmd in full for item in cmd))
