"""Owner-bound host replacement between turns; old transport facts stay immutable."""
from __future__ import annotations

import copy
from .models import digest
from .store import locked, read_ledger, append_event, atomic_write_json, run_dir


def snapshot(ledger):
    from .review_recovery import active_request
    keys = ('phase', 'owner_session_id', 'digests', 'runtime_identity', 'runtime_support',
            'driver_runtime', 'target_start_head', 'evidence', 'problems', 'doc_reference_scan',
            'dispatch_intent', 'hold', 'transport_cleanup_intent', 'driver_failure', 'recomposition_intent',
            'deployment_transaction', 'pending_blackbox', 'environment_lease', 'abandon_intent',
            'finalize_intent', 'supersede_intent', 'context_rotation_intent', 'dispatch_rehydration_intent',
            'tester_replacement_intent', 'reviewer_replacement_intent', 'candidate_residue_intent')
    value = {key: copy.deepcopy(ledger.get(key)) for key in keys}
    value['review_reconsideration'] = copy.deepcopy(active_request(ledger))
    return digest(value)


def current_grant(ledger):
    events = [e['details'] for e in ledger.get('events', [])
              if e.get('kind') == 'native_host_transition_authorized']
    if events and events[-1]['snapshot_digest'] == snapshot(ledger):
        return copy.deepcopy(events[-1])
    return None


def authorize(repo_value, run_value, *, session_id, action_id, reason, observation,
              allow_runtime_transition=False):
    from . import core
    from .driver import next_action
    repo, run_id = core.resolve_repo(repo_value), core.ensure_run_id(run_value)
    def refuse(message, suffix):
        raise core.AssuranceError(message, code='NATIVE_HOST_TRANSITION_' + suffix, status='NEEDS_USER')
    if not reason.strip():
        refuse('A migration reason is required', 'REASON_REQUIRED')
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        core._require_driver_runtime_owner(ledger, 'native')
        if session_id != ledger.get('owner_session_id'):
            refuse('Only the original owner may migrate the host', 'OWNER_MISMATCH')
        if ledger['phase'] != 'active' or ledger.get('hold') or core.recovery_transaction_blockers(ledger):
            refuse('Host migration requires an active run between transactions', 'PENDING')
        old = (ledger.get('driver_runtime') or {}).get('native_transport') or {}
        cleanup = ledger.get('transport_cleanup_intent') or {}
        if old.get('state') != 'cleaned' or cleanup.get('state') != 'completed' or cleanup.get('process_group_gone') is not True:
            refuse('The previous transport must be confirmed cleaned', 'CLEANUP_REQUIRED')
        previous = current_grant(ledger)
        if (previous and previous['action_id'] == action_id and previous['reason'] == reason.strip()
                and previous['observation_digest'] == digest(observation)):
            return {'status': 'READY', 'code': 'NATIVE_HOST_TRANSITION_AUTHORIZED', 'request_id': previous['request_id'], 'run_id': run_id}
        action = next_action(repo, run_id, _ledger=ledger)
        if (action.get('action_id') != action_id or action.get('status') != 'CONTINUE'
                or action.get('action') not in {'tester_author', 'tester_fix', 'tester_blackbox', 'reviewer_preflight', 'reviewer_final', 'builder_implement', 'builder_fix'}):
            refuse('Migration must bind the current next role turn', 'ACTION_STALE')
        if not isinstance(observation, dict) or observation.get('snapshot_digest') != snapshot(ledger):
            refuse('Run changed during host observation', 'SNAPSHOT_STALE')
        target = observation.get('executable_identity')
        fields = {'requested', 'resolved', 'runtime_version', 'sha256', 'protocol_schema_digest', 'protocol_canary_digest'}
        if not isinstance(target, dict) or set(target) != fields or any(not isinstance(v, str) or not v for v in target.values()):
            refuse('Incomplete host identity', 'IDENTITY_INVALID')
        for key in ('sha256', 'protocol_schema_digest', 'protocol_canary_digest'):
            if len(target[key]) != 64 or any(c not in '0123456789abcdef' for c in target[key]):
                refuse('Invalid identity digest', 'IDENTITY_INVALID')
        if target == old.get('executable_identity'):
            refuse('The host has not changed; use ordinary resume', 'UNCHANGED')
        probe = observation.get('probe') or {}
        if (probe.get('status') != 'READY' or probe.get('code') != 'NATIVE_PROBE_READY'
                or probe.get('executable_identity') != target
                or (probe.get('cleanup') or {}).get('process_group_gone') is not True
                or (observation.get('cleanup') or {}).get('process_group_gone') is not True):
            refuse('Fresh protocol/model observation and cleanup are required', 'PROBE_INVALID')
        agents = ledger['facets']['execution']['agents']
        expected = {role: identity['thread_id'] for role, identity in agents.items() if identity and identity.get('thread_id')}
        if observation.get('threads') != expected:
            refuse('Every original role thread must remain readable by identity', 'THREAD_MISMATCH')
        candidate = ledger['candidate_worktree']
        head = ledger['facets']['execution']['candidate_head']
        if (core.git(candidate, 'rev-parse', 'HEAD').stdout.strip() != head or core.dirty_paths(candidate)
                or core.branch_head(repo, ledger['candidate_branch']) != head
                or core.branch_head(repo, ledger['target_branch']) != ledger['target_start_head']
                or core.dirty_paths(repo)):
            refuse('Candidate or target changed', 'GIT_DRIFT')
        core._transition_recovery_runtime(ledger, repo, allow_runtime_transition=allow_runtime_transition,
            reason=reason.strip(), action_id=action_id, binding=digest(observation), error_prefix='NATIVE_HOST_TRANSITION')
        previous = current_grant(ledger)
        if previous and previous['to'] == target and previous['action_id'] == action_id and previous['reason'] == reason.strip():
            return {'status': 'READY', 'code': 'NATIVE_HOST_TRANSITION_AUTHORIZED', 'request_id': previous['request_id'], 'run_id': run_id}
        atomic_write_json(run_dir(repo, run_id) / 'artifacts' / ('host-transition-' + digest(observation) + '.json'), observation)
        grant = {'session_id': session_id, 'action_id': action_id, 'reason': reason.strip(),
                 'from': copy.deepcopy(old['executable_identity']), 'to': copy.deepcopy(target),
                 'observation_digest': digest(observation), 'threads': expected,
                 'snapshot_digest': snapshot(ledger)}
        grant['request_id'] = digest(grant)
        append_event(ledger, 'native_host_transition_authorized', grant)
        core.save_ledger(repo, ledger)
    return {'status': 'READY', 'code': 'NATIVE_HOST_TRANSITION_AUTHORIZED', 'run_id': run_id,
            'request_id': grant['request_id'], 'action_id': action_id, 'next': 'native-driver resume'}
