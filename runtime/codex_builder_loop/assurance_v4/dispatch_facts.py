"""Projection of an existing dispatch; never selects or starts work."""
from __future__ import annotations

import copy
from typing import Any, Mapping

from .core import (AssuranceError, canonical_context_projection, current_work_unit,
                   current_machine_failure, current_proof_failure, work_unit_progress)
from .driver_contract import AGENT_ACTION_CAPABILITIES
from .models import digest, progress_policy, work_units


def dispatch_action(
    ledger: Mapping[str, Any], run_id: str, pending: Mapping[str, Any]
) -> dict[str, Any]:
    application = pending.get("application")
    if isinstance(application, Mapping):
        from .result_application import validate_application
        validate_application(ledger, pending)
    action = str(pending.get("action"))
    execution = ledger["facets"]["execution"]
    payload: dict[str, Any] = {}
    if action in {
        "builder_implement",
        "builder_fix",
        "tester_author",
        "tester_fix",
        "tester_observe",
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
    if action == "tester_fix":
        # Recover from the original authorization event, including after consumption.
        consumed_ids = {
            e.get("details", {}).get("authorization_id") for e in ledger.get("events", [])
            if e.get("kind") == "tester_correction_authorization_consumed"
            and e.get("details", {}).get("tester_fix_action_id") == pending.get("action_id")
        }
        matches = [e["details"] for e in ledger.get("events", [])
                   if e.get("kind") == "tester_correction_authorized"
                   and e.get("details", {}).get("authorization_id") in consumed_ids]
        if consumed_ids and (len(consumed_ids) != 1 or len(matches) != 1):
            raise AssuranceError("dispatch lost its unique correction authorization",
                                 code="TESTER_CORRECTION_AUTHORIZATION_MISSING", status="NEEDS_USER")
        if len(matches) == 1:
            payload["tester_correction_authorization"] = copy.deepcopy(matches[0])
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
    capability = AGENT_ACTION_CAPABILITIES.get(action)
    role = capability.role if capability is not None else None
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
        stored_digest = (
            application["projection_digest"] if isinstance(application, Mapping)
            else pending.get("context_projection_digest")
        )
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
