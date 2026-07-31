from __future__ import annotations

from pathlib import Path
from typing import Any

from .core import ensure_run_id, readiness
from .models import digest
from .store import branch_head, dirty_paths, git, read_ledger, resolve_repo


def next_action(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    """Derive orchestration advice without mutating Core delivery facts."""

    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    def decision(status: str, action: str, reason: str, **payload: Any) -> dict[str, Any]:
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
            "status": status,
            "run_id": run_id,
            "action": action,
            "reason": reason,
            "action_id": identity,
            **payload,
        }
    if ledger["phase"] == "finalizing":
        return decision("CONTINUE", "recover_finalize", "persisted_finalize_intent")
    if ledger["phase"] != "active":
        return decision("STOP", "none", ledger["phase"])
    live_target = branch_head(repo, ledger["target_branch"])
    if live_target != ledger["target_start_head"]:
        return decision("CONTINUE", "rematerialize_target", "target_drift")
    open_problems = [item for item in ledger.get("problems", []) if item.get("status") == "open"]
    if open_problems:
        latest = open_problems[-1]
        owner = latest.get("owner")
        if owner == "plan":
            return decision(
                "NEEDS_USER",
                "contract_decision",
                "open_plan_problem",
                problem=latest,
            )
        action = "tester_fix" if owner == "tester" else "builder_fix"
        return decision("CONTINUE", action, f"open_{owner}_problem", problem=latest)
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
        return decision(
            "CONTINUE",
            "builder_implement",
            "candidate_has_uncommitted_work",
            candidate_worktree=ledger["candidate_worktree"],
            dirty_paths=candidate_dirty,
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
    if not ledger.get("builder_checkpointed", False):
        return decision(
            "CONTINUE",
            "builder_implement",
            "candidate_missing",
            candidate_worktree=ledger["candidate_worktree"],
        )
    publication = ledger.get("publication")
    if isinstance(publication, dict) and publication.get("required") and not publication.get("head"):
        return decision(
            "CONTINUE",
            "publish_prerequisites",
            "serial_prerequisites_unpublished",
            paths=publication.get("paths", []),
        )
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
        return decision("NEEDS_USER", "architecture_review", "same_failure_three_times")
    states = readiness(ledger)["states"]
    required = set(ledger["facets"]["assurance"]["required"])
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
        elif kind == "blackbox" and state in {"missing", "stale"}:
            action = "tester_blackbox"
        elif kind in {"reviewer", "doc_review"} and state in {"missing", "stale"}:
            action = "reviewer_final"
        elif state == "failed":
            action = "tester_fix" if kind == "tester" else "builder_fix"
        else:
            continue
        payload: dict[str, Any] = {"candidate_worktree": ledger["candidate_worktree"]}
        if action.startswith("tester_"):
            payload["agent"] = execution["agents"].get("tester")
            payload["tester_source"] = execution.get("tester_source")
        if action == "reviewer_final":
            payload["agent"] = execution["agents"].get("reviewer")
        return decision("CONTINUE", action, f"{kind}_{state}", **payload)
    return decision("READY", "finalize", "all_gates_pass")
