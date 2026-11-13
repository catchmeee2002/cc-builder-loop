"""Bound report reconsideration; only the original role can withdraw its report."""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from .models import digest
from .problems import problem_identity
from .store import append_event, locked, read_ledger, run_dir


def prerequisites(ledger, action):
    assurance = ledger['facets']['assurance']
    if action == 'reviewer_preflight':
        return (["tester"] if "tester" in assurance['required'] else []) + (
            ["preflight"] if assurance.get('preflight_before_proof') and any(
                cmd.get('run_before_full_suite') for cmd in assurance['machine_commands']) else [])
    return [kind for kind in assurance['required'] if kind not in {'reviewer', 'doc_review'}]


def snapshot(ledger, action):
    from .core import evidence_state, doc_reference_scan_state
    execution = ledger['facets']['execution']
    if action in {'tester_author', 'tester_fix'}:
        return {
            'candidate_head': execution.get('candidate_head'),
            'facets': copy.deepcopy(ledger['digests']),
            'tester': copy.deepcopy(execution['agents'].get('tester')),
            'tester_source': copy.deepcopy(execution.get('tester_source')),
            'publication': copy.deepcopy(ledger.get('publication')),
        }
    return {
        'candidate_head': execution.get('candidate_head'),
        'facets': copy.deepcopy(ledger['digests']),
        'reviewer': copy.deepcopy(execution['agents'].get('reviewer')),
        'prerequisites': {kind: {'state': evidence_state(ledger, kind),
                                 'record_digest': digest(ledger.get('evidence', {}).get(kind))}
                          for kind in prerequisites(ledger, action)},
        'doc_reference_scan_state': doc_reference_scan_state(ledger),
        'doc_reference_scan_digest': digest(ledger.get('doc_reference_scan')),
    }


def active_request(ledger, role="reviewer"):
    prefix = "tester" if role == "tester" else "review"
    completed = {e['details']['request_id'] for e in ledger.get('events', [])
                 if e['kind'] == prefix + '_problem_reconsidered'}
    requests = [e['details'] for e in ledger.get('events', [])
                if e['kind'] == prefix + '_problem_reconsideration_requested'
                and e['details']['request_id'] not in completed]
    if not requests:
        return None
    if len(requests) != 1:
        from .core import AssuranceError
        raise AssuranceError('ambiguous review reconsideration', code='REVIEW_RECONSIDERATION_AMBIGUOUS')
    return requests[0]


def validate_current(ledger, request, *, allow_refresh_pending=False):
    from .core import AssuranceError
    problems = [p for p in ledger.get('problems', [])
                if problem_identity(p) == request['problem_identity'] and p.get('status') == 'open']
    current = snapshot(ledger, request['action'])
    expected = copy.deepcopy(request['snapshot'])
    compared = copy.deepcopy(current)
    if request.get('refresh_prerequisites'):
        # Only these deterministic gates may change under this bound request.
        for value in (expected, compared):
            value.pop('doc_reference_scan_state', None)
            value.pop('doc_reference_scan_digest', None)
            value['prerequisites'].pop('preflight', None)
    if request.get('role') == 'tester':
        if len(problems) != 1 or compared != expected:
            raise AssuranceError('author reconsideration inputs drifted', code='REVIEW_RECONSIDERATION_STALE', status='NEEDS_USER')
        return problems[0]
    ready = (all(g['state'] == 'pass' for g in current['prerequisites'].values())
             and current['doc_reference_scan_state'] in {'pass', 'not_required'})
    refreshable = (request.get('refresh_prerequisites') and allow_refresh_pending
                   and all(g['state'] == 'pass' or (kind == 'preflight' and g['state'] in {'missing', 'stale'})
                           for kind, g in current['prerequisites'].items())
                   and current['doc_reference_scan_state'] in {'pass', 'not_required', 'missing', 'stale'})
    if len(problems) != 1 or compared != expected or not (ready or refreshable):
        raise AssuranceError('review reconsideration inputs drifted', code='REVIEW_RECONSIDERATION_STALE', status='NEEDS_USER')
    return problems[0]


