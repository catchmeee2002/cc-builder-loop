from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from .core import (
    AssuranceError,
    _derive_lineage,
    _validate_revision_transition,
    current_machine_failure,
    current_proof_failure,
    doc_reference_scan_state,
    ensure_run_id,
    evidence_state,
    readiness,
)
from .models import (
    EVIDENCE_KINDS,
    assurance_downgrades,
    authority_expands,
    digest,
    evidence_dependency,
    facet_digests,
    validate_contract,
)
from .store import branch_head, dirty_paths, git, read_ledger, resolve_repo


def _contract_decision_user_block(
    ledger: Mapping[str, Any], problem: Mapping[str, Any]
) -> str:
    request = problem.get("decision_request")
    lines = [
        "Builder-loop 需要你确认一项交付契约决定。",
        f"Run: {ledger['run_id']}",
        f"Candidate: {ledger['facets']['execution'].get('candidate_head') or '尚未形成'}",
    ]
    states = readiness(ledger)["states"]
    passed = sorted(kind for kind, state in states.items() if state == "pass")
    lines.append("已通过 gate: " + (", ".join(passed) if passed else "无"))
    if isinstance(request, Mapping):
        lines.append(f"决定类型: {request.get('kind')}")
        if request.get("facet"):
            lines.append(f"拟修改 facet: {request.get('facet')}")
        for change in request.get("changes", []):
            if not isinstance(change, Mapping):
                continue
            rendered = f"- {change.get('operation')} {change.get('pointer')}"
            if "value" in change:
                rendered += " = " + json.dumps(
                    change.get("value"), ensure_ascii=False, sort_keys=True
                )
            lines.append(rendered)
        lines.append("需要确认: " + str(request.get("question")))
    else:
        lines.append("需要确认: " + str(problem.get("details", problem.get("summary"))))
    facet = request.get("facet") if isinstance(request, Mapping) else None
    invalidated = {
        "mission": [
            "tester", "proof", "preflight", "machine", "blackbox",
            "reviewer_preflight", "reviewer", "doc_review",
        ],
        "authority": [
            "tester", "proof", "preflight", "machine", "blackbox",
            "reviewer_preflight", "reviewer", "doc_review",
        ],
        "assurance": [
            "tester", "proof", "preflight", "machine", "blackbox",
            "reviewer_preflight", "reviewer", "doc_review",
        ],
        "execution": ["依赖实际专用事务重新计算"],
    }.get(str(facet), ["依赖验证后重新计算"])
    lines.extend(
        [
            "保留: 当前提交、worktree、角色 thread、日志与问题账本。",
            "可能失效（以完整 replacement contract 的 dependency 校验为准）: "
            + ", ".join(invalidated),
            "目标分支: 当前 run 尚未把半成品 finalize 到 target。",
            "请选择：批准精确变化；保持原契约并停止；或质疑该 finding。",
        ]
    )
    return "\n".join(lines)


def _decision_result(
    ledger: Mapping[str, Any],
    run_id: str,
    status: str,
    action: str,
    reason: str,
    **payload: Any,
) -> dict[str, Any]:
    identity = digest(
        {
            "run_id": run_id,
            "updated_at": ledger["updated_at"],
            "phase": ledger["phase"],
            "digests": ledger["digests"],
            "action": action,
            "reason": reason,
            "payload": payload,
        }
    )
    return {
        "driver_protocol_version": 1,
        "status": status,
        "run_id": run_id,
        "action": action,
        "reason": reason,
        "action_id": identity,
        "driver_enforced": bool(ledger["facets"]["execution"].get("driver_enforced")),
        "driver_runtime_kind": ledger.get("driver_runtime", {}).get("kind")
        if isinstance(ledger.get("driver_runtime"), dict)
        else None,
        **payload,
    }


