from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import ensure_run_id, readiness
from .store import branch_head, read_ledger, resolve_repo


def next_action(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    """Derive orchestration advice without mutating Core delivery facts."""

    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    if ledger["phase"] == "finalizing":
        return {
            "status": "CONTINUE",
            "run_id": run_id,
            "action": "recover_finalize",
            "reason": "persisted_finalize_intent",
        }
    if ledger["phase"] != "active":
        return {"status": "STOP", "run_id": run_id, "action": "none", "reason": ledger["phase"]}
    live_target = branch_head(repo, ledger["target_branch"])
    if live_target != ledger["target_start_head"]:
        return {
            "status": "CONTINUE",
            "run_id": run_id,
            "action": "rematerialize_target",
            "reason": "target_drift",
        }
    execution = ledger["facets"]["execution"]
    if not execution.get("candidate_head") or (
        execution.get("candidate_head") == ledger["target_start_head"]
        and not execution.get("builder_files")
        and not execution.get("tester_files")
    ):
        return {
            "status": "CONTINUE",
            "run_id": run_id,
            "action": "builder_implement",
            "reason": "candidate_missing",
        }
    repeated: dict[tuple[str, str], int] = {}
    for event in ledger.get("events", []):
        if event.get("kind") not in {"evidence_recorded", "machine_verified"}:
            continue
        details = event.get("details", {})
        signature = details.get("failure_signature")
        kind = details.get("kind", "machine" if event.get("kind") == "machine_verified" else "")
        if signature and kind:
            repeated[(kind, signature)] = repeated.get((kind, signature), 0) + 1
    if repeated and max(repeated.values()) >= 3:
        return {
            "status": "NEEDS_USER",
            "run_id": run_id,
            "action": "architecture_review",
            "reason": "same_failure_three_times",
        }
    states = readiness(ledger)["states"]
    required = set(ledger["facets"]["assurance"]["required"])
    for kind in ("tester", "machine", "blackbox", "reviewer", "doc_review"):
        if kind not in required:
            continue
        state = states[kind]
        if kind == "tester" and state in {"missing", "stale"}:
            action = "tester_author"
        elif kind == "machine" and state in {"missing", "stale"}:
            action = "verify_machine"
        elif kind == "blackbox" and state in {"missing", "stale"}:
            action = "tester_blackbox"
        elif kind in {"reviewer", "doc_review"} and state in {"missing", "stale"}:
            action = "reviewer_final"
        elif state == "failed":
            action = "tester_fix" if kind == "tester" else "builder_fix"
        else:
            continue
        return {
            "status": "CONTINUE",
            "run_id": run_id,
            "action": action,
            "reason": f"{kind}_{state}",
        }
    return {
        "status": "READY",
        "run_id": run_id,
        "action": "finalize",
        "reason": "all_gates_pass",
    }
