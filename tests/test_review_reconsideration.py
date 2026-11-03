"""Exercise real report intake, CLI recovery, original thread and authoritative gates."""
import copy
import json
import unittest
from pathlib import Path
from tests import test_full_driver_v4_contract as fixture
from runtime.codex_builder_loop.assurance_v4 import core, driver, review_recovery
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator


class ReviewReconsiderationTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FullDriverV4ContractTest()
        self.f.setUp()
        original_invoke = self.f.invoke
        def invoke(command, *args, **kwargs):
            if command in {'checkpoint-builder', 'prepare-tester', 'integrate-tester', 'verify-preflight', 'record-evidence'} and '--driver-runtime-kind' not in args:
                args = (*args, '--driver-runtime-kind', 'full_driver_skill')
            return original_invoke(command, *args, **kwargs)
        self.f.invoke = invoke
        self.repo, self.run = self.f.repo, 'review-reconsideration'
        contract = fixture.contract_for(self.repo)
        contract['execution']['driver_enforced'] = False
        contract['assurance']['preflight_before_proof'] = True
        contract['assurance']['reviewer_preflight'] = True
        contract['assurance']['machine_commands'][0]['run_before_full_suite'] = True
        path = self.f.write_json('contract.json', contract)
        rc, started = self.f.invoke('start', '--repo', self.repo, '--run', self.run,
            '--session-id', 'owner', '--contract', path, '--driver-kind', 'full_driver_skill')
        self.assertEqual(rc, 0, started)
        self.run_path = Path(started['candidate_worktree']).parent
        rc, checked = self.f.invoke('checkpoint-builder', '--repo', self.repo, '--run', self.run)
        self.assertEqual(rc, 0, checked)
        self.f.prepare_and_record_tester(self.run, self.run_path)
        candidate = Path(self.ledger()['candidate_worktree'])
        implementation = candidate/'src/calc.py'
        implementation.write_text(implementation.read_text() + '\n# implementation-only revision\n')
        fixture.commit_all(candidate, 'implementation-only revision')
        rc, checked = self.f.invoke('checkpoint-builder', '--repo', self.repo, '--run', self.run)
        self.assertEqual(rc, 0, checked)
        self.assertEqual(self.f.invoke('verify-preflight', '--repo', self.repo, '--run', self.run)[0], 0)
        self.problem = {'key': 'source-head-mismatch', 'owner': 'builder_loop',
                        'summary': 'Old observed HEAD', 'details': 'Incorrectly requires equal source and candidate HEADs.'}
        self.original = self.next()
        self.assertEqual(self.original['action'], 'reviewer_preflight')
        self.submit(self.original, {'result': 'blocked', 'evidence_report': None, 'proof_spec': None,
                                   'problem_report': {'schema_version': 1, 'problems': [self.problem]}})
        self.blocked = self.next()
        self.assertEqual(self.blocked['action'], 'builder_loop_problem_decision')
        self.before = self.ledger()

    def tearDown(self):
        self.f.tearDown()

    def ledger(self):
        return self.f.load_ledger(self.run_path)

    def next(self):
        return driver.next_action(self.repo, self.run)

    def begin(self, action):
        core.begin_dispatch(self.repo, self.run, action_id=action['action_id'], action=action['action'],
                            role='reviewer', thread_id=action['agent']['thread_id'],
                            prompt_digest='b'*64, output_schema_digest='c'*64,
                            driver_runtime_kind='full_driver_skill')
        core.bind_dispatch_turn(self.repo, self.run, action_id=action['action_id'], turn_id='turn-'+action['action_id'])

    def submit(self, action, result):
        self.begin(action)
        core.complete_dispatch(self.repo, self.run, action_id=action['action_id'], result_value=result)
        return core.apply_dispatch_result(self.repo, self.run, action_id=action['action_id'], driver_runtime_kind='full_driver_skill')

    def request(self, **changes):
        args = dict(action_id=self.blocked['action_id'], reason='Verify source-bound gate without rewriting provenance.',
                    problem_key=self.problem['key'])
        args.update(changes)
        return self.f.invoke('reconsider-review-problem', '--repo', self.repo, '--run', self.run,
                             '--session-id', 'owner', '--action-id', args['action_id'], '--problem-key', args['problem_key'],
                             '--reason', args['reason'], '--driver-runtime-kind', 'full_driver_skill')

    def result(self, decision='withdrawn'):
        ledger = self.ledger(); request = review_recovery.active_request(ledger)
        candidate = ledger['facets']['execution']['candidate_head']
        report = {'schema_version': 1, 'kind': 'reviewer_preflight', 'status': 'pass' if decision=='withdrawn' else 'fail',
                  'candidate_head': candidate, 'producer': {'role':'reviewer', **ledger['facets']['execution']['agents']['reviewer']},
                  'details': {'reviewed_head': candidate, 'result': 'pass' if decision=='withdrawn' else 'blocked',
                              'review_problem_dispositions': [{'request_id':request['request_id'], 'problem_key':self.problem['key'],
                                                                'decision':decision, 'reason':'Rechecked dependency and integrated blobs.'}]}}
        return {'result':'pass' if decision=='withdrawn' else 'blocked', 'evidence_report':report,
                'proof_spec':None, 'problem_report':None if decision=='withdrawn' else {'schema_version':1,'problems':[self.problem]}}

    def test_cli_original_thread_prompt_and_explicit_withdrawal(self):
        rc, data = self.request(); self.assertEqual(rc, 0, data)
        after = self.ledger(); self.assertEqual(after['problems'], self.before['problems'])
        self.assertEqual(after['evidence'], self.before['evidence'])
        self.assertEqual(after['facets'], self.before['facets'])
        action = self.next(); self.assertEqual(action['action'], 'reviewer_preflight')
        self.assertEqual(action['agent'], self.original['agent'])
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.run, core=object(), transport=object(), project_root=fixture.ROOT)
        prompt = json.loads(coordinator._prompt(action, 'reviewer', core.driver_context(self.repo,self.run)).split('\n',1)[1])
        self.assertEqual(prompt['evidence_gates']['tester']['state'], 'pass')
        self.assertNotEqual(prompt['evidence_gates']['tester']['observed_head'], prompt['evidence_gates']['tester']['current_candidate'])
        self.assertEqual(prompt['review_reconsideration']['problem_key'], self.problem['key'])
        self.assertIn('historical provenance', prompt['gate_validity_rule'])
        self.assertIn('review_problem_dispositions', prompt['review_reconsideration']['result_contract'])
        from runtime.codex_builder_loop.assurance_v4.models import digest
        self.assertEqual(prompt['review_input_contract']['pre_turn_gates']['current_validity']['digest'], digest(prompt['evidence_gates']))
        self.submit(action, self.result())
        self.assertEqual(self.ledger()['problems'][0]['status'], 'resolved')
        self.assertEqual(self.next()['action'], 'verify_machine')
        self.assertEqual(self.ledger()['evidence']['tester'], self.before['evidence']['tester'])
        self.assertFalse(any(e['kind']=='tester_correction_authorized' for e in self.ledger()['events']))

    def test_upheld_problem_stops_without_automatic_second_review(self):
        self.assertEqual(self.request()[0], 0)
        self.submit(self.next(), self.result('upheld'))
        self.assertEqual(self.ledger()['problems'][0]['status'], 'open')
        self.assertEqual(self.next()['action'], 'builder_loop_problem_decision')
        self.assertIsNone(review_recovery.active_request(self.ledger()))

    def test_stale_request_and_conflicting_replay_do_not_mutate(self):
        path=self.run_path/'ledger.json'; before=path.read_bytes()
        self.assertNotEqual(self.request(action_id='f'*64)[0], 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.request()[0], 0)
        after=path.read_bytes()
        self.assertEqual(self.request()[0], 0);self.assertEqual(path.read_bytes(), after)
        self.assertNotEqual(self.request(reason='different reason')[0], 0)
        self.assertEqual(path.read_bytes(), after)

    def test_real_prerequisite_drift_remains_blocking(self):
        self.assertEqual(self.request()[0], 0)
        ledger=self.ledger();ledger['evidence']['preflight']['dependency_digest']='0'*64
        core.save_ledger(self.repo,ledger)
        self.assertEqual(self.next()['status'],'NEEDS_USER')
        self.assertEqual(self.next()['reason'],'REVIEW_RECONSIDERATION_STALE')
        self.assertEqual(self.ledger()['problems'][0]['status'],'open')

    def test_unbound_history_and_foreign_reviewer_are_rejected(self):
        ledger=self.ledger()
        artifact=self.run_path/'artifacts'/('dispatch-'+self.original['action_id']+'.json')
        original=artifact.read_bytes();artifact.write_text('{}')
        before=(self.run_path/'ledger.json').read_bytes()
        rc, result=self.request();self.assertNotEqual(rc,0)
        self.assertEqual(result['code'],'REVIEW_RECONSIDERATION_REPORT_UNBOUND')
        self.assertEqual((self.run_path/'ledger.json').read_bytes(),before)
        artifact.write_bytes(original)
        ledger['problems'][0]['producer']['thread_id']='another-thread'
        core.save_ledger(self.repo,ledger)
        self.blocked=self.next()
        rc,result=self.request();self.assertNotEqual(rc,0)
        self.assertEqual(result['code'],'REVIEW_RECONSIDERATION_PRODUCER_INVALID')

    def test_missing_disposition_never_closes_problem(self):
        self.assertEqual(self.request()[0],0)
        result=self.result();del result['evidence_report']['details']['review_problem_dispositions']
        with self.assertRaises(Exception) as raised:
            self.submit(self.next(),result)
        self.assertEqual(raised.exception.code,'EVIDENCE_REPORT_INVALID')
        self.assertNotEqual(self.ledger()['dispatch_intent']['state'],'completed')
        self.assertEqual(self.ledger()['problems'][0]['status'],'open')
        self.assertNotEqual(core.evidence_state(self.ledger(),'reviewer_preflight'),'pass')

    def test_native_normalization_preserves_failed_reconsideration(self):
        self.assertEqual(self.request()[0],0)
        result=self.result('upheld')
        self.assertEqual(NativeCoordinator._normalize_action_result('reviewer_preflight',result),result)

    def test_rebuilt_prompt_retry_and_result_replay_keep_single_disposition(self):
        self.assertEqual(self.request()[0],0)
        action=self.next()
        coordinator=NativeCoordinator(repo=self.repo,run_id=self.run,core=object(),transport=object(),project_root=fixture.ROOT)
        first=coordinator._prompt(action,'reviewer',core.driver_context(self.repo,self.run))
        self.begin(action)
        self.assertEqual(coordinator._prompt(self.next(),'reviewer',core.driver_context(self.repo,self.run)),first)
        core.retry_dispatch(self.repo,self.run,action_id=action['action_id'],failure_code='EVIDENCE_REPORT_INVALID')
        self.assertEqual(coordinator._prompt(self.next(),'reviewer',core.driver_context(self.repo,self.run)),first)
        core.bind_dispatch_turn(self.repo,self.run,action_id=action['action_id'],turn_id='corrected-review')
        result=self.result()
        core.complete_dispatch(self.repo,self.run,action_id=action['action_id'],result_value=result)
        core.apply_dispatch_result(self.repo,self.run,action_id=action['action_id'],driver_runtime_kind='full_driver_skill')
        before=(self.run_path/'ledger.json').read_bytes()
        core.apply_dispatch_result(self.repo,self.run,action_id=action['action_id'],driver_runtime_kind='full_driver_skill')
        self.assertEqual((self.run_path/'ledger.json').read_bytes(),before)
        self.assertEqual(sum(e['kind']=='review_problem_reconsidered' for e in self.ledger()['events']),1)

    def test_completed_transport_cleanup_is_retained_but_unknown_cleanup_rejects(self):
        ledger=self.ledger()
        identity={'pid':123,'started_at':'fixture'}
        ledger['driver_runtime']['native_transport']={'state':'cleaned','generation':'fixture','process_identity':identity}
        ledger['transport_cleanup_intent']={'state':'completed','source_generation':'fixture','process_identity':identity,'process_group_gone':True}
        self.assertEqual(core.recovery_transaction_blockers(ledger),[])
        ledger['transport_cleanup_intent']['process_group_gone']=False
        self.assertIn('transport_cleanup_intent',core.recovery_transaction_blockers(ledger))

    def test_withdrawal_preserves_other_findings_and_does_not_grant_pass(self):
        self.assertEqual(self.request()[0],0)
        result=self.result()
        result['result']='findings'
        result['evidence_report']['status']='fail'
        result['evidence_report']['details']['result']='findings'
        result['problem_report']={'schema_version':1,'problems':[{
            'key':'remaining-implementation-defect','owner':'builder','summary':'Still incorrect',
            'details':'Reconsidering gate semantics does not fix this implementation finding.'}]}
        self.submit(self.next(),result)
        problems={p['key']:p for p in self.ledger()['problems']}
        self.assertEqual(problems[self.problem['key']]['status'],'resolved')
        self.assertEqual(problems['remaining-implementation-defect']['status'],'open')
        self.assertEqual(core.evidence_state(self.ledger(),'reviewer_preflight'),'failed')
        self.assertEqual(self.next()['action'],'builder_fix')


class DispositionIntakeTest(unittest.TestCase):
    def test_unrequested_disposition_is_rejected_before_acceptance(self):
        from runtime.codex_builder_loop.assurance_v4.models import ContractError
        review_recovery.validate_result({'events': []}, {'evidence_report': None})
        with self.assertRaises(ContractError) as raised:
            review_recovery.validate_result({'events': []}, {'evidence_report': {'details': {
                'review_problem_dispositions': [{'decision': 'withdrawn'}]}}})
        self.assertEqual(raised.exception.code, 'EVIDENCE_REPORT_INVALID')
