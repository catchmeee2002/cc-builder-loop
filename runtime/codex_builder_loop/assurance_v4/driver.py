from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .core import (
    AssuranceError,
    PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_KEY,
    REVIEWER_REPLACEMENT_MAX,
    RUNTIME_PREPARATION_PROBLEM_KEY,
    _derive_lineage,
    _public_prerequisite_classification,
    _require_admission,
    _validate_revision_transition,
    _work_unit_role_for_action,
    canonical_context_projection,
    current_machine_failure,
    current_work_unit,
    public_prerequisite_recovery,
    current_proof_failure,
    doc_reference_scan_state,
    ensure_run_id,
    evidence_state,
    readiness,
    observation_view,
    observation_request_digest,
    hold_ready,
    runtime_compatibility,
    work_unit_progress,
)
from .decision_requests import apply_changes as _apply_decision_changes, effective_problem
from .driver_contract import AGENT_ACTION_CAPABILITIES
from .dispatch_facts import dispatch_action as _dispatch_action
from .models import (
    EVIDENCE_KINDS,
    ContractError,
    assurance_downgrades,
    authority_expands,
    digest,
    evidence_dependency,
    facet_digests,
    progress_policy,
    recovery_policy,
    validate_persisted_contract,
    work_units,
)
from .store import StoreError, branch_head, dirty_paths, git, read_ledger, resolve_repo


from .decisions import (_assert_engineering_correction_candidate, _contract_decision_user_block, _decision_result, _mission_semantics, contract_problem_decision, engineering_correction_preview, validate_decision)

from .execution_limits import TESTER_CORRECTION_LIMIT, _tester_correction_authorization, _tester_correction_progress, _tester_correction_review_binding
REVIEWER_REPLACEMENT_FAILURE_CODES = {
    "responseStreamDisconnected",
    "missingAgentResult",
}
REVIEWER_REPLACEMENT_LIMIT = REVIEWER_REPLACEMENT_MAX

def _included_execution_problem_keys(ledger: Mapping[str, Any]) -> set[str]:
    return {
        str(item["key"])
        for item in ledger.get("problem_dispositions", [])
        if isinstance(item, Mapping)
        and item.get("target_run_id") == ledger.get("run_id")
        and item.get("disposition") == "included"
        and item.get("key") == RUNTIME_PREPARATION_PROBLEM_KEY
    }


def _recoverable_public_prerequisite_problem(problem: Mapping[str, Any]) -> bool:
    """Keep partial Builder progress on the normal recovery path.

    A rejected checkpoint with at least one prerequisite already produced by
    the current Builder is recoverable: Driver must let that Builder continue
    instead of routing the run to a user decision.  An all-deferred
    classification remains a routable builder-loop problem, which prevents a
    zero-progress dispatch loop.
    """
    if problem.get("key") != PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_KEY:
        return False
    try:
        details = json.loads(str(problem["details"]))
    except (KeyError, TypeError, json.JSONDecodeError):
        return False
    classification = details.get("classification")
    return isinstance(classification, list) and any(
        isinstance(item, Mapping)
        and item.get("status") == "ready"
        and item.get("source") == "builder"
        for item in classification
    )






















from .execution_limits import _external_recovery_context

def _reviewer_replacement_count(ledger: Mapping[str, Any]) -> int:
    return sum(
        1
        for event in ledger.get("events", [])
        if isinstance(event, Mapping)
        and event.get("kind") == "reviewer_replacement_started"
    )


