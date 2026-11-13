"""One owner-authorized retry of a consumed, blocked Tester observation.

The retry preserves the original request and source. Command observations can
resolve execution inability, never the Reviewer's question or an evidence gate.
"""
from __future__ import annotations

import copy
import json

from .models import digest
from .problems import problem_identity, report_content
from .store import append_event, locked, read_ledger, run_dir


def active_request(ledger):
    completed = {e['details']['request_id'] for e in ledger.get('events', [])
                 if e['kind'] == 'observation_retry_completed'}
    pending = [e['details'] for e in ledger.get('events', [])
               if e['kind'] == 'observation_retry_requested'
               and e['details']['request_id'] not in completed]
    if len(pending) > 1:
        from .core import AssuranceError
        raise AssuranceError('ambiguous observation retry', code='OBSERVATION_RETRY_AMBIGUOUS')
    return pending[0] if pending else None


def snapshot(ledger):
    return {'digests': copy.deepcopy(ledger['digests']),
            'execution': copy.deepcopy(ledger['facets']['execution']),
            'candidate_worktree': ledger['candidate_worktree']}


def validate_current(ledger, request):
    from .core import AssuranceError, observation_request_digest
    problems = ledger.get('problems', [])
    blockers = [p for p in problems if p['status'] == 'open'
                and problem_identity(p) == request['problem_identity']]
    observations = [p for p in problems if p['status'] == 'open' and p.get('observation_request')
                    and observation_request_digest(p) == request['observation_request_digest']]
    if (len(blockers) != 1 or len(observations) != 1 or snapshot(ledger) != request['snapshot']
            or ledger['runtime_identity'] != request['runtime_identity']):
        raise AssuranceError('observation retry inputs changed', code='OBSERVATION_RETRY_STALE', status='NEEDS_USER')
    return blockers[0], observations[0]


