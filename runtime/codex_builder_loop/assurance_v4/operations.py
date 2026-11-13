"""Read-only, operation-local admission for native Agent orchestration.

An operation is selected by the caller, not by a runtime scheduler. Receipts are
derived from their input facts; they are not durable workflow state. Mutating
transactions must recheck admission under their own lock.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from . import core
from .dispatch_facts import dispatch_action
from .driver_contract import AGENT_ACTION_CAPABILITIES
from .models import digest
from .problems import effective_problems
from .store import read_ledger, resolve_repo


ORCHESTRATION = "native_agent"
GATE_ACTIONS = {
    "verify_machine", "verify_preflight", "scan_doc_references", "apply_engineering_correction",
    "checkpoint_builder", "publish_prerequisites", "recompose_candidate",
    "prepare_deployment", "restore_deployment", "complete_blackbox",
    "finalize", "recover_finalize", "complete_driver_failure",
    "restore_superseded_environment", "complete_supersede_transfer",
}
TRANSPORT_ACTIONS = {"replace_tester", "replace_reviewer", "rehydrate_dispatch", "rotate_context"}
ACTIONS = frozenset(AGENT_ACTION_CAPABILITIES) | GATE_ACTIONS | TRANSPORT_ACTIONS


def is_native(ledger: Mapping[str, Any]) -> bool:
    return (ledger.get("driver_runtime") or {}).get("orchestration") == ORCHESTRATION


def _reject(code: str, message: str, **details: Any) -> None:
    raise core.AssuranceError(message, code=code, status="FAIL", details=details or None)


def _fresh(ledger: Mapping[str, Any], kinds: set[str]) -> None:
    required = set(ledger["facets"]["assurance"]["required"])
    missing = {k: core.evidence_state(ledger, k) for k in kinds & required
               if core.evidence_state(ledger, k) != "pass"}
    if missing:
        _reject("OPERATION_EVIDENCE_REQUIRED", "operation requires current evidence", evidence=missing)


def correction_limit(ledger: Mapping[str, Any]) -> dict[str, Any] | None:
    from .execution_limits import (_tester_correction_progress, _tester_correction_review_binding,
                                   _tester_correction_authorization)
    from .decisions import _decision_result
    progress = _tester_correction_progress(ledger)
    if not progress["next_tester_fix_blocked"]:
        return None
    binding = _tester_correction_review_binding(ledger, progress, "native_tester_correction", {})
    authorization = _tester_correction_authorization(ledger, binding)
    return _decision_result(ledger, ledger["run_id"], "CONTINUE" if authorization else "NEEDS_USER",
        "tester_fix" if authorization else "architecture_review",
        "tester_correction_authorized" if authorization else "tester_correction_limit_reached",
        tester_correction_progress=progress, tester_correction_review_binding=binding,
        tester_correction_authorization=authorization)


def _input(ledger: Mapping[str, Any], action: str) -> dict[str, Any]:
    """No timestamps, event counts, transport counters or role preparation facts."""
    execution = ledger["facets"]["execution"]
    binding = {
        "run_id": ledger["run_id"], "action": action, "phase": ledger["phase"],
        "owner": ledger["owner_session_id"],
        "mission": ledger["digests"]["mission"],
        "authority": ledger["digests"]["authority"],
        "assurance": ledger["digests"]["assurance"],
        "candidate": execution.get("candidate_head"),
        "target": ledger.get("target_start_head"),
        "commands": execution.get("commands"),
        "deployment": execution.get("deployment"),
        "publication": ledger.get("publication"),
    }
    # Authoring can proceed while unrelated machine observations are recorded.
    if action not in {"builder_implement", "tester_author"}:
        binding["evidence"] = ledger.get("evidence")
        binding["problems"] = effective_problems(ledger)
    capability = AGENT_ACTION_CAPABILITIES.get(action)
    if capability:
        binding["work_unit"] = core.current_work_unit(ledger, role=capability.role)
    return binding


def transport_recovery(ledger, action):
    """Recover one persisted host transaction; never choose a business phase."""
    intents = {"replace_reviewer": ("reviewer_replacement_intent", "reviewer_replacement"),
               "replace_tester": ("tester_replacement_intent", "replacement"),
               "rehydrate_dispatch": ("dispatch_rehydration_intent", "dispatch_rehydration_intent"),
               "rotate_context": ("context_rotation_intent", "context_rotation_intent")}
    active = [(name, field, payload, ledger.get(field)) for name, (field, payload) in intents.items()
              if ledger.get(field) and not (name == "replace_tester" and
                  ledger[field].get("stage") == "awaiting_first_turn" and ledger[field].get("new_agent"))]
    if active:
        if len(active) != 1 or active[0][0] != action:
            _reject("OPERATION_TRANSACTION_PENDING", "complete the persisted host transaction", transactions=[x[1] for x in active])
        _, field, payload, intent = active[0]
        if action in {"rehydrate_dispatch", "rotate_context"}:
            if intent.get("spawn_state") != "spawned" or intent.get("state") != "prepared":
                _reject("OPERATION_RECOVERY_UNRESOLVED", "host identity or transaction state is unresolved", transaction=field)
        if action == "replace_reviewer" and intent.get("stage") not in {"prepared", "identity_bound"}:
            _reject("OPERATION_RECOVERY_UNRESOLVED", "Reviewer replacement state is unresolved")
        if action == "replace_tester":
            losses = sum(e.get("kind") == "tester_replacement_bootstrap_lost" and
                         e.get("details", {}).get("action_id") == intent.get("action_id") for e in ledger.get("events", []))
            if losses >= 3:
                _reject("OPERATION_EXECUTION_LIMIT", "Tester bootstrap identity failed three times")
        identity = digest({"run_id": ledger["run_id"], "rotation": intent}) if action == "rotate_context" else intent["action_id"]
        return {"status": "CONTINUE", "run_id": ledger["run_id"], "action": action, "action_id": identity,
                "reason": "persisted_host_transaction", payload: copy.deepcopy(intent),
                "problem_key": intent.get("problem_key"), "work_unit_id": intent.get("work_unit_id"),
                "agent": intent.get("new_agent")}
    if action not in TRANSPORT_ACTIONS:
        return None
    if action != "replace_tester":
        _reject("OPERATION_RECOVERY_NOT_REQUIRED", "no matching host transaction exists")
    execution = ledger['facets']['execution']
    agent, source = execution['agents'].get('tester'), execution.get('tester_source')
    matches = [p for p in effective_problems(ledger) if p.get('status') == 'open'
               and p.get('owner') == 'tester' and p.get('producer_continuity') == 'invalid']
    if (len(matches) != 1 or not agent or not source or source.get('agent') != agent
            or matches[0].get('producer') != {'role': 'tester', **agent}):
        _reject('OPERATION_RECOVERY_NOT_REQUIRED', 'original Tester must declare its own invalid continuity')
    count = sum(not p.get('inherited_from') and p.get('producer_continuity') == 'invalid'
                for p in ledger.get('problems', []))
    if count >= 3:
        _reject('OPERATION_EXECUTION_LIMIT', 'Tester continuity failed three times')
    if ledger.get('dispatch_intent') or ledger.get('hold') or ledger.get('phase') != 'active':
        _reject('OPERATION_TRANSACTION_PENDING', 'Tester replacement requires a quiescent active run')
    return {'status': 'CONTINUE', 'run_id': ledger['run_id'], 'action': action,
            'action_id': digest(_input(ledger, action)), 'reason': 'tester_producer_continuity_invalid',
            'problem': matches[0], 'agent': agent, 'tester_source': source}


def admit(repo: Path, ledger: Mapping[str, Any], action: str, *, include_projection: bool = True) -> dict[str, Any]:
    """Validate one requested operation. Never chooses an alternative operation."""
    if action not in ACTIONS:
        _reject("OPERATION_UNKNOWN", "unknown native operation", action=action)
    if not is_native(ledger):
        _reject("OPERATION_RUNTIME_MISMATCH", "run belongs to its frozen legacy runtime")
    compatibility = core.runtime_compatibility(ledger)
    if compatibility["state"] != "current" and not (
            action == "recover_finalize" and ledger.get("phase") == "finalizing"
            and compatibility["state"] == "previous-terminal-only"):
        _reject("FROZEN_RUNTIME_REQUIRED", "operation requires the run's compatible frozen runtime",
                runtime_compatibility=compatibility)
    recovery = transport_recovery(ledger, action)
    if recovery is not None:
        return recovery
    pending = ledger.get("dispatch_intent")
    if pending:
        if pending.get("interrupted_retry_blocked"):
            _reject("OPERATION_CONTINUITY_UNPROVEN", "interrupted retry observation drifted", action_id=pending.get("action_id"))
        if (pending.get("role") == "reviewer" and pending.get("state") == "exhausted"
                and sum(e.get("kind") == "reviewer_replacement_started" for e in ledger.get("events", [])) >= core.REVIEWER_REPLACEMENT_MAX):
            _reject("OPERATION_EXECUTION_LIMIT", "Reviewer replacement budget exhausted")
        if pending.get("action") != action:
            _reject("OPERATION_TRANSACTION_PENDING", "finish the existing role transaction first",
                    action_id=pending.get("action_id"), action=pending.get("action"))
        return dispatch_action(ledger, str(ledger["run_id"]), pending)
    if action == "recover_finalize":
        if not ledger.get("finalize_intent"):
            _reject("OPERATION_RECOVERY_NOT_REQUIRED", "there is no finalize intent to recover")
    elif action in {"restore_superseded_environment", "complete_supersede_transfer"}:
        if not ledger.get("supersede_intent"):
            _reject("OPERATION_RECOVERY_NOT_REQUIRED", "there is no supersession to recover")
    elif ledger.get("phase") != "active":
        _reject("ASSURANCE_RUN_NOT_ACTIVE", "run is not active")
    if core.hold_ready(ledger) and action not in {"restore_deployment", "recover_finalize", "restore_superseded_environment"}:
        _reject("OPERATION_HELD", "release the authorized hold before proceeding")
    # Recovery must preserve the identity of the transaction with side effects.
    for field in ("candidate_residue_intent", "abandon_intent",
                  "reviewer_replacement_intent",
                  "dispatch_rehydration_intent", "context_rotation_intent"):
        if ledger.get(field):
            _reject("OPERATION_TRANSACTION_PENDING", "recover the existing transaction first", transaction=field)
    if ledger.get("finalize_intent") and action != "recover_finalize":
        _reject("OPERATION_TRANSACTION_PENDING", "recover finalize first", transaction="finalize_intent")
    recomposition = ledger.get("recomposition_intent")
    if recomposition:
        allowed = {"waiting_builder": "builder_recompose_fix", "waiting_tester": "tester_recompose_fix"}
        expected = allowed.get(recomposition.get("state"), "recompose_candidate")
        if action != expected:
            _reject("OPERATION_TRANSACTION_PENDING", "complete the current recomposition transaction",
                    transaction="recomposition_intent", operation=expected)
    elif action in {"builder_recompose_fix", "tester_recompose_fix"}:
        _reject("OPERATION_RECOVERY_NOT_REQUIRED", "no recomposition conflict exists")
    deployment = ledger.get("deployment_transaction") or {}
    if ((ledger.get("hold") or {}).get("when") == "now" and deployment.get("state") == "deployed"
            and action not in {"restore_deployment", "complete_blackbox"}):
        _reject("OPERATION_HELD", "restore the leased environment before holding")
    if deployment.get("state") in {"restore_required", "restoring", "restore_failed"} and action != "restore_deployment":
        _reject("OPERATION_TRANSACTION_PENDING", "restore the external environment first")
    if ledger.get("driver_failure") and action not in {"restore_deployment", "recover_finalize", "complete_driver_failure"}:
        _reject("OPERATION_TRANSACTION_PENDING", "recover the recorded execution failure first")
    if action == "complete_driver_failure" and not ledger.get("driver_failure"):
        _reject("OPERATION_RECOVERY_NOT_REQUIRED", "no execution failure is recorded")
    execution = ledger["facets"]["execution"]
    assurance = ledger["facets"]["assurance"]
    capability = AGENT_ACTION_CAPABILITIES.get(action)
    role = capability.role if capability else None
    from .execution_limits import repeated_failures
    failures = repeated_failures(ledger)
    if failures and action not in {"restore_deployment", "recover_finalize", "checkpoint_builder",
                                   "complete_driver_failure", "restore_superseded_environment", "complete_supersede_transfer"}:
        _reject("OPERATION_EXECUTION_LIMIT", "repeated failure requires architecture review", failures=failures)
    from .tester_feedback import pending_review
    feedback = pending_review(ledger)
    if feedback and action in {"tester_fix", "tester_author"}:
        _reject("TESTER_FEEDBACK_REVIEW_REQUIRED", "original Reviewer must clarify or withdraw its finding",
                problem=feedback["problem"]["key"])
    if feedback and role == "reviewer" and feedback["followups"] >= 3:
        _reject("OPERATION_EXECUTION_LIMIT", "Reviewer feedback made no progress", problem=feedback["problem"]["key"])
    correction = correction_limit(ledger) if action == "tester_fix" else None
    if correction and correction["status"] == "NEEDS_USER":
        _reject("OPERATION_EXECUTION_LIMIT", "Tester correction budget requires an explicit continuation",
                decision=correction)
    if role and core._work_unit_role_for_action(action) and core._progress_enabled(ledger):
        done = core._completed_work_unit_ids(ledger)
        pending_units = [u for u in core._progress_work_units(ledger)
                         if u.get("role") == role and u["id"] not in done]
        if pending_units and core.current_work_unit(ledger, role=role) is None:
            _reject("OPERATION_WORK_UNIT_DEPENDENCY", "declared work unit prerequisites are incomplete",
                    work_units=[u["id"] for u in pending_units])
    problems = [p for p in effective_problems(ledger) if p.get("status") == "open"]
    if action == "apply_engineering_correction":
        from .decisions import engineering_correction_preview
        candidates = [p for p in problems if p.get("owner") == "plan" and
                      (p.get("decision_request") or {}).get("kind") == "engineering_correction"]
        if len(candidates) != 1:
            _reject("OPERATION_PROBLEM_STALE", "select a unique engineering correction request")
        preview = engineering_correction_preview(repo, ledger, candidates[0])
        return {"status": "CONTINUE", "run_id": ledger["run_id"], "action": action,
                "action_id": digest(_input(ledger, action)), "reason": "eligible_nonsemantic_engineering_correction",
                "problem": copy.deepcopy(candidates[0]), **preview}
    included_execution = {item.get("key") for item in ledger.get("problem_dispositions", [])
                          if item.get("target_run_id") == ledger.get("run_id")
                          and item.get("disposition") == "included"
                          and item.get("key") == core.RUNTIME_PREPARATION_PROBLEM_KEY}
    blockers = [p for p in problems if p.get("owner") in
                {"plan", "external_platform", "builder_loop", "current_project"}
                and p.get("key") not in included_execution]
    prerequisite = None
    if len(problems) == 1 and problems[0].get("key") == core.PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_KEY:
        prerequisite = core.public_prerequisite_recovery(ledger, problems[0])
        if prerequisite and prerequisite.get("eligible") and action in {"builder_implement", "builder_fix"}:
            blockers = []
    # A reconsideration is a bound request to the original producer, not a new
    # role or a waiver of the blocking report. Its result resolves the report.
    from . import review_recovery, observation_recovery
    reconsideration = review_recovery.active_request(ledger, role) if role in {"tester", "reviewer"} else None
    if reconsideration and reconsideration.get("action") == action:
        review_recovery.validate_current(ledger, reconsideration, allow_refresh_pending=True)
        blockers = [p for p in blockers if p["key"] != reconsideration["problem_key"]]
    if action in {"scan_doc_references", "verify_preflight"}:
        for review_role in ("tester", "reviewer"):
            refresh = review_recovery.active_request(ledger, review_role)
            if refresh and refresh.get("refresh_prerequisites"):
                review_recovery.validate_current(ledger, refresh, allow_refresh_pending=True)
                blockers = [p for p in blockers if p["key"] != refresh["problem_key"]]
    observation_retry = observation_recovery.active_request(ledger) if action == "tester_observe" else None
    if observation_retry:
        observation_recovery.validate_current(ledger, observation_retry)
        blockers = [p for p in blockers if p["key"] != observation_retry["problem_key"]]
    # A safety restore or checkpoint must remain possible after a blocked report.
    if blockers and action not in {"scan_doc_references", "restore_deployment", "checkpoint_builder", "recompose_candidate",
                                  "complete_driver_failure", "restore_superseded_environment", "complete_supersede_transfer"}:
        _reject("OPERATION_PROBLEM_OPEN", "resolve the bound problem before this operation",
                problems=[p["key"] for p in blockers])
    publication = ledger.get("publication") or {}
    if role == "tester" and action != "tester_machine_diagnose":
        if publication.get("required") and not publication.get("head"):
            _reject("PUBLICATION_REQUIRED", "publish the frozen public prerequisites before authoring")
    if capability and capability.preparation == "existing_tester_source" and not execution.get("tester_source"):
        _reject("TESTER_SOURCE_MISSING", "operation requires the original Tester source")
    if action in {"tester_proof", "tester_blackbox", "prepare_deployment"}:
        _fresh(ledger, {"tester"})
    if action == "tester_blackbox":
        _fresh(ledger, {"proof", "machine"})
        if execution.get("deployment") and deployment.get("state") != "deployed":
            _reject("DEPLOYMENT_REQUIRED", "blackbox requires the authorized deployed candidate")
    if action == "reviewer_final":
        _fresh(ledger, {"tester", "proof", "machine", "blackbox"})
    if action in {"reviewer_final", "reviewer_preflight", "tester_proof"}:
        if assurance.get("preflight_before_proof") and any(
                c.get("run_before_full_suite") for c in assurance["machine_commands"]):
            if core.evidence_state(ledger, "preflight") != "pass":
                _reject("OPERATION_PREFLIGHT_REQUIRED", "refresh focused preflight evidence first")
    if role == "reviewer" and core.doc_reference_scan_state(ledger) not in {"pass", "not_required"}:
        _reject("OPERATION_DOC_SCAN_REQUIRED", "refresh document reference observations first")
    if action == "tester_machine_diagnose" and not core.current_machine_failure(ledger):
        _reject("OPERATION_DIAGNOSIS_NOT_REQUIRED", "no current machine failure")
    if action == "tester_proof_diagnose" and not core.current_proof_failure(ledger):
        _reject("OPERATION_DIAGNOSIS_NOT_REQUIRED", "no current proof failure")
    if action == "finalize":
        _fresh(ledger, set(assurance["required"]))
        if problems or not core.readiness(ledger)["ready"]:
            _reject("OPERATION_NOT_READY", "delivery evidence or problem resolution is incomplete")
        if ledger.get("environment_lease"):
            _reject("OPERATION_ENVIRONMENT_LEASE", "release the external environment before finalizing")
    if action not in {"builder_implement", "builder_fix", "tester_author", "tester_fix",
                      "checkpoint_builder", "recompose_candidate", "recover_finalize",
                      "builder_recompose_fix", "tester_recompose_fix"} and not execution.get("candidate_head"):
        _reject("OPERATION_CANDIDATE_REQUIRED", "checkpoint a candidate before this operation")
    payload: dict[str, Any] = {"candidate_worktree": ledger["candidate_worktree"]}
    if prerequisite:
        payload["public_prerequisite_recovery"] = prerequisite
    if feedback and role == "reviewer":
        payload.update({k: v for k, v in feedback.items() if k != "followups"})
    if correction:
        payload.update({k: v for k, v in correction.items() if k.startswith("tester_correction_")})
        payload["reason"] = correction["reason"]
    if capability:
        payload["agent"] = execution["agents"].get(role)
        unit = core.current_work_unit(ledger, role=role)
        if unit:
            payload.update(work_unit_id=unit["id"], work_unit=unit)
        if role == "tester":
            payload["tester_source"] = execution.get("tester_source")
    if recomposition:
        payload["recomposition"] = copy.deepcopy(recomposition)
        if action == "builder_recompose_fix":
            payload["candidate_worktree"] = recomposition.get("builder_worktree")
        elif action == "tester_recompose_fix":
            payload["tester_source"] = {"worktree": recomposition.get("tester_worktree"), "head": recomposition.get("tester_head")}
    if action == "tester_machine_diagnose":
        payload["machine_failure"] = core.current_machine_failure(ledger)
    if action == "tester_proof_diagnose":
        payload["proof_failure"] = core.current_proof_failure(ledger)
    if reconsideration:
        payload["reason"] = "tester_problem_reconsideration" if role == "tester" else "review_problem_reconsideration"
        payload["problem"] = next(p for p in problems if p["key"] == reconsideration["problem_key"])
    if action == "tester_observe":
        requests = [p for p in problems if p.get("observation_request")]
        if observation_retry:
            _, request = observation_recovery.validate_current(ledger, observation_retry)
            requests = [request]
            payload["observation_retry_request_id"] = observation_retry["request_id"]
        if len(requests) != 1:
            _reject("OPERATION_OBSERVATION_REQUIRED", "select a unique current observation request")
        payload.update(problem=requests[0], request_digest=core.observation_request_digest(requests[0]))
    identity = digest(_input(ledger, action))
    result = {"status": "CONTINUE", "run_id": ledger["run_id"], "action": action,
              "action_id": identity, "reason": "caller_selected_operation",
              "driver_protocol_version": 1, "driver_runtime_kind": "native",
              "driver_enforced": False, **payload}
    if role and include_projection:
        projection = core.canonical_context_projection(ledger, action_id=identity, action=action,
            role=role, work_unit_id=payload.get("work_unit_id"), work_unit=payload.get("work_unit"))
        result.update(context_projection=projection, context_projection_digest=digest(projection))
    return result


def operation(repo_value: str | Path, run_id: str, action: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    return admit(repo, read_ledger(repo, run_id), action)


def problem_view(repo: Path, ledger: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Bind the explicitly selected problem, independent of unrelated blockers."""
    from .decisions import contract_problem_decision, _decision_result
    matches = [p for p in effective_problems(ledger) if p.get("key") == key and p.get("status") == "open"]
    if len(matches) != 1:
        _reject("OPERATION_PROBLEM_STALE", "problem is not uniquely open", problem_key=key)
    problem = matches[0]
    if problem.get("owner") == "plan":
        return contract_problem_decision(ledger, problem, matches)
    owner = problem.get("owner")
    names = {"external_platform": "external_problem_decision", "builder_loop": "builder_loop_problem_decision",
             "current_project": "current_project_problem_decision", "tester": "tester_problem_decision",
             "builder": "builder_problem_decision"}
    return _decision_result(ledger, ledger["run_id"], "NEEDS_USER", names[owner],
                            f"open_{owner}_problem", problem=problem)