def contract_problem_decision(
    ledger: Mapping[str, Any],
    problem: Mapping[str, Any],
    problems: list[dict[str, Any]],
) -> dict[str, Any]:
    request = problem.get("decision_request")
    payload: dict[str, Any] = {
        "problem": problem,
        "problems": [item for item in problems if item.get("owner") == "plan"],
        "decision_request": copy.deepcopy(request),
        "contract_digest": digest(ledger["facets"]),
        "required_user_block": _contract_decision_user_block(ledger, problem),
    }
    facet = request.get("facet") if isinstance(request, Mapping) else None
    if facet in {"mission", "authority", "assurance", "execution"}:
        payload["facet"] = facet
        payload["facet_digest"] = ledger["digests"][facet]
    return _decision_result(
        ledger,
        str(ledger["run_id"]),
        "NEEDS_USER",
        "contract_decision",
        "open_plan_problem",
        **payload,
    )


def _json_pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise AssuranceError(
            "decision change pointer must be an absolute JSON pointer",
            code="DECISION_POINTER_INVALID",
            status="FAIL",
            details={"pointer": pointer},
        )
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _apply_decision_changes(value: Any, changes: Any) -> Any:
    if not isinstance(changes, list) or not changes:
        raise AssuranceError(
            "structured decision request must declare at least one exact change",
            code="DECISION_CHANGES_REQUIRED",
            status="FAIL",
        )
    result = copy.deepcopy(value)
    for change in changes:
        if not isinstance(change, Mapping):
            raise AssuranceError(
                "decision change must be an object",
                code="DECISION_CHANGE_INVALID",
                status="FAIL",
            )
        operation = change.get("operation")
        pointer = change.get("pointer")
        if operation not in {"add", "replace", "remove"} or not isinstance(pointer, str):
            raise AssuranceError(
                "decision change operation or pointer is invalid",
                code="DECISION_CHANGE_INVALID",
                status="FAIL",
                details={"change": copy.deepcopy(change)},
            )
        if operation in {"add", "replace"} and "value" not in change:
            raise AssuranceError(
                "decision add or replace change requires a value",
                code="DECISION_CHANGE_VALUE_REQUIRED",
                status="FAIL",
                details={"pointer": pointer, "operation": operation},
            )
        parts = _json_pointer_parts(pointer)
        parent = result
        for token in parts[:-1]:
            if isinstance(parent, list):
                try:
                    index = int(token)
                except ValueError as exc:
                    raise AssuranceError(
                        "decision array pointer is invalid",
                        code="DECISION_POINTER_INVALID",
                        status="FAIL",
                        details={"pointer": pointer},
                    ) from exc
                if index < 0 or index >= len(parent):
                    raise AssuranceError(
                        "decision pointer does not exist",
                        code="DECISION_POINTER_MISSING",
                        status="FAIL",
                        details={"pointer": pointer},
                    )
                parent = parent[index]
            elif isinstance(parent, dict) and token in parent:
                parent = parent[token]
            else:
                raise AssuranceError(
                    "decision pointer does not exist",
                    code="DECISION_POINTER_MISSING",
                    status="FAIL",
                    details={"pointer": pointer},
                )
        token = parts[-1]
        if isinstance(parent, list):
            if operation == "add" and token == "-":
                parent.append(copy.deepcopy(change["value"]))
                continue
            try:
                index = int(token)
            except ValueError as exc:
                raise AssuranceError(
                    "decision array pointer is invalid",
                    code="DECISION_POINTER_INVALID",
                    status="FAIL",
                    details={"pointer": pointer},
                ) from exc
            upper = len(parent) if operation == "add" else len(parent) - 1
            if index < 0 or index > upper:
                raise AssuranceError(
                    "decision array pointer is out of range",
                    code="DECISION_POINTER_MISSING",
                    status="FAIL",
                    details={"pointer": pointer},
                )
            if operation == "add":
                parent.insert(index, copy.deepcopy(change["value"]))
            elif operation == "replace":
                parent[index] = copy.deepcopy(change["value"])
            else:
                parent.pop(index)
        elif isinstance(parent, dict):
            if operation in {"replace", "remove"} and token not in parent:
                raise AssuranceError(
                    "decision pointer does not exist",
                    code="DECISION_POINTER_MISSING",
                    status="FAIL",
                    details={"pointer": pointer},
                )
            if operation == "remove":
                parent.pop(token)
            else:
                parent[token] = copy.deepcopy(change["value"])
        else:
            raise AssuranceError(
                "decision pointer parent is not a container",
                code="DECISION_POINTER_INVALID",
                status="FAIL",
                details={"pointer": pointer},
            )
    return result