def request_reconsideration(repo_value, run_value, *, session_id, action_id, problem_key, reason,
                            driver_runtime_kind, allow_runtime_transition=False, refresh_prerequisites=False, role="reviewer"):
    from . import core
    from .operation_binding import current as next_action
    prefix = "tester" if role == "tester" else "review"
    repo, run_id = core.resolve_repo(repo_value), core.ensure_run_id(run_value)
    if not isinstance(reason, str) or not reason.strip():
        raise core.AssuranceError('review reconsideration needs a reason', code='REVIEW_RECONSIDERATION_REASON_REQUIRED')
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        core._require_driver_runtime_owner(ledger, driver_runtime_kind)
        if ledger.get("owner_session_id") != session_id:
            raise core.AssuranceError("reconsideration requires the run owner", code="REVIEW_RECONSIDERATION_OWNER_MISMATCH")
        matches = [e['details'] for e in ledger['events']
                   if e['kind'] == prefix + '_problem_reconsideration_requested'
                   and e['details']['decision_action_id'] == action_id]
        if matches:
            if len(matches) != 1 or matches[0]['problem_key'] != problem_key or matches[0]['reason'] != reason.strip() or bool(matches[0].get('refresh_prerequisites')) != refresh_prerequisites:
                raise core.AssuranceError('reconsideration replay differs', code='REVIEW_RECONSIDERATION_CONFLICT')
            return core.status(repo, run_id)
        current = next_action(repo, run_id, action_id=action_id, _ledger=ledger)
        if (ledger['phase'] != 'active' or current.get('action_id') != action_id
            or current.get('action') != 'builder_loop_problem_decision'
            or current.get('problem', {}).get('key') != problem_key):
            raise core.AssuranceError('review decision is stale', code='REVIEW_RECONSIDERATION_STALE', status='NEEDS_USER')
        core._assert_no_candidate_residue_intent(ledger)
        if core.recovery_transaction_blockers(ledger) or ledger.get('hold'):
            raise core.AssuranceError('reconsideration has pending transactions', code='REVIEW_RECONSIDERATION_PENDING')
        if role == 'tester':
            transport = (ledger.get('driver_runtime') or {}).get('native_transport')
            if isinstance(transport, dict) and (transport.get('state') != 'cleaned'
                    or transport.get('process_identity', {}).get('process_group_gone') is not True):
                raise core.AssuranceError('original transport is still live', code='REVIEW_RECONSIDERATION_PENDING')
        problem = current['problem']
        reviewer = ledger['facets']['execution']['agents'].get(role)
        if (problem.get('producer') != {'role': role, **(reviewer or {})}
            or not reviewer or problem.get('inherited_from') or problem.get('observation_request')
            or problem.get('candidate_head') != ledger['facets']['execution'].get('candidate_head')):
            raise core.AssuranceError('only the current original Reviewer report can be reconsidered', code='REVIEW_RECONSIDERATION_PRODUCER_INVALID')
        identity = problem_identity(next(p for p in ledger['problems'] if p['key'] == problem_key and p['producer'] == problem['producer'] and p['candidate_head'] == problem['candidate_head']))
        reports = [e['details'] for e in ledger['events'] if e['kind'] in {'problem_report_bound', 'problem_report_updated'}
                   and e['details'].get('problem_identity') == identity]
        if not reports:
            raise core.AssuranceError('review report has no accepted dispatch binding', code='REVIEW_RECONSIDERATION_REPORT_UNBOUND')
        binding = reports[-1]['binding']
        artifact_path = run_dir(repo, run_id) / 'artifacts' / ('dispatch-' + binding['action_id'] + '.json')
        try:
            if artifact_path.is_symlink():
                raise ValueError('symlink report')
            artifact = json.loads(artifact_path.read_text())
            from .problems import report_content
            if (digest(artifact) != binding['result_digest'] or not any(
                report_content(p) == reports[-1]['report'] for p in
                (artifact.get('problem_report') or {}).get('problems', []))):
                raise ValueError('report differs')
        except (OSError, ValueError, KeyError) as exc:
            raise core.AssuranceError('accepted review report artifact is unavailable or changed',
                                      code='REVIEW_RECONSIDERATION_REPORT_UNBOUND') from exc

        prepared = [e['details'] for e in ledger['events'] if e['kind'] == 'dispatch_prepared'
                    and e['details'].get('action_id') == binding['action_id']]
        consumed = [e for e in ledger['events'] if e['kind'] == 'dispatch_consumed' and e['details'].get('action_id') == binding['action_id']]
        if not prepared or not consumed or prepared[-1].get('action') not in ({'tester_author', 'tester_fix'} if role == 'tester' else {'reviewer_preflight', 'reviewer_final'}):
            raise core.AssuranceError('review result is not consumed', code='REVIEW_RECONSIDERATION_REPORT_UNBOUND')
        if active_request(ledger) is not None or active_request(ledger, 'tester') is not None:
            raise core.AssuranceError('another reconsideration is pending', code='REVIEW_RECONSIDERATION_PENDING')
        action = prepared[-1]['action']
        request = {'session_id': session_id, 'decision_action_id': action_id, 'problem_key': problem_key,
                   'problem_identity': identity, 'report_binding': copy.deepcopy(binding),
                   'action': action, 'reason': reason.strip(), 'snapshot': snapshot(ledger, action)}
        if role == 'tester':
            request['role'] = role
            source = ledger['facets']['execution'].get('tester_source')
            if not source:
                raise core.AssuranceError('Tester source missing', code='REVIEW_RECONSIDERATION_PRODUCER_INVALID')
            draft_binding = {}
            core._bind_recomposition_draft(repo, ledger, draft_binding)
            request["tester_draft"] = draft_binding.get("tester_draft")
        if refresh_prerequisites:
            request['refresh_prerequisites'] = True
        validate_current(ledger, request, allow_refresh_pending=True)
        candidate = ledger['candidate_worktree']
        if (core.git(candidate, 'rev-parse', 'HEAD').stdout.strip() != request['snapshot']['candidate_head'] or core.dirty_paths(candidate)
            or core.branch_head(repo, ledger['candidate_branch']) != request['snapshot']['candidate_head']
            or core.branch_head(repo, ledger['target_branch']) != ledger['target_start_head']):
            raise core.AssuranceError('candidate worktree drifted', code='REVIEW_RECONSIDERATION_STALE')
        core._transition_recovery_runtime(ledger, repo, allow_runtime_transition=allow_runtime_transition,
                                         reason=reason.strip(), action_id=action_id, binding=digest(request),
                                         error_prefix='REVIEW_RECONSIDERATION')
        request['runtime_identity'] = copy.deepcopy(ledger['runtime_identity'])
        request['request_id'] = digest(request)
        append_event(ledger, prefix + '_problem_reconsideration_requested', request)
        core.save_ledger(repo, ledger)
    return core.status(repo, run_id)


