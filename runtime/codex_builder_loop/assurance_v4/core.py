from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import SCHEMA_VERSION
from .models import (
    EVIDENCE_KINDS,
    ContractError,
    assurance_downgrades,
    authority_expands,
    digest,
    evidence_dependency,
    facet_digests,
    validate_contract,
    validate_agent_result,
    validate_environment_probe,
    validate_evidence_report,
    validate_problem_report,
    validate_repo_path,
    validate_telemetry,
    validate_test_proof_spec,
)
from .store import (
    StoreError,
    append_event,
    atomic_write_json,
    branch_head,
    changed_files,
    commit_exists,
    dirty_paths,
    dirty_paths_against,
    git,
    ledger_path,
    locked,
    now,
    read_ledger,
    resolve_repo,
    run_dir,
    save_ledger,
    target_worktree,
)

TRUSTED_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
TRUSTED_SYSTEM_ROOTS = tuple(Path(item).resolve() for item in TRUSTED_SYSTEM_PATH.split(":"))


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AssuranceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: str = "FATAL",
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})


def ensure_run_id(value: str) -> str:
    if not RUN_ID_RE.fullmatch(value):
        raise AssuranceError("invalid run id", code="ASSURANCE_RUN_ID_INVALID")
    return value


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _assert_authorized_files(contract: Mapping[str, Any], files: list[str]) -> None:
    authority = contract["authority"]
    allowed = list(authority["builder_write"]) + list(authority["tester_write"])
    denied = [path for path in files if not _matches(path, allowed)]
    if denied:
        raise AssuranceError(
            "candidate contains files outside the authority contract",
            code="AUTHORITY_WRITE_VIOLATION",
            status="NEEDS_USER",
            details={"paths": denied},
        )


def _file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _validate_dirty_intake(repo: Path, contract: dict[str, Any]) -> list[tuple[str, Path, str]]:
    captured: list[tuple[str, Path, str]] = []
    for item in contract["authority"]["dirty_intake"]:
        relative = validate_repo_path(item["path"])
        source = repo / relative
        if not source.is_file() or source.is_symlink():
            raise AssuranceError(
                "authorized dirty intake must be a regular file",
                code="DIRTY_INTAKE_INVALID",
                status="NEEDS_USER",
                details={"path": relative},
            )
        actual = _file_sha256(source)
        if actual != item["sha256"]:
            raise AssuranceError(
                "authorized dirty intake changed before capture",
                code="DIRTY_INTAKE_DRIFT",
                status="NEEDS_USER",
                details={"path": relative, "expected": item["sha256"], "actual": actual},
            )
        if not _matches(relative, contract["authority"]["builder_write"]):
            raise AssuranceError(
                "dirty intake is outside builder authority",
                code="DIRTY_INTAKE_AUTHORITY_VIOLATION",
                status="NEEDS_USER",
                details={"path": relative},
            )
        captured.append((relative, source, item["sha256"]))
    return captured


def _copy_dirty_intake(worktree: Path, captured: list[tuple[str, Path, str]]) -> list[dict[str, str]]:
    snapshots: list[dict[str, str]] = []
    for relative, source, expected in captured:
        destination = worktree / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied = _file_sha256(destination)
        source_after = _file_sha256(source)
        if copied != expected or source_after != expected:
            raise AssuranceError(
                "authorized dirty intake changed during snapshot capture",
                code="DIRTY_INTAKE_CAPTURE_RACE",
                status="NEEDS_USER",
                details={
                    "path": relative,
                    "expected": expected,
                    "copied": copied,
                    "source_after": source_after,
                },
            )
        snapshots.append({"path": relative, "sha256": expected, "blob": ""})
    return snapshots


