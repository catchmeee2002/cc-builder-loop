from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch
from pathlib import Path

from harness import ROOT, CLI, cleanup_repo, commit_all, git, init_repo, run_process
from tests.test_assurance_v4_contract import contract_for, blackbox_case_results
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.inherited import inherited_review_input
from runtime.codex_builder_loop.assurance_v4.models import digest
from runtime.codex_builder_loop.assurance_v4.problems import effective_problems
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator


class InheritedTesterObligationsTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.contract = contract_for(self.repo)
        self.contract['execution']['agents'] = {}
        self.source = core.start(self.repo, 'old', 'owner', self.contract)
        core.prepare_builder(self.repo, 'old', 'old-builder', thread_id='old-builder-thread')
        candidate = Path(self.source['candidate_worktree'])
        (candidate / 'src/calc.py').write_text('def add(a, b):\n    return a + b\n')
        commit_all(candidate, 'implement')
        core.checkpoint_builder(self.repo, 'old')
        core.prepare_tester(self.repo, 'old', 'old-tester', 'old-tester-thread')
        core.record_problems(self.repo, 'old', {'schema_version': 1, 'problems': [
            {'key': 'coverage', 'summary': 'Missing boundaries', 'details': 'OLD-PRIVATE-REPORT', 'owner': 'tester'}]},
            role='tester', agent_id='old-tester', thread_id='old-tester-thread')
        state = core.status(self.repo, 'old')
        self.contract['mission'].update(revision=2, supersedes={
            'run_id':'old','revision':1,'mission_digest':state['digests']['mission'],
            'candidate_head':read_ledger(self.repo,'old')['facets']['execution']['candidate_head']})
        self.contract['execution']['revision_transition'] = {'category':'execution_contract',
            'predecessor_pressure_digest':state['lineage']['pressure_digest']}
        self.contract['execution']['prior_problem_dispositions'] = {
            'source_snapshot_digest':state['lineage']['open_problem_snapshot_digest'],
            'items':[{'key':'coverage','disposition':'included'}]}

    def tearDown(self):
        cleanup_repo(self.repo)

    def cli(self, *args):
        p=run_process(['python3',CLI,'assurance','--experimental-v4',*map(str,args)])
        value=json.loads(p.stdout.splitlines()[-1])
        return p.returncode,value

    def start_successor(self):
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'contract.json';path.write_text(json.dumps(self.contract))
            for command in ('validate','start'):
                args=[command,'--repo',self.repo,'--contract',path]
                if command=='start':args+=['--run','new','--session-id','owner']
                rc,result=self.cli(*args);self.assertEqual(rc,0,result)
                if command=='start':
                    before=digest(read_ledger(self.repo,'new'))
                    rc,replay=self.cli(*args);self.assertEqual(rc,0,replay)
                    self.assertEqual(before,digest(read_ledger(self.repo,'new')))
        core.prepare_builder(self.repo,'new','builder',thread_id='builder-thread')
        core.checkpoint_builder(self.repo,'new')
        core.scan_doc_references(self.repo,'new')
        core.prepare_tester(self.repo,'new','tester','tester-thread')

    def test_preflight_rejects_revision_without_mutating_source(self):
        before=digest(read_ledger(self.repo,'old'))
        self.contract['mission']['revision']=1
        # Use source revision 2 would exercise the runtime +1 rule; here schema already rejects r1 supersedes.
        with self.assertRaises(Exception):core.validate(self.contract,self.repo)
        self.assertEqual(before,digest(read_ledger(self.repo,'old')))
        self.contract['mission']['revision']=3
        with self.assertRaises(core.AssuranceError) as caught:core.validate(self.contract,self.repo)
        self.assertEqual(caught.exception.code,'MISSION_REVISION_INVALID')
        self.assertEqual(before,digest(read_ledger(self.repo,'old')))

    def test_official_handoff_author_and_final_review_discharge(self):
        self.start_successor()
        action=driver.next_action(self.repo,'new')
        self.assertEqual(action['action'],'tester_author',action)
        context=core.driver_context(self.repo,'new')
        coordinator=NativeCoordinator(repo=self.repo,run_id='new',core=object(),transport=object(),project_root=ROOT)
        prompt=coordinator._prompt(action,'tester',context)
        self.assertNotIn('OLD-PRIVATE-REPORT',prompt)
        self.assertNotIn('old-tester-thread',prompt)
        self.assertNotIn('inherited_problem_review',prompt)
        ledger=read_ledger(self.repo,'new')
        old=ledger['problems'][0]
        self.assertEqual(old['producer']['thread_id'],'old-tester-thread')
        source=Path(ledger['facets']['execution']['tester_source']['worktree'])
        (source/'tests/test_new.py').write_text('from src.calc import add\ndef test_boundary():\n    assert add(-1, 1) == 0\n')
        commit_all(source,'independent boundary test')
        core.integrate_tester(self.repo,'new')
        ledger=read_ledger(self.repo,'new');src=ledger['facets']['execution']['tester_source']
        def report(kind,details):
            live=read_ledger(self.repo,'new');role='tester' if kind in {'tester','blackbox'} else 'reviewer'
            return {'schema_version':1,'kind':kind,'status':'pass','candidate_head':live['facets']['execution']['candidate_head'],
                    'producer':{'role':role,**live['facets']['execution']['agents'][role]},'details':details}
        core.record_evidence(self.repo,'new','tester',report('tester',{'result':'tests_ready','source_head':src['head'],'files':src['files']}))
        self.assertEqual(effective_problems(read_ledger(self.repo,'new'))[0]['status'],'open')
        core.verify_machine(self.repo,'new')
        ledger=read_ledger(self.repo,'new');head=ledger['facets']['execution']['candidate_head'];command=ledger['facets']['execution']['commands'][0]
        blackbox=run_process(command['argv'],cwd=ledger['candidate_worktree'])
        self.assertEqual(blackbox.returncode,0,blackbox.stderr)
        self.assertEqual(git(Path(ledger['candidate_worktree']),'rev-parse','HEAD'),head)
        core.record_evidence(self.repo,'new','blackbox',report('blackbox',{'result':'pass','worktree':ledger['candidate_worktree'],
            'before_head':head,'after_head':head,'executions':[{'id':command['id'],'argv':command['argv'],'returncode':blackbox.returncode,'timed_out':False}],
            'cases':blackbox_case_results(ledger)}))
        core.scan_doc_references(self.repo,'new')
        core.prepare_reviewer(self.repo,'new','reviewer','reviewer-thread')
        review_action=driver.next_action(self.repo,'new')
        review_prompt=coordinator._prompt(review_action,'reviewer',core.driver_context(self.repo,'new'))
        self.assertIn('OLD-PRIVATE-REPORT',review_prompt)
        self.assertIn('inherited_problem_review',review_prompt)
        review=report('reviewer',{'result':'pass','reviewed_head':head})
        with self.assertRaises(core.AssuranceError) as caught:core.record_evidence(self.repo,'new','reviewer',review)
        self.assertEqual(caught.exception.code,'INHERITED_PROBLEM_REVIEW_REQUIRED')
        view=inherited_review_input(read_ledger(self.repo,'new'))
        review['details']['inherited_problem_resolutions']=[{'obligation_id':o['obligation_id'],'evidence_digest':view['evidence_digest'],
            'accepted':True,'rationale':'New boundary test, machine run and blackbox cover the inherited gap.'} for o in view['obligations']]
        invalid=copy.deepcopy(review);invalid['details']['inherited_problem_resolutions'][0]['evidence_digest']='0'*64
        with self.assertRaises(core.AssuranceError):core.record_evidence(self.repo,'new','reviewer',invalid)
        duplicate=copy.deepcopy(review)
        duplicate['details']['inherited_problem_resolutions'] *= 2
        with self.assertRaises(core.AssuranceError):core.record_evidence(self.repo,'new','reviewer',duplicate)
        core.record_evidence(self.repo,'new','reviewer',review)
        core.record_evidence(self.repo,'new','reviewer',review)
        ledger=read_ledger(self.repo,'new')
        self.assertEqual(effective_problems(ledger)[0]['status'],'resolved')
        self.assertEqual(ledger['problems'][0]['status'],'open') # Immutable report, derived discharge.
        self.assertEqual(driver._tester_correction_progress(ledger)['completed'],0)
        self.assertTrue(core.readiness(ledger)['ready'])
        downgraded=copy.deepcopy(ledger)
        downgraded['facets']['assurance']['required'].remove('reviewer')
        downgraded['evidence'].pop('reviewer')
        self.assertFalse(core.readiness(downgraded)['ready'])
        self.assertEqual(effective_problems(downgraded)[0]['status'],'open')
        drift=copy.deepcopy(ledger);drift['evidence']['machine']['details']['injected']='drift'
        self.assertEqual(core.evidence_state(drift,'reviewer'),'stale')
        self.assertEqual(effective_problems(drift)[0]['status'],'open')
        core.finalize(self.repo,'new','fix(calc): [cr_id_skip] Complete independent delivery')
        self.assertEqual(core.status(self.repo,'new')['phase'],'finalized')

    def test_same_key_current_report_does_not_replace_inherited_obligation(self):
        self.start_successor()
        before=copy.deepcopy(read_ledger(self.repo,'new')['problems'][0])
        core.record_problems(self.repo,'new',{'schema_version':1,'problems':[
            {'key':'coverage','summary':'New fixture defect','details':'Current producer correction','owner':'tester'}]},
            role='tester',agent_id='tester',thread_id='tester-thread')
        ledger=read_ledger(self.repo,'new')
        self.assertEqual(ledger['problems'][0],before)
        self.assertEqual(len(ledger['problems']),2)
        keys=core._open_problem_snapshot(ledger)[1]
        self.assertEqual(len({p['key'] for p in keys}),2)
        self.assertEqual(driver.next_action(self.repo,'new')['action'],'tester_fix')
        core._close_problems(ledger,'tester','tester-source:new')
        self.assertEqual(ledger['problems'][0]['status'],'open')
        self.assertEqual(ledger['problems'][1]['status'],'resolved')

    def test_preflight_snapshot_drift_rejected_before_handoff(self):
        core.validate(self.contract,self.repo)
        core.record_problems(self.repo,'old',{'schema_version':1,'problems':[
            {'key':'second','summary':'Another gap','details':'Still pending','owner':'tester'}]},
            role='tester',agent_id='old-tester',thread_id='old-tester-thread')
        with self.assertRaises(core.AssuranceError) as caught:
            core.start(self.repo,'new','owner',self.contract)
        self.assertEqual(caught.exception.code,'PRIOR_PROBLEM_SNAPSHOT_MISMATCH')
        self.assertEqual(core.status(self.repo,'old')['phase'],'active')
        self.assertFalse((Path(self.source['candidate_worktree']).parent.parent/'new').exists())

    def test_role_report_cannot_forge_inheritance(self):
        from runtime.codex_builder_loop.assurance_v4.models import validate_problem_report, ContractError
        report={'schema_version':1,'problems':[{'key':'x','summary':'s','details':'d','owner':'tester',
            'inherited_from':{'run_id':'old','problem_digest':'a'*64}}]}
        with self.assertRaises(ContractError):validate_problem_report(report)

    def test_inherited_observation_and_continuity_cannot_drive_new_role(self):
        self.start_successor()
        ledger=read_ledger(self.repo,'new')
        old=ledger['problems'][0]
        old['observation_request']={'reason':'historical request'}
        old['producer_continuity']='invalid'
        old['candidate_head']='0'*40
        before=copy.deepcopy(old)
        core._refresh_observation_requests(ledger)
        self.assertEqual(old,before)
        with self.assertRaises(core.AssuranceError):core._tester_replacement_problem(ledger,old['key'])

    def test_interrupted_handoff_replays_without_duplicate_obligations(self):
        class Interrupted(BaseException):
            pass
        save=core._store_save_ledger
        def interrupt(repo,ledger):
            save(repo,ledger)
            if ledger['run_id']=='new':
                raise Interrupted()
        with patch.object(core,'_store_save_ledger',side_effect=interrupt):
            with self.assertRaises(Interrupted):core.start(self.repo,'new','owner',self.contract)
        core.start(self.repo,'new','owner',self.contract)
        ledger=read_ledger(self.repo,'new')
        self.assertEqual(len(ledger['problems']),1)
        self.assertEqual(len(inherited_review_input(ledger)['obligations']),1)
        self.assertEqual(sum(e['kind']=='run_started' for e in ledger['events']),1)
        self.assertEqual(core.status(self.repo,'old')['phase'],'superseded')
