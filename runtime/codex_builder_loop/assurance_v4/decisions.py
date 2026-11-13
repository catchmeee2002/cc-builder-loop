"""Semantic revision validation and bound user decisions, independent of role scheduling."""
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
            "updated_at": (None if (ledger.get("driver_runtime") or {}).get("orchestration") == "native_agent" else ledger["updated_at"]),
            "phase": ledger["phase"],
            "digests": ledger["digests"],
            "action": action,
            "reason": reason,
            "payload": payload,
        }
    )
    result = {
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
    capability = AGENT_ACTION_CAPABILITIES.get(action)
    role = capability.role if capability is not None else None
    if (
        role is not None
        and progress_policy(ledger["facets"]).get("mode") == "bounded_rehydration"
    ):
        projection = canonical_context_projection(
            ledger,
            action_id=identity,
            action=action,
            role=role,
            work_unit_id=(
                str(payload["work_unit_id"])
                if isinstance(payload.get("work_unit_id"), str)
                else None
            ),
            work_unit=(
                payload.get("work_unit")
                if isinstance(payload.get("work_unit"), Mapping)
                else None
            ),
        )
        result["context_projection"] = projection
        result["context_projection_digest"] = digest(projection)
    return result



def contract_problem_decision(
    ledger: Mapping[str, Any],
    problem: Mapping[str, Any],
    problems: list[dict[str, Any]],
    *, rejection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    request = problem.get("decision_request")
    payload: dict[str, Any] = {
        "problem": problem,
        "problems": [item for item in problems if item.get("owner") == "plan"],
        "decision_request": copy.deepcopy(request),
        "contract_digest": digest(ledger["facets"]),
        "required_user_block": _contract_decision_user_block(ledger, problem),
    }
    if isinstance(request, Mapping) and request.get("kind") == "facet_change":
        from .decision_requests import validate_request
        payload["request_digest"] = digest(request)
        try:
            validate_request(ledger["facets"], request)
        except AssuranceError as exc:
            payload["request_validation"] = {"status": "FAIL", "code": exc.code,
                "details": exc.details, "correction_command": "correct-decision-request"}
            payload["required_user_block"] = "决策请求格式无效；先由当前 owner 纠正请求，再验证授权变更。"
    if rejection is not None:
        payload["engineering_correction_rejection"] = copy.deepcopy(dict(rejection))
        payload["required_user_block"] += (
            "\n自动修正未执行: " + str(rejection["code"]) + " — " + str(rejection["message"])
            + "\n相关事实: " + json.dumps(rejection.get("details"), ensure_ascii=False, sort_keys=True)
        )
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



def engineering_correction_preview(
    repo: Path,
    ledger: Mapping[str, Any],
    problem: Mapping[str, Any],
) -> dict[str, Any]:
    policy = recovery_policy(ledger["facets"])
    if policy.get("mode") != "automatic_nonsemantic":
        raise AssuranceError(
            "automatic non-semantic recovery is not enabled",
            code="ENGINEERING_CORRECTION_POLICY_MANUAL",
            status="NEEDS_USER",
        )
    request = problem.get("decision_request")
    if (
        problem.get("owner") != "plan"
        or not isinstance(request, Mapping)
        or request.get("kind") != "engineering_correction"
        or request.get("facet") != "assurance"
    ):
        raise AssuranceError(
            "problem is not an exact Assurance engineering correction",
            code="ENGINEERING_CORRECTION_REQUEST_INVALID",
            status="NEEDS_USER",
        )
    _assert_engineering_correction_candidate(repo, ledger)
    current_contract = ledger["facets"]
    old = current_contract["assurance"]
    # Decision pointers are absolute against the frozen contract.  Apply the
    # proposed patch to that complete contract first, then prove that the
    # resulting delta is confined to the Assurance facet.
    replacement = _apply_decision_changes(current_contract, request.get("changes"))
    if replacement == current_contract:
        raise AssuranceError(
            "engineering correction is a no-op",
            code="ENGINEERING_CORRECTION_NOOP",
            status="NEEDS_USER",
        )
    replacement = validate_persisted_contract(replacement)
    new = replacement["assurance"]
    for facet in ("mission", "authority", "execution"):
        if replacement[facet] != current_contract[facet]:
            raise AssuranceError(
                "engineering correction must be confined to the Assurance facet",
                code="ENGINEERING_CORRECTION_NON_ASSURANCE_CHANGE",
                status="NEEDS_USER",
                details={"facet": facet},
            )
    if new.get("profile", "full") != old.get("profile", "full"):
        raise AssuranceError(
            "engineering correction cannot change the requested profile",
            code="ENGINEERING_CORRECTION_PROFILE_CHANGE",
            status="NEEDS_USER",
        )
    if not set(old["required"]).issubset(set(new["required"])):
        raise AssuranceError(
            "engineering correction cannot remove an assurance gate",
            code="ENGINEERING_CORRECTION_ASSURANCE_DOWNGRADE",
            status="NEEDS_USER",
        )
    added_gates = sorted(set(new["required"]) - set(old["required"]))
    if added_gates:
        raise AssuranceError(
            "engineering correction cannot add gates whose execution dependencies were not frozen",
            code="ENGINEERING_CORRECTION_GATE_ADDED",
            status="NEEDS_USER",
            details={"gates": added_gates},
        )
    for field in ("preflight_before_proof", "reviewer_preflight"):
        if old.get(field, False) and not new.get(field, False):
            raise AssuranceError(
                "engineering correction cannot disable an assurance stage",
                code="ENGINEERING_CORRECTION_ASSURANCE_DOWNGRADE",
                status="NEEDS_USER",
                details={"field": field},
            )
    old_commands = {item["id"]: item for item in old["machine_commands"]}
    new_commands = {item["id"]: item for item in new["machine_commands"]}
    missing = sorted(set(old_commands) - set(new_commands))
    if missing:
        raise AssuranceError(
            "engineering correction cannot remove machine commands",
            code="ENGINEERING_CORRECTION_COMMAND_REMOVED",
            status="NEEDS_USER",
            details={"command_ids": missing},
        )
    old_order = [item["id"] for item in old["machine_commands"]]
    new_positions = {
        item["id"]: index for index, item in enumerate(new["machine_commands"])
    }
    old_positions = [new_positions[command_id] for command_id in old_order]
    if old_positions != sorted(old_positions):
        raise AssuranceError(
            "engineering correction cannot reorder existing machine commands",
            code="ENGINEERING_CORRECTION_COMMAND_ORDER_CHANGED",
            status="NEEDS_USER",
            details={
                "old_order": old_order,
                "new_order": [item["id"] for item in new["machine_commands"]],
            },
        )
    for command_id, previous in old_commands.items():
        current = new_commands[command_id]
        stable_previous = {
            key: copy.deepcopy(previous.get(key))
            for key in ("id", "argv", "expected_returncodes", "run_before_full_suite")
        }
        stable_current = {
            key: copy.deepcopy(current.get(key))
            for key in ("id", "argv", "expected_returncodes", "run_before_full_suite")
        }
        if stable_current != stable_previous:
            raise AssuranceError(
                "engineering correction cannot replace machine command semantics",
                code="ENGINEERING_CORRECTION_COMMAND_REPLACED",
                status="NEEDS_USER",
                details={"command_id": command_id},
            )
        if int(current["timeout_seconds"]) < int(previous["timeout_seconds"]):
            raise AssuranceError(
                "engineering correction cannot reduce a machine timeout",
                code="ENGINEERING_CORRECTION_TIMEOUT_REDUCED",
                status="NEEDS_USER",
                details={"command_id": command_id},
            )
    _require_admission(repo, replacement)
    replacement_ledger = copy.deepcopy(dict(ledger))
    replacement_ledger["facets"] = replacement
    replacement_ledger["digests"] = facet_digests(replacement)
    invalidated = sorted(
        kind
        for kind in EVIDENCE_KINDS
        if isinstance(ledger.get("evidence", {}).get(kind), Mapping)
        and ledger["evidence"][kind].get("dependency_digest")
        != evidence_dependency(replacement_ledger, kind)
    )
    return {
        "problem_key": problem["key"],
        "base_assurance_digest": ledger["digests"]["assurance"],
        "replacement_assurance": copy.deepcopy(new),
        "replacement_assurance_digest": replacement_ledger["digests"]["assurance"],
        "replacement_contract_digest": digest(replacement),
        "invalidated_evidence": invalidated,
    }



def _assert_engineering_correction_candidate(
    repo: Path, ledger: Mapping[str, Any]
) -> None:
    """Require the correction to be bound to the current, clean candidate."""

    execution = ledger["facets"]["execution"]
    expected_head = execution.get("candidate_head")
    candidate_branch = ledger.get("candidate_branch")
    candidate_value = ledger.get("candidate_worktree")
    # A correction can be raised before the first Builder checkpoint.  In
    # that state the start transaction has already materialized a clean
    # candidate worktree at target_start_head, but execution.candidate_head is
    # intentionally still null.  Bind that pre-checkpoint state to the
    # frozen target head below; any branch/worktree drift still fails closed.
    if expected_head is None:
        expected_head = ledger.get("target_start_head")
    if (
        not isinstance(expected_head, str)
        or not expected_head
        or not isinstance(candidate_branch, str)
        or not candidate_branch
        or not isinstance(candidate_value, str)
        or not candidate_value
    ):
        raise AssuranceError(
            "automatic engineering correction requires a materialized candidate",
            code="ENGINEERING_CORRECTION_CANDIDATE_INVALID",
            status="NEEDS_USER",
            details={
                "candidate_head": expected_head,
                "candidate_branch": candidate_branch,
                "candidate_worktree": candidate_value,
            },
        )

    candidate_worktree = Path(candidate_value)
    if not candidate_worktree.is_dir():
        raise AssuranceError(
            "automatic engineering correction requires an existing candidate worktree",
            code="ENGINEERING_CORRECTION_CANDIDATE_INVALID",
            status="NEEDS_USER",
            details={"candidate_worktree": candidate_value},
        )

    repository_check = git(
        candidate_worktree, "rev-parse", "--show-toplevel", check=False
    )
    if repository_check.returncode != 0:
        raise AssuranceError(
            "automatic engineering correction requires a Git candidate worktree",
            code="ENGINEERING_CORRECTION_CANDIDATE_INVALID",
            status="NEEDS_USER",
            details={"candidate_worktree": candidate_value},
        )
    actual_root = Path(repository_check.stdout.strip()).resolve()
    if actual_root != candidate_worktree.resolve():
        raise AssuranceError(
            "automatic engineering correction requires the ledger candidate root",
            code="ENGINEERING_CORRECTION_CANDIDATE_INVALID",
            status="NEEDS_USER",
            details={
                "candidate_worktree": candidate_value,
                "git_worktree_root": str(actual_root),
            },
        )

    worktree_head = git(candidate_worktree, "rev-parse", "HEAD", check=False)
    worktree_branch = git(
        candidate_worktree, "symbolic-ref", "--quiet", "HEAD", check=False
    )
    branch_result = git(
        repo,
        "rev-parse",
        "--verify",
        f"refs/heads/{candidate_branch}",
        check=False,
    )
    try:
        dirty = dirty_paths(candidate_worktree)
    except StoreError as exc:
        raise AssuranceError(
            "automatic engineering correction could not inspect the candidate worktree",
            code="ENGINEERING_CORRECTION_CANDIDATE_INVALID",
            status="NEEDS_USER",
            details={
                "candidate_worktree": candidate_value,
                "error": str(exc),
            },
        ) from exc
    actual_head = worktree_head.stdout.strip() if worktree_head.returncode == 0 else None
    actual_branch = (
        worktree_branch.stdout.strip() if worktree_branch.returncode == 0 else None
    )
    branch_head = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    expected_branch = f"refs/heads/{candidate_branch}"
    if (
        actual_head != expected_head
        or actual_branch != expected_branch
        or branch_head != expected_head
        or dirty
    ):
        raise AssuranceError(
            "automatic engineering correction requires a clean, bound candidate",
            code="ENGINEERING_CORRECTION_CANDIDATE_INVALID",
            status="NEEDS_USER",
            details={
                "expected_head": expected_head,
                "actual_head": actual_head,
                "expected_branch": expected_branch,
                "actual_branch": actual_branch,
                "branch_head": branch_head,
                "dirty_paths": dirty,
            },
        )



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
    retain_contract: bool = False,
    reason: str | None = None,
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
    from . import operations
    if operations.is_native(ledger):
        current = operations.problem_view(repo, ledger, problem_key)
    else:
        from .driver import next_action
        current = next_action(repo, run_id)
    current_problem = current.get("problem")
    decision_owners = {"contract_decision": "plan", "current_project_problem_decision": "current_project"}
    if (
        current.get("status") != "NEEDS_USER"
        or current.get("action") not in decision_owners
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
        or current_problem.get("owner") != decision_owners.get(current.get("action"))
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

    project_scope = current_problem.get("owner") == "current_project"
    if project_scope and (facet != "authority" or retain_contract):
        raise AssuranceError("project recovery only grants exact Builder paths",
                             code="DECISION_FACET_UNSUPPORTED", status="FAIL")
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

    replacement = validate_persisted_contract(replacement_contract)
    current_contract = ledger["facets"]
    for unchanged in {"mission", "authority", "assurance"} - {facet}:
        if replacement[unchanged] != current_contract[unchanged]:
            raise AssuranceError(
                "replacement contract changed an unapproved facet",
                code="DECISION_REPLACEMENT_DRIFT",
                status="FAIL",
                details={"facet": unchanged},
            )

    if retain_contract:
        if not isinstance(reason, str) or not reason.strip():
            raise AssuranceError("retaining a contract requires a public execution explanation",
                                 code="DECISION_REASON_REQUIRED", status="FAIL")
        if replacement != current_contract:
            raise AssuranceError("retaining a contract cannot change any facet",
                                 code="DECISION_REPLACEMENT_DRIFT", status="FAIL")
        pending = [field for field in ("dispatch_intent", "candidate_residue_intent",
                   "context_rotation_intent", "dispatch_rehydration_intent", "tester_replacement_intent",
                   "reviewer_replacement_intent", "finalize_intent", "recomposition_intent") if ledger.get(field)]
        if pending or ledger.get("hold"):
            raise AssuranceError("decision conflicts with an unfinished transaction",
                                 code="DECISION_TRANSACTION_PENDING", status="FAIL", details={"transactions": pending})
        return {
            "status": "READY", "run_id": run_id, "problem_key": problem_key,
            "action_id": action_id, "facet": facet, "base_facet_digest": facet_digest,
            "replacement_facet_digest": facet_digest,
            "replacement_contract_digest": digest(replacement), "invalidated_evidence": [],
            "disposition": "retain_contract", "reason": reason.strip(),
            "apply": {"command": "update-facet", "authorization_flags": [],
                      "resolve_plan_problem_key": problem_key, "decision_reason": reason.strip()},
        }
    if reason is not None:
        raise AssuranceError("reason requires explicit retain-contract disposition",
                             code="DECISION_DISPOSITION_REQUIRED", status="FAIL")

    scope_preview = None
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
        if facet == "authority":
            from .decision_requests import authority_scope_preview
            scope_preview = authority_scope_preview(ledger, replacement, project_repair=project_scope)
        if facet != "authority" and replacement["execution"] != current_contract["execution"]:
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
            if authority_expands(old, new) or (scope_preview and scope_preview["linked"]):
                authorization_flags.append("authorize_expansion")
        elif assurance_downgrades(
            current_contract["assurance"], replacement["assurance"]
        ):
            authorization_flags.append("authorize_downgrade")
        command = "apply-authority-decision" if project_scope or (scope_preview and scope_preview["linked"]) else "update-facet"

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
        "scope_preview": scope_preview,
        "apply": {
            "command": command,
            "authorization_flags": authorization_flags,
            "resolve_plan_problem_key": problem_key,
        },
    }
