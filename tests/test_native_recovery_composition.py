"""Caller-selected operations preserve drafts through checkpoint/recomposition."""
import unittest
from pathlib import Path
from unittest.mock import patch
from harness import commit_all
from tests.helpers.prerequisite_recovery import PrerequisiteFixture
from runtime.codex_builder_loop.assurance_v4 import core, operations
from runtime.codex_builder_loop.assurance_v4.store import read_ledger


class NativeFixture(PrerequisiteFixture):
    def setUp(self):
        original = core.start
        def start(*args, **kwargs):
            kwargs['driver_runtime']['orchestration'] = operations.ORCHESTRATION
            return original(*args, **kwargs)
        with patch.object(core, 'start', side_effect=start):
            super().setUp(root_session=True, initial_files=['src/public_a.py', 'src/public_b.py'])

    def begin(self, role):
        ledger = read_ledger(self.repo, self.run_id)
        action_name = ('builder_implement' if not ledger['facets']['execution']['candidate_head'] else 'builder_fix') if role == 'builder' else 'tester_author'
        action = operations.operation(self.repo, self.run_id, action_name)
        core.begin_dispatch(self.repo, self.run_id, action_id=action['action_id'], action=action_name,
            role=role, thread_id='tester-thread' if role == 'tester' else None,
            owner_session_id='prerequisite-session' if role == 'builder' else None,
            prompt_digest='b' * 64, output_schema_digest='c' * 64,
            work_unit_id=action.get('work_unit_id'), context_projection_digest=action.get('context_projection_digest'))
        return action


class NativeRecoveryCompositionTest(unittest.TestCase):
    def test_blocked_checkpoint_external_resolution_preserves_same_tester_draft(self):
        f = NativeFixture()
        self.addCleanup(f.tearDown)
        with patch('runtime.codex_builder_loop.assurance_v4.driver.next_action', side_effect=AssertionError('old scheduler')):
            f.setUp()
            core.scan_doc_references(f.repo, f.run_id)
            operations.operation(f.repo, f.run_id, 'publish_prerequisites')
            core.publish_prerequisites(f.repo, f.run_id)
            core.prepare_tester(f.repo, f.run_id, 'tester', 'tester-thread')
            f.action = f.begin('tester')
            before = read_ledger(f.repo, f.run_id)
            source = Path(before['facets']['execution']['tester_source']['worktree'])
            (source / 'tests/partial.py').write_text('def test_partial():\n    assert True\n')
            draft = commit_all(source, 'test(fixture): [cr_id_skip] Preserve Independent Draft')
            def blocked(owner, key):
                return {'result':'blocked', 'evidence_report':None, 'proof_spec':None,
                        'problem_report':{'schema_version':1, 'problems':[{'key':key, 'owner':owner,
                        'summary':'Public prerequisite unavailable', 'details':'Actual prerequisite must be restored.'}]}}
            core.complete_dispatch(f.repo, f.run_id, action_id=f.action['action_id'], result_value=blocked('builder','public-api'))
            core.apply_dispatch_result(f.repo, f.run_id, action_id=f.action['action_id'], driver_runtime_kind='native')
            f.action = f.begin('builder')
            (f.candidate / 'src/public_a.py').write_text('PUBLIC_VALUE = 2\n')
            commit_all(f.candidate, 'fix(fixture): [cr_id_skip] Update Public Prerequisite')
            f.result = blocked('external_platform','environment')
            f.complete(); f.apply()
            operations.operation(f.repo, f.run_id, 'checkpoint_builder')
            core.checkpoint_builder(f.repo, f.run_id, owner_session_id='prerequisite-session')
            operations.operation(f.repo, f.run_id, 'recompose_candidate')
            core.recompose_candidate(f.repo, f.run_id)
            current = read_ledger(f.repo, f.run_id)
            with self.assertRaises(core.AssuranceError):
                operations.operation(f.repo, f.run_id, 'tester_author')
            core.resolve_external_problem(f.repo, f.run_id, problem_key='environment', reason='Fresh probe succeeded',
                expected_candidate_head=current['facets']['execution']['candidate_head'],
                owner_session_id='prerequisite-session', driver_runtime_kind='native')
            core.scan_doc_references(f.repo, f.run_id)
            operation = operations.operation(f.repo, f.run_id, 'tester_author')
            after = read_ledger(f.repo, f.run_id)
            self.assertEqual(operation['agent']['thread_id'], 'tester-thread')
            self.assertEqual(after['facets']['execution']['agents'], before['facets']['execution']['agents'])
            self.assertEqual(after['publication']['generation'], 2)
            self.assertTrue((source / 'tests/partial.py').exists())
            self.assertFalse((f.candidate / 'tests/partial.py').exists())
            self.assertNotIn('tester', after['evidence'])
            self.assertFalse(core.readiness(after)['ready'])
            self.assertIsNone(after['dispatch_intent'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([loader.loadTestsFromTestCase(NativeRecoveryCompositionTest),
                              loader.loadTestsFromTestCase(NativeProofRecoveryTest)])


class NativeProofRecoveryTest(unittest.TestCase):
    def test_proof_failure_application_replay_and_original_tester_correction(self):
        from tests.test_result_application_contract import ResultApplicationContractTest
        class Fixture(ResultApplicationContractTest):
            selected = iter(('builder_implement', 'tester_author', 'tester_proof', 'tester_proof_diagnose'))
            def begin(self, role):
                action = operations.operation(self.repo, self.run_id, next(self.selected))
                core.begin_dispatch(self.repo, self.run_id, action_id=action['action_id'], action=action['action'],
                    role=role, thread_id=role+'-thread', prompt_digest='b'*64, output_schema_digest='c'*64,
                    driver_runtime_kind='native', work_unit_id=action.get('work_unit_id'),
                    context_projection_digest=action.get('context_projection_digest'))
                return action
        f = Fixture()
        self.addCleanup(f.tearDown)
        original = core.start
        def start(*args, **kwargs):
            kwargs['driver_runtime']['orchestration'] = operations.ORCHESTRATION
            return original(*args, **kwargs)
        with patch.object(core, 'start', side_effect=start), patch(
                'runtime.codex_builder_loop.assurance_v4.driver.next_action', side_effect=AssertionError('old scheduler')):
            f.setUp()
            f.test_persisted_proof_failure_resumes_to_spec_correction_without_reintegration()
