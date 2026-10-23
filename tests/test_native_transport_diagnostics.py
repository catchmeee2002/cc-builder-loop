"""Failure observations must survive cleanup and must not change dispatch policy."""
from __future__ import annotations

import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import jsonschema
from unittest.mock import Mock, patch

from runtime.codex_builder_loop.native_driver.app_server import AppServerTransport, AppServerError, TurnResult
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator


class ReceiveDiagnosticsTest(unittest.TestCase):
    def transport(self):
        return AppServerTransport(executable_identity={'fixture': True})

    def test_timeout_retains_partial_bytes_and_cleanup_order_without_body(self):
        t = self.transport()
        readfd, writefd = os.pipe()
        stream = os.fdopen(readfd, 'rb', buffering=0)
        secret = b'{"body":"private-tool-text"'
        os.write(writefd, secret)
        t.process = SimpleNamespace(stdout=stream, stderr=None, stdin=io.BytesIO(), pid=99)
        t._process_identity = {'pgid': 99}
        try:
            with self.assertRaises(AppServerError):
                t._read(timeout=.02)
            cleanup = dict(process_group_gone=True, state='cleaned', term_attempt=1,
                           kill_attempt=0, returncode=0)
            with patch('runtime.codex_builder_loop.native_driver.app_server.reap_process_group', return_value=cleanup):
                t.close()
            receipt = t.diagnostic_receipt(failure_code='responseStreamTimeout')
            self.assertEqual(receipt['receive']['partial_bytes'], len(secret))
            self.assertEqual(receipt['receive']['partial_sha256'], hashlib.sha256(secret).hexdigest())
            self.assertEqual(receipt['receive']['received_bytes'], len(secret))
            self.assertEqual([e['kind'] for e in receipt['lifecycle_tail']], ['close_started', 'close_completed'])
            self.assertNotIn('private-tool-text', json.dumps(receipt))
            self.assertEqual(t._read_buffer, b'')
            self.assertLessEqual(receipt['receive']['offset_seconds'], receipt['lifecycle_tail'][0]['offset_seconds'])
        finally:
            stream.close()
            os.close(writefd)

    def test_broken_snapshot_does_not_prevent_process_cleanup(self):
        t = self.transport()
        t.process = SimpleNamespace(stdout=io.BytesIO(), stderr=None, stdin=io.BytesIO(), pid=99)
        t._process_identity = {'pgid': 99}
        t.receive_snapshot = Mock(side_effect=ValueError('bad diagnostic'))
        cleanup = dict(process_group_gone=True, state='cleaned', term_attempt=1,
                       kill_attempt=0, returncode=0)
        with patch('runtime.codex_builder_loop.native_driver.app_server.reap_process_group', return_value=cleanup) as reap:
            t.close()
        reap.assert_called_once()
        self.assertTrue(t.cleanup_observation['process_group_gone'])
        self.assertEqual(t.diagnostic_receipt(failure_code='timeout')['receive'],
                         {'diagnostic_error': 'ValueError'})

    def test_frames_and_counters_are_bounded_and_body_free(self):
        t = self.transport()
        message = {'method': 'item/completed', 'params': {'threadId': 'thread',
                   'item': {'type': 'agentMessage', 'text': 'sensitive result'}}}
        t._read_buffer = ((json.dumps(message) + '\n') * 200).encode()
        t.process = SimpleNamespace(stdout=io.BytesIO())
        for _ in range(200):
            t._read()
        snapshot = t.receive_snapshot()
        self.assertEqual(snapshot['wire']['sequence'], 200)
        self.assertEqual(len(snapshot['events']), 128)
        self.assertGreater(snapshot['dropped_events'], 0)
        self.assertNotIn('sensitive result', json.dumps(snapshot))
        last = snapshot['events'][-1]
        self.assertEqual(last['method'], 'item/completed')
        self.assertEqual(last['thread_id'], 'thread')
        self.assertEqual(last['sequence'], 200)

    def test_strict_ledger_accepts_legacy_and_extended_receipts(self):
        schema = json.loads((Path(__file__).resolve().parents[1] /
                             'schema/assurance-v4-ledger.schema.json').read_text())
        receipt_schema = {'$defs': schema['$defs'], '$ref': '#/$defs/diagnosticReceipt'}
        receipt = self.transport().diagnostic_receipt(failure_code='timeout')
        jsonschema.validate(receipt, receipt_schema)
        legacy = {k: v for k, v in receipt.items() if k not in {'receive', 'lifecycle_tail'}}
        jsonschema.validate(legacy, receipt_schema)
        receipt['receive']['events'] = [{'kind':'fixture', 'offset_seconds':0}] * 129
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(receipt, receipt_schema)

    def test_long_metadata_credentials_are_redacted_before_truncation(self):
        token = 'eyj' + 'a' * 200 + '.' + 'b' * 100 + '.' + 'c' * 100
        self.assertEqual(self.transport()._metadata(token), '[REDACTED]')

    def test_receipt_redacts_error_and_stderr_and_keeps_legacy_fields(self):
        t = self.transport()
        t._stderr_buffer.extend(b'Authorization: Bearer fixture-secret')
        r = t.diagnostic_receipt(failure_code='timeout', turn_error={'message':'password=fixture-password'})
        encoded = json.dumps(r)
        self.assertNotIn('fixture-secret', encoded)
        self.assertNotIn('fixture-password', encoded)
        for key in ('schema_version', 'stderr_bytes', 'stderr_sha256', 'stderr_summary',
                    'stderr_truncated', 'turn_error', 'cleanup_observation', 'receipt_digest'):
            self.assertIn(key, r)