def _mission_semantics(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value.get(key))
        for key in (
            "delivery_kind",
            "objective",
            "behaviors",
            "interfaces",
            "acceptance_cases",
            "trust_boundaries",
        )
    }


def validate_decision(
    repo_value: str | Path,
    run_value: str,
    *,
    session_id: str,
    problem_key: str,
    action_id: str,
    facet: str,
    facet_digest: str,
    replacement_contract: Any,
) -> dict[str, Any]:
    """Validate one same-run contract replacement without mutating the ledger."""

    if facet not in {"mission", "authority", "assurance"}:
        raise AssuranceError(
            "this decision cannot be expressed by a supported same-run facet transaction",
            code="DECISION_FACET_UNSUPPORTED",
            status="NEEDS_USER",
            details={"facet": facet},
        )
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    if ledger.get("phase") != "active":
        raise AssuranceError(
            "contract decision requires an active run",
            code="DECISION_RUN_NOT_ACTIVE",
            status="FAIL",
        )
    if ledger.get("owner_session_id") != session_id.strip():
        raise AssuranceError(
            "contract decision belongs to another Codex session",
            code="DECISION_SESSION_MISMATCH",
            status="FAIL",
        )
    current = next_action(repo, run_id)
    current_problem = current.get("problem")
    if (
        current.get("status") != "NEEDS_USER"
        or current.get("action") != "contract_decision"
        or current.get("action_id") != action_id
    ):
        raise AssuranceError(
            "contract decision handoff is stale",
            code="DECISION_ACTION_STALE",
            status="FAIL",
            details={
                "expected_action": current.get("action"),
                "expected_action_id": current.get("action_id"),
            },
        )
    if (
        not isinstance(current_problem, Mapping)
        or current_problem.get("key") != problem_key
        or current_problem.get("owner") != "plan"
        or current_problem.get("status") != "open"
    ):
        raise AssuranceError(
            "contract decision problem binding is stale or ambiguous",
            code="DECISION_PROBLEM_MISMATCH",
            status="FAIL",
            details={"problem_key": problem_key},
        )
    if ledger["digests"].get(facet) != facet_digest:
        raise AssuranceError(
            "contract decision facet digest is stale",
            code="DECISION_FACET_STALE",
            status="FAIL",
            details={
                "facet": facet,
                "expected": ledger["digests"].get(facet),
                "provided": facet_digest,
            },
        )

    request = current_problem.get("decision_request")
    if isinstance(request, Mapping):
        requested_facet = request.get("facet")
        if requested_facet is not None and requested_facet != facet:
            raise AssuranceError(
                "replacement facet does not match the recorded decision request",
                code="DECISION_FACET_MISMATCH",
                status="FAIL",
                details={"requested": requested_facet, "provided": facet},
            )

    replacement = validate_contract(replacement_contract)
    current_contract = ledger["facets"]
    for unchanged in {"mission", "authority", "assurance"} - {facet}:
        if replacement[unchanged] != current_contract[unchanged]:
            raise AssuranceError(
                "replacement contract changed an unapproved facet",
                code="DECISION_REPLACEMENT_DRIFT",
                status="FAIL",
                details={"facet": unchanged},
            )

    authorization_flags: list[str] = []
    if facet == "mission":
        expected_supersedes = {
            "run_id": run_id,
            "revision": current_contract["mission"]["revision"],
            "mission_digest": ledger["digests"]["mission"],
            "candidate_head": current_contract["execution"].get("candidate_head"),
        }
        mission = replacement["mission"]
        if (
            mission.get("revision") != current_contract["mission"]["revision"] + 1
            or mission.get("supersedes") != expected_supersedes
        ):
            raise AssuranceError(
                "mission replacement does not bind the current run revision",
                code="MISSION_REVISION_BINDING_INVALID",
                status="FAIL",
                details={"expected_supersedes": expected_supersedes},
            )
        transition = replacement["execution"].get("revision_transition")
        expected_execution = copy.deepcopy(current_contract["execution"])
        expected_execution["revision_transition"] = copy.deepcopy(transition)
        if replacement["execution"] != expected_execution:
            raise AssuranceError(
                "mission replacement changed execution facts outside revision_transition",
                code="DECISION_REPLACEMENT_DRIFT",
                status="FAIL",
                details={"facet": "execution"},
            )
        if not isinstance(transition, dict):
            raise AssuranceError(
                "mission replacement requires a revision transition",
                code="REVISION_TRANSITION_REQUIRED",
                status="FAIL",
            )
        _validate_revision_transition(_derive_lineage(repo, ledger), transition)
        if transition.get("category") != "mission_change":
            raise AssuranceError(
                "same-run mission revision requires mission_change semantics",
                code="REVISION_TRANSITION_SEMANTICS_MISMATCH",
                status="FAIL",
            )
        if isinstance(request, Mapping):
            requested = _apply_decision_changes(
                _mission_semantics(current_contract["mission"]), request.get("changes")
            )
            if requested != _mission_semantics(mission):
                raise AssuranceError(
                    "replacement mission contains changes outside the approved delta",
                    code="DECISION_DELTA_MISMATCH",
                    status="FAIL",
                )
        command = "revise-mission"
    else:
        if replacement["execution"] != current_contract["execution"]:
            raise AssuranceError(
                "replacement contract changed execution facts",
                code="DECISION_REPLACEMENT_DRIFT",
                status="FAIL",
                details={"facet": "execution"},
            )
        if isinstance(request, Mapping):
            requested = _apply_decision_changes(
                current_contract[facet], request.get("changes")
            )
            if requested != replacement[facet]:
                raise AssuranceError(
                    "replacement facet contains changes outside the approved delta",
                    code="DECISION_DELTA_MISMATCH",
                    status="FAIL",
                    details={"facet": facet},
                )
        if facet == "authority":
            old = current_contract["authority"]
            new = replacement["authority"]
            if new.get("target_branch") != old.get("target_branch"):
                raise AssuranceError(
                    "an active run cannot change its target branch",
                    code="AUTHORITY_TARGET_IMMUTABLE",
                    status="FAIL",
                )
            if new.get("dirty_intake") != old.get("dirty_intake"):
                raise AssuranceError(
                    "dirty intake requires a dedicated snapshot transaction",
                    code="AUTHORITY_DIRTY_INTAKE_IMMUTABLE",
                    status="FAIL",
                )
            for field in ("public_prerequisites", "protected_support_paths"):
                if new.get(field) != old.get(field):
                    raise AssuranceError(
                        "authority replacement requires a dedicated lifecycle transaction",
                        code="DECISION_AUTHORITY_TRANSACTION_UNSUPPORTED",
                        status="NEEDS_USER",
                        details={"field": field},
                    )
            if authority_expands(old, new):
                authorization_flags.append("authorize_expansion")
        elif assurance_downgrades(
            current_contract["assurance"], replacement["assurance"]
        ):
            authorization_flags.append("authorize_downgrade")
        command = "update-facet"

    digests = facet_digests(replacement)
    replacement_ledger = copy.deepcopy(ledger)
    replacement_ledger["facets"] = replacement
    replacement_ledger["digests"] = digests
    invalidated_evidence = sorted(
        kind
        for kind in EVIDENCE_KINDS
        if isinstance(ledger.get("evidence", {}).get(kind), Mapping)
        and ledger["evidence"][kind].get("dependency_digest")
        != evidence_dependency(replacement_ledger, kind)
    )
    return {
        "status": "READY",
        "run_id": run_id,
        "problem_key": problem_key,
        "action_id": action_id,
        "facet": facet,
        "base_facet_digest": facet_digest,
        "replacement_facet_digest": digests[facet],
        "replacement_contract_digest": digest(replacement),
        "invalidated_evidence": invalidated_evidence,
        "apply": {
            "command": command,
            "authorization_flags": authorization_flags,
            "resolve_plan_problem_key": problem_key,
        },
    }


