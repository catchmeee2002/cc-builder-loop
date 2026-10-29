from __future__ import annotations

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import commit_all
from tests import test_result_application_contract as fixture
from runtime.codex_builder_loop.assurance_v4 import core, driver, result_application
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from runtime.codex_builder_loop.native_driver.core_port import CorePort, CorePortError


class ReportRecoveryTest(unittest.TestCase):
    setUp = fixture.ResultApplicationContractTest.setUp
    tearDown = fixture.ResultApplicationContractTest.tearDown
    begin = fixture.ResultApplicationContractTest.begin
    apply = fixture.ResultApplicationContractTest.apply
    interrupt_after = fixture.ResultApplicationContractTest.interrupt_after

    def prepare_tester(self):
        self.apply()
        core.scan_doc_references(self.repo, self.run_id)
        core.prepare_tester(self.repo, self.run_id, 'tester', 'tester-thread')
        self.action = self.begin('tester')

    def report(self, details='No tests written yet', owner='tester'):
        return {'result': 'blocked', 'evidence_report': None, 'proof_spec': None,
                'problem_report': {'schema_version': 1, 'problems': [{
                    'key': 'coverage-incomplete', 'summary': 'Coverage incomplete',
                    'details': details, 'owner': owner}]}}

    def complete(self, result):
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action['action_id'], result_value=result)

    def test_followup_updates_same_problem_preserves_history_and_replays(self):
        self.prepare_tester()
        first = self.report()
        self.complete(first)
        self.apply()
        original = read_ledger(self.repo, self.run_id)['problems'][0]
        self.action = self.begin('tester')
        source = Path(read_ledger(self.repo, self.run_id)['facets']['execution']['tester_source']['worktree'])
        (source / 'tests/test_recovery.py').write_text('partial = True\n')
        commit_all(source, 'partial coverage')
        second = self.report('Partial tests committed; remaining coverage pending')
        self.complete(second)
        self.interrupt_after('problem_report_updated', self.apply)
        self.apply()
        after = read_ledger(self.repo, self.run_id)
        self.assertEqual(after['problems'][0], original)
        self.assertEqual(core.driver_context(self.repo, self.run_id)['problems'][0]['details'], second['problem_report']['problems'][0]['details'])
        self.assertEqual(core.status(self.repo, self.run_id)['problems'][0]['details'], second['problem_report']['problems'][0]['details'])
        _, snapshot = core._open_problem_snapshot(after)
        self.assertEqual(snapshot[0]['details'], second['problem_report']['problems'][0]['details'])
        self.assertEqual(sum(e['kind'] == 'problem_report_updated' for e in after['events']), 1)
        self.assertTrue((source / 'tests/test_recovery.py').exists())
        self.assertEqual(driver.next_action(self.repo, self.run_id)['action'], 'tester_fix')
        self.apply(second)
        with self.assertRaises(core.AssuranceError):
            self.apply(self.report('different replay'))
        fixture.ResultApplicationContractTest.prepare_tester_result(self)
        self.interrupt_after('tester_source_integrated', self.apply)
        self.apply()
        finished = read_ledger(self.repo, self.run_id)
        self.assertEqual(core.evidence_state(finished, 'tester'), 'pass')
        self.assertEqual(finished['problems'][0]['status'], 'resolved')

    def test_fake_action_cannot_update_problem(self):
        self.prepare_tester()
        self.complete(self.report())
        self.apply()
        before = read_ledger(self.repo, self.run_id)
        with self.assertRaises(core.AssuranceError):
            core.record_problems(self.repo, self.run_id, self.report('forged')['problem_report'],
                role='tester', agent_id='tester', thread_id='tester-thread', action_id='forged')
        self.assertEqual(read_ledger(self.repo, self.run_id), before)

    def test_application_failure_is_durable_and_does_not_adopt_worktree_drift(self):
        original = read_ledger(self.repo, self.run_id)['dispatch_intent']
        with patch.object(core, 'checkpoint_builder', side_effect=core.AssuranceError(
                'fixture blocked', code='FIXTURE_BLOCKED', status='FAIL', details={'path': 'src/calc.py'})):
            for _ in range(2):
                with self.assertRaises(core.AssuranceError):
                    self.apply()
        failed = read_ledger(self.repo, self.run_id)
        failures = [e for e in failed['events'] if e['kind'] == 'dispatch_application_failed']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['details']['effect'], 'checkpoint')
        self.assertEqual(driver.next_action(self.repo, self.run_id)['status'], 'NEEDS_USER')
        view = result_application.recovery_view(failed)
        self.assertEqual(view['failure']['code'], 'FIXTURE_BLOCKED')
        self.assertIn('apply-dispatch-result', view['command'])
        self.assertEqual(failed['dispatch_intent']['result_digest'], original['result_digest'])
        retro = core.retrospective_status(self.repo, 'result-session')
        self.assertEqual(retro['status'], 'REQUIRED')
        self.assertTrue(retro['snapshot']['snapshot_digest'])
        self.apply()
        self.assertIsNone(read_ledger(self.repo, self.run_id)['dispatch_intent'])

    def test_empty_changes_feedback_reaches_retry_input_through_real_cli(self):
        self.prepare_tester()
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.run_id, core=CorePort(), transport=None)
        bad = self.report(owner='plan')
        bad['problem_report']['problems'][0]['decision_request'] = {
            'kind': 'facet_change', 'facet': 'mission', 'changes': [], 'question': 'Clarify interface?'}
        self.assertFalse(coordinator._complete_dispatch_or_retry(self.action['action_id'], self.action['action'], bad))
        pending = read_ledger(self.repo, self.run_id)['dispatch_intent']
        self.assertEqual(pending['failure_details']['cause'], 'DECISION_CHANGES_REQUIRED')
        prompt = coordinator._retry_prompt('frozen task', pending)
        self.assertIn('DECISION_CHANGES_REQUIRED', prompt)
        self.assertIn('coverage-incomplete', prompt)
        self.assertIn('correct the result', prompt)
        rebuilt = NativeCoordinator(repo=self.repo, run_id=self.run_id, core=CorePort(), transport=None)
        self.assertEqual(rebuilt._retry_prompt('frozen task', pending), prompt)
        self.complete(self.report('Corrected result'))
        self.apply()
        self.assertIsNone(read_ledger(self.repo, self.run_id)['dispatch_intent'])

    def test_historical_report_requires_unique_consumed_artifact(self):
        from runtime.codex_builder_loop.assurance_v4 import problems
        self.prepare_tester()
        first = self.report()
        self.complete(first)
        with patch.object(problems, 'bind_report'):
            self.apply()
        original = copy.deepcopy(read_ledger(self.repo, self.run_id)['problems'])
        self.action = self.begin('tester')
        self.complete(self.report('Historical follow-up'))
        self.apply()
        after = read_ledger(self.repo, self.run_id)
        self.assertEqual(after['problems'], original)
        self.assertEqual(core.status(self.repo, self.run_id)['problems'][0]['details'], 'Historical follow-up')
        self.assertEqual(sum(e['kind'] == 'problem_report_bound' for e in after['events']), 1)

    def test_unbound_history_is_preserved_without_guessing_identity(self):
        from runtime.codex_builder_loop.assurance_v4 import problems
        self.prepare_tester()
        self.complete(self.report())
        artifact = Path(read_ledger(self.repo, self.run_id)['dispatch_intent']['result_path'])
        with patch.object(problems, 'bind_report'):
            self.apply()
        artifact.unlink()
        self.action = self.begin('tester')
        self.complete(self.report('Cannot infer old action'))
        with self.assertRaises(core.AssuranceError) as raised:
            self.apply()
        self.assertEqual(raised.exception.code, 'PROBLEM_HISTORY_UNBOUND')
        self.assertEqual(core.status(self.repo, self.run_id)['problems'][0]['details'], 'No tests written yet')

    def test_followup_cannot_change_owner_or_decision_request(self):
        self.prepare_tester()
        self.complete(self.report())
        self.apply()
        self.action = self.begin('tester')
        self.complete(self.report('Request unauthorized routing change', owner='plan'))
        with self.assertRaises(core.AssuranceError) as raised:
            self.apply()
        self.assertEqual(raised.exception.code, 'PROBLEM_FOLLOWUP_CONTROL_CHANGED')
        self.assertEqual(core.status(self.repo, self.run_id)['problems'][0]['owner'], 'tester')

    def test_retry_exhaustion_keeps_diagnostic_and_attempt_bound(self):
        self.prepare_tester()
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.run_id, core=CorePort(), transport=None)
        bad = self.report(owner='plan')
        bad['problem_report']['problems'][0]['decision_request'] = {
            'kind': 'facet_change', 'facet': 'mission', 'changes': [], 'question': 'Clarify?'}
        for _ in range(2):
            coordinator._complete_dispatch_or_retry(self.action['action_id'], self.action['action'], bad)
        with self.assertRaises(CorePortError) as raised:
            coordinator._complete_dispatch_or_retry(self.action['action_id'], self.action['action'], bad)
        self.assertEqual(raised.exception.code, 'NATIVE_DISPATCH_RETRY_EXHAUSTED')
        pending = read_ledger(self.repo, self.run_id)['dispatch_intent']
        self.assertEqual(pending['state'], 'exhausted')
        self.assertEqual(pending['attempt'], 3)
        self.assertEqual(pending['failure_details']['cause'], 'DECISION_CHANGES_REQUIRED')

    def test_real_retry_delivery_receives_feedback_after_coordinator_restart(self):
        import json
        from runtime.codex_builder_loop.assurance_v4.models import digest
        from runtime.codex_builder_loop.native_driver.app_server import TurnResult
        self.apply()
        core.scan_doc_references(self.repo, self.run_id)
        core.prepare_tester(self.repo, self.run_id, 'tester', 'tester-thread')
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.run_id, core=CorePort(), transport=None)
        self.action = driver.next_action(self.repo, self.run_id)
        context = core.driver_context(self.repo, self.run_id)
        base = coordinator._prompt(self.action, 'tester', context)
        core.begin_dispatch(self.repo, self.run_id, action_id=self.action['action_id'],
            action=self.action['action'], role='tester', thread_id='tester-thread',
            prompt_digest=digest(base), output_schema_digest=coordinator.output_schema_digest,
            driver_runtime_kind='native', work_unit_id=self.action.get('work_unit_id'),
            context_projection_digest=self.action.get('context_projection_digest'))
        bad = self.report(owner='plan')
        bad['problem_report']['problems'][0]['decision_request'] = {
            'kind': 'facet_change', 'facet': 'mission', 'changes': [], 'question': 'Clarify?'}
        coordinator._complete_dispatch_or_retry(self.action['action_id'], self.action['action'], bad)
        good = self.report('Corrected report after actionable feedback')
        delivered = []
        class Transport:
            def resume_thread(self, **kwargs):
                pass
            def read_thread(self, thread_id):
                return {'turns': []}
            def run_turn(self, **kwargs):
                delivered.append(kwargs)
                kwargs['on_started']('corrected-turn')
                return TurnResult(turn_id='corrected-turn', status='completed', text=json.dumps(good))
        rebuilt = NativeCoordinator(repo=self.repo, run_id=self.run_id, core=CorePort(), transport=Transport())
        context = core.driver_context(self.repo, self.run_id)
        result = rebuilt._recover_dispatch(context['dispatch_intent'], 'tester', context,
                                           driver.next_action(self.repo, self.run_id))
        self.assertEqual(result, good)
        self.assertEqual(delivered[0]['thread_id'], 'tester-thread')
        self.assertTrue(delivered[0]['prompt'].startswith(base))
        self.assertIn('DECISION_CHANGES_REQUIRED', delivered[0]['prompt'])
        self.assertIn('coverage-incomplete', delivered[0]['prompt'])
        self.apply()
        self.assertIsNone(read_ledger(self.repo, self.run_id)['dispatch_intent'])

    def test_failure_diagnostic_never_adopts_uncommitted_side_effect(self):
        before = None
        def fail_after_write(*args, **kwargs):
            nonlocal before
            before = copy.deepcopy(read_ledger(self.repo, self.run_id)['dispatch_intent']['application'])
            (self.candidate / 'src/calc.py').write_text('unconfirmed change\n')
            raise OSError('failed after partial effect')
        with patch.object(core, 'checkpoint_builder', side_effect=fail_after_write):
            with self.assertRaises(core.AssuranceError):
                self.apply()
        after = read_ledger(self.repo, self.run_id)
        self.assertEqual(after['dispatch_intent']['application'], before)
        with self.assertRaises(core.AssuranceError) as raised:
            self.apply()
        self.assertEqual(raised.exception.code, 'DISPATCH_APPLICATION_WORKTREE_DRIFT')
        self.assertEqual((self.candidate / 'src/calc.py').read_text(), 'unconfirmed change\n')