def request_retry(repo_value, run_value, *, session_id, action_id, problem_key,
                  request_digest, reason, driver_runtime_kind, allow_runtime_transition=False):
    from . import core
    from .operation_binding import current as next_action
    repo, run_id = core.resolve_repo(repo_value), core.ensure_run_id(run_value)
    if not isinstance(reason, str) or not reason.strip():
        raise core.AssuranceError('retry needs a reason', code='OBSERVATION_RETRY_REASON_REQUIRED')
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        core._require_driver_runtime_owner(ledger, driver_runtime_kind)
        if ledger.get('owner_session_id') != session_id:
            raise core.AssuranceError('retry requires original owner', code='OBSERVATION_RETRY_OWNER_MISMATCH')
        replay = [e['details'] for e in ledger['events'] if e['kind'] == 'observation_retry_requested'
                  and e['details']['decision_action_id'] == action_id]
        if replay:
            if (len(replay) != 1 or replay[0]['problem_key'] != problem_key
                    or replay[0]['observation_request_digest'] != request_digest
                    or replay[0]['reason'] != reason.strip()):
                raise core.AssuranceError('retry replay differs', code='OBSERVATION_RETRY_CONFLICT')
            return core.status(repo, run_id)
        current = next_action(repo, run_id, action_id=action_id, _ledger=ledger)
        if (ledger['phase'] != 'active' or current.get('action_id') != action_id
                or current.get('action') != 'builder_loop_problem_decision'
                or current.get('problem', {}).get('key') != problem_key):
            raise core.AssuranceError('retry decision changed', code='OBSERVATION_RETRY_STALE')
        core._assert_no_candidate_residue_intent(ledger)
        if core.recovery_transaction_blockers(ledger) or ledger.get('hold') or active_request(ledger):
            raise core.AssuranceError('retry has pending transactions', code='OBSERVATION_RETRY_PENDING')
        execution = ledger['facets']['execution']
        tester, reviewer = execution['agents'].get('tester'), execution['agents'].get('reviewer')
        problem = current['problem']
        if (not tester or not reviewer or problem.get('owner') != 'builder_loop'
                or problem.get('producer') != {'role': 'tester', **tester}
                or problem.get('inherited_from') or problem.get('decision_request') or problem.get('producer_continuity')
                or problem.get('observation_request')
                or problem.get('candidate_head') != execution.get('candidate_head')):
            raise core.AssuranceError('not a current original Tester blocker', code='OBSERVATION_RETRY_PRODUCER_INVALID')
        original = next(p for p in ledger['problems'] if p['key'] == problem_key
                        and p['producer'] == problem['producer'] and p['candidate_head'] == problem['candidate_head'])
        identity = problem_identity(original)
        if any(p['status'] == 'open' and p['owner'] in {'builder_loop', 'plan', 'external_platform', 'current_project'}
               and problem_identity(p) != identity for p in ledger['problems']):
            raise core.AssuranceError('another decision blocker remains open', code='OBSERVATION_RETRY_PENDING')
        reports = [e['details'] for e in ledger['events']
                   if e['kind'] in {'problem_report_bound', 'problem_report_updated'}
                   and e['details'].get('problem_identity') == identity]
        if not reports:
            raise core.AssuranceError('blocker is not bound', code='OBSERVATION_RETRY_REPORT_UNBOUND')
        binding = reports[-1]['binding']
        path = run_dir(repo, run_id) / 'artifacts' / ('dispatch-' + binding['action_id'] + '.json')
        prepared = [e for e in ledger['events'] if e['kind'] == 'dispatch_prepared'
                    and e['details'].get('action_id') == binding['action_id']]
        consumed = [e['details'] for e in ledger['events'] if e['kind'] == 'dispatch_consumed'
                    and e['details'].get('action_id') == binding['action_id']]
        try:
            if path.is_symlink():
                raise ValueError('symlink artifact')
            artifact = json.loads(path.read_text())
            if (digest(artifact) != binding['result_digest'] or artifact.get('result') != 'blocked'
                    or artifact.get('evidence_report') is not None
                    or artifact.get('proof_spec') is not None
                    or not any(report_content(p) == reports[-1]['report'] for p in
                               (artifact.get('problem_report') or {}).get('problems', []))
                    or len(prepared) != 1 or len(consumed) != 1
                    or prepared[0]['details'].get('action') != 'tester_observe'
                    or prepared[0]['details'].get('thread_id') != tester['thread_id']
                    or consumed[0].get('result_digest') != binding['result_digest']):
                raise ValueError('not a consumed blocked observation')
        except (OSError, ValueError, KeyError) as exc:
            raise core.AssuranceError('blocked observation artifact unavailable or changed',
                                      code='OBSERVATION_RETRY_REPORT_UNBOUND') from exc
        requests = [p for p in ledger['problems'] if p['status'] == 'open' and p.get('observation_request')]
        if (len(requests) != 1 or core.observation_request_digest(requests[0]) != request_digest
                or requests[0]['producer'] != {'role': 'reviewer', **reviewer}
                or requests[0].get('inherited_from')
                or requests[0]['recorded_at'] > prepared[0]['at']):
            raise core.AssuranceError('original observation request differs', code='OBSERVATION_RETRY_REQUEST_INVALID')
        source = execution.get('tester_source')
        if not source or source.get('agent') != tester:
            raise core.AssuranceError('original Tester source missing', code='OBSERVATION_RETRY_SOURCE_INVALID')
        core._assert_tester_source_exact(repo, source)
        if (core.git(ledger['candidate_worktree'], 'rev-parse', 'HEAD').stdout.strip() != execution['candidate_head']
                or core.dirty_paths(ledger['candidate_worktree'])
                or core.branch_head(repo, ledger['candidate_branch']) != execution['candidate_head']
                or core.branch_head(repo, ledger['target_branch']) != ledger['target_start_head']):
            raise core.AssuranceError('retry Git inputs changed', code='OBSERVATION_RETRY_STALE')
        request = dict(session_id=session_id, decision_action_id=action_id, problem_key=problem_key,
                       problem_identity=identity, report_binding=copy.deepcopy(binding),
                       observation_request_digest=request_digest, reason=reason.strip(), snapshot=snapshot(ledger))
        core._transition_recovery_runtime(ledger, repo, allow_runtime_transition=allow_runtime_transition,
                                         reason=reason.strip(), action_id=action_id, binding=digest(request),
                                         error_prefix='OBSERVATION_RETRY')
        request['runtime_identity'] = copy.deepcopy(ledger['runtime_identity'])
        request['request_id'] = digest(request)
        append_event(ledger, 'observation_retry_requested', request)
        core.save_ledger(repo, ledger)
    return core.status(repo, run_id)


def complete_retry(ledger, intent):
    """Consume the one-shot grant with the accepted result, atomically with dispatch."""
    request_id = intent.get('observation_retry_request_id')
    if not request_id:
        return
    from . import core
    from .result_application import read_result
    request = active_request(ledger)
    if request is None or request['request_id'] != request_id:
        raise core.AssuranceError('retry request differs', code='OBSERVATION_RETRY_STALE')
    problem, _ = validate_current(ledger, request)
    result = read_result(intent)
    observed = False
    if not result.get('problem_report'):
        report = result.get('evidence_report') or {}
        core._validate_local_observations(ledger, report)
        observed = any(o['request_digest'] == request['observation_request_digest']
                       for o in report['details']['observations'])
        if not observed:
            raise core.AssuranceError('retry did not observe bound request', code='OBSERVATION_RETRY_REQUEST_INVALID')
        problem.update(status='resolved', resolution='observation-retry:' + intent['result_digest'], resolved_at=core.now())
    append_event(ledger, 'observation_retry_completed', dict(request_id=request_id,
                 action_id=intent['action_id'], result_digest=intent['result_digest'], observed=observed))