def find(repo_value: str | Path, run_id: str, *, action_id: str | None,
         accepted: set[str] | frozenset[str] = ACTIONS,
         ledger: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Resolve a caller's receipt, not the scheduler's preferred operation."""
    repo = resolve_repo(repo_value)
    ledger = read_ledger(repo, run_id) if ledger is None else ledger
    if not action_id:
        _reject("OPERATION_ID_REQUIRED", "select an operation and supply its input-bound receipt")
    rejections = {}
    for action in sorted(accepted):
        try:
            result = admit(repo, ledger, action, include_projection=False)
        except core.AssuranceError as exc:
            rejections[action] = exc.code
            continue
        if result["action_id"] == action_id:
            return admit(repo, ledger, action)
    if accepted is ACTIONS:
        limit = correction_limit(ledger)
        if limit and limit["action_id"] == action_id:
            return limit
        for problem in effective_problems(ledger):
            if problem.get("status") == "open":
                view = problem_view(repo, ledger, problem["key"])
                if view["action_id"] == action_id:
                    return view
    _reject("OPERATION_INPUT_STALE", "operation inputs changed or its prerequisites are not met",
            rejections=rejections)


def status(repo_value: str | Path, run_id: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    ledger = read_ledger(repo, run_id)
    if not is_native(ledger):
        _reject("OPERATION_RUNTIME_MISMATCH", "use the frozen runtime for this legacy run")
    available, rejected = [], {}
    for action in sorted(ACTIONS):
        try:
            view = admit(repo, ledger, action, include_projection=False)
        except core.AssuranceError as exc:
            rejected[action] = {"code": exc.code, "message": str(exc), "details": exc.details}
        else:
            # Full role projections are available only on the selected operation.
            available.append({k: view[k] for k in ("action", "action_id", "reason")})
    return {"status": "READY", "run_id": run_id, "phase": ledger["phase"],
            "readiness": core.readiness(ledger), "available_operations": available,
            "rejected_operations": rejected, "problems": effective_problems(ledger),
            "dispatch": copy.deepcopy(ledger.get("dispatch_intent")),
            "decisions": [problem_view(repo, ledger, p["key"]) for p in effective_problems(ledger)
                          if p.get("status") == "open"] + ([correction_limit(ledger)] if correction_limit(ledger) else []),
            "transactions": {k: copy.deepcopy(v) for k, v in ledger.items()
                             if k.endswith("_intent") and v is not None},
            "roles": copy.deepcopy(ledger["facets"]["execution"]["agents"])}