def _external_recovery_context(
    ledger: Mapping[str, Any], candidate_head: str | None
) -> dict[str, Any] | None:
    """Derive the one-shot machine recovery boundary from immutable events."""

    events = ledger.get("events", [])
    if not isinstance(events, list):
        return None
    for event_index in range(len(events) - 1, -1, -1):
        event = events[event_index]
        if not isinstance(event, Mapping):
            continue
        if event.get("kind") != "external_problem_resolved":
            continue
        details = event.get("details")
        if (
            not isinstance(details, dict)
            or details.get("candidate_head") != candidate_head
        ):
            continue
        key = details.get("key")
        resolved = [
            problem
            for problem in ledger.get("problems", [])
            if problem.get("key") == key
            and problem.get("owner") == "external_platform"
            and problem.get("status") == "resolved"
            and problem.get("resolution") == details.get("reason")
        ]
        if len(resolved) != 1:
            return None
        prior_machine_details: Mapping[str, Any] | None = None
        for prior_event in reversed(events[:event_index]):
            if not isinstance(prior_event, Mapping):
                continue
            if prior_event.get("kind") != "machine_verified":
                continue
            candidate_details = prior_event.get("details")
            if isinstance(candidate_details, Mapping):
                prior_machine_details = candidate_details
            break
        failure_signature = None
        if (
            details.get("machine_state") == "failed"
            and isinstance(prior_machine_details, Mapping)
            and prior_machine_details.get("status") == "fail"
        ):
            failure_signature = prior_machine_details.get("failure_signature")
        later_machine = [
            item
            for item in events[event_index + 1 :]
            if isinstance(item, Mapping) and item.get("kind") == "machine_verified"
        ]
        if later_machine:
            first_details = later_machine[0].get("details")
            first_status = (
                first_details.get("status")
                if isinstance(first_details, Mapping)
                else None
            )
            return {
                "state": "succeeded" if first_status == "pass" else "failed",
                "event_index": event_index,
                "failure_signature": failure_signature,
            }
        machine_record = ledger.get("evidence", {}).get("machine")
        current_digest = (
            digest(machine_record) if isinstance(machine_record, dict) else None
        )
        if details.get("machine_evidence_digest") != current_digest:
            return None
        if evidence_state(ledger, "machine") not in {"missing", "stale", "failed"}:
            return None
        return {
            "state": "pending",
            "event_index": event_index,
            "failure_signature": failure_signature,
        }
    return None


