#!/usr/bin/env python3
"""Bounded transport comparison; offline by default, never resumes a business run."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.codex_builder_loop.native_driver.app_server import (
    AppServerError, AppServerTransport, TurnTimeoutProfile, _ReadinessTransport,
    probe_app_server,
)

SCENARIOS = ('normal', 'silent', 'partial', 'batch', 'disconnect', 'total')


def offline_case(scenario: str, idle: float) -> dict:
    transport = AppServerTransport(executable_identity={'fixture': True})
    reader, writer = os.pipe()
    stream = os.fdopen(reader, 'rb', buffering=0)
    transport.process = SimpleNamespace(stdout=stream, poll=lambda: 0)
    stop = threading.Event()
    completion = (json.dumps({'method': 'turn/completed', 'params': {
        'threadId': 'fixture-thread', 'turn': {'id': 'fixture-turn', 'status': 'completed',
        'items': [{'type': 'agentMessage', 'text': 'fixture-result'}]}}}) + '\n').encode()
    heartbeat = b'{"method":"item/started","params":{"threadId":"fixture-thread"}}\n'

    def produce():
        try:
            if scenario == 'disconnect':
                return
            if scenario == 'silent':
                if not stop.wait(.45):
                    os.write(writer, completion)
            elif scenario == 'partial':
                # Bytes arrive inside the short deadline, but the frame completes later.
                for chunk in (completion[:20], completion[20:40], completion[40:60], completion[60:]):
                    os.write(writer, chunk)
                    if stop.wait(.1):
                        return
            elif scenario == 'total':
                while not stop.is_set():
                    os.write(writer, heartbeat)
                    stop.wait(.03)
            else:
                os.write(writer, (heartbeat if scenario == 'batch' else b'') + completion)
        except (BrokenPipeError, OSError):
            pass
        finally:
            os.close(writer)

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()
    row = {'scenario': scenario, 'idle_seconds': idle, 'total_seconds': .9,
           'evidence_kind': 'synthetic_pipe'}
    turn_error = None
    try:
        result = transport.wait_turn(thread_id='fixture-thread', turn_id='fixture-turn',
                                     timeout_profile=TurnTimeoutProfile(idle, .9, 1))
        row['outcome'] = result.status
    except AppServerError as exc:
        row['outcome'] = exc.code
        row['error_details'] = exc.details
        turn_error = {'code': exc.code, 'details': exc.details}
    finally:
        row['receipt'] = transport.diagnostic_receipt(
            failure_code=row.get('outcome', 'unexpected'), turn_error=turn_error)
        stop.set()
        worker.join(timeout=2)
        stream.close()
        transport.process = None
    return row


def live_comparison(args, on_row=None) -> list[dict]:
    # Each group uses a new thread in an empty directory: no business candidate,
    # no role evidence, no retry and no reuse of the exhausted Tester thread.
    capability = probe_app_server(args.codex_bin, command_timeout=30)
    prompt = args.prompt_file.read_text() if args.prompt_file else (
        'Without tools, derive the sum of the squares of integers from 1 through 100. '
        'Return a JSON object with one string field named result containing the answer.')
    rows = []
    for idle in (120.0, 300.0):
        row = {'evidence_kind': 'live_diagnostic_only', 'idle_seconds': idle,
               'total_seconds': 600.0, 'requested_model': args.model,
               'reasoning_effort': args.effort,
               'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
               'executable_identity': capability.executable_identity}
        turn_error = None
        transport = _ReadinessTransport(codex_bin=args.codex_bin, strict_protocol=True,
                                        executable_identity=capability.executable_identity)
        try:
            with tempfile.TemporaryDirectory(prefix='native-transport-comparison-') as cwd:
                with transport:
                    started = transport._request('thread/start', {
                        'cwd': cwd, 'developerInstructions': 'Diagnostic only. Do not call any tools.',
                        'approvalPolicy': 'never', 'sandbox': 'read-only', 'model': args.model,
                        'config': {'model_reasoning_effort': args.effort}})
                    thread_id = started['thread']['id']
                    row['thread_id'] = thread_id
                    row['observed_model'] = started.get('model')
                    row['observed_effort'] = started.get('reasoningEffort')
                    row['observed_provider'] = started.get('modelProvider')
                    settings = {key: started.get(key) for key in
                                ('model', 'reasoningEffort', 'modelProvider', 'sandbox', 'serviceTier')}
                    # A new isolated cwd is expected; it is not a policy change.
                    settings_json = json.dumps(settings, sort_keys=True).replace(cwd, '<diagnostic-cwd>')
                    row['settings_sha256'] = hashlib.sha256(settings_json.encode()).hexdigest()
                    if rows and row['settings_sha256'] != rows[0].get('settings_sha256'):
                        raise AppServerError('Comparison settings changed', code='DIAGNOSTIC_SETTINGS_DRIFT')
                    result = transport.run_turn(
                        thread_id=thread_id, prompt=prompt,
                        output_schema={'type': 'object', 'properties': {'result': {'type': 'string'}},
                                       'required': ['result'], 'additionalProperties': False},
                        action_id='diagnostic-' + os.urandom(16).hex(),
                        timeout_profile=TurnTimeoutProfile(idle, 600, 600),
                        cwd=cwd, sandbox_policy={'type': 'readOnly'},
                        on_started=lambda turn_id: row.update(turn_id=turn_id))
                    turn_error = result.error
                    row.update(outcome=result.status,
                               result_sha256=hashlib.sha256(result.text.encode()).hexdigest())
        except AppServerError as exc:
            row['outcome'] = exc.code
            row['receipt'] = transport.diagnostic_receipt(
                failure_code=exc.code, turn_error={'message': str(exc), 'details': exc.details})
        except Exception as exc:
            row['outcome'] = 'DIAGNOSTIC_FAILED'
            row['error_type'] = type(exc).__name__
        finally:
            if 'receipt' not in row:
                row['receipt'] = transport.diagnostic_receipt(
                    failure_code=row.get('outcome', 'unexpected'), turn_error=turn_error)
        rows.append(row)
        if on_row is not None:
            on_row(row)
        cleanup = transport.cleanup_observation
        if not cleanup or not cleanup.get('process_group_gone') or row['outcome'] in {'NATIVE_PROBE_UNEXPECTED_ACTIVITY', 'DIAGNOSTIC_FAILED', 'DIAGNOSTIC_SETTINGS_DRIFT'}:
            break
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly run two real diagnostic model turns')
    parser.add_argument('--codex-bin', default='codex')
    parser.add_argument('--model', help='Required for live comparison; identical in both groups')
    parser.add_argument('--effort', choices=('low', 'medium', 'high', 'xhigh'), default='high')
    parser.add_argument('--prompt-file', type=Path, help='Live no-tools diagnostic task; content is not logged')
    parser.add_argument('--output', type=Path, required=True, help='New report file (never overwrites)')
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists')
    if args.live and not args.model:
        parser.error('--live requires --model')
    if not args.live and (args.model or args.prompt_file):
        parser.error('--model/--prompt-file require --live')
    identity = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/codex-builder-loop.py'),
                               'version', '--json'], capture_output=True, text=True, check=True)
    report = {'schema_version': 1, 'runtime_identity': json.loads(identity.stdout),
              'business_run_mutated': False, 'business_acceptance': False,
              'upstream_root_cause_proven': False, 'state': 'running', 'rows': []}
    with args.output.open('x') as stream:
        def persist(row=None):
            if row is not None:
                report['rows'].append(row)
            stream.seek(0)
            json.dump(report, stream, indent=2)
            stream.write('\n')
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        persist()
        try:
            if args.live:
                live_comparison(args, on_row=persist)
            else:
                for scenario in SCENARIOS:
                    for idle in (.2, .7):
                        persist(offline_case(scenario, idle))
            report['state'] = 'finished'
        except BaseException as exc:
            report['state'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
            report['error_type'] = type(exc).__name__
            if isinstance(exc, AppServerError):
                report['error_code'] = exc.code
            raise
        finally:
            persist()
    rows = report['rows']
    print(json.dumps({'report': str(args.output), 'outcomes': [
        {'scenario': row.get('scenario', 'live'), 'idle_seconds': row['idle_seconds'],
         'outcome': row['outcome']} for row in rows]}))


if __name__ == '__main__':
    main()
