from copy import deepcopy
import unittest
from harness import commit_all
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from tests.helpers import prerequisite_recovery as helpers
from tests.helpers.native_delivery_recovery import complete_delivery, kill_after_saved_event
from harness import ROOT
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from runtime.codex_builder_loop.native_driver.core_port import CorePort


class PrerequisiteRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.f = helpers.PrerequisiteFixture()
        self.addCleanup(self.f.tearDown)
        self.f.setUp()

    def ledger(self):
        return read_ledger(self.f.repo, self.f.run_id)

    def next(self, ledger=None):
        return driver.next_action(self.f.repo, self.f.run_id, _ledger=ledger)

    def test_all_missing_then_partial_then_ready_stays_on_original_builder(self):
        before = self.ledger()
        action = self.next()
        self.assertEqual(action['reason'], 'authorized_prerequisite_completion')
        self.assertEqual(action['public_prerequisite_recovery']['completed'], 0)
        self.f.complete_builder(['src/public_a.py'])
        self.f.apply()
        action = self.next()
        self.assertEqual(action['public_prerequisite_recovery']['paths'], ['src/public_b.py'])
        self.assertEqual(action['public_prerequisite_recovery']['completed'], 1)
        self.assertEqual(self.ledger()['facets']['execution']['agents']['builder'], before['facets']['execution']['agents']['builder'])
        self.f.complete_builder(['src/public_b.py'])
        self.f.apply()
        ledger = self.ledger()
        self.assertTrue(ledger['builder_checkpointed'])
        self.assertFalse(any(p['status'] == 'open' for p in ledger['problems']))
        self.assertEqual(ledger['facets']['mission'], before['facets']['mission'])
        self.assertIsNone(ledger['publication']['head'])
        core.scan_doc_references(self.f.repo, self.f.run_id)
        self.assertEqual(self.next()['action'], 'publish_prerequisites')
        core.publish_prerequisites(self.f.repo, self.f.run_id)
        # Existing fixture authors/integrates source and proves it against baseline.
        self.f.prepare_tester_result()
        self.f.apply()
        core.scan_doc_references(self.f.repo, self.f.run_id)
        self.f.action = self.f.begin('tester')
        self.f.result = self.f.proof_result()
        core.complete_dispatch(self.f.repo, self.f.run_id, action_id=self.f.action['action_id'], result_value=self.f.result)
        self.f.apply()
        complete_delivery(self.f)

    def test_three_consumed_corrections_stop_and_queries_do_not_reset_budget(self):
        for n in range(3):
            self.assertEqual(self.next()['public_prerequisite_recovery']['completed'], n)
            self.f.complete_builder()
            self.f.apply()
            self.f.apply(self.f.result)
        before = self.ledger()
        for _ in range(3):
            action = self.next()
            self.assertEqual(action['status'], 'NEEDS_USER')
            self.assertEqual(action['action'], 'architecture_review')
            self.assertEqual(action['public_prerequisite_recovery']['completed'], 3)
        self.assertEqual(self.ledger(), before)
        (self.f.candidate / 'src/unrelated.py').write_text('UNRELATED = 1\n')
        commit_all(self.f.candidate, 'unrelated candidate advance')
        self.assertEqual(self.next()['action'], 'architecture_review')

    def test_saved_partial_checkpoint_recovers_exactly_once(self):
        self.f.complete_builder(['src/public_a.py'])
        kill_after_saved_event(self.f, 'public_prerequisite_observation_replaced')
        self.f.apply()
        self.f.apply(self.f.result)
        action = self.next()
        self.assertEqual(action['public_prerequisite_recovery']['completed'], 1)
        self.assertEqual(sum(e['kind'] == 'public_prerequisite_observation_replaced' for e in self.ledger()['events']), 1)

    def test_unbound_checkpoint_cannot_close_or_refresh_problem(self):
        (self.f.candidate / 'src/public_a.py').write_text('VALUE = 1\n')
        commit_all(self.f.candidate, 'unbound write')
        before = self.ledger()
        with self.assertRaises(core.AssuranceError) as raised:
            core.checkpoint_builder(self.f.repo, self.f.run_id)
        self.assertEqual(raised.exception.code, 'PUBLIC_PREREQUISITE_RECOVERY_UNVERIFIED')
        self.assertEqual(self.ledger(), before)

    def test_unknown_or_changed_observations_do_not_auto_route(self):
        for mutate in ('authority', 'unit', 'builder', 'observation', 'other_problem'):
            ledger = deepcopy(self.ledger())
            if mutate == 'authority':
                ledger['facets']['authority']['builder_write'] = ['src/calc.py']
            elif mutate == 'unit':
                ledger['facets']['execution']['work_units'][0]['scope']['paths'] = ['src/calc.py']
            elif mutate == 'builder':
                ledger['facets']['execution']['agents']['builder']['thread_id'] = 'other'
            elif mutate == 'observation':
                ledger['problems'][0]['details'] = '{}'
            else:
                ledger['problems'].append({**ledger['problems'][0], 'key': 'missing-interface', 'owner': 'plan'})
            with self.subTest(mutate=mutate):
                self.assertEqual(self.next(ledger)['status'], 'NEEDS_USER')

    def test_other_problems_keep_owner_routing_and_remain_visible(self):
        for owner, expected in (
            ('plan', 'contract_decision'), ('external_platform', 'external_problem_decision'),
            ('current_project', 'current_project_problem_decision'),
            ('builder_loop', 'builder_loop_problem_decision'),
            ('builder', 'builder_loop_problem_decision'), ('tester', 'builder_loop_problem_decision'),
        ):
            with self.subTest(owner=owner):
                fixture = helpers.PrerequisiteFixture()
                self.addCleanup(fixture.tearDown)
                fixture.setUp()
                key = 'other-' + owner.replace('_', '-')
                core.record_problems(fixture.repo, fixture.run_id, {
                    'schema_version': 1, 'problems': [{
                        'key': key, 'owner': owner, 'summary': 'Other blocking problem',
                        'details': 'The prerequisite classification cannot hide this problem.',
                    }],
                }, role='builder', agent_id='builder', thread_id='builder-thread')
                before = read_ledger(fixture.repo, fixture.run_id)
                action = driver.next_action(fixture.repo, fixture.run_id)
                self.assertEqual(action['status'], 'NEEDS_USER')
                self.assertEqual(action['action'], expected)
                visible = [action['problem'], *action['other_open_problems']]
                self.assertIn(key, [p['key'] for p in visible])
                if owner not in {'builder', 'tester'}:
                    self.assertEqual(action['problem']['key'], key)
                self.assertEqual(read_ledger(fixture.repo, fixture.run_id), before)

    def test_third_correction_can_complete(self):
        self.f.complete_builder()
        self.f.apply()
        self.f.complete_builder()
        self.f.apply()
        self.f.complete_builder(['src/public_a.py', 'src/public_b.py'])
        self.f.apply()
        self.assertTrue(self.ledger()['builder_checkpointed'])
        self.assertNotEqual(self.next()['action'], 'architecture_review')

    def test_rejection_cause_is_preserved_without_mutating_contract(self):
        self.f.complete_builder(['src/public_a.py', 'src/public_b.py'])
        self.f.apply()
        report = {'schema_version': 1, 'problems': [{
            'key': 'machine-command-replacement', 'owner': 'plan',
            'summary': 'Change the command', 'details': 'An engineering correction proposal.',
            'decision_request': {'kind': 'engineering_correction', 'facet': 'assurance',
                'changes': [{'operation': 'replace', 'pointer': '/assurance/machine_commands/0/argv',
                             'value': ['python3', '-m', 'unittest']}], 'question': 'Approve the change?'},
        }]}
        core.record_problems(self.f.repo, self.f.run_id, report, role='builder', agent_id='builder', thread_id='builder-thread')
        before = self.ledger()
        decision = self.next()
        self.assertEqual(decision['status'], 'NEEDS_USER')
        reason = decision['engineering_correction_rejection']
        self.assertEqual(reason['code'], 'ENGINEERING_CORRECTION_COMMAND_REPLACED')
        self.assertEqual(reason['details']['command_id'], 'fixture-tests')
        self.assertIn(reason['code'], decision['required_user_block'])
        self.assertEqual(decision, self.next())
        self.assertEqual(before, self.ledger())
        # Candidate drift and malformed proposals remain rejected with their own cause.
        for pointer, value, code in [
            ('/assurance/machine_commands/0/timeout_seconds', 30, 'ENGINEERING_CORRECTION_NOOP'),
            ('/assurance/no-such-field/x', 3, 'DECISION_POINTER_MISSING'),
        ]:
            probe = deepcopy(before)
            problem = next(p for p in probe['problems'] if p['key'] == 'machine-command-replacement')
            problem['decision_request']['changes'] = [{'operation': 'replace', 'pointer': pointer, 'value': value}]
            response = self.next(probe)
            self.assertEqual(response['status'], 'NEEDS_USER')
            self.assertEqual(response['engineering_correction_rejection']['code'], code)
        (self.f.candidate / 'src/calc.py').write_text('DIRTY = True\n')
        response = self.next()
        self.assertEqual(response['engineering_correction_rejection']['code'], 'ENGINEERING_CORRECTION_CANDIDATE_INVALID')

    def test_lower_limit_and_manual_policy_keep_their_boundaries(self):
        fixture = helpers.PrerequisiteFixture()
        self.addCleanup(fixture.tearDown)
        # The current schema freezes attempt_limit=3. A stricter value cannot
        # be admitted; the derived min remains defensive for future contracts.
        with self.assertRaisesRegex(ValueError, '3 was expected'):
            fixture.setUp(attempt_limit=1)
        self.f.complete_builder()
        self.f.apply()
        ledger = deepcopy(self.ledger())
        ledger['facets']['execution']['progress_policy']['attempt_limit'] = 1
        action = self.next(ledger)
        self.assertEqual(action['action'], 'architecture_review')
        self.assertEqual(action['public_prerequisite_recovery']['limit'], 1)
        fixture = helpers.PrerequisiteFixture()
        self.addCleanup(fixture.tearDown)
        fixture.setUp(mode='manual')
        action = driver.next_action(fixture.repo, fixture.run_id)
        self.assertEqual(action['status'], 'NEEDS_USER')
        self.assertEqual(action['action'], 'builder_loop_problem_decision')

    def test_manual_partial_readiness_keeps_existing_builder_continuation(self):
        fixture = helpers.PrerequisiteFixture()
        self.addCleanup(fixture.tearDown)
        fixture.setUp(mode='manual', initial_files=['src/public_a.py'])
        action = driver.next_action(fixture.repo, fixture.run_id)
        self.assertEqual(action['action'], 'builder_implement')
        fixture.complete_builder(['src/public_b.py'])
        fixture.apply()
        self.assertTrue(read_ledger(fixture.repo, fixture.run_id)['builder_checkpointed'])

    def test_root_builder_handoff_exposes_bound_recovery_and_completes(self):
        fixture = helpers.PrerequisiteFixture()
        self.addCleanup(fixture.tearDown)
        fixture.setUp(root_session=True)
        coordinator = NativeCoordinator(repo=fixture.repo, run_id=fixture.run_id, core=CorePort(),
            transport=None, project_root=ROOT, builder_mode='root_session',
            root_session_id='prerequisite-session')
        action = driver.next_action(fixture.repo, fixture.run_id)
        handoff = coordinator._root_builder_handoff(action)
        self.assertEqual(handoff['public_prerequisite_recovery'], action['public_prerequisite_recovery'])
        fixture.complete_builder(['src/public_a.py'])
        fixture.apply()
        self.assertEqual(driver.next_action(fixture.repo, fixture.run_id)['public_prerequisite_recovery']['completed'], 1)
        fixture.complete_builder(['src/public_b.py'])
        fixture.apply()
        ledger = read_ledger(fixture.repo, fixture.run_id)
        self.assertTrue(ledger['builder_checkpointed'])
        self.assertEqual(ledger['facets']['execution']['agents']['builder']['session_id'], 'prerequisite-session')

    def test_new_dirty_or_uncheckpointed_head_stops_instead_of_starting_builder(self):
        (self.f.candidate / 'src/unrelated.py').write_text('UNRELATED = 1\n')
        self.assertEqual(self.next()['reason'], 'prerequisite_candidate_dirty')
        commit_all(self.f.candidate, 'unbound candidate advance')
        self.assertEqual(self.next()['reason'], 'prerequisite_candidate_drift')
