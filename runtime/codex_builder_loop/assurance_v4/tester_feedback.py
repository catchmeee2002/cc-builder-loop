"""Reviewer feedback references resolved against frozen public inputs.

No report prose, candidate identity or suggested implementation crosses this boundary.
The original finding remains in Core; these references request independent rechecking.
"""
from collections.abc import Mapping


def public_recheck(facets, problem):
    execution = facets.get("execution", {})
    reviewer = execution.get("agents", {}).get("reviewer")
    if (not reviewer or problem.get("producer") != {"role": "reviewer", **reviewer}
            or problem.get("owner") != "tester"):
        return None
    return validate_references(facets, problem.get("tester_recheck"))


def validate_references(facets, request):
    source = facets.get("execution", {}).get("tester_source") or {}
    if (not isinstance(request, Mapping)
            or set(request) != {"source_head", "files", "acceptance_case_ids"}
            or request.get("source_head") != source.get("head")):
        return None
    files = request.get("files")
    cases = request.get("acceptance_case_ids")
    manifest = {(f.get("path"), f.get("blob")) for f in source.get("files", [])}
    public_cases = {c["id"]: c for c in facets.get("mission", {}).get("acceptance_cases", [])}
    if (not isinstance(files, list) or not files or not isinstance(cases, list) or not cases
            or any(not isinstance(f, Mapping) or set(f) != {"path", "blob"}
                   or not isinstance(f.get("path"), str) or not isinstance(f.get("blob"), str)
                   or (f.get("path"), f.get("blob")) not in manifest for f in files)
            or any(not isinstance(c, str) or c not in public_cases for c in cases)):
        return None
    return {"source_head": source["head"],
            "files": [{"path": f["path"], "blob": f["blob"]} for f in files],
            "acceptance_case_ids": list(cases)}


def validate_withdrawals(ledger, report):
    from .models import ContractError
    from .problems import problem_identity
    from .models import digest
    items = (report or {}).get('details', {}).get('tester_finding_withdrawals', [])
    producer = (report or {}).get('producer')
    matches = []
    for item in items:
        found = [p for p in ledger.get('problems', []) if problem_identity(p) == item['problem_identity']
                 and p.get('owner') == 'tester' and p.get('producer') == producer
                 and not p.get('inherited_from') and not p.get('observation_request')]
        replay = any(e['kind'] == 'tester_finding_withdrawn' and
                     e['details']['problem_identity'] == item['problem_identity'] and
                     e['details']['report_digest'] == digest(report) for e in ledger.get('events', []))
        if (len(found) != 1 or (found[0]['status'] != 'open' and not replay)
                or item['source_head'] != (ledger['facets']['execution'].get('tester_source') or {}).get('head')):
            raise ContractError('Withdrawal must bind the original Reviewer finding and current Tester source',
                                code='TESTER_FINDING_WITHDRAWAL_INVALID')
        matches.append((found[0], item, replay))
    return matches


def apply_withdrawals(ledger, report):
    from .models import digest
    from .store import append_event, now
    for problem, item, replay in validate_withdrawals(ledger, report):
        if replay:
            continue
        problem.update(status='resolved', resolution='reviewer-withdrawal:' + digest(report), resolved_at=now())
        append_event(ledger, 'tester_finding_withdrawn', {**item, 'report_digest': digest(report)})