def next_action(
    repo_value: str | Path,
    run_value: str,
    *,
    _ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive orchestration advice without mutating Core delivery facts."""

    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id) if _ledger is None else _ledger

    def decision(status: str, action: str, reason: str, **payload: Any) -> dict[str, Any]:
        # Admission applies to every Reviewer route, including feedback and
        # observation follow-ups that bypass the ordinary gate-order loop.
        assurance = ledger["facets"]["assurance"]
        if (status == "CONTINUE" and action in {"reviewer_preflight", "reviewer_final"}
                and assurance.get("preflight_before_proof")
                and any(c.get("run_before_full_suite") for c in assurance["machine_commands"])
                and evidence_state(ledger, "preflight") in {"missing", "stale"}):
            return decision("CONTINUE", "verify_preflight", "review_prerequisite_preflight_refresh",
                            candidate_worktree=ledger["candidate_worktree"])
        if progress_policy(ledger["facets"]).get("mode") == "bounded_rehydration":
            role = (
                "builder"
                if action.startswith("builder_")
                else "tester"
                if action in {"tester_author", "tester_fix", "tester_recompose_fix"}
                else "reviewer"
                if action in {"reviewer_preflight", "reviewer_final"}
                else None
            )
            if role is not None and "work_unit_id" not in payload:
                unit = current_work_unit(ledger, role=role)
                if unit is not None:
                    payload["work_unit_id"] = unit["id"]
                    payload["work_unit"] = unit
            progress = work_unit_progress(ledger)
            payload.setdefault("work_unit_progress", progress)
            if progress.get("parallel_ready"):
                payload.setdefault("parallel_ready", True)
                payload.setdefault(
                    "parallel_work_units",
                    copy.deepcopy(progress.get("parallel_work_units", [])),
                )
        return _decision_result(
            ledger, run_id, status, action, reason, **payload
        )

    def tester_fix_decision(reason: str, **payload: Any) -> dict[str, Any]:
        progress = _tester_correction_progress(ledger)
        if progress["next_tester_fix_blocked"]:
            review_binding = _tester_correction_review_binding(
                ledger, progress, reason, payload
            )
            authorization = _tester_correction_authorization(
                ledger, review_binding
            )
            if authorization is not None:
                return decision(
                    "CONTINUE",
                    "tester_fix",
                    "tester_correction_authorized",
                    tester_correction_progress=progress,
                    tester_correction_review_binding=review_binding,
                    tester_correction_authorization=authorization,
                    **payload,
                )
            return decision(
                "NEEDS_USER",
                "architecture_review",
                "tester_correction_limit_reached",
                failures=[
                    {
                        "kind": "tester_correction",
                        "count": progress["completed"],
                        "limit": progress["limit"],
                    }
                ],
                tester_correction_progress=progress,
                tester_correction_review_binding=review_binding,
                **payload,
            )
        return decision("CONTINUE", "tester_fix", reason, **payload)

    def doc_scan_decision():
        scan_state = doc_reference_scan_state(ledger)
        if scan_state in {"missing", "stale"}:
            return decision(
                "CONTINUE",
                "scan_doc_references",
                f"doc_reference_scan_{scan_state}",
                candidate_worktree=ledger["candidate_worktree"],
            )
        if scan_state == "failed":
            return decision(
                "CONTINUE",
                "builder_fix",
                "doc_reference_scan_failed",
                candidate_worktree=ledger["candidate_worktree"],
                doc_reference_scan=copy.deepcopy(ledger.get("doc_reference_scan")),
                agent=ledger["facets"]["execution"]["agents"].get("builder"),
            )
        if scan_state == "error":
            return decision(
                "NEEDS_USER",
                "doc_reference_scan_decision",
                "doc_reference_scan_error",
                doc_reference_scan=copy.deepcopy(ledger.get("doc_reference_scan")),
            )
        return None

    def problem_decision(
        problem: Mapping[str, Any], problems: list[dict[str, Any]]
    ) -> dict[str, Any]:
        owner = str(problem.get("owner"))
        if owner == "builder_loop":
            from . import observation_recovery
            retry = observation_recovery.active_request(ledger)
            if retry and retry['problem_key'] == problem['key']:
                try:
                    _blocker, observation = observation_recovery.validate_current(ledger, retry)
                except AssuranceError as exc:
                    return decision("NEEDS_USER", "builder_loop_problem_decision", exc.code, problem=problem)
                execution = ledger['facets']['execution']
                return decision("CONTINUE", "tester_observe", "observation_execution_retry",
                                problem=observation, request_digest=retry['observation_request_digest'],
                                observation_retry_request_id=retry['request_id'], agent=execution['agents']['tester'],
                                tester_source=execution['tester_source'], candidate_worktree=ledger['candidate_worktree'])
            from .review_recovery import active_request, validate_current
            author_request = active_request(ledger, "tester")
            if author_request and author_request['problem_key'] == problem['key']:
                try:
                    validate_current(ledger, author_request)
                except AssuranceError as exc:
                    return decision("NEEDS_USER", "builder_loop_problem_decision", exc.code, problem=problem)
                return decision("CONTINUE", author_request['action'], "tester_problem_reconsideration",
                                problem=problem, agent=ledger['facets']['execution']['agents']['tester'],
                                tester_source=ledger['facets']['execution']['tester_source'])
            request = active_request(ledger)
            if request and request['problem_key'] == problem['key']:
                try:
                    validate_current(ledger, request, allow_refresh_pending=True)
                except AssuranceError as exc:
                    return decision("NEEDS_USER", "builder_loop_problem_decision", exc.code, problem=problem)
                if request.get('refresh_prerequisites'):
                    scan = doc_scan_decision()
                    if scan is not None:
                        return scan
                    if ('preflight' in request['snapshot']['prerequisites']
                            and evidence_state(ledger, 'preflight') in {'missing', 'stale'}):
                        return decision("CONTINUE", "verify_preflight", "review_reconsideration_preflight_refresh",
                                        candidate_worktree=ledger['candidate_worktree'])
                return decision("CONTINUE", request['action'], "review_problem_reconsideration",
                                problem=problem, agent=ledger['facets']['execution']['agents']['reviewer'],
                                candidate_worktree=ledger['candidate_worktree'])

        if owner == "builder":
            return decision(
                "CONTINUE",
                "builder_fix",
                "open_builder_problem",
                problem=problem,
                agent=ledger["facets"]["execution"]["agents"].get("builder"),
            )
        if owner == "tester" and problem.get("observation_request"):
            request_digest = observation_request_digest(problem)
            observations = [item for item in observation_view(ledger) if item["state"] == "current"
                            and any(o["request_digest"] == request_digest for o in item["observations"])]
            reviewed = {e["details"]["artifact_digest"] for e in ledger.get("events", [])
                        if e["kind"] == "local_observation_reviewed" and e["details"]["request_digest"] == request_digest}
            pending = [item for item in observations if item["artifact_digest"] not in reviewed]
            execution = ledger["facets"]["execution"]
            if pending:
                scan_decision = doc_scan_decision()
                if scan_decision is not None:
                    return scan_decision
                index = max(i for i, e in enumerate(ledger.get("events", []))
                            if e["kind"] == "local_observations_recorded" and e["details"]["artifact_digest"] == pending[-1]["artifact_digest"])
                review_actions = {e["details"]["action_id"] for e in ledger["events"][index + 1:]
                                  if e["kind"] == "dispatch_prepared" and e["details"].get("action") in {"reviewer_preflight", "reviewer_final"}}
                reviews = sum(1 for e in ledger["events"][index + 1:] if e["kind"] == "dispatch_consumed"
                              and e["details"].get("action_id") in review_actions)
                if reviews >= 3:
                    return decision("NEEDS_USER", "architecture_review", "observation_review_no_progress", problem=problem)
                prereqs = [kind for kind in ("tester", "machine", "proof", "blackbox")
                           if kind in ledger["facets"]["assurance"]["required"]]
                action = "reviewer_final" if all(evidence_state(ledger, kind) == "pass" for kind in prereqs) else "reviewer_preflight"
                return decision("CONTINUE", action, "observation_pending_review", problem=problem,
                                agent=execution["agents"].get("reviewer"), candidate_worktree=ledger["candidate_worktree"])
            attempts = sum(1 for e in ledger.get("events", []) if e["kind"] == "local_observation_reviewed"
                           and e["details"]["request_digest"] == request_digest)
            if attempts >= 3:
                return decision("NEEDS_USER", "architecture_review", "observation_no_progress", problem=problem)
            if not execution.get("tester_source") or not execution["agents"].get("tester"):
                return decision("NEEDS_USER", "continuity_decision", "observation_source_missing", problem=problem)
            return decision("CONTINUE", "tester_observe", "observation_requested", problem=problem,
                            request_digest=request_digest, agent=execution["agents"]["tester"],
                            tester_source=execution["tester_source"], candidate_worktree=ledger["candidate_worktree"])
        if owner == "tester":
            from .tester_feedback import public_recheck, disputed, collaborative_correction_allowed
            missing = [p for p in problems if p.get("owner") == "tester" and p.get("status") == "open"
                       and not p.get("inherited_from") and not p.get("observation_request")
                       and (p.get("producer") or {}).get("role") == "reviewer"
                       and ((not collaborative_correction_allowed(ledger) and public_recheck(ledger["facets"], p) is None) or disputed(ledger, p))]
            if missing:
                problem = missing[0]
                reviewer = ledger["facets"]["execution"]["agents"].get("reviewer")
                if not reviewer or problem.get("producer") != {"role": "reviewer", **reviewer}:
                    return decision("NEEDS_USER", "continuity_decision", "tester_recheck_reviewer_mismatch", problem=problem)
                # A feedback follow-up still uses the full preflight contract.
                # Refresh its prerequisites before dispatch, never ask Reviewer
                # to interpret stale evidence as a current pass.
                scan = doc_scan_decision()
                if scan is not None:
                    return scan
                assurance = ledger["facets"]["assurance"]
                if (assurance.get("preflight_before_proof")
                        and any(c.get("run_before_full_suite") for c in assurance["machine_commands"])
                        and evidence_state(ledger, "preflight") in {"missing", "stale"}):
                    return decision("CONTINUE", "verify_preflight", "feedback_preflight_refresh",
                                    candidate_worktree=ledger["candidate_worktree"])
                # Follow up the existing Reviewer report, preserving its identity.
                # An absent or stale reference is not a Tester correction attempt.
                from .problems import problem_identity
                original = next(p for p in ledger["problems"] if p.get("key") == problem["key"]
                                and p.get("producer") == problem.get("producer")
                                and p.get("candidate_head") == problem.get("candidate_head"))
                followups = {e["details"]["action_id"] for e in ledger.get("events", [])
                             if e["kind"] == "dispatch_prepared" and e.get("at", "") > original["recorded_at"]
                             and e["details"].get("action") in {"reviewer_preflight", "reviewer_final"}}
                updates = sum(e["kind"] == "dispatch_consumed" and e["details"].get("action_id") in followups
                              for e in ledger.get("events", []))
                if updates >= 3:
                    return decision("NEEDS_USER", "architecture_review", "tester_recheck_no_progress", problem=problem)
                return decision("CONTINUE", "reviewer_preflight", "tester_recheck_disputed" if disputed(ledger, problem) else "tester_recheck_references_required",
                                problem_identity=problem_identity(original), problem=problem, agent=ledger["facets"]["execution"]["agents"].get("reviewer"),
                                candidate_worktree=ledger["candidate_worktree"])
            if problem.get("producer_continuity") == "invalid":
                execution = ledger["facets"]["execution"]
                current_agent = execution["agents"].get("tester")
                current_source = execution.get("tester_source")
                producer = problem.get("producer")
                if (
                    not isinstance(current_agent, Mapping)
                    or not isinstance(current_source, Mapping)
                    or current_source.get("agent") != current_agent
                    or producer
                    != {
                        "role": "tester",
                        "agent_id": current_agent.get("agent_id"),
                        "thread_id": current_agent.get("thread_id"),
                    }
                ):
                    return decision(
                        "NEEDS_USER",
                        "continuity_decision",
                        "tester_replacement_producer_mismatch",
                        problem=problem,
                    )
                occurrences = sum(
                    1
                    for item in ledger.get("problems", [])
                    if isinstance(item, Mapping)
                    and not item.get("inherited_from")
                    and item.get("producer_continuity") == "invalid"
                )
                if occurrences >= 3:
                    return decision(
                        "NEEDS_USER",
                        "architecture_review",
                        "tester_continuity_invalid_three_times",
                        failures=[
                            {
                                "kind": "tester_identity",
                                "failure_signature": digest(
                                    {
                                        "candidate_head": execution.get("candidate_head"),
                                        "producer_continuity": "invalid",
                                    }
                                ),
                                "count": occurrences,
                            }
                        ],
                    )
                return decision(
                    "CONTINUE",
                    "replace_tester",
                    "tester_producer_continuity_invalid",
                    problem=problem,
                    agent=current_agent,
                    tester_source=current_source,
                )
            return tester_fix_decision(
                "open_tester_problem",
                problem=problem,
                agent=ledger["facets"]["execution"]["agents"].get("tester"),
            )
        blocking = {
            "plan": ("contract_decision", "open_plan_problem"),
            "external_platform": (
                "external_problem_decision",
                "open_external_platform_problem",
            ),
            "builder_loop": (
                "builder_loop_problem_decision",
                "open_builder_loop_problem",
            ),
            "current_project": (
                "current_project_problem_decision",
                "open_current_project_problem",
            ),
        }
        if owner in blocking:
            action, reason = blocking[owner]
            related = [item for item in problems if item.get("owner") == owner]
            if owner == "plan":
                request = problem.get("decision_request")
                engineering_corrections = [
                    item
                    for item in related
                    if isinstance(item.get("decision_request"), Mapping)
                    and item["decision_request"].get("kind")
                    == "engineering_correction"
                ]
                if (
                    isinstance(request, Mapping)
                    and request.get("kind") == "engineering_correction"
                    and len(engineering_corrections) == 1
                    and recovery_policy(ledger["facets"]).get("mode")
                    == "automatic_nonsemantic"
                ):
                    try:
                        preview = engineering_correction_preview(repo, ledger, problem)
                    except (AssuranceError, ContractError) as exc:
                        return contract_problem_decision(ledger, problem, problems, rejection={
                            "code": getattr(exc, "code", "CONTRACT_INVALID"),
                            "message": str(exc), "details": copy.deepcopy(getattr(exc, "details", None)),
                            "problem_key": problem["key"],
                            "candidate_head": ledger["facets"]["execution"].get("candidate_head"),
                            "contract_digest": digest(ledger["facets"]),
                        })
                    return decision(
                        "CONTINUE",
                        "apply_engineering_correction",
                        "eligible_nonsemantic_engineering_correction",
                        problem=copy.deepcopy(problem),
                        **preview,
                    )
                return contract_problem_decision(ledger, problem, problems)
            payload: dict[str, Any] = {
                "problem": problem,
                "problems": related,
            }
            return decision(
                "NEEDS_USER",
                action,
                reason,
                **payload,
            )
        return decision(
            "NEEDS_USER",
            "problem_owner_decision",
            "open_problem_owner_unknown",
            problem=problem,
        )

    driver_failure = ledger.get("driver_failure")
    if isinstance(driver_failure, dict) and driver_failure.get("state") in {
        "recorded",
        "recovering",
    }:
        return decision(
            "CONTINUE",
            "complete_driver_failure",
            f"driver_failure_{driver_failure['state']}",
            driver_failure=driver_failure,
        )
    if ledger["phase"] == "failed":
        return decision("STOP", "none", "failed", driver_failure=driver_failure)
    compatibility = runtime_compatibility(ledger)
    if ledger["phase"] in {"active", "finalizing"} and compatibility[
        "state"
    ] != "current" and not (
        compatibility["state"] == "previous-terminal-only"
        and ledger["phase"] == "finalizing"
        and ledger.get("finalize_intent") is not None
    ):
        return decision(
            "NEEDS_USER",
            "runtime_compatibility_decision",
            "runtime_identity_not_mutable",
            runtime_compatibility=compatibility,
        )
    from .result_application import application_failure, recovery_view
    failure = application_failure(ledger)
    if failure is not None:
        return decision("NEEDS_USER", "result_application_decision", "accepted_result_application_failed",
                        application_failure=failure, recovery=recovery_view(ledger))
    pending_activation = ledger.get("dispatch_intent")
    if (
        isinstance(pending_activation, Mapping)
        and pending_activation.get("interrupted_retry_blocked")
    ):
        return decision(
            "NEEDS_USER",
            "continuity_decision",
            "dispatch_interrupted_observation_drift",
            dispatch=copy.deepcopy(dict(pending_activation)),
            failure_details=copy.deepcopy(
                pending_activation.get("failure_details")
            ),
        )
    if (
        isinstance(pending_activation, Mapping)
        and pending_activation.get("activation_state") == "unknown"
    ):
        return decision(
            "NEEDS_USER",
            "continuity_decision",
            "dispatch_activation_unknown",
            dispatch=copy.deepcopy(dict(pending_activation)),
        )
    reviewer_replacement = ledger.get("reviewer_replacement_intent")
    if isinstance(reviewer_replacement, Mapping):
        if reviewer_replacement.get("stage") not in {"prepared", "identity_bound"}:
            return decision(
                "NEEDS_USER",
                "continuity_decision",
                "reviewer_replacement_intent_invalid",
                reviewer_replacement=copy.deepcopy(dict(reviewer_replacement)),
            )
        replacement_action = decision(
            "CONTINUE",
            "replace_reviewer",
            f"reviewer_replacement_{reviewer_replacement.get('stage')}",
            reviewer_replacement=copy.deepcopy(dict(reviewer_replacement)),
            agent=copy.deepcopy(reviewer_replacement.get("new_agent")),
        )
        replacement_action["action_id"] = reviewer_replacement["action_id"]
        return replacement_action

    rehydration = ledger.get("dispatch_rehydration_intent")
    if isinstance(rehydration, Mapping):
        if rehydration.get("spawn_state") != "spawned":
            return decision(
                "NEEDS_USER",
                "continuity_decision",
                "work_unit_rehydration_spawn_unresolved",
                dispatch_rehydration_intent=copy.deepcopy(dict(rehydration)),
                work_unit_id=rehydration.get("work_unit_id"),
            )
        if rehydration.get("state") != "prepared":
            return decision(
                "NEEDS_USER",
                "continuity_decision",
                "work_unit_rehydration_intent_invalid",
                dispatch_rehydration_intent=copy.deepcopy(dict(rehydration)),
            )
        rehydration_action = decision(
            "CONTINUE",
            "rehydrate_dispatch",
            "work_unit_rehydration_prepared",
            dispatch_rehydration_intent=copy.deepcopy(dict(rehydration)),
            work_unit_id=rehydration.get("work_unit_id"),
        )
        rehydration_action["action_id"] = rehydration["action_id"]
        return rehydration_action

    rotation = ledger.get("context_rotation_intent")
    if isinstance(rotation, Mapping):
        if rotation.get("spawn_state") != "spawned":
            return decision(
                "NEEDS_USER",
                "continuity_decision",
                "context_rotation_spawn_unresolved",
                context_rotation_intent=copy.deepcopy(dict(rotation)),
                work_unit_id=rotation.get("work_unit_id"),
            )
        if rotation.get("state") != "prepared":
            return decision(
                "NEEDS_USER",
                "continuity_decision",
                "context_rotation_intent_invalid",
                context_rotation_intent=copy.deepcopy(dict(rotation)),
            )
        rotation_action = decision(
            "CONTINUE",
            "rotate_context",
            "context_rotation_prepared",
            context_rotation_intent=copy.deepcopy(dict(rotation)),
            work_unit_id=rotation.get("work_unit_id"),
        )
        rotation_action["action_id"] = digest(
            {
                "run_id": run_id,
                "rotation": copy.deepcopy(dict(rotation)),
            }
        )
        return rotation_action

    pending_dispatch = ledger.get("dispatch_intent")
    if isinstance(pending_dispatch, dict):
        if (
            pending_dispatch.get("role") == "reviewer"
            and pending_dispatch.get("state") == "exhausted"
            and _reviewer_replacement_count(ledger) >= REVIEWER_REPLACEMENT_LIMIT
        ):
            return decision(
                "NEEDS_USER",
                "architecture_review",
                "reviewer_replacement_limit_reached",
                failures=[
                    {
                        "kind": "reviewer_identity",
                        "failure_code": pending_dispatch.get("failure_code"),
                        "count": _reviewer_replacement_count(ledger),
                        "limit": REVIEWER_REPLACEMENT_LIMIT,
                    }
                ],
                dispatch=copy.deepcopy(pending_dispatch),
            )
        return _dispatch_action(ledger, run_id, pending_dispatch)
    replacement = ledger.get("tester_replacement_intent")
    if isinstance(replacement, dict):
        bootstrap_losses = sum(
            1
            for event in ledger.get("events", [])
            if isinstance(event, Mapping)
            and event.get("kind") == "tester_replacement_bootstrap_lost"
            and isinstance(event.get("details"), Mapping)
            and event["details"].get("action_id") == replacement.get("action_id")
        )
        if bootstrap_losses >= 3:
            return decision(
                "NEEDS_USER",
                "architecture_review",
                "tester_replacement_bootstrap_lost_three_times",
                failures=[
                    {
                        "kind": "tester_bootstrap_identity",
                        "failure_signature": digest(
                            {
                                "candidate_head": replacement.get("candidate_head"),
                                "problem_key": replacement.get("problem_key"),
                            }
                        ),
                        "count": bootstrap_losses,
                    }
                ],
                replacement=replacement,
            )
        if (
            replacement.get("stage") != "awaiting_first_turn"
            or not isinstance(replacement.get("new_agent"), Mapping)
        ):
            replacement_action = decision(
                "CONTINUE",
                "replace_tester",
                f"tester_replacement_{replacement.get('stage')}",
                problem_key=replacement.get("problem_key"),
                replacement=replacement,
                agent=replacement.get("new_agent"),
            )
            replacement_action["action_id"] = replacement["action_id"]
            return replacement_action
    if ledger["phase"] == "finalizing":
        return decision("CONTINUE", "recover_finalize", "persisted_finalize_intent")
    if ledger["phase"] != "active":
        return decision("STOP", "none", ledger["phase"])
    recomposition = ledger.get("recomposition_intent")
    if isinstance(recomposition, dict):
        state = recomposition.get("state")
        if state == "waiting_builder":
            return decision(
                "CONTINUE",
                "builder_recompose_fix",
                "recomposition_builder_conflict",
                recomposition=recomposition,
                candidate_worktree=recomposition.get("builder_worktree"),
                agent=ledger["facets"]["execution"]["agents"].get("builder"),
            )
        if state == "waiting_tester":
            return decision(
                "CONTINUE",
                "tester_recompose_fix",
                "recomposition_tester_conflict",
                recomposition=recomposition,
                tester_source={
                    "worktree": recomposition.get("tester_worktree"),
                    "head": recomposition.get("tester_head"),
                },
                agent=ledger["facets"]["execution"]["agents"].get("tester"),
            )
        return decision(
            "CONTINUE",
            "recompose_candidate",
            "persisted_recomposition_intent",
            recomposition=recomposition,
        )
    source_supersede = ledger.get("supersede_intent")
    if (
        isinstance(source_supersede, dict)
        and source_supersede.get("source_run_id") == run_id
        and source_supersede.get("state") == "prepared"
    ):
        return decision("STOP", "none", "supersede_pending")
    deployment_transaction = ledger.get("deployment_transaction")
    pending_blackbox = ledger.get("pending_blackbox")
    supersede_intent = ledger.get("supersede_intent")
    if isinstance(supersede_intent, dict) and supersede_intent.get("state") == "received":
        return decision("CONTINUE", "complete_supersede_transfer", "supersede_receipt_pending")
    if isinstance(supersede_intent, dict) and supersede_intent.get("state") == "artifact_mismatch":
        return decision(
            "CONTINUE",
            "restore_superseded_environment",
            "superseded_artifact_changed",
            source_run_id=supersede_intent["source_run_id"],
        )
    if isinstance(deployment_transaction, dict):
        deployment_state = deployment_transaction.get("state")
        current_candidate = ledger["facets"]["execution"].get("candidate_head")
        if deployment_state == "deployed" and deployment_transaction.get("candidate_head") != current_candidate:
            return decision("CONTINUE", "restore_deployment", "leased_candidate_changed")
        if deployment_state in {"deploying", "restore_required", "restoring"}:
            return decision("CONTINUE", "restore_deployment", f"deployment_{deployment_state}")
        if deployment_state == "deployed" and isinstance(pending_blackbox, dict):
            lease = ledger.get("environment_lease")
            if isinstance(lease, dict) and lease.get("state") == "held":
                return decision("CONTINUE", "complete_blackbox", "blackbox_result_leased")
            return decision("CONTINUE", "restore_deployment", "blackbox_result_staged")
        if deployment_state == "restore_failed":
            return decision(
                "NEEDS_USER",
                "deployment_decision",
                "deployment_restore_failed",
                deployment=deployment_transaction,
            )
        if deployment_state == "restored" and isinstance(pending_blackbox, dict):
            return decision("CONTINUE", "complete_blackbox", "deployment_restored")
        if (
            deployment_state == "restored"
            and deployment_transaction.get("failure_code")
            and not isinstance(pending_blackbox, dict)
        ):
            return decision(
                "NEEDS_USER",
                "deployment_decision",
                "deployment_failed_after_restore",
                deployment=deployment_transaction,
            )
    if isinstance(ledger.get("hold"), Mapping) and ledger["hold"].get("when") == "now":
        if isinstance(deployment_transaction, dict) and deployment_transaction.get("state") == "deployed":
            return decision("CONTINUE", "restore_deployment", "release_environment_before_hold")
        if hold_ready(ledger):
            return decision("HELD", "none", "user_hold", hold=ledger["hold"])
    replacement_problem_key = (
        replacement.get("problem_key")
        if isinstance(replacement, Mapping)
        and replacement.get("stage") == "awaiting_first_turn"
        else None
    )
    included_execution_problem_keys = _included_execution_problem_keys(ledger)
    open_problems = [
        effective_problem(ledger, item)
        for item in ledger.get("problems", [])
        if item.get("status") == "open"
        and not item.get("inherited_from")
        and item.get("key") != replacement_problem_key
        and item.get("key") not in included_execution_problem_keys
    ]
    blocking_problems = [
        item
        for item in open_problems
        if item.get("owner")
        in {"plan", "external_platform", "builder_loop", "current_project"}
    ]
    live_target = branch_head(repo, ledger["target_branch"])
    if live_target != ledger["target_start_head"]:
        return decision("CONTINUE", "recompose_candidate", "target_drift")
    execution = ledger["facets"]["execution"]
    prerequisite_recovery = None
    public_problems = [p for p in open_problems
                       if p.get("key") == PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_KEY]
    recoverable_public_problem = False
    if len(public_problems) == 1:
        prerequisite_recovery = public_prerequisite_recovery(ledger, public_problems[0])
        if prerequisite_recovery is None:
            recoverable_public_problem = _recoverable_public_prerequisite_problem(public_problems[0])
        elif len(open_problems) == 1:
            if prerequisite_recovery["reason"] == "public_prerequisite_recovery_limit_reached":
                return decision("NEEDS_USER", "architecture_review",
                                "public_prerequisite_recovery_limit_reached",
                                problem=public_problems[0], public_prerequisite_recovery=prerequisite_recovery)
            recoverable_public_problem = prerequisite_recovery["eligible"]
    if recoverable_public_problem:
        open_problems = [p for p in open_problems if p not in public_problems]
        blocking_problems = [p for p in blocking_problems if p not in public_problems]
    candidate_worktree = Path(ledger["candidate_worktree"])
    candidate_ref = f"refs/heads/{ledger['candidate_branch']}"
    candidate_result = git(repo, "rev-parse", "--verify", candidate_ref, check=False)
    if candidate_result.returncode != 0 or not candidate_worktree.is_dir():
        return decision(
            "NEEDS_USER",
            "continuity_decision",
            "candidate_identity_missing",
            candidate_worktree=ledger["candidate_worktree"],
            candidate_branch=ledger["candidate_branch"],
        )
    live_candidate = candidate_result.stdout.strip()
    worktree_head = git(candidate_worktree, "rev-parse", "HEAD").stdout.strip()
    candidate_dirty = dirty_paths(candidate_worktree)
    if candidate_dirty:
        if prerequisite_recovery is not None and recoverable_public_problem:
            return decision("NEEDS_USER", "continuity_decision", "prerequisite_candidate_dirty",
                            public_prerequisite_recovery=prerequisite_recovery)
        if blocking_problems:
            return problem_decision(blocking_problems[-1], open_problems)
        return decision(
            "CONTINUE",
            "builder_implement",
            "candidate_has_uncommitted_work",
            candidate_worktree=ledger["candidate_worktree"],
            dirty_paths=candidate_dirty,
            agent=ledger["facets"]["execution"]["agents"].get("builder"),
        )
    if live_candidate != worktree_head:
        return decision(
            "NEEDS_USER",
            "continuity_decision",
            "candidate_branch_worktree_diverged",
            candidate_worktree=ledger["candidate_worktree"],
            branch_head=live_candidate,
            worktree_head=worktree_head,
        )
    if (
        execution.get("candidate_head") is None
        and isinstance(execution.get("cost_ancestry"), Mapping)
        and live_candidate == ledger["target_start_head"]
    ):
        return decision(
            "CONTINUE",
            "builder_implement",
            "candidate_missing",
            candidate_worktree=ledger["candidate_worktree"],
            agent=execution["agents"].get("builder"),
        )
    if public_problems and prerequisite_recovery is not None and not recoverable_public_problem:
        other_blockers = [p for p in blocking_problems if p not in public_problems]
        result = problem_decision(
            other_blockers[-1] if other_blockers else public_problems[0], open_problems
        )
        result["other_open_problems"] = [
            p for p in open_problems if p != result.get("problem")
        ]
        return result
    if live_candidate != execution.get("candidate_head"):
        if prerequisite_recovery is not None and recoverable_public_problem:
            return decision("NEEDS_USER", "continuity_decision", "prerequisite_candidate_drift",
                            public_prerequisite_recovery=prerequisite_recovery)
        return decision(
            "CONTINUE",
            "checkpoint_builder",
            "committed_candidate_uncheckpointed",
            candidate_worktree=ledger["candidate_worktree"],
            candidate_head=live_candidate,
        )
    if prerequisite_recovery is not None and recoverable_public_problem:
        return decision("CONTINUE", "builder_implement", "authorized_prerequisite_completion",
                        public_prerequisite_recovery=prerequisite_recovery,
                        candidate_worktree=ledger["candidate_worktree"],
                        agent=execution["agents"].get("builder"))
    repeated_proof_failures: dict[str, int] = {}
    for event in ledger.get("events", []):
        if not isinstance(event, Mapping) or event.get("kind") != "proof_failure_recorded":
            continue
        details = event.get("details")
        signature = details.get("failure_signature") if isinstance(details, Mapping) else None
        if isinstance(signature, str) and signature:
            repeated_proof_failures[signature] = repeated_proof_failures.get(signature, 0) + 1
    proof_review_failures = [
        (signature, count)
        for signature, count in repeated_proof_failures.items()
        if count >= 3
    ]
    if proof_review_failures:
        return decision(
            "NEEDS_USER",
            "architecture_review",
            "same_failure_three_times",
            failures=[
                {"kind": "proof", "failure_signature": signature, "count": count}
                for signature, count in sorted(proof_review_failures)
            ],
        )
    if open_problems:
        latest = blocking_problems[-1] if blocking_problems else open_problems[-1]
        return problem_decision(latest, open_problems)
    machine_failure = current_machine_failure(ledger)
    if isinstance(machine_failure, dict):
        signature = machine_failure.get("failure_signature")
        repeated_machine = sum(
            1
            for event in ledger.get("events", [])
            if event.get("kind") in {"preflight_verified", "machine_verified"}
            and isinstance(event.get("details"), Mapping)
            and event["details"].get("failure_signature") == signature
        )
        if isinstance(signature, str) and repeated_machine >= 3:
            return decision(
                "NEEDS_USER",
                "architecture_review",
                "same_failure_three_times",
                failures=[
                    {
                        "kind": machine_failure.get("stage", "machine"),
                        "failure_signature": signature,
                        "count": repeated_machine,
                    }
                ],
            )
        if machine_failure.get("recovery") == "tester_diagnosis":
            return decision(
                "CONTINUE",
                "tester_machine_diagnose",
                "machine_failure_requires_diagnosis",
                machine_failure=machine_failure,
                agent=execution["agents"].get("tester"),
                tester_source=execution.get("tester_source"),
            )
        return decision(
            "NEEDS_USER",
            "machine_failure_decision",
            "machine_failure_requires_user",
            machine_failure=machine_failure,
        )
    proof_failure = current_proof_failure(ledger)
    if isinstance(proof_failure, dict):
        if proof_failure.get("recovery") == "tester_diagnosis":
            return decision(
                "CONTINUE",
                "tester_proof_diagnose",
                "proof_failure_requires_diagnosis",
                proof_failure=proof_failure,
                agent=execution["agents"].get("tester"),
                tester_source=execution.get("tester_source"),
            )
        return decision(
            "NEEDS_USER",
            "proof_failure_decision",
            "proof_failure_requires_user",
            proof_failure=proof_failure,
        )
    public_classification = _public_prerequisite_classification(
        repo,
        execution,
        ledger["facets"]["authority"].get("public_prerequisites", []),
        candidate=live_candidate,
    )
    unready_public = [
        item["path"]
        for item in public_classification
        if item["status"] != "ready"
    ]
    if unready_public:
        builder_unit = current_work_unit(ledger, role="builder")
        unit_details = ({"work_unit_id": builder_unit["id"], "work_unit": builder_unit}
                        if builder_unit is not None else {})
        return decision(
            "CONTINUE",
            "builder_implement",
            "public_prerequisites_unready",
            **unit_details,
            candidate_worktree=ledger["candidate_worktree"],
            public_prerequisites=public_classification,
            agent=execution["agents"].get("builder"),
        )
    if progress_policy(ledger["facets"]).get("mode") == "bounded_rehydration":
        builder_unit = current_work_unit(ledger, role="builder")
        if builder_unit is not None:
            return decision(
                "CONTINUE",
                "builder_implement",
                "work_unit_pending",
                work_unit_id=builder_unit["id"],
                work_unit=builder_unit,
                candidate_worktree=ledger["candidate_worktree"],
                agent=execution["agents"].get("builder"),
            )
    if not ledger.get("builder_checkpointed", False):
        return decision(
            "CONTINUE",
            "builder_implement",
            "candidate_missing",
            candidate_worktree=ledger["candidate_worktree"],
            agent=execution["agents"].get("builder"),
        )
    scan_decision = doc_scan_decision()
    if scan_decision is not None:
        return scan_decision
    publication = ledger.get("publication")
    if isinstance(publication, dict) and publication.get("required") and not publication.get("head"):
        return decision(
            "CONTINUE",
            "publish_prerequisites",
            "serial_prerequisites_unpublished",
            paths=publication.get("paths", []),
        )
    external_recovery = _external_recovery_context(
        ledger, ledger["facets"]["execution"].get("candidate_head")
    )
    recovery_state = (
        external_recovery.get("state")
        if isinstance(external_recovery, dict)
        else None
    )
    recovery_event_index = (
        external_recovery.get("event_index")
        if isinstance(external_recovery, dict)
        else None
    )
    recovery_signature = (
        external_recovery.get("failure_signature")
        if isinstance(external_recovery, dict)
        else None
    )
    repeated: dict[tuple[str, str], int] = {}
    for event_index, event in enumerate(ledger.get("events", [])):
        event_kind = event.get("kind")
        if event_kind not in {
            "evidence_recorded",
            "preflight_verified",
            "machine_verified",
            "proof_failure_recorded",
        }:
            continue
        details = event.get("details", {})
        signature = details.get("failure_signature")
        kind = details.get(
            "kind",
            "machine"
            if event_kind == "machine_verified"
            else "preflight"
            if event_kind == "preflight_verified"
            else "proof"
            if event_kind == "proof_failure_recorded"
            else "",
        )
        if (
            kind == "machine"
            and recovery_state == "succeeded"
            and isinstance(recovery_event_index, int)
            and event_index < recovery_event_index
            and signature == recovery_signature
        ):
            continue
        if signature and kind:
            repeated[(kind, signature)] = repeated.get((kind, signature), 0) + 1
    states = readiness(ledger)["states"]
    required = set(ledger["facets"]["assurance"]["required"])
    recovered_external = recovery_state == "pending"
    if (
        progress_policy(ledger["facets"]).get("mode") == "bounded_rehydration"
        and "tester" in required
    ):
        tester_unit = current_work_unit(ledger, role="tester")
        if tester_unit is not None:
            return decision(
                "CONTINUE",
                "tester_author",
                "work_unit_pending",
                work_unit_id=tester_unit["id"],
                work_unit=tester_unit,
                candidate_worktree=ledger["candidate_worktree"],
                agent=execution["agents"].get("tester"),
                tester_source=execution.get("tester_source"),
            )
    review_failures = [
        (kind, signature, count)
        for (kind, signature), count in repeated.items()
        if count >= 3
        and not (
            recovered_external
            and kind == "machine"
            and signature == recovery_signature
        )
    ]
    if review_failures:
        return decision(
            "NEEDS_USER",
            "architecture_review",
            "same_failure_three_times",
            failures=[
                {"kind": kind, "failure_signature": signature, "count": count}
                for kind, signature, count in sorted(review_failures)
            ],
        )
    preflight_commands = [
        item
        for item in ledger["facets"]["assurance"]["machine_commands"]
        if item.get("run_before_full_suite")
    ]
    tester_ready = "tester" not in required or states.get("tester") == "pass"
    if (
        ledger["facets"]["assurance"].get("preflight_before_proof", False)
        and preflight_commands
        and tester_ready
    ):
        preflight_state = evidence_state(ledger, "preflight")
        if preflight_state in {"missing", "stale"}:
            return decision(
                "CONTINUE",
                "verify_preflight",
                f"preflight_{preflight_state}",
                candidate_worktree=ledger["candidate_worktree"],
            )
    if (
        ledger["facets"]["assurance"].get("reviewer_preflight", False)
        and "reviewer" in required
        and tester_ready
        and (
            not preflight_commands
            or not ledger["facets"]["assurance"].get("preflight_before_proof", False)
            or evidence_state(ledger, "preflight") == "pass"
        )
    ):
        review_preflight_state = evidence_state(ledger, "reviewer_preflight")
        if review_preflight_state in {"missing", "stale"}:
            return decision(
                "CONTINUE",
                "reviewer_preflight",
                f"reviewer_preflight_{review_preflight_state}",
                candidate_worktree=ledger["candidate_worktree"],
                agent=execution["agents"].get("reviewer"),
            )
    for kind in ("tester", "proof", "machine", "blackbox", "reviewer", "doc_review"):
        if kind not in required:
            continue
        state = states[kind]
        if kind == "tester" and state in {"missing", "stale"}:
            action = "tester_author"
        elif kind == "proof" and state in {"missing", "stale"}:
            action = "tester_proof"
        elif kind == "machine" and state in {"missing", "stale"}:
            action = "verify_machine"
        elif kind == "machine" and state == "failed" and recovered_external:
            action = "verify_machine"
        elif kind == "blackbox" and state in {"missing", "stale"}:
            deployment = execution.get("deployment")
            transaction = ledger.get("deployment_transaction")
            if isinstance(deployment, dict) and (
                not isinstance(transaction, dict) or transaction.get("state") != "deployed"
            ):
                return decision("CONTINUE", "prepare_deployment", "deployment_required")
            action = "tester_blackbox"
        elif kind in {"reviewer", "doc_review"} and state in {"missing", "stale"}:
            action = "reviewer_final"
        elif state == "failed" and kind == "tester":
            return tester_fix_decision(
                f"{kind}_{state}",
                candidate_worktree=ledger["candidate_worktree"],
                agent=execution["agents"].get("tester"),
                tester_source=execution.get("tester_source"),
            )
        elif state == "failed":
            action = "builder_fix"
        else:
            continue
        payload: dict[str, Any] = {"candidate_worktree": ledger["candidate_worktree"]}
        if action == "builder_fix":
            payload["agent"] = execution["agents"].get("builder")
        if action.startswith("tester_"):
            payload["agent"] = execution["agents"].get("tester")
            payload["tester_source"] = execution.get("tester_source")
        if action == "reviewer_final":
            payload["agent"] = execution["agents"].get("reviewer")
        return decision("CONTINUE", action, f"{kind}_{state}", **payload)
    if progress_policy(ledger["facets"]).get("mode") == "bounded_rehydration":
        reviewer_unit = current_work_unit(ledger, role="reviewer")
        if reviewer_unit is not None and (
            "reviewer" in required or "doc_review" in required
        ):
            return decision(
                "CONTINUE",
                "reviewer_final",
                "work_unit_pending",
                work_unit_id=reviewer_unit["id"],
                work_unit=reviewer_unit,
                candidate_worktree=ledger["candidate_worktree"],
                agent=execution["agents"].get("reviewer"),
            )
        pending_units = work_unit_progress(ledger)
        if pending_units["enabled"] and pending_units["pending"] and not pending_units["ready"]:
            return decision(
                "NEEDS_USER",
                "work_unit_dependency_decision",
                "work_unit_dependencies_blocked",
                work_unit_progress=pending_units,
            )
    transaction = ledger.get("deployment_transaction")
    lease = ledger.get("environment_lease")
    if (
        isinstance(transaction, dict)
        and transaction.get("state") == "deployed"
        and isinstance(lease, dict)
        and lease.get("state") == "held"
    ):
        return decision("CONTINUE", "restore_deployment", "release_environment_before_finalize")
    if ledger.get("hold") is not None:
        return decision("HELD", "none", "user_hold", hold=ledger["hold"])
    if states.get("inherited_problems") == "missing":
        return decision("NEEDS_USER", "architecture_review", "inherited_problem_review_required")
    return decision("READY", "finalize", "all_gates_pass")