def validate_result(ledger, result):
    """Reject incomplete reconsideration output before accepting an immutable artifact."""
    from .models import ContractError
    request = active_request(ledger)
    report = result.get('evidence_report') or {}
    dispositions = report.get('details', {}).get('review_problem_dispositions', [])
    if request is None:
        if dispositions:
            raise ContractError('review disposition has no bound request', code='EVIDENCE_REPORT_INVALID')
        return
    if (len(dispositions) != 1 or dispositions[0].get('request_id') != request['request_id']
        or dispositions[0].get('problem_key') != request['problem_key']
        or not dispositions[0].get('reason', '').strip()
        or dispositions[0].get('decision') not in {'withdrawn', 'upheld'}
        or dispositions[0].get('decision') == 'upheld' and report.get('status') == 'pass'):
        raise ContractError('reconsideration must explicitly withdraw or uphold the bound problem',
                            code='EVIDENCE_REPORT_INVALID',
                            details={'source_code': 'REVIEW_RECONSIDERATION_DISPOSITION_REQUIRED'})


def apply_disposition(ledger, report):
    from . import core
    from .result_application import read_result
    dispositions = report.get('details', {}).get('review_problem_dispositions', [])
    request = active_request(ledger)
    pending = ledger.get('dispatch_intent')
    if not request and not dispositions:
        return
    if request is None:
        # Same accepted result can be reapplied during transactional recovery.
        receipts = [e['details'] for e in ledger['events'] if e['kind'] == 'review_problem_reconsidered']
        if isinstance(pending, dict) and len(dispositions) == 1 and any(
            r['action_id'] == pending['action_id'] and r['disposition'] == dispositions[0]
            and r['result_digest'] == pending.get('result_digest') for r in receipts):
            return
        raise core.AssuranceError('unrequested review disposition', code='REVIEW_RECONSIDERATION_DISPOSITION_INVALID')
    problem = validate_current(ledger, request)
    expected_kind = 'reviewer_preflight' if request['action'] == 'reviewer_preflight' else 'reviewer'
    if (len(dispositions) != 1 or dispositions[0].get('request_id') != request['request_id']
        or dispositions[0].get('problem_key') != request['problem_key']
        or report['producer'] != problem['producer']
        or report['kind'] not in ({'reviewer', 'doc_review'} if expected_kind == 'reviewer' else {expected_kind})
        or not isinstance(pending, Mapping) or pending.get('state') != 'completed'
        or pending.get('action') != request['action'] or pending.get('thread_id') != problem['producer']['thread_id']):
        raise core.AssuranceError('review disposition is not bound to the original reviewer', code='REVIEW_RECONSIDERATION_DISPOSITION_INVALID')
    accepted = read_result(pending)
    if accepted.get('evidence_report', {}).get('details') != report['details']:
        raise core.AssuranceError('review disposition differs from accepted result', code='REVIEW_RECONSIDERATION_DISPOSITION_INVALID')
    disposition = dispositions[0]
    if not disposition['reason'].strip():
        raise core.AssuranceError('review disposition needs a reason', code='REVIEW_RECONSIDERATION_DISPOSITION_INVALID')
    if disposition['decision'] == 'upheld' and report['status'] == 'pass':
        raise core.AssuranceError('upheld blocker cannot pass review', code='REVIEW_RECONSIDERATION_DISPOSITION_INVALID')
    if disposition['decision'] == 'withdrawn':
        if any(p.get('key') == problem['key'] for p in (accepted.get('problem_report') or {}).get('problems', [])):
            raise core.AssuranceError('withdrawn problem is also reported open', code='REVIEW_RECONSIDERATION_DISPOSITION_INVALID')
        problem.update(status='resolved', resolution='review-reconsideration:' + request['request_id'], resolved_at=core.now())
    append_event(ledger, 'review_problem_reconsidered', {
        'request_id': request['request_id'], 'problem_identity': request['problem_identity'],
        'action_id': pending['action_id'], 'result_digest': pending['result_digest'],
        'disposition': copy.deepcopy(disposition), 'producer': copy.deepcopy(report['producer'])})


