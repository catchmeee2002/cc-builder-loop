"""Immutable role reports and the single derived current-problem view.

Problem identity is distinct from report transaction identity. Only descriptions
may advance via a bound follow-up; control changes retain their own contracts.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from .models import digest
from .store import append_event, run_dir

REPORT_FIELDS = ('key', 'summary', 'details', 'owner', 'producer_continuity',
                 'decision_request', 'observation_request')
CONTROL_FIELDS = ('key', 'owner', 'producer_continuity', 'decision_request', 'observation_request')


def report_content(problem: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(problem.get(key)) for key in REPORT_FIELDS}


def problem_identity(problem: Mapping[str, Any]) -> str:
    return digest({k: v for k, v in problem.items() if k not in {'status', 'resolution', 'resolved_at'}})


def effective_problem(ledger: Mapping[str, Any], problem: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(problem))
    identity = problem_identity(problem)
    for event in ledger.get('events', []):
        details = event.get('details', {})
        if details.get('problem_identity') != identity:
            continue
        if event['kind'] == 'problem_report_updated':
            result.update({key: details['report'][key] for key in ('summary', 'details')})
        elif event['kind'] == 'decision_request_corrected':
            result['decision_request'] = copy.deepcopy(details['request'])
    return result


def effective_problems(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [effective_problem(ledger, p) for p in ledger.get('problems', [])]


def dispatch_binding(ledger, action_id, role, producer, report):
    from .core import AssuranceError
    from .result_application import read_result
    if action_id is None:
        return None
    pending = ledger.get('dispatch_intent')
    if pending is None:
        # Legacy/manual registration may carry a derived action without a
        # dispatch. It retains strict replay semantics and grants no update right.
        return None
    expected = ledger['facets']['execution']['agents'].get(role)
    actual = {k: v for k, v in producer.items() if k != 'role'}
    if (ledger.get('phase') != 'active' or not isinstance(pending, dict) or pending.get('action_id') != action_id
            or pending.get('state') != 'completed' or pending.get('role') != role
            or expected != actual or read_result(pending).get('problem_report') != report):
        raise AssuranceError('problem report is not the accepted dispatch result',
                             code='PROBLEM_DISPATCH_MISMATCH', status='FAIL')
    return {key: pending.get(key) for key in ('action_id', 'generation', 'result_digest')}


def _historical_binding(repo, ledger, original):
    """Recover only one consumed, digest-verified report from the same producer."""
    from .core import AssuranceError
    producer = original['producer']
    matches = []
    events = ledger.get('events', [])
    for event in events:
        if event['kind'] != 'dispatch_consumed' or event.get('at', '') < original['recorded_at']:
            continue
        receipt = event['details']
        action = receipt.get('action_id')
        prepared = [e for e in events if e['kind'] == 'dispatch_prepared'
                    and e['details'].get('action_id') == action]
        completed = [e for e in events if e['kind'] == 'dispatch_completed'
                     and e['details'].get('action_id') == action
                     and e.get('at', '') <= original['recorded_at']]
        if len(prepared) != 1 or len(completed) != 1:
            continue
        source = prepared[0]['details']
        if (source.get('role') != producer['role']
                or source.get('thread_id') != producer.get('thread_id')
                or source.get('owner_session_id') != producer.get('session_id')):
            continue
        try:
            artifact = json.loads((run_dir(repo, ledger['run_id']) / 'artifacts' / f'dispatch-{action}.json').read_text())
        except (OSError, ValueError):
            continue
        if digest(artifact) != receipt.get('result_digest'):
            continue
        reports = (artifact.get('problem_report') or {}).get('problems', [])
        if any(report_content(p) == report_content(original) for p in reports):
            matches.append({key: receipt.get(key) for key in ('action_id', 'generation', 'result_digest')})
    if len(matches) != 1:
        raise AssuranceError('historical problem report has no unique consumed dispatch',
                             code='PROBLEM_HISTORY_UNBOUND', status='NEEDS_USER')
    return matches[0]


def bind_report(ledger, original, report, binding, *, updated=False):
    append_event(ledger, 'problem_report_updated' if updated else 'problem_report_bound', {
        'problem_identity': problem_identity(original), 'binding': binding,
        'report': report_content(report),
    })


def update_report(repo, ledger, original, report, binding):
    from .core import AssuranceError
    identity = problem_identity(original)
    previous = effective_problem(ledger, original)
    reports = [e['details'] for e in ledger.get('events', [])
               if e['kind'] in {'problem_report_bound', 'problem_report_updated'}
               and e['details'].get('problem_identity') == identity]
    if binding is not None:
        replay = [r for r in reports if r['binding']['action_id'] == binding['action_id']]
        if replay:
            if len(replay) != 1 or replay[0]['binding'] != binding or replay[0]['report'] != report_content(report):
                raise AssuranceError('same dispatch changed its problem report', code='PROBLEM_REPLAY_MISMATCH', status='FAIL')
            return False
    if report_content(previous) == report_content(report) and binding is None:
        return False
    if binding is None:
        raise AssuranceError('unbound report cannot update a previous observation', code='PROBLEM_REPLAY_MISMATCH', status='FAIL')
    if original['status'] != 'open' or any(previous.get(k) != report.get(k) for k in CONTROL_FIELDS):
        raise AssuranceError('follow-up cannot change problem control fields or reopen a resolved problem',
                             code='PROBLEM_FOLLOWUP_CONTROL_CHANGED', status='NEEDS_USER')
    if not reports:
        old_binding = _historical_binding(repo, ledger, original)
        if old_binding['action_id'] == binding['action_id']:
            if report_content(previous) != report_content(report):
                raise AssuranceError('same historical dispatch changed its report', code='PROBLEM_REPLAY_MISMATCH', status='FAIL')
            bind_report(ledger, original, original, old_binding)
            return True
        bind_report(ledger, original, original, old_binding)
    bind_report(ledger, original, report, binding, updated=True)
    return True
