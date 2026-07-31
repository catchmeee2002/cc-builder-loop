from __future__ import annotations

import copy
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
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
    validate_evidence_report,
    validate_repo_path,
)
from .store import (
    StoreError,
    append_event,
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
) -> dict[str, Any]:
    contract = validate_contract(contract_value)
    run_id = ensure_run_id(run_value)
    if not session_id.strip():
        raise AssuranceError("session id is required", code="SESSION_ID_REQUIRED")
    repo = resolve_repo(repo_value)
    target_branch = contract["authority"]["target_branch"]
    with locked(repo):
        if ledger_path(repo, run_id).exists():
            raise AssuranceError("assurance run already exists", code="ASSURANCE_RUN_EXISTS")
        target_head = branch_head(repo, target_branch)
        intake_sources = _validate_dirty_intake(repo, contract)
        branch = f"assurance-v4/{run_id}/candidate"
        if git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0:
            raise AssuranceError("candidate branch already exists", code="CANDIDATE_BRANCH_EXISTS")
        worktree = run_dir(repo, run_id) / "candidate"
        if worktree.exists():
            raise AssuranceError("candidate worktree already exists", code="CANDIDATE_WORKTREE_EXISTS")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        created = git(repo, "worktree", "add", "-b", branch, str(worktree), target_head, check=False)
        if created.returncode != 0:
            raise AssuranceError(
                created.stderr.strip() or "candidate worktree creation failed",
                code="CANDIDATE_WORKTREE_CREATE_FAILED",
            )
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
        except Exception:
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            git(repo, "branch", "-D", branch, check=False)
            try:
                worktree.parent.rmdir()
            except OSError:
                pass
            raise
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
        "readiness": readiness(ledger),
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
            ledger["target_start_head"],
            check=False,
        )
        if created.returncode != 0:
            raise AssuranceError(
                created.stderr.strip() or "Tester worktree creation failed",
                code="TESTER_WORKTREE_CREATE_FAILED",
            )
        agent = {"agent_id": agent_id.strip(), "thread_id": thread_id.strip()}
        replacement = {
            "head": ledger["target_start_head"],
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
        if git(repo, "merge-base", "--is-ancestor", ledger["target_start_head"], source_head, check=False).returncode != 0:
            raise AssuranceError("Tester source does not inherit target_start", code="TESTER_SOURCE_ANCESTRY_INVALID")
        tester_files = changed_files(repo, ledger["target_start_head"], source_head)
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
        role = "tester" if kind in {"tester", "blackbox"} else "reviewer"
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
            if report["status"] == "pass" and any(
                item["returncode"] != 0 or item["timed_out"] for item in details["executions"]
            ):
                raise AssuranceError("failed blackbox execution cannot produce pass", code="BLACKBOX_EXECUTION_FAILED")
        else:
            if details["reviewed_head"] != candidate:
                raise AssuranceError("review evidence is not bound to the candidate", code="REVIEW_HEAD_MISMATCH")
            if (report["status"] == "pass") != (details["result"] == "pass"):
                raise AssuranceError("review result does not match evidence status", code="EVIDENCE_RESULT_MISMATCH")
        if kind == "reviewer":
            prereqs = [name for name in ("machine", "tester", "blackbox") if name in ledger["facets"]["assurance"]["required"]]
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
                for name in ("tester", "machine")
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


def verify_machine(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if not candidate or not commit_exists(repo, candidate):
            raise AssuranceError("candidate head is unavailable", code="CANDIDATE_HEAD_INVALID")
        commands = ledger["facets"]["assurance"]["machine_commands"]
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
        passed = all(item["returncode"] == 0 and not item["timed_out"] for item in results)
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
        old_tester_head = tester_source.get("head") if isinstance(tester_source, dict) else None
        if isinstance(tester_source, dict):
            tester_rebase = git(
                Path(tester_source["worktree"]),
                "rebase",
                "--onto",
                new_base,
                old_base,
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
        append_event(
            ledger,
            "target_rematerialized",
            {"old_target_head": old_base, "new_target_head": new_base, "candidate_head": candidate},
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