def validate_tester_result(ledger, result):
    """A fresh author result, not the maintainer, decides the original blocker."""
    from .models import ContractError
    request = active_request(ledger, 'tester')
    report = result.get('evidence_report') or {}
    values = report.get('details', {}).get('tester_problem_dispositions', [])
    if request is None and not values:
        return
    pending = ledger.get('dispatch_intent') or {}
    problem = validate_current(ledger, request) if request else None
    if (not request or len(values) != 1
        or values[0].get('request_id') != request['request_id']
        or values[0].get('problem_key') != request['problem_key']
        or values[0].get('decision') not in {'withdrawn', 'upheld'}
        or not values[0].get('reason', '').strip()
        or report.get('kind') != 'tester' or report.get('producer') != problem['producer']
        or pending.get('role') != 'tester' or pending.get('action') != request['action']
        or pending.get('thread_id') != problem['producer']['thread_id']):
        raise ContractError('original Tester must explicitly reconsider the bound problem',
                            code='EVIDENCE_REPORT_INVALID')
    problems = (result.get('problem_report') or {}).get('problems', [])
    if values[0]['decision'] == 'upheld':
        valid = (result['result'] == 'blocked' and report.get('status') == 'fail'
                 and any(p['key'] == request['problem_key'] and p['owner'] == 'builder_loop' for p in problems))
    else:
        valid = (not any(p['key'] == request['problem_key'] for p in problems)
                 and ((problems and report.get('status') == 'fail')
                      or (not problems and result['result'] == 'tests_ready' and report.get('status') == 'pass')))
    if not valid:
        raise ContractError('Tester disposition conflicts with author result', code='EVIDENCE_REPORT_INVALID')


def apply_tester_disposition(repo, run_id):
    """Atomic receipt inside the existing result-application transaction; no passing evidence."""
    from . import core
    from .result_application import read_result
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        pending = ledger.get('dispatch_intent') or {}
        result = read_result(pending)
        values = (result.get('evidence_report') or {}).get('details', {}).get('tester_problem_dispositions', [])
        request = active_request(ledger, 'tester')
        if request is None:
            if not values or any(e['kind'] == 'tester_problem_reconsidered'
                and e['details']['action_id'] == pending.get('action_id')
                and e['details']['result_digest'] == pending.get('result_digest')
                and e['details']['disposition'] == values[0] for e in ledger['events']):
                return
            raise core.AssuranceError('unbound Tester disposition', code='EVIDENCE_REPORT_INVALID')
        validate_tester_result(ledger, result)
        problem = validate_current(ledger, request)
        if values[0]['decision'] == 'withdrawn':
            problem.update(status='resolved', resolution='tester-reconsideration:' + request['request_id'], resolved_at=core.now())
        append_event(ledger, 'tester_problem_reconsidered', {
            'request_id': request['request_id'], 'problem_identity': request['problem_identity'],
            'action_id': pending['action_id'], 'result_digest': pending['result_digest'],
            'disposition': copy.deepcopy(values[0]), 'producer': copy.deepcopy(problem['producer'])})
        core.save_ledger(repo, ledger)
