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
    current_proof_failure,
    doc_reference_scan_state,
    ensure_run_id,
    evidence_state,
    readiness,
    runtime_compatibility,
    work_unit_progress,
)
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


TESTER_CORRECTION_LIMIT = 3
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


def _tester_correction_progress(ledger: Mapping[str, Any]) -> dict[str, Any]:
    events = [item for item in ledger.get("events", []) if isinstance(item, Mapping)]
    prepared: dict[str, dict[str, Any]] = {}
    last_machine_pass_index = -1
    last_machine_pass_at: Any = None
    for index, event in enumerate(events):
        details = event.get("details")
        details = details if isinstance(details, Mapping) else {}
        if event.get("kind") == "dispatch_prepared":
            action_id = details.get("action_id")
            if details.get("action") == "tester_fix" and isinstance(action_id, str):
                prepared[action_id] = {
                    "action_id": action_id,
                    "prepared_at": event.get("at"),
                }
        elif (
            event.get("kind") == "machine_verified"
            and details.get("status") == "pass"
        ):
            last_machine_pass_index = index
            last_machine_pass_at = event.get("at")

    completed: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for index, event in enumerate(events):
        if index <= last_machine_pass_index or event.get("kind") != "dispatch_consumed":
            continue
        details = event.get("details")
        details = details if isinstance(details, Mapping) else {}
        action_id = details.get("action_id")
        if (
            isinstance(action_id, str)
            and action_id in prepared
            and action_id not in consumed
        ):
            consumed.add(action_id)
            completed.append(
                {
                    **prepared[action_id],
                    "consumed_at": event.get("at"),
                }
            )
    progress = {
        "limit": TESTER_CORRECTION_LIMIT,
        "completed": len(completed),
        "next_tester_fix_blocked": len(completed) >= TESTER_CORRECTION_LIMIT,
        "window_start": {
            "kind": "machine_pass" if last_machine_pass_index >= 0 else "run_start",
            "at": last_machine_pass_at,
        },
        "corrections": completed,
    }
    return {**progress, "progress_digest": digest(progress)}


def _tester_correction_review_binding(
    ledger: Mapping[str, Any],
    progress: Mapping[str, Any],
    reason: str,
    payload: Mapping[str, Any],
) -> str:
    problem = payload.get("problem")
    problem_identity = None
    if isinstance(problem, Mapping):
        problem_identity = {
            "key": problem.get("key"),
            "owner": problem.get("owner"),
            "candidate_head": problem.get("candidate_head"),
        }
    tester_source = ledger["facets"]["execution"].get("tester_source")
    return digest(
        {
            "run_id": ledger["run_id"],
            "reason": reason,
            "candidate_head": ledger["facets"]["execution"].get("candidate_head"),
            "tester_source_head": (
                tester_source.get("head")
                if isinstance(tester_source, Mapping)
                else None
            ),
            "progress_digest": progress["progress_digest"],
            "problem": problem_identity,
        }
    )


def _tester_correction_authorization(
    ledger: Mapping[str, Any], review_binding: str
) -> dict[str, Any] | None:
    consumed = {
        str(details.get("authorization_id"))
        for event in ledger.get("events", [])
        if isinstance(event, Mapping)
        and event.get("kind") == "tester_correction_authorization_consumed"
        and isinstance((details := event.get("details")), Mapping)
        and isinstance(details.get("authorization_id"), str)
    }
    for event in reversed(ledger.get("events", [])):
        if not isinstance(event, Mapping) or event.get("kind") != "tester_correction_authorized":
            continue
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        authorization_id = details.get("authorization_id")
        if (
            details.get("review_binding") == review_binding
            and isinstance(authorization_id, str)
            and authorization_id not in consumed
        ):
            return copy.deepcopy(dict(details))
    return None


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
    role = _work_unit_role_for_action(action)
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
    if isinstance(pending.get("work_unit_id"), str):
        payload["work_unit_id"] = pending["work_unit_id"]
        unit = current_work_unit(
            ledger,
            role=str(pending.get("role")),
        )
        if isinstance(unit, dict) and unit.get("id") == pending.get("work_unit_id"):
            payload["work_unit"] = unit
        else:
            for candidate in work_units(ledger["facets"]):
                if candidate.get("id") == pending.get("work_unit_id"):
                    payload["work_unit"] = candidate
                    break
        payload["work_unit_progress"] = work_unit_progress(ledger)
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
    result = {
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
    role = _work_unit_role_for_action(action)
    if (
        role is not None
        and progress_policy(ledger["facets"]).get("mode") == "bounded_rehydration"
    ):
        projection = canonical_context_projection(
            ledger,
            action_id=str(pending["action_id"]),
            action=action,
            role=role,
            work_unit_id=(
                str(pending["work_unit_id"])
                if isinstance(pending.get("work_unit_id"), str)
                else None
            ),
            work_unit=(
                payload.get("work_unit")
                if isinstance(payload.get("work_unit"), Mapping)
                else None
            ),
        )
        projection_digest = digest(projection)
        stored_digest = pending.get("context_projection_digest")
        if isinstance(stored_digest, str) and stored_digest != projection_digest:
            raise AssuranceError(
                "pending dispatch context projection is stale",
                code="WORK_UNIT_PROJECTION_DIGEST_MISMATCH",
                status="NEEDS_USER",
                details={
                    "expected": projection_digest,
                    "source": stored_digest,
                    "action_id": pending["action_id"],
                    "work_unit_id": pending.get("work_unit_id"),
                },
            )
        result["context_projection"] = projection
        result["context_projection_digest"] = projection_digest
    return result


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
                    except (AssuranceError, ContractError):
                        return contract_problem_decision(ledger, problem, problems)
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
    pending_activation = ledger.get("dispatch_intent")
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
    replacement_problem_key = (
        replacement.get("problem_key")
        if isinstance(replacement, Mapping)
        and replacement.get("stage") == "awaiting_first_turn"
        else None
    )
    included_execution_problem_keys = _included_execution_problem_keys(ledger)
    open_problems = [
        item
        for item in ledger.get("problems", [])
        if item.get("status") == "open"
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
    recoverable_public_problem = any(
        _recoverable_public_prerequisite_problem(item)
        for item in open_problems
    )
    if recoverable_public_problem:
        open_problems = [
            item
            for item in open_problems
            if not _recoverable_public_prerequisite_problem(item)
        ]
        blocking_problems = [
            item
            for item in blocking_problems
            if not _recoverable_public_prerequisite_problem(item)
        ]
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
        return decision(
            "CONTINUE",
            "builder_implement",
            "public_prerequisites_unready",
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
    return decision("READY", "finalize", "all_gates_pass")
