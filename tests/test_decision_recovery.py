from __future__ import annotations

import copy
import unittest

from harness import cleanup_repo, init_repo
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.models import digest
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from tests.test_assurance_v4_contract import contract_for


class DecisionRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.run = 'decision-recovery'
        contract = contract_for(self.repo)
        contract['execution']['work_units'][0]['scope']['paths'] = ['src/calc.py']
        core.start(self.repo, self.run, 'decision-session', contract,
            driver_runtime={'kind': 'native', 'protocol_version': 1, 'transport': 'codex_app_server',
                            'runtime_version': 'fixture', 'protocol_schema_digest': 'a'*64})

    def tearDown(self):
        cleanup_repo(self.repo)

    def problem(self, pointer='/builder_write/-'):
        return {'key': 'authority-gap', 'owner': 'plan', 'summary': 'Missing test path',
                'details': 'Approve the exact additional Builder-owned regression file.',
                'decision_request': {'kind': 'facet_change', 'facet': 'authority',
                    'changes': [{'operation': 'add', 'pointer': pointer,
                                 'value': 'regression/check.py'}], 'question': 'Allow this path?'}}

    def record(self, problem):
        return core.record_problems(self.repo, self.run,
            {'schema_version': 1, 'problems': [problem]}, role='builder',
            agent_id='builder-agent', thread_id='builder-thread')

    def test_invalid_pointer_rejected_before_problem_is_recorded(self):
        before = read_ledger(self.repo, self.run)
        with self.assertRaises(core.AssuranceError) as error:
            self.record(self.problem('/authority/builder_write'))
        self.assertEqual(error.exception.code, 'DECISION_REQUEST_INVALID')
        self.assertEqual(read_ledger(self.repo, self.run), before)
        self.record(self.problem())
        self.assertEqual(driver.next_action(self.repo, self.run)['action'], 'contract_decision')

    def test_json_pointer_rejects_invalid_escapes_and_array_indices(self):
        for pointer in ['/a/~2', '/items/+0', '/items/01', '/items/-1']:
            with self.subTest(pointer=pointer), self.assertRaises(core.AssuranceError):
                driver._apply_decision_changes({'a': {'~2': 1}, 'items': [1, 2]},
                    [{'operation': 'replace', 'pointer': pointer, 'value': 3}])

    def decision_args(self, replacement):
        ledger = read_ledger(self.repo, self.run)
        action = driver.next_action(self.repo, self.run)
        return dict(session_id='decision-session', problem_key='authority-gap',
                    action_id=action['action_id'], facet='authority',
                    facet_digest=ledger['digests']['authority'], replacement_contract=replacement)

    def extended(self):
        ledger = read_ledger(self.repo, self.run)
        replacement = copy.deepcopy(ledger['facets'])
        replacement['authority']['builder_write'].append('regression/check.py')
        replacement['execution']['work_units'][0]['scope']['paths'].append('regression/check.py')
        return replacement

    def test_retained_invalid_request_corrects_without_overwriting_history(self):
        from unittest.mock import patch
        # Model an accepted pre-fix result using the old intake behavior only.
        with patch('runtime.codex_builder_loop.assurance_v4.decision_requests.validate_request'):
            self.record(self.problem('/authority/builder_write'))
        original = read_ledger(self.repo, self.run)
        request = original['problems'][0]['decision_request']
        action = driver.next_action(self.repo, self.run)
        args = dict(session_id='decision-session', problem_key='authority-gap',
            action_id=action['action_id'], request_digest=digest(request),
            facet_digest=original['digests']['authority'], request_value=self.problem()['decision_request'],
            reason='Correct the facet-relative address; no authority is granted.', driver_runtime_kind='native')
        core.correct_decision_request(self.repo, self.run, **args)
        corrected = read_ledger(self.repo, self.run)
        self.assertEqual(corrected['problems'], original['problems'])
        self.assertEqual(corrected['facets'], original['facets'])
        self.assertNotEqual(driver.next_action(self.repo, self.run)['action_id'], action['action_id'])
        core.correct_decision_request(self.repo, self.run, **args)
        self.assertEqual(read_ledger(self.repo, self.run), corrected)
        with self.assertRaises(core.AssuranceError):
            core.correct_decision_request(self.repo, self.run, **{**args, 'session_id': 'other'})
        with self.assertRaises(core.AssuranceError):
            driver.validate_decision(self.repo, self.run, **{**self.decision_args(self.extended()), 'action_id': action['action_id']})
        self.apply_extension()
        final = read_ledger(self.repo, self.run)
        self.assertEqual(final['problems'][0]['status'], 'resolved')
        self.assertEqual(final['problems'][0]['decision_request'], request)

    def apply_extension(self):
        replacement = self.extended()
        args = self.decision_args(replacement)
        preview = driver.validate_decision(self.repo, self.run, **args)
        self.assertEqual(preview['apply']['command'], 'apply-authority-decision')
        self.assertEqual(preview['scope_preview']['added_paths'], ['regression/check.py'])
        args.pop('facet')
        args.update(replacement_digest=preview['replacement_contract_digest'],
                    authorize_expansion=True, driver_runtime_kind='native')
        core.apply_authority_decision(self.repo, self.run, **args)
        after = read_ledger(self.repo, self.run)
        core.apply_authority_decision(self.repo, self.run, **args)
        self.assertEqual(read_ledger(self.repo, self.run), after)
        self.assertEqual(after['facets']['authority'], replacement['authority'])
        self.assertEqual(after['facets']['execution'], replacement['execution'])
        return after

    def test_authority_and_scope_are_one_transaction_preserving_dirty_candidate(self):
        from pathlib import Path
        self.record(self.problem())
        before = read_ledger(self.repo, self.run)
        candidate = Path(before['candidate_worktree'])
        (candidate/'src/calc.py').write_text('# work in progress\n')
        after = self.apply_extension()
        self.assertEqual((candidate/'src/calc.py').read_text(), '# work in progress\n')
        self.assertEqual(after['facets']['execution']['agents'], before['facets']['execution']['agents'])
        self.assertEqual(after['evidence'], before['evidence'])
        self.assertNotEqual(driver.next_action(self.repo, self.run)['action'], 'contract_decision')

    def test_partial_scope_and_unauthorized_execution_changes_are_rejected(self):
        self.record(self.problem())
        before = read_ledger(self.repo, self.run)
        replacement = self.extended()
        replacement['execution'] = copy.deepcopy(before['facets']['execution'])
        with self.assertRaises(core.AssuranceError) as error:
            driver.validate_decision(self.repo, self.run, **self.decision_args(replacement))
        self.assertEqual(error.exception.code, 'DECISION_SCOPE_INVALID')
        replacement = self.extended()
        replacement['execution']['work_units'][0]['depends_on'] = ['reviewer-work-unit']
        with self.assertRaises((core.AssuranceError, core.ContractError)):
            driver.validate_decision(self.repo, self.run, **self.decision_args(replacement))
        self.assertEqual(read_ledger(self.repo, self.run), before)

    def test_linked_scope_requires_authorization_and_rechecks_digest(self):
        self.record(self.problem())
        args = self.decision_args(self.extended()); args.pop('facet')
        args.update(replacement_digest=digest(args['replacement_contract']), driver_runtime_kind='native')
        before = read_ledger(self.repo, self.run)
        with self.assertRaises(core.AssuranceError) as error:
            core.apply_authority_decision(self.repo, self.run, authorize_expansion=False, **args)
        self.assertEqual(error.exception.code, 'AUTHORITY_EXPANSION_REQUIRES_USER')
        with self.assertRaises(core.AssuranceError):
            core.apply_authority_decision(self.repo, self.run, authorize_expansion=True,
                                         **{**args, 'replacement_digest': '0'*64})
        self.assertEqual(read_ledger(self.repo, self.run), before)

    def test_root_result_rejection_preserves_dispatch_and_dirty_work_then_accepts_correction(self):
        from pathlib import Path
        run = 'root-decision'
        contract = contract_for(self.repo)
        contract['execution'].update(driver_enforced=True, agents={},
            builder_runtime={'schema_version': 1, 'mode': 'root_session'})
        from runtime.codex_builder_loop.native_driver.cli import _root_runtime
        core.start(self.repo, run, 'decision-session', contract, driver_runtime=_root_runtime('decision-session'))
        core.prepare_builder(self.repo, run, 'codex-root-session:decision-session', owner_mode='root_session',
                             session_id='decision-session')
        action = driver.next_action(self.repo, run)
        core.begin_dispatch(self.repo, run, action_id=action['action_id'], action=action['action'],
            role='builder', thread_id=None, owner_session_id='decision-session', prompt_digest='b'*64,
            output_schema_digest='c'*64, driver_runtime_kind='native',
            work_unit_id=action.get('work_unit_id'), context_projection_digest=action.get('context_projection_digest'))
        before = read_ledger(self.repo, run)
        path = Path(before['candidate_worktree'])/'src/calc.py'
        path.write_text('# unfinished implementation\n')
        bad = {'result': 'blocked', 'evidence_report': None, 'proof_spec': None,
               'problem_report': {'schema_version': 1, 'problems': [self.problem('/authority/builder_write')]}}
        with self.assertRaises(core.AssuranceError) as error:
            core.apply_root_builder_result(self.repo, run, action_id=action['action_id'],
                owner_session_id='decision-session', result_value=bad)
        self.assertEqual(error.exception.code, 'DECISION_REQUEST_INVALID')
        self.assertEqual(read_ledger(self.repo, run), before)
        from runtime.codex_builder_loop.assurance_v4.store import run_dir
        self.assertFalse((run_dir(self.repo, run) / 'artifacts' / f"dispatch-{action['action_id']}.json").exists())
        good = copy.deepcopy(bad); good['problem_report']['problems'] = [self.problem()]
        core.apply_root_builder_result(self.repo, run, action_id=action['action_id'],
            owner_session_id='decision-session', result_value=good)
        self.assertIsNone(read_ledger(self.repo, run)['dispatch_intent'])
        self.assertEqual(path.read_text(), '# unfinished implementation\n')
        self.assertEqual(driver.next_action(self.repo, run)['action'], 'contract_decision')

    def test_scope_rejects_completed_units_and_preserves_state_on_failed_save(self):
        from unittest.mock import patch
        self.record(self.problem())
        replacement = self.extended()
        before = read_ledger(self.repo, self.run)
        with patch.object(core, '_completed_work_unit_ids', return_value={'builder-work-unit'}):
            with self.assertRaises(core.AssuranceError) as error:
                driver.validate_decision(self.repo, self.run, **self.decision_args(replacement))
        self.assertEqual(error.exception.code, 'DECISION_SCOPE_INVALID')
        args = self.decision_args(replacement); args.pop('facet')
        args.update(replacement_digest=digest(replacement), authorize_expansion=True, driver_runtime_kind='native')
        with patch.object(core, 'save_ledger', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                core.apply_authority_decision(self.repo, self.run, **args)
        self.assertEqual(read_ledger(self.repo, self.run), before)
        core.apply_authority_decision(self.repo, self.run, **args)
        self.assertEqual(read_ledger(self.repo, self.run)['problems'][0]['status'], 'resolved')

    def test_cli_correction_then_linked_authority_application(self):
        import json
        from unittest.mock import patch
        from harness import run_cli
        with patch('runtime.codex_builder_loop.assurance_v4.decision_requests.validate_request'):
            self.record(self.problem('/authority/builder_write'))
        ledger = read_ledger(self.repo, self.run)
        action = driver.next_action(self.repo, self.run)
        request_file = self.repo / 'request.json'
        request_file.write_text(json.dumps(self.problem()['decision_request']))
        result = run_cli('assurance', '--experimental-v4', 'correct-decision-request',
            '--repo', self.repo, '--run', self.run, '--session-id', 'decision-session',
            '--problem-key', 'authority-gap', '--action-id', action['action_id'],
            '--request-digest', digest(ledger['problems'][0]['decision_request']),
            '--facet-digest', ledger['digests']['authority'], '--request', request_file,
            '--reason', 'Repair facet-relative pointer', '--driver-runtime-kind', 'native')
        self.assertEqual(result.returncode, 0, result.data)
        replacement = self.extended()
        contract_file = self.repo / 'replacement.json'
        contract_file.write_text(json.dumps(replacement))
        action = driver.next_action(self.repo, self.run)
        result = run_cli('assurance', '--experimental-v4', 'apply-authority-decision',
            '--repo', self.repo, '--run', self.run, '--session-id', 'decision-session',
            '--problem-key', 'authority-gap', '--action-id', action['action_id'],
            '--facet-digest', ledger['digests']['authority'], '--contract', contract_file,
            '--replacement-digest', digest(replacement), '--authorize-expansion',
            '--driver-runtime-kind', 'native')
        self.assertEqual(result.returncode, 0, result.data)
        self.assertEqual(read_ledger(self.repo, self.run)['problems'][0]['status'], 'resolved')

    def test_correction_cannot_change_delta_or_accept_another_invalid_pointer(self):
        from unittest.mock import patch
        with patch('runtime.codex_builder_loop.assurance_v4.decision_requests.validate_request'):
            self.record(self.problem('/authority/builder_write'))
        before = read_ledger(self.repo, self.run)
        args = dict(session_id='decision-session', problem_key='authority-gap',
            action_id=driver.next_action(self.repo, self.run)['action_id'],
            request_digest=digest(before['problems'][0]['decision_request']),
            facet_digest=before['digests']['authority'], reason='Repair pointer', driver_runtime_kind='native')
        changed = self.problem()['decision_request']
        changed['changes'][0]['value'] = 'another/file.py'
        for request in [changed, self.problem('/authority/missing')['decision_request']]:
            with self.subTest(request=request), self.assertRaises(core.AssuranceError):
                core.correct_decision_request(self.repo, self.run, request_value=request, **args)
            self.assertEqual(read_ledger(self.repo, self.run), before)

    def test_scope_preflight_reports_multiple_blockers_and_rejects_pending_transaction(self):
        from runtime.codex_builder_loop.assurance_v4.decision_requests import authority_scope_preview
        ledger = read_ledger(self.repo, self.run)
        ledger['facets']['authority']['protected_support_paths'] = ['support/**']
        replacement = copy.deepcopy(ledger['facets'])
        replacement['authority']['builder_write'] += ['support/check.py', 'regression/*.py']
        with self.assertRaises(core.AssuranceError) as error:
            authority_scope_preview(ledger, replacement)
        codes = {item['code'] for item in error.exception.details['blockers']}
        self.assertTrue({'PROTECTED_OR_PUBLISHED_PATH', 'EXACT_PATH_REQUIRED',
                         'MISSING_PENDING_BUILDER_SCOPE'}.issubset(codes))
        self.record(self.problem())
        ledger = read_ledger(self.repo, self.run)
        ledger['context_rotation_intent'] = {'pending': True}
        with self.assertRaises(core.AssuranceError) as error:
            authority_scope_preview(ledger, self.extended())
        self.assertIn('TRANSACTION_PENDING', {item['code'] for item in error.exception.details['blockers']})

    def test_committed_application_replays_after_response_loss(self):
        from unittest.mock import patch
        self.record(self.problem())
        replacement = self.extended()
        args = self.decision_args(replacement)
        args.pop('facet')
        args.update(replacement_digest=digest(replacement), authorize_expansion=True,
                    driver_runtime_kind='native')
        save = core.save_ledger
        def commit_then_interrupt(*values):
            save(*values)
            raise OSError('response lost after durable commit')
        with patch.object(core, 'save_ledger', side_effect=commit_then_interrupt):
            with self.assertRaises(OSError):
                core.apply_authority_decision(self.repo, self.run, **args)
        committed = read_ledger(self.repo, self.run)
        core.apply_authority_decision(self.repo, self.run, **args)
        self.assertEqual(read_ledger(self.repo, self.run), committed)
        self.assertEqual(committed['problems'][0]['status'], 'resolved')

    def test_scope_extension_preserves_real_evidence_but_invalidates_changed_inputs(self):
        contract = contract_for(self.repo)
        first = contract['execution']['work_units'][0]
        first['scope']['paths'] = ['src/calc.py']
        followup = copy.deepcopy(first)
        followup.update(id='builder-followup', depends_on=[first['id']])
        contract['execution']['work_units'].insert(1, followup)
        self.run = 'evidence-decision'
        core.start(self.repo, self.run, 'decision-session', contract,
                   driver_runtime=read_ledger(self.repo, 'decision-recovery')['driver_runtime'])
        core.checkpoint_builder(self.repo, self.run)
        core.verify_machine(self.repo, self.run)
        self.record(self.problem())
        before = read_ledger(self.repo, self.run)
        self.assertEqual(core.evidence_state(before, 'machine'), 'pass')
        replacement = copy.deepcopy(before['facets'])
        replacement['authority']['builder_write'].append('regression/check.py')
        replacement['execution']['work_units'][1]['scope']['paths'].append('regression/check.py')
        args = self.decision_args(replacement)
        preview = driver.validate_decision(self.repo, self.run, **args)
        args.pop('facet')
        args.update(replacement_digest=digest(replacement), authorize_expansion=True,
                    driver_runtime_kind='native')
        core.apply_authority_decision(self.repo, self.run, **args)
        after = read_ledger(self.repo, self.run)
        self.assertEqual(after['evidence']['machine'], before['evidence']['machine'])
        self.assertIn('machine', preview['invalidated_evidence'])
        self.assertEqual(core.evidence_state(after, 'machine'), 'stale')
