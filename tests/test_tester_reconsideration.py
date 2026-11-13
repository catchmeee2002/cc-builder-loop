"""Original author reconsideration through actual CLI, Git and result application."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from harness import ROOT, commit_all, git, run_cli
from tests.helpers import prerequisite_recovery as fixtures
from runtime.codex_builder_loop.assurance_v4 import core, driver, review_recovery
from runtime.codex_builder_loop.assurance_v4.role_input import role_context
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator


class TesterReconsiderationTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PrerequisiteFixture()
        self.f.setUp(initial_files=['src/public_a.py', 'src/public_b.py'])
        self.addCleanup(self.f.tearDown)
        f = self.f
        core.scan_doc_references(f.repo, f.run_id)
        core.publish_prerequisites(f.repo, f.run_id)
        core.prepare_tester(f.repo, f.run_id, 'tester', 'tester-thread')
        self.original = f.begin('tester')
        self.tester = Path(self.ledger()['facets']['execution']['tester_source']['worktree'])
        (self.tester/'tests/test_partial.py').write_text('def test_partial():\n    assert True\n')
        self.head = commit_all(self.tester, 'test(fixture): [cr_id_skip] Preserve author draft')
        self.problem = {'key': 'input-unavailable', 'owner': 'builder_loop', 'summary': 'Input unavailable',
                        'details': 'Cannot read complete author input in current context.'}
        self.blocked_result = {'result': 'blocked', 'evidence_report': None, 'proof_spec': None,
                              'problem_report': {'schema_version': 1, 'problems': [self.problem]}}
        core.complete_dispatch(f.repo, f.run_id, action_id=self.original['action_id'], result_value=self.blocked_result)
        core.apply_dispatch_result(f.repo, f.run_id, action_id=self.original['action_id'], driver_runtime_kind='native')
        self.decision = driver.next_action(f.repo, f.run_id)
        self.assertEqual(self.decision['action'], 'builder_loop_problem_decision')

    def ledger(self):
        return read_ledger(self.f.repo, self.f.run_id)

    def request(self, **changes):
        values = dict(session_id='prerequisite-session', action_id=self.decision['action_id'],
                      problem_key='input-unavailable', reason='Re-read complete public input', driver_runtime_kind='native')
        values.update(changes)
        args = [x for k,v in values.items() for x in ('--'+k.replace('_','-'), v)]
        return run_cli('assurance', '--experimental-v4', 'reconsider-tester-problem',
                       '--repo', self.f.repo, '--run', self.f.run_id, *args)

    def result(self, decision='withdrawn', other=False):
        request = review_recovery.active_request(self.ledger(), 'tester')
        blocked = decision == 'upheld' or other
        problems = [self.problem] if decision == 'upheld' else ([dict(self.problem,key='actual-prerequisite',owner='builder')] if other else [])
        return {'result': 'blocked' if blocked else 'tests_ready', 'proof_spec': None,
                'problem_report': {'schema_version':1,'problems':problems} if problems else None,
                'evidence_report': {'schema_version':1,'kind':'tester','status':'fail' if blocked else 'pass',
                  'candidate_head':self.head,'producer':{'role':'tester','agent_id':'tester','thread_id':'tester-thread'},
                  'details':{'result':'fail' if blocked else 'tests_ready', 'source_head':self.head,
                    'files':[{'path':'tests/test_partial.py','blob':git(self.tester,'rev-parse',self.head+':tests/test_partial.py')}],
                    'tester_problem_dispositions':[{'request_id':request['request_id'],'problem_key':'input-unavailable',
                        'decision':decision,'reason':'Read current frozen input in bounded sections.'}]}}}

    def complete(self, result):
        self.action = self.f.begin('tester')
        result = NativeCoordinator._normalize_action_result(self.action['action'], result)
        core.complete_dispatch(self.f.repo,self.f.run_id,action_id=self.action['action_id'],result_value=result)

    def apply(self):
        return core.apply_dispatch_result(self.f.repo,self.f.run_id,action_id=self.action['action_id'],driver_runtime_kind='native')

    def test_cli_request_keeps_problem_open_and_original_source_and_role_safe_input(self):
        before=self.ledger()
        response=self.request();self.assertEqual(response.returncode,0,response.stdout)
        after=self.ledger()
        self.assertEqual(after['facets'],before['facets']);self.assertEqual(after['evidence'],before['evidence'])
        self.assertEqual(after['problems'],before['problems'])
        self.assertEqual(git(self.tester,'rev-parse','HEAD'),self.head)
        self.assertEqual(self.request().returncode,0)
        action=driver.next_action(self.f.repo,self.f.run_id)
        self.assertEqual(action['agent']['thread_id'],'tester-thread')
        payload=role_context(self.f.repo,self.f.run_id,action['action_id'])
        coordinator = NativeCoordinator(repo=self.f.repo, run_id=self.f.run_id, core=object(), transport=object(), project_root=ROOT)
        prompt = json.loads(coordinator._prompt(action, 'tester', core.driver_context(self.f.repo,self.f.run_id)).split('\n',1)[1])
        self.assertIn('tester_reconsideration', prompt)
        self.assertIn('author_input_access', prompt)
        serialized=coordinator._prompt(action, 'tester', core.driver_context(self.f.repo,self.f.run_id))
        self.assertLess(serialized.index('author_input_access'),serialized.index('\"contract\"'))
        self.assertIn('tester_problem_disposition',prompt['result_field_contract']['evidence_report'])
        self.assertNotIn('snapshot',prompt['tester_reconsideration'])
        self.assertNotIn('candidate_worktree', prompt)
        self.assertIn('tester_reconsideration',payload)
        from runtime.codex_builder_loop.assurance_v4.models import digest
        self.assertEqual(payload['role_input_digest'],digest({k:v for k,v in payload.items() if k != 'role_input_digest'}))
        self.assertNotIn('snapshot',payload['tester_reconsideration'])
        self.assertNotIn('candidate_worktree',payload)
        case=role_context(self.f.repo,self.f.run_id,action['action_id'],'/contract/mission/acceptance_cases/0')
        self.assertEqual(case['value'],payload['contract']['mission']['acceptance_cases'][0])
        self.assertIn('$defs',role_context(self.f.repo,self.f.run_id,action['action_id'],'/evidence_report_schema')['value'])
        with self.assertRaises(core.AssuranceError): role_context(self.f.repo,self.f.run_id,self.original['action_id'])

    def test_withdrawal_and_new_blocker_does_not_integrate_or_pass(self):
        self.assertEqual(self.request().returncode,0)
        self.complete(self.result(other=True));self.apply();self.apply()
        ledger=self.ledger()
        self.assertEqual(next(p for p in ledger['problems'] if p['key']=='input-unavailable')['status'],'resolved')
        self.assertEqual(next(p for p in ledger['problems'] if p['key']=='actual-prerequisite')['status'],'open')
        self.assertNotIn('tester',ledger['evidence'])
        self.assertEqual(sum(e['kind']=='tester_problem_reconsidered' for e in ledger['events']),1)

    def test_upheld_report_stays_blocked_without_automatic_retry(self):
        self.assertEqual(self.request().returncode,0)
        self.complete(self.result('upheld'));self.apply()
        self.assertEqual(driver.next_action(self.f.repo,self.f.run_id)['action'],'builder_loop_problem_decision')
        self.assertIsNone(review_recovery.active_request(self.ledger(),'tester'))
        self.assertNotIn('tester',self.ledger()['evidence'])

    def test_withdrawal_integrates_only_normal_tests_ready_and_recovers_receipt(self):
        self.assertEqual(self.request().returncode,0)
        self.complete(self.result())
        save=core._store_save_ledger
        class Interrupted(BaseException): pass
        def interrupt(repo,ledger):
            save(repo,ledger)
            if any(e['kind']=='tester_problem_reconsidered' for e in ledger['events']): raise Interrupted()
        with patch.object(core,'_store_save_ledger',side_effect=interrupt):
            with self.assertRaises(Interrupted): self.apply()
        self.apply()
        ledger=self.ledger()
        self.assertEqual(ledger['evidence']['tester']['status'],'pass')
        self.assertNotIn('blackbox',ledger['evidence'])
        self.assertEqual(sum(e['kind']=='tester_problem_reconsidered' for e in ledger['events']),1)

    def test_wrong_owner_action_dirty_draft_and_invalid_disposition_rejected(self):
        before=self.ledger()
        for changes in ({'session_id':'wrong'},{'action_id':'f'*64},{'driver_runtime_kind':'full_driver_skill'}):
            self.assertNotEqual(self.request(**changes).returncode,0)
            self.assertEqual(self.ledger(),before)
        path=self.tester/'tests/test_partial.py';path.write_text('dirty\n')
        self.assertNotEqual(self.request().returncode,0);self.assertEqual(self.ledger(),before)
        path.write_text('def test_partial():\n    assert True\n')
        self.assertEqual(self.request().returncode,0)
        result=self.result();result['evidence_report']['details']['tester_problem_dispositions'][0]['request_id']='f'*64
        with self.assertRaises(core.ContractError): self.complete(result)

    def test_live_transport_and_changed_candidate_refuse_recovery(self):
        before=self.ledger()
        kwargs=dict(session_id='prerequisite-session',action_id=self.decision['action_id'],
                    problem_key='input-unavailable',reason='Read full input',driver_runtime_kind='native',role='tester')
        ledger=copy.deepcopy(before)
        ledger['driver_runtime']['native_transport']={'state':'running'}
        with patch.object(review_recovery,'read_ledger',return_value=ledger):
            with self.assertRaises(core.AssuranceError):
                review_recovery.request_reconsideration(self.f.repo,self.f.run_id,**kwargs)
        self.assertEqual(self.ledger(),before)
        (self.f.candidate/'src/public_a.py').write_text('changed\n')
        self.assertNotEqual(self.request().returncode,0)
        self.assertEqual(self.ledger(),before)

    def test_runtime_transition_requires_explicit_opt_in(self):
        from runtime.codex_builder_loop import core as runtime_core
        before=self.ledger()
        runtime=dict(runtime_core.capture_runtime_identity(),adapter_commit='f'*40,adapter_dirty=False)
        kwargs=dict(session_id='prerequisite-session',action_id=self.decision['action_id'],
                    problem_key='input-unavailable',reason='Read full input',driver_runtime_kind='native',role='tester')
        with patch.object(runtime_core,'capture_runtime_identity',return_value=runtime):
            with self.assertRaises(core.AssuranceError):
                review_recovery.request_reconsideration(self.f.repo,self.f.run_id,**kwargs)
            self.assertEqual(self.ledger(),before)
            review_recovery.request_reconsideration(self.f.repo,self.f.run_id,**kwargs,allow_runtime_transition=True)
        after=self.ledger()
        self.assertEqual(after['runtime_identity'],runtime)
        self.assertEqual(after['facets'],before['facets'])
        self.assertEqual(after['evidence'],before['evidence'])
        self.assertEqual(after['problems'],before['problems'])

    def test_unmodified_0425_blocked_author_migrates_without_rewriting_source(self):
        import contextlib
        import io
        import os
        import subprocess
        import sys
        import tempfile
        from harness import cleanup_repo
        from runtime.codex_builder_loop import core as runtime_core
        from runtime.codex_builder_loop.assurance_v4 import cli
        with tempfile.TemporaryDirectory(prefix='tester-old-runtime-') as raw:
            old=Path(raw)/'runtime'
            subprocess.run(['git','clone','--quiet','--shared','--no-checkout',str(ROOT),str(old)],check=True)
            subprocess.run(['git','-C',str(old),'checkout','--quiet','--detach','176e28ba794b011e99b4f648357e0f9a091f608e'],check=True)
            program="""
