"""Retained contracts resume real author dispatch without accepting new semantics."""
import copy
import unittest
from pathlib import Path

from tests import test_tester_result_retry as retry_fixture
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.driver_contract import AGENT_ACTION_CAPABILITIES
from runtime.codex_builder_loop.assurance_v4.role_input import build_role_input, project_role_input
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from runtime.codex_builder_loop.native_driver.core_port import CorePort


class PlanDispositionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = retry_fixture.TesterResultRetryTest()
        self.fixture.setUp()
        self.repo, self.rid = self.fixture.repo, self.fixture.rid
        self.coordinator = NativeCoordinator(repo=self.repo, run_id=self.rid, core=CorePort(),
            transport=object(), project_root=Path(__file__).resolve().parents[1])
        self.result = {'result': 'target_change_required', 'evidence_report': None, 'proof_spec': None,
            'problem_report': {'schema_version': 1, 'problems': [{
                'key': 'needs-fixture', 'owner': 'plan', 'summary': 'Need an internal API',
                'details': 'Requested a new API for a public lifecycle test.', 'decision_request': {
                    'kind': 'facet_change', 'facet': 'mission', 'changes': [
                        {'operation': 'add', 'pointer': '/interfaces/-', 'value': {
                            'id': 'extra-api', 'description': 'Expose an internal fixture API.'}}],
                    'question': 'Add an API?'}}]}}
        action = self.fixture.action
        assert self.coordinator._complete_dispatch_or_retry(action['action_id'], 'tester_author', self.result)
        self.coordinator._apply_agent_result(action, 'tester', self.result, self.coordinator._context())
        self.before = self.fixture.ledger()
        self.action = driver.next_action(self.repo, self.rid)
        self.reason = 'Use the frozen public lifecycle with isolated DB facts and a local SDK endpoint; no new API.'

    def tearDown(self):
        self.fixture.tearDown()

    def preview(self, **override):
        args = dict(session_id='owner', problem_key='needs-fixture', action_id=self.action['action_id'],
            facet='mission', facet_digest=self.before['digests']['mission'],
            replacement_contract=self.before['facets'], retain_contract=True, reason=self.reason)
        args.update(override)
        return driver.validate_decision(self.repo, self.rid, **args)

    def apply(self, **override):
        args = dict(resolve_plan_problem_key='needs-fixture', decision_action_id=self.action['action_id'],
            expected_facet_digest=self.before['digests']['mission'], owner_session_id='owner', decision_reason=self.reason)
        args.update(override)
        return core.update_facet(self.repo, self.rid, 'mission', self.before['facets']['mission'], **args)

    def test_real_cli_preview_apply_and_restarted_original_thread_delivery(self):
        port = CorePort()
        preview = port.call('validate-decision', '--repo', str(self.repo), '--run', self.rid,
            '--session-id', 'owner', '--problem-key', 'needs-fixture', '--action-id', self.action['action_id'],
            '--facet', 'mission', '--facet-digest', self.before['digests']['mission'], '--contract', '-',
            '--retain-contract', '--reason', self.reason, input_value=self.before['facets'])
        self.assertEqual(preview['status'], 'READY')
        self.assertEqual(preview['invalidated_evidence'], [])
        self.assertEqual(self.fixture.ledger(), self.before)
        port.call('update-facet', '--repo', str(self.repo), '--run', self.rid, '--facet', 'mission',
            '--value', '-', '--resolve-plan-problem-key', 'needs-fixture', '--decision-action-id', self.action['action_id'],
            '--expected-facet-digest', self.before['digests']['mission'], '--session-id', 'owner',
            '--decision-reason', self.reason, input_value=self.before['facets']['mission'])
        after = self.fixture.ledger()
        self.assertEqual(after['facets'], self.before['facets'])
        self.assertEqual(after['evidence'], self.before['evidence'])
        self.assertEqual(after['problems'][0]['details'], self.before['problems'][0]['details'])
        self.assertEqual(after['problems'][0]['status'], 'resolved')
        self.assertTrue(self.fixture.draft.exists())
        next_action = driver.next_action(self.repo, self.rid)
        self.assertEqual(next_action['action'], 'tester_author')
        captured = {}
        class Captured(BaseException): pass
        class Transport:
            def resume_thread(self, **kwargs): captured['resume'] = kwargs
            def run_turn(self, **kwargs):
                captured['turn'] = kwargs
                raise Captured()
        restarted = NativeCoordinator(repo=self.repo, run_id=self.rid, core=CorePort(),
            transport=Transport(), project_root=Path(__file__).resolve().parents[1])
        with self.assertRaises(Captured):
            restarted._run_agent_action(next_action, AGENT_ACTION_CAPABILITIES['tester_author'])
        self.assertEqual(captured['turn']['thread_id'], 'tester-thread')
        self.assertEqual(captured['turn']['cwd'], str(self.fixture.source))
        self.assertIn(self.reason, captured['turn']['prompt'])
        self.assertIn('retain_contract', captured['turn']['prompt'])
        self.assertNotIn('decision_request', captured['turn']['prompt'].split('"problems"', 1)[-1].split('"publication"', 1)[0])

    def test_missing_reason_does_not_mutate(self):
        for reason in (None, '', '   '):
            with self.assertRaises(core.AssuranceError) as e: self.preview(reason=reason)
            self.assertEqual(e.exception.code, 'DECISION_REASON_REQUIRED')
        self.assertEqual(self.fixture.ledger(), self.before)

    def test_semantic_change_cannot_be_disguised_as_retention(self):
        replacement = copy.deepcopy(self.before['facets'])
        replacement['mission']['objective'] += ' changed'
        with self.assertRaises(core.AssuranceError) as e: self.preview(replacement_contract=replacement)
        self.assertEqual(e.exception.code, 'DECISION_REPLACEMENT_DRIFT')

    def test_original_acceptance_path_still_requires_revision(self):
        with self.assertRaises(core.AssuranceError) as e: self.preview(retain_contract=False, reason=None)
        self.assertEqual(e.exception.code, 'MISSION_REVISION_BINDING_INVALID')

    def test_stale_owner_action_and_facet_rejected_before_mutation(self):
        for key, value in (('session_id', 'wrong'), ('action_id', '0' * 64), ('facet_digest', '0' * 64)):
            with self.assertRaises(core.AssuranceError): self.preview(**{key: value})
        with self.assertRaises(core.AssuranceError): self.apply(owner_session_id='wrong')
        with self.assertRaises(core.AssuranceError): self.apply(decision_action_id=None)
        self.assertEqual(self.fixture.ledger(), self.before)

    def test_pending_transaction_rejected(self):
        core.hold_run(self.repo, self.rid, session_id='owner', reason='freeze')
        with self.assertRaises(core.AssuranceError): self.preview()

    def test_double_projection_keeps_own_resolution_but_not_foreign_or_inherited(self):
        self.apply()
        context = self.coordinator._context()
        action = driver.next_action(self.repo, self.rid)
        own = context['problems'][0]
        foreign = copy.deepcopy(own); foreign['producer']['thread_id'] = 'other'
        foreign['decision_resolution']['reason'] = 'foreign candidate metadata'
        inherited = copy.deepcopy(own); inherited['inherited_from'] = {'run_id': 'old'}
        inherited['decision_resolution']['reason'] = 'old candidate metadata'
        context['problems'] += [foreign, inherited]
        view = project_role_input(build_role_input(context, action, 'tester'))
        self.assertEqual(len(view['problems']), 1)
        self.assertEqual(view['problems'][0]['decision_resolution']['reason'], self.reason)
        self.assertNotIn('decision_request', view['problems'][0])
