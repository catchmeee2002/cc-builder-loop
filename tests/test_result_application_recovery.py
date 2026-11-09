"""Evidence consumption failures stay local; legacy terminal recovery is bound."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_local_command_observations import review_fixture
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.models import digest


class ApplicationRecoveryTest(unittest.TestCase):
    def failed_application(self, *, terminal=False):
        f = review_fixture(self, early_review=True)
        action = f.begin('reviewer', 'reviewer_preflight')
        report = f.evidence('reviewer_preflight', dict(result='pass',
            reviewed_head=f.ledger()['facets']['execution']['candidate_head']))
        result = f.envelope('pass', report)
        f.complete(action, result)
        with patch.dict(os.environ, f.environment), patch.object(core, 'record_evidence',
                side_effect=core.AssuranceError('injected consumption defect', code='FIXTURE_EVIDENCE_DEFECT')):
            with self.assertRaises(core.AssuranceError) as raised:
                core.apply_dispatch_result(f.repo, f.run, action_id=action['action_id'], driver_runtime_kind=f.kind)
        self.assertEqual(raised.exception.status, 'NEEDS_USER')
        ledger = f.ledger()
        self.assertEqual(ledger['phase'], 'active')
        self.assertIsNone(ledger.get('driver_failure'))
        self.assertEqual(ledger['dispatch_intent']['application']['effects'], [])
        if terminal:
            failure = dict(source='native_driver', status='FATAL', code='FIXTURE_EVIDENCE_DEFECT',
                message='injected consumption defect', details={},
                action={k:action[k] for k in ('action','action_id','reason')})
            f.call('record-driver-failure', '--failure', f.json_file(failure), '--driver-runtime-kind', f.kind)
            f.call('complete-driver-failure', '--driver-runtime-kind', f.kind)
            self.assertEqual(f.ledger()['phase'], 'failed')
        return f, action, digest(result)

    def recover_args(self, action, expected_digest, **overrides):
        values = dict(session_id='observation-session', action_id=action['action_id'],
                      result_digest=expected_digest, reason='Repair evidence consumer', driver_runtime_kind='native')
        values.update(overrides)
        return [v for k,value in values.items() for v in ('--'+k.replace('_','-'), value)]

    def test_local_failure_replays_original_result_without_role_redispatch(self):
        f, action, _ = self.failed_application()
        original = f.ledger()['dispatch_intent']
        artifact = Path(original['result_path']).read_bytes()
        f.apply(action)
        self.assertIsNone(f.ledger()['dispatch_intent'])
        self.assertEqual(Path(original['result_path']).read_bytes(), artifact)
        self.assertEqual(f.ledger()['phase'], 'active')
        self.assertEqual(f.next()['action'], 'verify_machine')

    def test_legacy_recovery_preserves_history_and_rejects_input_drift(self):
        f, action, result_digest = self.failed_application(terminal=True)
        original = f.ledger()
        args = self.recover_args(action, result_digest)
        for override in ({'session_id':'wrong-owner'}, {'result_digest':'0'*64}, {'action_id':'1'*64}):
            rc, _ = f.raw('recover-result-application', *self.recover_args(action, result_digest, **override))
            self.assertNotEqual(rc, 0)
            self.assertEqual(f.ledger(), original)
        path = f.candidate / 'unrelated.txt'
        path.write_text('leave this file alone')
        rc, _ = f.raw('recover-result-application', *args)
        self.assertNotEqual(rc, 0)
        self.assertEqual(path.read_text(), 'leave this file alone')
        self.assertEqual(f.ledger(), original)
        path.unlink()
        for root in (f.repo, Path(original['facets']['execution']['tester_source']['worktree'])):
            path = root / 'recovery-drift.txt'
            path.write_text('unrelated user data')
            rc, _ = f.raw('recover-result-application', *args)
            self.assertNotEqual(rc, 0)
            self.assertEqual(f.ledger(), original)
            self.assertEqual(path.read_text(), 'unrelated user data')
            path.unlink()
        artifact = Path(original['dispatch_intent']['result_path'])
        content = artifact.read_bytes()
        artifact.write_text('{}')
        rc, _ = f.raw('recover-result-application', *args)
        self.assertNotEqual(rc, 0)
        self.assertEqual(f.ledger(), original)
        artifact.write_bytes(content)
        f.call('recover-result-application', *args)
        recovered = f.ledger()
        self.assertEqual(recovered['phase'], 'active')
        self.assertEqual(recovered['facets'], original['facets'])
        self.assertEqual(recovered['evidence'], original['evidence'])
        receipt = [e['details'] for e in recovered['events'] if e['kind']=='result_application_recovered'][-1]
        self.assertEqual(receipt['previous_driver_failure'], original['driver_failure'])
        self.assertEqual(recovered['events'][:len(original['events'])], original['events'])
        f.call('recover-result-application', *args)
        self.assertEqual(recovered, f.ledger())
        f.apply(action)
        self.assertEqual(f.next()['action'], 'verify_machine')
        f.call('recover-result-application', *args)
        self.assertIsNone(f.ledger()['dispatch_intent'])

    def test_partial_effects_require_original_transaction_not_reopen(self):
        f = review_fixture(self, early_review=True)
        action = f.begin('reviewer', 'reviewer_preflight')
        report = f.evidence('reviewer_preflight', dict(result='pass',
            reviewed_head=f.ledger()['facets']['execution']['candidate_head']))
        result = f.envelope('pass', report)
        f.complete(action, result)
        record = core.record_evidence
        def after_save(*args, **kwargs):
            record(*args, **kwargs)
            raise core.AssuranceError('interrupted after evidence save', code='FIXTURE_AFTER_SAVE')
        with patch.dict(os.environ, f.environment), patch.object(core, 'record_evidence', side_effect=after_save):
            with self.assertRaises(core.AssuranceError):
                core.apply_dispatch_result(f.repo, f.run, action_id=action['action_id'], driver_runtime_kind=f.kind)
        before = f.ledger()
        self.assertEqual(before['dispatch_intent']['application']['effects'], ['evidence:reviewer_preflight'])
        rc, _ = f.raw('recover-result-application', *self.recover_args(action, digest(result)))
        self.assertNotEqual(rc, 0)
        self.assertEqual(before, f.ledger())
        f.apply(action)
        self.assertEqual(f.next()['action'], 'verify_machine')

    def test_runtime_transition_is_explicit_clean_and_keeps_original_bindings(self):
        from runtime.codex_builder_loop import core as runtime_core
        from runtime.codex_builder_loop.assurance_v4.result_application import recover
        f, action, result_digest = self.failed_application(terminal=True)
        before = f.ledger()
        current = dict(before['runtime_identity'], adapter_commit='b'*40, adapter_dirty=False)
        kwargs = dict(session_id='observation-session', action_id=action['action_id'],
                      result_digest=result_digest, reason='Repair evidence consumer', driver_runtime_kind=f.kind)
        with patch.dict(os.environ, f.environment), patch.object(runtime_core, 'capture_runtime_identity', return_value=current):
            with self.assertRaises(core.AssuranceError):
                recover(f.repo, f.run, **kwargs)
            self.assertEqual(before, f.ledger())
            with patch.object(runtime_core, 'capture_runtime_identity', return_value=dict(current, adapter_dirty=True)):
                with self.assertRaises(core.AssuranceError):
                    recover(f.repo, f.run, **kwargs, allow_runtime_transition=True)
                self.assertEqual(before, f.ledger())
            recover(f.repo, f.run, **kwargs, allow_runtime_transition=True)
            after = f.ledger()
            self.assertEqual(after['runtime_identity'], current)
            self.assertEqual(after['dispatch_intent']['result_digest'], result_digest)
            for key in ('projection_digest', 'observation_digest', 'effects'):
                self.assertEqual(after['dispatch_intent']['application'][key], before['dispatch_intent']['application'][key])
            core.apply_dispatch_result(f.repo, f.run, action_id=action['action_id'], driver_runtime_kind=f.kind)
            self.assertIsNone(f.ledger()['dispatch_intent'])