import importlib.util,json,sys
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/tests']
spec=importlib.util.spec_from_file_location('recovery_fixture',sys.argv[2])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
t=m.TesterReconsiderationTest();t.setUp()
print(json.dumps({'repo':str(t.f.repo),'run':t.f.run_id,'action_id':t.decision['action_id'],'source':str(t.tester),'head':t.head}))
"""
            result=subprocess.run([sys.executable,'-c',program,str(old),str(Path(__file__).resolve())],cwd=raw,
                                  env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stderr)
            info=json.loads(result.stdout.splitlines()[-1]);repo=Path(info['repo'])
            self.addCleanup(cleanup_repo,repo)
            before=read_ledger(repo,info['run'])
            self.assertEqual(before['runtime_identity']['builder_loop_version'],'0.4.25')
            identity=dict(runtime_core.capture_runtime_identity(),adapter_dirty=False)
            argv=['--experimental-v4','reconsider-tester-problem','--repo',str(repo),'--run',info['run'],
                  '--session-id','prerequisite-session','--action-id',info['action_id'],'--problem-key','input-unavailable',
                  '--reason','Read complete canonical input','--driver-runtime-kind','native','--allow-runtime-transition']
            with patch.object(runtime_core,'capture_runtime_identity',return_value=identity):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    rc=cli.main(argv)
                self.assertEqual(rc,0,output.getvalue())
            after=read_ledger(repo,info['run'])
            self.assertEqual(after['facets'],before['facets']);self.assertEqual(after['problems'],before['problems'])
            self.assertEqual(after['evidence'],before['evidence'])
            self.assertEqual(git(Path(info['source']),'rev-parse','HEAD'),info['head'])
            action=driver.next_action(repo,info['run'])
            self.assertEqual(action['action'],'tester_author')
            self.assertEqual(action['agent']['thread_id'],'tester-thread')

    def test_upheld_report_can_truthfully_have_no_authored_tests(self):
        self.assertEqual(self.request().returncode,0)
        self.action=self.f.begin('tester')
        (self.tester/'tests/test_partial.py').unlink()
        self.head=commit_all(self.tester,'test(fixture): [cr_id_skip] Remove incomplete draft')
        request=review_recovery.active_request(self.ledger(),'tester')
        result=copy.deepcopy(self.blocked_result)
        result['evidence_report']={'schema_version':1,'kind':'tester','status':'fail','candidate_head':self.head,
            'producer':{'role':'tester','agent_id':'tester','thread_id':'tester-thread'},
            'details':{'result':'fail','source_head':self.head,'files':[],
                'tester_problem_dispositions':[{'request_id':request['request_id'],'problem_key':'input-unavailable',
                    'decision':'upheld','reason':'Still cannot access required input; no completed tests.'}]}}
        from runtime.codex_builder_loop.assurance_v4.models import validate_evidence_report, ContractError
        invalid=copy.deepcopy(result['evidence_report']);invalid['status']='pass';invalid['details']['result']='tests_ready'
        with self.assertRaises(ContractError): validate_evidence_report(invalid)
        core.complete_dispatch(self.f.repo,self.f.run_id,action_id=self.action['action_id'],result_value=result)
        self.apply()
        self.assertEqual(driver.next_action(self.f.repo,self.f.run_id)['action'],'builder_loop_problem_decision')
        self.assertNotIn('tester',self.ledger()['evidence'])