def _dispatch_action(
    ledger: Mapping[str, Any], run_id: str, pending: Mapping[str, Any]
) -> dict[str, Any]:
    action = str(pending.get("action"))
    execution = ledger["facets"]["execution"]
    payload: dict[str, Any] = {}
    if action in {
        "builder_implement",
        "builder_fix",
        "tester_author",
        "tester_fix",
        "tester_proof",
        "tester_blackbox",
        "reviewer_preflight",
        "reviewer_final",
    }:
        payload["candidate_worktree"] = ledger["candidate_worktree"]
    if action.startswith("builder_"):
        payload["agent"] = execution["agents"].get("builder")
    if action.startswith("tester_"):
        payload["agent"] = execution["agents"].get("tester")
        payload["tester_source"] = execution.get("tester_source")
    if action.startswith("reviewer_"):
        payload["agent"] = execution["agents"].get("reviewer")
    if action in {"builder_recompose_fix", "tester_recompose_fix"}:
        recomposition = ledger.get("recomposition_intent")
        if not isinstance(recomposition, Mapping) and pending.get("state") != "completed":
            raise AssuranceError(
                "prepared recomposition dispatch lost its current intent",
                code="DISPATCH_ACTION_PAYLOAD_MISSING",
                status="NEEDS_USER",
                details={"action": action},
            )
        payload["recomposition"] = copy.deepcopy(recomposition)
        if action == "builder_recompose_fix" and isinstance(recomposition, Mapping):
            payload["candidate_worktree"] = recomposition.get("builder_worktree")
        if action == "tester_recompose_fix" and isinstance(recomposition, Mapping):
            payload["tester_source"] = {
                "worktree": recomposition.get("tester_worktree"),
                "head": recomposition.get("tester_head"),
            }
    if action == "tester_proof_diagnose":
        failure = current_proof_failure(ledger)
        if not isinstance(failure, Mapping) and pending.get("state") != "completed":
            raise AssuranceError(
                "prepared proof diagnosis lost its current failure",
                code="DISPATCH_ACTION_PAYLOAD_MISSING",
                status="NEEDS_USER",
                details={"action": action},
            )
        payload["proof_failure"] = copy.deepcopy(failure)
    if action == "tester_machine_diagnose":
        failure = current_machine_failure(ledger)
        if not isinstance(failure, Mapping) and pending.get("state") != "completed":
            raise AssuranceError(
                "prepared machine diagnosis lost its current failure",
                code="DISPATCH_ACTION_PAYLOAD_MISSING",
                status="NEEDS_USER",
                details={"action": action},
            )
        payload["machine_failure"] = copy.deepcopy(failure)
    return {
        "driver_protocol_version": 1,
        "status": "CONTINUE",
        "run_id": run_id,
        "action": action,
        "reason": f"dispatch_{pending.get('state')}",
        "action_id": pending["action_id"],
        "driver_enforced": True,
        "driver_runtime_kind": ledger.get("driver_runtime", {}).get("kind")
        if isinstance(ledger.get("driver_runtime"), dict)
        else None,
        "dispatch": copy.deepcopy(pending),
        **payload,
    }


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
        return _decision_result(
            ledger, run_id, status, action, reason, **payload
        )

    def problem_decision(
        problem: Mapping[str, Any], problems: list[dict[str, Any]]
    ) -> dict[str, Any]:
        owner = str(problem.get("owner"))
        if owner == "builder":
            return decision(
                "CONTINUE",
                "builder_fix",
                "open_builder_problem",
                problem=problem,
                agent=ledger["facets"]["execution"]["agents"].get("builder"),
            )
        if owner == "tester":
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
            return decision(
                "CONTINUE",
                "tester_fix",
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
    pending_dispatch = ledger.get("dispatch_intent")
    if isinstance(pending_dispatch, dict):
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
    replacement_problem_key = (
        replacement.get("problem_key")
        if isinstance(replacement, Mapping)
        and replacement.get("stage") == "awaiting_first_turn"
        else None
    )
    open_problems = [
        item
        for item in ledger.get("problems", [])
        if item.get("status") == "open"
        and item.get("key") != replacement_problem_key
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
    if live_candidate != execution.get("candidate_head"):
        return decision(
            "CONTINUE",
            "checkpoint_builder",
            "committed_candidate_uncheckpointed",
            candidate_worktree=ledger["candidate_worktree"],
            candidate_head=live_candidate,
        )
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
    if not ledger.get("builder_checkpointed", False):
        return decision(
            "CONTINUE",
            "builder_implement",
            "candidate_missing",
            candidate_worktree=ledger["candidate_worktree"],
            agent=execution["agents"].get("builder"),
        )
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
            agent=execution["agents"].get("builder"),
        )
    if scan_state == "error":
        return decision(
            "NEEDS_USER",
            "doc_reference_scan_decision",
            "doc_reference_scan_error",
            doc_reference_scan=copy.deepcopy(ledger.get("doc_reference_scan")),
        )
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
        elif state == "failed":
            action = "tester_fix" if kind == "tester" else "builder_fix"
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
    transaction = ledger.get("deployment_transaction")
    lease = ledger.get("environment_lease")
    if (
        isinstance(transaction, dict)
        and transaction.get("state") == "deployed"
        and isinstance(lease, dict)
        and lease.get("state") == "held"
    ):
        return decision("CONTINUE", "restore_deployment", "release_environment_before_finalize")
    return decision("READY", "finalize", "all_gates_pass")
