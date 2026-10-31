from __future__ import annotations

import copy
import unittest
from pathlib import Path

from harness import init_repo, cleanup_repo, commit_all, git
from tests.test_assurance_v4_contract import contract_for
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.models import digest
from runtime.codex_builder_loop.assurance_v4.driver_contract import AGENT_ACTION_CAPABILITIES
from runtime.codex_builder_loop.assurance_v4.store import read_ledger, save_ledger
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from runtime.codex_builder_loop.native_driver.core_port import CorePort


class TesterResultRetryTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.rid = 'result-retry'
        contract = contract_for(self.repo)
        contract['execution']['agents'] = {}
        contract['execution']['work_units'][1]['scope']['paths'] = ['tests/test_retry_probe.py']
        state = core.start(self.repo, self.rid, 'owner', contract, driver_runtime={
            'kind': 'native', 'protocol_version': 1, 'transport': 'codex_app_server',
            'runtime_version': 'fixture', 'protocol_schema_digest': 'a' * 64})
        core.prepare_builder(self.repo, self.rid, 'builder', 'builder-thread', owner_mode='native_thread')
        self.candidate = Path(state['candidate_worktree'])
        (self.candidate / 'src/calc.py').write_text('def add(a, b):\n    return a + b\n')
        commit_all(self.candidate, 'implement')
        core.checkpoint_builder(self.repo, self.rid)
        core.scan_doc_references(self.repo, self.rid)
        core.prepare_tester(self.repo, self.rid, 'tester', 'tester-thread')
        self.action = driver.next_action(self.repo, self.rid)
        a = self.action
        projection = core.canonical_context_projection_digest(self.ledger(), action_id=a['action_id'],
            action=a['action'], role='tester', work_unit_id=a['work_unit_id'])
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.rid, core=CorePort(), transport=object())
        prompt = coordinator._prompt(a, 'tester', core.driver_context(self.repo, self.rid))
        core.begin_dispatch(self.repo, self.rid, action_id=a['action_id'], action=a['action'],
            role='tester', thread_id='tester-thread', prompt_digest=digest(prompt),
            output_schema_digest=coordinator.output_schema_digest, work_unit_id=a['work_unit_id'],
            timeout_profile_digest=coordinator._timeout_profile_digest(a['action']),
            context_projection_digest=projection, driver_runtime_kind='native')
        self.activate()
        core.bind_dispatch_turn(self.repo, self.rid, action_id=a['action_id'], turn_id='turn-1')
        self.source = Path(self.ledger()['facets']['execution']['tester_source']['worktree'])
        self.draft = self.source / 'tests/test_retry_probe.py'
        self.draft.write_text('def test_probe():\n    assert 1 + 1 == 2\n')

    def tearDown(self):
        cleanup_repo(self.repo)

    def ledger(self):
        return read_ledger(self.repo, self.rid)

    def activate(self):
        return core.record_dispatch_activation(self.repo, self.rid,
            action_id=self.action['action_id'], activation_state='activated')

    def retry(self, code='DECISION_REQUEST_INVALID', **kwargs):
        return core.retry_dispatch(self.repo, self.rid, action_id=self.action['action_id'],
            failure_code=code, failure_details={'cause': 'DECISION_CHANGES_REQUIRED'}, **kwargs)

    def test_drafts_survive_correction_and_real_cli_feedback_survives_restart(self):
        before = self.ledger()['dispatch_intent']
        invalid = {'result': 'target_change_required', 'evidence_report': None, 'proof_spec': None,
            'problem_report': {'schema_version': 1, 'problems': [{
                'key': 'missing-contract', 'owner': 'plan', 'summary': 'Missing public contract',
                'details': 'Precise request still required.', 'decision_request': {
                    'kind': 'facet_change', 'facet': 'mission', 'changes': [], 'question': 'Clarify interface.'}}]}}
        def coordinator():
            return NativeCoordinator(repo=self.repo, run_id=self.rid, core=CorePort(),
                transport=object(), project_root=Path(__file__).resolve().parents[1])
        self.assertFalse(coordinator()._complete_dispatch_or_retry(
            self.action['action_id'], 'tester_author', invalid))
        pending = self.ledger()['dispatch_intent']
        self.assertEqual(pending['attempt'], 2)
        self.assertEqual(pending['failure_details']['cause'], 'DECISION_CHANGES_REQUIRED')
        prompt = coordinator()._retry_prompt('frozen task', pending)
        self.assertIn('DECISION_CHANGES_REQUIRED', prompt)
        delivered = {}
        class Captured(BaseException):
            pass
        class Transport:
            def resume_thread(self, **kwargs):
                delivered['resume'] = kwargs
            def read_thread(self, thread_id):
                return {'id': thread_id, 'turns': [{'id': 'turn-1', 'status': 'completed'}]}
            def run_turn(self, **kwargs):
                delivered['turn'] = kwargs
                raise Captured()
        restarted = coordinator()
        restarted.transport = Transport()
        with self.assertRaises(Captured):
            restarted._run_agent_action(self.action, AGENT_ACTION_CAPABILITIES['tester_author'])
        self.assertIn('DECISION_CHANGES_REQUIRED', delivered['turn']['prompt'])
        self.assertEqual(delivered['turn']['thread_id'], 'tester-thread')
        self.assertEqual(delivered['turn']['cwd'], str(self.source))
        self.assertTrue(self.draft.exists())
        self.activate()
        core.bind_dispatch_turn(self.repo, self.rid, action_id=self.action['action_id'], turn_id='turn-2')
        corrected = copy.deepcopy(invalid)
        corrected['problem_report']['problems'][0]['decision_request']['changes'] = [
            {'operation': 'add', 'pointer': '/interfaces/-', 'value': {'id': 'clarification', 'description': 'Public seam.'}}]
        self.assertTrue(coordinator()._complete_dispatch_or_retry(
            self.action['action_id'], 'tester_author', corrected))
        final = self.ledger()['dispatch_intent']
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(final['thread_id'], before['thread_id'])
        self.assertEqual(final['generation'], before['generation'])
        self.assertTrue(self.draft.exists())
        event = next(e['details'] for e in self.ledger()['events'] if e['kind'] == 'dispatch_retry_scheduled')
        self.assertEqual(event['pre_execution_observation_digest'], before['pre_execution_observation_digest'])
        self.assertNotEqual(event['pre_execution_observation_digest'], event['post_execution_observation_digest'])

    def test_committed_author_source_can_correct_result(self):
        commit_all(self.source, 'test source')
        self.retry(); self.activate()
        self.assertEqual(self.ledger()['dispatch_intent']['attempt'], 2)

    def test_candidate_drift_before_retry_is_rejected_without_mutation(self):
        (self.candidate / 'src/calc.py').write_text('foreign change\n')
        before = digest(self.ledger())
        with self.assertRaises(core.AssuranceError) as caught: self.retry()
        self.assertEqual(caught.exception.code, 'TESTER_RESULT_RETRY_UNSAFE')
        self.assertEqual(digest(self.ledger()), before)

    def test_outside_authority_write_is_rejected(self):
        (self.source / 'src/calc.py').write_text('foreign change\n')
        with self.assertRaises(core.AssuranceError) as caught: self.retry()
        self.assertEqual(caught.exception.code, 'TESTER_RESULT_RETRY_UNSAFE')

    def test_target_commit_drift_is_rejected(self):
        (self.repo / 'src/calc.py').write_text('target change\n'); commit_all(self.repo, 'target drift')
        with self.assertRaises(core.AssuranceError) as caught: self.retry()
        self.assertEqual(caught.exception.code, 'TESTER_RESULT_RETRY_UNSAFE')

    def test_change_after_retry_before_activation_is_rejected(self):
        self.retry()
        self.draft.write_text('changed after accepted retry\n')
        with self.assertRaises(core.AssuranceError) as caught: self.activate()
        self.assertEqual(caught.exception.code, 'DISPATCH_ACTIVATION_OBSERVATION_DRIFT')

    def test_interrupted_turn_still_rejects_source_side_effect(self):
        with self.assertRaises(core.AssuranceError) as caught:
            self.retry('responseStreamDisconnected', interrupted_retry=True)
        self.assertEqual(caught.exception.code, 'NATIVE_DISPATCH_INTERRUPTED_REQUIRES_USER')
        self.assertEqual(self.ledger()['dispatch_intent']['attempt'], 1)

    def test_result_retry_limit_is_not_reset(self):
        self.retry(); self.activate()
        core.bind_dispatch_turn(self.repo, self.rid, action_id=self.action['action_id'], turn_id='turn-2')
        self.retry(); self.activate()
        core.bind_dispatch_turn(self.repo, self.rid, action_id=self.action['action_id'], turn_id='turn-3')
        with self.assertRaises(core.AssuranceError) as caught: self.retry()
        self.assertEqual(caught.exception.code, 'NATIVE_DISPATCH_RETRY_EXHAUSTED')
        self.assertEqual(self.ledger()['dispatch_intent']['attempt'], 3)

    def test_legacy_missing_guard_fails_closed(self):
        ledger = self.ledger()
        ledger['dispatch_intent'].pop('tester_retry_guard_digest')
        save_ledger(self.repo, ledger)
        with self.assertRaises(core.AssuranceError) as caught: self.retry()
        self.assertEqual(caught.exception.code, 'TESTER_RESULT_RETRY_UNSAFE')

    def test_transport_failure_cannot_accept_author_side_effect(self):
        self.retry('responseStreamDisconnected')
        with self.assertRaises(core.AssuranceError) as caught: self.activate()
        self.assertEqual(caught.exception.code, 'DISPATCH_ACTIVATION_OBSERVATION_DRIFT')

    def test_changed_evidence_cannot_be_blessed_by_correction(self):
        ledger = self.ledger()
        ledger['evidence']['machine'] = {'unexpected': 'drift'}
        # A direct in-memory mutation exercises the guard without inventing valid evidence.
        with self.assertRaises(core.AssuranceError) as caught:
            core._assert_tester_result_retry_safe(self.repo, ledger, ledger['dispatch_intent'],
                core._progress_side_effect_observation(ledger, 'tester'))
        self.assertEqual(caught.exception.code, 'TESTER_RESULT_RETRY_UNSAFE')

    def test_rehydrated_author_binds_new_identity_before_correction(self):
        self.draft.unlink()
        self.retry('responseStreamDisconnected'); self.activate()
        core.bind_dispatch_turn(self.repo, self.rid, action_id=self.action['action_id'], turn_id='turn-2')
        self.retry('responseStreamDisconnected'); self.activate()
        core.bind_dispatch_turn(self.repo, self.rid, action_id=self.action['action_id'], turn_id='turn-3')
        with self.assertRaises(core.AssuranceError): self.retry('responseStreamDisconnected')
        a = self.action
        projection = core.canonical_context_projection_digest(self.ledger(), action_id=a['action_id'],
            action=a['action'], role='tester', work_unit_id=a['work_unit_id'])
        core.begin_dispatch_rehydration(self.repo, self.rid, action_id=a['action_id'],
            context_projection_digest=projection)
        core.claim_dispatch_rehydration(self.repo, self.rid, action_id=a['action_id'], claim_id='d'*64)
        core.record_dispatch_rehydration_spawn(self.repo, self.rid, action_id=a['action_id'],
            claim_id='d'*64, new_agent_id='new-tester', new_thread_id='new-thread')
        core.bind_dispatch_rehydration(self.repo, self.rid, action_id=a['action_id'],
            new_agent_id='new-tester', new_thread_id='new-thread', prompt_digest='e'*64)
        self.activate()
        core.bind_dispatch_turn(self.repo, self.rid, action_id=a['action_id'], turn_id='new-turn')
        self.draft.write_text('def test_new(): assert True\n')
        self.retry(); self.activate()
        pending = self.ledger()['dispatch_intent']
        self.assertEqual(pending['thread_id'], 'new-thread')
        self.assertEqual(pending['rehydration_count'], 1)

    def test_authority_does_not_override_narrow_work_unit(self):
        (self.source / 'tests/outside_unit.py').write_text('out of unit scope\n')
        with self.assertRaises(core.AssuranceError) as caught: self.retry()
        self.assertEqual(caught.exception.code, 'TESTER_RESULT_RETRY_UNSAFE')

    def test_target_drift_after_retry_is_rejected_at_activation(self):
        self.retry()
        (self.repo / 'src/calc.py').write_text('target change\n'); commit_all(self.repo, 'target drift')
        with self.assertRaises(core.AssuranceError) as caught: self.activate()
        self.assertEqual(caught.exception.code, 'DISPATCH_ACTIVATION_OBSERVATION_DRIFT')

    def test_detached_source_after_retry_is_rejected_at_activation(self):
        self.retry()
        git(self.source, 'checkout', '--detach')
        with self.assertRaises(core.AssuranceError) as caught: self.activate()
        self.assertEqual(caught.exception.code, 'DISPATCH_ACTIVATION_OBSERVATION_DRIFT')
