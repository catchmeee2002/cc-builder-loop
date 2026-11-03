from __future__ import annotations
import copy
import json
import unittest
from pathlib import Path
from harness import init_repo, cleanup_repo, run_cli, git
from tests.test_assurance_v4_contract import contract_for
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.assurance_v4.models import digest
from runtime.codex_builder_loop.assurance_v4.problems import effective_problems


class ProjectAuthorityRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.run = 'project-repair'
        c = contract_for(self.repo)
        c['execution']['work_units'][0]['scope']['paths'] = ['src/calc.py']
        core.start(self.repo, self.run, 'owner', c, driver_runtime={
            'kind': 'native', 'protocol_version': 1, 'transport': 'codex_app_server',
            'runtime_version': 'fixture', 'protocol_schema_digest': 'a'*64})
        core.checkpoint_builder(self.repo, self.run)
        core.verify_machine(self.repo, self.run)
        core.record_problems(self.repo, self.run, {'schema_version': 1, 'problems': [{
            'key': 'fixture-scope', 'owner': 'current_project', 'summary': 'Fixture import prerequisite',
            'details': 'Repair regression/check.py imports, preserving assertions.'}]},
            role='tester', agent_id='assurance-v4-tester', thread_id='assurance-v4-tester-thread')

    def tearDown(self):
        cleanup_repo(self.repo)

    def replacement(self):
        r = copy.deepcopy(read_ledger(self.repo, self.run)['facets'])
        r['authority']['builder_write'].append('regression/check.py')
        return r

    def args(self, replacement=None):
        l=read_ledger(self.repo,self.run)
        return dict(session_id='owner', problem_key='fixture-scope',
                    action_id=driver.next_action(self.repo,self.run)['action_id'],
                    facet='authority', facet_digest=l['digests']['authority'],
                    replacement_contract=replacement or self.replacement())

    def apply_args(self):
        a=self.args();a.pop('facet')
        return dict(**a,replacement_digest=digest(a['replacement_contract']),
                    authorize_expansion=True,driver_runtime_kind='native',reason='Only repair imports; preserve assertions.')

    def test_cli_grant_keeps_report_open_routes_builder_and_real_checkpoint_closes(self):
        before=read_ledger(self.repo,self.run)
        self.assertEqual(driver.next_action(self.repo,self.run)['action'],'current_project_problem_decision')
        preview=driver.validate_decision(self.repo,self.run,**self.args())
        self.assertEqual(preview['apply']['command'],'apply-authority-decision')
        self.assertFalse(preview['scope_preview']['linked'])
        self.assertTrue(preview['scope_preview']['post_completion_repair'])
        a=self.apply_args();f=self.repo/'replacement.json';f.write_text(json.dumps(a['replacement_contract']))
        result=run_cli('assurance','--experimental-v4','apply-authority-decision','--repo',self.repo,
            '--run',self.run,'--session-id','owner','--problem-key','fixture-scope',
            '--action-id',a['action_id'],'--facet-digest',a['facet_digest'],'--contract',f,
            '--replacement-digest',a['replacement_digest'],'--authorize-expansion',
            '--driver-runtime-kind','native','--reason',a['reason'])
        self.assertEqual(result.returncode,0,result.data)
        after=read_ledger(self.repo,self.run)
        self.assertEqual(after['problems'],before['problems'])
        self.assertEqual(after['evidence'],before['evidence'])
        self.assertEqual(after['facets']['execution'],before['facets']['execution'])
        self.assertEqual(core.evidence_state(after,'machine'),'stale')
        decision=driver.next_action(self.repo,self.run)
        self.assertEqual(decision['action'],'builder_fix')
        self.assertIn(a['reason'],decision['problem']['details'])
        self.assertNotIn('work_unit_id',decision)
        from runtime.codex_builder_loop.assurance_v4.role_input import role_context
        payload=role_context(self.repo,self.run,decision['action_id'])
        self.assertIn(a['reason'],json.dumps(payload))
        core.apply_authority_decision(self.repo,self.run,**a)
        self.assertEqual(read_ledger(self.repo,self.run),after)
        candidate=Path(after['candidate_worktree']);(candidate/'regression').mkdir()
        (candidate/'regression/check.py').write_text('assert 1 + 1 == 2\n')
        git(candidate,'add','regression/check.py')
        git(candidate,'commit','-m','test(fixture): [cr_id_skip] Repair import fixture')
        core.checkpoint_builder(self.repo,self.run)
        final=read_ledger(self.repo,self.run)
        self.assertEqual(final['problems'][0]['owner'],'current_project')
        self.assertEqual(final['problems'][0]['status'],'resolved')
        self.assertNotEqual(driver.next_action(self.repo,self.run)['action'],'current_project_problem_decision')

    def test_rejects_unapproved_semantics_wildcards_ownership_and_missing_scope(self):
        before=read_ledger(self.repo,self.run)
        for path in ['regression/*','tests/private.py','.git/config']:
            r=self.replacement();r['authority']['builder_write'][-1]=path
            with self.subTest(path=path),self.assertRaises((core.AssuranceError,core.ContractError)):
                driver.validate_decision(self.repo,self.run,**self.args(r))
        for facet in ['mission','assurance']:
            a=self.args();a['facet']=facet;a['facet_digest']=before['digests'][facet]
            with self.assertRaises(core.AssuranceError):driver.validate_decision(self.repo,self.run,**a)
        r=self.replacement();r['authority']['tester_write']=[]
        with self.assertRaises((core.AssuranceError,core.ContractError)):
            driver.validate_decision(self.repo,self.run,**self.args(r))
        self.assertEqual(read_ledger(self.repo,self.run),before)

    def test_requires_authorization_reason_owner_digest_and_fresh_action(self):
        before=read_ledger(self.repo,self.run);a=self.apply_args()
        for changes in [{'authorize_expansion':False},{'reason':None},{'session_id':'other'},
                        {'action_id':'0'*64},{'replacement_digest':'0'*64}]:
            with self.subTest(changes=changes),self.assertRaises(core.AssuranceError):
                core.apply_authority_decision(self.repo,self.run,**{**a,**changes})
            self.assertEqual(read_ledger(self.repo,self.run),before)

    def test_failed_save_does_not_grant_paths_or_reroute_problem(self):
        from unittest.mock import patch
        before=read_ledger(self.repo,self.run)
        with patch.object(core,'save_ledger',side_effect=OSError('failure')):
            with self.assertRaises(OSError):core.apply_authority_decision(self.repo,self.run,**self.apply_args())
        self.assertEqual(read_ledger(self.repo,self.run),before)
        self.assertEqual(effective_problems(before)[0]['owner'],'current_project')

    def test_runtime_transition_requires_flag_and_preserves_support_and_roles(self):
        from unittest.mock import patch
        before=read_ledger(self.repo,self.run)
        identity={**before['runtime_identity'],'adapter_commit':'b'*40,'adapter_dirty':False,'capture_status':'captured'}
        with patch('runtime.codex_builder_loop.core.capture_runtime_identity',return_value=identity):
            with self.assertRaises(core.AssuranceError) as error:
                core.apply_authority_decision(self.repo,self.run,**self.apply_args())
            self.assertEqual(error.exception.code,'AUTHORITY_DECISION_RUNTIME_IDENTITY_MISMATCH')
            self.assertEqual(read_ledger(self.repo,self.run),before)
            core.apply_authority_decision(self.repo,self.run,**self.apply_args(),allow_runtime_transition=True)
        after=read_ledger(self.repo,self.run)
        self.assertEqual(after['runtime_identity'],identity)
        self.assertEqual(after['facets']['execution']['agents'],before['facets']['execution']['agents'])
        self.assertEqual(sum(e['kind']=='runtime_identity_transitioned' for e in after['events']),1)

    def test_pending_transaction_blocks_readonly_preview(self):
        from runtime.codex_builder_loop.assurance_v4.decision_requests import authority_scope_preview
        ledger=read_ledger(self.repo,self.run)
        ledger['context_rotation_intent']={'state':'pending'}
        with self.assertRaises(core.AssuranceError) as error:
            authority_scope_preview(ledger,self.replacement(),project_repair=True)
        self.assertIn('TRANSACTION_PENDING',{b['code'] for b in error.exception.details['blockers']})

    def test_explicit_scope_requires_coverage_and_completed_scope_is_not_extended(self):
        from runtime.codex_builder_loop.assurance_v4.decision_requests import authority_scope_preview
        from unittest.mock import patch
        ledger=read_ledger(self.repo,self.run)
        ledger['facets']['execution']['work_units'][0]['scope']['paths']=['src/calc.py']
        r=copy.deepcopy(ledger['facets']);r['authority']['builder_write'].append('regression/check.py')
        with patch.object(core,'_completed_work_unit_ids',return_value=set()), self.assertRaises(core.AssuranceError) as error:
            authority_scope_preview(ledger,r,project_repair=True)
        self.assertIn('MISSING_PENDING_BUILDER_SCOPE',{b['code'] for b in error.exception.details['blockers']})
        r['execution']['work_units'][0]['scope']['paths'].append('regression/check.py')
        with patch.object(core,'_completed_work_unit_ids',return_value={'builder-work-unit'}):
            with self.assertRaises(core.AssuranceError):authority_scope_preview(ledger,r,project_repair=True)
        with patch.object(core,'_completed_work_unit_ids',return_value=set()):
            self.assertTrue(authority_scope_preview(ledger,r,project_repair=True)['linked'])

    def test_completed_root_builder_resumes_with_no_old_unit_and_consumes_real_result(self):
        from runtime.codex_builder_loop.native_driver.cli import _root_runtime
        from runtime.codex_builder_loop.assurance_v4.role_input import role_context
        c=contract_for(self.repo)
        c['execution']['builder_runtime']={'schema_version':1,'mode':'root_session'}
        c['execution']['work_units'][0]['scope']['paths']=['src/calc.py']
        c['execution']['driver_enforced']=True
        self.run='root-project-repair'
        core.start(self.repo,self.run,'owner',c,driver_runtime=_root_runtime('owner'))
        core.prepare_builder(self.repo,self.run,'codex-root-session:owner',owner_mode='root_session',session_id='owner')
        def begin():
            d=driver.next_action(self.repo,self.run)
            core.begin_dispatch(self.repo,self.run,action_id=d['action_id'],action=d['action'],
                role='builder',thread_id=None,owner_session_id='owner',prompt_digest='b'*64,
                output_schema_digest='c'*64,driver_runtime_kind='native',work_unit_id=d.get('work_unit_id'),
                context_projection_digest=d.get('context_projection_digest'))
            return d
        result={'result':'implemented','evidence_report':None,'proof_spec':None,'problem_report':None}
        d=begin()
        core.apply_root_builder_result(self.repo,self.run,action_id=d['action_id'],owner_session_id='owner',result_value=result)
        core.record_problems(self.repo,self.run,{'schema_version':1,'problems':[{
            'key':'fixture-scope','owner':'current_project','summary':'Fixture permission',
            'details':'Repair imports without changing assertions.'}]},role='tester',
            agent_id='assurance-v4-tester',thread_id='assurance-v4-tester-thread')
        before=read_ledger(self.repo,self.run)
        core.apply_authority_decision(self.repo,self.run,**self.apply_args())
        d=driver.next_action(self.repo,self.run)
        self.assertEqual(d['action'],'builder_fix');self.assertNotIn('work_unit_id',d)
        self.assertIn('Only repair imports',json.dumps(role_context(self.repo,self.run,d['action_id'])))
        d=begin();candidate=Path(before['candidate_worktree']);(candidate/'regression').mkdir()
        (candidate/'regression/check.py').write_text('assert 2 == 2\n')
        git(candidate,'add','regression/check.py');git(candidate,'commit','-m','test(fixture): [cr_id_skip] Repair fixture')
        core.apply_root_builder_result(self.repo,self.run,action_id=d['action_id'],owner_session_id='owner',result_value=result)
        after=read_ledger(self.repo,self.run)
        self.assertIsNone(after['dispatch_intent'])
        self.assertEqual(after['problems'][0]['status'],'resolved')
        self.assertEqual(after['facets']['execution']['work_units'],before['facets']['execution']['work_units'])
        self.assertEqual([e for e in after['events'] if e['kind']=='work_unit_completed'],
                         [e for e in before['events'] if e['kind']=='work_unit_completed'])
