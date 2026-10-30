from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from harness import ROOT, CLI, run_process, commit_all, git
from tests.test_native_driver_v1 import native_contract
from tests import test_result_application_contract as fixture
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.models import digest
from runtime.codex_builder_loop.assurance_v4.role_input import build_role_input, project_role_input
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator, NativeDriverError


class RoleInputBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.coordinator = NativeCoordinator(repo=ROOT, run_id='input-boundary',
            core=object(), transport=object(), project_root=ROOT)
        self.agent = {'agent_id': 'tester', 'thread_id': 'tester-thread'}
        self.context = {
            'run_id': 'input-boundary', 'facets': native_contract(ROOT),
            'target_start_head': '0' * 40, 'candidate_worktree': '/secret-candidate',
            'publication': {'required': False, 'candidate_head': 'SECRET'},
            'evidence': {'tester': {'candidate_head': 'SECRET'}},
            'evidence_origin': {'source': 'SECRET'},
            'doc_reference_scan': {'changed_paths': ['SECRET'], 'changed_definitions': ['SECRET'],
                                   'semantic_checks': ['SECRET']},
            'doc_reference_scan_state': 'pass',
            'problems': [{'owner': 'tester', 'producer': {'role': 'reviewer'}, 'details': 'SECRET'}],
            'role_observations': {'target_drift': ['SECRET'], 'local_commands': ['SECRET']},
            'progress_policy': {'mode': 'bounded_rehydration'},
        }
        self.context['facets']['authority']['dirty_intake'] = [{'path': 'SECRET'}]
        self.context['facets']['execution'].update({
            'candidate_head': 'SECRET', 'builder_files': ['SECRET'], 'dirty_snapshot': ['SECRET'],
            'carryover': {'candidate_head': 'SECRET'}, 'prior_problems': ['SECRET'],
            'agents': {'builder': {'thread_id': 'SECRET'}, 'tester': self.agent},
            'tester_source': {'base_head': '0' * 40, 'head': '2' * 40,
                'branch': 'tester/source', 'worktree': '/tester', 'files': [], 'agent': self.agent},
        })
        projection = {'facets': copy.deepcopy(self.context['facets']), 'candidate': 'SECRET',
                      'problems': ['SECRET'], 'work_unit_progress': ['SECRET']}
        self.action = {'action': 'tester_author', 'action_id': 'a' * 64,
                       'work_unit_id': 'tester-work-unit',
                       'work_unit': {'id': 'tester-work-unit', 'role': 'tester', 'objective': 'Write tests'},
                       'context_projection': projection, 'context_projection_digest': digest(projection)}

    def prompt(self, action=None, context=None, role='tester'):
        return json.loads(self.coordinator._prompt(action or self.action, role,
                          context or self.context).split('\n', 1)[1])

    def test_author_final_wire_excludes_all_candidate_channels(self):
        for action in ['tester_author', 'tester_fix']:
            with self.subTest(action=action):
                result = self.prompt({**self.action, 'action': action})
                self.assertNotIn('SECRET', json.dumps(result))
                for field in ('candidate_worktree', 'canonical_projection', 'context_projection_digest',
                              'evidence', 'evidence_origin', 'role_observations', 'doc_reference_scan'):
                    self.assertNotIn(field, result)
                self.assertEqual(result['tester_worktree'], '/tester')
                self.assertEqual(result['work_unit']['objective'], 'Write tests')
                self.assertEqual(result['contract']['mission'], self.context['facets']['mission'])
                self.assertIn('evidence_report_schema', result)

    def test_candidate_only_changes_do_not_change_author_wire(self):
        original = self.prompt()
        changed = json.loads(json.dumps(self.context).replace('SECRET', 'OTHER-CANDIDATE'))
        changed['candidate_worktree'] = '/other-candidate'
        projection = {'candidate': 'another-head', 'work_unit_progress': ['other-progress']}
        action = {**self.action, 'context_projection': projection, 'context_projection_digest': digest(projection)}
        self.assertEqual(self.prompt(action, changed), original)

    def test_required_publication_preserves_exact_manifest_without_source_candidate(self):
        publication = {'required': True, 'generation': 2, 'paths': ['public/api.py'],
                       'head': '3' * 40, 'tree': '4' * 40, 'files': [{'path': 'public/api.py', 'blob': '5' * 40}],
                       'manifest_digest': '6' * 64, 'candidate_head': 'SECRET', 'extra': 'SECRET'}
        self.context['publication'] = publication
        result = self.prompt()
        self.assertEqual(result['publication'], {k: v for k, v in publication.items() if k not in {'candidate_head', 'extra'}})
        self.assertNotIn('SECRET', json.dumps(result))
        publication['candidate_head'] = 'OTHER'
        self.assertEqual(result, self.prompt())
        publication['files'][0]['blob'] = '7' * 40
        self.assertNotEqual(result['role_input_digest'], self.prompt()['role_input_digest'])

    def test_author_accepts_only_same_producer_problem_and_not_ledger_candidate_metadata(self):
        own = {'key': 'coverage', 'owner': 'tester', 'producer': {'role': 'tester', **self.agent},
               'status': 'open', 'summary': 'Write remaining tests', 'details': 'Boundary tests pending',
               'candidate_head': 'SECRET', 'extra': 'SECRET'}
        self.context['problems'].extend([own, {**own, 'status': 'resolved', 'producer': {'role': 'tester', 'thread_id': 'old'}}])
        result = self.prompt()
        self.assertEqual(len(result['problems']), 1)
        self.assertEqual(result['problems'][0]['details'], own['details'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_foreign_open_correction_fails_before_send_instead_of_disappearing(self):
        self.context['problems'] = [{'key': 'reviewer-finding', 'status': 'open',
            'owner': 'tester', 'producer': {'role': 'reviewer'}, 'details': 'SECRET'}]
        with self.assertRaises(NativeDriverError) as caught:
            self.prompt({**self.action, 'action': 'tester_fix'})
        self.assertEqual(caught.exception.code, 'TESTER_CORRECTION_INPUT_UNSAFE')
        self.assertEqual(caught.exception.status, 'NEEDS_USER')
        self.assertEqual(caught.exception.details['problem_keys'], ['reviewer-finding'])
        self.assertNotIn('SECRET', str(caught.exception))

    def test_projection_is_pure_idempotent_and_binds_visible_fields(self):
        before = copy.deepcopy(self.context)
        result = build_role_input(self.context, self.action, 'tester')
        self.assertEqual(project_role_input(result), result)
        self.assertEqual(self.context, before)
        self.assertEqual(digest({k: v for k, v in result.items() if k != 'role_input_digest'}), result['role_input_digest'])
        self.context['facets']['mission']['objective'] = 'Changed objective'
        self.assertNotEqual(result['role_input_digest'], build_role_input(self.context, self.action, 'tester')['role_input_digest'])

    def test_internal_projection_digest_is_still_verified(self):
        self.action['context_projection']['candidate'] = 'tampered'
        with self.assertRaises(NativeDriverError) as caught:
            self.prompt()
        self.assertEqual(caught.exception.code, 'NATIVE_CONTEXT_PROJECTION_DIGEST_MISMATCH')

    def test_reviewer_keeps_candidate_scan_and_full_diff(self):
        result = self.prompt({**self.action, 'action': 'reviewer_final'}, role='reviewer')
        self.assertEqual(result['doc_reference_scan'], self.context['doc_reference_scan'])
        self.assertEqual(result['contract'], self.context['facets'])
        self.assertEqual(result['review_input_contract']['complete_diff']['cwd'], '/secret-candidate')

    def test_tester_later_phases_keep_required_facts_but_not_doc_scan(self):
        for action in ('tester_observe', 'tester_blackbox', 'tester_machine_diagnose', 'tester_recompose_fix'):
            with self.subTest(action=action):
                result = self.prompt({**self.action, 'action': action, 'machine_failure': {'failure_signature': 'bound'}})
                self.assertNotIn('doc_reference_scan', result)
                self.assertEqual(result['candidate_worktree'], '/secret-candidate')
                if action == 'tester_machine_diagnose':
                    self.assertEqual(result['machine_failure']['failure_signature'], 'bound')

    def test_retry_feedback_cannot_reintroduce_candidate_error_details(self):
        pending = {'action': 'tester_author', 'action_id': self.action['action_id'],
                   'failure_code': 'DECISION_REQUEST_INVALID', 'attempt': 2, 'generation': 1,
                   'failure_details': {'cause': 'DECISION_CHANGES_REQUIRED', 'problem_key': 'coverage',
                       'details': {'candidate': 'SECRET'}, 'expected': 'SECRET', 'error': 'SECRET'}}
        base = self.coordinator._prompt(self.action, 'tester', self.context)
        retried = self.coordinator._retry_prompt(base, pending)
        self.assertNotIn('SECRET', retried)
        self.assertIn('DECISION_CHANGES_REQUIRED', retried)
        self.assertIn('coverage', retried)
        self.assertTrue(retried.startswith(base))
        restarted = NativeCoordinator(repo=ROOT, run_id='input-boundary', core=object(), transport=object(), project_root=ROOT)
        self.assertEqual(restarted._retry_prompt(base, pending), retried)


class RoleInputCLIContractTest(unittest.TestCase):
    setUp = fixture.ResultApplicationContractTest.setUp
    tearDown = fixture.ResultApplicationContractTest.tearDown
    begin = fixture.ResultApplicationContractTest.begin
    apply = fixture.ResultApplicationContractTest.apply

    def test_role_context_cli_matches_native_common_brief_and_leaves_ledger_unchanged(self):
        self.apply()
        core.scan_doc_references(self.repo, self.run_id)
        core.prepare_tester(self.repo, self.run_id, 'tester', 'tester-thread')
        action = driver.next_action(self.repo, self.run_id)
        before = read_ledger(self.repo, self.run_id)
        result = run_process([str(CLI), 'assurance', '--experimental-v4', 'role-context',
                              '--repo', str(self.repo), '--run', self.run_id, '--action-id', action['action_id']])
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        expected = build_role_input(core.driver_context(self.repo, self.run_id), action, 'tester')
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertNotIn('doc_reference_scan', json.loads(result.stdout))
        self.assertNotIn('candidate_head', json.loads(result.stdout)['contract']['execution'])
        self.assertEqual(read_ledger(self.repo, self.run_id), before)
        stale = run_process([str(CLI), 'assurance', '--experimental-v4', 'role-context',
                             '--repo', str(self.repo), '--run', self.run_id, '--action-id', 'stale'])
        self.assertNotEqual(stale.returncode, 0)
        self.assertEqual(json.loads(stale.stdout)['code'], 'ROLE_INPUT_ACTION_MISMATCH')

    def test_author_source_head_report_integrates_without_knowing_candidate_head(self):
        self.apply()
        core.scan_doc_references(self.repo, self.run_id)
        core.prepare_tester(self.repo, self.run_id, 'tester', 'tester-thread')
        self.action = self.begin('tester')
        context = core.driver_context(self.repo, self.run_id)
        brief = build_role_input(context, self.action, 'tester')
        source = Path(brief['tester_worktree'])
        path = 'tests/test_public_behavior.py'
        (source / path).write_text('def test_add():\n    from src.calc import add\n    assert add(2, 3) == 5\n')
        head = commit_all(source, 'author public behavior')
        report = {'result': 'tests_ready', 'problem_report': None, 'proof_spec': None,
                  'evidence_report': {'schema_version': 1, 'kind': 'tester', 'status': 'pass',
                      'candidate_head': head,
                      'producer': {'role': 'tester', **brief['contract']['execution']['agents']['tester']},
                      'details': {'result': 'tests_ready', 'source_head': head,
                                  'files': [{'path': path, 'blob': git(source, 'rev-parse', head + ':' + path)}]}}}
        self.assertNotEqual(head, context['facets']['execution']['candidate_head'])
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action['action_id'], result_value=report)
        self.apply()
        ledger = read_ledger(self.repo, self.run_id)
        self.assertEqual(core.evidence_state(ledger, 'tester'), 'pass')
        self.assertEqual(ledger['evidence']['tester']['candidate_head'], ledger['facets']['execution']['candidate_head'])
        self.assertEqual(ledger['evidence']['tester']['details']['source_head'], head)
