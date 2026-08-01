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
    pending_dispatch = ledger.get("dispatch_intent")
    if isinstance(pending_dispatch, dict):
        return {
            "driver_protocol_version": 1,
            "status": "CONTINUE",
            "run_id": run_id,
            "action": pending_dispatch["action"],
            "reason": f"dispatch_{pending_dispatch['state']}",
            "action_id": pending_dispatch["action_id"],
            "driver_enforced": True,
            "driver_runtime_kind": ledger.get("driver_runtime", {}).get("kind")
            if isinstance(ledger.get("driver_runtime"), dict)
            else None,
            "dispatch": pending_dispatch,
        }
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
    if ledger["phase"] == "finalizing":
        return decision("CONTINUE", "recover_finalize", "persisted_finalize_intent")
    if ledger["phase"] != "active":
        return decision("STOP", "none", ledger["phase"])
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
        payload = {"problem": latest}
        if action == "builder_fix":
            payload["agent"] = ledger["facets"]["execution"]["agents"].get("builder")
        elif action == "tester_fix":
            payload["agent"] = ledger["facets"]["execution"]["agents"].get("tester")
        return decision("CONTINUE", action, f"open_{owner}_problem", **payload)
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
    if not ledger.get("builder_checkpointed", False):
        return decision(
            "CONTINUE",
            "builder_implement",
            "candidate_missing",
            candidate_worktree=ledger["candidate_worktree"],
            agent=execution["agents"].get("builder"),
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