class FailurePropagationTest(unittest.TestCase):
    def coordinator(self):
        c = NativeCoordinator.__new__(NativeCoordinator)
        c.repo, c.run_id = Path('/fixture'), 'run'
        c.current_action = {'action': 'tester_author', 'action_id': 'action'}
        pending = {**c.current_action, 'role': 'tester', 'state': 'in_flight', 'thread_id': 'thread',
                   'turn_id': 'turn', 'attempt': 2, 'generation': 1, 'native_transport_generation': 'transport'}
        c._context = lambda: {'dispatch_intent': pending, 'facets': {'execution': {
            'agents': {'tester': {'thread_id': 'thread'}}}}}
        c._assert_builder_retry_safe = Mock()
        c.transport = Mock()
        c.transport.diagnostic_receipt.return_value = {'stderr_summary': 'retained', 'receipt_digest': 'd'*64}
        c.transport.wire_snapshot.return_value = {'generation': 'transport', 'sequence': 9, 'event_digest': 'e'*64}
        c.core = SimpleNamespace(call=Mock(return_value={'status':'CONTINUE'}))
        return c

    def test_timeout_persists_receipt_and_wire_before_retry_without_rpc(self):
        c = self.coordinator()
        c.retry_transport_failure(AppServerError('timeout', code='NATIVE_APP_SERVER_TIMEOUT', details={'timeout_seconds':120}))
        calls = c.core.call.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['record-dispatch-wire', 'retry-dispatch'])
        details = calls[-1].kwargs['input_value']
        self.assertEqual(details['dispatch']['attempt'], 2)
        self.assertEqual(details['diagnostic_receipt']['stderr_summary'], 'retained')
        self.assertEqual(details['error']['details']['timeout_seconds'], 120)
        c.transport.read_thread.assert_not_called()
        c.transport.run_turn.assert_not_called()

    def test_diagnostic_failures_do_not_mask_timeout_or_skip_other_observation(self):
        c = self.coordinator()
        c.transport.diagnostic_receipt.side_effect = ValueError('bad observation')
        result = c.retry_transport_failure(AppServerError('timeout', code='NATIVE_APP_SERVER_TIMEOUT'))
        self.assertEqual(result['transport_retry']['source_code'], 'NATIVE_APP_SERVER_TIMEOUT')
        self.assertEqual(c.core.call.call_args.kwargs['input_value']['diagnostic_error'], 'ValueError')
        c.transport.wire_snapshot.assert_called_once()

    def test_failed_checkpoint_still_records_original_failure_and_receipt(self):
        c = self.coordinator()
        c.core.call.side_effect = [ValueError('checkpoint refused'), {'status':'CONTINUE'}]
        c.retry_transport_failure(AppServerError('timeout', code='NATIVE_APP_SERVER_TIMEOUT'))
        details = c.core.call.call_args.kwargs['input_value']
        self.assertEqual(details['checkpoint_error'], 'ValueError')
        self.assertEqual(details['source_code'], 'NATIVE_APP_SERVER_TIMEOUT')
        self.assertIn('diagnostic_receipt', details)

    def test_generation_mismatch_never_records_wire_under_new_identity(self):
        c = self.coordinator()
        c.transport.wire_snapshot.return_value['generation'] = 'other'
        c.retry_transport_failure(AppServerError('timeout', code='NATIVE_APP_SERVER_TIMEOUT'))
        self.assertEqual([call.args[0] for call in c.core.call.call_args_list], ['retry-dispatch'])


