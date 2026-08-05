from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .core import current_proof_failure, ensure_run_id, evidence_state, readiness
from .models import digest
from .store import branch_head, dirty_paths, git, read_ledger, resolve_repo


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
            return decision(
                "NEEDS_USER",
                action,
                reason,
                problem=problem,
                problems=related,
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
    open_problems = [
        item for item in ledger.get("problems", []) if item.get("status") == "open"
    ]
    blocking_problems = [
        item
        for item in open_problems
        if item.get("owner")
        in {"plan", "external_platform", "builder_loop", "current_project"}
    ]
    live_target = branch_head(repo, ledger["target_branch"])
    if live_target != ledger["target_start_head"]:
        return decision("CONTINUE", "rematerialize_target", "target_drift")
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