def _blob_at(repo: Path, head: str, path: str) -> str | None:
    result = git(repo, "ls-tree", head, "--", path, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    metadata, _, listed_path = result.stdout.rstrip("\n").partition("\t")
    parts = metadata.split()
    if listed_path != path or len(parts) != 3 or parts[1] != "blob":
        return None
    return parts[2]


def validate(contract: Any) -> dict[str, Any]:
    value = validate_contract(contract)
    return {"status": "READY", "schema_version": SCHEMA_VERSION, "digests": facet_digests(value)}


def start(
    repo_value: str | Path,
    run_value: str,
    session_id: str,
    contract_value: Any,
    *,
    driver_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_contract_digest = digest(contract_value)
    contract = validate_contract(contract_value)
    run_id = ensure_run_id(run_value)
    if not session_id.strip():
        raise AssuranceError("session id is required", code="SESSION_ID_REQUIRED")
    repo = resolve_repo(repo_value)
    target_branch = contract["authority"]["target_branch"]
    with locked(repo):
        if ledger_path(repo, run_id).exists():
            existing = read_ledger(repo, run_id)
            continuation = contract["execution"].get("continuation")
            if isinstance(continuation, dict):
                preparation = read_ledger(repo, continuation["preparation_run_id"])
                intent = preparation.get("continuation_consume_intent")
                expected_intent = {
                    "business_run_id": run_id,
                    "target_head": existing["target_start_head"],
                    "contract_digest": source_contract_digest,
                }
                if (
                    intent == expected_intent
                    and preparation.get("continuation_consumed_by") in {None, run_id}
                    and existing["owner_session_id"] == session_id.strip()
                    and existing["facets"]["execution"].get("continuation") == continuation
                ):
                    preparation["continuation_consumed_by"] = run_id
                    preparation["continuation_consume_intent"] = None
                    append_event(
                        preparation,
                        "continuation_consumed",
                        {"business_run_id": run_id, "target_head": existing["target_start_head"]},
                    )
                    save_ledger(repo, preparation)
                    return status(repo, run_id)
            raise AssuranceError("assurance run already exists", code="ASSURANCE_RUN_EXISTS")
        target_head = branch_head(repo, target_branch)
        continuation = contract["execution"].get("continuation")
        preparation_ledger: dict[str, Any] | None = None
        if isinstance(continuation, dict):
            preparation_ledger = read_ledger(repo, continuation["preparation_run_id"])
            if preparation_ledger["phase"] != "finalized":
                raise AssuranceError(
                    "continuation preparation run is not finalized",
                    code="CONTINUATION_PREPARATION_NOT_FINALIZED",
                    status="NEEDS_USER",
                )
            if preparation_ledger.get("final_head") != continuation["preparation_final_head"]:
                raise AssuranceError(
                    "continuation preparation final HEAD does not match",
                    code="CONTINUATION_HEAD_MISMATCH",
                )
            if target_head != continuation["preparation_final_head"]:
                raise AssuranceError(
                    "target does not contain the finalized preparation",
                    code="CONTINUATION_TARGET_MISMATCH",
                    status="NEEDS_USER",
                )
            consumed_by = preparation_ledger.get("continuation_consumed_by")
            if consumed_by not in {None, run_id}:
                raise AssuranceError(
                    "preparation continuation was already consumed",
                    code="CONTINUATION_ALREADY_CONSUMED",
                    details={"consumed_by": consumed_by},
                )
            intent = preparation_ledger.get("continuation_consume_intent")
            expected_intent = {
                "business_run_id": run_id,
                "target_head": target_head,
                "contract_digest": source_contract_digest,
            }
            if intent is not None and intent != expected_intent:
                raise AssuranceError(
                    "preparation has a different pending continuation intent",
                    code="CONTINUATION_INTENT_CONFLICT",
                    status="NEEDS_USER",
                    details={"intent": intent},
                )
            actual_support = set(
                preparation_ledger["facets"]["authority"].get("protected_support_paths", [])
            )
            if set(continuation["support_paths"]) != actual_support:
                raise AssuranceError(
                    "continuation support paths do not match the preparation authority",
                    code="CONTINUATION_SUPPORT_MISMATCH",
                )
        intake_sources = _validate_dirty_intake(repo, contract)
        branch = f"assurance-v4/{run_id}/candidate"
        if git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0:
            raise AssuranceError("candidate branch already exists", code="CANDIDATE_BRANCH_EXISTS")
        worktree = run_dir(repo, run_id) / "candidate"
        if worktree.exists():
            raise AssuranceError("candidate worktree already exists", code="CANDIDATE_WORKTREE_EXISTS")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if preparation_ledger is not None:
            preparation_ledger["continuation_consume_intent"] = expected_intent
            append_event(preparation_ledger, "continuation_consume_intent_written", expected_intent)
            save_ledger(repo, preparation_ledger)
        created = git(repo, "worktree", "add", "-b", branch, str(worktree), target_head, check=False)
        if created.returncode != 0:
            if preparation_ledger is not None:
                preparation_ledger["continuation_consume_intent"] = None
                append_event(
                    preparation_ledger,
                    "continuation_consume_intent_rolled_back",
                    {"business_run_id": run_id},
                )
                save_ledger(repo, preparation_ledger)
            raise AssuranceError(
                created.stderr.strip() or "candidate worktree creation failed",
                code="CANDIDATE_WORKTREE_CREATE_FAILED",
            )
        business_persisted = False
        try:
            snapshots = _copy_dirty_intake(worktree, intake_sources)
            captured = [item["path"] for item in snapshots]
            if snapshots:
                git(worktree, "add", "--", *captured)
                committed = git(
                    worktree,
                    "-c",
                    "commit.gpgSign=false",
                    "commit",
                    "-m",
                    "chore(assurance): [cr_id_skip] Capture Authorized Workspace Intake",
                    check=False,
                )
                if committed.returncode != 0:
                    raise AssuranceError(
                        committed.stderr.strip() or "dirty intake commit failed",
                        code="DIRTY_INTAKE_COMMIT_FAILED",
                    )
            candidate_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
            for item in snapshots:
                blob = _blob_at(repo, candidate_head, item["path"])
                if blob is None:
                    raise AssuranceError(
                        "dirty intake snapshot blob cannot be proven",
                        code="DIRTY_INTAKE_BLOB_MISSING",
                        details={"path": item["path"]},
                    )
                item["blob"] = blob
            execution = copy.deepcopy(contract["execution"])
            execution["candidate_head"] = candidate_head
            execution["dirty_snapshot"] = snapshots
            if snapshots:
                execution["version"] += 1
                execution["builder_files"] = sorted(set(execution["builder_files"]) | set(captured))
            contract["execution"] = execution
            created_at = now()
            ledger = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "owner_session_id": session_id.strip(),
                "phase": "active",
                "repo_root": str(repo),
                "target_branch": target_branch,
                "target_start_head": target_head,
                "candidate_branch": branch,
                "candidate_worktree": str(worktree),
                "facets": contract,
                "digests": facet_digests(contract),
                "evidence": {},
                "retired_tester_sources": [],
                "retired_reviewer_agents": [],
                "driver_runtime": copy.deepcopy(driver_runtime),
                "dispatch_intent": None,
                "deployment_transaction": None,
                "pending_blackbox": None,
                "problems": [],
                "publication": {
                    "required": bool(contract["authority"].get("public_prerequisites")),
                    "paths": list(contract["authority"].get("public_prerequisites", [])),
                    "head": None,
                    "tree": None,
                    "files": [],
                    "manifest_digest": None,
                },
                "builder_checkpointed": False,
                "continuation_consumed_by": None,
                "continuation_consume_intent": None,
                "finalize_intent": None,
                "final_head": None,
                "created_at": created_at,
                "updated_at": created_at,
                "events": [],
            }
            append_event(
                ledger,
                "run_started",
                {"target_head": target_head, "candidate_head": candidate_head, "dirty_intake": captured},
            )
            save_ledger(repo, ledger)
            business_persisted = True
            if preparation_ledger is not None:
                preparation_ledger["continuation_consumed_by"] = run_id
                preparation_ledger["continuation_consume_intent"] = None
                append_event(
                    preparation_ledger,
                    "continuation_consumed",
                    {"business_run_id": run_id, "target_head": target_head},
                )
                save_ledger(repo, preparation_ledger)
        except Exception:
            if business_persisted:
                raise
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            git(repo, "branch", "-D", branch, check=False)
            try:
                worktree.parent.rmdir()
            except OSError:
                pass
            if preparation_ledger is not None:
                current_preparation = read_ledger(repo, continuation["preparation_run_id"])
                if current_preparation.get("continuation_consume_intent") == expected_intent:
                    current_preparation["continuation_consume_intent"] = None
                    append_event(
                        current_preparation,
                        "continuation_consume_intent_rolled_back",
                        {"business_run_id": run_id},
                    )
                    save_ledger(repo, current_preparation)
            raise
    return status(repo, run_id)


def prepare_builder(
    repo_value: str | Path,
    run_value: str,
    agent_id: str,
    thread_id: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    agent = {"agent_id": agent_id.strip(), "thread_id": thread_id.strip()}
    if not all(agent.values()):
        raise AssuranceError("Builder identity is required", code="BUILDER_IDENTITY_REQUIRED")
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        existing = ledger["facets"]["execution"]["agents"].get("builder")
        if existing == agent:
            return status(repo, run_id)
        if existing is not None:
            raise AssuranceError(
                "Builder continuity replacement is not supported",
                code="BUILDER_CONTINUITY_REPLACEMENT_REQUIRED",
                status="NEEDS_USER",
            )
        ledger["facets"]["execution"]["agents"]["builder"] = agent
        ledger["facets"]["execution"]["version"] += 1
        ledger["digests"] = facet_digests(ledger["facets"])
        append_event(ledger, "builder_prepared", {"agent": agent})
        save_ledger(repo, ledger)
    return status(repo, run_id)


def begin_dispatch(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    action: str,
    role: str,
    thread_id: str,
    prompt_digest: str,
    output_schema_digest: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    if role not in {"builder", "tester", "reviewer"}:
        raise AssuranceError("dispatch role is invalid", code="DISPATCH_ROLE_INVALID")
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        runtime = ledger.get("driver_runtime")
        if not isinstance(runtime, dict) or runtime.get("kind") != "native":
            raise AssuranceError("run is not owned by Native Driver", code="NATIVE_DRIVER_NOT_OWNER")
        existing = ledger.get("dispatch_intent")
        if existing is not None:
            if existing.get("action_id") == action_id:
                return status(repo, run_id)
            raise AssuranceError(
                "another dispatch is already pending",
                code="DISPATCH_ALREADY_PENDING",
                status="NEEDS_USER",
            )
        from .driver import next_action

        current = next_action(repo, run_id)
        if current.get("action_id") != action_id or current.get("action") != action:
            raise AssuranceError("driver action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        agent = ledger["facets"]["execution"]["agents"].get(role)
        if not isinstance(agent, dict) or agent.get("thread_id") != thread_id:
            raise AssuranceError("dispatch thread identity mismatch", code="DISPATCH_IDENTITY_MISMATCH")
        ledger["dispatch_intent"] = {
            "action_id": action_id,
            "action": action,
            "role": role,
            "thread_id": thread_id,
            "prompt_digest": prompt_digest,
            "output_schema_digest": output_schema_digest,
            "state": "prepared",
            "attempt": 1,
            "created_at": now(),
        }
        append_event(ledger, "dispatch_prepared", copy.deepcopy(ledger["dispatch_intent"]))
        save_ledger(repo, ledger)
    return status(repo, run_id)


def bind_dispatch_turn(
    repo_value: str | Path, run_value: str, *, action_id: str, turn_id: str
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    if not turn_id.strip():
        raise AssuranceError("turn id is required", code="DISPATCH_TURN_ID_REQUIRED")
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if intent.get("turn_id") not in {None, turn_id.strip()}:
            raise AssuranceError("dispatch turn identity changed", code="DISPATCH_TURN_MISMATCH")
        intent["turn_id"] = turn_id.strip()
        intent["state"] = "in_flight"
        append_event(ledger, "dispatch_turn_bound", {"action_id": action_id, "turn_id": turn_id.strip()})
        save_ledger(repo, ledger)
    return status(repo, run_id)


def complete_dispatch(
    repo_value: str | Path, run_value: str, *, action_id: str, result_value: Any
) -> dict[str, Any]:
    result = validate_agent_result(result_value)
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if intent.get("state") == "completed":
            if intent.get("result_digest") != digest(result):
                raise AssuranceError(
                    "completed dispatch result cannot be replaced",
                    code="DISPATCH_RESULT_MISMATCH",
                )
            return status(repo, run_id)
        artifact = run_dir(repo, run_id) / "artifacts" / f"dispatch-{action_id}.json"
        atomic_write_json(artifact, result)
        intent["state"] = "completed"
        intent["result_path"] = str(artifact)
        intent["result_digest"] = digest(result)
        intent["completed_at"] = now()
        append_event(
            ledger,
            "dispatch_completed",
            {"action_id": action_id, "result": result["result"], "result_digest": intent["result_digest"]},
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def retry_dispatch(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    failure_code: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if intent.get("state") == "completed":
            raise AssuranceError("completed dispatch cannot be retried", code="DISPATCH_ALREADY_COMPLETE")
        attempt = int(intent.get("attempt", 1))
        if attempt >= 3:
            deployment = ledger.get("deployment_transaction")
            if isinstance(deployment, dict) and deployment.get("state") == "deployed":
                deployment["state"] = "restore_required"
                deployment["failure_code"] = "NATIVE_DISPATCH_RETRY_EXHAUSTED"
                append_event(
                    ledger,
                    "dispatch_retry_exhausted_restore_required",
                    {"failure_code": failure_code, "attempt": attempt},
                )
                ledger["dispatch_intent"] = None
                save_ledger(repo, ledger)
                return status(repo, run_id)
            raise AssuranceError(
                "Native role transport failed three times",
                code="NATIVE_DISPATCH_RETRY_EXHAUSTED",
                status="NEEDS_USER",
                details={"failure_code": failure_code, "attempt": attempt},
            )
        append_event(
            ledger,
            "dispatch_retry_scheduled",
            {
                "action_id": action_id,
                "attempt": attempt,
                "turn_id": intent.get("turn_id"),
                "failure_code": failure_code,
            },
        )
        intent["attempt"] = attempt + 1
        intent["state"] = "prepared"
        intent.pop("turn_id", None)
        save_ledger(repo, ledger)
    return status(repo, run_id)


def consume_dispatch(repo_value: str | Path, run_value: str, *, action_id: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if intent.get("state") != "completed":
            raise AssuranceError("dispatch is not complete", code="DISPATCH_NOT_COMPLETE")
        append_event(
            ledger,
            "dispatch_consumed",
            {"action_id": action_id, "result_digest": intent.get("result_digest")},
        )
        ledger["dispatch_intent"] = None
        save_ledger(repo, ledger)
    return status(repo, run_id)


def evidence_state(ledger: Mapping[str, Any], kind: str) -> str:
    record = ledger.get("evidence", {}).get(kind)
    if not isinstance(record, dict):
        return "missing"
    if record.get("status") != "pass":
        return "failed"
    if kind == "tester":
        candidate = ledger["facets"]["execution"].get("candidate_head")
        files = record.get("details", {}).get("files", [])
        if not isinstance(candidate, str) or any(
            not isinstance(item, dict)
            or _blob_at(Path(ledger["repo_root"]), candidate, str(item.get("path", "")))
            != item.get("blob")
            for item in files
        ):
            return "stale"
    dependency_evidence = record if kind == "machine" else None
    if record.get("dependency_digest") != evidence_dependency(
        ledger, kind, evidence=dependency_evidence
    ):
        return "stale"
    return "pass"


def readiness(ledger: Mapping[str, Any]) -> dict[str, Any]:
    required = ledger["facets"]["assurance"]["required"]
    states = {kind: evidence_state(ledger, kind) for kind in required}
    missing = [kind for kind, state in states.items() if state == "missing"]
    stale = [kind for kind, state in states.items() if state == "stale"]
    failed = [kind for kind, state in states.items() if state == "failed"]
    ready = not missing and not stale and not failed and bool(
        ledger["facets"]["execution"].get("candidate_head")
    )
    return {"ready": ready, "states": states, "missing": missing, "stale": stale, "failed": failed}


def _timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def telemetry(ledger: Mapping[str, Any]) -> dict[str, Any]:
    events = [item for item in ledger.get("events", []) if isinstance(item, dict)]
    terminal = ledger.get("phase") in {"finalized", "abandoned"}
    end_at = ledger["updated_at"] if terminal else now()
    elapsed_ms = max(0, _timestamp_ms(end_at) - _timestamp_ms(ledger["created_at"]))
    stage_stats: dict[str, dict[str, Any]] = {}
    dispatches: dict[str, tuple[str, int]] = {}
    evidence_attempts = {kind: 0 for kind in EVIDENCE_KINDS}
    retry_codes: dict[str, int] = {}
    candidate_changes = 0

    def stage(name: str) -> dict[str, Any]:
        return stage_stats.setdefault(
            name,
            {
                "name": name,
                "attempts": 0,
                "completed_attempts": 0,
                "failed_attempts": 0,
                "retry_count": 0,
                "total_duration_ms": 0,
                "last_failure_code": None,
            },
        )

    for event in events:
        kind = event.get("kind")
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        at = event.get("at")
        if kind == "dispatch_prepared" and isinstance(details.get("action_id"), str):
            action = str(details.get("action", "unknown"))
            dispatches[details["action_id"]] = (action, _timestamp_ms(str(at)))
            stage(action)["attempts"] += 1
        elif kind == "dispatch_completed" and isinstance(details.get("action_id"), str):
            dispatch = dispatches.get(details["action_id"])
            if dispatch is not None:
                action, started_at = dispatch
                current = stage(action)
                current["completed_attempts"] += 1
                current["total_duration_ms"] += max(0, _timestamp_ms(str(at)) - started_at)
                if details.get("result") in {
                    "fail",
                    "findings",
                    "blocked",
                    "target_change_required",
                }:
                    current["failed_attempts"] += 1
                    current["last_failure_code"] = str(details["result"])
        elif kind == "dispatch_retry_scheduled":
            action_id = details.get("action_id")
            action = dispatches.get(action_id, ("unknown", 0))[0]
            current = stage(action)
            current["retry_count"] += 1
            failure_code = str(details.get("failure_code", "unknown"))
            current["last_failure_code"] = failure_code
            retry_codes[failure_code] = retry_codes.get(failure_code, 0) + 1
        elif kind == "machine_verified":
            current = stage("verify_machine")
            current["attempts"] += 1
            current["completed_attempts"] += 1
            current["total_duration_ms"] += int(details.get("duration_ms", 0))
            evidence_attempts["machine"] += 1
            if details.get("status") != "pass":
                current["failed_attempts"] += 1
                current["last_failure_code"] = "machine_failed"
        elif kind == "evidence_recorded":
            evidence_kind = details.get("kind")
            if evidence_kind in evidence_attempts:
                evidence_attempts[evidence_kind] += 1
            if evidence_kind == "proof":
                current = stage("tester_proof")
                if current["attempts"] == 0:
                    current["attempts"] = 1
                    current["completed_attempts"] = 1
                current["total_duration_ms"] += int(details.get("duration_ms", 0))
            if details.get("status") == "fail" and isinstance(evidence_kind, str):
                current = stage(f"evidence_{evidence_kind}")
                current["failed_attempts"] += 1
                current["last_failure_code"] = f"{evidence_kind}_failed"
        if kind in {"builder_checkpointed", "tester_source_integrated", "target_rematerialized"}:
            candidate_changes += 1

    pending = ledger.get("dispatch_intent")
    active_stage = pending.get("action") if isinstance(pending, dict) else None
    evidence_replays = sum(max(0, count - 1) for count in evidence_attempts.values())
    return validate_telemetry(
        {
            "schema_version": 1,
            "elapsed_ms": elapsed_ms,
            "active_stage": active_stage,
            "stages": [stage_stats[name] for name in sorted(stage_stats)],
            "candidate_changes": candidate_changes,
            "evidence_attempts": evidence_attempts,
            "evidence_replays": evidence_replays,
            "retries": {
                "total": sum(retry_codes.values()),
                "by_failure_code": dict(sorted(retry_codes.items())),
            },
        }
    )


def status(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    return {
        "status": "READY" if readiness(ledger)["ready"] else "ACTIVE",
        "run_id": run_id,
        "phase": ledger["phase"],
        "repo_root": ledger["repo_root"],
        "target_branch": ledger["target_branch"],
        "target_start_head": ledger["target_start_head"],
        "candidate_branch": ledger["candidate_branch"],
        "candidate_worktree": ledger["candidate_worktree"],
        "digests": ledger["digests"],
        "mission_revision": ledger["facets"]["mission"]["revision"],
        "builder_checkpointed": ledger.get("builder_checkpointed", False),
        "driver_runtime": copy.deepcopy(ledger.get("driver_runtime")),
        "dispatch_intent": copy.deepcopy(ledger.get("dispatch_intent")),
        "deployment_transaction": copy.deepcopy(ledger.get("deployment_transaction")),
        "pending_blackbox": copy.deepcopy(ledger.get("pending_blackbox")),
        "telemetry": telemetry(ledger),
        "readiness": readiness(ledger),
        "publication": copy.deepcopy(ledger.get("publication")),
        "problems": copy.deepcopy(ledger.get("problems", [])),
    }


def driver_context(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    return {
        "status": "READY",
        "run_id": run_id,
        "phase": ledger["phase"],
        "repo_root": ledger["repo_root"],
        "target_start_head": ledger["target_start_head"],
        "candidate_worktree": ledger["candidate_worktree"],
        "facets": copy.deepcopy(ledger["facets"]),
        "evidence": copy.deepcopy(ledger.get("evidence", {})),
        "publication": copy.deepcopy(ledger.get("publication")),
        "problems": copy.deepcopy(ledger.get("problems", [])),
        "driver_runtime": copy.deepcopy(ledger.get("driver_runtime")),
        "dispatch_intent": copy.deepcopy(ledger.get("dispatch_intent")),
        "deployment_transaction": copy.deepcopy(ledger.get("deployment_transaction")),
        "pending_blackbox": copy.deepcopy(ledger.get("pending_blackbox")),
    }


def update_facet(
    repo_value: str | Path,
    run_value: str,
    facet: str,
    value: Any,
    *,
    semantic_revision: bool = False,
    authorize_expansion: bool = False,
    authorize_downgrade: bool = False,
) -> dict[str, Any]:
    if facet not in {"mission", "authority", "assurance", "execution"}:
        raise AssuranceError("unknown contract facet", code="ASSURANCE_FACET_INVALID")
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        old = ledger["facets"][facet]
        if value == old:
            return status(repo, run_id)
        if facet == "execution" and old.get("driver_enforced") is True:
            raise AssuranceError(
                "Full Driver execution facts require dedicated transactions",
                code="DRIVER_EXECUTION_FACET_LOCKED",
            )
        if facet == "mission":
            if not semantic_revision:
                raise AssuranceError(
                    "mission changes require an explicit semantic revision",
                    code="SEMANTIC_REVISION_REQUIRED",
                    status="NEEDS_USER",
                )
            if not isinstance(value, dict) or value.get("revision") != old["revision"] + 1:
                raise AssuranceError(
                    "semantic revision must increment mission.revision by one",
                    code="MISSION_REVISION_INVALID",
                    status="NEEDS_USER",
                )
        elif facet == "authority":
            if not isinstance(value, dict) or value.get("target_branch") != old["target_branch"]:
                raise AssuranceError(
                    "an active run cannot change its target branch",
                    code="AUTHORITY_TARGET_IMMUTABLE",
                    status="NEEDS_USER",
                )
            if value.get("dirty_intake") != old["dirty_intake"]:
                raise AssuranceError(
                    "dirty intake changes require a dedicated snapshot transaction",
                    code="AUTHORITY_DIRTY_INTAKE_IMMUTABLE",
                    status="NEEDS_USER",
                )
            if authority_expands(old, value) and not authorize_expansion:
                raise AssuranceError(
                    "authority expansion requires explicit user authorization",
                    code="AUTHORITY_EXPANSION_REQUIRES_USER",
                    status="NEEDS_USER",
                )
        elif facet == "assurance" and assurance_downgrades(old, value) and not authorize_downgrade:
            raise AssuranceError(
                "assurance downgrade requires explicit user authorization",
                code="ASSURANCE_DOWNGRADE_REQUIRES_USER",
                status="NEEDS_USER",
            )
        candidate = copy.deepcopy(ledger["facets"])
        candidate[facet] = copy.deepcopy(value)
        validated = validate_contract(candidate)
        if facet == "execution":
            if value.get("dirty_snapshot") != old["dirty_snapshot"]:
                raise AssuranceError(
                    "execution dirty snapshot is immutable",
                    code="DIRTY_SNAPSHOT_IMMUTABLE",
                )
            if value.get("tester_files") != old["tester_files"] or value.get("tester_source") != old["tester_source"]:
                raise AssuranceError(
                    "Tester source changes require the dedicated integration transaction",
                    code="TESTER_SOURCE_TRANSACTION_REQUIRED",
                )
            head = validated["execution"].get("candidate_head")
            if not head or not commit_exists(repo, head):
                raise AssuranceError("execution candidate head is invalid", code="CANDIDATE_HEAD_INVALID")
            live_candidate = branch_head(repo, ledger["candidate_branch"])
            worktree_head = git(Path(ledger["candidate_worktree"]), "rev-parse", "HEAD").stdout.strip()
            if head != live_candidate or head != worktree_head:
                raise AssuranceError(
                    "execution candidate must be the live candidate branch and worktree HEAD",
                    code="CANDIDATE_IDENTITY_MISMATCH",
                    details={
                        "declared": head,
                        "branch_head": live_candidate,
                        "worktree_head": worktree_head,
                    },
                )
            if git(repo, "merge-base", "--is-ancestor", ledger["target_start_head"], head, check=False).returncode != 0:
                raise AssuranceError(
                    "candidate is not descended from the frozen target head",
                    code="CANDIDATE_ANCESTRY_INVALID",
                )
            files = changed_files(repo, ledger["target_start_head"], head)
            _assert_authorized_files(validated, files)
            previous_head = old.get("candidate_head")
            if isinstance(previous_head, str) and previous_head != head:
                changed_now = changed_files(repo, previous_head, head)
                tester_touched = [
                    path
                    for path in changed_now
                    if _matches(path, validated["authority"]["tester_write"])
                ]
                if tester_touched:
                    raise AssuranceError(
                        "Builder execution update modified Tester-owned source",
                        code="BUILDER_MODIFIED_TESTER_SOURCE",
                        details={"paths": tester_touched},
                    )
            declared = set(validated["execution"]["builder_files"]) | set(
                validated["execution"]["tester_files"]
            )
            undeclared = sorted(set(files) - declared)
            if undeclared:
                raise AssuranceError(
                    "execution manifest does not classify every candidate file",
                    code="EXECUTION_FILES_INCOMPLETE",
                    details={"paths": undeclared},
                )
            if validated["execution"]["version"] <= old["version"]:
                raise AssuranceError(
                    "execution version must increase",
                    code="EXECUTION_VERSION_INVALID",
                )
        ledger["facets"] = validated
        if facet == "execution" and validated["execution"].get("candidate_head") is not None:
            ledger["builder_checkpointed"] = True
        ledger["digests"] = facet_digests(validated)
        append_event(
            ledger,
            "facet_updated",
            {
                "facet": facet,
                "old_digest": digest(old),
                "new_digest": ledger["digests"][facet],
                "semantic_revision": semantic_revision,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def _close_problems(ledger: dict[str, Any], owner: str, resolution: str) -> None:
    for item in ledger.get("problems", []):
        if item.get("owner") == owner and item.get("status") == "open":
            item["status"] = "resolved"
            item["resolution"] = resolution
            item["resolved_at"] = now()


def checkpoint_builder(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        worktree = Path(ledger["candidate_worktree"])
        if dirty_paths(worktree):
            raise AssuranceError(
                "Builder checkpoint requires a clean committed candidate",
                code="CANDIDATE_WORKTREE_DIRTY",
                status="NEEDS_USER",
            )
        candidate = git(worktree, "rev-parse", "HEAD").stdout.strip()
        if branch_head(repo, ledger["candidate_branch"]) != candidate:
            raise AssuranceError("candidate branch and worktree diverged", code="CANDIDATE_IDENTITY_MISMATCH")
        execution = ledger["facets"]["execution"]
        files = changed_files(repo, ledger["target_start_head"], candidate)
        tester_manifest = {
            item["path"]: item["blob"]
            for item in (execution.get("tester_source") or {}).get("files", [])
        }
        tester_files = set(execution.get("tester_files", []))
        tester_violations = sorted(
            path
            for path in files
            if _matches(path, ledger["facets"]["authority"]["tester_write"])
            and (path not in tester_files or _blob_at(repo, candidate, path) != tester_manifest.get(path))
        )
        if tester_violations:
            raise AssuranceError(
                "Builder checkpoint changed Tester-owned files",
                code="BUILDER_TESTER_OWNERSHIP_VIOLATION",
                details={"paths": tester_violations},
            )
        builder_files = sorted(set(files) - tester_files)
        invalid = [
            path
            for path in builder_files
            if not _matches(path, ledger["facets"]["authority"]["builder_write"])
        ]
        if invalid:
            raise AssuranceError(
                "Builder checkpoint changed files outside authority",
                code="BUILDER_AUTHORITY_VIOLATION",
                details={"paths": invalid},
            )
        publication = ledger.get("publication")
        if isinstance(publication, dict) and publication.get("head"):
            frozen = {item["path"]: item["blob"] for item in publication.get("files", [])}
            changed_public = sorted(
                path for path, blob in frozen.items() if _blob_at(repo, candidate, path) != blob
            )
            if changed_public:
                raise AssuranceError(
                    "published prerequisites are immutable after Tester publication",
                    code="PUBLISHED_PREREQUISITE_DRIFT",
                    details={"paths": changed_public},
                )
        previous = execution.get("candidate_head")
        if (
            previous == candidate
            and execution.get("builder_files") == builder_files
            and ledger.get("builder_checkpointed") is True
        ):
            return status(repo, run_id)
        execution["version"] += 1
        execution["candidate_head"] = candidate
        execution["builder_files"] = builder_files
        ledger["builder_checkpointed"] = True
        ledger["digests"] = facet_digests(ledger["facets"])
        _close_problems(ledger, "builder", f"checkpoint:{candidate}")
        append_event(
            ledger,
            "builder_checkpointed",
            {"old_head": previous, "candidate_head": candidate, "files": builder_files},
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def _materialize_publication(
    repo: Path,
    run_id: str,
    *,
    base_head: str,
    source_head: str,
    paths: list[str],
) -> tuple[str, str, list[dict[str, str]]]:
    files: list[dict[str, str]] = []
    for path in paths:
        blob = _blob_at(repo, source_head, path)
        if blob is None:
            raise AssuranceError(
                "public prerequisite must be a committed regular file",
                code="PUBLIC_PREREQUISITE_INVALID",
                details={"path": path},
            )
        files.append({"path": path, "blob": blob})
    raw = tempfile.mkdtemp(prefix=f"assurance-v4-{run_id}-publication-")
    isolated = Path(raw)
    try:
        added = git(repo, "worktree", "add", "--detach", str(isolated), base_head, check=False)
        if added.returncode != 0:
            raise AssuranceError("publication worktree creation failed", code="PUBLICATION_WORKTREE_FAILED")
        for path in paths:
            checked = git(isolated, "checkout", source_head, "--", path, check=False)
            if checked.returncode != 0:
                raise AssuranceError(
                    "public prerequisite could not be isolated",
                    code="PUBLICATION_CHECKOUT_FAILED",
                    details={"path": path},
                )
        if dirty_paths(isolated):
            committed = git(
                isolated,
                "-c",
                "commit.gpgSign=false",
                "commit",
                "-m",
                "test(assurance): [cr_id_skip] Publish Full Driver Prerequisites",
                check=False,
            )
            if committed.returncode != 0:
                raise AssuranceError("publication commit failed", code="PUBLICATION_COMMIT_FAILED")
        head = git(isolated, "rev-parse", "HEAD").stdout.strip()
        tree = git(isolated, "rev-parse", "HEAD^{tree}").stdout.strip()
        return head, tree, files
    finally:
        git(repo, "worktree", "remove", "--force", str(isolated), check=False)
        shutil.rmtree(isolated, ignore_errors=True)


def publish_prerequisites(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        publication = ledger.get("publication")
        if not isinstance(publication, dict) or not publication.get("required"):
            raise AssuranceError("run has no serial prerequisites", code="PUBLICATION_NOT_REQUIRED")
        if publication.get("head"):
            return status(repo, run_id)
        candidate_worktree = Path(ledger["candidate_worktree"])
        if dirty_paths(candidate_worktree):
            raise AssuranceError("candidate must be clean before publication", code="CANDIDATE_WORKTREE_DIRTY")
        candidate = git(candidate_worktree, "rev-parse", "HEAD").stdout.strip()
        if candidate != ledger["facets"]["execution"].get("candidate_head"):
            raise AssuranceError(
                "Builder checkpoint must bind the candidate before publication",
                code="BUILDER_CHECKPOINT_REQUIRED",
            )
        paths = list(publication["paths"])
        head, tree, files = _materialize_publication(
            repo,
            run_id,
            base_head=ledger["target_start_head"],
            source_head=candidate,
            paths=paths,
        )
        publication.update(
            head=head,
            tree=tree,
            files=files,
            manifest_digest=digest(files),
            candidate_head=candidate,
        )
        append_event(ledger, "prerequisites_published", copy.deepcopy(publication))
        save_ledger(repo, ledger)
    return status(repo, run_id)


def prepare_reviewer(
    repo_value: str | Path,
    run_value: str,
    agent_id: str,
    thread_id: str,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    agent = {"agent_id": agent_id.strip(), "thread_id": thread_id.strip()}
    if not all(agent.values()):
        raise AssuranceError("Reviewer identity is required", code="REVIEWER_IDENTITY_REQUIRED")
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        execution = ledger["facets"]["execution"]
        existing = execution["agents"].get("reviewer")
        if existing == agent:
            return status(repo, run_id)
        if isinstance(existing, dict) and not replace:
            raise AssuranceError(
                "Reviewer continuity replacement must be explicit",
                code="REVIEWER_CONTINUITY_REPLACEMENT_REQUIRED",
                status="NEEDS_USER",
            )
        if isinstance(existing, dict):
            ledger.setdefault("retired_reviewer_agents", []).append(copy.deepcopy(existing))
        execution["agents"]["reviewer"] = agent
        execution["version"] += 1
        ledger["digests"] = facet_digests(ledger["facets"])
        append_event(
            ledger,
            "reviewer_replaced" if isinstance(existing, dict) else "reviewer_prepared",
            {"old_agent": existing, "new_agent": agent},
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def record_problems(
    repo_value: str | Path,
    run_value: str,
    report_value: Any,
    *,
    role: str,
    agent_id: str,
    thread_id: str,
) -> dict[str, Any]:
    report = validate_problem_report(report_value)
    if role not in {"builder", "tester", "reviewer"}:
        raise AssuranceError("problem producer role is invalid", code="PROBLEM_ROLE_INVALID")
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        expected = None
        if role in {"builder", "tester", "reviewer"}:
            expected = ledger["facets"]["execution"]["agents"].get(role)
        if expected is not None:
            if expected != {"agent_id": agent_id, "thread_id": thread_id}:
                raise AssuranceError("problem producer identity mismatch", code="PROBLEM_PRODUCER_MISMATCH")
        candidate = ledger["facets"]["execution"].get("candidate_head")
        for problem in report["problems"]:
            stored = {
                **copy.deepcopy(problem),
                "status": "open",
                "producer": {"role": role, "agent_id": agent_id, "thread_id": thread_id},
                "candidate_head": candidate,
                "recorded_at": now(),
            }
            ledger.setdefault("problems", []).append(stored)
        append_event(
            ledger,
            "problems_recorded",
            {"role": role, "keys": [item["key"] for item in report["problems"]]},
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def prepare_tester(
    repo_value: str | Path,
    run_value: str,
    agent_id: str,
    thread_id: str,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    if not agent_id.strip() or not thread_id.strip():
        raise AssuranceError("Tester identity is required", code="TESTER_IDENTITY_REQUIRED")
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        execution = ledger["facets"]["execution"]
        existing = execution.get("tester_source")
        if isinstance(existing, dict):
            if existing["agent"] == {"agent_id": agent_id, "thread_id": thread_id}:
                return status(repo, run_id)
            if not replace:
                raise AssuranceError(
                    "Tester continuity replacement must be explicit",
                    code="TESTER_CONTINUITY_REPLACEMENT_REQUIRED",
                    status="NEEDS_USER",
                )
            existing_worktree = Path(existing["worktree"])
            if dirty_paths(existing_worktree):
                raise AssuranceError(
                    "lost Tester worktree is dirty and was preserved",
                    code="TESTER_REPLACEMENT_WORKTREE_DIRTY",
                    status="NEEDS_USER",
                )
            live = branch_head(repo, existing["branch"])
            if live != existing["head"]:
                raise AssuranceError(
                    "lost Tester branch drifted and was preserved",
                    code="TESTER_REPLACEMENT_BRANCH_DRIFT",
                    status="NEEDS_USER",
                )
        publication = ledger.get("publication")
        if isinstance(publication, dict) and publication.get("required") and not publication.get("head"):
            raise AssuranceError(
                "serial prerequisites must be published before Tester preparation",
                code="PUBLICATION_REQUIRED",
            )
        tester_base = (
            publication["head"]
            if isinstance(publication, dict) and publication.get("head")
            else ledger["target_start_head"]
        )
        suffix = f"r{execution['version'] + 1}" if isinstance(existing, dict) else "initial"
        branch = f"assurance-v4/{run_id}/tester-{suffix}"
        worktree = run_dir(repo, run_id) / f"tester-{suffix}"
        if git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0 or worktree.exists():
            raise AssuranceError("Tester branch or worktree already exists", code="TESTER_WORKTREE_EXISTS")
        created = git(
            repo,
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            tester_base,
            check=False,
        )
        if created.returncode != 0:
            raise AssuranceError(
                created.stderr.strip() or "Tester worktree creation failed",
                code="TESTER_WORKTREE_CREATE_FAILED",
            )
        agent = {"agent_id": agent_id.strip(), "thread_id": thread_id.strip()}
        replacement = {
            "head": tester_base,
            "base_head": tester_base,
            "branch": branch,
            "worktree": str(worktree),
            "files": [],
            "replaces_files": copy.deepcopy(existing["files"]) if isinstance(existing, dict) else [],
            "agent": agent,
        }
        execution["version"] += 1
        execution["agents"]["tester"] = agent
        execution["tester_source"] = replacement
        if isinstance(existing, dict):
            ledger["retired_tester_sources"].append(copy.deepcopy(existing))
        ledger["digests"] = facet_digests(ledger["facets"])
        append_event(
            ledger,
            "tester_continuity_replaced" if isinstance(existing, dict) else "tester_source_prepared",
            {
                "old_agent": existing["agent"] if isinstance(existing, dict) else None,
                "new_agent": agent,
                "worktree": str(worktree),
            },
        )
        try:
            save_ledger(repo, ledger)
        except Exception:
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            git(repo, "branch", "-D", branch, check=False)
            raise
        if isinstance(existing, dict):
            removed_worktree = git(repo, "worktree", "remove", str(existing_worktree), check=False)
            removed_branch = git(repo, "branch", "-D", existing["branch"], check=False)
            if removed_worktree.returncode == 0 and removed_branch.returncode == 0:
                ledger["retired_tester_sources"] = [
                    item
                    for item in ledger["retired_tester_sources"]
                    if item["branch"] != existing["branch"]
                ]
                append_event(ledger, "retired_tester_source_cleaned", {"branch": existing["branch"]})
                save_ledger(repo, ledger)
            else:
                raise AssuranceError(
                    "Tester replacement was persisted but retired source cleanup is pending",
                    code="TESTER_RETIRED_CLEANUP_PENDING",
                    status="NEEDS_USER",
                    details={
                        "branch": existing["branch"],
                        "worktree": str(existing_worktree),
                        "worktree_remove_stderr": removed_worktree.stderr[-8000:],
                        "branch_remove_stderr": removed_branch.stderr[-8000:],
                    },
                )
    return status(repo, run_id)


def integrate_tester(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        execution = ledger["facets"]["execution"]
        source = execution.get("tester_source")
        if not isinstance(source, dict):
            raise AssuranceError("Tester source is not prepared", code="TESTER_SOURCE_NOT_PREPARED")
        tester_worktree = Path(source["worktree"])
        if dirty_paths(tester_worktree):
            raise AssuranceError(
                "Tester source must be committed before integration",
                code="TESTER_WORKTREE_DIRTY",
                status="NEEDS_USER",
            )
        source_head = branch_head(repo, source["branch"])
        worktree_head = git(tester_worktree, "rev-parse", "HEAD").stdout.strip()
        if source_head != worktree_head:
            raise AssuranceError("Tester branch and worktree diverged", code="TESTER_SOURCE_IDENTITY_MISMATCH")
        source_base = source.get("base_head", ledger["target_start_head"])
        if git(repo, "merge-base", "--is-ancestor", source_base, source_head, check=False).returncode != 0:
            raise AssuranceError("Tester source does not inherit its frozen base", code="TESTER_SOURCE_ANCESTRY_INVALID")
        tester_files = changed_files(repo, source_base, source_head)
        if not tester_files:
            raise AssuranceError("Tester source contains no tests", code="TESTER_SOURCE_EMPTY")
        invalid = [
            path for path in tester_files if not _matches(path, ledger["facets"]["authority"]["tester_write"])
        ]
        if invalid:
            raise AssuranceError(
                "Tester source changed files outside Tester authority",
                code="TESTER_SOURCE_AUTHORITY_VIOLATION",
                details={"paths": invalid},
            )
        manifest: list[dict[str, str]] = []
        for path in tester_files:
            blob = _blob_at(repo, source_head, path)
            if blob is None:
                raise AssuranceError(
                    "Tester source deletion or non-file entry is unsupported in v4 first scope",
                    code="TESTER_SOURCE_ENTRY_UNSUPPORTED",
                    details={"path": path},
                )
            manifest.append({"path": path, "blob": blob})
        candidate_worktree = Path(ledger["candidate_worktree"])
        if dirty_paths(candidate_worktree):
            raise AssuranceError(
                "candidate worktree must be clean before Tester integration",
                code="CANDIDATE_WORKTREE_DIRTY",
                status="NEEDS_USER",
            )
        candidate_before = git(candidate_worktree, "rev-parse", "HEAD").stdout.strip()
        if (
            execution.get("candidate_head") == candidate_before
            and execution.get("tester_files") == tester_files
            and source.get("head") == source_head
            and source.get("files") == manifest
            and all(_blob_at(repo, candidate_before, item["path"]) == item["blob"] for item in manifest)
        ):
            return status(repo, run_id)
        try:
            new_paths = set(tester_files)
            for item in source.get("replaces_files", []):
                if item["path"] in new_paths:
                    continue
                removed = git(candidate_worktree, "rm", "--ignore-unmatch", "--", item["path"], check=False)
                if removed.returncode != 0:
                    raise AssuranceError(
                        "replaced Tester source could not remove an old file",
                        code="TESTER_REPLACEMENT_REMOVE_FAILED",
                        details={"path": item["path"], "stderr": removed.stderr[-8000:]},
                    )
            for path in tester_files:
                checked_out = git(candidate_worktree, "checkout", source_head, "--", path, check=False)
                if checked_out.returncode != 0:
                    raise AssuranceError(
                        "Tester source could not be materialized into candidate",
                        code="TESTER_INTEGRATION_CHECKOUT_FAILED",
                        details={"path": path, "stderr": checked_out.stderr[-8000:]},
                    )
            committed = git(
                candidate_worktree,
                "-c",
                "commit.gpgSign=false",
                "commit",
                "-m",
                f"test(assurance): [cr_id_skip] Integrate Tester Source {source_head[:12]}",
                check=False,
            )
            if committed.returncode != 0:
                raise AssuranceError(
                    committed.stderr.strip() or "Tester integration commit failed",
                    code="TESTER_INTEGRATION_COMMIT_FAILED",
                )
            candidate = git(candidate_worktree, "rev-parse", "HEAD").stdout.strip()
            execution["version"] += 1
            execution["candidate_head"] = candidate
            execution["tester_files"] = tester_files
            execution["tester_source"] = {
                **source,
                "head": source_head,
                "files": manifest,
                "replaces_files": [],
            }
            ledger["digests"] = facet_digests(ledger["facets"])
            _close_problems(ledger, "tester", f"tester-source:{source_head}")
            append_event(
                ledger,
                "tester_source_integrated",
                {"source_head": source_head, "candidate_head": candidate, "files": tester_files},
            )
            save_ledger(repo, ledger)
        except Exception:
            git(candidate_worktree, "reset", "--hard", candidate_before, check=False)
            raise
    return status(repo, run_id)


def record_evidence(
    repo_value: str | Path,
    run_value: str,
    kind: str,
    report: Any,
) -> dict[str, Any]:
    if kind not in EVIDENCE_KINDS or kind == "machine":
        raise AssuranceError("unknown evidence kind", code="EVIDENCE_KIND_INVALID")
    if kind == "proof":
        raise AssuranceError(
            "proof evidence must be produced by the deterministic prove-tests gate",
            code="TEST_PROOF_RUNTIME_REQUIRED",
        )
    report = validate_evidence_report(report)
    if report["kind"] != kind:
        raise AssuranceError("evidence kind does not match the command", code="EVIDENCE_KIND_MISMATCH")
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if report.get("candidate_head") != candidate:
            raise AssuranceError(
                "evidence candidate does not match the execution manifest",
                code="EVIDENCE_CANDIDATE_MISMATCH",
            )
        role = "tester" if kind in {"tester", "proof", "blackbox"} else "reviewer"
        expected_producer = ledger["facets"]["execution"]["agents"].get(role)
        producer = report["producer"]
        if not isinstance(expected_producer, dict) or {
            "agent_id": producer["agent_id"],
            "thread_id": producer["thread_id"],
        } != expected_producer:
            raise AssuranceError(
                "evidence producer does not match the execution manifest",
                code="EVIDENCE_PRODUCER_MISMATCH",
            )
        details = report["details"]
        if kind == "tester":
            if (report["status"] == "pass") != (details["result"] == "tests_ready"):
                raise AssuranceError("Tester result does not match evidence status", code="EVIDENCE_RESULT_MISMATCH")
            tester_source = ledger["facets"]["execution"].get("tester_source")
            if not isinstance(tester_source, dict) or details["source_head"] != tester_source["head"]:
                raise AssuranceError("Tester source head does not match Execution", code="TESTER_SOURCE_HEAD_MISMATCH")
            manifest = {item["path"]: item["blob"] for item in details["files"]}
            expected_manifest = {item["path"]: item["blob"] for item in tester_source["files"]}
            if manifest != expected_manifest:
                raise AssuranceError(
                    "Tester source manifest must exactly cover execution tester files",
                    code="TESTER_SOURCE_MANIFEST_MISMATCH",
                )
            mismatched = [
                path
                for path, blob in manifest.items()
                if _blob_at(repo, details["source_head"], path) != blob
                or _blob_at(repo, candidate, path) != blob
            ]
            if mismatched:
                raise AssuranceError(
                    "Tester source blobs do not match the candidate",
                    code="TESTER_SOURCE_BLOB_MISMATCH",
                    details={"paths": mismatched},
                )
        elif kind == "proof":
            if details["result"] != report["status"]:
                raise AssuranceError("proof result does not match evidence status", code="EVIDENCE_RESULT_MISMATCH")
            tester_source = ledger["facets"]["execution"].get("tester_source")
            if not isinstance(tester_source, dict) or details["source_head"] != tester_source["head"]:
                raise AssuranceError("proof source does not match Tester source", code="PROOF_SOURCE_MISMATCH")
            expected_behaviors = {item["id"] for item in ledger["facets"]["mission"]["behaviors"]}
            if set(details["behaviors"]) != expected_behaviors:
                raise AssuranceError(
                    "proof must cover every frozen behavior exactly",
                    code="PROOF_BEHAVIOR_COVERAGE_MISMATCH",
                )
        elif kind == "blackbox":
            if details["result"] != report["status"]:
                raise AssuranceError("blackbox result does not match evidence status", code="EVIDENCE_RESULT_MISMATCH")
            if Path(details["worktree"]).resolve() != Path(ledger["candidate_worktree"]).resolve():
                raise AssuranceError("blackbox worktree is not the candidate worktree", code="BLACKBOX_WORKTREE_MISMATCH")
            if details["before_head"] != candidate or details["after_head"] != candidate:
                raise AssuranceError("blackbox execution changed or missed the candidate HEAD", code="BLACKBOX_HEAD_MISMATCH")
            declared_commands = [
                (item["id"], item["argv"])
                for item in ledger["facets"]["execution"]["commands"]
            ]
            observed_commands = [
                (item["id"], item["argv"])
                for item in details["executions"]
            ]
            if not declared_commands or observed_commands != declared_commands:
                raise AssuranceError("blackbox executions are not frozen in Execution", code="BLACKBOX_COMMAND_MISMATCH")
            expected = {
                item["id"]: item["expected_returncodes"]
                for item in ledger["facets"]["execution"]["commands"]
            }
            if report["status"] == "pass" and any(
                item["timed_out"] or item["returncode"] not in expected[item["id"]]
                for item in details["executions"]
            ):
                raise AssuranceError(
                    "blackbox execution did not match its frozen expected return codes",
                    code="BLACKBOX_EXECUTION_FAILED",
                )
            deployment = ledger["facets"]["execution"].get("deployment")
            if isinstance(deployment, dict):
                transaction = ledger.get("deployment_transaction")
                observed = details.get("deployment")
                if not isinstance(transaction, dict) or transaction.get("state") != "restored":
                    raise AssuranceError(
                        "deployment-backed blackbox evidence requires a restored environment",
                        code="DEPLOYMENT_NOT_RESTORED",
                    )
                expected_deployment = {
                    "target_id": transaction["target_id"],
                    "artifact_sha256": transaction["artifact_sha256"],
                    "baseline_state_digest": transaction["baseline_probe"]["state_digest"],
                    "deployed_state_digest": transaction["deployed_probe"]["state_digest"],
                    "restored_state_digest": transaction["restored_probe"]["state_digest"],
                }
                if observed != expected_deployment:
                    raise AssuranceError(
                        "blackbox deployment facts do not match the runtime transaction",
                        code="BLACKBOX_DEPLOYMENT_MISMATCH",
                    )
            elif "deployment" in details:
                raise AssuranceError(
                    "blackbox report declared an unexpected deployment",
                    code="BLACKBOX_DEPLOYMENT_UNEXPECTED",
                )
        else:
            if details["reviewed_head"] != candidate:
                raise AssuranceError("review evidence is not bound to the candidate", code="REVIEW_HEAD_MISMATCH")
            if (report["status"] == "pass") != (details["result"] == "pass"):
                raise AssuranceError("review result does not match evidence status", code="EVIDENCE_RESULT_MISMATCH")
        if kind == "reviewer":
            prereqs = [name for name in ("machine", "tester", "proof", "blackbox") if name in ledger["facets"]["assurance"]["required"]]
            blockers = [name for name in prereqs if evidence_state(ledger, name) != "pass"]
            if blockers:
                raise AssuranceError(
                    "final Reviewer evidence requires current prerequisite evidence",
                    code="REVIEWER_PREREQUISITES_MISSING",
                    status="NEEDS_USER",
                    details={"blockers": blockers},
                )
        if kind == "blackbox":
            prereqs = [
                name
                for name in ("tester", "proof", "machine")
                if name in ledger["facets"]["assurance"]["required"]
            ]
            blockers = [name for name in prereqs if evidence_state(ledger, name) != "pass"]
            if blockers:
                raise AssuranceError(
                    "blackbox evidence requires current Tester and machine prerequisites",
                    code="BLACKBOX_PREREQUISITES_MISSING",
                    status="NEEDS_USER",
                    details={"blockers": blockers},
                )
        record = {
            "kind": kind,
            "status": report["status"],
            "dependency_digest": "",
            "candidate_head": candidate,
            "producer": copy.deepcopy(report["producer"]),
            "details": copy.deepcopy(report.get("details", {})),
            "recorded_at": now(),
        }
        ledger["evidence"][kind] = record
        record["dependency_digest"] = evidence_dependency(ledger, kind)
        append_event(
            ledger,
            "evidence_recorded",
            {
                "kind": kind,
                "status": record["status"],
                "failure_signature": digest(record["details"]) if record["status"] == "fail" else None,
            },
        )
        if report["status"] == "pass" and role in {"tester", "reviewer"}:
            _close_problems(ledger, role, f"evidence:{kind}")
        save_ledger(repo, ledger)
    return status(repo, run_id)


def _proof_framework(argv: list[str]) -> str:
    executable = Path(argv[0]).name.lower()
    if executable in {"pytest", "py.test"}:
        return "pytest"
    if executable.startswith(("python", "pypy")) and len(argv) >= 3 and argv[1] == "-m":
        if argv[2] == "pytest":
            return "pytest"
        if argv[2] == "unittest":
            return "unittest"
    raise AssuranceError(
        "test proof requires a direct pytest or unittest command",
        code="TEST_PROOF_COMMAND_UNSUPPORTED",
        status="FAIL",
        details={"argv": argv},
    )


def _proof_run(
    repo: Path,
    worktree: Path,
    head: str,
    group: Mapping[str, Any],
    artifact_root: Path,
    label: str,
) -> dict[str, Any]:
    from ..core import run_proof_argv

    requested = list(group["argv"])
    framework = _proof_framework(requested)
    resolved, identity = _resolve_machine_executable(repo, worktree, head, requested[0])
    if resolved is None:
        raise AssuranceError(
            "test proof executable could not be resolved",
            code="TEST_PROOF_EXECUTABLE_INVALID",
            status="FAIL",
            details={"identity": identity},
        )
    execution_argv = [resolved, *requested[1:]]
    result = run_proof_argv(
        execution_argv,
        framework=framework,
        test_ids=list(group["test_ids"]),
        worktree=worktree,
        timeout=int(group["timeout_seconds"]),
        log_path=artifact_root / f"{label}.log",
        cache_path=artifact_root / f"{label}-cache",
    )
    return {**result, "requested_argv": requested, "executable_identity": identity}


def prove_tests(
    repo_value: str | Path,
    run_value: str,
    spec_value: Any,
    *,
    agent_id: str,
    thread_id: str,
) -> dict[str, Any]:
    spec = validate_test_proof_spec(spec_value)
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    started_at = time.monotonic()
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        if "proof" not in ledger["facets"]["assurance"]["required"]:
            raise AssuranceError("test proof is not required", code="TEST_PROOF_NOT_REQUIRED")
        expected_agent = ledger["facets"]["execution"]["agents"].get("tester")
        if expected_agent != {"agent_id": agent_id, "thread_id": thread_id}:
            raise AssuranceError("proof producer identity mismatch", code="EVIDENCE_PRODUCER_MISMATCH")
        if evidence_state(ledger, "tester") != "pass":
            raise AssuranceError(
                "test proof requires current Tester author evidence",
                code="TEST_PROOF_TESTER_MISSING",
                status="FAIL",
            )
        execution = ledger["facets"]["execution"]
        candidate = execution.get("candidate_head")
        tester_source = execution.get("tester_source")
        if not isinstance(candidate, str) or not isinstance(tester_source, dict):
            raise AssuranceError("test proof source is unavailable", code="TEST_PROOF_SOURCE_MISSING")
        behavior_ids = [item["id"] for item in ledger["facets"]["mission"]["behaviors"]]
        observed_behaviors = [
            behavior
            for group in spec["groups"]
            for behavior in group["behavior_ids"]
        ]
        if sorted(observed_behaviors) != sorted(behavior_ids) or len(observed_behaviors) != len(set(observed_behaviors)):
            raise AssuranceError(
                "test proof groups must cover every frozen behavior exactly once",
                code="PROOF_BEHAVIOR_COVERAGE_MISMATCH",
            )
        source_manifest = {item["path"]: item["blob"] for item in tester_source["files"]}
        mismatched = [
            path
            for path, blob in source_manifest.items()
            if _blob_at(repo, tester_source["head"], path) != blob
            or _blob_at(repo, candidate, path) != blob
        ]
        if mismatched:
            raise AssuranceError(
                "Tester source differs from the integrated candidate",
                code="TEST_PROOF_MANIFEST_MISMATCH",
                status="FAIL",
                details={"paths": mismatched},
            )
        from ..core import proof_test_source_path

        tester_patterns = ledger["facets"]["authority"]["tester_write"]
        unbound_tests: list[dict[str, str]] = []
        for group in spec["groups"]:
            framework = _proof_framework(list(group["argv"]))
            if group["method"] == "reviewed-boundaries":
                boundary_ids = {
                    test_id
                    for values in group["reviewed_boundaries"].values()
                    for test_id in values
                }
                if boundary_ids != set(group["test_ids"]):
                    raise AssuranceError(
                        "reviewed-boundaries ids must exactly equal the executed Tester test ids",
                        code="TEST_PROOF_BOUNDARY_TEST_IDS_INVALID",
                        status="FAIL",
                        details={
                            "declared_test_ids": sorted(group["test_ids"]),
                            "reviewed_boundary_ids": sorted(boundary_ids),
                        },
                    )
            for test_id in group["test_ids"]:
                if proof_test_source_path(
                    repo,
                    tester_source["head"],
                    framework,
                    test_id,
                    tester_patterns,
                ) is None:
                    unbound_tests.append({"test_id": test_id, "framework": framework})
        if unbound_tests:
            raise AssuranceError(
                "test proof ids are not bound to Tester-owned source",
                code="TEST_PROOF_TEST_SOURCE_UNBOUND",
                status="FAIL",
                details={"tests": unbound_tests},
            )
        artifact_root = run_dir(repo, run_id) / "proof-artifacts" / digest(spec)
        artifact_root.mkdir(parents=True, exist_ok=True)
        proof_root = Path(tempfile.mkdtemp(prefix=f"assurance-v4-{run_id}-proof-"))
        created_worktrees: list[Path] = []
        results: list[dict[str, Any]] = []
        try:
            for index, group in enumerate(spec["groups"]):
                candidate_worktree = proof_root / f"candidate-{index}"
                added = git(repo, "worktree", "add", "--detach", str(candidate_worktree), candidate, check=False)
                if added.returncode != 0:
                    raise AssuranceError("proof candidate worktree creation failed", code="TEST_PROOF_WORKTREE_CREATE_FAILED")
                created_worktrees.append(candidate_worktree)
                candidate_result = _proof_run(
                    repo,
                    candidate_worktree,
                    candidate,
                    group,
                    artifact_root,
                    f"group-{index}-candidate",
                )
                if candidate_result["test_result"].get("classification") != "pass":
                    raise AssuranceError(
                        "candidate tests did not pass before effectiveness proof",
                        code="TEST_PROOF_CANDIDATE_FAILED",
                        status="FAIL",
                        details={"group": index, "result": candidate_result},
                    )
                method = group["method"]
                counterexample: dict[str, Any] | None = None
                if method == "baseline-red":
                    baseline_worktree = proof_root / f"baseline-{index}"
                    added = git(repo, "worktree", "add", "--detach", str(baseline_worktree), tester_source["head"], check=False)
                    if added.returncode != 0:
                        raise AssuranceError("proof baseline worktree creation failed", code="TEST_PROOF_WORKTREE_CREATE_FAILED")
                    created_worktrees.append(baseline_worktree)
                    counterexample = _proof_run(
                        repo,
                        baseline_worktree,
                        tester_source["head"],
                        group,
                        artifact_root,
                        f"group-{index}-baseline",
                    )
                elif method == "mutation":
                    mutation_worktree = proof_root / f"mutation-{index}"
                    added = git(repo, "worktree", "add", "--detach", str(mutation_worktree), candidate, check=False)
                    if added.returncode != 0:
                        raise AssuranceError("proof mutation worktree creation failed", code="TEST_PROOF_WORKTREE_CREATE_FAILED")
                    created_worktrees.append(mutation_worktree)
                    applied = subprocess.run(
                        ["git", "-C", str(mutation_worktree), "apply", "--whitespace=nowarn", "-"],
                        input=group["patch"],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    if applied.returncode != 0:
                        raise AssuranceError(
                            "test proof mutation patch could not be applied",
                            code="TEST_PROOF_MUTATION_INVALID",
                            status="FAIL",
                            details={"stderr": applied.stderr[-8000:]},
                        )
                    mutation_paths = dirty_paths(mutation_worktree)
                    invalid_paths = [
                        path
                        for path in mutation_paths
                        if not _matches(path, ledger["facets"]["authority"]["builder_write"])
                        or _matches(path, ledger["facets"]["authority"]["tester_write"])
                        or _blob_at(repo, candidate, path) is None
                        or not (mutation_worktree / path).is_file()
                        or (mutation_worktree / path).is_symlink()
                    ]
                    if not mutation_paths or invalid_paths:
                        raise AssuranceError(
                            "test proof mutation escaped Builder-owned implementation files",
                            code="TEST_PROOF_MUTATION_AUTHORITY_VIOLATION",
                            status="FAIL",
                            details={"paths": invalid_paths or mutation_paths},
                        )
                    counterexample = _proof_run(
                        repo,
                        mutation_worktree,
                        candidate,
                        group,
                        artifact_root,
                        f"group-{index}-mutation",
                    )
                if counterexample is not None and counterexample["test_result"].get("classification") != "assertion-failure":
                    raise AssuranceError(
                        "test proof counterexample was not an assertion failure",
                        code="TEST_PROOF_COUNTEREXAMPLE_INVALID",
                        status="FAIL",
                        details={"group": index, "result": counterexample},
                    )
                results.append(
                    {
                        "behavior_ids": list(group["behavior_ids"]),
                        "method": method,
                        "candidate": candidate_result,
                        "counterexample": counterexample,
                    }
                )
        finally:
            for worktree in reversed(created_worktrees):
                git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(proof_root, ignore_errors=True)
        details = {
            "result": "pass",
            "source_head": tester_source["head"],
            "report_digest": digest({"spec": spec, "results": results}),
            "behaviors": behavior_ids,
            "spec": spec,
            "results": results,
            "artifact_root": str(artifact_root),
        }
        record = {
            "kind": "proof",
            "status": "pass",
            "dependency_digest": "",
            "candidate_head": candidate,
            "producer": {"role": "tester", "agent_id": agent_id, "thread_id": thread_id},
            "details": details,
            "recorded_at": now(),
        }
        ledger["evidence"]["proof"] = record
        record["dependency_digest"] = evidence_dependency(ledger, "proof")
        append_event(
            ledger,
            "evidence_recorded",
            {
                "kind": "proof",
                "status": "pass",
                "failure_signature": None,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def _sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _resolve_machine_executable(
    repo: Path,
    worktree: Path,
    candidate: str,
    value: str,
) -> tuple[str | None, dict[str, Any]]:
    requested = Path(value)
    if not requested.is_absolute() and "/" in value:
        if any(part in {"", ".", ".."} for part in requested.parts):
            raise AssuranceError(
                "repository machine executable path must be canonical",
                code="MACHINE_EXECUTABLE_PATH_INVALID",
                details={"requested": value},
            )
        declared = worktree / requested
        try:
            resolved = declared.resolve(strict=True)
        except OSError:
            return None, {"kind": "repository", "requested": value, "reason": "not_found"}
        try:
            relative = resolved.relative_to(worktree.resolve()).as_posix()
        except ValueError as exc:
            raise AssuranceError(
                "repository machine executable escapes the candidate worktree",
                code="MACHINE_EXECUTABLE_OUTSIDE_CANDIDATE",
                details={"requested": value},
            ) from exc
        cursor = declared
        while cursor != worktree:
            if cursor.is_symlink():
                raise AssuranceError(
                    "repository machine executable cannot traverse symlinks",
                    code="MACHINE_EXECUTABLE_SYMLINK",
                    details={"requested": value},
                )
            cursor = cursor.parent
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise AssuranceError(
                "repository machine executable must be a regular executable file",
                code="MACHINE_EXECUTABLE_INVALID",
                details={"requested": value},
            )
        blob = _blob_at(repo, candidate, relative)
        if blob is None:
            raise AssuranceError(
                "repository machine executable is not frozen in the candidate",
                code="MACHINE_EXECUTABLE_NOT_FROZEN",
                details={"requested": value, "path": relative},
            )
        return str(resolved), {
            "kind": "repository",
            "requested": value,
            "path": relative,
            "blob": blob,
        }

    if requested.is_absolute():
        executable = requested
        resolution = "explicit_absolute"
        if not executable.exists():
            return None, {"kind": "system", "requested": value, "reason": "not_found"}
    else:
        found = shutil.which(value, path=TRUSTED_SYSTEM_PATH)
        if found is None:
            return None, {"kind": "system", "requested": value, "reason": "not_found"}
        executable = Path(found)
        resolution = "trusted_path"
    try:
        resolved = executable.resolve(strict=True)
    except OSError:
        return None, {"kind": "system", "requested": value, "reason": "not_found"}
    if resolution == "trusted_path" and not any(
        resolved.is_relative_to(root) for root in TRUSTED_SYSTEM_ROOTS
    ):
        raise AssuranceError(
            "system machine executable is outside the trusted system path",
            code="MACHINE_EXECUTABLE_UNTRUSTED",
            details={"requested": value, "resolved": str(resolved)},
        )
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise AssuranceError(
            "system machine executable must be a regular executable file",
            code="MACHINE_EXECUTABLE_INVALID",
            details={"requested": value, "resolved": str(resolved)},
        )
    return str(resolved), {
        "kind": "system",
        "requested": value,
        "resolution": resolution,
        "path": str(resolved),
        "sha256": _sha256_file(resolved),
    }


def _run_frozen_command(
    repo: Path,
    worktree: Path,
    candidate: str,
    command: Mapping[str, Any],
    *,
    artifact_path: str | None = None,
    artifact_sha256: str | None = None,
) -> dict[str, Any]:
    executable, identity = _resolve_machine_executable(
        repo, worktree, candidate, command["argv"][0]
    )
    if executable is None:
        return {
            "id": command["id"],
            "argv": command["argv"],
            "returncode": None,
            "stdout": "",
            "stderr": "executable not found",
            "timed_out": False,
            "executable_identity": identity,
        }
    env = {
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": TRUSTED_SYSTEM_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if artifact_path is not None:
        env["BUILDER_LOOP_ARTIFACT_PATH"] = artifact_path
    if artifact_sha256 is not None:
        env["BUILDER_LOOP_ARTIFACT_SHA256"] = artifact_sha256
    try:
        completed = subprocess.run(
            [executable, *command["argv"][1:]],
            cwd=worktree,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=command["timeout_seconds"],
            check=False,
            env=env,
        )
        return {
            "id": command["id"],
            "argv": command["argv"],
            "returncode": completed.returncode,
            "stdout": completed.stdout[-8000:],
            "stderr": completed.stderr[-8000:],
            "timed_out": False,
            "executable_identity": identity,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "id": command["id"],
            "argv": command["argv"],
            "returncode": None,
            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
            "timed_out": True,
            "executable_identity": identity,
        }


def _command_passed(command: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    return not result["timed_out"] and result["returncode"] in command["expected_returncodes"]


def _probe_environment(
    repo: Path,
    worktree: Path,
    candidate: str,
    deployment: Mapping[str, Any],
    *,
    artifact_sha256: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = _run_frozen_command(
        repo,
        worktree,
        candidate,
        deployment["probe_command"],
        artifact_path=deployment["artifact_path"],
        artifact_sha256=artifact_sha256,
    )
    if not _command_passed(deployment["probe_command"], result):
        raise AssuranceError(
            "environment probe command failed",
            code="DEPLOYMENT_PROBE_FAILED",
            status="NEEDS_USER",
            details={"result": result},
        )
    lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
    if not lines:
        raise AssuranceError(
            "environment probe produced no JSON",
            code="DEPLOYMENT_PROBE_OUTPUT_MISSING",
        )
    try:
        probe = validate_environment_probe(json.loads(lines[-1]))
    except (json.JSONDecodeError, ContractError) as exc:
        raise AssuranceError(
            "environment probe output is invalid",
            code=getattr(exc, "code", "DEPLOYMENT_PROBE_OUTPUT_INVALID"),
            details=getattr(exc, "details", {}),
        ) from exc
    if probe["target_id"] != deployment["target_id"]:
        raise AssuranceError(
            "environment probe target does not match the authorized target",
            code="DEPLOYMENT_TARGET_MISMATCH",
        )
    return probe, result


def prepare_deployment(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        deployment = ledger["facets"]["execution"].get("deployment")
        if not isinstance(deployment, dict):
            raise AssuranceError("run has no deployment contract", code="DEPLOYMENT_NOT_REQUIRED")
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if not isinstance(candidate, str):
            raise AssuranceError("deployment candidate is unavailable", code="CANDIDATE_HEAD_INVALID")
        prereqs = [
            kind
            for kind in ("tester", "proof", "machine")
            if kind in ledger["facets"]["assurance"]["required"]
        ]
        blockers = [kind for kind in prereqs if evidence_state(ledger, kind) != "pass"]
        if blockers:
            raise AssuranceError(
                "deployment requires current local assurance evidence",
                code="DEPLOYMENT_PREREQUISITES_MISSING",
                status="NEEDS_USER",
                details={"blockers": blockers},
            )
        transaction = ledger.get("deployment_transaction")
        if isinstance(transaction, dict):
            if transaction["state"] == "restored" and transaction["candidate_head"] != candidate:
                git(repo, "worktree", "remove", "--force", transaction["worktree"], check=False)
                shutil.rmtree(transaction["worktree"], ignore_errors=True)
                ledger["deployment_transaction"] = None
                transaction = None
                save_ledger(repo, ledger)
            elif transaction["candidate_head"] != candidate:
                raise AssuranceError(
                    "deployment transaction is stale for the candidate",
                    code="DEPLOYMENT_CANDIDATE_STALE",
                )
            if isinstance(transaction, dict) and transaction["state"] == "restored":
                git(repo, "worktree", "remove", "--force", transaction["worktree"], check=False)
                shutil.rmtree(transaction["worktree"], ignore_errors=True)
                ledger["deployment_transaction"] = None
                transaction = None
                save_ledger(repo, ledger)
            if isinstance(transaction, dict) and transaction["state"] in {"deployed", "restore_required", "restoring", "restore_failed"}:
                return status(repo, run_id)
            if isinstance(transaction, dict):
                worktree = Path(transaction["worktree"])
        if not isinstance(transaction, dict):
            worktree = run_dir(repo, run_id) / "deployment"
            if worktree.exists():
                raise AssuranceError(
                    "unregistered deployment worktree exists",
                    code="DEPLOYMENT_WORKTREE_EXISTS",
                    status="NEEDS_USER",
                )
            created = git(repo, "worktree", "add", "--detach", str(worktree), candidate, check=False)
            if created.returncode != 0:
                raise AssuranceError(
                    created.stderr.strip() or "deployment worktree creation failed",
                    code="DEPLOYMENT_WORKTREE_CREATE_FAILED",
                )
            transaction = {
                "transaction_id": digest(
                    {"run_id": run_id, "candidate_head": candidate, "target_id": deployment["target_id"]}
                ),
                "state": "preparing",
                "candidate_head": candidate,
                "target_id": deployment["target_id"],
                "worktree": str(worktree),
                "artifact_path": deployment["artifact_path"],
                "artifact_sha256": None,
                "baseline_probe": None,
                "deployed_probe": None,
                "restored_probe": None,
                "failure_code": None,
            }
            ledger["deployment_transaction"] = transaction
            append_event(ledger, "deployment_prepared", {"transaction_id": transaction["transaction_id"]})
            save_ledger(repo, ledger)
        baseline, probe_result = _probe_environment(
            repo, worktree, candidate, deployment, artifact_sha256=None
        )
        transaction["baseline_probe"] = baseline
        build_result = _run_frozen_command(
            repo, worktree, candidate, deployment["build_command"], artifact_path=deployment["artifact_path"]
        )
        if not _command_passed(deployment["build_command"], build_result):
            transaction["failure_code"] = "DEPLOYMENT_BUILD_FAILED"
            transaction["state"] = "restored"
            transaction["restored_probe"] = baseline
            append_event(ledger, "deployment_build_failed", {"result": build_result})
            save_ledger(repo, ledger)
            return status(repo, run_id)
        artifact_entry = worktree / deployment["artifact_path"]
        artifact = artifact_entry.resolve()
        if (
            artifact_entry.is_symlink()
            or not artifact.is_relative_to(worktree.resolve())
            or not artifact.is_file()
        ):
            transaction["failure_code"] = "DEPLOYMENT_ARTIFACT_INVALID"
            transaction["state"] = "restored"
            transaction["restored_probe"] = baseline
            save_ledger(repo, ledger)
            return status(repo, run_id)
        transaction["artifact_sha256"] = _sha256_file(artifact)
        transaction["state"] = "deploying"
        append_event(
            ledger,
            "deployment_intent_written",
            {"artifact_sha256": transaction["artifact_sha256"], "baseline_probe": baseline},
        )
        save_ledger(repo, ledger)
        deploy_result = _run_frozen_command(
            repo,
            worktree,
            candidate,
            deployment["deploy_command"],
            artifact_path=deployment["artifact_path"],
            artifact_sha256=transaction["artifact_sha256"],
        )
        if not _command_passed(deployment["deploy_command"], deploy_result):
            transaction["state"] = "restore_required"
            transaction["failure_code"] = "DEPLOYMENT_COMMAND_FAILED"
            append_event(ledger, "deployment_command_failed", {"result": deploy_result})
            save_ledger(repo, ledger)
            return status(repo, run_id)
        try:
            deployed, deployed_probe_result = _probe_environment(
                repo,
                worktree,
                candidate,
                deployment,
                artifact_sha256=transaction["artifact_sha256"],
            )
        except AssuranceError as exc:
            transaction["state"] = "restore_required"
            transaction["failure_code"] = exc.code
            append_event(ledger, "deployment_probe_failed", {"code": exc.code})
            save_ledger(repo, ledger)
            return status(repo, run_id)
        if deployed["deployed_artifact_sha256"] != transaction["artifact_sha256"]:
            transaction["state"] = "restore_required"
            transaction["failure_code"] = "DEPLOYMENT_ARTIFACT_MISMATCH"
            save_ledger(repo, ledger)
            return status(repo, run_id)
        if _sha256_file(artifact) != transaction["artifact_sha256"]:
            transaction["state"] = "restore_required"
            transaction["failure_code"] = "DEPLOYMENT_ARTIFACT_DRIFT"
            save_ledger(repo, ledger)
            return status(repo, run_id)
        transaction["deployed_probe"] = deployed
        transaction["state"] = "deployed"
        append_event(
            ledger,
            "deployment_completed",
            {"probe": deployed, "probe_result": deployed_probe_result, "baseline_probe_result": probe_result},
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def stage_blackbox(repo_value: str | Path, run_value: str, report_value: Any) -> dict[str, Any]:
    report = validate_evidence_report(report_value)
    if report["kind"] != "blackbox":
        raise AssuranceError("staged evidence must be blackbox", code="EVIDENCE_KIND_MISMATCH")
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        transaction = ledger.get("deployment_transaction")
        if not isinstance(transaction, dict) or transaction["state"] != "deployed":
            raise AssuranceError("deployment is not active", code="DEPLOYMENT_NOT_ACTIVE")
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if report["candidate_head"] != candidate:
            raise AssuranceError("blackbox candidate is stale", code="EVIDENCE_CANDIDATE_MISMATCH")
        pending = ledger.get("pending_blackbox")
        report_digest = digest(report)
        if isinstance(pending, dict):
            if pending["report_digest"] != report_digest:
                raise AssuranceError("staged blackbox result changed", code="PENDING_BLACKBOX_MISMATCH")
            return status(repo, run_id)
        ledger["pending_blackbox"] = {
            "report": report,
            "report_digest": report_digest,
            "candidate_head": candidate,
        }
        append_event(ledger, "blackbox_result_staged", {"report_digest": report_digest})
        save_ledger(repo, ledger)
    return status(repo, run_id)


def restore_deployment(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        deployment = ledger["facets"]["execution"].get("deployment")
        transaction = ledger.get("deployment_transaction")
        if not isinstance(deployment, dict) or not isinstance(transaction, dict):
            raise AssuranceError("deployment transaction is missing", code="DEPLOYMENT_TRANSACTION_MISSING")
        if transaction["state"] == "restored":
            return status(repo, run_id)
        if transaction["state"] == "restore_failed":
            raise AssuranceError(
                "deployment restore previously failed",
                code="DEPLOYMENT_RESTORE_FAILED",
                status="NEEDS_USER",
            )
        baseline = transaction.get("baseline_probe")
        if not isinstance(baseline, dict):
            raise AssuranceError("deployment baseline is missing", code="DEPLOYMENT_BASELINE_MISSING")
        transaction["state"] = "restoring"
        append_event(ledger, "deployment_restore_intent_written", {})
        save_ledger(repo, ledger)
        worktree = Path(transaction["worktree"])
        result = _run_frozen_command(
            repo,
            worktree,
            transaction["candidate_head"],
            deployment["restore_command"],
            artifact_path=transaction["artifact_path"],
            artifact_sha256=transaction.get("artifact_sha256"),
        )
        if not _command_passed(deployment["restore_command"], result):
            transaction["state"] = "restore_failed"
            transaction["failure_code"] = "DEPLOYMENT_RESTORE_FAILED"
            append_event(ledger, "deployment_restore_failed", {"result": result})
            save_ledger(repo, ledger)
            raise AssuranceError(
                "deployment restore command failed",
                code="DEPLOYMENT_RESTORE_FAILED",
                status="NEEDS_USER",
                details={"result": result},
            )
        try:
            restored, probe_result = _probe_environment(
                repo,
                worktree,
                transaction["candidate_head"],
                deployment,
                artifact_sha256=transaction.get("artifact_sha256"),
            )
        except AssuranceError as exc:
            transaction["state"] = "restore_failed"
            transaction["failure_code"] = exc.code
            append_event(ledger, "deployment_restore_probe_failed", {"code": exc.code})
            save_ledger(repo, ledger)
            raise AssuranceError(
                "deployment restore could not be verified",
                code=exc.code,
                status="NEEDS_USER",
                details=exc.details,
            ) from exc
        if restored["state_digest"] != baseline["state_digest"]:
            transaction["state"] = "restore_failed"
            transaction["failure_code"] = "DEPLOYMENT_RESTORE_STATE_MISMATCH"
            save_ledger(repo, ledger)
            raise AssuranceError(
                "deployment restore did not recover the baseline state",
                code="DEPLOYMENT_RESTORE_STATE_MISMATCH",
                status="NEEDS_USER",
            )
        transaction["restored_probe"] = restored
        transaction["state"] = "restored"
        append_event(ledger, "deployment_restored", {"probe": restored, "probe_result": probe_result})
        save_ledger(repo, ledger)
    return status(repo, run_id)


def require_deployment_restore(
    repo_value: str | Path, run_value: str, *, failure_code: str
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        transaction = ledger.get("deployment_transaction")
        if not isinstance(transaction, dict) or transaction.get("state") != "deployed":
            raise AssuranceError("deployment is not active", code="DEPLOYMENT_NOT_ACTIVE")
        transaction["state"] = "restore_required"
        transaction["failure_code"] = failure_code
        ledger["dispatch_intent"] = None
        append_event(ledger, "deployment_restore_required", {"failure_code": failure_code})
        save_ledger(repo, ledger)
    return status(repo, run_id)


def complete_staged_blackbox(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        pending = copy.deepcopy(ledger.get("pending_blackbox"))
        transaction = copy.deepcopy(ledger.get("deployment_transaction"))
        if not isinstance(pending, dict) or not isinstance(transaction, dict):
            raise AssuranceError("staged blackbox result is missing", code="PENDING_BLACKBOX_MISSING")
        if transaction["state"] != "restored":
            raise AssuranceError("deployment is not restored", code="DEPLOYMENT_NOT_RESTORED")
    report = pending["report"]
    report["details"]["deployment"] = {
        "target_id": transaction["target_id"],
        "artifact_sha256": transaction["artifact_sha256"],
        "baseline_state_digest": transaction["baseline_probe"]["state_digest"],
        "deployed_state_digest": transaction["deployed_probe"]["state_digest"],
        "restored_state_digest": transaction["restored_probe"]["state_digest"],
    }
    result = record_evidence(repo, run_id, "blackbox", report)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger.get("pending_blackbox", {}).get("report_digest") == pending["report_digest"]:
            ledger["pending_blackbox"] = None
            append_event(ledger, "blackbox_result_completed", {"report_digest": pending["report_digest"]})
            save_ledger(repo, ledger)
    return result


def verify_machine(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    started_at = time.monotonic()
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if not candidate or not commit_exists(repo, candidate):
            raise AssuranceError("candidate head is unavailable", code="CANDIDATE_HEAD_INVALID")
        commands = ledger["facets"]["assurance"]["machine_commands"]
        commands = sorted(
            enumerate(commands),
            key=lambda item: (not item[1].get("run_before_full_suite", False), item[0]),
        )
        commands = [item[1] for item in commands]
        if "machine" in ledger["facets"]["assurance"]["required"] and not commands:
            raise AssuranceError("machine evidence requires at least one command", code="MACHINE_COMMAND_REQUIRED")
        raw_temp = tempfile.mkdtemp(prefix=f"assurance-v4-{run_id}-verify-")
        verify_worktree = Path(raw_temp)
        added = git(repo, "worktree", "add", "--detach", str(verify_worktree), candidate, check=False)
        if added.returncode != 0:
            shutil.rmtree(verify_worktree, ignore_errors=True)
            raise AssuranceError(
                added.stderr.strip() or "verification worktree creation failed",
                code="VERIFY_WORKTREE_CREATE_FAILED",
            )
        results: list[dict[str, Any]] = []
        try:
            for command in commands:
                executable, executable_identity = _resolve_machine_executable(
                    repo,
                    verify_worktree,
                    candidate,
                    command["argv"][0],
                )
                if executable is None:
                    results.append(
                        {
                            "id": command["id"],
                            "argv": command["argv"],
                            "returncode": None,
                            "stdout": "",
                            "stderr": "executable not found",
                            "timed_out": False,
                            "executable": None,
                            "executable_identity": executable_identity,
                        }
                    )
                    break
                argv = [executable, *command["argv"][1:]]
                try:
                    completed = subprocess.run(
                        argv,
                        cwd=verify_worktree,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=command["timeout_seconds"],
                        check=False,
                        env={
                            "HOME": os.environ.get("HOME", ""),
                            "LANG": os.environ.get("LANG", "C.UTF-8"),
                            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                            "PATH": TRUSTED_SYSTEM_PATH,
                            "PYTHONDONTWRITEBYTECODE": "1",
                        },
                    )
                    results.append(
                        {
                            "id": command["id"],
                            "argv": command["argv"],
                            "executable": executable,
                            "executable_identity": executable_identity,
                            "returncode": completed.returncode,
                            "stdout": completed.stdout[-8000:],
                            "stderr": completed.stderr[-8000:],
                            "timed_out": False,
                        }
                    )
                    if completed.returncode not in command["expected_returncodes"]:
                        break
                except subprocess.TimeoutExpired as exc:
                    results.append(
                        {
                            "id": command["id"],
                            "argv": command["argv"],
                            "executable": executable,
                            "executable_identity": executable_identity,
                            "returncode": None,
                            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
                            "stderr": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
                            "timed_out": True,
                        }
                    )
                    break
        finally:
            git(repo, "worktree", "remove", "--force", str(verify_worktree), check=False)
            shutil.rmtree(verify_worktree, ignore_errors=True)
        declared = {item["id"]: item for item in commands}
        passed = len(results) == len(commands) and all(
            not item["timed_out"]
            and item["returncode"] in declared[item["id"]]["expected_returncodes"]
            for item in results
        )
        report = {
            "status": "pass" if passed else "fail",
            "candidate_head": candidate,
            "details": {"commands": results},
        }
        record = {
            "kind": "machine",
            "status": report["status"],
            "dependency_digest": "",
            "candidate_head": candidate,
            "producer": {
                "role": "runtime",
                "agent_id": "assurance-core-v4",
                "thread_id": "deterministic-machine",
            },
            "details": report["details"],
            "recorded_at": now(),
        }
        ledger["evidence"]["machine"] = record
        record["dependency_digest"] = evidence_dependency(ledger, "machine", evidence=record)
        append_event(
            ledger,
            "machine_verified",
            {
                "status": record["status"],
                "commands": len(results),
                "failure_signature": digest(record["details"]) if record["status"] == "fail" else None,
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def rematerialize_target(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        old_base = ledger["target_start_head"]
        new_base = branch_head(repo, ledger["target_branch"])
        if new_base == old_base:
            return status(repo, run_id)
        worktree = Path(ledger["candidate_worktree"])
        if dirty_paths(worktree):
            raise AssuranceError(
                "candidate worktree must be clean before target rematerialization",
                code="CANDIDATE_WORKTREE_DIRTY",
                status="NEEDS_USER",
            )
        old_candidate = ledger["facets"]["execution"].get("candidate_head")
        tester_source = ledger["facets"]["execution"].get("tester_source")
        if isinstance(tester_source, dict) and dirty_paths(Path(tester_source["worktree"])):
            raise AssuranceError(
                "Tester source must be clean before target rematerialization",
                code="TESTER_WORKTREE_DIRTY",
                status="NEEDS_USER",
            )
        rebased = git(
            worktree,
            "rebase",
            "--onto",
            new_base,
            old_base,
            ledger["candidate_branch"],
            check=False,
        )
        if rebased.returncode != 0:
            git(worktree, "rebase", "--abort", check=False)
            raise AssuranceError(
                "candidate could not be rematerialized on the moved target",
                code="TARGET_REBASE_CONFLICT",
                status="NEEDS_USER",
                details={"stderr": rebased.stderr[-8000:]},
            )
        candidate = git(worktree, "rev-parse", "HEAD").stdout.strip()
        files = changed_files(repo, new_base, candidate)
        try:
            _assert_authorized_files(ledger["facets"], files)
            declared = set(ledger["facets"]["execution"]["builder_files"]) | set(
                ledger["facets"]["execution"]["tester_files"]
            )
            undeclared = sorted(set(files) - declared)
            if undeclared:
                raise AssuranceError(
                    "rematerialized candidate contains undeclared files",
                    code="EXECUTION_FILES_INCOMPLETE",
                    details={"paths": undeclared},
                )
        except Exception:
            if isinstance(old_candidate, str):
                git(worktree, "reset", "--hard", old_candidate, check=False)
            raise
        tester_old_base = old_base
        tester_new_base = new_base
        publication = ledger.get("publication")
        if isinstance(publication, dict) and publication.get("head"):
            old_publication_head = publication["head"]
            try:
                new_publication_head, new_publication_tree, publication_files = _materialize_publication(
                    repo,
                    run_id,
                    base_head=new_base,
                    source_head=candidate,
                    paths=list(publication["paths"]),
                )
            except Exception:
                if isinstance(old_candidate, str):
                    git(worktree, "reset", "--hard", old_candidate, check=False)
                raise
            publication.update(
                head=new_publication_head,
                tree=new_publication_tree,
                files=publication_files,
                manifest_digest=digest(publication_files),
                candidate_head=candidate,
            )
            tester_old_base = old_publication_head
            tester_new_base = new_publication_head
        old_tester_head = tester_source.get("head") if isinstance(tester_source, dict) else None
        if isinstance(tester_source, dict):
            tester_rebase = git(
                Path(tester_source["worktree"]),
                "rebase",
                "--onto",
                tester_new_base,
                tester_old_base,
                tester_source["branch"],
                check=False,
            )
            if tester_rebase.returncode != 0:
                git(Path(tester_source["worktree"]), "rebase", "--abort", check=False)
                if isinstance(old_candidate, str):
                    git(worktree, "reset", "--hard", old_candidate, check=False)
                raise AssuranceError(
                    "Tester source could not be rematerialized on the moved target",
                    code="TESTER_TARGET_REBASE_CONFLICT",
                    status="NEEDS_USER",
                    details={"stderr": tester_rebase.stderr[-8000:]},
                )
            tester_head = git(Path(tester_source["worktree"]), "rev-parse", "HEAD").stdout.strip()
            tester_source["head"] = tester_head
            tester_source["base_head"] = tester_new_base
            refreshed: list[dict[str, str]] = []
            for item in tester_source["files"]:
                blob = _blob_at(repo, tester_head, item["path"])
                if blob is None:
                    if isinstance(old_candidate, str):
                        git(worktree, "reset", "--hard", old_candidate, check=False)
                    if isinstance(old_tester_head, str):
                        git(Path(tester_source["worktree"]), "reset", "--hard", old_tester_head, check=False)
                    raise AssuranceError(
                        "rematerialized Tester source lost a frozen file",
                        code="TESTER_SOURCE_BLOB_MISSING",
                        details={"path": item["path"]},
                    )
                refreshed.append({"path": item["path"], "blob": blob})
            tester_source["files"] = refreshed
        execution = ledger["facets"]["execution"]
        execution["version"] += 1
        execution["candidate_head"] = candidate
        ledger["target_start_head"] = new_base
        ledger["digests"] = facet_digests(ledger["facets"])
        if isinstance(tester_source, dict):
            _close_problems(ledger, "tester", f"tester-source:{tester_source['head']}")
        append_event(
            ledger,
            "target_rematerialized",
            {
                "old_target_head": old_base,
                "new_target_head": new_base,
                "candidate_head": candidate,
                "publication_head": publication.get("head") if isinstance(publication, dict) else None,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right.rstrip("/") + "/") or right.startswith(left.rstrip("/") + "/")


def _assert_owned_source_stable(
    repo: Path,
    *,
    role: str,
    worktree: str,
    branch: str,
    expected_head: str,
) -> None:
    path = Path(worktree)
    if not path.exists():
        raise AssuranceError(
            f"{role} worktree is missing and was preserved for diagnosis",
            code=f"{role.upper()}_WORKTREE_MISSING",
            status="NEEDS_USER",
        )
    live_worktree = git(path, "rev-parse", "HEAD", check=False)
    live_branch = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    residue = dirty_paths(path) if live_worktree.returncode == 0 else ["<unreadable>"]
    if (
        live_worktree.returncode != 0
        or live_worktree.stdout.strip() != expected_head
        or live_branch.returncode != 0
        or live_branch.stdout.strip() != expected_head
        or residue
    ):
        raise AssuranceError(
            f"{role} source drifted and was preserved",
            code=f"{role.upper()}_SOURCE_DRIFT",
            status="NEEDS_USER",
            details={
                "worktree": str(path),
                "branch": branch,
                "expected_head": expected_head,
                "worktree_head": live_worktree.stdout.strip() if live_worktree.returncode == 0 else None,
                "branch_head": live_branch.stdout.strip() if live_branch.returncode == 0 else None,
                "residue": residue,
            },
        )


def finalize(repo_value: str | Path, run_value: str, message: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] == "finalized":
            return status(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        deployment = ledger["facets"]["execution"].get("deployment")
        if isinstance(deployment, dict):
            transaction = ledger.get("deployment_transaction")
            if (
                not isinstance(transaction, dict)
                or transaction.get("state") != "restored"
                or ledger.get("pending_blackbox") is not None
            ):
                raise AssuranceError(
                    "deployment must be restored and blackbox evidence completed before finalize",
                    code="DEPLOYMENT_FINALIZE_BLOCKED",
                    status="NEEDS_USER",
                )
        ready = readiness(ledger)
        if not ready["ready"]:
            raise AssuranceError(
                "assurance gates are not ready",
                code="ASSURANCE_GATES_INCOMPLETE",
                status="NEEDS_USER",
                details=ready,
            )
        candidate = ledger["facets"]["execution"]["candidate_head"]
        assert isinstance(candidate, str)
        _assert_owned_source_stable(
            repo,
            role="candidate",
            worktree=ledger["candidate_worktree"],
            branch=ledger["candidate_branch"],
            expected_head=candidate,
        )
        target_head = branch_head(repo, ledger["target_branch"])
        if target_head != ledger["target_start_head"]:
            raise AssuranceError(
                "target branch moved after the run started",
                code="TARGET_DRIFT",
                status="NEEDS_USER",
                details={"expected": ledger["target_start_head"], "actual": target_head},
            )
        files = changed_files(repo, target_head, candidate)
        _assert_authorized_files(ledger["facets"], files)
        tester_source = ledger["facets"]["execution"].get("tester_source")
        if isinstance(tester_source, dict):
            _assert_owned_source_stable(
                repo,
                role="tester",
                worktree=tester_source["worktree"],
                branch=tester_source["branch"],
                expected_head=tester_source["head"],
            )
        checkout = target_worktree(repo, ledger["target_branch"])
        if checkout is not None:
            dirty = dirty_paths(checkout)
            collisions = sorted(
                path
                for path in dirty
                if any(_paths_overlap(path, changed) for changed in files)
            )
            if collisions:
                raise AssuranceError(
                    "target dirty content overlaps the final candidate",
                    code="TARGET_DIRTY_COLLISION",
                    status="NEEDS_USER",
                    details={"paths": collisions},
                )
        tree = git(repo, "rev-parse", f"{candidate}^{{tree}}").stdout.strip()
        committed = git(
            repo,
            "-c",
            "commit.gpgSign=false",
            "commit-tree",
            tree,
            "-p",
            target_head,
            "-m",
            message,
            check=False,
        )
        if committed.returncode != 0:
            raise AssuranceError(
                committed.stderr.strip() or "final commit staging failed",
                code="FINAL_COMMIT_CREATE_FAILED",
            )
        final_head = committed.stdout.strip()
        ledger["phase"] = "finalizing"
        ledger["finalize_intent"] = {
            "expected_target_head": target_head,
            "candidate_head": candidate,
            "final_head": final_head,
            "changed_files": files,
        }
        append_event(ledger, "finalize_intent_created", copy.deepcopy(ledger["finalize_intent"]))
        save_ledger(repo, ledger)
        updated = git(
            repo,
            "update-ref",
            f"refs/heads/{ledger['target_branch']}",
            final_head,
            target_head,
            check=False,
        )
        if updated.returncode != 0:
            ledger["phase"] = "active"
            append_event(
                ledger,
                "finalize_cas_failed",
                {"stderr": updated.stderr[-8000:]},
            )
            ledger["finalize_intent"] = None
            save_ledger(repo, ledger)
            raise AssuranceError(
                "target changed during final compare-and-swap",
                code="FINALIZE_CAS_FAILED",
                status="NEEDS_USER",
            )
        if checkout is not None:
            synchronized = git(checkout, "read-tree", "-u", "-m", target_head, final_head, check=False)
            if synchronized.returncode != 0:
                rollback = git(
                    repo,
                    "update-ref",
                    f"refs/heads/{ledger['target_branch']}",
                    target_head,
                    final_head,
                    check=False,
                )
                ledger["phase"] = "active" if rollback.returncode == 0 else "finalizing"
                append_event(
                    ledger,
                    "finalize_worktree_sync_failed",
                    {
                        "stderr": synchronized.stderr[-8000:],
                        "ref_rolled_back": rollback.returncode == 0,
                    },
                )
                if rollback.returncode == 0:
                    ledger["finalize_intent"] = None
                save_ledger(repo, ledger)
                raise AssuranceError(
                    "target worktree could not be synchronized",
                    code="FINALIZE_WORKTREE_SYNC_FAILED",
                    status="NEEDS_USER",
                    details={"ref_rolled_back": rollback.returncode == 0},
                )
        _assert_owned_source_stable(
            repo,
            role="candidate",
            worktree=ledger["candidate_worktree"],
            branch=ledger["candidate_branch"],
            expected_head=candidate,
        )
        if isinstance(tester_source, dict):
            _assert_owned_source_stable(
                repo,
                role="tester",
                worktree=tester_source["worktree"],
                branch=tester_source["branch"],
                expected_head=tester_source["head"],
            )
        ledger["phase"] = "finalized"
        ledger["final_head"] = final_head
        append_event(ledger, "run_finalized", {"final_head": final_head})
        save_ledger(repo, ledger)
        worktree = Path(ledger["candidate_worktree"])
        git(repo, "worktree", "remove", str(worktree), check=False)
        git(repo, "branch", "-D", ledger["candidate_branch"], check=False)
        if isinstance(tester_source, dict):
            git(repo, "worktree", "remove", tester_source["worktree"], check=False)
            git(repo, "branch", "-D", tester_source["branch"], check=False)
        deployment_transaction = ledger.get("deployment_transaction")
        if isinstance(deployment_transaction, dict):
            git(repo, "worktree", "remove", "--force", deployment_transaction["worktree"], check=False)
            shutil.rmtree(deployment_transaction["worktree"], ignore_errors=True)
    return status(repo, run_id)


def recover_finalize(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] == "finalized":
            return status(repo, run_id)
        if ledger["phase"] != "finalizing" or not isinstance(ledger.get("finalize_intent"), dict):
            raise AssuranceError("run has no recoverable finalize intent", code="FINALIZE_INTENT_NOT_FOUND")
        intent = ledger["finalize_intent"]
        expected = intent["expected_target_head"]
        final_head = intent["final_head"]
        candidate = intent["candidate_head"]
        _assert_owned_source_stable(
            repo,
            role="candidate",
            worktree=ledger["candidate_worktree"],
            branch=ledger["candidate_branch"],
            expected_head=candidate,
        )
        tester_source = ledger["facets"]["execution"].get("tester_source")
        if isinstance(tester_source, dict):
            _assert_owned_source_stable(
                repo,
                role="tester",
                worktree=tester_source["worktree"],
                branch=tester_source["branch"],
                expected_head=tester_source["head"],
            )
        live = branch_head(repo, ledger["target_branch"])
        if live == expected:
            updated = git(
                repo,
                "update-ref",
                f"refs/heads/{ledger['target_branch']}",
                final_head,
                expected,
                check=False,
            )
            if updated.returncode != 0:
                raise AssuranceError(
                    "finalize intent compare-and-swap could not be recovered",
                    code="FINALIZE_RECOVERY_CAS_FAILED",
                    status="NEEDS_USER",
                )
        elif live != final_head:
            raise AssuranceError(
                "target diverged from the persisted finalize intent",
                code="FINALIZE_INTENT_TARGET_DIVERGED",
                status="NEEDS_USER",
                details={"expected": expected, "final_head": final_head, "actual": live},
            )
        checkout = target_worktree(repo, ledger["target_branch"])
        if checkout is not None:
            changed = list(intent.get("changed_files", []))
            baseline = expected
            if live == final_head:
                index_tree = git(checkout, "write-tree", check=False)
                expected_tree = git(repo, "rev-parse", f"{expected}^{{tree}}").stdout.strip()
                final_tree = git(repo, "rev-parse", f"{final_head}^{{tree}}").stdout.strip()
                if index_tree.returncode != 0:
                    raise AssuranceError(
                        "target index cannot prove finalize recovery state",
                        code="FINALIZE_RECOVERY_INDEX_INVALID",
                        status="NEEDS_USER",
                    )
                if index_tree.stdout.strip() == final_tree:
                    baseline = final_head
                elif index_tree.stdout.strip() != expected_tree:
                    raise AssuranceError(
                        "target index matches neither side of the finalize intent",
                        code="FINALIZE_RECOVERY_INDEX_DIVERGED",
                        status="NEEDS_USER",
                    )
            baseline_dirty = dirty_paths_against(checkout, baseline)
            collisions = sorted(
                path
                for path in baseline_dirty
                if any(_paths_overlap(path, candidate_path) for candidate_path in changed)
            )
            if collisions:
                raise AssuranceError(
                    "target dirty content overlaps the persisted finalize intent",
                    code="TARGET_DIRTY_COLLISION",
                    status="NEEDS_USER",
                    details={"paths": collisions},
                )
            synchronized = git(checkout, "read-tree", "-u", "-m", expected, final_head, check=False)
            if synchronized.returncode != 0:
                raise AssuranceError(
                    "persisted finalize intent could not synchronize the target worktree",
                    code="FINALIZE_RECOVERY_SYNC_FAILED",
                    status="NEEDS_USER",
                    details={"stderr": synchronized.stderr[-8000:]},
                )
        _assert_owned_source_stable(
            repo,
            role="candidate",
            worktree=ledger["candidate_worktree"],
            branch=ledger["candidate_branch"],
            expected_head=candidate,
        )
        if isinstance(tester_source, dict):
            _assert_owned_source_stable(
                repo,
                role="tester",
                worktree=tester_source["worktree"],
                branch=tester_source["branch"],
                expected_head=tester_source["head"],
            )
        ledger["phase"] = "finalized"
        ledger["final_head"] = final_head
        append_event(ledger, "finalize_intent_recovered", {"final_head": final_head})
        save_ledger(repo, ledger)
        worktree = Path(ledger["candidate_worktree"])
        git(repo, "worktree", "remove", str(worktree), check=False)
        git(repo, "branch", "-D", ledger["candidate_branch"], check=False)
        if isinstance(tester_source, dict):
            git(repo, "worktree", "remove", tester_source["worktree"], check=False)
            git(repo, "branch", "-D", tester_source["branch"], check=False)
    return status(repo, run_id)


def abandon(repo_value: str | Path, run_value: str, reason: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] == "finalized":
            raise AssuranceError("finalized run cannot be abandoned", code="ASSURANCE_RUN_FINALIZED")
        if ledger["phase"] != "abandoned":
            ledger["phase"] = "abandoned"
            append_event(ledger, "run_abandoned", {"reason": reason})
            save_ledger(repo, ledger)
    return status(repo, run_id)


def cleanup(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] not in {"finalized", "abandoned"}:
            raise AssuranceError(
                "only terminal assurance runs can be cleaned",
                code="ASSURANCE_CLEANUP_NOT_TERMINAL",
                status="NEEDS_USER",
            )
        owned = [
            {
                "role": "candidate",
                "worktree": ledger["candidate_worktree"],
                "branch": ledger["candidate_branch"],
                "expected_head": ledger["facets"]["execution"].get("candidate_head"),
            }
        ]
        tester_source = ledger["facets"]["execution"].get("tester_source")
        if isinstance(tester_source, dict):
            owned.append(
                {
                    "role": "tester",
                    "worktree": tester_source["worktree"],
                    "branch": tester_source["branch"],
                    "expected_head": tester_source["head"],
                }
            )
        for index, source in enumerate(ledger.get("retired_tester_sources", []), start=1):
            owned.append(
                {
                    "role": f"retired_tester_{index}",
                    "worktree": source["worktree"],
                    "branch": source["branch"],
                    "expected_head": source["head"],
                }
            )
        blockers: list[dict[str, Any]] = []
        for item in owned:
            path = Path(item["worktree"])
            if path.exists():
                live = git(path, "rev-parse", "HEAD", check=False)
                residue = dirty_paths(path) if live.returncode == 0 else ["<unreadable>"]
                if live.returncode != 0 or live.stdout.strip() != item["expected_head"] or residue:
                    blockers.append(
                        {
                            "role": item["role"],
                            "worktree": str(path),
                            "expected_head": item["expected_head"],
                            "actual_head": live.stdout.strip() if live.returncode == 0 else None,
                            "residue": residue,
                        }
                    )
            branch = git(repo, "rev-parse", "--verify", f"refs/heads/{item['branch']}", check=False)
            if branch.returncode == 0 and branch.stdout.strip() != item["expected_head"]:
                blockers.append(
                    {
                        "role": item["role"],
                        "branch": item["branch"],
                        "expected_head": item["expected_head"],
                        "actual_head": branch.stdout.strip(),
                    }
                )
        if blockers:
            raise AssuranceError(
                "terminal assurance worktrees drifted and were preserved",
                code="ASSURANCE_CLEANUP_DRIFT",
                status="NEEDS_USER",
                details={"blockers": blockers},
            )
        cleanup_failures: list[dict[str, Any]] = []
        for item in owned:
            path = Path(item["worktree"])
            if path.exists():
                removed = git(repo, "worktree", "remove", str(path), check=False)
                if removed.returncode != 0:
                    cleanup_failures.append(
                        {
                            "role": item["role"],
                            "worktree": str(path),
                            "stderr": removed.stderr[-8000:],
                        }
                    )
                    continue
            branch = git(repo, "rev-parse", "--verify", f"refs/heads/{item['branch']}", check=False)
            if branch.returncode == 0:
                removed_branch = git(repo, "branch", "-D", item["branch"], check=False)
                if removed_branch.returncode != 0:
                    cleanup_failures.append(
                        {
                            "role": item["role"],
                            "branch": item["branch"],
                            "stderr": removed_branch.stderr[-8000:],
                        }
                    )
                    continue
            if item["role"].startswith("retired_tester_"):
                ledger["retired_tester_sources"] = [
                    source
                    for source in ledger["retired_tester_sources"]
                    if source["branch"] != item["branch"]
                ]
                append_event(ledger, "retired_tester_source_cleaned", {"branch": item["branch"]})
                save_ledger(repo, ledger)
        if cleanup_failures:
            raise AssuranceError(
                "terminal assurance cleanup is incomplete and remains recoverable",
                code="ASSURANCE_CLEANUP_INCOMPLETE",
                status="NEEDS_USER",
                details={"failures": cleanup_failures},
            )
        append_event(ledger, "terminal_worktrees_cleaned", {})
        save_ledger(repo, ledger)
    return status(repo, run_id)