class ComparisonRunnerTest(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_offline_matrix_distinguishes_deadlines_from_disconnect_and_total(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'report.json'
            command = [sys.executable, '-B', str(self.root / 'experiments/native-transport-diagnostics/runner.py'),
                       '--output', str(output)]
            p = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            report = json.loads(output.read_text())
            self.assertFalse(report['business_acceptance'])
            expected = {
                'normal': ['completed', 'completed'],
                'silent': ['NATIVE_APP_SERVER_TIMEOUT', 'completed'],
                'partial': ['NATIVE_APP_SERVER_TIMEOUT', 'completed'],
                'batch': ['completed', 'completed'],
                'disconnect': ['NATIVE_APP_SERVER_DISCONNECTED'] * 2,
                'total': ['NATIVE_APP_SERVER_TURN_TIMEOUT'] * 2,
            }
            for scenario, outcomes in expected.items():
                self.assertEqual([row['outcome'] for row in report['rows']
                                  if row['scenario'] == scenario], outcomes)
            before = output.read_bytes()
            p = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(output.read_bytes(), before)

    def test_live_comparison_uses_new_readonly_threads_and_stops_on_settings_drift(self):
        module = runpy.run_path(str(self.root / 'experiments/native-transport-diagnostics/runner.py'))
        live = module['live_comparison']
        instances = []
        class Transport:
            def __init__(self, **kwargs):
                instances.append(self)
                self.calls = []
                self.cleanup_observation = None
            def __enter__(self): return self
            def __exit__(self, *_): self.cleanup_observation = {'process_group_gone': True}
            def _request(self, method, params):
                self.calls.append((method, params))
                return {'thread': {'id': 'thread-' + str(len(instances))},
                        'model': 'same' if len(instances) == 1 else 'different',
                        'reasoningEffort': 'high'}
            def run_turn(self, **kwargs):
                self.calls.append(('run_turn', kwargs))
                kwargs['on_started']('turn')
                return TurnResult(turn_id='turn', status='completed', text='private-result')
            def diagnostic_receipt(self, **kwargs): return {'cleanup_observation': self.cleanup_observation}
        args = SimpleNamespace(codex_bin='unused', model='same', effort='high', prompt_file=None)
        with patch.dict(live.__globals__, _ReadinessTransport=Transport,
                        probe_app_server=lambda _, **_kwargs: SimpleNamespace(executable_identity={'fixture': True})):
            rows = live(args)
        self.assertEqual([row['outcome'] for row in rows], ['completed', 'DIAGNOSTIC_SETTINGS_DRIFT'])
        self.assertEqual(instances[0].calls[0][1]['sandbox'], 'read-only')
        self.assertEqual(instances[0].calls[1][1]['sandbox_policy'], {'type': 'readOnly'})
        self.assertEqual(len(instances[1].calls), 1)
        self.assertNotEqual(rows[0]['thread_id'], rows[1]['thread_id'])
        self.assertNotIn('private-result', json.dumps(rows))

    def test_live_preserves_failed_turn_error_and_normalizes_isolated_cwd(self):
        module = runpy.run_path(str(self.root / 'experiments/native-transport-diagnostics/runner.py'))
        live = module['live_comparison']
        profiles = []
        class Transport:
            cleanup_observation = {'process_group_gone': True}
            def __init__(self, **_): pass
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def _request(self, method, params):
                return {'thread': {'id': params['cwd']}, 'model': 'fixture',
                        'reasoningEffort': 'high', 'sandbox': {'readableRoots': [params['cwd']]}}
            def run_turn(self, **kwargs):
                profiles.append(kwargs['timeout_profile'].as_dict())
                return TurnResult(turn_id='turn', status='failed', text='',
                                  error={'message': 'fixture upstream failure'})
            def diagnostic_receipt(self, **kwargs): return kwargs
        args = SimpleNamespace(codex_bin='unused', model='fixture', effort='high', prompt_file=None)
        with patch.dict(live.__globals__, _ReadinessTransport=Transport,
                        probe_app_server=lambda _, **_kwargs: SimpleNamespace(executable_identity={'fixture': True})):
            rows = live(args)
        self.assertEqual([row['outcome'] for row in rows], ['failed', 'failed'])
        self.assertEqual([profile['turn_idle_seconds'] for profile in profiles], [120, 300])
        self.assertEqual([profile['turn_total_seconds'] for profile in profiles], [600, 600])
        self.assertEqual(rows[0]['settings_sha256'], rows[1]['settings_sha256'])
        for row in rows:
            self.assertEqual(row['receipt']['turn_error'], {'message': 'fixture upstream failure'})

    def test_live_requires_explicit_model_before_contacting_server(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'report.json'
            p = subprocess.run([sys.executable, '-B',
                str(self.root / 'experiments/native-transport-diagnostics/runner.py'), '--live',
                '--codex-bin', '/nonexistent-fixture-executable', '--output', str(output)],
                capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
            self.assertIn('--live requires --model', p.stderr)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
