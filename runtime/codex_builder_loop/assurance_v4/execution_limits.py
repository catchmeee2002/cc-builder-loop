"""Bound execution-budget facts; not role scheduling."""
from __future__ import annotations
import copy
from typing import Any, Mapping
from .models import digest
TESTER_CORRECTION_LIMIT = 3

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


def _external_recovery_context(
    ledger: Mapping[str, Any], candidate_head: str | None
) -> dict[str, Any] | None:
    """Derive the one-shot machine recovery boundary from immutable events."""

    from .core import evidence_state
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




def repeated_failures(ledger):
    """Derive failure limits, including an explicitly resolved environment retry."""
    recovery = _external_recovery_context(ledger, ledger['facets']['execution'].get('candidate_head')) or {}
    counts = {}
    kinds = {'machine_verified': 'machine', 'preflight_verified': 'preflight',
             'proof_failure_recorded': 'proof', 'evidence_recorded': None}
    for index, event in enumerate(ledger.get('events', [])):
        if event.get('kind') not in kinds:
            continue
        details = event.get('details') or {}
        signature = details.get('failure_signature')
        kind = details.get('kind', kinds[event['kind']])
        if not signature or not kind:
            continue
        if (kind == 'machine' and signature == recovery.get('failure_signature')
                and (recovery.get('state') == 'pending' or
                     (recovery.get('state') == 'succeeded' and index < recovery['event_index']))):
            continue
        key = (kind, signature)
        counts[key] = counts.get(key, 0) + 1
    return [{'kind': kind, 'failure_signature': signature, 'count': count}
            for (kind, signature), count in sorted(counts.items()) if count >= 3]
