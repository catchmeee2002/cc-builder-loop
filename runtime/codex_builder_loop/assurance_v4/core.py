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
    validate_lineage,
    validate_problem_report,
    validate_repo_path,
    validate_retrospective_report,
    validate_retrospective_snapshot,
    validate_stored_retrospective_report,
    validate_telemetry,
    validate_test_proof_spec,
    validate_ledger,
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
    state_root,
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


def _candidate_manifest(repo: Path, base: str, candidate: str) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    for path in changed_files(repo, base, candidate):
        blob = _blob_at(repo, candidate, path)
        if blob is not None:
            manifest.append({"path": path, "blob": blob})
    return manifest


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
            requested_supersedes = contract["mission"].get("supersedes")
            existing_carryover = existing["facets"]["execution"].get("carryover")
            if (
                isinstance(requested_supersedes, dict)
                and isinstance(existing_carryover, dict)
                and existing["owner_session_id"] == session_id.strip()
                and existing["facets"]["mission"] == contract["mission"]
                and existing_carryover.get("source_run_id") == requested_supersedes["run_id"]
            ):
                return status(repo, run_id)
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
        supersedes = contract["mission"].get("supersedes")
        if contract["mission"]["revision"] > 1 and not isinstance(supersedes, dict):
            raise AssuranceError(
                "a new higher-revision run requires mission supersedes",
                code="MISSION_SUPERSEDES_REQUIRED",
                status="NEEDS_USER",
            )
        source_ledger: dict[str, Any] | None = None
        inherited_problems: list[dict[str, Any]] = []
        problem_dispositions: list[dict[str, Any]] = []
        revision_transitions: list[dict[str, Any]] = []
        candidate_base = target_head
        if isinstance(supersedes, dict):
            source_ledger = read_ledger(repo, supersedes["run_id"])
            source_mission = source_ledger["facets"]["mission"]
            source_candidate = source_ledger["facets"]["execution"].get("candidate_head")
            if source_ledger["phase"] != "active":
                raise AssuranceError(
                    "superseded run must remain active until continuity transfers",
                    code="SUPERSEDED_RUN_NOT_ACTIVE",
                    status="NEEDS_USER",
                )
            if (
                supersedes["revision"] != source_mission["revision"]
                or supersedes["mission_digest"] != source_ledger["digests"]["mission"]
                or supersedes["candidate_head"] != source_candidate
            ):
                raise AssuranceError(
                    "mission supersedes binding does not match the source run",
                    code="MISSION_SUPERSEDES_MISMATCH",
                )
            if contract["mission"]["revision"] != source_mission["revision"] + 1:
                raise AssuranceError(
                    "superseding mission must increment revision by one",
                    code="MISSION_REVISION_INVALID",
                )
            if source_ledger["target_branch"] != target_branch:
                raise AssuranceError(
                    "superseding run cannot change target branch",
                    code="SUPERSEDE_TARGET_MISMATCH",
                    status="NEEDS_USER",
                )
            if target_head != source_ledger["target_start_head"]:
                raise AssuranceError(
                    "target moved before supersede continuity could be captured",
                    code="SUPERSEDE_TARGET_DRIFT",
                    status="NEEDS_USER",
                )
            if not isinstance(source_candidate, str) or not commit_exists(repo, source_candidate):
                raise AssuranceError(
                    "superseded candidate is unavailable",
                    code="SUPERSEDE_CANDIDATE_MISSING",
                    status="NEEDS_USER",
                )
            source_worktree = Path(source_ledger["candidate_worktree"])
            if dirty_paths(source_worktree) or branch_head(repo, source_ledger["candidate_branch"]) != source_candidate:
                raise AssuranceError(
                    "superseded candidate is not a clean immutable snapshot",
                    code="SUPERSEDE_CANDIDATE_DRIFT",
                    status="NEEDS_USER",
                )
            source_deployment = source_ledger["facets"]["execution"].get("deployment")
            if digest(source_deployment) != digest(contract["execution"].get("deployment")):
                raise AssuranceError(
                    "superseding run changed the deployment contract",
                    code="SUPERSEDE_DEPLOYMENT_MISMATCH",
                    status="NEEDS_USER",
                )
            transition = contract["execution"].get("revision_transition")
            prior_problems = contract["execution"].get("prior_problem_dispositions")
            snapshot_digest, source_problems = _open_problem_snapshot(source_ledger)
            legacy_transition = (
                source_mission["revision"] == 1
                and transition is None
                and prior_problems is None
                and not source_problems
            )
            if not legacy_transition and (
                not isinstance(transition, dict) or not isinstance(prior_problems, dict)
            ):
                raise AssuranceError(
                    "supersession requires revision transition and prior-problem dispositions",
                    code="REVISION_CONTINUITY_REQUIRED",
                    status="NEEDS_USER",
                )
            source_semantics = {
                key: source_mission.get(key)
                for key in (
                    "delivery_kind", "objective", "behaviors", "interfaces",
                    "acceptance_cases", "trust_boundaries",
                )
            }
            target_semantics = {
                key: contract["mission"].get(key)
                for key in (
                    "delivery_kind", "objective", "behaviors", "interfaces",
                    "acceptance_cases", "trust_boundaries",
                )
            }
            semantic_changed = source_semantics != target_semantics
            if not legacy_transition:
                assert isinstance(transition, dict) and isinstance(prior_problems, dict)
                source_lineage = _derive_lineage(repo, source_ledger)
                _validate_revision_transition(source_lineage, transition)
                if (transition["category"] == "mission_change") != semantic_changed:
                    raise AssuranceError(
                        "revision transition category does not match the mission semantic delta",
                        code="REVISION_TRANSITION_SEMANTICS_MISMATCH",
                        status="NEEDS_USER",
                    )
                if (
                    prior_problems.get("source_snapshot_digest") != snapshot_digest
                ):
                    raise AssuranceError(
                        "prior-problem snapshot does not match the source run",
                        code="PRIOR_PROBLEM_SNAPSHOT_MISMATCH",
                        status="NEEDS_USER",
                    )
                source_by_key = {item["key"]: item for item in source_problems}
                dispositions = prior_problems.get("items", [])
                disposition_by_key = {item["key"]: item for item in dispositions}
                if set(disposition_by_key) != set(source_by_key):
                    raise AssuranceError(
                        "every open source problem requires exactly one disposition",
                        code="PRIOR_PROBLEM_DISPOSITIONS_INCOMPLETE",
                        status="NEEDS_USER",
                        details={
                            "missing": sorted(set(source_by_key) - set(disposition_by_key)),
                            "unexpected": sorted(set(disposition_by_key) - set(source_by_key)),
                        },
                    )
                for key in sorted(disposition_by_key):
                    decision = disposition_by_key[key]
                    problem_dispositions.append(
                        {
                            "source_run_id": source_ledger["run_id"],
                            "target_run_id": run_id,
                            **copy.deepcopy(decision),
                        }
                    )
                    if decision["disposition"] == "included":
                        inherited_problems.append(copy.deepcopy(source_by_key[key]))
                revision_transitions.append(
                    _recorded_transition(
                        transition,
                        source_run=source_ledger["run_id"],
                        target_run=run_id,
                        from_revision=source_mission["revision"],
                        to_revision=contract["mission"]["revision"],
                    )
                )
            expected_supersede_intent = {
                "source_run_id": supersedes["run_id"],
                "target_run_id": run_id,
                "state": "prepared",
            }
            if source_ledger.get("supersede_intent") not in (None, expected_supersede_intent):
                raise AssuranceError(
                    "source run already has another supersede intent",
                    code="SUPERSEDE_INTENT_CONFLICT",
                    status="NEEDS_USER",
                )
            candidate_base = source_candidate
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
        intake_sources = [] if source_ledger is not None else _validate_dirty_intake(repo, contract)
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
        if source_ledger is not None:
            source_ledger["supersede_intent"] = {
                "source_run_id": source_ledger["run_id"],
                "target_run_id": run_id,
                "state": "prepared",
            }
            append_event(source_ledger, "supersede_intent_written", {"target_run_id": run_id})
            save_ledger(repo, source_ledger)
        created = git(repo, "worktree", "add", "-b", branch, str(worktree), candidate_base, check=False)
        if created.returncode != 0:
            if source_ledger is not None:
                source_ledger["supersede_intent"] = None
                append_event(source_ledger, "supersede_intent_rolled_back", {"target_run_id": run_id})
                save_ledger(repo, source_ledger)
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
            snapshots = (
                copy.deepcopy(source_ledger["facets"]["execution"]["dirty_snapshot"])
                if source_ledger is not None
                else _copy_dirty_intake(worktree, intake_sources)
            )
            captured = [item["path"] for item in snapshots]
            if snapshots and source_ledger is None:
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
            retired_tester_sources: list[dict[str, Any]] = []
            if source_ledger is not None:
                source_execution = source_ledger["facets"]["execution"]
                execution["builder_files"] = copy.deepcopy(source_execution["builder_files"])
                execution["tester_files"] = []
                execution["tester_source"] = None
                execution["carryover"] = {
                    "source_run_id": source_ledger["run_id"],
                    "source_candidate_head": candidate_head,
                    "files": _candidate_manifest(repo, target_head, candidate_head),
                }
                if isinstance(source_execution.get("tester_source"), dict):
                    retired_tester_sources.append(copy.deepcopy(source_execution["tester_source"]))
            if snapshots:
                execution["version"] += 1
                execution["builder_files"] = sorted(set(execution["builder_files"]) | set(captured))
            contract["execution"] = execution
            created_at = now()
            from ..core import capture_runtime_identity

            ledger = {
                "schema_version": SCHEMA_VERSION,
                "runtime_identity": capture_runtime_identity(),
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
                "retired_tester_sources": retired_tester_sources,
                "retired_reviewer_agents": [],
                "driver_runtime": copy.deepcopy(driver_runtime),
                "dispatch_intent": None,
                "deployment_transaction": None,
                "pending_blackbox": None,
                "environment_lease": None,
                "supersede_intent": None,
                "abandon_intent": None,
                "problems": [
                    {
                        **problem,
                        "status": "open",
                        "producer": None,
                        "candidate_head": candidate_head,
                        "recorded_at": created_at,
                    }
                    for problem in inherited_problems
                ],
                "revision_transitions": revision_transitions,
                "problem_dispositions": problem_dispositions,
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
            if source_ledger is not None:
                current_source = read_ledger(repo, source_ledger["run_id"])
                source_lease = current_source.get("environment_lease")
                if not isinstance(source_lease, dict) or source_lease.get("state") != "held":
                    current_source["phase"] = "superseded"
                    current_source["supersede_intent"] = {
                        "source_run_id": current_source["run_id"],
                        "target_run_id": run_id,
                        "state": "received",
                    }
                    append_event(current_source, "run_superseded", {"target_run_id": run_id})
                    save_ledger(repo, current_source)
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
            if source_ledger is not None:
                current_source = read_ledger(repo, source_ledger["run_id"])
                intent = current_source.get("supersede_intent")
                if isinstance(intent, dict) and intent.get("target_run_id") == run_id:
                    current_source["supersede_intent"] = None
                    append_event(current_source, "supersede_intent_rolled_back", {"target_run_id": run_id})
                    save_ledger(repo, current_source)
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


def consume_dispatch(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    consumer_source: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if intent.get("state") != "completed":
            raise AssuranceError("dispatch is not complete", code="DISPATCH_NOT_COMPLETE")
        if consumer_source is None:
            runtime = ledger.get("driver_runtime")
            kind = runtime.get("kind") if isinstance(runtime, dict) else None
            consumer_source = {
                "native": "native_driver",
                "full_driver_skill": "full_driver_skill",
            }.get(kind)
        if consumer_source not in {"native_driver", "full_driver_skill", "operator_recovery"}:
            raise AssuranceError(
                "dispatch consumer source is required",
                code="DISPATCH_CONSUMER_SOURCE_REQUIRED",
            )
        append_event(
            ledger,
            "dispatch_consumed",
            {
                "action_id": action_id,
                "result_digest": intent.get("result_digest"),
                "consumer_source": consumer_source,
            },
        )
        ledger["dispatch_intent"] = None
        save_ledger(repo, ledger)
    return status(repo, run_id)


def resolve_external_problem(
    repo_value: str | Path,
    run_value: str,
    *,
    problem_key: str,
    reason: str,
) -> dict[str, Any]:
    """Record one authorized recovery decision without manufacturing evidence."""

    key = problem_key.strip()
    normalized_reason = reason.strip()
    if not key:
        raise AssuranceError(
            "external problem key is required",
            code="EXTERNAL_PROBLEM_KEY_REQUIRED",
            status="FAIL",
        )
    if not normalized_reason:
        raise AssuranceError(
            "external problem resolution requires a non-empty reason",
            code="EXTERNAL_PROBLEM_REASON_REQUIRED",
            status="FAIL",
        )
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError(
                "external problems can only be resolved on an active run",
                code="EXTERNAL_PROBLEM_RUN_TERMINAL",
                status="FAIL",
                details={"phase": ledger["phase"]},
            )
        keyed = [item for item in ledger.get("problems", []) if item.get("key") == key]
        if not keyed:
            raise AssuranceError(
                "external problem key was not found",
                code="EXTERNAL_PROBLEM_NOT_FOUND",
                status="FAIL",
                details={"key": key},
            )
        if len(keyed) != 1:
            raise AssuranceError(
                "external problem key is ambiguous",
                code="EXTERNAL_PROBLEM_AMBIGUOUS",
                status="FAIL",
                details={"key": key, "matches": len(keyed)},
            )
        problem = keyed[0]
        if problem.get("owner") != "external_platform":
            raise AssuranceError(
                "only external-platform problems use this recovery transition",
                code="EXTERNAL_PROBLEM_OWNER_INVALID",
                status="FAIL",
                details={"key": key, "owner": problem.get("owner")},
            )
        current_candidate = ledger["facets"]["execution"].get("candidate_head")
        if problem.get("candidate_head") != current_candidate:
            raise AssuranceError(
                "external problem no longer binds the current candidate",
                code="EXTERNAL_PROBLEM_STALE",
                status="FAIL",
                details={
                    "key": key,
                    "problem_candidate_head": problem.get("candidate_head"),
                    "candidate_head": current_candidate,
                },
            )
        resolution_events = [
            event
            for event in ledger.get("events", [])
            if event.get("kind") == "external_problem_resolved"
            and isinstance(event.get("details"), dict)
            and event["details"].get("key") == key
        ]
        if len(resolution_events) > 1:
            raise AssuranceError(
                "external problem has duplicate resolution events",
                code="EXTERNAL_PROBLEM_RESOLUTION_DUPLICATE",
                status="FAIL",
                details={"key": key, "events": len(resolution_events)},
            )
        if problem.get("status") == "resolved":
            event_details = (
                resolution_events[0].get("details", {}) if resolution_events else {}
            )
            if (
                problem.get("resolution") == normalized_reason
                and event_details.get("reason") == normalized_reason
                and event_details.get("candidate_head") == current_candidate
            ):
                return status(repo, run_id)
            raise AssuranceError(
                "resolved external problem conflicts with this replay",
                code="EXTERNAL_PROBLEM_RESOLUTION_CONFLICT",
                status="FAIL",
                details={"key": key},
            )
        if problem.get("status") != "open" or resolution_events:
            raise AssuranceError(
                "external problem resolution state is inconsistent",
                code="EXTERNAL_PROBLEM_RESOLUTION_CONFLICT",
                status="FAIL",
                details={"key": key},
            )
        if isinstance(ledger.get("dispatch_intent"), dict):
            raise AssuranceError(
                "external problem cannot be resolved while a dispatch is pending",
                code="EXTERNAL_PROBLEM_DISPATCH_PENDING",
                status="FAIL",
                details={"key": key},
            )
        machine_record = ledger.get("evidence", {}).get("machine")
        machine_record_digest = (
            digest(machine_record) if isinstance(machine_record, dict) else None
        )
        problem["status"] = "resolved"
        problem["resolution"] = normalized_reason
        problem["resolved_at"] = now()
        append_event(
            ledger,
            "external_problem_resolved",
            {
                "key": key,
                "reason": normalized_reason,
                "candidate_head": current_candidate,
                "machine_state": evidence_state(ledger, "machine"),
                "machine_evidence_digest": machine_record_digest,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


RETROSPECTIVE_TERMINAL_PHASES = {"finalized", "abandoned", "superseded"}


def _retrospective_report_path(repo: Path, session_id: str) -> Path:
    key = digest({"owner_session_id": session_id})
    return state_root(repo) / "retrospectives" / f"{key}.json"


def _retrospective_signal(
    kind: str,
    severity: str,
    run_ids: list[str],
    summary: str,
    facts: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_runs = sorted(set(run_ids))
    identity = digest(
        {
            "kind": kind,
            "run_ids": normalized_runs,
            "facts": facts,
        }
    )[:16]
    return {
        "signal_id": f"{kind}-{identity}",
        "kind": kind,
        "severity": severity,
        "run_ids": normalized_runs,
        "summary": summary,
        "facts": copy.deepcopy(dict(facts)),
    }


def _retrospective_root_run_id(ledger: Mapping[str, Any]) -> str:
    supersedes = ledger.get("facets", {}).get("mission", {}).get("supersedes")
    if isinstance(supersedes, dict) and isinstance(supersedes.get("run_id"), str):
        return str(supersedes["run_id"])
    return str(ledger["run_id"])


def _matching_retrospective_ledgers(
    repo: Path, session_id: str
) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, Any]]]:
    matched: list[tuple[dict[str, Any], str]] = []
    malformed: list[dict[str, Any]] = []
    runs = state_root(repo) / "runs"
    if not runs.is_dir():
        return matched, malformed
    for path in sorted(runs.glob("*/ledger.json")):
        try:
            raw_bytes = path.read_bytes()
            raw = json.loads(raw_bytes)
        except OSError as exc:
            malformed.append(
                {
                    "run_id": path.parent.name,
                    "code": "ASSURANCE_LEDGER_UNREADABLE",
                    "message": str(exc),
                    "path": None,
                }
            )
            continue
        except json.JSONDecodeError as exc:
            malformed.append(
                {
                    "run_id": path.parent.name,
                    "code": "ASSURANCE_LEDGER_INVALID",
                    "message": str(exc),
                    "path": None,
                }
            )
            continue
        if not isinstance(raw, dict):
            malformed.append(
                {
                    "run_id": path.parent.name,
                    "code": "ASSURANCE_LEDGER_INVALID",
                    "message": "ledger must be an object",
                    "path": None,
                }
            )
            continue
        if raw.get("owner_session_id") != session_id:
            continue
        try:
            raw_repo = resolve_repo(str(raw.get("repo_root", "")))
            raw_run_id = ensure_run_id(str(raw.get("run_id", "")))
            expected_path = ledger_path(raw_repo, raw_run_id).resolve()
            same_repository = state_root(raw_repo) == state_root(repo)
        except (AssuranceError, OSError, RuntimeError, StoreError) as exc:
            malformed.append(
                {
                    "run_id": str(raw.get("run_id") or path.parent.name),
                    "code": getattr(exc, "code", "ASSURANCE_LEDGER_INVALID"),
                    "message": str(exc),
                    "path": None,
                }
            )
            continue
        if not same_repository or expected_path != path.resolve():
            malformed.append(
                {
                    "run_id": raw_run_id,
                    "code": "ASSURANCE_LEDGER_LOCATION_MISMATCH",
                    "message": "ledger is outside its recorded repository state root",
                    "path": None,
                }
            )
            continue
        try:
            ledger = validate_ledger(raw)
        except ContractError as exc:
            malformed.append(
                {
                    "run_id": str(raw.get("run_id") or path.parent.name),
                    "code": exc.code,
                    "message": str(exc),
                    "path": exc.details.get("path"),
                }
            )
            continue
        matched.append((ledger, hashlib.sha256(raw_bytes).hexdigest()))
    return matched, malformed


def _derive_retrospective_snapshot(
    repo: Path,
    session_id: str,
    ledgers: list[tuple[dict[str, Any], str]],
    terminal_facts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    run_facts: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    roots: dict[str, list[str]] = {}
    by_run = {str(item[0]["run_id"]): item[0] for item in ledgers}

    def root_for(ledger: Mapping[str, Any]) -> str:
        current = str(ledger["run_id"])
        seen: set[str] = set()
        while current in by_run and current not in seen:
            seen.add(current)
            parent = _retrospective_root_run_id(by_run[current])
            if parent == current or parent not in by_run:
                return current
            current = parent
        return current

    for ledger, ledger_digest in sorted(ledgers, key=lambda item: str(item[0]["run_id"])):
        run_id = str(ledger["run_id"])
        mission = ledger["facets"]["mission"]
        root_run_id = root_for(ledger)
        roots.setdefault(root_run_id, []).append(run_id)
        events = [item for item in ledger.get("events", []) if isinstance(item, dict)]
        runtime_identity = copy.deepcopy(ledger["runtime_identity"])
        problems = [item for item in ledger.get("problems", []) if isinstance(item, dict)]
        run_facts.append(
            {
                "run_id": run_id,
                "phase": str(ledger["phase"]),
                "terminal_status": str(terminal_facts[run_id]["terminal_status"]),
                "root_run_id": root_run_id,
                "mission_revision": int(mission["revision"]),
                "ledger_digest": ledger_digest,
                "runtime_identity": runtime_identity,
                "problem_count": len(problems),
                "event_count": len(events),
            }
        )
        terminal_fact = terminal_facts[run_id]
        if terminal_fact["terminal_status"] in {"needs-user", "fatal", "continuity-failure"}:
            signals.append(
                _retrospective_signal(
                    "terminal-runtime-failure",
                    "mandatory",
                    [run_id],
                    f"Run {run_id} ended with {terminal_fact['terminal_status']}",
                    terminal_fact,
                )
            )
        sorted_problems = sorted(
            problems,
            key=lambda item: (
                str(item.get("key", "")),
                str(item.get("recorded_at", "")),
                digest(item),
            ),
        )
        for occurrence, problem in enumerate(sorted_problems, start=1):
            facts = {
                "key": str(problem.get("key", "unknown")),
                "owner": str(problem.get("owner", "unknown")),
                "status": str(problem.get("status", "unknown")),
                "summary": str(problem.get("summary", "recorded problem")),
                "details": str(problem.get("details", "")),
                "candidate_head": problem.get("candidate_head"),
                "producer": copy.deepcopy(problem.get("producer")),
                "recorded_at": problem.get("recorded_at"),
                "occurrence": occurrence,
            }
            signals.append(
                _retrospective_signal(
                    "recorded-problem",
                    "mandatory",
                    [run_id],
                    f"Run {run_id} recorded problem {facts['key']}",
                    facts,
                )
            )

        prepared_counts: dict[str, int] = {}
        completed: set[str] = set()
        correction_counts: dict[str, int] = {}
        evidence_attempts: dict[str, int] = {}
        evidence_failures: dict[str, int] = {}
        retry_counts: dict[str, int] = {}
        manual_recoveries: list[dict[str, Any]] = []
        for event in events:
            kind = event.get("kind")
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            action_id = details.get("action_id")
            if kind == "dispatch_prepared" and isinstance(action_id, str):
                prepared_counts[action_id] = prepared_counts.get(action_id, 0) + 1
                action = str(details.get("action", ""))
                if action in {"tester_fix", "builder_fix"}:
                    correction_counts[action] = correction_counts.get(action, 0) + 1
            elif kind == "dispatch_completed" and isinstance(action_id, str):
                completed.add(action_id)
            elif kind == "dispatch_retry_scheduled" and isinstance(action_id, str):
                retry_counts[action_id] = retry_counts.get(action_id, 0) + 1
            elif kind == "dispatch_consumed" and isinstance(action_id, str):
                source = details.get("consumer_source")
                if source == "operator_recovery" or (source is None and action_id not in completed):
                    manual_recoveries.append(
                        {
                            "action_id": action_id,
                            "consumer_source": source or "legacy-inferred",
                        }
                    )
            elif kind == "evidence_recorded":
                evidence_kind = str(details.get("kind", "unknown"))
                evidence_attempts[evidence_kind] = evidence_attempts.get(evidence_kind, 0) + 1
                if details.get("status") == "fail":
                    evidence_failures[evidence_kind] = evidence_failures.get(evidence_kind, 0) + 1
            elif kind == "machine_verified":
                evidence_attempts["machine"] = evidence_attempts.get("machine", 0) + 1
                if details.get("status") != "pass":
                    evidence_failures["machine"] = evidence_failures.get("machine", 0) + 1

        for action, count in sorted(correction_counts.items()):
            if count > 1:
                signals.append(
                    _retrospective_signal(
                        "repeated-role-correction",
                        "mandatory",
                        [run_id],
                        f"Run {run_id} required {count} {action} dispatches",
                        {"action": action, "count": count},
                    )
                )
        for action_id in sorted(set(prepared_counts) | set(retry_counts)):
            attempts = prepared_counts.get(action_id, 0) + retry_counts.get(action_id, 0)
            if attempts > 1:
                signals.append(
                    _retrospective_signal(
                        "repeated-dispatch",
                        "mandatory",
                        [run_id],
                        f"Run {run_id} replayed dispatch {action_id}",
                        {"action_id": action_id, "attempts": attempts},
                    )
                )
        for recovery in manual_recoveries:
            signals.append(
                _retrospective_signal(
                    "manual-dispatch-recovery",
                    "mandatory",
                    [run_id],
                    f"Run {run_id} consumed a dispatch through manual recovery",
                    recovery,
                )
            )
        for evidence_kind, count in sorted(evidence_failures.items()):
            signals.append(
                _retrospective_signal(
                    "failed-evidence",
                    "mandatory",
                    [run_id],
                    f"Run {run_id} recorded failed {evidence_kind} evidence",
                    {"kind": evidence_kind, "failures": count},
                )
            )
        for evidence_kind, count in sorted(evidence_attempts.items()):
            if count > 1:
                signals.append(
                    _retrospective_signal(
                        "replayed-evidence",
                        "mandatory",
                        [run_id],
                        f"Run {run_id} recorded {count} {evidence_kind} evidence attempts",
                        {"kind": evidence_kind, "attempts": count},
                    )
                )
        revision = int(mission["revision"])
        if revision > 1:
            signals.append(
                _retrospective_signal(
                    "revision-pressure",
                    "mandatory" if revision >= 3 else "advisory",
                    [run_id],
                    f"Run {run_id} reached mission revision {revision}",
                    {"mission_revision": revision},
                )
            )
        if runtime_identity.get("capture_status") != "captured":
            signals.append(
                _retrospective_signal(
                    "runtime-identity-unavailable",
                    "mandatory",
                    [run_id],
                    f"Run {run_id} did not capture a complete runtime identity",
                    runtime_identity,
                )
            )

    if len(roots) > 1:
        root_ids = sorted(roots)
        signals.append(
            _retrospective_signal(
                "multiple-terminal-roots",
                "mandatory",
                [run_id for root in root_ids for run_id in sorted(roots[root])],
                f"Session contains {len(root_ids)} independent terminal root runs",
                {"root_run_ids": root_ids},
            )
        )
    signals.sort(key=lambda item: item["signal_id"])
    repository_identity = str(Path(str(ledgers[0][0]["repo_root"])).resolve())
    snapshot_base = {
        "schema_version": 1,
        "repo_root": repository_identity,
        "owner_session_id": session_id,
        "runs": run_facts,
        "signals": signals,
    }
    snapshot = {**snapshot_base, "snapshot_digest": digest(snapshot_base)}
    return validate_retrospective_snapshot(snapshot)


def _read_retrospective_report(repo: Path, session_id: str) -> dict[str, Any] | None:
    path = _retrospective_report_path(repo, session_id)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AssuranceError(
            str(exc), code="RETROSPECTIVE_REPORT_STORED_INVALID", status="FATAL"
        ) from exc
    try:
        report = validate_stored_retrospective_report(value)
    except ContractError as exc:
        raise AssuranceError(
            str(exc),
            code="RETROSPECTIVE_REPORT_STORED_INVALID",
            status="FATAL",
            details=exc.details,
        ) from exc
    report_base = {
        key: report[key]
        for key in (
            "schema_version",
            "repo_root",
            "owner_session_id",
            "snapshot_digest",
            "dispositions",
        )
    }
    if report["report_digest"] != digest(report_base):
        raise AssuranceError(
            "stored retrospective report digest does not match its content",
            code="RETROSPECTIVE_REPORT_DIGEST_MISMATCH",
            status="FATAL",
        )
    return report


def _render_retrospective_block(
    snapshot: Mapping[str, Any], report: Mapping[str, Any], *, pending: bool
) -> str:
    signal_by_id = {item["signal_id"]: item for item in snapshot["signals"]}
    heading = (
        "Builder-loop retrospective requires user input."
        if pending
        else "Builder-loop retrospective complete."
    )
    lines = [
        heading,
        f"Session: {snapshot['owner_session_id']}",
        f"Snapshot: {snapshot['snapshot_digest']}",
        f"Report: {report['report_digest']}",
        "Dispositions:",
    ]
    if not report["dispositions"]:
        lines.append("- No retrospective signals.")
    for item in report["dispositions"]:
        signal = signal_by_id[item["signal_id"]]
        if item["disposition"] == "issue":
            result = f"issue {item['owner']} {item['reference']}"
        else:
            result = f"{item['disposition']} {item['reason']}"
        lines.append(
            f"- {item['signal_id']} [{signal['severity']}] {signal['summary']} => {result}"
        )
    if pending:
        lines.append(
            f"BUILDER_INPUT_REQUIRED:{snapshot['owner_session_id']}:{snapshot['snapshot_digest']}"
        )
    else:
        lines.append(
            f"BUILDER_RETROSPECTIVE_READY:{snapshot['snapshot_digest']}:{report['report_digest']}"
        )
    return "\n".join(lines)


def retrospective_status(repo_value: str | Path, session_id: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    owner_session_id = session_id.strip()
    if not owner_session_id:
        raise AssuranceError("session id is required", code="SESSION_ID_REQUIRED")
    ledgers, malformed = _matching_retrospective_ledgers(repo, owner_session_id)
    if malformed:
        return {
            "status": "FATAL",
            "owner_session_id": owner_session_id,
            "message": "matching Assurance v4 ledgers are malformed",
            "code": "RETROSPECTIVE_LEDGER_MALFORMED",
            "malformed_ledgers": malformed,
        }
    if not ledgers:
        return {
            "status": "NOOP",
            "owner_session_id": owner_session_id,
            "message": "no matching Assurance v4 runs",
        }
    terminal_facts: dict[str, dict[str, Any]] = {}
    active: list[str] = []
    for ledger, _ledger_digest in ledgers:
        run_id = str(ledger["run_id"])
        phase = str(ledger["phase"])
        if phase in RETROSPECTIVE_TERMINAL_PHASES:
            terminal_facts[run_id] = {"terminal_status": phase}
            continue
        try:
            from .driver import next_action

            decision = next_action(repo, run_id)
        except (AssuranceError, ContractError, StoreError) as exc:
            terminal_facts[run_id] = {
                "terminal_status": "fatal",
                "code": getattr(exc, "code", "RETROSPECTIVE_DRIVER_STATUS_FAILED"),
                "reason": str(exc),
            }
            continue
        if decision.get("status") == "NEEDS_USER":
            reason = str(decision.get("reason") or "needs_user")
            action = str(decision.get("action") or "unknown")
            terminal_status = (
                "continuity-failure"
                if action == "continuity_decision" or "continuity" in reason
                else "needs-user"
            )
            terminal_facts[run_id] = {
                "terminal_status": terminal_status,
                "action": action,
                "reason": reason,
            }
        else:
            active.append(run_id)
    active.sort()
    if active:
        return {
            "status": "ACTIVE",
            "owner_session_id": owner_session_id,
            "message": "Assurance v4 run remains active",
            "active_runs": active,
            "run_id": active[0] if len(active) == 1 else None,
        }
    snapshot = _derive_retrospective_snapshot(
        repo, owner_session_id, ledgers, terminal_facts
    )
    report = _read_retrospective_report(repo, owner_session_id)
    if report is None:
        return {
            "status": "REQUIRED",
            "owner_session_id": owner_session_id,
            "message": "terminal Assurance v4 runs require a retrospective report",
            "snapshot": snapshot,
        }
    if (
        report["repo_root"] != snapshot["repo_root"]
        or report["owner_session_id"] != owner_session_id
        or report["snapshot_digest"] != snapshot["snapshot_digest"]
    ):
        return {
            "status": "STALE",
            "owner_session_id": owner_session_id,
            "message": "the stored retrospective report is stale",
            "snapshot": snapshot,
            "report": report,
        }
    pending = any(item["disposition"] == "needs-user" for item in report["dispositions"])
    required_block = _render_retrospective_block(snapshot, report, pending=pending)
    return {
        "status": "NEEDS_USER" if pending else "READY",
        "owner_session_id": owner_session_id,
        "message": (
            "retrospective awaits an authorized user decision"
            if pending
            else "retrospective report is complete"
        ),
        "snapshot": snapshot,
        "report": report,
        "required_block": required_block,
    }


def record_retrospective(
    repo_value: str | Path,
    session_id: str,
    report_value: Any,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    owner_session_id = session_id.strip()
    report_input = validate_retrospective_report(report_value)
    with locked(repo):
        current = retrospective_status(repo, owner_session_id)
        if current["status"] in {"NOOP", "ACTIVE", "FATAL"}:
            raise AssuranceError(
                "retrospective report cannot be recorded in the current session state",
                code="RETROSPECTIVE_NOT_RECORDABLE",
                status="NEEDS_USER" if current["status"] == "ACTIVE" else "FATAL",
                details={"retrospective_status": current["status"]},
            )
        snapshot = current["snapshot"]
        if report_input["snapshot_digest"] != snapshot["snapshot_digest"]:
            raise AssuranceError(
                "retrospective report does not bind the current snapshot",
                code="RETROSPECTIVE_SNAPSHOT_STALE",
                status="FAIL",
                details={
                    "expected_snapshot_digest": snapshot["snapshot_digest"],
                    "actual_snapshot_digest": report_input["snapshot_digest"],
                },
            )
        signals = {item["signal_id"]: item for item in snapshot["signals"]}
        dispositions = report_input["dispositions"]
        ids = [item["signal_id"] for item in dispositions]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        unknown = sorted(set(ids) - set(signals))
        missing = sorted(set(signals) - set(ids))
        if duplicates or unknown or missing:
            raise AssuranceError(
                "retrospective dispositions must cover every current signal exactly once",
                code="RETROSPECTIVE_COVERAGE_INVALID",
                status="FAIL",
                details={"duplicates": duplicates, "unknown": unknown, "missing": missing},
            )
        by_id = {item["signal_id"]: item for item in dispositions}
        ordered = [copy.deepcopy(by_id[item["signal_id"]]) for item in snapshot["signals"]]
        invalid_mandatory = [
            item["signal_id"]
            for item in ordered
            if signals[item["signal_id"]]["severity"] == "mandatory"
            and item["disposition"] == "not-incident"
        ]
        if invalid_mandatory:
            raise AssuranceError(
                "mandatory retrospective signals require issue routing or a user decision",
                code="RETROSPECTIVE_MANDATORY_ROUTE_REQUIRED",
                status="FAIL",
                details={"signal_ids": invalid_mandatory},
            )
        report_base = {
            "schema_version": 1,
            "repo_root": snapshot["repo_root"],
            "owner_session_id": owner_session_id,
            "snapshot_digest": snapshot["snapshot_digest"],
            "dispositions": ordered,
        }
        stored = {
            **report_base,
            "report_digest": digest(report_base),
            "recorded_at": now(),
        }
        existing = _read_retrospective_report(repo, owner_session_id)
        if existing is not None and existing["snapshot_digest"] == stored["snapshot_digest"]:
            if existing["report_digest"] == stored["report_digest"]:
                return retrospective_status(repo, owner_session_id)
            if not replace:
                raise AssuranceError(
                    "a different report already exists for this snapshot",
                    code="RETROSPECTIVE_REPORT_CONFLICT",
                    status="FAIL",
                )
        atomic_write_json(_retrospective_report_path(repo, owner_session_id), stored)
    return retrospective_status(repo, owner_session_id)


def evidence_state(ledger: Mapping[str, Any], kind: str) -> str:
    record = ledger.get("evidence", {}).get(kind)
    if not isinstance(record, dict):
        return "missing"
    dependency_evidence = record if kind == "machine" else None
    if record.get("dependency_digest") != evidence_dependency(
        ledger, kind, evidence=dependency_evidence
    ):
        return "stale"
    if record.get("status") == "pass" and kind == "tester":
        candidate = ledger["facets"]["execution"].get("candidate_head")
        files = record.get("details", {}).get("files", [])
        if not isinstance(candidate, str) or any(
            not isinstance(item, dict)
            or _blob_at(Path(ledger["repo_root"]), candidate, str(item.get("path", "")))
            != item.get("blob")
            for item in files
        ):
            return "stale"
    return "pass" if record.get("status") == "pass" else "failed"


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
    end_at = ledger["updated_at"]
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


def _problem_snapshot_value(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    problems = [
        {
            "key": item["key"],
            "summary": item["summary"],
            "details": item["details"],
            "owner": item["owner"],
        }
        for item in ledger.get("problems", [])
        if isinstance(item, dict) and item.get("status") == "open"
    ]
    problems.sort(key=lambda item: item["key"])
    return problems


def _open_problem_snapshot(ledger: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    problems = _problem_snapshot_value(ledger)
    keys = [item["key"] for item in problems]
    if len(keys) != len(set(keys)):
        raise AssuranceError(
            "source run has ambiguous duplicate open problem keys",
            code="PRIOR_PROBLEM_SOURCE_DUPLICATE",
            status="NEEDS_USER",
            details={"keys": keys},
        )
    return digest(problems), problems


def _lineage_ledgers(repo: Path, current: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    values = [copy.deepcopy(dict(current))]
    complete = True
    seen = {str(current["run_id"])}
    cursor = current
    while True:
        supersedes = cursor["facets"]["mission"].get("supersedes")
        if not isinstance(supersedes, dict) or supersedes.get("run_id") == cursor.get("run_id"):
            break
        source_run = str(supersedes["run_id"])
        if source_run in seen:
            raise AssuranceError(
                "revision lineage contains a cycle",
                code="REVISION_LINEAGE_CYCLE",
                status="NEEDS_USER",
            )
        try:
            source = read_ledger(repo, source_run)
        except StoreError:
            complete = False
            break
        values.append(source)
        seen.add(source_run)
        cursor = source
    values.reverse()
    return values, complete


def _derive_lineage(repo: Path, current: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(current["run_id"])
    ledgers, complete = _lineage_ledgers(repo, current)
    transitions: list[dict[str, Any]] = []
    stage_values: dict[str, dict[str, Any]] = {}
    evidence_attempts = {kind: 0 for kind in EVIDENCE_KINDS}
    retry_codes: dict[str, int] = {}
    elapsed_ms = 0
    candidate_changes = 0
    disposition_counts = {"included": 0, "handled_elsewhere": 0, "discarded": 0}
    telemetry_by_run: dict[str, dict[str, Any]] = {}
    for ledger in ledgers:
        revision = int(ledger["facets"]["mission"]["revision"])
        recorded = ledger.get("revision_transitions")
        if revision > 1 and not isinstance(ledger["facets"]["execution"].get("revision_transition"), dict):
            complete = False
        if recorded is None:
            recorded = []
        transitions.extend(copy.deepcopy(recorded))
        run_telemetry = telemetry(ledger)
        telemetry_by_run[str(ledger["run_id"])] = run_telemetry
        elapsed_ms += run_telemetry["elapsed_ms"]
        candidate_changes += run_telemetry["candidate_changes"]
        for kind, count in run_telemetry["evidence_attempts"].items():
            evidence_attempts[kind] += count
        for code, count in run_telemetry["retries"]["by_failure_code"].items():
            retry_codes[code] = retry_codes.get(code, 0) + count
        for item in run_telemetry["stages"]:
            target = stage_values.setdefault(
                item["name"],
                {
                    "name": item["name"], "attempts": 0, "completed_attempts": 0,
                    "failed_attempts": 0, "retry_count": 0, "total_duration_ms": 0,
                    "last_failure_code": None,
                },
            )
            for field in ("attempts", "completed_attempts", "failed_attempts", "retry_count", "total_duration_ms"):
                target[field] += item[field]
            if item["last_failure_code"] is not None:
                target["last_failure_code"] = item["last_failure_code"]
        for item in ledger.get("problem_dispositions", []):
            disposition = item.get("disposition")
            if disposition in disposition_counts:
                disposition_counts[disposition] += 1
    transitions.sort(key=lambda item: (item["to_revision"], item["source_run_id"], item["target_run_id"]))
    semantic = sum(1 for item in transitions if item["semantic"])
    categories: dict[str, int] = {}
    for item in transitions:
        categories[item["category"]] = categories.get(item["category"], 0) + 1
    non_semantic = len(transitions) - semantic
    pressure_value = {
        "root_run_id": ledgers[0]["run_id"],
        "current_run_id": run_id,
        "complete": complete,
        "non_semantic": non_semantic,
        "by_category": {key: value for key, value in sorted(categories.items()) if key != "mission_change"},
    }
    pressure_digest = digest(pressure_value)
    lineage_digest = digest(
        [
            {
                "run_id": item["run_id"],
                "mission_digest": item["digests"]["mission"],
                "candidate_head": item["facets"]["execution"].get("candidate_head"),
                "revision_transitions": item.get("revision_transitions"),
                "problem_dispositions": item.get("problem_dispositions"),
                "open_problem_snapshot": digest(_problem_snapshot_value(item)),
                "telemetry": {
                    key: value
                    for key, value in telemetry_by_run[str(item["run_id"])].items()
                    if key not in {"elapsed_ms", "active_stage"}
                },
            }
            for item in ledgers
        ]
    )
    review_required = not complete or non_semantic >= 3 or any(
        count >= 3 for category, count in categories.items() if category != "mission_change"
    )
    current_problem_snapshot = _problem_snapshot_value(current)
    value = {
        "schema_version": 1,
        "root_run_id": ledgers[0]["run_id"],
        "current_run_id": run_id,
        "complete": complete,
        "health": "incomplete" if not complete else ("review_required" if review_required else "healthy"),
        "revision_count": int(current["facets"]["mission"]["revision"]),
        "transitions": transitions,
        "transition_count": len(transitions),
        "non_semantic_transition_count": non_semantic,
        "transition_category_counts": dict(sorted(categories.items())),
        "cumulative_telemetry": {
            "elapsed_ms": elapsed_ms,
            "stages": [stage_values[name] for name in sorted(stage_values)],
            "candidate_changes": candidate_changes,
            "evidence_attempts": evidence_attempts,
            "evidence_replays": sum(max(0, count - 1) for count in evidence_attempts.values()),
            "retries": {"total": sum(retry_codes.values()), "by_failure_code": dict(sorted(retry_codes.items()))},
        },
        "problem_disposition_counts": disposition_counts,
        "open_problem_snapshot_digest": digest(current_problem_snapshot),
        "open_problem_keys": [item["key"] for item in current_problem_snapshot],
        "lineage_digest": lineage_digest,
        "pressure_digest": pressure_digest,
    }
    return validate_lineage(value)


def lineage(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    return _derive_lineage(repo, read_ledger(repo, run_id))


def _validate_revision_transition(
    source_lineage: Mapping[str, Any], transition: Mapping[str, Any]
) -> None:
    if transition.get("predecessor_pressure_digest") != source_lineage["pressure_digest"]:
        raise AssuranceError(
            "revision transition does not bind the current predecessor lineage",
            code="REVISION_LINEAGE_DIGEST_MISMATCH",
            status="NEEDS_USER",
        )
    category = transition.get("category")
    semantic = category == "mission_change"
    proposed_non_semantic = int(source_lineage["non_semantic_transition_count"]) + (0 if semantic else 1)
    proposed_category = int(source_lineage["transition_category_counts"].get(category, 0)) + 1
    requires_review = (
        not source_lineage["complete"]
        or (not semantic and proposed_non_semantic >= 3)
        or (not semantic and proposed_category >= 3)
    )
    decision = transition.get("architecture_review")
    valid_decision = (
        isinstance(decision, dict)
        and decision.get("decision") == "continue"
        and decision.get("pressure_digest") == source_lineage["pressure_digest"]
    )
    if requires_review and not valid_decision:
        raise AssuranceError(
            "revision lineage pressure requires an architecture-review continuation decision",
            code="LINEAGE_ARCHITECTURE_REVIEW_REQUIRED",
            status="NEEDS_USER",
            details={"pressure_digest": source_lineage["pressure_digest"]},
        )
    if isinstance(decision, dict) and not valid_decision:
        raise AssuranceError(
            "architecture-review decision is stale for the current lineage pressure",
            code="LINEAGE_ARCHITECTURE_REVIEW_REQUIRED",
            status="NEEDS_USER",
        )


def _recorded_transition(
    transition: Mapping[str, Any], *, source_run: str, target_run: str, from_revision: int, to_revision: int
) -> dict[str, Any]:
    return {
        "source_run_id": source_run,
        "target_run_id": target_run,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "category": transition["category"],
        "semantic": transition["category"] == "mission_change",
        "predecessor_pressure_digest": transition["predecessor_pressure_digest"],
        "architecture_review": copy.deepcopy(transition.get("architecture_review")),
    }


def status(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    return {
        "status": "READY" if readiness(ledger)["ready"] else "ACTIVE",
        "run_id": run_id,
        "runtime_identity": copy.deepcopy(ledger["runtime_identity"]),
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
        "environment_lease": copy.deepcopy(ledger.get("environment_lease")),
        "supersede_intent": copy.deepcopy(ledger.get("supersede_intent")),
        "abandon_intent": copy.deepcopy(ledger.get("abandon_intent")),
        "telemetry": telemetry(ledger),
        "lineage": _derive_lineage(repo, ledger),
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
        "runtime_identity": copy.deepcopy(ledger["runtime_identity"]),
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
        "environment_lease": copy.deepcopy(ledger.get("environment_lease")),
        "supersede_intent": copy.deepcopy(ledger.get("supersede_intent")),
        "abandon_intent": copy.deepcopy(ledger.get("abandon_intent")),
        "lineage": _derive_lineage(repo, ledger),
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
    resolve_plan_problem_key: str | None = None,
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
        plan_problem: dict[str, Any] | None = None
        expected_resolution: str | None = None
        if resolve_plan_problem_key is not None:
            if facet == "execution":
                raise AssuranceError(
                    "execution facet cannot resolve a plan problem",
                    code="PLAN_PROBLEM_NOT_FOUND",
                    status="FAIL",
                    details={"key": resolve_plan_problem_key, "facet": facet},
                )
            keyed = [
                item
                for item in ledger.get("problems", [])
                if item.get("key") == resolve_plan_problem_key
            ]
            open_keyed = [item for item in keyed if item.get("status") == "open"]
            if len(open_keyed) > 1:
                raise AssuranceError(
                    "multiple open problems use the requested key",
                    code="PLAN_PROBLEM_AMBIGUOUS",
                    status="FAIL",
                    details={"key": resolve_plan_problem_key},
                )
            matching = [item for item in keyed if item.get("owner") == "plan"]
            open_matching = [item for item in matching if item.get("status") == "open"]
            if open_matching:
                plan_problem = open_matching[0]
            elif matching:
                resolved = matching[-1]
                expected_resolution = (
                    f"plan-decision:{facet}:{ledger['digests'][facet]}"
                    if value == old
                    else None
                )
                if (
                    value == old
                    and resolved.get("status") == "resolved"
                    and resolved.get("resolution") == expected_resolution
                ):
                    return status(repo, run_id)
                raise AssuranceError(
                    "resolved plan problem conflicts with the requested decision",
                    code="PLAN_PROBLEM_DECISION_CONFLICT",
                    status="FAIL",
                    details={"key": resolve_plan_problem_key, "facet": facet},
                )
            else:
                raise AssuranceError(
                    "plan problem key was not found",
                    code="PLAN_PROBLEM_NOT_FOUND",
                    status="FAIL",
                    details={"key": resolve_plan_problem_key},
                )
        if value == old:
            if plan_problem is None:
                return status(repo, run_id)
            expected_resolution = f"plan-decision:{facet}:{ledger['digests'][facet]}"
            plan_problem["status"] = "resolved"
            plan_problem["resolution"] = expected_resolution
            plan_problem["resolved_at"] = now()
            current_digest = ledger["digests"][facet]
            append_event(
                ledger,
                "plan_problem_decision_applied",
                {
                    "key": resolve_plan_problem_key,
                    "facet": facet,
                    "old_digest": current_digest,
                    "new_digest": current_digest,
                    "facet_changed": False,
                },
            )
            save_ledger(repo, ledger)
            return status(repo, run_id)
        if facet == "execution" and old.get("driver_enforced") is True:
            raise AssuranceError(
                "Full Driver execution facts require dedicated transactions",
                code="DRIVER_EXECUTION_FACET_LOCKED",
            )
        if facet == "mission":
            lease = ledger.get("environment_lease")
            if isinstance(lease, dict) and lease.get("state") == "held":
                raise AssuranceError(
                    "held environment lease requires the revise-mission transaction",
                    code="MISSION_REVISION_TRANSACTION_REQUIRED",
                    status="NEEDS_USER",
                )
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
            carryover = validated["execution"].get("carryover")
            if isinstance(carryover, dict):
                declared |= {item["path"] for item in carryover["files"]}
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
        old_digest = digest(old)
        facet_changed = value != old
        ledger["facets"] = validated
        if facet == "execution" and validated["execution"].get("candidate_head") is not None:
            ledger["builder_checkpointed"] = True
        ledger["digests"] = facet_digests(validated)
        expected_resolution = f"plan-decision:{facet}:{ledger['digests'][facet]}"
        append_event(
            ledger,
            "facet_updated",
            {
                "facet": facet,
                "old_digest": old_digest,
                "new_digest": ledger["digests"][facet],
                "semantic_revision": semantic_revision,
            },
        )
        if plan_problem is not None:
            assert expected_resolution is not None
            plan_problem["status"] = "resolved"
            plan_problem["resolution"] = expected_resolution
            plan_problem["resolved_at"] = now()
            append_event(
                ledger,
                "plan_problem_decision_applied",
                {
                    "key": resolve_plan_problem_key,
                    "facet": facet,
                    "old_digest": old_digest,
                    "new_digest": ledger["digests"][facet],
                    "facet_changed": facet_changed,
                },
            )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def revise_mission(
    repo_value: str | Path,
    run_value: str,
    mission_value: Any,
    transition_value: Any,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        old = ledger["facets"]["mission"]
        if not isinstance(mission_value, dict):
            raise AssuranceError("mission revision must be an object", code="MISSION_REVISION_INVALID")
        expected_supersedes = {
            "run_id": run_id,
            "revision": old["revision"],
            "mission_digest": ledger["digests"]["mission"],
            "candidate_head": ledger["facets"]["execution"].get("candidate_head"),
        }
        if mission_value.get("revision") != old["revision"] + 1 or mission_value.get("supersedes") != expected_supersedes:
            raise AssuranceError(
                "mission revision must bind the immediately preceding run state",
                code="MISSION_REVISION_BINDING_INVALID",
                status="NEEDS_USER",
                details={"expected_supersedes": expected_supersedes},
            )
        source_lineage = _derive_lineage(repo, ledger)
        if transition_value is None:
            transition_value = {
                "category": "mission_change",
                "predecessor_pressure_digest": source_lineage["pressure_digest"],
                "architecture_review": None,
            }
        if not isinstance(transition_value, dict):
            raise AssuranceError("mission revision transition must be an object", code="REVISION_TRANSITION_REQUIRED")
        _validate_revision_transition(source_lineage, transition_value)
        if transition_value.get("category") != "mission_change":
            raise AssuranceError(
                "revise-mission requires the mission-change transition category",
                code="REVISION_TRANSITION_SEMANTICS_MISMATCH",
                status="NEEDS_USER",
            )
        candidate = copy.deepcopy(ledger["facets"])
        candidate["mission"] = copy.deepcopy(mission_value)
        candidate["execution"]["revision_transition"] = copy.deepcopy(transition_value)
        validated = validate_contract(candidate)
        lease = ledger.get("environment_lease")
        if isinstance(lease, dict) and lease.get("state") == "held":
            transaction = ledger.get("deployment_transaction")
            deployment = ledger["facets"]["execution"].get("deployment")
            if not isinstance(transaction, dict) or not isinstance(deployment, dict):
                raise AssuranceError("held environment lease lost its transaction", code="ENVIRONMENT_LEASE_INVALID")
            observed, result = _probe_environment(
                repo,
                Path(transaction["worktree"]),
                transaction["candidate_head"],
                deployment,
                artifact_sha256=lease["artifact_sha256"],
            )
            if observed != lease["active_probe"]:
                lease["state"] = "restore_failed"
                append_event(
                    ledger,
                    "environment_lease_revision_drift",
                    {"expected": lease["active_probe"], "observed": observed, "probe_result": result},
                )
                save_ledger(repo, ledger)
                raise AssuranceError(
                    "environment changed before mission revision",
                    code="ENVIRONMENT_LEASE_STATE_DRIFT",
                    status="NEEDS_USER",
                )
            lease["mission_revision"] = mission_value["revision"]
        ledger["facets"] = validated
        ledger["digests"] = facet_digests(validated)
        ledger.setdefault("revision_transitions", []).append(
            _recorded_transition(
                transition_value,
                source_run=run_id,
                target_run=run_id,
                from_revision=old["revision"],
                to_revision=mission_value["revision"],
            )
        )
        append_event(
            ledger,
            "mission_revised",
            {
                "old_revision": old["revision"],
                "new_revision": mission_value["revision"],
                "category": transition_value["category"],
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
        carryover_manifest = {
            item["path"]: item["blob"]
            for item in (execution.get("carryover") or {}).get("files", [])
        }
        tester_violations = sorted(
            path
            for path in files
            if _matches(path, ledger["facets"]["authority"]["tester_write"])
            and (
                (
                    path not in tester_files
                    or _blob_at(repo, candidate, path) != tester_manifest.get(path)
                )
                and _blob_at(repo, candidate, path) != carryover_manifest.get(path)
            )
        )
        if tester_violations:
            raise AssuranceError(
                "Builder checkpoint changed Tester-owned files",
                code="BUILDER_TESTER_OWNERSHIP_VIOLATION",
                details={"paths": tester_violations},
            )
        builder_files = sorted(
            path
            for path in files
            if path not in tester_files
            and not (
                _matches(path, ledger["facets"]["authority"]["tester_write"])
                and _blob_at(repo, candidate, path) == carryover_manifest.get(path)
            )
        )
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
        producer = {"role": role, "agent_id": agent_id, "thread_id": thread_id}
        added: list[str] = []
        for problem in report["problems"]:
            replay = next(
                (
                    item
                    for item in ledger.get("problems", [])
                    if item.get("key") == problem["key"]
                    and item.get("candidate_head") == candidate
                    and item.get("producer") == producer
                ),
                None,
            )
            if isinstance(replay, dict):
                content = {field: replay.get(field) for field in ("key", "summary", "details", "owner")}
                if content != problem:
                    raise AssuranceError(
                        "problem replay changed content for the same producer and candidate",
                        code="PROBLEM_REPLAY_MISMATCH",
                        status="FAIL",
                        details={"key": problem["key"], "candidate_head": candidate},
                    )
                continue
            stored = {
                **copy.deepcopy(problem),
                "status": "open",
                "producer": producer,
                "candidate_head": candidate,
                "recorded_at": now(),
            }
            ledger.setdefault("problems", []).append(stored)
            added.append(problem["key"])
        if not added:
            return status(repo, run_id)
        append_event(
            ledger,
            "problems_recorded",
            {"role": role, "keys": added},
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
        from ..core import python_test_role_boundary_marker

        role_boundary_findings: list[dict[str, str]] = []
        for path in tester_files:
            if not path.endswith(".py"):
                continue
            source_text = git(repo, "show", f"{source_head}:{path}", check=False)
            if source_text.returncode != 0:
                raise AssuranceError(
                    "Tester source could not be inspected before integration",
                    code="TESTER_SOURCE_INSPECTION_FAILED",
                    details={"path": path},
                )
            marker = python_test_role_boundary_marker(source_text.stdout)
            if marker is not None:
                role_boundary_findings.append({"path": path, "marker": marker})
        if role_boundary_findings:
            raise AssuranceError(
                "Tester-owned tests cannot wrap another test runner or remove proof channels",
                code="TESTER_ROLE_BOUNDARY_VIOLATION",
                status="FAIL",
                details={"findings": role_boundary_findings},
            )
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
            carryover = execution.get("carryover")
            identical_carryover = isinstance(carryover, dict) and all(
                _blob_at(repo, candidate_before, item["path"]) == item["blob"]
                for item in manifest
            )
            committed = git(
                candidate_worktree,
                "-c",
                "commit.gpgSign=false",
                "commit",
                *(["--allow-empty"] if identical_carryover else []),
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
            if isinstance(carryover, dict):
                carryover["files"] = [
                    item for item in carryover["files"] if item["path"] not in set(tester_files)
                ]
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
                lease = ledger.get("environment_lease")
                observed = details.get("deployment")
                disposition = observed.get("environment_disposition") if isinstance(observed, dict) else None
                restored = isinstance(transaction, dict) and transaction.get("state") == "restored"
                leased = (
                    isinstance(transaction, dict)
                    and transaction.get("state") == "deployed"
                    and isinstance(lease, dict)
                    and lease.get("state") == "held"
                    and disposition == "leased"
                )
                if not restored and not leased:
                    raise AssuranceError(
                        "deployment-backed blackbox evidence requires a restored or leased environment",
                        code="DEPLOYMENT_DISPOSITION_INVALID",
                    )
                expected_deployment = {
                    "target_id": transaction["target_id"],
                    "artifact_sha256": transaction["artifact_sha256"],
                    "baseline_state_digest": transaction["baseline_probe"]["state_digest"],
                    "deployed_state_digest": transaction["deployed_probe"]["state_digest"],
                    "restored_state_digest": (
                        transaction["restored_probe"]["state_digest"]
                        if restored
                        else lease["active_probe"]["state_digest"]
                    ),
                    "deploy_action": transaction.get("deploy_action", "executed"),
                    "environment_disposition": "restored" if restored else "leased",
                    "lease_id": None if restored else lease["lease_id"],
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
    if argv and Path(argv[0]).name.lower() == "uv":
        from ..core import RuntimeProblem, parse_canonical_uv_proof_command

        try:
            parsed = parse_canonical_uv_proof_command(argv)
        except RuntimeProblem as exc:
            raise AssuranceError(
                str(exc),
                code="TEST_PROOF_UV_COMMAND_INVALID",
                status="FAIL",
                details={"argv": argv},
            ) from exc
        assert parsed is not None
        return str(parsed["framework"])
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


def _proof_uv_project_identity(
    repo: Path, worktree: Path, head: str, argv: list[str]
) -> dict[str, Any] | None:
    if not argv or Path(argv[0]).name.lower() != "uv":
        return None
    launcher = Path(argv[0])
    if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise AssuranceError(
            "uv proof launcher must be an absolute regular executable file",
            code="TEST_PROOF_EXECUTABLE_INVALID",
            status="FAIL",
            details={"path": argv[0]},
        )
    files: list[dict[str, str]] = []
    for relative in ("pyproject.toml", "uv.lock"):
        path = worktree / relative
        if path.is_symlink() or not path.is_file():
            raise AssuranceError(
                "uv proof requires regular pyproject.toml and uv.lock files",
                code="TEST_PROOF_UV_PROJECT_INVALID",
                status="FAIL",
                details={"path": relative, "head": head},
            )
        blob = _blob_at(repo, head, relative)
        if blob is None:
            raise AssuranceError(
                "uv proof project files must be frozen in the proof HEAD",
                code="TEST_PROOF_UV_PROJECT_INVALID",
                status="FAIL",
                details={"path": relative, "head": head},
            )
        files.append({"path": relative, "blob": blob, "sha256": _sha256_file(path)})
    return {"files": files}


def _proof_run(
    repo: Path,
    worktree: Path,
    head: str,
    group: Mapping[str, Any],
    artifact_root: Path,
    label: str,
    expected_launcher_identity: Mapping[str, Any] | None = None,
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
    if expected_launcher_identity is not None and identity != expected_launcher_identity:
        raise AssuranceError(
            "test proof launcher identity changed during proof execution",
            code="TEST_PROOF_EXECUTABLE_IDENTITY_DRIFT",
            status="FAIL",
            details={"expected": expected_launcher_identity, "actual": identity},
        )
    project_identity = _proof_uv_project_identity(repo, worktree, head, requested)
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
    return {
        **result,
        "requested_argv": requested,
        "executable_identity": identity,
        "project_identity": project_identity,
    }


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
        launcher_identities: list[dict[str, Any] | None] = []
        candidate_worktree_path = Path(ledger["candidate_worktree"])
        for group in spec["groups"]:
            requested = list(group["argv"])
            _proof_framework(requested)
            if requested and Path(requested[0]).name.lower() == "uv":
                resolved, identity = _resolve_machine_executable(
                    repo, candidate_worktree_path, candidate, requested[0]
                )
                if resolved is None:
                    raise AssuranceError(
                        "test proof uv launcher could not be resolved",
                        code="TEST_PROOF_EXECUTABLE_INVALID",
                        status="FAIL",
                        details={"identity": identity},
                    )
                _proof_uv_project_identity(repo, candidate_worktree_path, candidate, requested)
                launcher_identities.append(identity)
            else:
                launcher_identities.append(None)
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
                    launcher_identities[index],
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
                        launcher_identities[index],
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
                        launcher_identities[index],
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


def _hold_environment_lease(
    ledger: dict[str, Any], deployment: Mapping[str, Any], transaction: Mapping[str, Any]
) -> None:
    if deployment.get("revision_retention", "restore") != "lease":
        return
    baseline = transaction.get("baseline_probe")
    active = transaction.get("deployed_probe")
    artifact = transaction.get("artifact_sha256")
    if not isinstance(baseline, dict) or not isinstance(active, dict) or not isinstance(artifact, str):
        raise AssuranceError("deployment cannot establish an environment lease", code="ENVIRONMENT_LEASE_INVALID")
    ledger["environment_lease"] = {
        "lease_id": digest(
            {
                "run_id": ledger["run_id"],
                "target_id": deployment["target_id"],
                "artifact_sha256": artifact,
                "baseline_probe": baseline,
            }
        ),
        "state": "held",
        "owner_run_id": ledger["run_id"],
        "target_id": deployment["target_id"],
        "artifact_sha256": artifact,
        "deployment_digest": digest(deployment),
        "baseline_probe": copy.deepcopy(baseline),
        "active_probe": copy.deepcopy(active),
        "mission_revision": ledger["facets"]["mission"]["revision"],
        "transferred_to_run_id": None,
    }


def _held_lease_owners(repo: Path, target_id: str, *, exclude: set[str]) -> list[str]:
    owners: list[str] = []
    runs = state_root(repo) / "runs"
    if not runs.exists():
        return owners
    for path in runs.iterdir():
        if not path.is_dir() or path.name in exclude or not (path / "ledger.json").exists():
            continue
        try:
            ledger = read_ledger(repo, path.name)
        except StoreError:
            continue
        lease = ledger.get("environment_lease")
        if (
            isinstance(lease, dict)
            and lease.get("target_id") == target_id
            and lease.get("state") in {"held", "transfer_prepared", "restore_required", "restoring", "restore_failed"}
        ):
            owners.append(path.name)
    return sorted(owners)


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
                "deploy_action": None,
                "restore_required_after_reuse": False,
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
        supersedes = ledger["facets"]["mission"].get("supersedes")
        allowed_source = supersedes["run_id"] if isinstance(supersedes, dict) else None
        competing = _held_lease_owners(
            repo,
            deployment["target_id"],
            exclude={run_id, *({allowed_source} if allowed_source else set())},
        )
        if competing:
            raise AssuranceError(
                "external target already has another environment lease owner",
                code="ENVIRONMENT_LEASE_CONFLICT",
                status="NEEDS_USER",
                details={"owners": competing},
            )
        if allowed_source is None:
            direct_owners = _held_lease_owners(repo, deployment["target_id"], exclude={run_id})
            if direct_owners:
                raise AssuranceError(
                    "external target lease requires an explicit supersedes binding",
                    code="ENVIRONMENT_LEASE_SUPERSEDES_REQUIRED",
                    status="NEEDS_USER",
                    details={"owners": direct_owners},
                )
        if isinstance(supersedes, dict):
            source = read_ledger(repo, supersedes["run_id"])
            source_lease = source.get("environment_lease")
            if isinstance(source_lease, dict) and source_lease.get("state") in {"held", "transfer_prepared"}:
                if (
                    source_lease["target_id"] != deployment["target_id"]
                    or source_lease["deployment_digest"] != digest(deployment)
                ):
                    raise AssuranceError(
                        "superseded environment lease does not match the target contract",
                        code="ENVIRONMENT_LEASE_TRANSFER_MISMATCH",
                        status="NEEDS_USER",
                    )
                observed, observed_result = _probe_environment(
                    repo,
                    worktree,
                    candidate,
                    deployment,
                    artifact_sha256=transaction["artifact_sha256"],
                )
                if observed != source_lease["active_probe"]:
                    raise AssuranceError(
                        "superseded environment changed before transfer",
                        code="ENVIRONMENT_LEASE_STATE_DRIFT",
                        status="NEEDS_USER",
                    )
                if source_lease["artifact_sha256"] == transaction["artifact_sha256"]:
                    source_lease["state"] = "transfer_prepared"
                    source_lease["transferred_to_run_id"] = run_id
                    append_event(source, "environment_lease_transfer_prepared", {"target_run_id": run_id})
                    save_ledger(repo, source)
                    transaction["baseline_probe"] = copy.deepcopy(source_lease["baseline_probe"])
                    transaction["deployed_probe"] = copy.deepcopy(observed)
                    transaction["deploy_action"] = "skipped_existing"
                    transaction["restore_required_after_reuse"] = True
                    transaction["state"] = "deployed"
                    ledger["environment_lease"] = {
                        **copy.deepcopy(source_lease),
                        "state": "held",
                        "owner_run_id": run_id,
                        "mission_revision": ledger["facets"]["mission"]["revision"],
                        "transferred_to_run_id": None,
                    }
                    ledger["supersede_intent"] = {
                        "source_run_id": source["run_id"],
                        "target_run_id": run_id,
                        "state": "received",
                    }
                    append_event(
                        ledger,
                        "environment_lease_received",
                        {"source_run_id": source["run_id"], "probe_result": observed_result},
                    )
                    save_ledger(repo, ledger)
                    source_lease["state"] = "transferred"
                    source["phase"] = "superseded"
                    source["supersede_intent"] = {
                        "source_run_id": source["run_id"],
                        "target_run_id": run_id,
                        "state": "received",
                    }
                    append_event(source, "environment_lease_transferred", {"target_run_id": run_id})
                    save_ledger(repo, source)
                    ledger["supersede_intent"] = None
                    save_ledger(repo, ledger)
                    return status(repo, run_id)
                ledger["supersede_intent"] = {
                    "source_run_id": source["run_id"],
                    "target_run_id": run_id,
                    "state": "artifact_mismatch",
                }
                append_event(
                    ledger,
                    "superseded_artifact_mismatch",
                    {
                        "source_artifact_sha256": source_lease["artifact_sha256"],
                        "candidate_artifact_sha256": transaction["artifact_sha256"],
                    },
                )
                save_ledger(repo, ledger)
                return status(repo, run_id)
        if baseline["deployed_artifact_sha256"] == transaction["artifact_sha256"]:
            transaction["deploy_action"] = "skipped_existing"
            transaction["deployed_probe"] = baseline
            transaction["state"] = "deployed"
            _hold_environment_lease(ledger, deployment, transaction)
            append_event(
                ledger,
                "deployment_skipped_existing",
                {
                    "artifact_sha256": transaction["artifact_sha256"],
                    "probe": baseline,
                    "baseline_probe_result": probe_result,
                },
            )
            save_ledger(repo, ledger)
            return status(repo, run_id)
        transaction["deploy_action"] = "executed"
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
        _hold_environment_lease(ledger, deployment, transaction)
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
        lease = ledger.get("environment_lease")
        if isinstance(lease, dict):
            lease["state"] = "restoring"
        if (
            transaction.get("deploy_action") == "skipped_existing"
            and not transaction.get("restore_required_after_reuse", False)
        ):
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
                if isinstance(lease, dict):
                    lease["state"] = "restore_failed"
                append_event(ledger, "deployment_reuse_probe_failed", {"code": exc.code})
                save_ledger(repo, ledger)
                raise AssuranceError(
                    "reused deployment could not be verified",
                    code=exc.code,
                    status="NEEDS_USER",
                    details=exc.details,
                ) from exc
            if restored != baseline:
                transaction["state"] = "restore_failed"
                transaction["failure_code"] = "DEPLOYMENT_REUSE_STATE_DRIFT"
                if isinstance(lease, dict):
                    lease["state"] = "restore_failed"
                append_event(
                    ledger,
                    "deployment_reuse_state_drift",
                    {"baseline_probe": baseline, "observed_probe": restored},
                )
                save_ledger(repo, ledger)
                raise AssuranceError(
                    "reused deployment environment changed during blackbox",
                    code="DEPLOYMENT_REUSE_STATE_DRIFT",
                    status="NEEDS_USER",
                )
            transaction["restored_probe"] = restored
            transaction["state"] = "restored"
            if isinstance(lease, dict):
                lease["state"] = "released"
            append_event(
                ledger,
                "deployment_reuse_released",
                {"probe": restored, "probe_result": probe_result},
            )
            if isinstance(ledger.get("abandon_intent"), dict):
                ledger["phase"] = "abandoned"
                append_event(ledger, "run_abandoned", copy.deepcopy(ledger["abandon_intent"]))
            save_ledger(repo, ledger)
            return status(repo, run_id)
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
            if isinstance(lease, dict):
                lease["state"] = "restore_failed"
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
            if isinstance(lease, dict):
                lease["state"] = "restore_failed"
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
            if isinstance(lease, dict):
                lease["state"] = "restore_failed"
            save_ledger(repo, ledger)
            raise AssuranceError(
                "deployment restore did not recover the baseline state",
                code="DEPLOYMENT_RESTORE_STATE_MISMATCH",
                status="NEEDS_USER",
            )
        transaction["restored_probe"] = restored
        transaction["state"] = "restored"
        if isinstance(lease, dict):
            lease["state"] = "released"
        append_event(ledger, "deployment_restored", {"probe": restored, "probe_result": probe_result})
        if isinstance(ledger.get("abandon_intent"), dict):
            ledger["phase"] = "abandoned"
            append_event(ledger, "run_abandoned", copy.deepcopy(ledger["abandon_intent"]))
        save_ledger(repo, ledger)
    return status(repo, run_id)


def restore_superseded_environment(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    target = read_ledger(repo, run_id)
    intent = target.get("supersede_intent")
    if not isinstance(intent, dict) or intent.get("state") != "artifact_mismatch":
        raise AssuranceError("superseded environment restore is not required", code="SUPERSEDE_RESTORE_NOT_REQUIRED")
    source_run = intent["source_run_id"]
    restore_deployment(repo, source_run)
    with locked(repo):
        source = read_ledger(repo, source_run)
        target = read_ledger(repo, run_id)
        source["phase"] = "superseded"
        source["supersede_intent"] = {
            "source_run_id": source_run,
            "target_run_id": run_id,
            "state": "received",
        }
        append_event(source, "run_superseded", {"target_run_id": run_id})
        target["supersede_intent"] = None
        append_event(target, "superseded_environment_restored", {"source_run_id": source_run})
        save_ledger(repo, source)
        save_ledger(repo, target)
    return status(repo, run_id)


def complete_supersede_transfer(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        target = read_ledger(repo, run_id)
        intent = target.get("supersede_intent")
        if not isinstance(intent, dict) or intent.get("state") != "received":
            raise AssuranceError("supersede transfer receipt is missing", code="SUPERSEDE_RECEIPT_MISSING")
        source = read_ledger(repo, intent["source_run_id"])
        source_lease = source.get("environment_lease")
        target_lease = target.get("environment_lease")
        if not isinstance(target_lease, dict) or target_lease.get("owner_run_id") != run_id:
            raise AssuranceError("target environment lease receipt is invalid", code="SUPERSEDE_RECEIPT_INVALID")
        if isinstance(source_lease, dict) and source_lease.get("state") != "transferred":
            source_lease["state"] = "transferred"
            source_lease["transferred_to_run_id"] = run_id
        source["phase"] = "superseded"
        source["supersede_intent"] = {
            "source_run_id": source["run_id"],
            "target_run_id": run_id,
            "state": "received",
        }
        target["supersede_intent"] = None
        append_event(source, "environment_lease_transferred", {"target_run_id": run_id})
        append_event(target, "supersede_transfer_completed", {"source_run_id": source["run_id"]})
        save_ledger(repo, source)
        save_ledger(repo, target)
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
        lease = copy.deepcopy(ledger.get("environment_lease"))
        restored = transaction["state"] == "restored"
        leased = (
            transaction["state"] == "deployed"
            and isinstance(lease, dict)
            and lease.get("state") == "held"
        )
        if not restored and not leased:
            raise AssuranceError("deployment is neither restored nor leased", code="DEPLOYMENT_DISPOSITION_INVALID")
        if leased:
            deployment = ledger["facets"]["execution"].get("deployment")
            assert isinstance(deployment, dict)
            observed, probe_result = _probe_environment(
                repo,
                Path(transaction["worktree"]),
                transaction["candidate_head"],
                deployment,
                artifact_sha256=transaction.get("artifact_sha256"),
            )
            if observed != lease["active_probe"]:
                raise AssuranceError(
                    "leased environment changed during blackbox",
                    code="ENVIRONMENT_LEASE_STATE_DRIFT",
                    status="NEEDS_USER",
                )
            append_event(ledger, "environment_lease_blackbox_confirmed", {"probe_result": probe_result})
            save_ledger(repo, ledger)
    report = pending["report"]
    report["details"]["deployment"] = {
        "target_id": transaction["target_id"],
        "artifact_sha256": transaction["artifact_sha256"],
        "baseline_state_digest": transaction["baseline_probe"]["state_digest"],
        "deployed_state_digest": transaction["deployed_probe"]["state_digest"],
        "restored_state_digest": (
            transaction["restored_probe"]["state_digest"]
            if restored
            else lease["active_probe"]["state_digest"]
        ),
        "deploy_action": transaction.get("deploy_action", "executed"),
        "environment_disposition": "restored" if restored else "leased",
        "lease_id": None if restored else lease["lease_id"],
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
            carryover = ledger["facets"]["execution"].get("carryover")
            if isinstance(carryover, dict):
                declared |= {item["path"] for item in carryover["files"]}
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
            lease = ledger.get("environment_lease")
            if (
                not isinstance(transaction, dict)
                or transaction.get("state") != "restored"
                or ledger.get("pending_blackbox") is not None
                or (isinstance(lease, dict) and lease.get("state") != "released")
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
        if ledger["phase"] in {"abandoned", "superseded"}:
            return status(repo, run_id)
        transaction = ledger.get("deployment_transaction")
        lease = ledger.get("environment_lease")
        if isinstance(transaction, dict) and transaction.get("state") not in {"restored", "restore_failed"}:
            ledger["abandon_intent"] = {"reason": reason}
            transaction["state"] = "restore_required"
            if isinstance(lease, dict):
                lease["state"] = "restore_required"
            append_event(ledger, "abandon_restore_required", {"reason": reason})
            save_ledger(repo, ledger)
            return status(repo, run_id)
        ledger["phase"] = "abandoned"
        ledger["abandon_intent"] = {"reason": reason}
        append_event(ledger, "run_abandoned", {"reason": reason})
        save_ledger(repo, ledger)
    return status(repo, run_id)


def cleanup(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] not in {"finalized", "abandoned", "superseded"}:
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