def recover_feedback(repo_value, run_value, *, session_id, action_id, problem_key, reason,
                     driver_runtime_kind, allow_runtime_transition=False):
    """Migrate a stopped foreign-feedback run without changing problems or quotas."""
    from . import core, driver
    from .models import digest
    from .problems import problem_identity
    from .store import locked, read_ledger, append_event
    repo, run_id = core.resolve_repo(repo_value), core.ensure_run_id(run_value)
    if not isinstance(reason, str) or not reason.strip():
        raise core.AssuranceError('Recovery reason required', code='TESTER_FEEDBACK_REASON_REQUIRED')
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        core._require_driver_runtime_owner(ledger, driver_runtime_kind)
        if ledger.get('owner_session_id') != session_id:
            raise core.AssuranceError('Run owner required', code='TESTER_FEEDBACK_OWNER_MISMATCH')
        previous = [e['details'] for e in ledger['events'] if e['kind'] == 'tester_feedback_recovery_requested'
                    and e['details']['action_id'] == action_id]
        if previous:
            if len(previous) != 1 or previous[0]['problem_key'] != problem_key or previous[0]['reason'] != reason.strip():
                raise core.AssuranceError('Recovery replay differs', code='TESTER_FEEDBACK_REPLAY_MISMATCH')
            return core.status(repo, run_id)
        current = driver.next_action(repo, run_id, _ledger=ledger)
        if ledger['phase'] != 'active' or current.get('action_id') != action_id:
            raise core.AssuranceError('Recovery action is stale', code='TESTER_FEEDBACK_STALE')
        if core.recovery_transaction_blockers(ledger) or ledger.get('hold'):
            raise core.AssuranceError('Recovery transactions pending', code='TESTER_FEEDBACK_PENDING')
        reviewer = ledger['facets']['execution']['agents'].get('reviewer')
        problems = [p for p in ledger['problems'] if p['key'] == problem_key and p['status'] == 'open'
                    and p['owner'] == 'tester' and p.get('producer') == {'role':'reviewer', **(reviewer or {})}
                    and not p.get('inherited_from') and not p.get('observation_request')]
        if not reviewer or len(problems) != 1:
            raise core.AssuranceError('Unique original Reviewer Tester finding required', code='TESTER_FEEDBACK_PROBLEM_INVALID')
        source = ledger['facets']['execution'].get('tester_source')
        candidate = ledger['facets']['execution']['candidate_head']
        if (not source or source.get('agent') != ledger['facets']['execution']['agents'].get('tester')
                or core.git(ledger['candidate_worktree'], 'rev-parse', 'HEAD').stdout.strip() != candidate
                or core.branch_head(repo, ledger['candidate_branch']) != candidate
                or core.dirty_paths(ledger['candidate_worktree']) or core.dirty_paths(source['worktree'])
                or core.git(source['worktree'], 'rev-parse', 'HEAD').stdout.strip() != source['head']
                or core.branch_head(repo, ledger['target_branch']) != ledger['target_start_head']):
            raise core.AssuranceError('Feedback recovery inputs drifted', code='TESTER_FEEDBACK_INPUT_DRIFT')
        binding = {'action_id':action_id, 'problem_key':problem_key, 'problem_identity':problem_identity(problems[0]),
                   'candidate_head':candidate, 'source_head':source['head'], 'reason':reason.strip()}
        core._transition_recovery_runtime(ledger, repo, allow_runtime_transition=allow_runtime_transition,
            reason=reason.strip(), action_id=action_id, binding=digest(binding), error_prefix='TESTER_FEEDBACK')
        append_event(ledger, 'tester_feedback_recovery_requested', binding)
        core.save_ledger(repo, ledger)
    return core.status(repo, run_id)


def disputed(ledger, problem):
    from .models import digest
    report = ledger.get('evidence', {}).get('tester') or {}
    source = ledger['facets']['execution'].get('tester_source') or {}
    request = public_recheck(ledger['facets'], problem)
    if not request or report.get('details', {}).get('source_head') != source.get('head'):
        return False
    return any(r.get('request_digest') == digest(request) and r.get('decision') == 'disputed'
               for r in report.get('details', {}).get('recheck_results', []))


def validate_disputes(ledger, report):
    from .models import ContractError, digest
    from .problems import effective_problems
    results = (report or {}).get('details', {}).get('recheck_results', [])
    if not results:
        return
    source = ledger['facets']['execution'].get('tester_source') or {}
    requests = {digest(r) for p in effective_problems(ledger) if p.get('status') == 'open'
                for r in [public_recheck(ledger['facets'], p)] if r}
    if ((report or {}).get('kind') != 'tester' or report['details'].get('source_head') != source.get('head')
            or any(item['request_digest'] not in requests for item in results)):
        raise ContractError('Dispute must bind unchanged Tester source and current recheck references',
                            code='TESTER_RECHECK_DISPUTE_INVALID')
