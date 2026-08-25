from __future__ import annotations

import copy
import ctypes
import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

import jsonschema

from . import SCHEMA_VERSION
from .doc_references import (
    DOC_REFERENCE_CONTRACT_VERSION,
    DocReferenceScanError,
    scan_repository,
)
from .models import (
    EVIDENCE_KINDS,
    ContractError,
    assurance_downgrades,
    authority_expands,
    builder_runtime_mode,
    compact_ineligibility_reasons,
    digest,
    doc_reference_scan_digest_input,
    evidence_dependency,
    facet_digests,
    recovery_policy,
    requested_profile,
    acceptance_observation_mode,
    validate_contract,
    validate_new_contract,
    validate_agent_result,
    validate_admission,
    validate_candidate_residue_resolution,
    validate_environment_probe,
    validate_evidence_report,
    validate_lineage,
    validate_problem_report,
    validate_repo_path,
    validate_retrospective_report,
    validate_runtime_support_manifest,
    validate_retrospective_snapshot,
    validate_stored_retrospective_report,
    validate_telemetry,
    validate_test_proof_spec,
    validate_ledger,
    schema_root,
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
    save_ledger as _store_save_ledger,
    state_root,
    target_worktree,
)
from ..process import run_owned_command

TRUSTED_SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
TRUSTED_SYSTEM_ROOTS = tuple(Path(item).resolve() for item in TRUSTED_SYSTEM_PATH.split(":"))
AUTH_UNAVAILABLE_RETRY_BASE_SECONDS = 30
AUTH_UNAVAILABLE_RETRY_MAX_SECONDS = 120
NON_SEMANTIC_CONTINUATION_WINDOW = 3
REVIEWER_COMPACTION_FAILURE_CODES = frozenset(
    {"responseStreamDisconnected", "missingAgentResult"}
)
RUNTIME_SUPPORT_MANIFEST_PATH = (
    "runtime/codex_builder_loop/assurance_v4/runtime-support.json"
)


RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

PROOF_TESTER_DIAGNOSIS_CODES = frozenset(
    {
        "PROOF_BEHAVIOR_COVERAGE_MISMATCH",
        "TEST_PROOF_BOUNDARY_TEST_IDS_INVALID",
        "TEST_PROOF_CANDIDATE_FAILED",
        "TEST_PROOF_COMMAND_UNSUPPORTED",
        "TEST_PROOF_COUNTEREXAMPLE_INVALID",
        "TEST_PROOF_MUTATION_AUTHORITY_VIOLATION",
        "TEST_PROOF_MUTATION_INVALID",
        "TEST_PROOF_TEST_SOURCE_UNBOUND",
        "TEST_PROOF_UV_COMMAND_INVALID",
    }
)
PROOF_FAILURE_VOLATILE_DETAIL_KEYS = frozenset(
    {"duration_ms", "log_path", "log_sha256", "log_tail"}
)
RUNTIME_PREPARATION_PROBLEM_KEY = "runtime-preparation-required"
PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_KEY = (
    "public-prerequisite-classification-invalid"
)
RUNTIME_PREPARATION_ERROR_CODES = frozenset(
    {
        "RUNTIME_PREPARATION_REQUIRED",
        "RUNTIME_PREPARATION_PATH_MISMATCH",
        "RUNTIME_PREPARATION_GATE_CYCLE",
        "RUNTIME_PREPARATION_ASSURANCE_INCOMPLETE",
    }
)
BUILDER_CHECKPOINT_ACTIONS = frozenset({"builder_implement", "builder_fix"})
BUILDER_SIDE_EFFECT_ACTIONS = frozenset(
    {"builder_implement", "builder_fix", "builder_recompose_fix"}
)
ROOT_SESSION_AGENT_PREFIX = "codex-root-session:"
CANDIDATE_RESIDUE_RENAME_EXCHANGE = 2
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = _LIBC.renameat2


def _builder_mode(ledger: Mapping[str, Any]) -> str:
    return builder_runtime_mode(ledger["facets"])


def _validate_builder_runtime_binding(
    contract: Mapping[str, Any],
    session_id: str,
    driver_runtime: Mapping[str, Any] | None,
) -> None:
    if not isinstance(driver_runtime, Mapping):
        return
    mode = builder_runtime_mode(contract)
    transport = driver_runtime.get("transport")
    if mode == "root_session":
        if (
            driver_runtime.get("kind") != "native"
            or transport != "root_session"
        ):
            raise AssuranceError(
                "root_session Builder mode requires the root-session Native runtime",
                code="BUILDER_RUNTIME_MODE_TRANSPORT_MISMATCH",
                status="FAIL",
                details={
                    "builder_mode": mode,
                    "driver_kind": driver_runtime.get("kind"),
                    "driver_transport": transport,
                },
            )
        identity = driver_runtime.get("root_session_identity")
        expected_agent = ROOT_SESSION_AGENT_PREFIX + session_id
        expected_digest = digest(
            {
                "mode": "root_session",
                "session_id": session_id,
                "agent_id": expected_agent,
            }
        )
        if (
            not isinstance(identity, Mapping)
            or identity.get("session_id") != session_id
            or identity.get("agent_id") != expected_agent
            or identity.get("identity_digest") != expected_digest
        ):
            raise AssuranceError(
                "root-session Native runtime identity is not bound to the run owner",
                code="BUILDER_RUNTIME_ROOT_IDENTITY_MISMATCH",
                status="FAIL",
                details={
                    "session_id": session_id,
                    "root_session_identity": copy.deepcopy(identity),
                },
            )
    elif transport == "root_session":
        raise AssuranceError(
            "native_thread Builder mode cannot use the root-session transport",
            code="BUILDER_RUNTIME_MODE_TRANSPORT_MISMATCH",
            status="FAIL",
            details={
                "builder_mode": mode,
                "driver_kind": driver_runtime.get("kind"),
                "driver_transport": transport,
            },
        )


def _builder_owner_mode(owner: Mapping[str, Any] | None) -> str:
    if owner and owner.get("mode") in {"root_session", "native_thread"}:
        return str(owner["mode"])
    return "native_thread"


def _builder_owner_session(owner: Mapping[str, Any] | None) -> str | None:
    value = owner.get("session_id") if isinstance(owner, Mapping) else None
    return value if isinstance(value, str) and value else None


def _builder_owner_thread(owner: Mapping[str, Any] | None) -> str | None:
    value = owner.get("thread_id") if isinstance(owner, Mapping) else None
    return value if isinstance(value, str) and value else None


def _root_session_id(ledger: Mapping[str, Any]) -> str | None:
    runtime = ledger.get("driver_runtime")
    identity = runtime.get("root_session_identity") if isinstance(runtime, Mapping) else None
    value = identity.get("session_id") if isinstance(identity, Mapping) else None
    if isinstance(value, str) and value:
        return value
    owner = ledger.get("facets", {}).get("execution", {}).get("agents", {}).get("builder")
    return _builder_owner_session(owner if isinstance(owner, Mapping) else None)


def _assert_root_session_identity(
    ledger: Mapping[str, Any], owner_session_id: str | None
) -> None:
    expected = _root_session_id(ledger)
    if not expected or owner_session_id != expected:
        raise AssuranceError(
            "root Builder session identity does not match",
            code="BUILDER_OWNER_SESSION_MISMATCH",
            status="FAIL",
            details={
                "expected_session_id": expected,
                "provided_session_id": owner_session_id,
            },
        )


def _assert_builder_owner(
    ledger: Mapping[str, Any],
    *,
    owner_session_id: str | None = None,
    owner_thread_id: str | None = None,
) -> dict[str, Any]:
    owner = ledger["facets"]["execution"]["agents"].get("builder")
    if not isinstance(owner, Mapping):
        raise AssuranceError(
            "Builder owner is not prepared",
            code="BUILDER_OWNER_MISSING",
            status="NEEDS_USER",
        )
    mode = _builder_owner_mode(owner)
    if mode != _builder_mode(ledger):
        raise AssuranceError(
            "Builder owner mode does not match the frozen contract",
            code="BUILDER_OWNER_MODE_MISMATCH",
            status="NEEDS_USER",
            details={
                "contract_mode": _builder_mode(ledger),
                "owner_mode": mode,
            },
        )
    if mode == "root_session":
        expected = _builder_owner_session(owner)
        _assert_root_session_identity(ledger, owner_session_id)
        if not expected or owner_session_id != expected:
            raise AssuranceError(
                "root Builder owner identity does not match the session",
                code="BUILDER_OWNER_SESSION_MISMATCH",
                status="FAIL",
                details={
                    "expected_session_id": expected,
                    "provided_session_id": owner_session_id,
                },
            )
    else:
        expected = _builder_owner_thread(owner)
        if owner_thread_id is not None and expected != owner_thread_id:
            raise AssuranceError(
                "Native Builder thread identity does not match",
                code="BUILDER_OWNER_THREAD_MISMATCH",
                status="FAIL",
                details={
                    "expected_thread_id": expected,
                    "provided_thread_id": owner_thread_id,
                },
            )
    return dict(owner)


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


def save_ledger(repo: Path, ledger: dict[str, Any]) -> None:
    from ..core import runtime_identity_compatibility

    compatibility = runtime_identity_compatibility(ledger["runtime_identity"])
    safe_previous_terminal = compatibility["state"] == "previous-terminal-only" and (
        ledger.get("phase") in {"finalized", "failed", "abandoned", "superseded"}
        or (
            ledger.get("phase") == "finalizing"
            and ledger.get("finalize_intent") is not None
        )
    )
    if compatibility["state"] != "current" and not safe_previous_terminal:
        raise AssuranceError(
            "runtime identity is not compatible with active ledger mutation",
            code="RUNTIME_IDENTITY_MUTATION_INCOMPATIBLE",
            status="NEEDS_USER",
            details={"runtime_compatibility": compatibility},
        )
    _store_save_ledger(repo, ledger)


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


def _regular_blob_at(repo: Path, head: str, path: str) -> str | None:
    result = git(
        repo,
        "ls-tree",
        "-z",
        head,
        "--",
        f":(literal){path}",
        check=False,
    )
    if (
        result.returncode != 0
        or not result.stdout.endswith("\0")
        or result.stdout.count("\0") != 1
    ):
        return None
    metadata, separator, listed_path = result.stdout[:-1].partition("\t")
    parts = metadata.split()
    if (
        separator != "\t"
        or listed_path != path
        or len(parts) != 3
        or parts[0] not in {"100644", "100755"}
        or parts[1] != "blob"
    ):
        return None
    return parts[2]


def _regular_blob_sha256_at(repo: Path, head: str, path: str) -> tuple[str, str] | None:
    blob = _regular_blob_at(repo, head, path)
    if blob is None:
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", blob],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return None
    return blob, hashlib.sha256(result.stdout).hexdigest()


def _candidate_manifest(repo: Path, base: str, candidate: str) -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    for path in changed_files(repo, base, candidate):
        blob = _blob_at(repo, candidate, path)
        if blob is not None:
            manifest.append({"path": path, "blob": blob})
    return manifest


def _candidate_worktree_manifest(worktree: Path) -> dict[str, Any]:
    """Capture durable candidate changes without mutating the worktree."""

    head_result = git(worktree, "rev-parse", "HEAD", check=False)
    head = head_result.stdout.strip() if head_result.returncode == 0 else None
    paths = dirty_paths(worktree)
    entries: list[dict[str, Any]] = []
    for relative in paths:
        path = worktree / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            entries.append(
                {
                    "path": relative,
                    "status": "deleted",
                    "sha256": None,
                    "size_bytes": 0,
                }
            )
            continue
        except OSError:
            entries.append(
                {
                    "path": relative,
                    "status": "unreadable",
                    "sha256": None,
                    "size_bytes": 0,
                }
            )
            continue

        hasher = hashlib.sha256()
        size = 0
        try:
            if stat.S_ISREG(info.st_mode):
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                        size += len(chunk)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(path).encode("utf-8", errors="surrogateescape")
                hasher.update(target)
                size = len(target)
            else:
                hasher.update(stat.filemode(info.st_mode).encode("ascii"))
        except OSError:
            entries.append(
                {
                    "path": relative,
                    "status": "unreadable",
                    "sha256": None,
                    "size_bytes": 0,
                }
            )
            continue
        entries.append(
            {
                "path": relative,
                "status": "present",
                "sha256": hasher.hexdigest(),
                "size_bytes": size,
            }
        )

    manifest: dict[str, Any] = {
        "head": head,
        "dirty_paths": paths,
        "entries": entries,
    }
    manifest["manifest_digest"] = digest(manifest)
    return manifest


def _candidate_manifest_for_ledger(ledger: Mapping[str, Any]) -> dict[str, Any] | None:
    worktree_value = ledger.get("candidate_worktree")
    if not isinstance(worktree_value, str) or not worktree_value:
        return None
    worktree = Path(worktree_value)
    if not worktree.is_dir():
        return None
    try:
        return _candidate_worktree_manifest(worktree)
    except (OSError, StoreError):
        return None


def _runtime_source_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git_common_dir(repo: Path) -> Path | None:
    result = git(repo, "rev-parse", "--git-common-dir", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    value = Path(result.stdout.strip())
    if not value.is_absolute():
        value = (repo / value).resolve()
    return value.resolve()


def _load_runtime_support_manifest(
    source_root: Path,
    *,
    runtime_head: str | None = None,
    expected_blob: str | None = None,
    expected_digest: str | None = None,
) -> tuple[dict[str, Any], str | None, str | None, str]:
    explicit_head = runtime_head is not None
    head = runtime_head
    if head is None:
        resolved = git(source_root, "rev-parse", "--verify", "HEAD^{commit}", check=False)
        if resolved.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", resolved.stdout.strip()):
            head = resolved.stdout.strip()
    raw: str
    blob: str | None = None
    if head is not None:
        shown = git(
            source_root,
            "show",
            f"{head}:{RUNTIME_SUPPORT_MANIFEST_PATH}",
            check=False,
        )
        resolved_blob = git(
            source_root,
            "rev-parse",
            f"{head}:{RUNTIME_SUPPORT_MANIFEST_PATH}",
            check=False,
        )
        if shown.returncode == 0 and resolved_blob.returncode == 0:
            raw = shown.stdout
            blob = resolved_blob.stdout.strip()
        elif explicit_head:
            raise AssuranceError(
                "runtime support manifest is unavailable at the frozen runtime HEAD",
                code="RUNTIME_SUPPORT_MANIFEST_INVALID",
                status="FAIL",
                details={"runtime_head": head},
            )
        else:
            path = source_root / RUNTIME_SUPPORT_MANIFEST_PATH
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise AssuranceError(
                    "runtime support manifest is unavailable",
                    code="RUNTIME_SUPPORT_MANIFEST_INVALID",
                    status="FAIL",
                    details={"path": str(path)},
                ) from exc
            hashed = git(source_root, "hash-object", "--", str(path), check=False)
            blob = hashed.stdout.strip() if hashed.returncode == 0 else None
            head = None
    else:
        path = source_root / RUNTIME_SUPPORT_MANIFEST_PATH
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AssuranceError(
                "runtime support manifest is unavailable",
                code="RUNTIME_SUPPORT_MANIFEST_INVALID",
                status="FAIL",
                details={"path": str(path)},
            ) from exc
    try:
        manifest = validate_runtime_support_manifest(json.loads(raw))
    except (json.JSONDecodeError, ContractError) as exc:
        raise AssuranceError(
            str(exc),
            code=getattr(exc, "code", "RUNTIME_SUPPORT_MANIFEST_INVALID"),
            status="FAIL",
            details=getattr(exc, "details", {}),
        ) from exc
    manifest_digest = digest(manifest)
    if expected_blob is not None and blob != expected_blob:
        raise AssuranceError(
            "runtime support manifest blob does not match the frozen ledger fact",
            code="RUNTIME_SUPPORT_MANIFEST_DRIFT",
            status="NEEDS_USER",
            details={"expected_blob": expected_blob, "actual_blob": blob},
        )
    if expected_digest is not None and manifest_digest != expected_digest:
        raise AssuranceError(
            "runtime support manifest digest does not match the frozen ledger fact",
            code="RUNTIME_SUPPORT_MANIFEST_DRIFT",
            status="NEEDS_USER",
            details={
                "expected_manifest_digest": expected_digest,
                "actual_manifest_digest": manifest_digest,
            },
        )
    return manifest, head, blob, manifest_digest


def _runtime_support_selection(
    manifest: Mapping[str, Any],
    paths: Sequence[str],
) -> tuple[list[str], list[str], list[str]]:
    affected_paths: set[str] = set()
    affected_gates: set[str] = set()
    required_independent: set[str] = set()
    for support in manifest["support_sets"]:
        matched = {
            path
            for path in paths
            if _matches(path, list(support["path_patterns"]))
        }
        if not matched:
            continue
        affected_paths.update(matched)
        affected_gates.update(support["affected_gates"])
        required_independent.update(support["required_independent_gates"])
    return (
        sorted(affected_paths),
        sorted(affected_gates),
        sorted(required_independent),
    )


def _runtime_support_snapshot(
    repo: Path,
    contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    source_root = _runtime_source_root()
    manifest, runtime_head, manifest_blob, manifest_digest = (
        _load_runtime_support_manifest(source_root)
    )
    source_common = _git_common_dir(source_root)
    target_common = _git_common_dir(repo)
    self_hosted = source_common is not None and source_common == target_common
    candidate_paths: list[str] = []
    required_independent: list[str] = []
    affected_paths: list[str] = []
    affected_gates: list[str] = []
    if self_hosted:
        if runtime_head is None:
            raise AssuranceError(
                "self-hosted runtime support requires a frozen runtime Git identity",
                code="RUNTIME_SUPPORT_UNAVAILABLE",
                status="NEEDS_USER",
            )
        listed = git(
            source_root,
            "ls-tree",
            "-r",
            "--name-only",
            runtime_head,
            check=False,
        )
        if listed.returncode != 0:
            raise AssuranceError(
                "self-hosted runtime support manifest could not resolve its tracked inputs",
                code="RUNTIME_SUPPORT_UNAVAILABLE",
                status="NEEDS_USER",
            )
        authority_patterns = [
            *contract["authority"]["builder_write"],
            *contract["authority"]["tester_write"],
        ]
        candidate_paths = [
            path
            for path in listed.stdout.splitlines()
            if path and _matches(path, authority_patterns)
        ]
        affected_paths, affected_gates, required_independent = (
            _runtime_support_selection(manifest, candidate_paths)
        )
    snapshot = {
        "schema_version": 1,
        "mode": "self_hosted" if self_hosted else "external",
        "runtime_head": runtime_head,
        "manifest_blob": manifest_blob,
        "manifest_digest": manifest_digest,
        "affected_gates": affected_gates,
        "affected_paths": affected_paths,
    }
    return snapshot, manifest, required_independent


def _assert_runtime_support_contract(
    contract: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    required_independent: Sequence[str],
) -> None:
    if snapshot.get("mode") != "self_hosted" or not snapshot.get("affected_paths"):
        return
    details = {
        "runtime_support": copy.deepcopy(snapshot),
        "required_independent_gates": sorted(set(required_independent)),
    }
    if contract["mission"].get("delivery_kind", "code") != "preparation":
        raise AssuranceError(
            "self-hosted assurance runtime changes require a protected preparation",
            code="RUNTIME_PREPARATION_REQUIRED",
            status="NEEDS_USER",
            details=details,
        )
    declared = sorted(contract["authority"].get("protected_support_paths", []))
    expected = sorted(snapshot["affected_paths"])
    if declared != expected:
        raise AssuranceError(
            "protected preparation paths do not exactly match the affected runtime support",
            code="RUNTIME_PREPARATION_PATH_MISMATCH",
            status="NEEDS_USER",
            details={**details, "expected_paths": expected, "declared_paths": declared},
        )
    required = set(contract["assurance"]["required"])
    cycle = sorted(required & set(snapshot["affected_gates"]))
    if cycle:
        raise AssuranceError(
            "protected preparation cannot require the assurance gate that it changes",
            code="RUNTIME_PREPARATION_GATE_CYCLE",
            status="NEEDS_USER",
            details={**details, "cyclic_gates": cycle},
        )
    missing = sorted(set(required_independent) - required)
    if missing:
        raise AssuranceError(
            "protected preparation is missing independent assurance gates",
            code="RUNTIME_PREPARATION_ASSURANCE_INCOMPLETE",
            status="NEEDS_USER",
            details={**details, "missing_gates": missing},
        )


def _runtime_support_for_changed_paths(
    repo: Path,
    ledger: Mapping[str, Any],
    paths: Sequence[str],
) -> tuple[dict[str, Any], list[str]]:
    frozen = ledger.get("runtime_support")
    if not isinstance(frozen, Mapping) or frozen.get("mode") != "self_hosted":
        return copy.deepcopy(frozen), []
    manifest, _head, _blob, _manifest_digest = _load_runtime_support_manifest(
        repo,
        runtime_head=frozen.get("runtime_head"),
        expected_blob=frozen.get("manifest_blob"),
        expected_digest=frozen.get("manifest_digest"),
    )
    affected_paths, affected_gates, required_independent = (
        _runtime_support_selection(manifest, paths)
    )
    snapshot = {
        **copy.deepcopy(frozen),
        "affected_gates": affected_gates,
        "affected_paths": affected_paths,
    }
    return snapshot, required_independent


def _contract_command_surfaces(
    contract: Mapping[str, Any],
) -> list[tuple[str, str, Mapping[str, Any]]]:
    commands = [
        ("assurance.machine_commands", "verify_machine", command)
        for command in contract["assurance"]["machine_commands"]
    ]
    commands.extend(
        ("execution.commands", "complete_blackbox", command)
        for command in contract["execution"]["commands"]
    )
    deployment = contract["execution"].get("deployment")
    if isinstance(deployment, Mapping):
        for field, latest_check_stage in (
            ("build_command", "prepare_deployment"),
            ("deploy_command", "deploy"),
            ("probe_command", "probe"),
            ("restore_command", "restore"),
        ):
            commands.append(
                (f"execution.deployment.{field}", latest_check_stage, deployment[field])
            )
    return commands


def _public_prerequisite_classification(
    repo: Path,
    execution: Mapping[str, Any],
    paths: Sequence[str],
    *,
    candidate: str | None,
) -> list[dict[str, str]]:
    builder_files = set(execution.get("builder_files", []))
    carryover = execution.get("carryover")
    carryover_manifest = (
        {item["path"]: item["blob"] for item in carryover.get("files", [])}
        if isinstance(carryover, Mapping)
        else {}
    )
    candidate_available = (
        isinstance(candidate, str) and commit_exists(repo, candidate)
    )
    result: list[dict[str, str]] = []
    for path in paths:
        blob = _blob_at(repo, candidate, path) if candidate_available else None
        if path in builder_files and blob is not None:
            result.append({"path": path, "status": "ready", "source": "builder"})
        elif carryover_manifest.get(path) is not None and blob == carryover_manifest[path]:
            result.append({"path": path, "status": "ready", "source": "carryover"})
        else:
            result.append({"path": path, "status": "deferred", "source": "builder"})
    return result


def _prospective_admission_execution(
    repo: Path, contract: Mapping[str, Any]
) -> tuple[dict[str, Any], str | None]:
    execution = copy.deepcopy(contract["execution"])
    candidate = execution.get("candidate_head")
    if not isinstance(candidate, str) or not commit_exists(repo, candidate):
        candidate = None
    supersedes = contract["mission"].get("supersedes")
    if not isinstance(supersedes, Mapping):
        return execution, candidate
    requested_candidate = supersedes.get("candidate_head")
    if not isinstance(requested_candidate, str) or not commit_exists(
        repo, requested_candidate
    ):
        return execution, candidate
    candidate = requested_candidate
    if isinstance(execution.get("carryover"), Mapping):
        return execution, candidate
    try:
        source = read_ledger(repo, str(supersedes["run_id"]))
    except (KeyError, StoreError):
        return execution, candidate
    source_candidate = source["facets"]["execution"].get("candidate_head")
    if (
        source_candidate != requested_candidate
        or source["facets"]["mission"].get("revision") != supersedes.get("revision")
        or source["digests"].get("mission") != supersedes.get("mission_digest")
    ):
        return execution, candidate
    execution["carryover"] = {
        "source_run_id": source["run_id"],
        "source_candidate_head": requested_candidate,
        "files": _candidate_manifest(
            repo,
            source["target_start_head"],
            requested_candidate,
        ),
    }
    return execution, candidate


def _admission_report(
    repo: Path,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    command_admission: list[dict[str, Any]] = []
    for surface, latest_check_stage, command in _contract_command_surfaces(contract):
        requested = command["argv"][0]
        requested_path = Path(requested)
        if not requested_path.is_absolute() and "/" in requested:
            command_admission.append(
                {
                    "surface": surface,
                    "command_id": command["id"],
                    "requested_executable": requested,
                    "status": "deferred",
                    "identity": {
                        "kind": "repository",
                        "requested": requested,
                        "path": requested,
                    },
                    "reason": "candidate_bound",
                    "latest_check_stage": latest_check_stage,
                }
            )
            continue
        try:
            _executable, identity = _resolve_host_machine_executable(requested)
        except AssuranceError as exc:
            identity = {
                "kind": "system",
                "requested": requested,
                "reason": exc.code,
            }
            resolved = exc.details.get("resolved")
            if isinstance(resolved, str) and resolved:
                identity["path"] = resolved
            command_admission.append(
                {
                    "surface": surface,
                    "command_id": command["id"],
                    "requested_executable": requested,
                    "status": "blocked",
                    "identity": identity,
                    "reason": exc.code,
                    "latest_check_stage": latest_check_stage,
                }
            )
            continue
        status_value = "ready" if _executable is not None else "blocked"
        command_admission.append(
            {
                "surface": surface,
                "command_id": command["id"],
                "requested_executable": requested,
                "status": status_value,
                "identity": identity,
                "reason": None if status_value == "ready" else identity.get("reason"),
                "latest_check_stage": latest_check_stage,
            }
        )
    execution, candidate = _prospective_admission_execution(repo, contract)
    public_prerequisites = _public_prerequisite_classification(
        repo,
        execution,
        contract["authority"].get("public_prerequisites", []),
        candidate=candidate,
    )
    report = {
        "schema_version": 1,
        "status": (
            "BLOCKED"
            if any(item["status"] == "blocked" for item in command_admission)
            else "READY"
        ),
        "trusted_system_path": TRUSTED_SYSTEM_PATH,
        "commands": command_admission,
        "public_prerequisites": public_prerequisites,
    }
    return validate_admission(report)


def _require_admission(repo: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    admission = _admission_report(repo, contract)
    if admission["status"] == "BLOCKED":
        raise AssuranceError(
            "one or more host executables are unavailable from the trusted execution boundary",
            code="ASSURANCE_ADMISSION_BLOCKED",
            status="FAIL",
            details={"admission": admission},
        )
    return admission


def _runtime_preparation_problem_details(error: AssuranceError) -> dict[str, Any]:
    return {
        "code": error.code,
        "message": str(error),
        **copy.deepcopy(error.details),
    }


def _assert_completed_builder_dispatch(
    repo: Path,
    source_ledger: Mapping[str, Any],
    problem: Mapping[str, Any],
) -> None:
    execution = source_ledger["facets"]["execution"]
    builder = execution.get("agents", {}).get("builder")
    expected_producer = (
        {"role": "builder", **copy.deepcopy(builder)}
        if isinstance(builder, Mapping)
        else None
    )
    intent = source_ledger.get("dispatch_intent")
    builder_mode = _builder_owner_mode(builder if isinstance(builder, Mapping) else None)
    builder_identity_matches = (
        (
            builder_mode == "root_session"
            and intent.get("owner_mode") == "root_session"
            and intent.get("owner_session_id") == builder.get("session_id")
            and intent.get("thread_id") is None
        )
        if (
            builder_mode == "root_session"
            and isinstance(intent, Mapping)
            and isinstance(builder, Mapping)
        )
        else (
            isinstance(intent, Mapping)
            and isinstance(builder, Mapping)
            and intent.get("thread_id") == builder.get("thread_id")
        )
    )
    if (
        expected_producer is None
        or problem.get("producer") != expected_producer
        or not isinstance(intent, Mapping)
        or intent.get("state") != "completed"
        or intent.get("role") != "builder"
        or intent.get("action") not in BUILDER_CHECKPOINT_ACTIONS
        or not builder_identity_matches
    ):
        raise AssuranceError(
            "rejected checkpoint successor requires one completed unconsumed Builder dispatch",
            code="SUPERSEDE_REJECTED_DISPATCH_UNAVAILABLE",
            status="NEEDS_USER",
        )
    action_id = intent.get("action_id")
    if not isinstance(action_id, str) or re.fullmatch(r"[0-9a-f]{64}", action_id) is None:
        raise AssuranceError(
            "rejected checkpoint Builder dispatch identity is invalid",
            code="SUPERSEDE_REJECTED_DISPATCH_INVALID",
            status="NEEDS_USER",
        )
    expected_artifact = (
        run_dir(repo, str(source_ledger["run_id"]))
        / "artifacts"
        / f"dispatch-{action_id}.json"
    )
    result_path = intent.get("result_path")
    if (
        not isinstance(result_path, str)
        or Path(result_path) != expected_artifact
        or expected_artifact.is_symlink()
        or not expected_artifact.is_file()
    ):
        raise AssuranceError(
            "rejected checkpoint Builder dispatch artifact is unavailable",
            code="SUPERSEDE_REJECTED_DISPATCH_ARTIFACT_INVALID",
            status="NEEDS_USER",
        )
    try:
        raw_result = json.loads(expected_artifact.read_text(encoding="utf-8"))
        result = validate_agent_result(raw_result)
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        raise AssuranceError(
            "rejected checkpoint Builder dispatch artifact is invalid",
            code="SUPERSEDE_REJECTED_DISPATCH_ARTIFACT_INVALID",
            status="NEEDS_USER",
        ) from exc
    expected_result = {
        "result": "implemented",
        "evidence_report": None,
        "proof_spec": None,
        "problem_report": None,
    }
    if result != expected_result or intent.get("result_digest") != digest(result):
        raise AssuranceError(
            "rejected checkpoint Builder dispatch result does not prove an implementation",
            code="SUPERSEDE_REJECTED_DISPATCH_RESULT_MISMATCH",
            status="NEEDS_USER",
        )


def _rejected_checkpoint_candidate(
    repo: Path,
    source_ledger: Mapping[str, Any],
    requested_candidate: str,
) -> str:
    problems = [
        item
        for item in source_ledger.get("problems", [])
        if isinstance(item, Mapping)
        and item.get("key") == RUNTIME_PREPARATION_PROBLEM_KEY
        and item.get("status") == "open"
    ]
    if len(problems) != 1:
        raise AssuranceError(
            "rejected checkpoint successor requires exactly one open runtime preparation problem",
            code="SUPERSEDE_REJECTED_PROBLEM_AMBIGUOUS",
            status="NEEDS_USER",
            details={"problem_count": len(problems)},
        )
    problem = problems[0]
    if (
        problem.get("owner") != "builder_loop"
        or problem.get("summary")
        != "Candidate changes require a protected runtime preparation"
        or problem.get("candidate_head") != requested_candidate
    ):
        raise AssuranceError(
            "mission supersedes binding does not match the rejected checkpoint problem",
            code="MISSION_SUPERSEDES_MISMATCH",
        )
    source_candidate = source_ledger["facets"]["execution"].get("candidate_head")
    if (
        not isinstance(source_candidate, str)
        or requested_candidate == source_candidate
        or not commit_exists(repo, source_candidate)
        or not commit_exists(repo, requested_candidate)
        or git(
            repo,
            "merge-base",
            "--is-ancestor",
            source_candidate,
            requested_candidate,
            check=False,
        ).returncode
        != 0
    ):
        raise AssuranceError(
            "rejected checkpoint candidate is not newer than the source execution candidate",
            code="SUPERSEDE_REJECTED_CANDIDATE_ANCESTRY_INVALID",
            status="NEEDS_USER",
        )
    worktree = Path(str(source_ledger["candidate_worktree"]))
    branch = str(source_ledger["candidate_branch"])
    branch_result = git(
        repo,
        "rev-parse",
        "--verify",
        f"refs/heads/{branch}",
        check=False,
    )
    worktree_result = git(worktree, "rev-parse", "HEAD", check=False)
    symbolic_result = git(worktree, "symbolic-ref", "--quiet", "HEAD", check=False)
    if (
        branch_result.returncode != 0
        or branch_result.stdout.strip() != requested_candidate
        or worktree_result.returncode != 0
        or worktree_result.stdout.strip() != requested_candidate
        or symbolic_result.returncode != 0
        or symbolic_result.stdout.strip() != f"refs/heads/{branch}"
        or dirty_paths(worktree)
    ):
        raise AssuranceError(
            "rejected checkpoint candidate branch or worktree drifted",
            code="SUPERSEDE_CANDIDATE_DRIFT",
            status="NEEDS_USER",
        )
    target_head = str(source_ledger["target_start_head"])
    if (
        not commit_exists(repo, target_head)
        or git(
            repo,
            "merge-base",
            "--is-ancestor",
            target_head,
            requested_candidate,
            check=False,
        ).returncode
        != 0
    ):
        raise AssuranceError(
            "rejected checkpoint candidate does not descend from the source target",
            code="SUPERSEDE_REJECTED_CANDIDATE_ANCESTRY_INVALID",
            status="NEEDS_USER",
        )
    files = changed_files(repo, target_head, requested_candidate)
    _validated_builder_files(
        repo,
        source_ledger,
        candidate=requested_candidate,
        files=files,
    )
    actual_runtime_support, required_independent = _runtime_support_for_changed_paths(
        repo,
        source_ledger,
        files,
    )
    try:
        _assert_runtime_support_contract(
            source_ledger["facets"],
            actual_runtime_support,
            required_independent,
        )
    except AssuranceError as error:
        if error.code not in RUNTIME_PREPARATION_ERROR_CODES:
            raise
        expected_problem_details = _runtime_preparation_problem_details(error)
    else:
        raise AssuranceError(
            "rejected checkpoint runtime preparation problem is no longer reproducible",
            code="SUPERSEDE_REJECTED_PROBLEM_STALE",
            status="NEEDS_USER",
        )
    try:
        recorded_problem_details = json.loads(str(problem.get("details")))
    except json.JSONDecodeError as exc:
        raise AssuranceError(
            "rejected checkpoint runtime preparation problem is malformed",
            code="SUPERSEDE_REJECTED_PROBLEM_STALE",
            status="NEEDS_USER",
        ) from exc
    if recorded_problem_details != expected_problem_details:
        raise AssuranceError(
            "rejected checkpoint runtime preparation facts changed",
            code="SUPERSEDE_REJECTED_PROBLEM_STALE",
            status="NEEDS_USER",
            details={
                "expected_problem_details": expected_problem_details,
                "recorded_problem_details": recorded_problem_details,
            },
        )
    _assert_completed_builder_dispatch(repo, source_ledger, problem)
    return requested_candidate


def validate(contract: Any, repo_value: str | Path | None = None) -> dict[str, Any]:
    value = validate_new_contract(contract)
    result = {
        "status": "READY",
        "schema_version": SCHEMA_VERSION,
        "digests": facet_digests(value),
        "profile": requested_profile(value),
        "recovery_policy": recovery_policy(value),
    }
    if repo_value is not None:
        repo = resolve_repo(repo_value)
        runtime_support, _manifest, required_independent = (
            _runtime_support_snapshot(repo, value)
        )
        _assert_runtime_support_contract(value, runtime_support, required_independent)
        result["runtime_support"] = runtime_support
        result["admission"] = _require_admission(repo, value)
    return result


def _reject_acceptance_observation_downgrade(
    old_mission: Mapping[str, Any], new_mission: Mapping[str, Any]
) -> None:
    old_mode = acceptance_observation_mode({"mission": old_mission})
    new_mode = acceptance_observation_mode({"mission": new_mission})
    if old_mode == "bound" and new_mode != "bound":
        raise AssuranceError(
            "an active mission cannot remove frozen acceptance observations",
            code="ACCEPTANCE_OBSERVATION_DOWNGRADE_FORBIDDEN",
            status="NEEDS_USER",
        )


def start(
    repo_value: str | Path,
    run_value: str,
    session_id: str,
    contract_value: Any,
    *,
    driver_runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_contract_digest = digest(contract_value)
    contract = validate_new_contract(contract_value)
    run_id = ensure_run_id(run_value)
    if not session_id.strip():
        raise AssuranceError("session id is required", code="SESSION_ID_REQUIRED")
    repo = resolve_repo(repo_value)
    runtime_support, _runtime_manifest, required_independent = (
        _runtime_support_snapshot(repo, contract)
    )
    _assert_runtime_support_contract(
        contract,
        runtime_support,
        required_independent,
    )
    _require_admission(repo, contract)
    _validate_builder_runtime_binding(contract, session_id.strip(), driver_runtime)
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
                _seal_replayed_successor_source(repo, existing, contract, session_id)
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
            _assert_no_candidate_residue_intent(source_ledger)
            source_mission = source_ledger["facets"]["mission"]
            checkpointed_candidate = source_ledger["facets"]["execution"].get(
                "candidate_head"
            )
            if source_ledger["phase"] != "active":
                raise AssuranceError(
                    "superseded run must remain active until continuity transfers",
                    code="SUPERSEDED_RUN_NOT_ACTIVE",
                    status="NEEDS_USER",
                )
            if (
                supersedes["revision"] != source_mission["revision"]
                or supersedes["mission_digest"] != source_ledger["digests"]["mission"]
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
            requested_candidate = supersedes["candidate_head"]
            if requested_candidate == checkpointed_candidate:
                source_candidate = checkpointed_candidate
            else:
                source_candidate = _rejected_checkpoint_candidate(
                    repo,
                    source_ledger,
                    requested_candidate,
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
                source_open_by_key = {
                    item["key"]: copy.deepcopy(item)
                    for item in source_ledger.get("problems", [])
                    if isinstance(item, Mapping) and item.get("status") == "open"
                }
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
                        inherited = source_open_by_key.get(key)
                        if not isinstance(inherited, dict):
                            raise AssuranceError(
                                "included source problem is not present in the source ledger",
                                code="PRIOR_PROBLEM_SOURCE_MISSING",
                                status="NEEDS_USER",
                                details={"key": key},
                            )
                        inherited_problems.append(inherited)
                revision_transitions.append(
                    _recorded_transition(
                        transition,
                        source_run=source_ledger["run_id"],
                        target_run=run_id,
                        from_revision=source_mission["revision"],
                        to_revision=contract["mission"]["revision"],
                        source_lineage=source_lineage,
                        mission_semantics_digest=digest(
                            _mission_semantics_value(source_mission)
                        ),
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
        cost_source_lineage = _validate_cost_ancestry_start(repo, run_id, contract)
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
            execution["candidate_head"] = (
                None if cost_source_lineage is not None else candidate_head
            )
            execution["dirty_snapshot"] = snapshots
            retired_tester_sources: list[dict[str, Any]] = []
            if source_ledger is not None:
                source_execution = source_ledger["facets"]["execution"]
                execution["builder_files"] = []
                execution["tester_files"] = []
                execution["tester_source"] = None
                execution["carryover"] = {
                    "source_run_id": source_ledger["run_id"],
                    "source_candidate_head": candidate_head,
                    "files": _candidate_manifest(repo, target_head, candidate_head),
                }
                if isinstance(source_execution.get("tester_source"), dict):
                    retired_tester_sources.append(copy.deepcopy(source_execution["tester_source"]))
            if snapshots and source_ledger is None:
                execution["version"] += 1
                execution["builder_files"] = sorted(set(execution["builder_files"]) | set(captured))
            contract["execution"] = execution
            created_at = now()
            from ..core import capture_runtime_identity

            ledger = {
                "schema_version": SCHEMA_VERSION,
                "runtime_identity": capture_runtime_identity(),
                "runtime_support": runtime_support,
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
                "doc_reference_contract_version": DOC_REFERENCE_CONTRACT_VERSION,
                "doc_reference_scan": None,
                "retired_tester_sources": retired_tester_sources,
                "retired_reviewer_agents": [],
                "driver_runtime": copy.deepcopy(driver_runtime),
                "driver_failure": None,
                "dispatch_intent": None,
                "transport_cleanup_intent": None,
                "deferred_wait": None,
                "tester_replacement_intent": None,
                "proof_failure": None,
                "machine_failure": None,
                "recomposition_intent": None,
                "candidate_residue_intent": None,
                "deployment_transaction": None,
                "pending_blackbox": None,
                "environment_lease": None,
                "supersede_intent": None,
                "abandon_intent": None,
                "problems": [
                    copy.deepcopy(problem) for problem in inherited_problems
                ],
                "revision_transitions": revision_transitions,
                "problem_dispositions": problem_dispositions,
                "publication": {
                    "required": bool(contract["authority"].get("public_prerequisites")),
                    "generation": 0,
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
                {
                    "target_head": target_head,
                    "candidate_head": execution["candidate_head"],
                    "candidate_worktree_head": candidate_head,
                    "dirty_intake": captured,
                    "runtime_support": copy.deepcopy(runtime_support),
                    "cost_ancestry": copy.deepcopy(
                        execution.get("cost_ancestry")
                    ),
                },
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
            if business_persisted or ledger_path(repo, run_id).exists():
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
    thread_id: str | None = None,
    *,
    owner_mode: str = "native_thread",
    session_id: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        if owner_mode != _builder_mode(ledger):
            raise AssuranceError(
                "requested Builder owner mode does not match the frozen contract",
                code="BUILDER_OWNER_MODE_MISMATCH",
                status="FAIL",
                details={
                    "contract_mode": _builder_mode(ledger),
                    "requested_mode": owner_mode,
                },
            )
        if owner_mode == "root_session":
            normalized_session = (session_id or "").strip()
            if not normalized_session:
                raise AssuranceError(
                    "root Builder session identity is required",
                    code="BUILDER_OWNER_SESSION_REQUIRED",
                    status="FAIL",
                )
            normalized_agent = agent_id.strip() or (
                ROOT_SESSION_AGENT_PREFIX + normalized_session
            )
            agent = {
                "mode": "root_session",
                "agent_id": normalized_agent,
                "session_id": normalized_session,
            }
        elif owner_mode == "native_thread":
            normalized_thread = (thread_id or "").strip()
            normalized_agent = agent_id.strip()
            if not normalized_agent or not normalized_thread:
                raise AssuranceError(
                    "Native Builder identity is required",
                    code="BUILDER_IDENTITY_REQUIRED",
                    status="FAIL",
                )
            agent = {
                "agent_id": normalized_agent,
                "thread_id": normalized_thread,
            }
        else:
            raise AssuranceError(
                "Builder owner mode is invalid",
                code="BUILDER_OWNER_MODE_INVALID",
                status="FAIL",
                details={"owner_mode": owner_mode},
            )
        existing = ledger["facets"]["execution"]["agents"].get("builder")
        if (
            isinstance(existing, Mapping)
            and _builder_owner_mode(existing) == owner_mode
            and (
                (
                    owner_mode == "root_session"
                    and existing.get("session_id") == agent.get("session_id")
                    and existing.get("agent_id") == agent.get("agent_id")
                )
                or (
                    owner_mode == "native_thread"
                    and existing.get("thread_id") == agent.get("thread_id")
                    and existing.get("agent_id") == agent.get("agent_id")
                )
            )
        ):
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
    thread_id: str | None,
    prompt_digest: str,
    output_schema_digest: str,
    owner_session_id: str | None = None,
    native_transport_generation: str | None = None,
    timeout_profile_digest: str | None = None,
    driver_runtime_kind: str = "native",
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    if role not in {"builder", "tester", "reviewer"}:
        raise AssuranceError("dispatch role is invalid", code="DISPATCH_ROLE_INVALID")
    if driver_runtime_kind not in {"native", "full_driver_skill"}:
        raise AssuranceError(
            "dispatch driver runtime kind is invalid",
            code="DRIVER_RUNTIME_KIND_INVALID",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        if driver_runtime_kind == "full_driver_skill" and (
            native_transport_generation is not None
            or timeout_profile_digest is not None
        ):
            raise AssuranceError(
                "Full Driver dispatch cannot bind Native transport facts",
                code="FULL_DRIVER_NATIVE_TRANSPORT_INVALID",
            )
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
        owner_mode = "native_thread"
        if role == "builder":
            owner = _assert_builder_owner(
                ledger,
                owner_session_id=owner_session_id,
                owner_thread_id=thread_id,
            )
            owner_mode = _builder_owner_mode(owner)
            if owner_mode == "root_session":
                thread_id = None
            elif _builder_owner_thread(owner) != thread_id:
                raise AssuranceError(
                    "dispatch thread identity mismatch",
                    code="DISPATCH_IDENTITY_MISMATCH",
                )
        elif not isinstance(agent, dict) or agent.get("thread_id") != thread_id:
            raise AssuranceError(
                "dispatch thread identity mismatch",
                code="DISPATCH_IDENTITY_MISMATCH",
            )
        authorization = current.get("tester_correction_authorization")
        if action == "tester_fix" and current.get("reason") == "tester_correction_authorized":
            if not isinstance(authorization, Mapping):
                raise AssuranceError(
                    "authorized Tester correction lost its decision binding",
                    code="TESTER_CORRECTION_AUTHORIZATION_MISSING",
                    status="FAIL",
                )
            expected_runtime_identity = authorization.get("runtime_identity")
            from ..core import capture_runtime_identity

            current_runtime_identity = capture_runtime_identity()
            if (
                expected_runtime_identity != ledger.get("runtime_identity")
                or current_runtime_identity != expected_runtime_identity
            ):
                raise AssuranceError(
                    "authorized Tester correction runtime identity changed before dispatch",
                    code="TESTER_CORRECTION_AUTHORIZATION_RUNTIME_MISMATCH",
                    status="NEEDS_USER",
                    details={
                        "authorized_runtime_identity": copy.deepcopy(
                            expected_runtime_identity
                        ),
                        "ledger_runtime_identity": copy.deepcopy(
                            ledger.get("runtime_identity")
                        ),
                        "actual_runtime_identity": current_runtime_identity,
                    },
                )
            append_event(
                ledger,
                "tester_correction_authorization_consumed",
                {
                    "authorization_id": authorization["authorization_id"],
                    "review_binding": authorization["review_binding"],
                    "tester_fix_action_id": action_id,
                },
            )
        observation = {
            "candidate_head": ledger["facets"]["execution"].get("candidate_head"),
            "target_start_head": ledger.get("target_start_head"),
            "evidence": ledger.get("evidence", {}),
            "publication": ledger.get("publication"),
            "deployment_transaction": ledger.get("deployment_transaction"),
        }
        candidate_manifest_digest: str | None = None
        if action in BUILDER_SIDE_EFFECT_ACTIONS:
            try:
                candidate_manifest_digest = _candidate_worktree_manifest(
                    Path(str(ledger["candidate_worktree"]))
                )["manifest_digest"]
            except (OSError, StoreError) as exc:
                raise AssuranceError(
                    "Builder dispatch requires a readable candidate manifest",
                    code="DISPATCH_CANDIDATE_OBSERVATION_UNAVAILABLE",
                    status="NEEDS_USER",
                    details={"error": str(exc)},
                ) from exc
        dispatch_intent = {
            "action_id": action_id,
            "action": action,
            "role": role,
            "thread_id": thread_id,
            "prompt_digest": prompt_digest,
            "output_schema_digest": output_schema_digest,
            "dispatch_observation_digest": digest(observation),
            "state": "in_flight" if owner_mode == "root_session" else "prepared",
            "attempt": 1,
            "generation": 1,
            "continuity_state": (
                "in_flight" if owner_mode == "root_session" else "prepared"
            ),
            "interrupted_retry_used": False,
            "interrupted_retry_blocked": False,
            "created_at": now(),
        }
        if role == "builder" and owner_mode == "root_session":
            dispatch_intent["owner_mode"] = owner_mode
            dispatch_intent["owner_session_id"] = owner_session_id
        if candidate_manifest_digest is not None:
            dispatch_intent["candidate_manifest_digest"] = candidate_manifest_digest
        if native_transport_generation:
            dispatch_intent["native_transport_generation"] = native_transport_generation
            dispatch_intent["last_wire_sequence"] = 0
            dispatch_intent["last_wire_event_digest"] = digest(
                {"generation": native_transport_generation, "sequence": 0}
            )
        if timeout_profile_digest:
            dispatch_intent["timeout_profile_digest"] = timeout_profile_digest
        dispatch_intent["side_effect_observation_digest"] = digest(observation)
        ledger["dispatch_intent"] = dispatch_intent
        append_event(ledger, "dispatch_prepared", copy.deepcopy(ledger["dispatch_intent"]))
        save_ledger(repo, ledger)
    return status(repo, run_id)


def authorize_tester_correction(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    review_binding: str,
    reason: str,
    driver_runtime_kind: str,
    allow_runtime_transition: bool = False,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise AssuranceError(
            "Tester correction authorization requires a non-empty reason",
            code="TESTER_CORRECTION_AUTHORIZATION_REASON_REQUIRED",
            status="NEEDS_USER",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        if ledger["phase"] != "active":
            raise AssuranceError(
                "Tester correction authorization requires an active run",
                code="ASSURANCE_RUN_NOT_ACTIVE",
                status="NEEDS_USER",
            )
        matching = [
            event.get("details")
            for event in ledger.get("events", [])
            if isinstance(event, Mapping)
            and event.get("kind") == "tester_correction_authorized"
            and isinstance(event.get("details"), Mapping)
            and event["details"].get("architecture_action_id") == action_id
            and event["details"].get("review_binding") == review_binding
        ]
        if matching:
            if len(matching) != 1 or matching[0].get("reason") != normalized_reason:
                raise AssuranceError(
                    "Tester correction authorization replay conflicts with the recorded decision",
                    code="TESTER_CORRECTION_AUTHORIZATION_CONFLICT",
                    status="NEEDS_USER",
                )
            return status(repo, run_id)

        from .driver import next_action

        current = next_action(repo, run_id, _ledger=ledger)
        if (
            current.get("status") != "NEEDS_USER"
            or current.get("action") != "architecture_review"
            or current.get("reason") != "tester_correction_limit_reached"
            or current.get("action_id") != action_id
            or current.get("tester_correction_review_binding") != review_binding
        ):
            raise AssuranceError(
                "Tester correction architecture review decision is stale",
                code="TESTER_CORRECTION_REVIEW_STALE",
                status="NEEDS_USER",
                details={"current": current},
            )

        from ..core import capture_runtime_identity

        current_runtime_identity = capture_runtime_identity()
        previous_runtime_identity = copy.deepcopy(ledger["runtime_identity"])
        if current_runtime_identity != previous_runtime_identity:
            if not allow_runtime_transition:
                raise AssuranceError(
                    "Tester correction authorization cannot change the frozen runtime identity",
                    code="TESTER_CORRECTION_RUNTIME_IDENTITY_MISMATCH",
                    status="NEEDS_USER",
                    details={
                        "expected_runtime_identity": previous_runtime_identity,
                        "actual_runtime_identity": current_runtime_identity,
                    },
                )
            if (
                current_runtime_identity.get("capture_status") != "captured"
                or current_runtime_identity.get("adapter_dirty") is not False
            ):
                raise AssuranceError(
                    "Tester correction runtime transition requires a captured clean runtime",
                    code="TESTER_CORRECTION_RUNTIME_TRANSITION_UNSAFE",
                    status="NEEDS_USER",
                    details={"actual_runtime_identity": current_runtime_identity},
                )
            current_runtime_support, _manifest, required_independent = (
                _runtime_support_snapshot(repo, copy.deepcopy(ledger["facets"]))
            )
            _assert_runtime_support_contract(
                ledger["facets"], current_runtime_support, required_independent
            )
            previous_support = copy.deepcopy(ledger["runtime_support"])
            comparable_previous = {
                key: value
                for key, value in previous_support.items()
                if key != "runtime_head"
            }
            comparable_current = {
                key: value
                for key, value in current_runtime_support.items()
                if key != "runtime_head"
            }
            if comparable_current != comparable_previous:
                raise AssuranceError(
                    "Tester correction runtime support changed beyond the approved runtime commit",
                    code="TESTER_CORRECTION_RUNTIME_SUPPORT_DRIFT",
                    status="NEEDS_USER",
                    details={
                        "expected_runtime_support": previous_support,
                        "actual_runtime_support": current_runtime_support,
                    },
                )
            ledger["runtime_identity"] = current_runtime_identity
            ledger["runtime_support"] = current_runtime_support
            append_event(
                ledger,
                "runtime_identity_transitioned",
                {
                    "reason": normalized_reason,
                    "architecture_action_id": action_id,
                    "review_binding": review_binding,
                    "previous_runtime_identity": previous_runtime_identity,
                    "current_runtime_identity": copy.deepcopy(current_runtime_identity),
                    "previous_runtime_support": previous_support,
                    "current_runtime_support": copy.deepcopy(current_runtime_support),
                },
            )

        authorization_id = digest(
            {
                "run_id": run_id,
                "architecture_action_id": action_id,
                "review_binding": review_binding,
                "reason": normalized_reason,
                "runtime_identity": ledger["runtime_identity"],
            }
        )
        append_event(
            ledger,
            "tester_correction_authorized",
            {
                "authorization_id": authorization_id,
                "architecture_action_id": action_id,
                "review_binding": review_binding,
                "reason": normalized_reason,
                "runtime_identity": copy.deepcopy(ledger["runtime_identity"]),
            },
        )
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
        _assert_no_candidate_residue_intent(ledger)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if intent.get("turn_id") not in {None, turn_id.strip()}:
            raise AssuranceError("dispatch turn identity changed", code="DISPATCH_TURN_MISMATCH")
        intent["turn_id"] = turn_id.strip()
        intent["state"] = "in_flight"
        intent["continuity_state"] = "in_flight"
        replacement = ledger.get("tester_replacement_intent")
        if (
            intent.get("action") == "tester_author"
            and intent.get("role") == "tester"
            and isinstance(replacement, dict)
            and replacement.get("stage") == "awaiting_first_turn"
            and intent.get("thread_id")
            == replacement.get("new_agent", {}).get("thread_id")
        ):
            candidate = _assert_tester_replacement_candidate(repo, ledger)
            snapshot_digest = _tester_replacement_problem_snapshot(ledger)
            if snapshot_digest != replacement.get("problem_snapshot_digest"):
                raise AssuranceError(
                    "Tester replacement problem snapshot drifted before the first turn",
                    code="TESTER_REPLACEMENT_PROBLEM_DRIFT",
                    status="FAIL",
                )
            execution = ledger["facets"]["execution"]
            source = execution.get("tester_source")
            if (
                candidate != replacement.get("candidate_head")
                or ledger.get("target_start_head")
                != replacement.get("target_start_head")
                or execution.get("agents", {}).get("tester")
                != replacement.get("new_agent")
                or not isinstance(source, dict)
                or source.get("agent") != replacement.get("new_agent")
                or source.get("head") != replacement.get("source_base_head")
                or source.get("base_head") != replacement.get("source_base_head")
                or source.get("branch") != replacement.get("branch")
                or source.get("worktree") != replacement.get("worktree")
                or source.get("files") != []
            ):
                raise AssuranceError(
                    "Tester replacement source drifted before the first turn",
                    code="TESTER_REPLACEMENT_EXECUTION_DRIFT",
                    status="FAIL",
                )
            _assert_tester_source_exact(repo, source)
            problem = _tester_replacement_problem(
                ledger, str(replacement["problem_key"])
            )
            if problem.get("producer") != {
                "role": "tester",
                **dict(replacement["old_agent"]),
            }:
                raise AssuranceError(
                    "Tester replacement problem producer changed",
                    code="TESTER_REPLACEMENT_PRODUCER_MISMATCH",
                    status="FAIL",
                )
            problem["status"] = "resolved"
            problem["resolution"] = (
                f"tester-replacement:{replacement['new_agent']['thread_id']}"
            )
            problem["resolved_at"] = now()
            append_event(
                ledger,
                "tester_replacement_first_turn_bound",
                {
                    "action_id": replacement["action_id"],
                    "dispatch_action_id": action_id,
                    "problem_key": replacement["problem_key"],
                    "agent": copy.deepcopy(replacement["new_agent"]),
                    "turn_id": turn_id.strip(),
                },
            )
            ledger["tester_replacement_intent"] = None
        append_event(ledger, "dispatch_turn_bound", {"action_id": action_id, "turn_id": turn_id.strip()})
        save_ledger(repo, ledger)
    return status(repo, run_id)


def bind_dispatch_transport(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    native_transport_generation: str,
    timeout_profile_digest: str | None = None,
    driver_runtime_kind: str = "native",
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    generation = native_transport_generation.strip()
    if not generation:
        raise AssuranceError(
            "native transport generation is required",
            code="DISPATCH_TRANSPORT_GENERATION_REQUIRED",
            status="FAIL",
        )
    if timeout_profile_digest is not None and not re.fullmatch(
        r"[0-9a-f]{64}", timeout_profile_digest
    ):
        raise AssuranceError(
            "timeout profile digest is invalid",
            code="DISPATCH_TIMEOUT_PROFILE_INVALID",
            status="FAIL",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        _assert_no_candidate_residue_intent(ledger)
        cleanup = ledger.get("transport_cleanup_intent")
        if isinstance(cleanup, Mapping) and cleanup.get("state") not in {
            "completed",
            "unknown",
        }:
            raise AssuranceError(
                "transport cleanup is still pending",
                code="DISPATCH_TRANSPORT_CLEANUP_PENDING",
                status="NEEDS_USER",
            )
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError(
                "dispatch action is stale",
                code="DRIVER_ACTION_STALE",
                status="FAIL",
            )
        if intent.get("state") == "completed":
            raise AssuranceError(
                "completed dispatch transport cannot be rebound",
                code="DISPATCH_TRANSPORT_ALREADY_COMPLETE",
                status="FAIL",
            )
        previous = intent.get("native_transport_generation")
        previous_timeout = intent.get("timeout_profile_digest")
        if previous == generation and (
            timeout_profile_digest is None or previous_timeout == timeout_profile_digest
        ):
            return status(repo, run_id)
        intent["native_transport_generation"] = generation
        intent["last_wire_sequence"] = 0
        intent["last_wire_event_digest"] = digest(
            {"generation": generation, "sequence": 0}
        )
        if timeout_profile_digest is not None:
            intent["timeout_profile_digest"] = timeout_profile_digest
        intent["continuity_state"] = intent.get("state", "unknown")
        append_event(
            ledger,
            "dispatch_transport_bound",
            {
                "action_id": action_id,
                "thread_id": intent.get("thread_id"),
                "previous_generation": previous,
                "generation": generation,
                "timeout_profile_digest": timeout_profile_digest,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def bind_native_transport(
    repo_value: str | Path,
    run_value: str,
    *,
    native_transport: Mapping[str, Any],
    driver_runtime_kind: str = "native",
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    candidate = copy.deepcopy(dict(native_transport))
    if candidate.get("contract_version") != 2:
        raise AssuranceError(
            "native transport contract version is unsupported",
            code="NATIVE_TRANSPORT_CONTRACT_UNSUPPORTED",
            status="FAIL",
        )
    generation = candidate.get("generation")
    if not isinstance(generation, str) or not generation or generation == "unbound":
        raise AssuranceError(
            "native transport generation is required",
            code="NATIVE_TRANSPORT_GENERATION_REQUIRED",
            status="FAIL",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        runtime = ledger.get("driver_runtime")
        if not isinstance(runtime, dict):
            raise AssuranceError(
                "Native Driver runtime identity is missing",
                code="NATIVE_TRANSPORT_RUNTIME_MISSING",
                status="NEEDS_USER",
            )
        previous = runtime.get("native_transport")
        previous_generation = (
            previous.get("generation") if isinstance(previous, Mapping) else None
        )
        if previous_generation == generation:
            if previous != candidate:
                raise AssuranceError(
                    "native transport identity changed within one generation",
                    code="NATIVE_TRANSPORT_IDENTITY_DRIFT",
                    status="NEEDS_USER",
                )
            return status(repo, run_id)
        cleanup = ledger.get("transport_cleanup_intent")
        if isinstance(cleanup, Mapping) and cleanup.get("state") not in {
            "completed",
            "unknown",
        }:
            raise AssuranceError(
                "previous Native transport cleanup is still pending",
                code="NATIVE_TRANSPORT_PREVIOUS_CLEANUP_PENDING",
                status="NEEDS_USER",
                details={"generation": cleanup.get("source_generation")},
            )
        if isinstance(cleanup, Mapping) and cleanup.get("state") == "unknown":
            raise AssuranceError(
                "previous Native transport cleanup is unknown",
                code="NATIVE_TRANSPORT_PREVIOUS_CLEANUP_UNKNOWN",
                status="NEEDS_USER",
                details={"generation": cleanup.get("source_generation")},
            )
        runtime["native_transport"] = candidate
        # Terminal cleanup history remains in immutable events; this slot tracks
        # only the currently-owned transport cleanup intent.
        ledger["transport_cleanup_intent"] = None
        intent = ledger.get("dispatch_intent")
        if isinstance(intent, dict):
            intent["native_transport_generation"] = generation
            intent["last_wire_sequence"] = 0
            intent["last_wire_event_digest"] = digest(
                {"generation": generation, "sequence": 0}
            )
            intent["continuity_state"] = intent.get("state", "unknown")
        append_event(
            ledger,
            "native_transport_generation_bound",
            {
                "previous_generation": previous_generation,
                "generation": generation,
                "dispatch_action_id": (
                    intent.get("action_id") if isinstance(intent, dict) else None
                ),
                "process_identity": copy.deepcopy(candidate.get("process_identity")),
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def record_dispatch_wire(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    native_transport_generation: str,
    sequence: int,
    event_digest: str,
    driver_runtime_kind: str = "native",
) -> dict[str, Any]:
    if sequence < 0 or not re.fullmatch(r"[0-9a-f]{64}", event_digest):
        raise AssuranceError(
            "dispatch wire observation is invalid",
            code="DISPATCH_WIRE_OBSERVATION_INVALID",
            status="FAIL",
        )
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        _assert_no_candidate_residue_intent(ledger)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError(
                "dispatch action is stale",
                code="DRIVER_ACTION_STALE",
                status="FAIL",
            )
        expected_generation = intent.get("native_transport_generation")
        if expected_generation != native_transport_generation:
            raise AssuranceError(
                "dispatch wire transport generation changed",
                code="DISPATCH_WIRE_GENERATION_MISMATCH",
                status="NEEDS_USER",
                details={
                    "expected_generation": expected_generation,
                    "actual_generation": native_transport_generation,
                },
            )
        previous_sequence = int(intent.get("last_wire_sequence", 0))
        previous_digest = intent.get("last_wire_event_digest")
        if sequence < previous_sequence:
            raise AssuranceError(
                "dispatch wire sequence moved backwards",
                code="DISPATCH_WIRE_SEQUENCE_DRIFT",
                status="NEEDS_USER",
            )
        if sequence == previous_sequence:
            if previous_digest == event_digest:
                return status(repo, run_id)
            raise AssuranceError(
                "dispatch wire observation changed at the same sequence",
                code="DISPATCH_WIRE_OBSERVATION_DRIFT",
                status="NEEDS_USER",
            )
        intent["last_wire_sequence"] = sequence
        intent["last_wire_event_digest"] = event_digest
        append_event(
            ledger,
            "wire_observation_checkpointed",
            {
                "action_id": action_id,
                "thread_id": intent.get("thread_id"),
                "transport_generation": native_transport_generation,
                "sequence": sequence,
                "event_digest": event_digest,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def record_deferred_wait(
    repo_value: str | Path,
    run_value: str,
    *,
    state: str,
    action_id: str,
    dispatch_generation: int,
    transport_generation: str,
    delivery_state: str,
    last_heartbeat_at: str | None = None,
    terminal_at: str | None = None,
    driver_runtime_kind: str = "native",
) -> dict[str, Any]:
    if state not in {"idle", "active", "stalled", "terminal", "unknown"}:
        raise AssuranceError(
            "deferred wait state is invalid",
            code="DEFERRED_WAIT_STATE_INVALID",
            status="FAIL",
        )
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError(
                "deferred wait action is stale",
                code="DEFERRED_WAIT_ACTION_STALE",
                status="FAIL",
            )
        if int(intent.get("generation", 0)) != dispatch_generation:
            raise AssuranceError(
                "deferred wait dispatch generation is stale",
                code="DEFERRED_WAIT_DISPATCH_GENERATION_STALE",
                status="NEEDS_USER",
            )
        runtime = ledger.get("driver_runtime")
        native_transport = (
            runtime.get("native_transport")
            if isinstance(runtime, Mapping)
            else None
        )
        if not isinstance(native_transport, Mapping) or native_transport.get(
            "generation"
        ) != transport_generation:
            raise AssuranceError(
                "deferred wait transport generation is stale",
                code="DEFERRED_WAIT_TRANSPORT_GENERATION_STALE",
                status="NEEDS_USER",
            )
        observation = {
            "state": state,
            "action_id": action_id,
            "dispatch_generation": dispatch_generation,
            "transport_generation": transport_generation,
            "started_at": (
                ledger.get("deferred_wait", {}).get("started_at")
                if isinstance(ledger.get("deferred_wait"), Mapping)
                else now()
            ),
            "last_heartbeat_at": last_heartbeat_at,
            "terminal_at": terminal_at,
            "delivery_state": delivery_state,
        }
        observation["observation_digest"] = digest(observation)
        ledger["deferred_wait"] = observation
        append_event(
            ledger,
            "deferred_wait_" + ("heartbeat" if state == "active" else state),
            copy.deepcopy(observation),
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def record_transport_cleanup(
    repo_value: str | Path,
    run_value: str,
    *,
    generation: str,
    process_identity: Mapping[str, Any],
    state: str,
    term_attempt: int,
    kill_attempt: int,
    process_group_gone: bool,
    driver_runtime_kind: str = "native",
) -> dict[str, Any]:
    if state not in {"completed", "unknown"}:
        raise AssuranceError(
            "transport cleanup state is invalid",
            code="TRANSPORT_CLEANUP_STATE_INVALID",
            status="FAIL",
        )
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        runtime = ledger.get("driver_runtime")
        native_transport = (
            runtime.get("native_transport")
            if isinstance(runtime, Mapping)
            else None
        )
        if not isinstance(native_transport, Mapping):
            raise AssuranceError(
                "run has no Native transport identity",
                code="TRANSPORT_CLEANUP_IDENTITY_MISSING",
                status="NEEDS_USER",
            )
        if native_transport.get("generation") != generation:
            raise AssuranceError(
                "transport cleanup generation is stale",
                code="TRANSPORT_CLEANUP_GENERATION_MISMATCH",
                status="NEEDS_USER",
            )
        existing = ledger.get("transport_cleanup_intent")
        payload = {
            "source_generation": generation,
            "process_identity": copy.deepcopy(dict(process_identity)),
            "state": state,
            "term_attempt": term_attempt,
            "kill_attempt": kill_attempt,
            "process_group_gone": process_group_gone,
            "observation_digest": digest(
                {
                    "generation": generation,
                    "process_identity": process_identity,
                    "state": state,
                    "term_attempt": term_attempt,
                    "kill_attempt": kill_attempt,
                    "process_group_gone": process_group_gone,
                }
            ),
            "created_at": (
                existing.get("created_at")
                if isinstance(existing, Mapping)
                else now()
            ),
            "updated_at": now(),
        }
        if isinstance(existing, Mapping):
            comparable_keys = (
                "source_generation",
                "process_identity",
                "state",
                "term_attempt",
                "kill_attempt",
                "process_group_gone",
                "observation_digest",
                "created_at",
            )
            if all(existing.get(key) == payload.get(key) for key in comparable_keys):
                return status(repo, run_id)
            if existing.get("state") == "unknown" and state == "completed":
                payload["created_at"] = existing.get("created_at", payload["created_at"])
            elif existing.get("observation_digest") != payload["observation_digest"]:
                raise AssuranceError(
                    "transport cleanup observation changed",
                    code="TRANSPORT_CLEANUP_OBSERVATION_DRIFT",
                    status="NEEDS_USER",
                )
        native_transport = dict(native_transport)
        native_transport["state"] = (
            "cleaned" if state == "completed" else "cleanup_unknown"
        )
        native_transport["process_identity"] = copy.deepcopy(
            payload["process_identity"]
        )
        runtime["native_transport"] = native_transport
        ledger["transport_cleanup_intent"] = payload
        append_event(
            ledger,
            "transport_cleanup_completed"
            if state == "completed"
            else "transport_cleanup_unknown",
            {
                "generation": generation,
                "process_identity": copy.deepcopy(dict(process_identity)),
                "term_attempt": term_attempt,
                "kill_attempt": kill_attempt,
                "process_group_gone": process_group_gone,
                "observation_digest": payload["observation_digest"],
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def complete_dispatch(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    result_value: Any,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    result = validate_agent_result(result_value)
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if (
            intent.get("owner_mode") == "root_session"
            and intent.get("owner_session_id") != owner_session_id
        ):
            raise AssuranceError(
                "root Builder dispatch completion identity does not match",
                code="BUILDER_OWNER_SESSION_MISMATCH",
                status="FAIL",
                details={
                    "expected_session_id": intent.get("owner_session_id"),
                    "provided_session_id": owner_session_id,
                },
            )
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
        intent["continuity_state"] = "completed"
        intent["result_path"] = str(artifact)
        intent["result_digest"] = digest(result)
        intent["completed_at"] = now()
        append_event(
            ledger,
            "dispatch_completed",
            {
                "action_id": action_id,
                "turn_id": intent.get("turn_id"),
                "result": result["result"],
                "result_digest": intent["result_digest"],
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def _root_builder_dispatch_for_application(
    ledger: Mapping[str, Any],
    *,
    action_id: str | None,
    owner_session_id: str | None,
) -> dict[str, Any] | None:
    intent = ledger.get("dispatch_intent")
    if not (
        _builder_mode(ledger) == "root_session"
        and isinstance(intent, Mapping)
        and intent.get("role") == "builder"
        and intent.get("owner_mode") == "root_session"
    ):
        return None
    _assert_builder_owner(ledger, owner_session_id=owner_session_id)
    if action_id is None or intent.get("action_id") != action_id:
        raise AssuranceError(
            "root Builder result application action is stale",
            code="DRIVER_ACTION_STALE",
            status="FAIL",
        )
    if intent.get("state") != "completed":
        raise AssuranceError(
            "root Builder result must complete before its side effects are applied",
            code="ROOT_BUILDER_DISPATCH_NOT_COMPLETE",
            status="FAIL",
        )
    return intent if isinstance(intent, dict) else dict(intent)


def _mark_root_builder_result_applied(
    ledger: dict[str, Any],
    intent: dict[str, Any] | None,
    application: str,
) -> bool:
    if intent is None:
        return False
    existing = intent.get("result_application")
    if existing is not None and existing != application:
        raise AssuranceError(
            "root Builder result was already applied through another transaction",
            code="ROOT_BUILDER_RESULT_APPLICATION_MISMATCH",
            status="FAIL",
            details={"expected": existing, "provided": application},
        )
    if existing == application:
        return False
    intent["result_application"] = application
    append_event(
        ledger,
        "root_builder_result_applied",
        {
            "action_id": intent["action_id"],
            "action": intent["action"],
            "application": application,
            "result_digest": intent.get("result_digest"),
        },
    )
    return True


def _expected_root_builder_result_application(intent: Mapping[str, Any]) -> str:
    result_path = intent.get("result_path")
    if not isinstance(result_path, str):
        raise AssuranceError(
            "root Builder result artifact is missing",
            code="ROOT_BUILDER_RESULT_ARTIFACT_MISSING",
            status="NEEDS_USER",
        )
    try:
        result = json.loads(Path(result_path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AssuranceError(
            "root Builder result artifact is unreadable",
            code="ROOT_BUILDER_RESULT_ARTIFACT_INVALID",
            status="NEEDS_USER",
            details={"path": result_path, "error": str(exc)},
        ) from exc
    if digest(result) != intent.get("result_digest"):
        raise AssuranceError(
            "root Builder result artifact digest changed",
            code="ROOT_BUILDER_RESULT_ARTIFACT_DRIFT",
            status="NEEDS_USER",
        )
    if isinstance(result.get("problem_report"), Mapping):
        return "record_problems"
    if intent.get("action") == "builder_recompose_fix":
        return "recompose_candidate"
    return "checkpoint_builder"


def retry_dispatch(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    failure_code: str,
    interrupted_retry: bool = False,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    normalized_failure_code = failure_code.strip()
    if not normalized_failure_code:
        raise AssuranceError(
            "dispatch retry failure code is required",
            code="DISPATCH_RETRY_FAILURE_CODE_REQUIRED",
            status="FAIL",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if intent.get("state") == "completed":
            raise AssuranceError("completed dispatch cannot be retried", code="DISPATCH_ALREADY_COMPLETE")
        if (
            intent.get("role") == "builder"
            and intent.get("owner_mode") == "root_session"
        ):
            raise AssuranceError(
                "root Builder dispatch requires explicit session continuation",
                code="NATIVE_ROOT_BUILDER_RETRY_FORBIDDEN",
                status="NEEDS_USER",
            )
        baseline_manifest_digest = intent.get("candidate_manifest_digest")
        if (
            intent.get("action") in BUILDER_SIDE_EFFECT_ACTIONS
            and isinstance(baseline_manifest_digest, str)
        ):
            current_manifest = _candidate_manifest_for_ledger(ledger)
            if current_manifest is None:
                raise AssuranceError(
                    "Builder retry cannot prove the current candidate manifest",
                    code="NATIVE_BUILDER_SIDE_EFFECT_RETRY_BLOCKED",
                    status="FATAL",
                    details={
                        "baseline_manifest_digest": baseline_manifest_digest,
                        "candidate_manifest": None,
                    },
                )
            if current_manifest["manifest_digest"] != baseline_manifest_digest:
                raise AssuranceError(
                    "Builder retry is blocked after a candidate side effect",
                    code="NATIVE_BUILDER_SIDE_EFFECT_RETRY_BLOCKED",
                    status="FATAL",
                    details={
                        "baseline_manifest_digest": baseline_manifest_digest,
                        "candidate_manifest": current_manifest,
                    },
                )
        if interrupted_retry:
            if intent.get("state") != "in_flight":
                raise AssuranceError(
                    "interrupted retry requires an in-flight dispatch",
                    code="DISPATCH_INTERRUPTED_RETRY_STATE_INVALID",
                    status="NEEDS_USER",
                )
            if intent.get("interrupted_retry_used"):
                intent["interrupted_retry_blocked"] = True
                append_event(
                    ledger,
                    "dispatch_interrupted_retry_rejected",
                    {
                        "action_id": action_id,
                        "generation": intent.get("generation"),
                        "attempt": intent.get("attempt"),
                        "turn_id": intent.get("turn_id"),
                        "reason": "interrupted_retry_already_used",
                    },
                )
                save_ledger(repo, ledger)
                raise AssuranceError(
                    "interrupted dispatch retry has already been consumed",
                    code="NATIVE_DISPATCH_INTERRUPTED_REQUIRES_USER",
                    status="NEEDS_USER",
                )
            intent["interrupted_retry_used"] = True
            append_event(
                ledger,
                "dispatch_interrupted_retry",
                {
                    "action_id": action_id,
                    "generation": intent.get("generation"),
                    "previous_attempt": intent.get("attempt"),
                    "next_attempt": int(intent.get("attempt", 1)) + 1,
                    "thread_id": intent.get("thread_id"),
                    "turn_id": intent.get("turn_id"),
                    "prompt_digest": intent.get("prompt_digest"),
                    "output_schema_digest": intent.get("output_schema_digest"),
                    "observation_digest": intent.get("dispatch_observation_digest"),
                    "reason": "no_output_no_side_effect",
                },
            )
        attempt = int(intent.get("attempt", 1))
        generation = int(intent.get("generation", 1))
        if attempt >= 3:
            deployment = ledger.get("deployment_transaction")
            if isinstance(deployment, dict) and deployment.get("state") == "deployed":
                deployment["state"] = "restore_required"
                deployment["failure_code"] = "NATIVE_DISPATCH_RETRY_EXHAUSTED"
                append_event(
                    ledger,
                    "dispatch_retry_exhausted_restore_required",
                    {
                        "action_id": action_id,
                        "failure_code": normalized_failure_code,
                        "attempt": attempt,
                        "generation": generation,
                        "turn_id": intent.get("turn_id"),
                    },
                )
                ledger["dispatch_intent"] = None
                save_ledger(repo, ledger)
                return status(repo, run_id)
            if intent.get("state") != "exhausted":
                intent["state"] = "exhausted"
                intent["continuity_state"] = "exhausted"
                intent["failure_code"] = normalized_failure_code
                intent["exhausted_at"] = now()
                append_event(
                    ledger,
                    "dispatch_retry_exhausted",
                    {
                        "action_id": action_id,
                        "attempt": attempt,
                        "generation": generation,
                        "turn_id": intent.get("turn_id"),
                        "failure_code": normalized_failure_code,
                    },
                )
                save_ledger(repo, ledger)
            raise AssuranceError(
                "Native role transport failed three times",
                code="NATIVE_DISPATCH_RETRY_EXHAUSTED",
                status="NEEDS_USER",
                details={
                    "failure_code": intent.get("failure_code", normalized_failure_code),
                    "attempt": attempt,
                    "generation": generation,
                },
            )
        scheduled_at = now()
        next_attempt = attempt + 1
        retry_delay = 0
        if normalized_failure_code == "authUnavailable":
            retry_delay = min(
                AUTH_UNAVAILABLE_RETRY_BASE_SECONDS * (2 ** (next_attempt - 2)),
                AUTH_UNAVAILABLE_RETRY_MAX_SECONDS,
            )
        retry_not_before = None
        if retry_delay:
            retry_not_before = (
                datetime.fromisoformat(scheduled_at) + timedelta(seconds=retry_delay)
            ).isoformat()
        append_event(
            ledger,
            "dispatch_retry_scheduled",
            {
                "action_id": action_id,
                "attempt": attempt,
                "next_attempt": next_attempt,
                "generation": generation,
                "turn_id": intent.get("turn_id"),
                "failure_code": normalized_failure_code,
                "retry_scheduled_at": scheduled_at,
                "retry_not_before": retry_not_before,
            },
        )
        intent["attempt"] = next_attempt
        intent["state"] = "prepared"
        intent["continuity_state"] = "prepared"
        intent["failure_code"] = normalized_failure_code
        intent["retry_scheduled_at"] = scheduled_at
        if retry_not_before is None:
            intent.pop("retry_not_before", None)
        else:
            intent["retry_not_before"] = retry_not_before
        intent.pop("turn_id", None)
        save_ledger(repo, ledger)
    return status(repo, run_id)


def renew_dispatch(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    reason: str,
    driver_runtime_kind: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise AssuranceError(
            "dispatch renewal requires a non-empty reason",
            code="DISPATCH_RENEWAL_REASON_REQUIRED",
            status="NEEDS_USER",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        if ledger["phase"] != "active":
            raise AssuranceError(
                "dispatch renewal requires an active run",
                code="ASSURANCE_RUN_NOT_ACTIVE",
                status="NEEDS_USER",
            )
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if (
            intent.get("role") == "builder"
            and intent.get("owner_mode") == "root_session"
        ):
            raise AssuranceError(
                "root Builder dispatch cannot renew a Native generation",
                code="NATIVE_ROOT_BUILDER_RENEWAL_FORBIDDEN",
                status="NEEDS_USER",
            )
        if intent.get("state") != "exhausted" or int(intent.get("attempt", 1)) < 3:
            raise AssuranceError(
                "only an exhausted dispatch can start a new generation",
                code="DISPATCH_RENEWAL_NOT_AVAILABLE",
                status="NEEDS_USER",
            )
        recovery = intent.get("compaction_recovery")
        if isinstance(recovery, Mapping) and recovery.get("state") == "prepared":
            raise AssuranceError(
                "dispatch compaction recovery must finish before manual renewal",
                code="DISPATCH_COMPACTION_PENDING",
                status="NEEDS_USER",
            )
        from ..core import capture_runtime_identity

        current_runtime_identity = capture_runtime_identity()
        if current_runtime_identity != ledger["runtime_identity"]:
            raise AssuranceError(
                "dispatch renewal cannot change the frozen runtime identity",
                code="DISPATCH_RUNTIME_IDENTITY_MISMATCH",
                status="NEEDS_USER",
                details={
                    "expected_runtime_identity": copy.deepcopy(ledger["runtime_identity"]),
                    "actual_runtime_identity": current_runtime_identity,
                },
            )
        agent = ledger["facets"]["execution"]["agents"].get(intent["role"])
        if not isinstance(agent, dict) or agent.get("thread_id") != intent.get("thread_id"):
            raise AssuranceError(
                "dispatch renewal lost role thread continuity",
                code="DISPATCH_RENEWAL_IDENTITY_MISMATCH",
                status="NEEDS_USER",
            )
        previous_intent = copy.deepcopy(intent)
        previous_digest = digest(previous_intent)
        previous_generation = int(previous_intent.get("generation", 1))
        renewed_intent = {
            "action_id": previous_intent["action_id"],
            "action": previous_intent["action"],
            "role": previous_intent["role"],
            "thread_id": previous_intent["thread_id"],
            "prompt_digest": previous_intent["prompt_digest"],
            "output_schema_digest": previous_intent["output_schema_digest"],
            "dispatch_observation_digest": previous_intent.get(
                "dispatch_observation_digest"
            ),
            "candidate_manifest_digest": previous_intent.get(
                "candidate_manifest_digest"
            ),
            "native_transport_generation": previous_intent.get(
                "native_transport_generation"
            ),
            "timeout_profile_digest": previous_intent.get("timeout_profile_digest"),
            "last_wire_sequence": previous_intent.get("last_wire_sequence", 0),
            "last_wire_event_digest": previous_intent.get("last_wire_event_digest"),
            "side_effect_observation_digest": previous_intent.get(
                "side_effect_observation_digest"
            ),
            "state": "prepared",
            "continuity_state": "prepared",
            "attempt": 1,
            "generation": previous_generation + 1,
            "interrupted_retry_used": False,
            "interrupted_retry_blocked": False,
            "renewed_from_digest": previous_digest,
            "renewal_reason": normalized_reason,
            "created_at": now(),
        }
        if isinstance(previous_intent.get("compaction_recovery"), Mapping):
            renewed_intent["compaction_recovery"] = copy.deepcopy(
                previous_intent["compaction_recovery"]
            )
        for key in (
            "native_transport_generation",
            "timeout_profile_digest",
            "last_wire_event_digest",
            "side_effect_observation_digest",
            "candidate_manifest_digest",
        ):
            if renewed_intent.get(key) is None:
                renewed_intent.pop(key, None)
        ledger["dispatch_intent"] = renewed_intent
        append_event(
            ledger,
            "dispatch_renewed",
            {
                "action_id": action_id,
                "role": previous_intent["role"],
                "thread_id": previous_intent["thread_id"],
                "previous_generation": previous_generation,
                "generation": renewed_intent["generation"],
                "previous_attempt": int(previous_intent.get("attempt", 1)),
                "previous_turn_id": previous_intent.get("turn_id"),
                "failure_code": previous_intent.get("failure_code"),
                "previous_digest": previous_digest,
                "reason": normalized_reason,
            },
        )
        append_event(ledger, "dispatch_prepared", copy.deepcopy(renewed_intent))
        save_ledger(repo, ledger)
    return status(repo, run_id)


def prepare_dispatch_compaction(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    prior_turn_count: int,
    prior_tail_turn_id: str,
    prior_turns_digest: str,
    driver_runtime_kind: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    if prior_turn_count < 1:
        raise AssuranceError(
            "dispatch compaction requires a non-empty thread observation",
            code="DISPATCH_COMPACTION_THREAD_EMPTY",
            status="FAIL",
        )
    if not prior_tail_turn_id.strip() or not re.fullmatch(
        r"[0-9a-f]{64}", prior_turns_digest
    ):
        raise AssuranceError(
            "dispatch compaction observation identity is invalid",
            code="DISPATCH_COMPACTION_OBSERVATION_INVALID",
            status="FAIL",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        if ledger["phase"] != "active":
            raise AssuranceError(
                "dispatch compaction requires an active run",
                code="ASSURANCE_RUN_NOT_ACTIVE",
                status="NEEDS_USER",
            )
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError(
                "dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL"
            )
        if (
            intent.get("state") != "exhausted"
            or int(intent.get("attempt", 1)) < 3
            or intent.get("role") != "reviewer"
            or intent.get("action") not in {"reviewer_preflight", "reviewer_final"}
            or intent.get("failure_code") not in REVIEWER_COMPACTION_FAILURE_CODES
        ):
            raise AssuranceError(
                "dispatch is not eligible for Reviewer thread compaction",
                code="DISPATCH_COMPACTION_NOT_AVAILABLE",
                status="NEEDS_USER",
            )
        if intent.get("turn_id") != prior_tail_turn_id:
            raise AssuranceError(
                "dispatch compaction tail does not match the exhausted turn",
                code="DISPATCH_COMPACTION_TAIL_MISMATCH",
                status="NEEDS_USER",
            )
        expected = {
            "state": "prepared",
            "source_generation": int(intent.get("generation", 1)),
            "source_attempt": int(intent.get("attempt", 1)),
            "failure_code": intent["failure_code"],
            "thread_id": intent["thread_id"],
            "prompt_digest": intent["prompt_digest"],
            "prior_turn_count": prior_turn_count,
            "prior_tail_turn_id": prior_tail_turn_id,
            "prior_turns_digest": prior_turns_digest,
        }
        existing = intent.get("compaction_recovery")
        if isinstance(existing, Mapping):
            if all(existing.get(key) == value for key, value in expected.items()):
                return status(repo, run_id)
            raise AssuranceError(
                "dispatch compaction observation changed",
                code="DISPATCH_COMPACTION_OBSERVATION_DRIFT",
                status="NEEDS_USER",
            )
        recovery = {**expected, "prepared_at": now()}
        intent["compaction_recovery"] = recovery
        append_event(
            ledger,
            "dispatch_compaction_prepared",
            {
                "action_id": action_id,
                "thread_id": intent["thread_id"],
                "generation": recovery["source_generation"],
                "attempt": recovery["source_attempt"],
                "failure_code": recovery["failure_code"],
                "prior_turn_count": prior_turn_count,
                "prior_tail_turn_id": prior_tail_turn_id,
                "prior_turns_digest": prior_turns_digest,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def complete_dispatch_compaction(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    compaction_turn_id: str,
    context_item_id: str,
    compaction_turn_digest: str,
    compaction_duration_ms: int,
    observed_turn_count: int,
    driver_runtime_kind: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    if (
        not compaction_turn_id.strip()
        or not context_item_id.strip()
        or not re.fullmatch(r"[0-9a-f]{64}", compaction_turn_digest)
        or compaction_duration_ms < 0
    ):
        raise AssuranceError(
            "completed dispatch compaction observation is invalid",
            code="DISPATCH_COMPACTION_RESULT_INVALID",
            status="FAIL",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError(
                "dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL"
            )
        recovery = intent.get("compaction_recovery")
        if not isinstance(recovery, dict):
            raise AssuranceError(
                "dispatch compaction was not prepared",
                code="DISPATCH_COMPACTION_NOT_PREPARED",
                status="FAIL",
            )
        if recovery.get("state") == "completed":
            if (
                recovery.get("compaction_turn_id") == compaction_turn_id
                and recovery.get("context_item_id") == context_item_id
                and recovery.get("compaction_turn_digest") == compaction_turn_digest
                and recovery.get("compaction_duration_ms") == compaction_duration_ms
                and recovery.get("observed_turn_count") == observed_turn_count
            ):
                return status(repo, run_id)
            raise AssuranceError(
                "completed dispatch compaction observation changed",
                code="DISPATCH_COMPACTION_RESULT_DRIFT",
                status="NEEDS_USER",
            )
        if intent.get("state") != "exhausted" or recovery.get("state") != "prepared":
            raise AssuranceError(
                "dispatch compaction is not completable",
                code="DISPATCH_COMPACTION_STATE_INVALID",
                status="NEEDS_USER",
            )
        if observed_turn_count != int(recovery["prior_turn_count"]) + 1:
            raise AssuranceError(
                "dispatch thread changed by more than one compaction turn",
                code="DISPATCH_COMPACTION_TURN_COUNT_DRIFT",
                status="NEEDS_USER",
            )
        from ..core import capture_runtime_identity

        current_runtime_identity = capture_runtime_identity()
        if current_runtime_identity != ledger["runtime_identity"]:
            raise AssuranceError(
                "dispatch compaction cannot change the frozen runtime identity",
                code="DISPATCH_RUNTIME_IDENTITY_MISMATCH",
                status="NEEDS_USER",
                details={
                    "expected_runtime_identity": copy.deepcopy(ledger["runtime_identity"]),
                    "actual_runtime_identity": current_runtime_identity,
                },
            )
        agent = ledger["facets"]["execution"]["agents"].get("reviewer")
        if not isinstance(agent, dict) or agent.get("thread_id") != recovery.get(
            "thread_id"
        ):
            raise AssuranceError(
                "dispatch compaction lost Reviewer thread continuity",
                code="DISPATCH_COMPACTION_IDENTITY_MISMATCH",
                status="NEEDS_USER",
            )
        completed_at = now()
        completed_recovery = {
            **copy.deepcopy(recovery),
            "state": "completed",
            "compaction_turn_id": compaction_turn_id,
            "context_item_id": context_item_id,
            "compaction_turn_digest": compaction_turn_digest,
            "compaction_duration_ms": compaction_duration_ms,
            "observed_turn_count": observed_turn_count,
            "completed_at": completed_at,
        }
        previous_intent = copy.deepcopy(intent)
        previous_intent["compaction_recovery"] = copy.deepcopy(completed_recovery)
        previous_digest = digest(previous_intent)
        previous_generation = int(previous_intent.get("generation", 1))
        renewed_intent = {
            "action_id": previous_intent["action_id"],
            "action": previous_intent["action"],
            "role": previous_intent["role"],
            "thread_id": previous_intent["thread_id"],
            "prompt_digest": previous_intent["prompt_digest"],
            "output_schema_digest": previous_intent["output_schema_digest"],
            "dispatch_observation_digest": previous_intent.get(
                "dispatch_observation_digest"
            ),
            "candidate_manifest_digest": previous_intent.get(
                "candidate_manifest_digest"
            ),
            "native_transport_generation": previous_intent.get(
                "native_transport_generation"
            ),
            "timeout_profile_digest": previous_intent.get("timeout_profile_digest"),
            "last_wire_sequence": previous_intent.get("last_wire_sequence", 0),
            "last_wire_event_digest": previous_intent.get("last_wire_event_digest"),
            "side_effect_observation_digest": previous_intent.get(
                "side_effect_observation_digest"
            ),
            "state": "prepared",
            "continuity_state": "prepared",
            "attempt": 1,
            "generation": previous_generation + 1,
            "interrupted_retry_used": False,
            "interrupted_retry_blocked": False,
            "renewed_from_digest": previous_digest,
            "renewal_reason": "automatic_reviewer_thread_compaction",
            "compaction_recovery": completed_recovery,
            "created_at": completed_at,
        }
        for key in (
            "native_transport_generation",
            "timeout_profile_digest",
            "last_wire_event_digest",
            "side_effect_observation_digest",
            "candidate_manifest_digest",
        ):
            if renewed_intent.get(key) is None:
                renewed_intent.pop(key, None)
        ledger["dispatch_intent"] = renewed_intent
        append_event(
            ledger,
            "dispatch_compaction_completed",
            {
                "action_id": action_id,
                "thread_id": previous_intent["thread_id"],
                "source_generation": previous_generation,
                "generation": renewed_intent["generation"],
                "compaction_turn_id": compaction_turn_id,
                "context_item_id": context_item_id,
                "compaction_turn_digest": compaction_turn_digest,
                "duration_ms": compaction_duration_ms,
                "observed_turn_count": observed_turn_count,
            },
        )
        append_event(
            ledger,
            "dispatch_renewed",
            {
                "action_id": action_id,
                "role": previous_intent["role"],
                "thread_id": previous_intent["thread_id"],
                "previous_generation": previous_generation,
                "generation": renewed_intent["generation"],
                "previous_attempt": int(previous_intent.get("attempt", 1)),
                "previous_turn_id": previous_intent.get("turn_id"),
                "failure_code": previous_intent.get("failure_code"),
                "previous_digest": previous_digest,
                "reason": renewed_intent["renewal_reason"],
            },
        )
        append_event(ledger, "dispatch_prepared", copy.deepcopy(renewed_intent))
        save_ledger(repo, ledger)
    return status(repo, run_id)


def consume_dispatch(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    consumer_source: str | None = None,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        intent = ledger.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError("dispatch action is stale", code="DRIVER_ACTION_STALE", status="FAIL")
        if (
            intent.get("owner_mode") == "root_session"
            and intent.get("owner_session_id") != owner_session_id
        ):
            raise AssuranceError(
                "root Builder dispatch consumption identity does not match",
                code="BUILDER_OWNER_SESSION_MISMATCH",
                status="FAIL",
                details={
                    "expected_session_id": intent.get("owner_session_id"),
                    "provided_session_id": owner_session_id,
                },
            )
        if (
            intent.get("role") == "builder"
            and intent.get("owner_mode") == "root_session"
        ):
            expected_application = _expected_root_builder_result_application(intent)
            if intent.get("result_application") != expected_application:
                raise AssuranceError(
                    "root Builder result must be applied before dispatch consumption",
                    code="ROOT_BUILDER_RESULT_NOT_APPLIED",
                    status="FAIL",
                    details={
                        "expected_application": expected_application,
                        "actual_application": intent.get("result_application"),
                    },
                )
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


def _require_driver_runtime_owner(
    ledger: Mapping[str, Any], driver_runtime_kind: str
) -> None:
    runtime = ledger.get("driver_runtime")
    expected = runtime.get("kind") if isinstance(runtime, Mapping) else None
    if expected is None or expected != driver_runtime_kind:
        raise AssuranceError(
            "driver runtime owner does not match this failure transaction",
            code="DRIVER_RUNTIME_OWNER_MISMATCH",
            status="FAIL",
            details={"expected_driver_runtime_kind": expected},
        )


def _normalize_driver_failure_input(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AssuranceError(
            "driver failure must be a JSON object",
            code="DRIVER_FAILURE_INVALID",
            status="FAIL",
        )
    required = {"source", "status", "code", "message", "details", "action"}
    allowed = required | {"diagnostic_receipt"}
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise AssuranceError(
            "driver failure fields do not match the public transaction contract",
            code="DRIVER_FAILURE_INVALID",
            status="FAIL",
            details={"required_fields": sorted(required), "allowed_fields": sorted(allowed)},
        )
    normalized: dict[str, Any] = {}
    for field in ("source", "code", "message"):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            raise AssuranceError(
                f"driver failure {field} is required",
                code="DRIVER_FAILURE_INVALID",
                status="FAIL",
            )
        normalized[field] = item.strip()
    if value.get("status") != "FATAL":
        raise AssuranceError(
            "only unhandled FATAL results use the driver failure transaction",
            code="DRIVER_FAILURE_STATUS_INVALID",
            status="FAIL",
        )
    normalized["status"] = "FATAL"
    normalized["details"] = copy.deepcopy(value.get("details"))
    if "diagnostic_receipt" in value:
        normalized["diagnostic_receipt"] = copy.deepcopy(value["diagnostic_receipt"])
    action = value.get("action")
    if action is None:
        normalized["action"] = None
    else:
        if not isinstance(action, Mapping) or set(action) != {
            "action_id",
            "action",
            "reason",
        }:
            raise AssuranceError(
                "driver failure action identity is invalid",
                code="DRIVER_FAILURE_ACTION_INVALID",
                status="FAIL",
            )
        action_id = action.get("action_id")
        action_name = action.get("action")
        reason = action.get("reason")
        if (
            not isinstance(action_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", action_id) is None
            or not isinstance(action_name, str)
            or not action_name.strip()
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise AssuranceError(
                "driver failure action identity is invalid",
                code="DRIVER_FAILURE_ACTION_INVALID",
                status="FAIL",
            )
        normalized["action"] = {
            "action_id": action_id,
            "action": action_name.strip(),
            "reason": reason.strip(),
        }
    try:
        digest(normalized)
    except (TypeError, ValueError) as exc:
        raise AssuranceError(
            "driver failure must contain canonical JSON values",
            code="DRIVER_FAILURE_INVALID",
            status="FAIL",
        ) from exc
    return normalized


def _driver_failure_dispatch(ledger: Mapping[str, Any]) -> dict[str, Any] | None:
    intent = ledger.get("dispatch_intent")
    if not isinstance(intent, Mapping):
        return None
    value = {
        "action_id": intent["action_id"],
        "action": intent["action"],
        "role": intent["role"],
        "thread_id": intent["thread_id"],
        "state": intent["state"],
        "attempt": int(intent.get("attempt", 1)),
        "generation": int(intent.get("generation", 1)),
    }
    if isinstance(intent.get("turn_id"), str):
        value["turn_id"] = intent["turn_id"]
    elif intent.get("thread_id") is None:
        value["thread_id"] = None
    if isinstance(intent.get("owner_mode"), str):
        value["owner_mode"] = intent["owner_mode"]
    if isinstance(intent.get("owner_session_id"), str):
        value["owner_session_id"] = intent["owner_session_id"]
    if isinstance(intent.get("candidate_manifest_digest"), str):
        value["candidate_manifest_digest"] = intent["candidate_manifest_digest"]
    return value


def _optional_ref_head(repo: Path, ref: str) -> str | None:
    result = git(repo, "rev-parse", "--verify", ref, check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def _driver_failure_observation(
    repo: Path, ledger: Mapping[str, Any]
) -> dict[str, Any]:
    worktree = Path(str(ledger["candidate_worktree"]))
    worktree_exists = worktree.is_dir()
    worktree_head = None
    dirty: list[str] = []
    manifest: dict[str, Any] | None = None
    if worktree_exists:
        worktree_head = _optional_ref_head(worktree, "HEAD")
        try:
            dirty = dirty_paths(worktree)
            manifest = _candidate_worktree_manifest(worktree)
        except StoreError:
            dirty = ["<unreadable>"]
        except OSError:
            dirty = ["<unreadable>"]
    return {
        "ledger_phase": ledger["phase"],
        "ledger_candidate_head": ledger["facets"]["execution"].get("candidate_head"),
        "target_head": _optional_ref_head(
            repo, f"refs/heads/{ledger['target_branch']}"
        ),
        "candidate_branch": ledger["candidate_branch"],
        "candidate_branch_head": _optional_ref_head(
            repo, f"refs/heads/{ledger['candidate_branch']}"
        ),
        "candidate_worktree": str(worktree),
        "candidate_worktree_exists": worktree_exists,
        "candidate_worktree_head": worktree_head,
        "candidate_dirty_paths": dirty,
        "candidate_manifest": manifest,
    }


def _driver_failure_environment_requires_recovery(
    ledger: Mapping[str, Any]
) -> bool:
    transaction = ledger.get("deployment_transaction")
    if isinstance(transaction, Mapping) and transaction.get("state") in {
        "deploying",
        "deployed",
        "restore_required",
        "restoring",
        "restore_failed",
    }:
        return True
    lease = ledger.get("environment_lease")
    return isinstance(lease, Mapping) and lease.get("state") in {
        "held",
        "transfer_prepared",
        "restore_required",
        "restoring",
        "restore_failed",
    }


def _driver_failure_recovery(ledger: Mapping[str, Any]) -> str:
    if ledger["phase"] == "finalizing" or isinstance(
        ledger.get("finalize_intent"), Mapping
    ):
        return "finalize"
    if _driver_failure_environment_requires_recovery(ledger):
        return "deployment"
    return "none"


def record_driver_failure(
    repo_value: str | Path,
    run_value: str,
    failure_value: Any,
    *,
    driver_runtime_kind: str,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    normalized = _normalize_driver_failure_input(failure_value)
    signature = digest(normalized)
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        if _builder_mode(ledger) == "root_session":
            _assert_root_session_identity(ledger, owner_session_id)
        existing = ledger.get("driver_failure")
        if isinstance(existing, dict):
            if existing.get("signature") == signature:
                return status(repo, run_id)
            raise AssuranceError(
                "a different driver failure is already persisted",
                code="DRIVER_FAILURE_CONFLICT",
                status="FAIL",
                details={
                    "existing_signature": existing.get("signature"),
                    "incoming_signature": signature,
                },
            )
        if ledger["phase"] not in {"active", "finalizing"}:
            raise AssuranceError(
                "terminal assurance runs cannot record a new driver failure",
                code="DRIVER_FAILURE_RUN_TERMINAL",
                status="FAIL",
                details={"phase": ledger["phase"]},
            )
        recorded_at = now()
        record = {
            **normalized,
            "dispatch": _driver_failure_dispatch(ledger),
            "observation": _driver_failure_observation(repo, ledger),
            "signature": signature,
            "recovery": _driver_failure_recovery(ledger),
            "state": "recorded",
            "recorded_at": recorded_at,
        }
        record["digest"] = digest(record)
        ledger["driver_failure"] = record
        append_event(
            ledger,
            "driver_failure_recorded",
            {
                "code": record["code"],
                "signature": record["signature"],
                "failure_digest": record["digest"],
                "recovery": record["recovery"],
                "action_id": (
                    record["action"].get("action_id")
                    if isinstance(record.get("action"), dict)
                    else None
                ),
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def complete_driver_failure(
    repo_value: str | Path,
    run_value: str,
    *,
    driver_runtime_kind: str,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        if _builder_mode(ledger) == "root_session":
            _assert_root_session_identity(ledger, owner_session_id)
        failure = ledger.get("driver_failure")
        if not isinstance(failure, dict):
            raise AssuranceError(
                "run has no persisted driver failure",
                code="DRIVER_FAILURE_NOT_FOUND",
                status="FAIL",
            )
        if failure["state"] in {"recovered", "terminal"}:
            return status(repo, run_id)
        failure_digest = failure["digest"]
        if failure["state"] == "recorded":
            failure["state"] = "recovering"
            failure["recovery_started_at"] = now()
            append_event(
                ledger,
                "driver_failure_recovery_started",
                {
                    "failure_digest": failure_digest,
                    "recovery": failure["recovery"],
                },
            )
            save_ledger(repo, ledger)
        recovery = failure["recovery"]

    if recovery == "finalize":
        recover_finalize(repo, run_id)
        with locked(repo):
            ledger = read_ledger(repo, run_id)
            _assert_no_candidate_residue_intent(ledger)
            failure = ledger.get("driver_failure")
            if not isinstance(failure, dict) or failure.get("digest") != failure_digest:
                raise AssuranceError(
                    "driver failure identity changed during finalize recovery",
                    code="DRIVER_FAILURE_IDENTITY_DRIFT",
                    status="NEEDS_USER",
                )
            if ledger["phase"] != "finalized":
                raise AssuranceError(
                    "finalize recovery did not reach the finalized phase",
                    code="DRIVER_FAILURE_RECOVERY_INCOMPLETE",
                    status="NEEDS_USER",
                )
            failure["state"] = "recovered"
            failure["recovered_at"] = now()
            append_event(
                ledger,
                "driver_failure_recovered",
                {"failure_digest": failure_digest, "recovery": "finalize"},
            )
            save_ledger(repo, ledger)
        return status(repo, run_id)

    if recovery == "deployment":
        current = read_ledger(repo, run_id)
        if _driver_failure_environment_requires_recovery(current):
            restore_deployment(repo, run_id)

    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        failure = ledger.get("driver_failure")
        if not isinstance(failure, dict) or failure.get("digest") != failure_digest:
            raise AssuranceError(
                "driver failure identity changed during recovery",
                code="DRIVER_FAILURE_IDENTITY_DRIFT",
                status="NEEDS_USER",
            )
        if _driver_failure_environment_requires_recovery(ledger):
            raise AssuranceError(
                "driver failure recovery has not restored the external environment",
                code="DRIVER_FAILURE_RECOVERY_INCOMPLETE",
                status="NEEDS_USER",
            )
        ledger["phase"] = "failed"
        failure["state"] = "terminal"
        failure["terminal_at"] = now()
        append_event(
            ledger,
            "run_failed",
            {
                "code": failure["code"],
                "failure_digest": failure_digest,
                "recovery": recovery,
            },
        )
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
        _assert_no_candidate_residue_intent(ledger)
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
        ledger["machine_failure"] = None
        save_ledger(repo, ledger)
    return status(repo, run_id)


def resolve_candidate_residue(
    repo_value: str | Path,
    run_value: str,
    request_value: Any,
) -> dict[str, Any]:
    """Remove one exact ignored-file manifest without changing delivery facts."""

    request = validate_candidate_residue_resolution(request_value)
    request["reason"] = request["reason"].strip()
    if not request["reason"]:
        raise AssuranceError(
            "candidate residue resolution requires a non-empty reason",
            code="CANDIDATE_RESIDUE_REQUEST_INVALID",
            status="FAIL",
        )
    request_digest = digest(request)
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        intent = ledger.get("candidate_residue_intent")
        completed = [
            event
            for event in ledger.get("events", [])
            if isinstance(event, Mapping)
            and event.get("kind") == "candidate_residue_resolved"
            and isinstance(event.get("details"), Mapping)
            and event["details"].get("problem_key") == request["problem_key"]
        ]
        if intent is None and completed:
            matching = [
                event
                for event in completed
                if event["details"].get("request_digest") == request_digest
            ]
            if len(completed) == 1 and len(matching) == 1:
                return status(repo, run_id)
            raise AssuranceError(
                "candidate residue resolution conflicts with a completed request",
                code="CANDIDATE_RESIDUE_RESOLUTION_CONFLICT",
                status="FAIL",
            )
        if isinstance(intent, Mapping):
            if intent.get("request_digest") != request_digest:
                raise AssuranceError(
                    "candidate residue recovery already has a different request",
                    code="CANDIDATE_RESIDUE_INTENT_CONFLICT",
                    status="FAIL",
                )
        elif intent is not None:
            raise AssuranceError(
                "candidate residue recovery intent is malformed",
                code="CANDIDATE_RESIDUE_INTENT_CONFLICT",
                status="FAIL",
            )
        elif ledger["phase"] not in {"active", "superseded"}:
            raise AssuranceError(
                "candidate residue recovery is not allowed in this run phase",
                code="CANDIDATE_RESIDUE_RUN_PHASE_INVALID",
                status="FAIL",
                details={"phase": ledger["phase"]},
            )

        execution = ledger["facets"]["execution"]
        checkpointed_candidate = execution.get("candidate_head")
        worktree = Path(str(ledger["candidate_worktree"]))
        if not worktree.is_dir():
            raise AssuranceError(
                "candidate worktree is unavailable",
                code="CANDIDATE_RESIDUE_CANDIDATE_DRIFT",
                status="FAIL",
            )
        live_head = git(worktree, "rev-parse", "HEAD", check=False)
        live_branch = git(worktree, "symbolic-ref", "--quiet", "HEAD", check=False)
        branch = git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/heads/{ledger['candidate_branch']}",
            check=False,
        )
        target = git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/heads/{ledger['target_branch']}",
            check=False,
        )
        if (
            live_head.returncode != 0
            or live_head.stdout.strip() != request["candidate_head"]
            or live_branch.returncode != 0
            or live_branch.stdout.strip() != f"refs/heads/{ledger['candidate_branch']}"
            or branch.returncode != 0
            or branch.stdout.strip() != request["candidate_head"]
            or target.returncode != 0
            or target.stdout.strip() != ledger["target_start_head"]
        ):
            raise AssuranceError(
                "candidate or target identity changed before residue recovery",
                code="CANDIDATE_RESIDUE_CANDIDATE_DRIFT",
                status="FAIL",
            )

        requested_files = {
            item["path"]: item["sha256"] for item in request["files"]
        }
        live_hashes, unauthorized = _candidate_residue_filesystem(worktree)
        tracked = dirty_paths(worktree)
        already_missing: set[str] = set()
        if intent is None:
            blockers = _candidate_residue_transaction_blockers(ledger)
            if blockers:
                raise AssuranceError(
                    "candidate residue recovery conflicts with a pending transaction",
                    code="CANDIDATE_RESIDUE_TRANSACTION_PENDING",
                    status="FAIL",
                    details={"transactions": sorted(set(blockers))},
                )
            snapshot_digest, problems = _open_problem_snapshot(ledger)
            keyed = [
                item
                for item in ledger.get("problems", [])
                if isinstance(item, Mapping)
                and item.get("key") == request["problem_key"]
            ]
            if len(keyed) != 1:
                raise AssuranceError(
                    "candidate residue problem is missing or ambiguous",
                    code="CANDIDATE_RESIDUE_PROBLEM_INVALID",
                    status="FAIL",
                    details={"matches": len(keyed)},
                )
            problem = keyed[0]
            if (
                problem.get("status") != "open"
                or problem.get("owner") != "builder_loop"
                or problem.get("candidate_head") != request["candidate_head"]
                or request["problem_snapshot_digest"] != snapshot_digest
                or request["problem_key"]
                not in {item["key"] for item in problems}
            ):
                raise AssuranceError(
                    "candidate residue problem facts are stale or unauthorized",
                    code="CANDIDATE_RESIDUE_PROBLEM_INVALID",
                    status="FAIL",
                )
            if ledger["phase"] == "superseded":
                supersede_intent = ledger.get("supersede_intent")
                target_run = (
                    supersede_intent.get("target_run_id")
                    if isinstance(supersede_intent, Mapping)
                    else None
                )
                try:
                    target_ledger = (
                        read_ledger(repo, str(target_run))
                        if isinstance(target_run, str)
                        else None
                    )
                except StoreError as exc:
                    raise AssuranceError(
                        "superseded source target ledger is unavailable",
                        code="CANDIDATE_RESIDUE_SOURCE_DISPOSITION_INVALID",
                        status="FAIL",
                    ) from exc
                dispositioned = isinstance(target_ledger, Mapping) and any(
                    item.get("source_run_id") == run_id
                    and item.get("target_run_id") == target_run
                    and item.get("key") == request["problem_key"]
                    and item.get("disposition") == "handled_elsewhere"
                    for item in target_ledger.get("problem_dispositions", [])
                    if isinstance(item, Mapping)
                )
                if not dispositioned:
                    raise AssuranceError(
                        "superseded source residue was not dispositioned as handled elsewhere",
                        code="CANDIDATE_RESIDUE_SOURCE_DISPOSITION_INVALID",
                        status="FAIL",
                    )
            if tracked:
                raise AssuranceError(
                    "candidate contains tracked or ordinary-untracked drift",
                    code="CANDIDATE_RESIDUE_UNAUTHORIZED_DRIFT",
                    status="FAIL",
                    details={"paths": tracked},
                )
            if unauthorized or live_hashes != requested_files:
                raise AssuranceError(
                    "candidate ignored residue does not exactly match the authorized manifest",
                    code="CANDIDATE_RESIDUE_MANIFEST_MISMATCH",
                    status="FAIL",
                    details={
                        "requested": request["files"],
                        "observed": [
                            {"path": path, "sha256": value}
                            for path, value in sorted(live_hashes.items())
                        ],
                        "invalid_or_ordinary": unauthorized,
                    },
                )
            pre_value = _candidate_residue_replay_value(
                repo,
                ledger,
                request["files"],
                problem_key=request["problem_key"],
            )
            created_at = now()
            intent = {
                "request_digest": request_digest,
                "problem_key": request["problem_key"],
                "problem_snapshot_digest": request["problem_snapshot_digest"],
                "candidate_head": request["candidate_head"],
                "reason": request["reason"],
                "files": copy.deepcopy(request["files"]),
                "pre_mutation_digest": digest(pre_value),
                "state": "prepared",
                "created_at": created_at,
                "updated_at": created_at,
            }
            ledger["candidate_residue_intent"] = intent
            append_event(
                ledger,
                "candidate_residue_intent_written",
                {
                    "request_digest": request_digest,
                    "problem_key": request["problem_key"],
                    "candidate_head": request["candidate_head"],
                    "pre_mutation_digest": intent["pre_mutation_digest"],
                    "created_at": created_at,
                },
            )
            save_ledger(repo, ledger)
        else:
            request_recorded_at = next(
                (
                    str(event["details"].get("created_at"))
                    for event in reversed(ledger.get("events", []))
                    if isinstance(event, Mapping)
                    and event.get("kind") == "candidate_residue_intent_written"
                    and isinstance(event.get("details"), Mapping)
                    and event["details"].get("request_digest") == request_digest
                ),
                None,
            )
            problem = next(
                (
                    item
                    for item in ledger.get("problems", [])
                    if isinstance(item, Mapping)
                    and item.get("key") == request["problem_key"]
                ),
                None,
            )
            if not isinstance(problem, dict) or problem.get("status") != "open":
                raise AssuranceError(
                    "candidate residue replay no longer binds an open problem",
                    code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
                    status="FAIL",
                )
            if (
                request_recorded_at != intent.get("created_at")
                or intent.get("problem_key") != request["problem_key"]
                or intent.get("problem_snapshot_digest")
                != request["problem_snapshot_digest"]
                or intent.get("candidate_head") != request["candidate_head"]
                or intent.get("reason") != request["reason"]
                or intent.get("files") != request["files"]
            ):
                raise AssuranceError(
                    "candidate residue replay intent facts changed",
                    code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
                    status="FAIL",
                )
            pre_value = _candidate_residue_replay_value(
                repo, ledger, request["files"]
            )
            if digest(pre_value) != intent.get("pre_mutation_digest"):
                raise AssuranceError(
                    "candidate residue replay facts changed",
                    code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
                    status="FAIL",
                )
            unexpected = sorted(set(live_hashes) - set(requested_files))
            changed = sorted(
                path
                for path, value in live_hashes.items()
                if requested_files.get(path) != value
            )
            remaining = sorted(set(requested_files) & set(live_hashes))
            missing = sorted(set(requested_files) - set(live_hashes))
            already_missing = set(missing)
            if tracked or unauthorized or unexpected or changed or any(
                _candidate_residue_path(worktree, path).exists()
                or _candidate_residue_path(worktree, path).is_symlink()
                for path in missing
            ):
                raise AssuranceError(
                    "candidate residue replay observed changed filesystem facts",
                    code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
                    status="FAIL",
                    details={
                        "tracked": tracked,
                        "invalid_or_ordinary": unauthorized,
                        "unexpected": unexpected,
                        "changed": changed,
                    },
                )

        assert isinstance(intent, dict)
        intent["state"] = "deleting"
        intent["updated_at"] = now()
        save_ledger(repo, ledger)
        for item in request["files"]:
            _quarantine_candidate_residue_file(
                repo,
                ledger,
                worktree,
                item["path"],
                item["sha256"],
                allow_missing=item["path"] in already_missing,
            )
        remaining_hashes, remaining_invalid = _candidate_residue_filesystem(worktree)
        if dirty_paths(worktree) or remaining_hashes or remaining_invalid:
            raise AssuranceError(
                "candidate residue recovery did not leave a clean worktree",
                code="CANDIDATE_RESIDUE_CLEANUP_INCOMPLETE",
                status="FAIL",
                details={
                    "remaining": sorted(remaining_hashes),
                    "invalid_or_ordinary": remaining_invalid,
                },
            )
        intent["state"] = "completing"
        intent["updated_at"] = now()
        save_ledger(repo, ledger)
        problem["status"] = "resolved"
        problem["resolution"] = request["reason"]
        problem["resolved_at"] = now()
        append_event(
            ledger,
            "candidate_residue_resolved",
            {
                "request_digest": request_digest,
                "problem_key": request["problem_key"],
                "candidate_head": request["candidate_head"],
                "files": copy.deepcopy(request["files"]),
                "phase": ledger["phase"],
                "checkpointed_candidate_head": checkpointed_candidate,
            },
        )
        ledger["candidate_residue_intent"] = None
        save_ledger(repo, ledger)
    return status(repo, run_id)


RETROSPECTIVE_TERMINAL_PHASES = {"finalized", "failed", "abandoned", "superseded"}


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
        elif isinstance(ledger.get("driver_failure"), dict):
            failure = ledger["driver_failure"]
            signals.append(
                _retrospective_signal(
                    "terminal-runtime-failure",
                    "mandatory",
                    [run_id],
                    f"Run {run_id} recorded recovered driver FATAL {failure['code']}",
                    {
                        "terminal_status": terminal_fact["terminal_status"],
                        "code": failure["code"],
                        "reason": failure["message"],
                        "failure_digest": failure["digest"],
                        "failure_state": failure["state"],
                        "recovery": failure["recovery"],
                    },
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
                if action in {"tester_fix", "builder_fix", "tester_proof_diagnose"}:
                    correction_counts[action] = correction_counts.get(action, 0) + 1
            elif kind == "tester_continuity_replaced":
                action = "tester_continuity_replaced"
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
            elif kind == "dispatch_renewed" and isinstance(action_id, str):
                manual_recoveries.append(
                    {
                        "action_id": action_id,
                        "consumer_source": "user_authorized_generation",
                        "generation": details.get("generation"),
                        "failure_code": details.get("failure_code"),
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
        lineage_facts = _derive_lineage(repo, ledger)
        if (
            lineage_facts["task_revision_count"] > 1
            or lineage_facts["task_transition_count"] > 0
            or lineage_facts["health"] != "healthy"
        ):
            severity = (
                "mandatory"
                if lineage_facts["task_revision_count"] >= 3
                or lineage_facts["health"] != "healthy"
                else "advisory"
            )
            signals.append(
                _retrospective_signal(
                    "revision-pressure",
                    severity,
                    [run_id],
                    (
                        f"Run {run_id} recorded revision pressure "
                        f"{lineage_facts['pressure_digest']}"
                    ),
                    {
                        "pressure_digest": lineage_facts["pressure_digest"],
                        "revision_count": lineage_facts["revision_count"],
                        "transition_count": lineage_facts["transition_count"],
                        "non_semantic_transition_count": lineage_facts[
                            "non_semantic_transition_count"
                        ],
                        "transition_category_counts": lineage_facts[
                            "transition_category_counts"
                        ],
                        "task_root_run_id": lineage_facts["task_root_run_id"],
                        "task_revision_count": lineage_facts[
                            "task_revision_count"
                        ],
                        "task_transition_count": lineage_facts[
                            "task_transition_count"
                        ],
                        "task_non_semantic_transition_count": lineage_facts[
                            "task_non_semantic_transition_count"
                        ],
                        "task_transition_category_counts": lineage_facts[
                            "task_transition_category_counts"
                        ],
                        "task_pressure_digest": lineage_facts[
                            "task_pressure_digest"
                        ],
                        "health": lineage_facts["health"],
                    },
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
    runtime_commits = {
        str(item[0].get("runtime_identity", {}).get("adapter_commit"))
        for item in ledgers
        if item[0].get("runtime_identity", {}).get("capture_status") == "captured"
        and isinstance(item[0].get("runtime_identity", {}).get("adapter_commit"), str)
    }
    identity_statuses = {
        str(item[0].get("runtime_identity", {}).get("capture_status"))
        for item in ledgers
    }
    if len(runtime_commits) == 1 and identity_statuses == {"captured"}:
        runtime_commit: str | None = next(iter(runtime_commits))
        derivation_capture_status = "captured"
    elif runtime_commits:
        runtime_commit = None
        derivation_capture_status = "partial"
    else:
        runtime_commit = None
        derivation_capture_status = "unavailable"
    derivation_identity = {
        "runtime_commit": runtime_commit,
        "retrospective_schema_version": 2,
        "derivation_version": "assurance-v4-retrospective-v2",
        "capture_status": derivation_capture_status,
    }
    derivation_identity_digest = digest(derivation_identity)
    snapshot_base = {
        "schema_version": 2,
        "repo_root": repository_identity,
        "owner_session_id": session_id,
        "runs": run_facts,
        "signals": signals,
        "derivation_identity": derivation_identity,
        "derivation_identity_digest": derivation_identity_digest,
    }
    snapshot = {**snapshot_base, "snapshot_digest": digest(snapshot_base)}
    return validate_retrospective_snapshot(snapshot)


def _retrospective_report_digest_input(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    value = {
        key: copy.deepcopy(report[key])
        for key in (
            "schema_version",
            "repo_root",
            "owner_session_id",
            "snapshot_digest",
            "dispositions",
        )
    }
    if report["schema_version"] == 2:
        value["issue_updates"] = copy.deepcopy(report["issue_updates"])
    if report["schema_version"] == 3:
        value["derivation_identity_digest"] = report["derivation_identity_digest"]
        value["issue_updates"] = copy.deepcopy(report["issue_updates"])
    return value


def _retrospective_issue_bindings(
    snapshot_digest: str,
    dispositions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for item in dispositions:
        if item.get("disposition") != "issue":
            continue
        key = (str(item["owner"]), str(item["reference"]))
        groups.setdefault(key, []).append(str(item["signal_id"]))
    bindings: list[dict[str, Any]] = []
    for (owner, reference), signal_ids in sorted(groups.items()):
        normalized_signal_ids = sorted(signal_ids)
        binding_input = {
            "snapshot_digest": snapshot_digest,
            "owner": owner,
            "reference": reference,
            "signal_ids": normalized_signal_ids,
        }
        bindings.append(
            {
                "owner": owner,
                "reference": reference,
                "signal_ids": normalized_signal_ids,
                "signal_digest": digest(binding_input),
            }
        )
    return bindings


def _ordered_retrospective_issue_updates(
    snapshot_digest: str,
    dispositions: Sequence[Mapping[str, Any]],
    issue_updates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    expected = _retrospective_issue_bindings(snapshot_digest, dispositions)
    expected_by_key = {
        (item["owner"], item["reference"]): item for item in expected
    }
    received_keys = [
        (str(item["owner"]), str(item["reference"])) for item in issue_updates
    ]
    duplicate_keys = sorted(
        {key for key in received_keys if received_keys.count(key) > 1}
    )
    received_by_key = {
        (str(item["owner"]), str(item["reference"])): item
        for item in issue_updates
    }
    missing = sorted(set(expected_by_key) - set(received_by_key))
    unexpected = sorted(set(received_by_key) - set(expected_by_key))
    if duplicate_keys or missing or unexpected:
        raise AssuranceError(
            "retrospective Issue update receipts must cover each owner/reference exactly once",
            code="RETROSPECTIVE_ISSUE_UPDATE_COVERAGE_INVALID",
            status="FAIL",
            details={
                "duplicates": [list(item) for item in duplicate_keys],
                "missing": [list(item) for item in missing],
                "unexpected": [list(item) for item in unexpected],
            },
        )
    remote_records = [str(item["remote_record_reference"]) for item in issue_updates]
    duplicate_remote_records = sorted(
        {item for item in remote_records if remote_records.count(item) > 1}
    )
    if duplicate_remote_records:
        raise AssuranceError(
            "one remote Issue record cannot verify multiple retrospective bindings",
            code="RETROSPECTIVE_ISSUE_UPDATE_REMOTE_RECORD_REUSED",
            status="FAIL",
            details={"remote_record_references": duplicate_remote_records},
        )
    ordered: list[dict[str, Any]] = []
    for binding in expected:
        key = (binding["owner"], binding["reference"])
        receipt = received_by_key[key]
        if (
            list(receipt["signal_ids"]) != binding["signal_ids"]
            or receipt["signal_digest"] != binding["signal_digest"]
        ):
            raise AssuranceError(
                "retrospective Issue update receipt does not bind the current routed signals",
                code="RETROSPECTIVE_ISSUE_UPDATE_SIGNAL_MISMATCH",
                status="FAIL",
                details={
                    "owner": binding["owner"],
                    "reference": binding["reference"],
                    "expected_signal_ids": binding["signal_ids"],
                    "actual_signal_ids": list(receipt["signal_ids"]),
                    "expected_signal_digest": binding["signal_digest"],
                    "actual_signal_digest": receipt["signal_digest"],
                },
            )
        if receipt["written_body_digest"] != receipt["read_back_body_digest"]:
            raise AssuranceError(
                "retrospective Issue update was not read back byte-identically",
                code="RETROSPECTIVE_ISSUE_UPDATE_BODY_MISMATCH",
                status="FAIL",
                details={
                    "owner": binding["owner"],
                    "reference": binding["reference"],
                    "remote_record_reference": receipt["remote_record_reference"],
                    "written_body_digest": receipt["written_body_digest"],
                    "read_back_body_digest": receipt["read_back_body_digest"],
                },
            )
        ordered.append(copy.deepcopy(dict(receipt)))
    return ordered


def _retrospective_issue_sync(report: Mapping[str, Any]) -> dict[str, Any]:
    bindings = _retrospective_issue_bindings(
        str(report["snapshot_digest"]), report["dispositions"]
    )
    if not bindings:
        state = "not-required"
        receipts: list[dict[str, Any]] = []
    elif report["schema_version"] == 1:
        state = "legacy-unverified"
        receipts = []
    else:
        state = "verified"
        receipts = copy.deepcopy(list(report["issue_updates"]))
    return {
        "state": state,
        "required_update_count": len(bindings),
        "verified_update_count": len(receipts),
        "required_bindings": bindings,
        "receipts": receipts,
    }


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
    if report["schema_version"] in {2, 3}:
        try:
            ordered_updates = _ordered_retrospective_issue_updates(
                report["snapshot_digest"],
                report["dispositions"],
                report["issue_updates"],
            )
        except AssuranceError as exc:
            raise AssuranceError(
                str(exc),
                code="RETROSPECTIVE_REPORT_STORED_INVALID",
                status="FATAL",
                details={"reason_code": exc.code, **dict(exc.details or {})},
            ) from exc
        if ordered_updates != report["issue_updates"]:
            raise AssuranceError(
                "stored retrospective Issue update receipts are not canonical",
                code="RETROSPECTIVE_REPORT_STORED_INVALID",
                status="FATAL",
            )
    report_base = _retrospective_report_digest_input(report)
    if report["report_digest"] != digest(report_base):
        raise AssuranceError(
            "stored retrospective report digest does not match its content",
            code="RETROSPECTIVE_REPORT_DIGEST_MISMATCH",
            status="FATAL",
        )
    return report


def _render_retrospective_block(
    snapshot: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    pending_user: bool,
    issue_sync: Mapping[str, Any],
    pending_issue_updates: Sequence[Mapping[str, Any]],
) -> str:
    signal_by_id = {item["signal_id"]: item for item in snapshot["signals"]}
    if pending_issue_updates:
        heading = "Builder-loop retrospective requires Issue synchronization."
    elif pending_user:
        heading = "Builder-loop retrospective requires user input."
    else:
        heading = "Builder-loop retrospective complete."
    lines = [
        heading,
        f"Session: {snapshot['owner_session_id']}",
        f"Snapshot: {snapshot['snapshot_digest']}",
        f"Report: {report['report_digest']}",
        (
            f"Issue synchronization: {issue_sync['state']} "
            f"({issue_sync['verified_update_count']}/"
            f"{issue_sync['required_update_count']} verified)."
        ),
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
    for binding in pending_issue_updates:
        lines.append(
            "- Unverified Issue update "
            f"{binding['owner']} {binding['reference']} => "
            f"{','.join(binding['signal_ids'])} ({binding['signal_digest']})"
        )
    if pending_issue_updates:
        lines.append(
            f"BUILDER_RETROSPECTIVE_SYNC_REQUIRED:{snapshot['snapshot_digest']}:"
            f"{report['report_digest']}"
        )
    elif pending_user:
        lines.append(
            f"BUILDER_INPUT_REQUIRED:{snapshot['owner_session_id']}:{snapshot['snapshot_digest']}"
        )
    else:
        lines.append(
            f"BUILDER_RETROSPECTIVE_READY:{snapshot['snapshot_digest']}:{report['report_digest']}"
        )
    return "\n".join(lines)


def _render_retrospective_user_block(
    snapshot: Mapping[str, Any],
    report: Mapping[str, Any],
    *,
    pending_runs: Mapping[str, Mapping[str, Any]],
    pending_dispositions: list[Mapping[str, Any]],
) -> str:
    issue_routes = sum(
        item.get("disposition") == "issue" for item in report["dispositions"]
    )
    if not pending_runs and not pending_dispositions:
        return "\n".join(
            [
                "Builder-loop retrospective complete.",
                (
                    f"Runs: {len(snapshot['runs'])}; Signals: {len(snapshot['signals'])}; "
                    f"Issue routes: {issue_routes}."
                ),
                f"Report: {report['report_digest']}",
                (
                    f"BUILDER_RETROSPECTIVE_READY:{snapshot['snapshot_digest']}:"
                    f"{report['report_digest']}"
                ),
            ]
        )

    pending_count = len(pending_runs) + len(pending_dispositions)
    lines = [
        "Builder-loop retrospective requires user input.",
        (
            f"Runs: {len(snapshot['runs'])}; Signals: {len(snapshot['signals'])}; "
            f"Pending: {pending_count}; Issue routes: {issue_routes}."
        ),
        f"Report: {report['report_digest']}",
        "Pending:",
    ]
    for run_id in sorted(pending_runs):
        fact = pending_runs[run_id]
        action = " ".join(str(fact.get("action") or "unknown").split())
        reason = " ".join(str(fact.get("reason") or "needs_user").split())
        lines.append(f"- Run {run_id}: {action} ({reason})")
    for item in pending_dispositions:
        reason = " ".join(str(item.get("reason") or "needs user").split())
        lines.append(f"- Disposition {item['signal_id']}: {reason}")
    lines.append(
        f"BUILDER_INPUT_REQUIRED:{snapshot['owner_session_id']}:{snapshot['snapshot_digest']}"
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
            if phase == "failed":
                failure = ledger.get("driver_failure")
                terminal_facts[run_id] = {
                    "terminal_status": "fatal",
                    "code": (
                        failure.get("code")
                        if isinstance(failure, Mapping)
                        else "DRIVER_FAILURE_UNAVAILABLE"
                    ),
                    "reason": (
                        failure.get("message")
                        if isinstance(failure, Mapping)
                        else "failed run has no readable driver failure"
                    ),
                    "failure_digest": (
                        failure.get("digest")
                        if isinstance(failure, Mapping)
                        else None
                    ),
                }
            else:
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
        or (
            report["schema_version"] == 3
            and report.get("derivation_identity_digest")
            != snapshot.get("derivation_identity_digest")
        )
    ):
        return {
            "status": "STALE",
            "owner_session_id": owner_session_id,
            "message": "the stored retrospective report is stale",
            "snapshot": snapshot,
            "report": report,
        }
    pending_dispositions = [
        item for item in report["dispositions"] if item["disposition"] == "needs-user"
    ]
    pending_runs = {
        run_id: fact
        for run_id, fact in terminal_facts.items()
        if fact["terminal_status"] in {"needs-user", "continuity-failure"}
    }
    issue_sync = _retrospective_issue_sync(report)
    pending_issue_updates = (
        issue_sync["required_bindings"]
        if issue_sync["state"] == "legacy-unverified"
        else []
    )
    pending_user = bool(pending_dispositions or pending_runs)
    required_block = _render_retrospective_block(
        snapshot,
        report,
        pending_user=pending_user,
        issue_sync=issue_sync,
        pending_issue_updates=pending_issue_updates,
    )
    if pending_issue_updates:
        result_status = "REQUIRED"
    elif pending_user:
        result_status = "NEEDS_USER"
    else:
        result_status = "READY"
    result = {
        "status": result_status,
        "owner_session_id": owner_session_id,
        "message": (
            "retrospective Issue synchronization is required"
            if pending_issue_updates
            else "retrospective awaits an authorized user decision"
            if pending_user
            else "retrospective report is complete"
        ),
        "snapshot": snapshot,
        "report": report,
        "issue_sync": issue_sync,
        "derivation_status": (
            "verified"
            if report["schema_version"] == 3
            and report.get("derivation_identity_digest")
            == snapshot.get("derivation_identity_digest")
            else "legacy-unverified"
        ),
        "required_block": required_block,
    }
    if not pending_issue_updates:
        result["required_user_block"] = _render_retrospective_user_block(
            snapshot,
            report,
            pending_runs=pending_runs,
            pending_dispositions=pending_dispositions,
        )
    return result


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
            "schema_version": report_input["schema_version"],
            "repo_root": snapshot["repo_root"],
            "owner_session_id": owner_session_id,
            "snapshot_digest": snapshot["snapshot_digest"],
            "dispositions": ordered,
        }
        if report_input["schema_version"] in {2, 3}:
            report_base["issue_updates"] = _ordered_retrospective_issue_updates(
                snapshot["snapshot_digest"],
                ordered,
                report_input["issue_updates"],
            )
        if report_input["schema_version"] == 3:
            if (
                report_input["derivation_identity_digest"]
                != snapshot["derivation_identity_digest"]
            ):
                raise AssuranceError(
                    "retrospective report does not bind the current derivation identity",
                    code="RETROSPECTIVE_DERIVATION_STALE",
                    status="FAIL",
                    details={
                        "expected_derivation_identity_digest": snapshot[
                            "derivation_identity_digest"
                        ],
                        "actual_derivation_identity_digest": report_input[
                            "derivation_identity_digest"
                        ],
                    },
                )
            report_base["derivation_identity_digest"] = snapshot[
                "derivation_identity_digest"
            ]
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


def _proof_tester_scope(
    repo: Path,
    source_head: Any,
    tester_patterns: Sequence[str],
) -> list[dict[str, str]] | None:
    if (
        not isinstance(source_head, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_head) is None
        or not commit_exists(repo, source_head)
    ):
        return None
    try:
        from ..core import proof_manifest

        entries, _manifest_digest = proof_manifest(
            repo,
            source_head,
            tester_patterns,
        )
    except Exception:
        return None
    scope: list[dict[str, str]] = []
    for item in entries:
        if (
            not isinstance(item, Mapping)
            or item.get("type") != "blob"
            or item.get("mode") not in {"100644", "100755"}
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("oid"), str)
        ):
            return None
        path = str(item["path"])
        blob = str(item["oid"])
        if _regular_blob_at(repo, source_head, path) != blob:
            return None
        scope.append({"path": path, "blob": blob})
    return scope


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
    if record.get("status") == "pass" and kind == "proof":
        mission = ledger.get("facets", {}).get("mission", {})
        behaviors = mission.get("behaviors") if isinstance(mission, Mapping) else None
        expected_behaviors = (
            [item.get("id") for item in behaviors]
            if isinstance(behaviors, list)
            and all(isinstance(item, Mapping) for item in behaviors)
            else None
        )
        execution = ledger.get("facets", {}).get("execution", {})
        source = execution.get("tester_source") if isinstance(execution, Mapping) else None
        authority = ledger.get("facets", {}).get("authority", {})
        builder_patterns = (
            authority.get("builder_write", [])
            if isinstance(authority, Mapping)
            else []
        )
        tester_patterns = (
            authority.get("tester_write", [])
            if isinstance(authority, Mapping)
            else []
        )
        expected_source_head = source.get("head") if isinstance(source, Mapping) else None
        tester_paths = _proof_tester_scope(
            Path(ledger["repo_root"]),
            expected_source_head,
            tester_patterns,
        )
        if tester_paths is None:
            return "stale"
        candidate_head = (
            execution.get("candidate_head") if isinstance(execution, Mapping) else None
        )
        candidate_tester_paths = _proof_tester_scope(
            Path(ledger["repo_root"]),
            candidate_head,
            tester_patterns,
        )
        if candidate_tester_paths != tester_paths:
            return "stale"
        tester_agent = (
            execution.get("agents", {}).get("tester")
            if isinstance(execution, Mapping)
            and isinstance(execution.get("agents"), Mapping)
            else None
        )
        expected_producer = (
            {
                "role": "tester",
                "agent_id": tester_agent.get("agent_id"),
                "thread_id": tester_agent.get("thread_id"),
            }
            if isinstance(tester_agent, Mapping)
            else None
        )
        if not _proof_record_replayable(
            record,
            expected_behaviors=expected_behaviors,
            expected_candidate_head=candidate_head,
            expected_producer=expected_producer,
            expected_source_head=expected_source_head,
            tester_paths=tester_paths,
            repo=Path(ledger["repo_root"]),
            builder_patterns=builder_patterns,
            tester_patterns=tester_patterns,
        ):
            return "stale"
    return "pass" if record.get("status") == "pass" else "failed"


def doc_reference_scan_state(ledger: Mapping[str, Any]) -> str:
    if ledger.get("doc_reference_contract_version") is None:
        return "not_required"
    scan = ledger.get("doc_reference_scan")
    if not isinstance(scan, Mapping):
        return "missing"
    execution = ledger["facets"]["execution"]
    if (
        scan.get("target_start_head") != ledger.get("target_start_head")
        or scan.get("candidate_head") != execution.get("candidate_head")
    ):
        return "stale"
    status_value = scan.get("status")
    if status_value == "pass":
        return "pass"
    if status_value == "fail":
        return "failed"
    return "error"


def proof_failure_state(ledger: Mapping[str, Any]) -> str:
    record = ledger.get("proof_failure")
    if not isinstance(record, Mapping):
        return "missing"
    if record.get("dependency_digest") != evidence_dependency(ledger, "proof"):
        return "stale"
    execution = ledger["facets"]["execution"]
    source = execution.get("tester_source")
    source_head = source.get("head") if isinstance(source, Mapping) else None
    if record.get("candidate_head") != execution.get("candidate_head"):
        return "stale"
    if record.get("tester_source_head") != source_head:
        return "stale"
    expected_producer = execution.get("agents", {}).get("tester")
    producer = record.get("producer")
    if not isinstance(expected_producer, Mapping) or producer != {
        "role": "tester",
        "agent_id": expected_producer.get("agent_id"),
        "thread_id": expected_producer.get("thread_id"),
    }:
        return "stale"
    return "current"


def current_proof_failure(ledger: Mapping[str, Any]) -> dict[str, Any] | None:
    if proof_failure_state(ledger) != "current":
        return None
    record = ledger.get("proof_failure")
    return copy.deepcopy(record) if isinstance(record, dict) else None


def readiness(ledger: Mapping[str, Any]) -> dict[str, Any]:
    required = ledger["facets"]["assurance"]["required"]
    states = {kind: evidence_state(ledger, kind) for kind in required}
    scan_state = doc_reference_scan_state(ledger)
    if scan_state != "not_required":
        states["doc_reference_scan"] = scan_state
    missing = [kind for kind, state in states.items() if state == "missing"]
    stale = [kind for kind, state in states.items() if state == "stale"]
    failed = [kind for kind, state in states.items() if state in {"failed", "error"}]
    ready = not missing and not stale and not failed and bool(
        ledger["facets"]["execution"].get("candidate_head")
    )
    return {"ready": ready, "states": states, "missing": missing, "stale": stale, "failed": failed}


def _timestamp_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _profile_projection(ledger: Mapping[str, Any]) -> dict[str, Any]:
    requested = requested_profile(ledger["facets"])
    if requested != "compact":
        return {"requested": "full", "effective": "full", "escalation_reason": None}
    evidence_counts: dict[str, int] = {}
    for event in ledger.get("events", []):
        if not isinstance(event, Mapping):
            continue
        kind = event.get("kind")
        details = event.get("details")
        details = details if isinstance(details, Mapping) else {}
        if kind == "automatic_engineering_correction_applied":
            reason = "engineering-correction"
        elif kind in {"facet_updated", "mission_revised"}:
            reason = "contract-correction"
        elif kind == "recomposition_started":
            reason = "recomposition"
        elif kind in {
            "tester_replacement_started",
            "tester_continuity_replaced",
            "reviewer_replaced",
        }:
            reason = "role-replacement"
        elif kind == "dispatch_renewed":
            reason = "dispatch-renewal"
        elif kind == "proof_failure_recorded":
            reason = "evidence-replay:proof"
        elif kind == "dispatch_prepared" and details.get("action") in {
            "builder_fix",
            "tester_fix",
            "builder_recompose_fix",
            "tester_recompose_fix",
        }:
            reason = "correction"
        elif kind in {"machine_verified", "preflight_verified", "evidence_recorded"}:
            evidence_kind = details.get(
                "kind",
                "machine"
                if kind == "machine_verified"
                else "preflight"
                if kind == "preflight_verified"
                else None,
            )
            if isinstance(evidence_kind, str):
                evidence_counts[evidence_kind] = evidence_counts.get(evidence_kind, 0) + 1
                if evidence_counts[evidence_kind] > 1:
                    reason = f"evidence-replay:{evidence_kind}"
                else:
                    continue
            else:
                continue
        else:
            continue
        return {
            "requested": "compact",
            "effective": "full",
            "escalation_reason": reason,
        }
    reasons = compact_ineligibility_reasons(ledger["facets"])
    if reasons:
        return {
            "requested": "compact",
            "effective": "full",
            "escalation_reason": f"eligibility-drift:{reasons[0]}",
        }
    return {"requested": "compact", "effective": "compact", "escalation_reason": None}


def telemetry(ledger: Mapping[str, Any]) -> dict[str, Any]:
    events = [item for item in ledger.get("events", []) if isinstance(item, dict)]
    end_at = ledger["updated_at"]
    elapsed_ms = max(0, _timestamp_ms(end_at) - _timestamp_ms(ledger["created_at"]))
    stage_stats: dict[str, dict[str, Any]] = {}
    dispatches: dict[str, tuple[str, int]] = {}
    dispatch_turns: dict[tuple[str, str], tuple[str, int]] = {}
    open_dispatch_turns: dict[str, list[str]] = {}
    legacy_attempt_started: dict[str, int] = {}
    evidence_attempts = {kind: 0 for kind in EVIDENCE_KINDS}
    retry_codes: dict[str, int] = {}
    wire_observation_checkpoints = 0
    transport_cleanup_unknown = 0
    deferred_wait_stalls = 0
    candidate_changes = 0
    agent_turn_ms = 0
    deterministic_gate_ms = 0
    recomposition_ms = 0
    recomposition_started: dict[str, int] = {}
    lifecycle = {
        "publication_generations": int(
            (ledger.get("publication") or {}).get("generation", 0)
            if isinstance(ledger.get("publication"), Mapping)
            else 0
        ),
        "target_drifts": 0,
        "recomposition_attempts": 0,
        "recomposition_restarts": 0,
        "builder_conflict_repairs": 0,
        "tester_conflict_repairs": 0,
        "reviewer_preflight_attempts": 0,
        "dispatch_renewals": 0,
        "reviewer_thread_compactions": 0,
    }

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

    def bind_dispatch_turn(
        action_id: str, turn_id: str, *, action: str, at_ms: int
    ) -> None:
        key = (action_id, turn_id)
        if key in dispatch_turns:
            return
        dispatch_turns[key] = (action, at_ms)
        open_dispatch_turns.setdefault(action_id, []).append(turn_id)
        stage(action)["attempts"] += 1

    def finish_dispatch_turn(
        action_id: str, turn_id: Any, *, at_ms: int
    ) -> tuple[str, int]:
        action = dispatches.get(action_id, ("unknown", at_ms))[0]
        selected: str | None = turn_id if isinstance(turn_id, str) else None
        key = (action_id, selected) if selected is not None else None
        if key not in dispatch_turns:
            pending_turns = open_dispatch_turns.get(action_id, [])
            selected = pending_turns[-1] if pending_turns else None
            key = (action_id, selected) if selected is not None else None
        if key in dispatch_turns:
            action, started_at = dispatch_turns.pop(key)
            pending_turns = open_dispatch_turns.get(action_id, [])
            if selected in pending_turns:
                pending_turns.remove(selected)
            return action, max(0, at_ms - started_at)

        # Older ledgers and hand-written fixtures may predate turn binding. They
        # cannot identify the exact turn, but their ordered terminal events still
        # provide a non-overlapping lower-fidelity duration fallback.
        started_at = legacy_attempt_started.get(
            action_id, dispatches.get(action_id, (action, at_ms))[1]
        )
        legacy_attempt_started[action_id] = at_ms
        stage(action)["attempts"] += 1
        return action, max(0, at_ms - started_at)

    for event in events:
        kind = event.get("kind")
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        at = event.get("at")
        if kind == "dispatch_prepared" and isinstance(details.get("action_id"), str):
            action = str(details.get("action", "unknown"))
            prepared_at = _timestamp_ms(str(at))
            dispatches[details["action_id"]] = (action, prepared_at)
            legacy_attempt_started[details["action_id"]] = prepared_at
        elif kind == "dispatch_turn_bound" and isinstance(details.get("action_id"), str):
            action_id = details["action_id"]
            turn_id = details.get("turn_id")
            if isinstance(turn_id, str):
                action = dispatches.get(action_id, ("unknown", 0))[0]
                bind_dispatch_turn(
                    action_id,
                    turn_id,
                    action=action,
                    at_ms=_timestamp_ms(str(at)),
                )
        elif kind == "dispatch_completed" and isinstance(details.get("action_id"), str):
            action, duration = finish_dispatch_turn(
                details["action_id"],
                details.get("turn_id"),
                at_ms=_timestamp_ms(str(at)),
            )
            current = stage(action)
            current["completed_attempts"] += 1
            current["total_duration_ms"] += duration
            agent_turn_ms += duration
            if details.get("result") in {
                "fail",
                "findings",
                "blocked",
                "target_change_required",
            }:
                current["failed_attempts"] += 1
                current["last_failure_code"] = str(details["result"])
        elif kind in {
            "dispatch_retry_scheduled",
            "dispatch_retry_exhausted",
            "dispatch_retry_exhausted_restore_required",
        }:
            action_id = details.get("action_id")
            if not isinstance(action_id, str):
                continue
            action, duration = finish_dispatch_turn(
                action_id,
                details.get("turn_id"),
                at_ms=_timestamp_ms(str(at)),
            )
            current = stage(action)
            current["failed_attempts"] += 1
            current["retry_count"] += 1
            current["total_duration_ms"] += duration
            agent_turn_ms += duration
            failure_code = str(details.get("failure_code", "unknown"))
            current["last_failure_code"] = failure_code
            retry_codes[failure_code] = retry_codes.get(failure_code, 0) + 1
        elif kind == "dispatch_renewed":
            lifecycle["dispatch_renewals"] += 1
        elif kind == "wire_observation_checkpointed":
            wire_observation_checkpoints += 1
        elif kind == "transport_cleanup_unknown":
            transport_cleanup_unknown += 1
        elif kind == "deferred_wait_stalled":
            deferred_wait_stalls += 1
        elif kind == "dispatch_compaction_completed":
            current = stage("reviewer_thread_compaction")
            current["attempts"] += 1
            current["completed_attempts"] += 1
            lifecycle["reviewer_thread_compactions"] += 1
            duration = max(0, int(details.get("duration_ms", 0)))
            current["total_duration_ms"] += duration
            agent_turn_ms += duration
        elif kind == "machine_verified":
            current = stage("verify_machine")
            current["attempts"] += 1
            current["completed_attempts"] += 1
            current["total_duration_ms"] += int(details.get("duration_ms", 0))
            deterministic_gate_ms += int(details.get("duration_ms", 0))
            evidence_attempts["machine"] += 1
            if details.get("status") != "pass":
                current["failed_attempts"] += 1
                current["last_failure_code"] = "machine_failed"
        elif kind == "preflight_verified":
            current = stage("verify_preflight")
            current["attempts"] += 1
            current["completed_attempts"] += 1
            current["total_duration_ms"] += int(details.get("duration_ms", 0))
            deterministic_gate_ms += int(details.get("duration_ms", 0))
            evidence_attempts["preflight"] += 1
            if details.get("status") != "pass":
                current["failed_attempts"] += 1
                current["last_failure_code"] = "preflight_failed"
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
                deterministic_gate_ms += int(details.get("duration_ms", 0))
            if evidence_kind == "reviewer_preflight":
                lifecycle["reviewer_preflight_attempts"] += 1
            if details.get("status") == "fail" and isinstance(evidence_kind, str):
                current = stage(f"evidence_{evidence_kind}")
                current["failed_attempts"] += 1
                current["last_failure_code"] = f"{evidence_kind}_failed"
        elif kind == "proof_failure_recorded":
            evidence_attempts["proof"] += 1
            current = stage("tester_proof")
            if current["attempts"] == 0:
                current["attempts"] = 1
                current["completed_attempts"] = 1
            current["failed_attempts"] += 1
            current["total_duration_ms"] += int(details.get("duration_ms", 0))
            deterministic_gate_ms += int(details.get("duration_ms", 0))
            current["last_failure_code"] = str(details.get("code", "proof_failed"))
        elif kind == "recomposition_started":
            intent_id = details.get("intent_id")
            if isinstance(intent_id, str):
                recomposition_started[intent_id] = _timestamp_ms(str(at))
            lifecycle["recomposition_attempts"] += 1
            if details.get("kind") == "target_rematerialization":
                lifecycle["target_drifts"] += 1
        elif kind == "recomposition_restarted":
            lifecycle["recomposition_restarts"] += 1
        elif kind == "recomposition_conflict_resolved":
            owner = details.get("owner")
            if owner == "builder":
                lifecycle["builder_conflict_repairs"] += 1
            elif owner == "tester":
                lifecycle["tester_conflict_repairs"] += 1
        elif kind in {"target_rematerialized", "prerequisites_republished"}:
            intent_id = details.get("intent_id")
            started = recomposition_started.get(intent_id) if isinstance(intent_id, str) else None
            if isinstance(started, int):
                recomposition_ms += max(0, _timestamp_ms(str(at)) - started)
        if kind in {
            "builder_checkpointed",
            "tester_source_integrated",
            "target_rematerialized",
            "prerequisites_republished",
        }:
            candidate_changes += 1

    pending = ledger.get("dispatch_intent")
    recomposition = ledger.get("recomposition_intent")
    active_stage = (
        f"recomposition:{recomposition.get('state')}"
        if isinstance(recomposition, Mapping)
        else pending.get("action")
        if isinstance(pending, dict)
        else None
    )
    evidence_replays = sum(max(0, count - 1) for count in evidence_attempts.values())
    stage_values = [stage_stats[name] for name in sorted(stage_stats)]
    delay_candidates = [
        {
            "stage": item["name"],
            "duration_ms": item["total_duration_ms"],
            "attempts": item["attempts"],
            "last_failure_code": item["last_failure_code"],
        }
        for item in stage_values
        if item["total_duration_ms"] > 0
    ]
    if recomposition_ms:
        delay_candidates.append(
            {
                "stage": "recomposition",
                "duration_ms": recomposition_ms,
                "attempts": lifecycle["recomposition_attempts"],
                "last_failure_code": None,
            }
        )
    primary_delays = sorted(
        delay_candidates,
        key=lambda item: (-item["duration_ms"], item["stage"]),
    )[:3]
    implementation_ms = sum(
        item["total_duration_ms"]
        for item in stage_values
        if item["name"] in {"builder_implement", "builder_fix", "builder_recompose_fix"}
    )
    verification_ms = sum(
        item["total_duration_ms"]
        for item in stage_values
        if item["name"]
        in {
            "verify_preflight",
            "tester_author",
            "tester_fix",
            "tester_recompose_fix",
            "tester_proof",
            "tester_proof_diagnose",
            "tester_machine_diagnose",
            "verify_machine",
            "tester_blackbox",
            "reviewer_preflight",
            "reviewer_final",
        }
    )
    raw_waiting_ms = max(
        0,
        elapsed_ms - (agent_turn_ms + deterministic_gate_ms + recomposition_ms),
    )
    waiting_ms = min(raw_waiting_ms, elapsed_ms)
    remaining_ms = elapsed_ms - waiting_ms
    bounded_implementation_ms = min(implementation_ms, remaining_ms)
    remaining_ms -= bounded_implementation_ms
    bounded_verification_ms = min(verification_ms, remaining_ms)
    orchestration_ms = remaining_ms - bounded_verification_ms
    warnings: list[str] = []
    if implementation_ms and verification_ms > implementation_ms:
        warnings.append("verification_exceeds_implementation")
    if evidence_replays:
        warnings.append("evidence_replayed")
    if lifecycle["recomposition_restarts"]:
        warnings.append("target_recomposition_restarted")
    if lifecycle["dispatch_renewals"]:
        warnings.append("dispatch_renewed")
    if any(item["failed_attempts"] >= 2 for item in stage_values):
        warnings.append("stage_failed_repeatedly")
    assurance = ledger["facets"]["assurance"]
    required = set(assurance["required"])
    expected_stages = ["builder"]
    publication = ledger.get("publication")
    if isinstance(publication, Mapping) and publication.get("required"):
        expected_stages.append("publication")
    if "tester" in required:
        expected_stages.append("tester")
    if assurance.get("preflight_before_proof") and any(
        item.get("run_before_full_suite") for item in assurance["machine_commands"]
    ):
        expected_stages.append("preflight")
    if assurance.get("reviewer_preflight") and "reviewer" in required:
        expected_stages.append("reviewer_preflight")
    for name in ("proof", "machine"):
        if name in required:
            expected_stages.append(name)
    if isinstance(ledger["facets"]["execution"].get("deployment"), Mapping):
        expected_stages.append("deployment")
    for name in ("blackbox", "reviewer", "doc_review"):
        if name in required:
            expected_stages.append(name)
    expected_stages.append("finalize")
    accounted_ms = agent_turn_ms + deterministic_gate_ms + recomposition_ms
    return validate_telemetry(
        {
            "schema_version": 1,
            "profile": _profile_projection(ledger),
            "duration_breakdown": {
                "implementation_ms": bounded_implementation_ms,
                "verification_ms": bounded_verification_ms,
                "orchestration_ms": orchestration_ms,
                "waiting_ms": waiting_ms,
            },
            "elapsed_ms": elapsed_ms,
            "wire_observation_checkpoints": wire_observation_checkpoints,
            "transport_cleanup_unknown": transport_cleanup_unknown,
            "deferred_wait_stalls": deferred_wait_stalls,
            "active_stage": active_stage,
            "stages": stage_values,
            "candidate_changes": candidate_changes,
            "evidence_attempts": evidence_attempts,
            "evidence_replays": evidence_replays,
            "retries": {
                "total": sum(retry_codes.values()),
                "by_failure_code": dict(sorted(retry_codes.items())),
            },
            "time_classes": {
                "agent_turn_ms": agent_turn_ms,
                "deterministic_gate_ms": deterministic_gate_ms,
                "recomposition_ms": recomposition_ms,
                "idle_or_user_wait_ms": max(0, elapsed_ms - accounted_ms),
            },
            "primary_delays": primary_delays,
            "lifecycle": lifecycle,
            "expected_stages": expected_stages,
            "warnings": warnings,
        }
    )


def _problem_snapshot_value(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    problems = []
    for item in ledger.get("problems", []):
        if not isinstance(item, dict) or item.get("status") != "open":
            continue
        problem = {
            "key": item["key"],
            "summary": item["summary"],
            "details": item["details"],
            "owner": item["owner"],
        }
        if item.get("producer_continuity") == "invalid":
            problem["producer_continuity"] = "invalid"
        if isinstance(item.get("decision_request"), Mapping):
            problem["decision_request"] = copy.deepcopy(item["decision_request"])
        problems.append(problem)
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


def _candidate_residue_pre_mutation_value(
    repo: Path,
    ledger: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    execution = ledger["facets"]["execution"]
    worktree = Path(str(ledger["candidate_worktree"]))
    worktree_head = git(worktree, "rev-parse", "HEAD", check=False)
    worktree_branch = git(worktree, "symbolic-ref", "--quiet", "HEAD", check=False)
    candidate_branch = git(
        repo,
        "rev-parse",
        "--verify",
        f"refs/heads/{ledger['candidate_branch']}",
        check=False,
    )
    target_branch = git(
        repo,
        "rev-parse",
        "--verify",
        f"refs/heads/{ledger['target_branch']}",
        check=False,
    )
    return {
        "phase": ledger["phase"],
        "candidate_head": execution.get("candidate_head"),
        "candidate_branch": ledger["candidate_branch"],
        "candidate_worktree": ledger["candidate_worktree"],
        "candidate_branch_head": (
            candidate_branch.stdout.strip() if candidate_branch.returncode == 0 else None
        ),
        "candidate_worktree_head": (
            worktree_head.stdout.strip() if worktree_head.returncode == 0 else None
        ),
        "candidate_worktree_branch": (
            worktree_branch.stdout.strip() if worktree_branch.returncode == 0 else None
        ),
        "target_start_head": ledger["target_start_head"],
        "target_head": target_branch.stdout.strip() if target_branch.returncode == 0 else None,
        "digests": copy.deepcopy(ledger["digests"]),
        "agents": copy.deepcopy(execution.get("agents")),
        "tester_source": copy.deepcopy(execution.get("tester_source")),
        "evidence": copy.deepcopy(ledger.get("evidence", {})),
        "problems": copy.deepcopy(ledger.get("problems", [])),
        "driver_failure": copy.deepcopy(ledger.get("driver_failure")),
        "dispatch_intent": copy.deepcopy(ledger.get("dispatch_intent")),
        "proof_failure": copy.deepcopy(ledger.get("proof_failure")),
        "machine_failure": copy.deepcopy(ledger.get("machine_failure")),
        "recomposition_intent": copy.deepcopy(ledger.get("recomposition_intent")),
        "deployment_transaction": copy.deepcopy(ledger.get("deployment_transaction")),
        "pending_blackbox": copy.deepcopy(ledger.get("pending_blackbox")),
        "environment_lease": copy.deepcopy(ledger.get("environment_lease")),
        "supersede_intent": copy.deepcopy(ledger.get("supersede_intent")),
        "abandon_intent": copy.deepcopy(ledger.get("abandon_intent")),
        "finalize_intent": copy.deepcopy(ledger.get("finalize_intent")),
        "files": copy.deepcopy(list(files)),
    }


def _candidate_residue_replay_value(
    repo: Path,
    ledger: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
    *,
    problem_key: str | None = None,
) -> dict[str, Any]:
    value = _candidate_residue_pre_mutation_value(repo, ledger, files)
    intent = ledger.get("candidate_residue_intent")
    bound_problem_key = (
        problem_key
        if problem_key is not None
        else str(intent.get("problem_key")) if isinstance(intent, Mapping) else ""
    )
    value["problems"] = [
        copy.deepcopy(item)
        for item in ledger.get("problems", [])
        if isinstance(item, Mapping)
        and item.get("key") != bound_problem_key
    ]
    value["candidate_residue_problem"] = next(
        (
            {
                key: copy.deepcopy(item.get(key))
                for key in (
                    "key", "summary", "details", "owner", "status", "producer",
                    "candidate_head", "recorded_at",
                )
            }
            for item in ledger.get("problems", [])
            if isinstance(item, Mapping)
            and item.get("key") == bound_problem_key
        ),
        None,
    )
    return value


def _candidate_residue_filesystem(
    worktree: Path,
) -> tuple[dict[str, str], list[str]]:
    ignored = {
        path
        for path in git(
            worktree,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        ).stdout.split("\0")
        if path
    }
    ordinary = {
        path
        for path in git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout.split("\0")
        if path
    }
    hashes: dict[str, str] = {}
    invalid: list[str] = []
    root = worktree.resolve()
    for relative in sorted(ignored):
        path = worktree / relative
        try:
            resolved_parent = path.parent.resolve(strict=True)
            stat = path.lstat()
        except OSError:
            invalid.append(relative)
            continue
        if root != resolved_parent and root not in resolved_parent.parents:
            invalid.append(relative)
            continue
        cursor = worktree
        if any(
            (cursor := cursor / part).is_symlink()
            for part in Path(relative).parts[:-1]
        ):
            invalid.append(relative)
            continue
        if path.is_symlink() or not path.is_file():
            invalid.append(relative)
            continue
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes, sorted(ordinary | set(invalid))


def _candidate_residue_path(worktree: Path, relative: str) -> Path:
    try:
        validate_repo_path(relative)
    except ContractError as exc:
        raise AssuranceError(
            "candidate residue path is invalid",
            code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
            status="FAIL",
            details={"path": relative},
        ) from exc
    root = worktree.resolve()
    cursor = worktree
    for part in Path(relative).parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise AssuranceError(
                "candidate residue path crosses a symbolic link",
                code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
                status="FAIL",
                details={"path": relative},
            )
    try:
        parent = (worktree / relative).parent.resolve(strict=True)
    except OSError as exc:
        raise AssuranceError(
            "candidate residue parent changed",
            code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
            status="FAIL",
            details={"path": relative},
        ) from exc
    if root != parent and root not in parent.parents:
        raise AssuranceError(
            "candidate residue path escapes the worktree",
            code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
            status="FAIL",
            details={"path": relative},
        )
    return worktree / relative


def _candidate_residue_identity(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _candidate_residue_file_identity(
    value: os.stat_result,
) -> tuple[int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
    )


def _candidate_residue_content_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
    )


def _raise_candidate_residue_drift(
    message: str,
    relative: str,
    cause: BaseException | None = None,
) -> None:
    error = AssuranceError(
        message,
        code="CANDIDATE_RESIDUE_REPLAY_DRIFT",
        status="FAIL",
        details={"path": relative},
    )
    if cause is None:
        raise error
    raise error from cause


def _candidate_residue_open_parent(
    worktree: Path,
    parent_parts: Sequence[str],
    relative: str,
) -> tuple[int, tuple[tuple[int, int], ...]]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current_fd = -1
    next_fd = -1
    try:
        current_fd = os.open(worktree, flags)
        identities = [_candidate_residue_identity(os.fstat(current_fd))]
        for part in parent_parts:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            opened = os.fstat(next_fd)
            named = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(named.st_mode)
                or _candidate_residue_identity(opened)
                != _candidate_residue_identity(named)
            ):
                _raise_candidate_residue_drift(
                    "candidate residue parent changed",
                    relative,
                )
            os.close(current_fd)
            current_fd = next_fd
            next_fd = -1
            identities.append(_candidate_residue_identity(opened))
        return current_fd, tuple(identities)
    except AssuranceError:
        if next_fd >= 0:
            os.close(next_fd)
        if current_fd >= 0:
            os.close(current_fd)
        raise
    except OSError as exc:
        if next_fd >= 0:
            os.close(next_fd)
        if current_fd >= 0:
            os.close(current_fd)
        _raise_candidate_residue_drift(
            "candidate residue parent changed",
            relative,
            exc,
        )
    raise AssertionError("unreachable")


def _candidate_residue_fd_sha256(fd: int) -> str:
    value = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return value.hexdigest()
        value.update(chunk)


def _candidate_residue_quarantine_dir(
    repo: Path,
    ledger: Mapping[str, Any],
) -> Path:
    return run_dir(repo, str(ledger["run_id"])) / "candidate-residue-quarantine"


def _candidate_residue_quarantine_name(
    relative: str,
    expected_sha256: str,
) -> str:
    return digest({"path": relative, "sha256": expected_sha256})


def _candidate_residue_rename_exchange(
    directory_fd: int,
    first: str,
    second: str,
) -> None:
    """Atomically exchange two entries anchored to one trusted directory."""

    result = _RENAMEAT2(
        directory_fd,
        ctypes.c_char_p(os.fsencode(first)),
        directory_fd,
        ctypes.c_char_p(os.fsencode(second)),
        CANDIDATE_RESIDUE_RENAME_EXCHANGE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _candidate_residue_create_tombstone(
    directory_fd: int,
    name: str,
) -> tuple[int, tuple[int, int, int]]:
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
    except FileExistsError:
        fd = os.open(name, flags, dir_fd=directory_fd)
        value = os.fstat(fd)
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_size != 0
            or _candidate_residue_file_identity(value)
            != _candidate_residue_file_identity(named)
        ):
            os.close(fd)
            raise OSError("candidate residue deletion tombstone changed")
        return fd, _candidate_residue_file_identity(value)
    value = os.fstat(fd)
    return fd, _candidate_residue_file_identity(value)


def _candidate_residue_exchange_verified_entry(
    directory_fd: int,
    verified_name: str,
    verified_identity: tuple[int, int, int],
    tombstone_name: str,
    tombstone_identity: tuple[int, int, int],
) -> None:
    """Move only the currently named verified inode behind the tombstone name."""

    _candidate_residue_rename_exchange(
        directory_fd,
        verified_name,
        tombstone_name,
    )
    try:
        exchanged_verified = os.stat(
            tombstone_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        exchanged_tombstone = os.stat(
            verified_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError:
        try:
            _candidate_residue_rename_exchange(
                directory_fd,
                verified_name,
                tombstone_name,
            )
        except OSError:
            pass
        raise
    if (
        _candidate_residue_file_identity(exchanged_verified) == verified_identity
        and _candidate_residue_file_identity(exchanged_tombstone)
        == tombstone_identity
    ):
        return
    try:
        _candidate_residue_rename_exchange(
            directory_fd,
            verified_name,
            tombstone_name,
        )
    except OSError:
        pass
    raise OSError("candidate residue exchange identity changed")


def _candidate_residue_unlink_fd_entry(
    directory_fd: int,
    name: str,
) -> None:
    """Remove one entry relative to an already verified directory descriptor."""

    proc_parent = Path("/proc/self/fd") / str(directory_fd)
    if _candidate_residue_identity(
        proc_parent.stat()
    ) != _candidate_residue_identity(os.fstat(directory_fd)):
        raise OSError("directory descriptor identity changed")
    (proc_parent / name).unlink()


def _candidate_residue_replace_with_tombstone(
    directory_fd: int,
    verified_name: str,
    verified_identity: tuple[int, int, int],
    tombstone_name: str,
    tombstone_identity: tuple[int, int, int],
) -> None:
    """Replace the bound entry while leaving its verified inode quarantined."""

    _candidate_residue_exchange_verified_entry(
        directory_fd,
        verified_name,
        verified_identity,
        tombstone_name,
        tombstone_identity,
    )
    try:
        _candidate_residue_unlink_fd_entry(
            directory_fd,
            verified_name,
        )
    except OSError as exc:
        try:
            _candidate_residue_rename_exchange(
                directory_fd,
                verified_name,
                tombstone_name,
            )
        except OSError:
            pass
        raise exc


def _quarantine_candidate_residue_file(
    repo: Path,
    ledger: Mapping[str, Any],
    worktree: Path,
    relative: str,
    expected_sha256: str,
    *,
    allow_missing: bool,
) -> None:
    try:
        normalized = validate_repo_path(relative)
    except ContractError as exc:
        _raise_candidate_residue_drift(
            "candidate residue path is invalid",
            relative,
            exc,
        )
    parts = Path(normalized).parts
    parent_parts = parts[:-1]
    leaf = parts[-1]
    quarantine_dir = _candidate_residue_quarantine_dir(repo, ledger)
    quarantine_name = _candidate_residue_quarantine_name(
        normalized,
        expected_sha256,
    )
    quarantine_path = quarantine_dir / quarantine_name
    parent_fd = -1
    verify_parent_fd = -1
    file_fd = -1
    quarantine_fd = -1
    try:
        parent_fd, parent_identity = _candidate_residue_open_parent(
            worktree,
            parent_parts,
            normalized,
        )
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            file_fd = os.open(leaf, file_flags, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            verify_parent_fd, verify_parent_identity = _candidate_residue_open_parent(
                worktree,
                parent_parts,
                normalized,
            )
            if parent_identity != verify_parent_identity:
                _raise_candidate_residue_drift(
                    "candidate residue parent changed",
                    normalized,
                )
            try:
                os.stat(leaf, dir_fd=verify_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                if allow_missing:
                    if quarantine_path.exists() or quarantine_path.is_symlink():
                        quarantine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                        quarantine_fd = os.open(
                            quarantine_dir,
                            os.O_RDONLY
                            | os.O_DIRECTORY
                            | os.O_NOFOLLOW
                            | os.O_CLOEXEC,
                        )
                        _remove_quarantined_candidate_residue(
                            quarantine_fd,
                            quarantine_name,
                            normalized,
                            expected_sha256,
                        )
                    return
                _raise_candidate_residue_drift(
                    "candidate residue disappeared before deletion",
                    normalized,
                    exc,
                )
            except OSError as verify_exc:
                _raise_candidate_residue_drift(
                    "candidate residue changed before deletion",
                    normalized,
                    verify_exc,
                )
            _raise_candidate_residue_drift(
                "candidate residue changed before deletion",
                normalized,
                exc,
            )
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue changed before deletion",
                normalized,
                exc,
            )
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            _raise_candidate_residue_drift(
                "candidate residue changed before deletion",
                normalized,
            )
        try:
            actual_sha256 = _candidate_residue_fd_sha256(file_fd)
            hashed = os.fstat(file_fd)
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue content changed before deletion",
                normalized,
                exc,
            )
        if (
            _candidate_residue_content_identity(opened)
            != _candidate_residue_content_identity(hashed)
            or actual_sha256 != expected_sha256
        ):
            _raise_candidate_residue_drift(
                "candidate residue content changed before deletion",
                normalized,
            )
        verify_parent_fd, verify_parent_identity = _candidate_residue_open_parent(
            worktree,
            parent_parts,
            normalized,
        )
        if parent_identity != verify_parent_identity:
            _raise_candidate_residue_drift(
                "candidate residue parent changed",
                normalized,
            )
        try:
            named = os.stat(
                leaf,
                dir_fd=verify_parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue changed before deletion",
                normalized,
                exc,
            )
        if _candidate_residue_file_identity(named) != _candidate_residue_file_identity(
            hashed
        ):
            _raise_candidate_residue_drift(
                "candidate residue identity changed before deletion",
                normalized,
            )
        quarantine_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if quarantine_path.exists() or quarantine_path.is_symlink():
            _raise_candidate_residue_drift(
                "candidate residue quarantine entry already exists",
                normalized,
            )
        quarantine_fd = os.open(
            quarantine_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            os.rename(
                leaf,
                quarantine_name,
                src_dir_fd=verify_parent_fd,
                dst_dir_fd=quarantine_fd,
            )
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue changed before deletion",
                normalized,
                exc,
            )
        try:
            quarantined = os.stat(
                quarantine_name,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue quarantine identity is unavailable",
                normalized,
                exc,
            )
        if _candidate_residue_file_identity(
            quarantined
        ) != _candidate_residue_file_identity(hashed):
            try:
                os.link(
                    quarantine_name,
                    leaf,
                    src_dir_fd=quarantine_fd,
                    dst_dir_fd=verify_parent_fd,
                    follow_symlinks=False,
                )
            except OSError:
                pass
            else:
                try:
                    os.unlink(quarantine_name, dir_fd=quarantine_fd)
                except OSError:
                    pass
            _raise_candidate_residue_drift(
                "candidate residue quarantine identity changed",
                normalized,
            )
        _remove_quarantined_candidate_residue(
            quarantine_fd,
            quarantine_name,
            normalized,
            expected_sha256,
        )
        return
    finally:
        for fd in (quarantine_fd, file_fd, verify_parent_fd, parent_fd):
            if fd >= 0:
                os.close(fd)


def _remove_quarantined_candidate_residue(
    quarantine_fd: int,
    quarantine_name: str,
    relative: str,
    expected_sha256: str,
) -> None:
    file_fd = -1
    tombstone_fd = -1
    tombstone_name = f".{quarantine_name}.delete"
    tombstone_identity: tuple[int, int, int] | None = None
    try:
        try:
            tombstone_fd, tombstone_identity = _candidate_residue_create_tombstone(
                quarantine_fd,
                tombstone_name,
            )
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue deletion tombstone is unavailable",
                relative,
                exc,
            )
        try:
            file_fd = os.open(
                quarantine_name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=quarantine_fd,
            )
            value = os.fstat(file_fd)
            if not stat.S_ISREG(value.st_mode):
                _raise_candidate_residue_drift(
                    "candidate residue quarantine entry changed",
                    relative,
                )
            actual_sha256 = _candidate_residue_fd_sha256(file_fd)
            verified = os.fstat(file_fd)
        except AssuranceError:
            raise
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue quarantine entry changed",
                relative,
                exc,
            )
        if (
            _candidate_residue_content_identity(value)
            != _candidate_residue_content_identity(verified)
            or actual_sha256 != expected_sha256
        ):
            _raise_candidate_residue_drift(
                "candidate residue quarantine content changed",
                relative,
            )
        try:
            named = os.stat(
                quarantine_name,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue quarantine entry changed",
                relative,
                exc,
            )
        if _candidate_residue_file_identity(
            named
        ) != _candidate_residue_file_identity(verified):
            _raise_candidate_residue_drift(
                "candidate residue quarantine identity changed",
                relative,
            )
        assert tombstone_identity is not None
        try:
            _candidate_residue_replace_with_tombstone(
                quarantine_fd,
                quarantine_name,
                _candidate_residue_file_identity(verified),
                tombstone_name,
                tombstone_identity,
            )
        except OSError as exc:
            _raise_candidate_residue_drift(
                "candidate residue quarantine entry could not be replaced",
                relative,
                exc,
            )
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if tombstone_fd >= 0:
            os.close(tombstone_fd)


def _candidate_residue_transaction_blockers(
    ledger: Mapping[str, Any],
) -> list[str]:
    blockers = []
    for field in (
        "driver_failure",
        "recomposition_intent",
        "deployment_transaction",
        "pending_blackbox",
        "environment_lease",
        "abandon_intent",
        "finalize_intent",
    ):
        if ledger.get(field) is not None:
            blockers.append(field)
    dispatch = ledger.get("dispatch_intent")
    if isinstance(dispatch, Mapping) and not (
        ledger.get("phase") == "superseded" and dispatch.get("state") == "completed"
    ):
        blockers.append("dispatch_intent")
    supersede = ledger.get("supersede_intent")
    if isinstance(supersede, Mapping) and not (
        ledger.get("phase") == "superseded" and supersede.get("state") == "received"
    ):
        blockers.append("supersede_intent")
    return blockers


def _assert_no_candidate_residue_intent(ledger: Mapping[str, Any]) -> None:
    if ledger.get("candidate_residue_intent") is not None:
        raise AssuranceError(
            "candidate residue recovery is the only allowed run mutation",
            code="CANDIDATE_RESIDUE_TRANSACTION_PENDING",
            status="NEEDS_USER",
        )


def candidate_residue_recovery_action(
    repo_value: str | Path,
    run_value: str,
) -> dict[str, Any] | None:
    """Derive the sole allowed action while residue recovery is persisted."""

    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    intent = ledger.get("candidate_residue_intent")
    if not isinstance(intent, Mapping):
        return None
    action = "resolve_candidate_residue"
    reason = "candidate_residue_recovery_pending"
    payload = {"candidate_residue_intent": copy.deepcopy(dict(intent))}
    action_id = digest(
        {
            "run_id": ledger["run_id"],
            "updated_at": ledger["updated_at"],
            "phase": ledger["phase"],
            "digests": ledger["digests"],
            "action": action,
            "reason": reason,
            "payload": payload,
        }
    )
    runtime = ledger.get("driver_runtime")
    return {
        "driver_protocol_version": 1,
        "status": "NEEDS_USER",
        "run_id": ledger["run_id"],
        "action": action,
        "reason": reason,
        "action_id": action_id,
        "driver_enforced": bool(
            ledger["facets"]["execution"].get("driver_enforced")
        ),
        "driver_runtime_kind": (
            runtime.get("kind") if isinstance(runtime, Mapping) else None
        ),
        **payload,
    }


def _candidate_residue_safety_restore_only(ledger: Mapping[str, Any]) -> bool:
    """Allow only an already-required external safety restore during recovery."""

    transaction = ledger.get("deployment_transaction")
    return isinstance(transaction, Mapping) and transaction.get("state") in {
        "restore_required",
        "restoring",
        "restore_failed",
    }


def _seal_replayed_successor_source(
    repo: Path,
    existing: Mapping[str, Any],
    contract: Mapping[str, Any],
    session_id: str,
) -> None:
    supersedes = contract["mission"].get("supersedes")
    existing_facets = existing.get("facets")
    carryover = (
        existing_facets.get("execution", {}).get("carryover")
        if isinstance(existing_facets, Mapping)
        else None
    )
    if not isinstance(supersedes, Mapping) or not isinstance(carryover, Mapping):
        raise AssuranceError(
            "existing successor continuity metadata is incomplete",
            code="SUPERSEDE_REPLAY_MISMATCH",
            status="NEEDS_USER",
        )
    expected_candidate = str(supersedes["candidate_head"])
    expected_facets = validate_contract(copy.deepcopy(dict(contract)))
    expected_execution = expected_facets["execution"]
    expected_execution["candidate_head"] = expected_candidate
    expected_execution["dirty_snapshot"] = []
    expected_execution["builder_files"] = []
    expected_execution["tester_files"] = []
    expected_execution["tester_source"] = None
    expected_files = _candidate_manifest(
        repo,
        str(existing["target_start_head"]),
        expected_candidate,
    )
    expected_execution["carryover"] = {
        "source_run_id": str(supersedes["run_id"]),
        "source_candidate_head": expected_candidate,
        "files": expected_files,
    }
    candidate_branch = str(existing.get("candidate_branch", ""))
    candidate_worktree_value = existing.get("candidate_worktree")
    candidate_worktree = (
        Path(candidate_worktree_value)
        if isinstance(candidate_worktree_value, str) and candidate_worktree_value
        else None
    )
    branch_result = git(
        repo,
        "rev-parse",
        "--verify",
        f"refs/heads/{candidate_branch}",
        check=False,
    )
    worktree_result = (
        git(candidate_worktree, "rev-parse", "HEAD", check=False)
        if candidate_worktree is not None and candidate_worktree.is_dir()
        else None
    )
    live_target = branch_head(repo, str(existing["target_branch"]))
    if (
        existing.get("owner_session_id") != session_id.strip()
        or existing_facets != expected_facets
        or existing.get("digests") != facet_digests(expected_facets)
        or carryover.get("source_run_id") != supersedes["run_id"]
        or carryover.get("source_candidate_head") != expected_candidate
        or carryover.get("files") != expected_files
        or candidate_branch != f"assurance-v4/{existing['run_id']}/candidate"
        or branch_result.returncode != 0
        or branch_result.stdout.strip() != expected_candidate
        or worktree_result is None
        or worktree_result.returncode != 0
        or worktree_result.stdout.strip() != expected_candidate
        or candidate_worktree is None
        or dirty_paths(candidate_worktree)
        or live_target != existing.get("target_start_head")
    ):
        raise AssuranceError(
            "existing successor does not match the persisted continuity receipt",
            code="SUPERSEDE_REPLAY_MISMATCH",
            status="NEEDS_USER",
        )
    source = read_ledger(repo, str(supersedes["run_id"]))
    expected_intent = {
        "source_run_id": str(supersedes["run_id"]),
        "target_run_id": str(existing["run_id"]),
        "state": "prepared",
    }
    received_intent = {**expected_intent, "state": "received"}
    if source["phase"] == "superseded" and source.get("supersede_intent") == received_intent:
        return
    if source["phase"] != "active" or source.get("supersede_intent") != expected_intent:
        raise AssuranceError(
            "source run changed before successor replay could seal continuity",
            code="SUPERSEDE_REPLAY_SOURCE_DRIFT",
            status="NEEDS_USER",
        )
    source_lease = source.get("environment_lease")
    if isinstance(source_lease, Mapping) and source_lease.get("state") == "held":
        return
    source["phase"] = "superseded"
    source["supersede_intent"] = received_intent
    append_event(source, "run_superseded", {"target_run_id": existing["run_id"]})
    save_ledger(repo, source)


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


def _cost_ancestry_consumers(
    repo: Path, source_run_id: str, *, exclude_run_id: str
) -> list[str]:
    consumers: list[str] = []
    runs_root = state_root(repo) / "runs"
    if not runs_root.is_dir():
        return consumers
    for path in sorted(runs_root.glob("*/ledger.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        run_id = value.get("run_id")
        ancestry = value.get("facets", {}).get("execution", {}).get("cost_ancestry")
        if (
            isinstance(run_id, str)
            and run_id != exclude_run_id
            and isinstance(ancestry, Mapping)
            and ancestry.get("source_run_id") == source_run_id
        ):
            consumers.append(run_id)
    return consumers


def _validate_cost_ancestry_start(
    repo: Path,
    run_id: str,
    contract: Mapping[str, Any],
) -> dict[str, Any] | None:
    ancestry = contract["execution"].get("cost_ancestry")
    if not isinstance(ancestry, Mapping):
        return None
    source_run_id = str(ancestry["source_run_id"])
    if source_run_id == run_id:
        raise AssuranceError(
            "cost ancestry cannot point to the new root itself",
            code="COST_ANCESTRY_CYCLE",
            status="NEEDS_USER",
        )
    source = read_ledger(repo, source_run_id)
    if source.get("phase") not in {"failed", "abandoned"}:
        raise AssuranceError(
            "cost ancestry requires one failed or abandoned source",
            code="COST_ANCESTRY_SOURCE_PHASE_INVALID",
            status="NEEDS_USER",
            details={"source_run_id": source_run_id, "phase": source.get("phase")},
        )
    if source.get("target_branch") != contract["authority"]["target_branch"]:
        raise AssuranceError(
            "cost ancestry cannot change the target branch",
            code="COST_ANCESTRY_TARGET_MISMATCH",
            status="NEEDS_USER",
        )
    consumers = _cost_ancestry_consumers(
        repo, source_run_id, exclude_run_id=run_id
    )
    if consumers:
        raise AssuranceError(
            "cost ancestry source already has a recovery-root consumer",
            code="COST_ANCESTRY_FORK",
            status="NEEDS_USER",
            details={"source_run_id": source_run_id, "consumers": consumers},
        )
    source_lineage = _derive_lineage(repo, source)
    if not source_lineage["complete"]:
        raise AssuranceError(
            "cost ancestry source lineage is incomplete",
            code="COST_ANCESTRY_SOURCE_INCOMPLETE",
            status="NEEDS_USER",
        )
    if (
        ancestry.get("source_lineage_digest") != source_lineage["lineage_digest"]
        or ancestry.get("source_task_pressure_digest")
        != source_lineage["task_pressure_digest"]
    ):
        raise AssuranceError(
            "cost ancestry does not bind the current source lineage",
            code="COST_ANCESTRY_BINDING_STALE",
            status="NEEDS_USER",
        )
    if _mission_semantics_value(source["facets"]["mission"]) != _mission_semantics_value(
        contract["mission"]
    ):
        raise AssuranceError(
            "cost ancestry cannot cross Mission semantics",
            code="COST_ANCESTRY_SEMANTICS_MISMATCH",
            status="NEEDS_USER",
        )
    if any(
        item.get("source_run_id") == run_id
        for item in source_lineage.get("cost_ancestry", [])
        if isinstance(item, Mapping)
    ):
        raise AssuranceError(
            "cost ancestry contains a cycle",
            code="COST_ANCESTRY_CYCLE",
            status="NEEDS_USER",
        )
    return source_lineage


def _merge_cumulative_telemetry(*values: Mapping[str, Any]) -> dict[str, Any]:
    stages: dict[str, dict[str, Any]] = {}
    evidence_attempts = {kind: 0 for kind in EVIDENCE_KINDS}
    retry_codes: dict[str, int] = {}
    elapsed_ms = 0
    candidate_changes = 0
    for value in values:
        elapsed_ms += int(value.get("elapsed_ms", 0))
        candidate_changes += int(value.get("candidate_changes", 0))
        for kind, count in value.get("evidence_attempts", {}).items():
            evidence_attempts[str(kind)] = evidence_attempts.get(str(kind), 0) + int(count)
        for code, count in value.get("retries", {}).get("by_failure_code", {}).items():
            retry_codes[str(code)] = retry_codes.get(str(code), 0) + int(count)
        for item in value.get("stages", []):
            if not isinstance(item, Mapping):
                continue
            name = str(item["name"])
            target = stages.setdefault(
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
            for field in (
                "attempts",
                "completed_attempts",
                "failed_attempts",
                "retry_count",
                "total_duration_ms",
            ):
                target[field] += int(item.get(field, 0))
            if item.get("last_failure_code") is not None:
                target["last_failure_code"] = item["last_failure_code"]
    return {
        "elapsed_ms": elapsed_ms,
        "stages": [stages[name] for name in sorted(stages)],
        "candidate_changes": candidate_changes,
        "evidence_attempts": evidence_attempts,
        "evidence_replays": sum(max(0, count - 1) for count in evidence_attempts.values()),
        "retries": {
            "total": sum(retry_codes.values()),
            "by_failure_code": dict(sorted(retry_codes.items())),
        },
    }


def _mission_semantics_value(mission: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(mission.get(key))
        for key in (
            "delivery_kind",
            "objective",
            "behaviors",
            "interfaces",
            "acceptance_cases",
            "trust_boundaries",
        )
    }


def _derive_lineage(
    repo: Path,
    current: Mapping[str, Any],
    *,
    _task_seen: set[str] | None = None,
) -> dict[str, Any]:
    run_id = str(current["run_id"])
    ledgers, complete = _lineage_ledgers(repo, current)
    lineage_run_ids = {str(item["run_id"]) for item in ledgers}
    task_seen = set(_task_seen or set())
    if task_seen & lineage_run_ids:
        raise AssuranceError(
            "task cost ancestry contains a cycle",
            code="COST_ANCESTRY_CYCLE",
            status="NEEDS_USER",
        )
    task_seen.update(lineage_run_ids)
    transitions: list[dict[str, Any]] = []
    disposition_counts = {"included": 0, "handled_elsewhere": 0, "discarded": 0}
    telemetry_by_run: dict[str, dict[str, Any]] = {}
    for ledger in ledgers:
        revision = int(ledger["facets"]["mission"]["revision"])
        recorded = ledger.get("revision_transitions")
        if revision > 1 and not isinstance(
            ledger["facets"]["execution"].get("revision_transition"), dict
        ):
            complete = False
        if recorded is None:
            recorded = []
        transitions.extend(copy.deepcopy(recorded))
        telemetry_by_run[str(ledger["run_id"])] = telemetry(ledger)
        for item in ledger.get("problem_dispositions", []):
            disposition = item.get("disposition")
            if disposition in disposition_counts:
                disposition_counts[disposition] += 1
    transitions.sort(
        key=lambda item: (
            item["to_revision"],
            item["source_run_id"],
            item["target_run_id"],
        )
    )
    semantic = sum(1 for item in transitions if item["semantic"])
    categories: dict[str, int] = {}
    for item in transitions:
        categories[item["category"]] = categories.get(item["category"], 0) + 1
    non_semantic = len(transitions) - semantic
    cumulative = _merge_cumulative_telemetry(*telemetry_by_run.values())

    root = ledgers[0]
    ancestry_input = root["facets"]["execution"].get("cost_ancestry")
    source_lineage: dict[str, Any] | None = None
    cost_ancestry: list[dict[str, Any]] = []
    if isinstance(ancestry_input, Mapping):
        source_run_id = str(ancestry_input["source_run_id"])
        if source_run_id in task_seen:
            raise AssuranceError(
                "task cost ancestry contains a cycle",
                code="COST_ANCESTRY_CYCLE",
                status="NEEDS_USER",
            )
        source = read_ledger(repo, source_run_id)
        if source.get("phase") not in {"failed", "abandoned"}:
            raise AssuranceError(
                "cost ancestry source is no longer an eligible terminal root",
                code="COST_ANCESTRY_SOURCE_PHASE_INVALID",
                status="NEEDS_USER",
                details={"source_run_id": source_run_id, "phase": source.get("phase")},
            )
        source_lineage = _derive_lineage(repo, source, _task_seen=task_seen)
        if (
            ancestry_input.get("source_lineage_digest")
            != source_lineage["lineage_digest"]
            or ancestry_input.get("source_task_pressure_digest")
            != source_lineage["task_pressure_digest"]
        ):
            raise AssuranceError(
                "cost ancestry binding is stale",
                code="COST_ANCESTRY_BINDING_STALE",
                status="NEEDS_USER",
            )
        cost_ancestry = [
            *copy.deepcopy(source_lineage["cost_ancestry"]),
            {
                "source_run_id": source_run_id,
                "target_run_id": str(root["run_id"]),
                "source_lineage_digest": ancestry_input["source_lineage_digest"],
                "source_task_pressure_digest": ancestry_input[
                    "source_task_pressure_digest"
                ],
            },
        ]

    source_task_categories = (
        copy.deepcopy(source_lineage["task_transition_category_counts"])
        if source_lineage is not None
        else {}
    )
    task_categories = dict(source_task_categories)
    for category, count in categories.items():
        task_categories[category] = task_categories.get(category, 0) + count
    source_task_non_semantic = (
        int(source_lineage["task_non_semantic_transition_count"])
        if source_lineage is not None
        else 0
    )
    task_non_semantic = source_task_non_semantic + non_semantic
    task_transition_count = (
        int(source_lineage["task_transition_count"])
        if source_lineage is not None
        else 0
    ) + len(transitions)
    task_revision_count = (
        int(source_lineage["task_revision_count"])
        if source_lineage is not None
        else 0
    ) + int(current["facets"]["mission"]["revision"])
    task_cumulative = _merge_cumulative_telemetry(
        source_lineage["task_cumulative_telemetry"]
        if source_lineage is not None
        else {},
        cumulative,
    )
    mission_semantics_digest = digest(
        _mission_semantics_value(current["facets"]["mission"])
    )
    continuation_grant = (
        copy.deepcopy(source_lineage.get("continuation_grant"))
        if source_lineage is not None
        else None
    )
    running_non_semantic = source_task_non_semantic
    for item in transitions:
        if item["semantic"]:
            continuation_grant = None
            continue
        running_non_semantic += 1
        if isinstance(continuation_grant, Mapping) and (
            continuation_grant.get("category") != item["category"]
            or running_non_semantic
            > int(continuation_grant.get("authorized_through_count", -1))
        ):
            continuation_grant = None
        recorded_grant = item.get("continuation_grant")
        if isinstance(recorded_grant, Mapping):
            continuation_grant = copy.deepcopy(dict(recorded_grant))
    if isinstance(continuation_grant, Mapping) and (
        continuation_grant.get("mission_semantics_digest")
        != mission_semantics_digest
        or task_non_semantic
        > int(continuation_grant.get("authorized_through_count", -1))
    ):
        continuation_grant = None

    task_root_run_id = (
        str(source_lineage["task_root_run_id"])
        if source_lineage is not None
        else str(root["run_id"])
    )
    task_pressure_value = {
        "task_root_run_id": task_root_run_id,
        "current_run_id": run_id,
        "complete": complete and (source_lineage is None or source_lineage["complete"]),
        "mission_semantics_digest": mission_semantics_digest,
        "non_semantic": task_non_semantic,
        "by_category": {
            key: value
            for key, value in sorted(task_categories.items())
            if key != "mission_change"
        },
        "cost_ancestry": cost_ancestry,
        "continuation_grant": continuation_grant,
    }
    task_pressure_digest = digest(task_pressure_value)
    lineage_digest = digest(
        {
            "source_lineage_digest": (
                source_lineage["lineage_digest"] if source_lineage is not None else None
            ),
            "cost_ancestry": cost_ancestry,
            "runs": [
                {
                    "run_id": item["run_id"],
                    "mission_digest": item["digests"]["mission"],
                    "candidate_head": item["facets"]["execution"].get(
                        "candidate_head"
                    ),
                    "revision_transitions": item.get("revision_transitions"),
                    "problem_dispositions": item.get("problem_dispositions"),
                    "open_problem_snapshot": digest(_problem_snapshot_value(item)),
                    "telemetry": {
                        key: copy.deepcopy(
                            telemetry_by_run[str(item["run_id"])].get(key)
                        )
                        for key in (
                            "stages",
                            "candidate_changes",
                            "evidence_attempts",
                            "evidence_replays",
                            "retries",
                            "lifecycle",
                            "profile",
                        )
                    },
                }
                for item in ledgers
            ],
        }
    )
    lineage_complete = complete and (
        source_lineage is None or bool(source_lineage["complete"])
    )
    grant_covers_pressure = (
        isinstance(continuation_grant, Mapping)
        and continuation_grant.get("mission_semantics_digest")
        == mission_semantics_digest
        and task_non_semantic
        <= int(continuation_grant.get("authorized_through_count", -1))
    )
    review_required = not lineage_complete or (
        (
            task_non_semantic >= 3
            or any(
                count >= 3
                for category, count in task_categories.items()
                if category != "mission_change"
            )
        )
        and not grant_covers_pressure
    )
    current_problem_snapshot = _problem_snapshot_value(current)
    value = {
        "schema_version": 1,
        "root_run_id": root["run_id"],
        "current_run_id": run_id,
        "complete": lineage_complete,
        "health": (
            "incomplete"
            if not lineage_complete
            else "review_required"
            if review_required
            else "healthy"
        ),
        "revision_count": int(current["facets"]["mission"]["revision"]),
        "transitions": transitions,
        "transition_count": len(transitions),
        "non_semantic_transition_count": non_semantic,
        "transition_category_counts": dict(sorted(categories.items())),
        "cumulative_telemetry": cumulative,
        "task_root_run_id": task_root_run_id,
        "cost_ancestry": cost_ancestry,
        "task_revision_count": task_revision_count,
        "task_transition_count": task_transition_count,
        "task_non_semantic_transition_count": task_non_semantic,
        "task_transition_category_counts": dict(sorted(task_categories.items())),
        "task_cumulative_telemetry": task_cumulative,
        "task_pressure_digest": task_pressure_digest,
        "continuation_grant": copy.deepcopy(continuation_grant),
        "problem_disposition_counts": disposition_counts,
        "open_problem_snapshot_digest": digest(current_problem_snapshot),
        "open_problem_keys": [item["key"] for item in current_problem_snapshot],
        "lineage_digest": lineage_digest,
        "pressure_digest": task_pressure_digest,
    }
    return validate_lineage(value)


def lineage(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    return _derive_lineage(repo, read_ledger(repo, run_id))


def runtime_compatibility(ledger: Mapping[str, Any]) -> dict[str, Any]:
    from ..core import runtime_identity_compatibility

    return runtime_identity_compatibility(ledger["runtime_identity"])


def _validate_revision_transition(
    source_lineage: Mapping[str, Any], transition: Mapping[str, Any]
) -> None:
    if (
        transition.get("predecessor_pressure_digest")
        != source_lineage["task_pressure_digest"]
    ):
        raise AssuranceError(
            "revision transition does not bind the current task pressure",
            code="REVISION_LINEAGE_DIGEST_MISMATCH",
            status="NEEDS_USER",
        )
    category = transition.get("category")
    semantic = category == "mission_change"
    decision = transition.get("architecture_review")
    valid_decision = (
        isinstance(decision, dict)
        and decision.get("decision") == "continue"
        and decision.get("pressure_digest")
        == source_lineage["task_pressure_digest"]
    )
    if semantic:
        if not source_lineage["complete"]:
            if not valid_decision:
                raise AssuranceError(
                    "incomplete revision lineage requires an architecture-review continuation decision",
                    code="LINEAGE_ARCHITECTURE_REVIEW_REQUIRED",
                    status="NEEDS_USER",
                    details={
                        "pressure_digest": source_lineage[
                            "task_pressure_digest"
                        ],
                        "task_cost_summary": {
                            "task_root_run_id": source_lineage[
                                "task_root_run_id"
                            ],
                            "task_revision_count": source_lineage[
                                "task_revision_count"
                            ],
                            "task_transition_count": source_lineage[
                                "task_transition_count"
                            ],
                            "task_non_semantic_transition_count": source_lineage[
                                "task_non_semantic_transition_count"
                            ],
                            "task_transition_category_counts": source_lineage[
                                "task_transition_category_counts"
                            ],
                            "task_cumulative_telemetry": source_lineage[
                                "task_cumulative_telemetry"
                            ],
                            "proposed_category": category,
                            "proposed_non_semantic_count": source_lineage[
                                "task_non_semantic_transition_count"
                            ],
                            "continuation_window": NON_SEMANTIC_CONTINUATION_WINDOW,
                        },
                    },
                )
            return
        if decision is not None:
            raise AssuranceError(
                "a semantic transition cannot consume a non-semantic continuation grant",
                code="LINEAGE_CONTINUATION_DECISION_UNEXPECTED",
                status="NEEDS_USER",
            )
        return
    proposed_non_semantic = int(
        source_lineage["task_non_semantic_transition_count"]
    ) + 1
    proposed_category = int(
        source_lineage["task_transition_category_counts"].get(category, 0)
    ) + 1
    grant = source_lineage.get("continuation_grant")
    grant_applies = (
        isinstance(grant, Mapping)
        and grant.get("category") == category
        and proposed_non_semantic
        <= int(grant.get("authorized_through_count", -1))
    )
    requires_review = (
        not source_lineage["complete"]
        or proposed_non_semantic >= 3
        or proposed_category >= 3
    ) and not grant_applies
    if requires_review and not valid_decision:
        raise AssuranceError(
            "revision lineage pressure requires an architecture-review continuation decision",
            code="LINEAGE_ARCHITECTURE_REVIEW_REQUIRED",
            status="NEEDS_USER",
            details={
                "pressure_digest": source_lineage["task_pressure_digest"],
                "task_cost_summary": {
                    "task_root_run_id": source_lineage["task_root_run_id"],
                    "task_revision_count": source_lineage["task_revision_count"],
                    "task_transition_count": source_lineage[
                        "task_transition_count"
                    ],
                    "task_non_semantic_transition_count": source_lineage[
                        "task_non_semantic_transition_count"
                    ],
                    "task_transition_category_counts": source_lineage[
                        "task_transition_category_counts"
                    ],
                    "task_cumulative_telemetry": source_lineage[
                        "task_cumulative_telemetry"
                    ],
                    "proposed_category": category,
                    "proposed_non_semantic_count": proposed_non_semantic,
                    "continuation_window": NON_SEMANTIC_CONTINUATION_WINDOW,
                },
            },
        )
    if isinstance(decision, dict) and (not valid_decision or not requires_review):
        raise AssuranceError(
            "architecture-review decision is stale or not required for the current task pressure",
            code="LINEAGE_ARCHITECTURE_REVIEW_REQUIRED",
            status="NEEDS_USER",
        )


def _recorded_transition(
    transition: Mapping[str, Any], *, source_run: str, target_run: str,
    from_revision: int, to_revision: int, source_lineage: Mapping[str, Any],
    mission_semantics_digest: str,
) -> dict[str, Any]:
    value = {
        "source_run_id": source_run,
        "target_run_id": target_run,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "category": transition["category"],
        "semantic": transition["category"] == "mission_change",
        "predecessor_pressure_digest": transition["predecessor_pressure_digest"],
        "architecture_review": copy.deepcopy(transition.get("architecture_review")),
    }
    decision = transition.get("architecture_review")
    if transition["category"] != "mission_change" and isinstance(decision, Mapping):
        granted_at = int(source_lineage["task_non_semantic_transition_count"]) + 1
        value["continuation_grant"] = {
            "category": transition["category"],
            "granted_at_count": granted_at,
            "authorized_through_count": (
                granted_at + NON_SEMANTIC_CONTINUATION_WINDOW
            ),
            "pressure_digest": source_lineage["task_pressure_digest"],
            "mission_semantics_digest": mission_semantics_digest,
        }
    return value


def _target_contenders(repo: Path, ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    runs_root = state_root(repo) / "runs"
    if not runs_root.is_dir():
        return values
    for path in sorted(runs_root.glob("*/ledger.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            raw.get("run_id") == ledger.get("run_id")
            or raw.get("target_branch") != ledger.get("target_branch")
            or raw.get("phase") not in {"active", "finalizing"}
        ):
            continue
        execution = raw.get("facets", {}).get("execution", {})
        values.append(
            {
                "run_id": raw.get("run_id"),
                "phase": raw.get("phase"),
                "target_start_head": raw.get("target_start_head"),
                "candidate_head": execution.get("candidate_head"),
            }
        )
    return values


def status(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    public_status = (
        "FATAL"
        if ledger["phase"] == "failed"
        else "READY"
        if readiness(ledger)["ready"]
        else "ACTIVE"
    )
    telemetry_value = telemetry(ledger)
    return {
        "status": public_status,
        "run_id": run_id,
        "runtime_identity": copy.deepcopy(ledger["runtime_identity"]),
        "runtime_compatibility": runtime_compatibility(ledger),
        "runtime_support": copy.deepcopy(ledger["runtime_support"]),
        "phase": ledger["phase"],
        "repo_root": ledger["repo_root"],
        "target_branch": ledger["target_branch"],
        "target_start_head": ledger["target_start_head"],
        "target_contenders": _target_contenders(repo, ledger),
        "candidate_branch": ledger["candidate_branch"],
        "candidate_worktree": ledger["candidate_worktree"],
        "digests": ledger["digests"],
        "mission_revision": ledger["facets"]["mission"]["revision"],
        "recovery_policy": recovery_policy(ledger["facets"]),
        "builder_checkpointed": ledger.get("builder_checkpointed", False),
        "driver_runtime": copy.deepcopy(ledger.get("driver_runtime")),
        "transport_cleanup_intent": copy.deepcopy(
            ledger.get("transport_cleanup_intent")
        ),
        "deferred_wait": copy.deepcopy(ledger.get("deferred_wait")),
        "driver_failure": copy.deepcopy(ledger.get("driver_failure")),
        "dispatch_intent": copy.deepcopy(ledger.get("dispatch_intent")),
        "tester_replacement_intent": copy.deepcopy(
            ledger.get("tester_replacement_intent")
        ),
        "proof_failure": copy.deepcopy(ledger.get("proof_failure")),
        "proof_failure_state": proof_failure_state(ledger),
        "machine_failure": copy.deepcopy(ledger.get("machine_failure")),
        "machine_failure_state": machine_failure_state(ledger),
        "recomposition_intent": copy.deepcopy(ledger.get("recomposition_intent")),
        "candidate_residue_intent": copy.deepcopy(ledger.get("candidate_residue_intent")),
        "deployment_transaction": copy.deepcopy(ledger.get("deployment_transaction")),
        "pending_blackbox": copy.deepcopy(ledger.get("pending_blackbox")),
        "doc_reference_contract_version": ledger.get("doc_reference_contract_version"),
        "doc_reference_scan": copy.deepcopy(ledger.get("doc_reference_scan")),
        "doc_reference_scan_state": doc_reference_scan_state(ledger),
        "environment_lease": copy.deepcopy(ledger.get("environment_lease")),
        "supersede_intent": copy.deepcopy(ledger.get("supersede_intent")),
        "abandon_intent": copy.deepcopy(ledger.get("abandon_intent")),
        "profile": copy.deepcopy(telemetry_value["profile"]),
        "expected_stages": copy.deepcopy(telemetry_value["expected_stages"]),
        "duration_breakdown": copy.deepcopy(
            telemetry_value["duration_breakdown"]
        ),
        "telemetry": telemetry_value,
        "lineage": _derive_lineage(repo, ledger),
        "readiness": readiness(ledger),
        "publication": copy.deepcopy(ledger.get("publication")),
        "problems": copy.deepcopy(ledger.get("problems", [])),
    }


def _proof_test_run_view(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    fields = (
        "argv",
        "returncode",
        "timed_out",
        "test_result",
        "duration_ms",
        "log_path",
        "log_sha256",
        "log_tail",
        "worktree_residue",
    )
    if any(field not in value for field in fields) or value.get("worktree_residue") != []:
        return None
    return {field: copy.deepcopy(value[field]) for field in fields}


@lru_cache(maxsize=1)
def _proof_evidence_schema() -> dict[str, Any]:
    return json.loads((schema_root() / "codex-test-proof.schema.json").read_text())


@lru_cache(maxsize=None)
def _proof_evidence_validator(definition: str) -> jsonschema.Draft202012Validator:
    schema = _proof_evidence_schema()
    return jsonschema.Draft202012Validator(
        {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            "$ref": f"#/$defs/{definition}",
        }
    )


def _proof_group_evidence_valid(value: Any) -> bool:
    return _proof_evidence_validator("proofGroupEvidence").is_valid(value)


def _proof_git_quoted_path(value: str, start: int) -> tuple[str, int] | None:
    if start >= len(value) or value[start] != '"':
        return None
    decoded = bytearray()
    index = start + 1
    escapes = {
        "a": 7,
        "b": 8,
        "t": 9,
        "n": 10,
        "v": 11,
        "f": 12,
        "r": 13,
        '"': 34,
        "\\": 92,
    }
    while index < len(value):
        character = value[index]
        if character == '"':
            try:
                path = decoded.decode("utf-8")
            except UnicodeDecodeError:
                return None
            return (path, index + 1) if "\0" not in path else None
        if character != "\\":
            decoded.extend(character.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(value):
            return None
        escaped = value[index]
        if escaped in escapes:
            decoded.append(escapes[escaped])
            index += 1
            continue
        octal = value[index : index + 3]
        if len(octal) != 3 or any(item not in "01234567" for item in octal):
            return None
        byte = int(octal, 8)
        if byte > 255:
            return None
        decoded.append(byte)
        index += 3
    return None


def _proof_diff_header_path(line: str) -> str | None:
    prefix = "diff --git "
    if not line.startswith(prefix):
        return None
    value = line[len(prefix) :]
    if value.startswith('"'):
        parsed_left = _proof_git_quoted_path(value, 0)
        if parsed_left is None:
            return None
        left, index = parsed_left
        if value[index : index + 1] != " ":
            return None
        parsed_right = _proof_git_quoted_path(value, index + 1)
        if parsed_right is None or parsed_right[1] != len(value):
            return None
        right = parsed_right[0]
    else:
        if len(value) < 6 or len(value) % 2 == 0:
            return None
        middle = len(value) // 2
        if value[middle] != " ":
            return None
        left = value[:middle]
        right = value[middle + 1 :]
    if (
        not left.startswith("a/")
        or not right.startswith("b/")
        or left[2:] != right[2:]
    ):
        return None
    path = left[2:]
    try:
        normalized = validate_repo_path(path)
    except ContractError:
        return None
    return path if normalized == path else None


def _proof_applied_diff_paths(value: Any) -> list[str] | None:
    if not isinstance(value, str) or not value.startswith("diff --git "):
        return None
    paths: list[str] = []
    for line in value.splitlines():
        if not line.startswith("diff --git "):
            continue
        path = _proof_diff_header_path(line)
        if path is None:
            return None
        paths.append(path)
    if not paths or len(paths) != len(set(paths)) or paths != sorted(paths):
        return None
    return paths


def _proof_reviewed_boundary_ids(value: Any) -> set[str] | None:
    boundaries = value.get("reviewed_boundaries") if isinstance(value, Mapping) else None
    categories = {
        "positive_test_ids",
        "negative_test_ids",
        "boundary_test_ids",
        "invariant_test_ids",
    }
    if not isinstance(boundaries, Mapping) or set(boundaries) != categories:
        return None
    observed: set[str] = set()
    for category in categories:
        test_ids = boundaries.get(category)
        if (
            not isinstance(test_ids, list)
            or not test_ids
            or any(not isinstance(test_id, str) or not test_id.strip() for test_id in test_ids)
            or len(test_ids) != len(set(test_ids))
        ):
            return None
        observed.update(test_ids)
    return observed


def _proof_pass_counts_bound(value: Any, test_ids: Any) -> bool:
    allowed = {
        "passed",
        "failed",
        "failures",
        "errors",
        "skipped",
        "xfailed",
        "xpassed",
    }
    if (
        not isinstance(value, Mapping)
        or not isinstance(test_ids, list)
        or not test_ids
        or any(
            key not in allowed
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for key, count in value.items()
        )
    ):
        return False
    return value.get("passed") == len(test_ids) and all(
        count == 0 for key, count in value.items() if key != "passed"
    )


def _proof_supervisor_observation_bound(
    value: Any,
    *,
    framework: Any,
    returncode: Any,
    test_ids: Any,
    test_result: Any,
) -> bool:
    tests = value.get("tests") if isinstance(value, Mapping) else None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"schema_version", "framework", "exitstatus", "tests"}
        or value.get("schema_version") != 1
        or value.get("framework") != framework
        or value.get("exitstatus") != returncode
        or not isinstance(tests, list)
        or not isinstance(test_ids, list)
        or [item.get("id") if isinstance(item, Mapping) else None for item in tests]
        != test_ids
        or any(
            not isinstance(item, Mapping)
            or set(item) != {"id", "outcome", "failure_kind", "counts"}
            for item in tests
        )
        or not isinstance(test_result, Mapping)
    ):
        return False
    from ..core import classify_structured_proof_test_result

    classified = classify_structured_proof_test_result(
        [value],
        framework=str(framework),
        returncode=returncode,
        test_ids=test_ids,
        fallback={
            "framework": str(framework),
            "classification": "unclassified-failure",
            "counts": {},
        },
    )
    return classified == test_result


def _proof_tester_sources_bound(
    repo: Path,
    *,
    source_head: str,
    candidate_head: str,
    framework: str,
    test_ids: Any,
    tester_patterns: Sequence[str],
    tester_paths: Sequence[Mapping[str, str]],
) -> bool:
    if (
        not re.fullmatch(r"[0-9a-f]{40}", source_head)
        or not re.fullmatch(r"[0-9a-f]{40}", candidate_head)
        or framework not in {"unittest", "pytest"}
        or not isinstance(test_ids, list)
        or not test_ids
    ):
        return False
    manifest: dict[str, str] = {}
    for item in tester_paths:
        path = item.get("path") if isinstance(item, Mapping) else None
        blob = item.get("blob") if isinstance(item, Mapping) else None
        if (
            not isinstance(path, str)
            or not isinstance(blob, str)
            or not re.fullmatch(r"[0-9a-f]{40}", blob)
            or path in manifest
        ):
            return False
        manifest[path] = blob
    try:
        from ..core import proof_test_source_path

        for test_id in test_ids:
            if not isinstance(test_id, str) or not test_id.strip():
                return False
            path = proof_test_source_path(
                repo,
                source_head,
                framework,
                test_id,
                tester_patterns,
            )
            blob = manifest.get(path) if path is not None else None
            if (
                path is None
                or blob is None
                or _regular_blob_at(repo, source_head, path) != blob
                or _regular_blob_at(repo, candidate_head, path) != blob
            ):
                return False
    except Exception:
        return False
    return True


def _proof_repository_inputs_bound(
    repo: Path,
    *,
    requested_argv: Any,
    executable_identity: Any,
    project_identity: Any,
    heads: Sequence[str],
) -> set[str] | None:
    if (
        not isinstance(requested_argv, list)
        or not requested_argv
        or not isinstance(requested_argv[0], str)
        or not isinstance(executable_identity, Mapping)
        or executable_identity.get("requested") != requested_argv[0]
        or not heads
        or any(not re.fullmatch(r"[0-9a-f]{40}", head) for head in heads)
    ):
        return None
    protected: set[str] = set()
    kind = executable_identity.get("kind")
    if kind == "repository":
        path = executable_identity.get("path")
        blob = executable_identity.get("blob")
        try:
            normalized = validate_repo_path(path) if isinstance(path, str) else None
        except ContractError:
            return None
        if (
            project_identity is not None
            or normalized != path
            or executable_identity.get("requested") != path
            or not isinstance(blob, str)
            or not re.fullmatch(r"[0-9a-f]{40}", blob)
            or any(_regular_blob_at(repo, head, path) != blob for head in heads)
        ):
            return None
        protected.add(path)
        return protected
    if kind != "system":
        return None
    if Path(requested_argv[0]).name.lower() != "uv":
        return protected if project_identity is None else None
    files = project_identity.get("files") if isinstance(project_identity, Mapping) else None
    expected_paths = ["pyproject.toml", "uv.lock"]
    if (
        not isinstance(project_identity, Mapping)
        or set(project_identity) != {"files"}
        or not isinstance(files, list)
        or len(files) != len(expected_paths)
        or [item.get("path") if isinstance(item, Mapping) else None for item in files]
        != expected_paths
    ):
        return None
    for item, path in zip(files, expected_paths):
        if not isinstance(item, Mapping) or set(item) != {"path", "blob", "sha256"}:
            return None
        blob = item.get("blob")
        sha256 = item.get("sha256")
        if (
            not isinstance(blob, str)
            or not re.fullmatch(r"[0-9a-f]{40}", blob)
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            return None
        for head in heads:
            observed = _regular_blob_sha256_at(repo, head, path)
            if observed != (blob, sha256):
                return None
        protected.add(path)
    return protected


def _proof_canonical_mutation(
    repo: Path,
    candidate_head: str,
    patch: Any,
) -> tuple[str, list[str]] | None:
    if (
        not re.fullmatch(r"[0-9a-f]{40}", candidate_head)
        or not isinstance(patch, str)
        or not patch.strip()
    ):
        return None
    common = git(repo, "rev-parse", "--git-common-dir", check=False)
    if common.returncode != 0 or not common.stdout.strip():
        return None
    common_path = Path(common.stdout.strip())
    if not common_path.is_absolute():
        common_path = (repo / common_path).resolve()
    source_objects = common_path / "objects"
    if not source_objects.is_dir():
        return None
    with tempfile.TemporaryDirectory(prefix="assurance-v4-proof-replay-") as raw:
        root = Path(raw)
        object_directory = root / "objects"
        object_directory.mkdir()
        env = os.environ.copy()
        for key in (
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_QUARANTINE_PATH",
            "GIT_WORK_TREE",
        ):
            env.pop(key, None)
        alternates = [str(source_objects)]
        inherited_alternates = env.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
        if inherited_alternates:
            alternates.append(inherited_alternates)
        env.update(
            {
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.pathsep.join(alternates),
                "GIT_INDEX_FILE": str(root / "index"),
                "GIT_OBJECT_DIRECTORY": str(object_directory),
                "GIT_OPTIONAL_LOCKS": "0",
            }
        )

        def run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )

        if run("read-tree", candidate_head).returncode != 0:
            return None
        if (
            run(
                "apply",
                "--cached",
                "--binary",
                "--whitespace=nowarn",
                "-",
                input_text=patch,
            ).returncode
            != 0
        ):
            return None
        names = run("diff", "--cached", "--name-only", "-z", candidate_head, "--")
        if names.returncode != 0 or not names.stdout.endswith("\0"):
            return None
        paths = names.stdout[:-1].split("\0")
        if not paths or paths != sorted(set(paths)):
            return None
        for path in paths:
            staged = run("ls-files", "--stage", "-z", "--", f":(literal){path}")
            if (
                staged.returncode != 0
                or not staged.stdout.endswith("\0")
                or staged.stdout.count("\0") != 1
            ):
                return None
            metadata, separator, listed_path = staged.stdout[:-1].partition("\t")
            fields = metadata.split()
            if (
                separator != "\t"
                or listed_path != path
                or len(fields) != 3
                or fields[0] not in {"100644", "100755"}
                or fields[2] != "0"
            ):
                return None
        diff = run(
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            "--full-index",
            candidate_head,
            "--",
        )
        if diff.returncode != 0 or not diff.stdout:
            return None
        return diff.stdout, paths


def _proof_project_paths(value: Any) -> list[dict[str, str]] | None:
    files = value.get("files") if isinstance(value, Mapping) else None
    if not isinstance(files, list) or not files:
        return None
    paths: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, Mapping):
            return None
        path = item.get("path")
        blob = item.get("blob")
        sha256 = item.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(blob, str)
            or not re.fullmatch(r"[0-9a-f]{40}", blob)
            or not isinstance(sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        ):
            return None
        paths.append({"path": path, "blob": blob})
    return paths


def _proof_executable_identity_view(
    value: Any,
    *,
    requested_argv: Any,
    project_identity: Any,
    tester_paths: list[dict[str, str]],
) -> dict[str, Any] | None:
    if (
        not isinstance(value, Mapping)
        or not isinstance(requested_argv, list)
        or not requested_argv
        or value.get("requested") != requested_argv[0]
    ):
        return None
    kind = value.get("kind")
    requested = value.get("requested")
    if kind == "repository":
        path = value.get("path")
        blob = value.get("blob")
        if all(isinstance(item, str) for item in (requested, path, blob)):
            return {
                "kind": "frozen-repository-entry",
                "requested": requested,
                "repository_paths": [{"path": path, "blob": blob}],
            }
        return None
    if kind != "system":
        return None
    path = value.get("path")
    sha256 = value.get("sha256")
    size = value.get("size")
    if (
        not all(isinstance(item, str) for item in (requested, path, sha256))
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        return None
    executable_name = Path(str(requested)).name.lower()
    resolved_name = Path(str(path)).name.lower()
    python_pattern = r"(?:python|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?"
    if re.fullmatch(python_pattern, executable_name) or re.fullmatch(
        python_pattern, resolved_name
    ):
        return {
            "kind": "trusted-python",
            "requested": requested,
            "path": path,
            "sha256": sha256,
            "size": size,
        }
    if executable_name == "uv":
        repository_paths = _proof_project_paths(project_identity)
    else:
        repository_paths = tester_paths
    if not repository_paths:
        return None
    return {
        "kind": (
            "absolute-launcher"
            if value.get("resolution") == "explicit_absolute"
            else "trusted-system-launcher"
        ),
        "requested": requested,
        "path": path,
        "sha256": sha256,
        "size": size,
        "repository_paths": copy.deepcopy(repository_paths),
    }


def _proof_test_run_bound(
    value: Any,
    *,
    requested_argv: Any,
    execution_argv: Any,
    framework: Any,
    executable_identity: Any,
    project_identity: Any,
    test_ids: Any,
    classification: str,
) -> bool:
    public_run = _proof_test_run_view(value)
    if (
        not isinstance(value, Mapping)
        or public_run is None
        or any(
            field not in value
            for field in (
                "requested_argv",
                "executable_identity",
                "project_identity",
                "supervisor_observation",
            )
        )
    ):
        return False
    test_result = value.get("test_result")
    matched_test_ids = (
        test_result.get("matched_test_ids")
        if isinstance(test_result, Mapping)
        else None
    )
    return bool(
        value.get("requested_argv") == requested_argv
        and value.get("argv") == execution_argv
        and value.get("executable_identity") == executable_identity
        and value.get("project_identity") == project_identity
        and value.get("timed_out") is False
        and isinstance(value.get("returncode"), int)
        and not isinstance(value.get("returncode"), bool)
        and ((value.get("returncode") == 0) == (classification == "pass"))
        and isinstance(test_result, Mapping)
        and test_result.get("framework") == framework
        and test_result.get("classification") == classification
        and _proof_supervisor_observation_bound(
            value.get("supervisor_observation"),
            framework=framework,
            returncode=value.get("returncode"),
            test_ids=test_ids,
            test_result=test_result,
        )
        and (
            classification != "pass"
            or _proof_pass_counts_bound(test_result.get("counts"), test_ids)
        )
        and (
            classification != "assertion-failure"
            or (
                isinstance(test_ids, list)
                and isinstance(matched_test_ids, list)
                and bool(matched_test_ids)
                and all(isinstance(test_id, str) for test_id in matched_test_ids)
                and set(matched_test_ids).issubset(set(test_ids))
            )
        )
        and _proof_evidence_validator(
            "passTestRun"
            if classification == "pass"
            else "assertionFailureTestRun"
        ).is_valid(public_run)
    )


def _proof_group_replayable(
    spec: Any,
    result: Any,
    *,
    candidate_head: str,
    source_head: str,
    tester_paths: Sequence[Mapping[str, str]],
    repo: Path | None,
    builder_patterns: Sequence[str],
    tester_patterns: Sequence[str],
) -> bool:
    if not isinstance(spec, Mapping) or not isinstance(result, Mapping):
        return False
    try:
        expected_framework = _proof_framework(list(spec.get("argv", [])))
    except (AssuranceError, IndexError, TypeError):
        return False
    common_matches = bool(
        result.get("behavior_ids") == spec.get("behavior_ids")
        and result.get("method") == spec.get("method")
        and result.get("argv") == spec.get("argv")
        and result.get("test_ids") == spec.get("test_ids")
        and result.get("timeout_seconds") == spec.get("timeout_seconds")
        and result.get("framework") == expected_framework
        and isinstance(result.get("execution_argv"), list)
        and isinstance(result.get("executable_identity"), Mapping)
        and "project_identity" in result
    )
    if not common_matches:
        return False
    candidate = result.get("candidate")
    identity = result.get("executable_identity")
    project_identity = result.get("project_identity")
    method = spec.get("method")
    input_heads = [candidate_head]
    if method == "baseline-red":
        input_heads.append(source_head)
    if not isinstance(repo, Path) or not _proof_tester_sources_bound(
        repo,
        source_head=source_head,
        candidate_head=candidate_head,
        framework=expected_framework,
        test_ids=spec.get("test_ids"),
        tester_patterns=tester_patterns,
        tester_paths=tester_paths,
    ):
        return False
    protected_inputs = _proof_repository_inputs_bound(
        repo,
        requested_argv=spec.get("argv"),
        executable_identity=identity,
        project_identity=project_identity,
        heads=input_heads,
    )
    if protected_inputs is None:
        return False
    if not _proof_test_run_bound(
        candidate,
        requested_argv=spec.get("argv"),
        execution_argv=result.get("execution_argv"),
        framework=expected_framework,
        executable_identity=identity,
        project_identity=project_identity,
        test_ids=spec.get("test_ids"),
        classification="pass",
    ):
        return False
    if method == "baseline-red":
        matches = bool(
            result.get("claimed_failure_kind") == spec.get("claimed_failure_kind")
            and _proof_test_run_bound(
                result.get("counterexample"),
                requested_argv=spec.get("argv"),
                execution_argv=result.get("execution_argv"),
                framework=expected_framework,
                executable_identity=identity,
                project_identity=project_identity,
                test_ids=spec.get("test_ids"),
                classification="assertion-failure",
            )
        )
    elif method == "mutation":
        mutation = result.get("mutation")
        if not _proof_test_run_bound(
            mutation,
            requested_argv=spec.get("argv"),
            execution_argv=result.get("execution_argv"),
            framework=expected_framework,
            executable_identity=identity,
            project_identity=project_identity,
            test_ids=spec.get("test_ids"),
            classification="assertion-failure",
        ) or not isinstance(mutation, Mapping):
            return False
        applied_diff = mutation.get("applied_diff")
        changed_paths = _proof_applied_diff_paths(applied_diff)
        canonical_mutation = _proof_canonical_mutation(
            repo,
            candidate_head,
            spec.get("patch"),
        )
        matches = bool(
            mutation.get("patch_sha256")
            == hashlib.sha256(str(spec.get("patch", "")).encode()).hexdigest()
            and isinstance(applied_diff, str)
            and mutation.get("applied_diff_sha256")
            == hashlib.sha256(applied_diff.encode()).hexdigest()
            and mutation.get("changed_paths") == changed_paths
            and isinstance(changed_paths, list)
            and canonical_mutation == (applied_diff, changed_paths)
            and not protected_inputs.intersection(changed_paths)
            and all(
                _matches(path, list(builder_patterns))
                and not _matches(path, list(tester_patterns))
                and _regular_blob_at(repo, candidate_head, path) is not None
                for path in changed_paths
            )
            and mutation.get("head_before") == candidate_head
            and mutation.get("head_after") == candidate_head
        )
    else:
        matches = bool(
            method == "reviewed-boundaries"
            and result.get("reason") == spec.get("reason")
            and result.get("reviewed_boundaries") == spec.get("reviewed_boundaries")
            and _proof_reviewed_boundary_ids(spec) == set(spec.get("test_ids", []))
            and result.get("counterexample") is None
        )
    if not matches:
        return False
    view = _proof_group_evidence_view(
        result,
        machine_head=candidate_head if method == "reviewed-boundaries" else None,
        tester_paths=[dict(item) for item in tester_paths],
    )
    return view is not None and _proof_group_evidence_valid(view)


def _proof_record_replayable(
    record: Any,
    *,
    expected_behaviors: Sequence[str] | None = None,
    expected_candidate_head: Any = None,
    expected_producer: Any = None,
    expected_source_head: str | None = None,
    tester_paths: Sequence[Mapping[str, str]] = (),
    repo: Path | None = None,
    builder_patterns: Sequence[str] = (),
    tester_patterns: Sequence[str] = (),
) -> bool:
    if (
        not isinstance(record, Mapping)
        or record.get("kind") != "proof"
        or record.get("status") != "pass"
    ):
        return False
    candidate_head = record.get("candidate_head")
    details = record.get("details")
    spec = details.get("spec") if isinstance(details, Mapping) else None
    groups = spec.get("groups") if isinstance(spec, Mapping) else None
    results = details.get("results") if isinstance(details, Mapping) else None
    try:
        normalized_spec = validate_test_proof_spec(spec)
    except ContractError:
        return False
    recorded_behaviors = details.get("behaviors") if isinstance(details, Mapping) else None
    observed_behaviors = (
        [behavior for group in groups for behavior in group.get("behavior_ids", [])]
        if isinstance(groups, list) and all(isinstance(group, Mapping) for group in groups)
        else []
    )
    if (
        not isinstance(candidate_head, str)
        or not re.fullmatch(r"[0-9a-f]{40}", candidate_head)
        or candidate_head != expected_candidate_head
        or record.get("producer") != expected_producer
        or not isinstance(details, Mapping)
        or details.get("result") != "pass"
        or not isinstance(details.get("source_head"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", details["source_head"])
        or (
            expected_source_head is not None
            and details.get("source_head") != expected_source_head
        )
        or not isinstance(details.get("artifact_root"), str)
        or not details["artifact_root"].strip()
        or normalized_spec != spec
        or not isinstance(groups, list)
        or not isinstance(results, list)
        or not groups
        or len(groups) != len(results)
        or not isinstance(recorded_behaviors, list)
        or any(
            not isinstance(behavior, str) or not behavior.strip()
            for behavior in recorded_behaviors
        )
        or len(recorded_behaviors) != len(set(recorded_behaviors))
        or len(observed_behaviors) != len(set(observed_behaviors))
        or sorted(recorded_behaviors) != sorted(observed_behaviors)
        or (
            expected_behaviors is not None
            and list(recorded_behaviors) != list(expected_behaviors)
        )
        or details.get("report_digest")
        != digest({"spec": spec, "results": results})
    ):
        return False
    if isinstance(repo, Path):
        source_scope = _proof_tester_scope(
            repo,
            details["source_head"],
            tester_patterns,
        )
        candidate_scope = _proof_tester_scope(
            repo,
            candidate_head,
            tester_patterns,
        )
        if source_scope is None or candidate_scope != source_scope:
            return False
    return all(
        _proof_group_replayable(
            group,
            result,
            candidate_head=candidate_head,
            source_head=details["source_head"],
            tester_paths=tester_paths,
            repo=repo,
            builder_patterns=builder_patterns,
            tester_patterns=tester_patterns,
        )
        for group, result in zip(groups, results)
    )


def _proof_group_evidence_view(
    result: Any,
    *,
    machine_head: str | None,
    tester_paths: list[dict[str, str]],
) -> dict[str, Any] | None:
    if not isinstance(result, Mapping):
        return None
    candidate_run = _proof_test_run_view(result.get("candidate"))
    identity = _proof_executable_identity_view(
        result.get("executable_identity"),
        requested_argv=result.get("argv"),
        project_identity=result.get("project_identity"),
        tester_paths=tester_paths,
    )
    if candidate_run is None or identity is None:
        return None
    method = result.get("method")
    group = {
        "behavior_ids": copy.deepcopy(result.get("behavior_ids")),
        "method": method,
        "argv": copy.deepcopy(result.get("argv")),
        "execution_argv": copy.deepcopy(result.get("execution_argv")),
        "framework": result.get("framework"),
        "executable_identity": identity,
        "test_ids": copy.deepcopy(result.get("test_ids")),
        "timeout_seconds": result.get("timeout_seconds"),
    }
    if method == "baseline-red":
        baseline = _proof_test_run_view(result.get("counterexample"))
        if baseline is None:
            return None
        return {
            **group,
            "claimed_failure_kind": result.get("claimed_failure_kind"),
            "candidate": candidate_run,
            "baseline": baseline,
        }
    if method == "mutation":
        mutation = _proof_test_run_view(result.get("mutation"))
        raw_mutation = result.get("mutation")
        if mutation is None or not isinstance(raw_mutation, Mapping):
            return None
        for field in (
            "patch_sha256",
            "applied_diff",
            "applied_diff_sha256",
            "changed_paths",
            "head_before",
            "head_after",
        ):
            if field not in raw_mutation:
                return None
            mutation[field] = copy.deepcopy(raw_mutation[field])
        return {**group, "candidate": candidate_run, "mutation": mutation}
    if method == "reviewed-boundaries" and isinstance(machine_head, str):
        return {
            **group,
            "reason": copy.deepcopy(result.get("reason")),
            "reviewed_boundaries": copy.deepcopy(result.get("reviewed_boundaries")),
            "machine_evidence_head": machine_head,
        }
    return None


def _driver_evidence_view(ledger: Mapping[str, Any]) -> dict[str, Any]:
    evidence = copy.deepcopy(ledger.get("evidence", {}))
    proof = evidence.get("proof")
    details = proof.get("details") if isinstance(proof, dict) else None
    results = details.get("results") if isinstance(details, dict) else None
    spec = details.get("spec") if isinstance(details, dict) else None
    groups = spec.get("groups") if isinstance(spec, dict) else None
    if (
        not isinstance(proof, dict)
        or evidence_state(ledger, "proof") != "pass"
        or not isinstance(results, list)
        or not isinstance(groups, list)
    ):
        return evidence
    source = ledger.get("facets", {}).get("execution", {}).get("tester_source")
    authority = ledger.get("facets", {}).get("authority", {})
    tester_patterns = (
        authority.get("tester_write", [])
        if isinstance(authority, Mapping)
        else []
    )
    tester_paths = _proof_tester_scope(
        Path(str(ledger["repo_root"])),
        source.get("head") if isinstance(source, Mapping) else None,
        tester_patterns,
    )
    if tester_paths is None:
        return evidence
    machine_head: str | None = None
    if any(group.get("method") == "reviewed-boundaries" for group in groups):
        machine = ledger.get("evidence", {}).get("machine")
        if evidence_state(ledger, "machine") != "pass" or not isinstance(machine, Mapping):
            return evidence
        observed_head = machine.get("candidate_head")
        if observed_head != proof.get("candidate_head"):
            return evidence
        machine_head = str(observed_head)
    views: list[dict[str, Any]] = []
    for result in results:
        view = _proof_group_evidence_view(
            result,
            machine_head=machine_head,
            tester_paths=tester_paths,
        )
        if view is None or not _proof_group_evidence_valid(view):
            return evidence
        views.append(view)
    details["results"] = views
    details["report_digest"] = digest({"spec": spec, "results": views})
    return evidence


def driver_context(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    ledger = read_ledger(repo, run_id)
    dispatch = ledger.get("dispatch_intent")
    candidate_observation: dict[str, Any] | None = None
    if (
        isinstance(dispatch, Mapping)
        and dispatch.get("action") in BUILDER_SIDE_EFFECT_ACTIONS
        and isinstance(dispatch.get("candidate_manifest_digest"), str)
    ):
        current_manifest = _candidate_manifest_for_ledger(ledger)
        candidate_observation = {
            "candidate_manifest": copy.deepcopy(current_manifest),
            "manifest_digest": (
                current_manifest.get("manifest_digest")
                if isinstance(current_manifest, Mapping)
                else None
            ),
        }
    return {
        "status": "FATAL" if ledger["phase"] == "failed" else "READY",
        "run_id": run_id,
        "runtime_identity": copy.deepcopy(ledger["runtime_identity"]),
        "runtime_compatibility": runtime_compatibility(ledger),
        "runtime_support": copy.deepcopy(ledger["runtime_support"]),
        "phase": ledger["phase"],
        "repo_root": ledger["repo_root"],
        "target_start_head": ledger["target_start_head"],
        "target_contenders": _target_contenders(repo, ledger),
        "candidate_worktree": ledger["candidate_worktree"],
        "facets": copy.deepcopy(ledger["facets"]),
        "recovery_policy": recovery_policy(ledger["facets"]),
        "evidence": _driver_evidence_view(ledger),
        "publication": copy.deepcopy(ledger.get("publication")),
        "problems": copy.deepcopy(ledger.get("problems", [])),
        "driver_runtime": copy.deepcopy(ledger.get("driver_runtime")),
        "transport_cleanup_intent": copy.deepcopy(
            ledger.get("transport_cleanup_intent")
        ),
        "deferred_wait": copy.deepcopy(ledger.get("deferred_wait")),
        "driver_failure": copy.deepcopy(ledger.get("driver_failure")),
        "dispatch_intent": copy.deepcopy(ledger.get("dispatch_intent")),
        "candidate_observation": candidate_observation,
        "tester_replacement_intent": copy.deepcopy(
            ledger.get("tester_replacement_intent")
        ),
        "proof_failure": copy.deepcopy(ledger.get("proof_failure")),
        "proof_failure_state": proof_failure_state(ledger),
        "machine_failure": copy.deepcopy(ledger.get("machine_failure")),
        "machine_failure_state": machine_failure_state(ledger),
        "recomposition_intent": copy.deepcopy(ledger.get("recomposition_intent")),
        "candidate_residue_intent": copy.deepcopy(ledger.get("candidate_residue_intent")),
        "deployment_transaction": copy.deepcopy(ledger.get("deployment_transaction")),
        "pending_blackbox": copy.deepcopy(ledger.get("pending_blackbox")),
        "doc_reference_contract_version": ledger.get("doc_reference_contract_version"),
        "doc_reference_scan": copy.deepcopy(ledger.get("doc_reference_scan")),
        "doc_reference_scan_state": doc_reference_scan_state(ledger),
        "environment_lease": copy.deepcopy(ledger.get("environment_lease")),
        "supersede_intent": copy.deepcopy(ledger.get("supersede_intent")),
        "abandon_intent": copy.deepcopy(ledger.get("abandon_intent")),
        "lineage": _derive_lineage(repo, ledger),
    }


def _assert_plan_decision_mutation_binding(
    repo: Path,
    ledger: Mapping[str, Any],
    *,
    problem_key: str | None,
    facet: str,
    decision_action_id: str | None,
    expected_facet_digest: str | None,
    owner_session_id: str | None,
) -> None:
    provided = (decision_action_id, expected_facet_digest, owner_session_id)
    if not any(item is not None for item in provided):
        return
    if problem_key is None or not all(isinstance(item, str) and item for item in provided):
        raise AssuranceError(
            "decision mutation binding requires action, facet digest, session and problem key",
            code="DECISION_MUTATION_BINDING_INCOMPLETE",
            status="FAIL",
        )
    if ledger.get("owner_session_id") != owner_session_id:
        raise AssuranceError(
            "contract decision belongs to another Codex session",
            code="DECISION_SESSION_MISMATCH",
            status="FAIL",
        )
    if ledger["digests"].get(facet) != expected_facet_digest:
        raise AssuranceError(
            "contract decision facet digest is stale",
            code="DECISION_FACET_STALE",
            status="FAIL",
            details={
                "facet": facet,
                "expected": ledger["digests"].get(facet),
                "provided": expected_facet_digest,
            },
        )
    from .driver import next_action

    current = next_action(repo, str(ledger["run_id"]))
    problem = current.get("problem")
    if (
        current.get("status") != "NEEDS_USER"
        or current.get("action") != "contract_decision"
        or current.get("action_id") != decision_action_id
        or not isinstance(problem, Mapping)
        or problem.get("key") != problem_key
    ):
        raise AssuranceError(
            "contract decision mutation handoff is stale",
            code="DECISION_ACTION_STALE",
            status="FAIL",
            details={
                "expected_action": current.get("action"),
                "expected_action_id": current.get("action_id"),
                "problem_key": problem_key,
            },
        )


def apply_engineering_correction(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    driver_runtime_kind: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger.get("phase") != "active":
            raise AssuranceError(
                "engineering correction requires an active run",
                code="ASSURANCE_RUN_NOT_ACTIVE",
                status="FAIL",
            )
        _assert_no_candidate_residue_intent(ledger)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        from .driver import engineering_correction_preview, next_action

        current = next_action(repo, run_id, _ledger=ledger)
        problem = current.get("problem")
        if (
            current.get("status") != "CONTINUE"
            or current.get("action") != "apply_engineering_correction"
            or current.get("action_id") != action_id
            or not isinstance(problem, Mapping)
        ):
            raise AssuranceError(
                "engineering correction action is stale",
                code="DRIVER_ACTION_STALE",
                status="FAIL",
            )
        preview = engineering_correction_preview(repo, ledger, problem)
        for field in (
            "problem_key",
            "base_assurance_digest",
            "replacement_assurance_digest",
            "replacement_contract_digest",
            "invalidated_evidence",
        ):
            if current.get(field) != preview.get(field):
                raise AssuranceError(
                    "engineering correction preview changed before mutation",
                    code="ENGINEERING_CORRECTION_STALE",
                    status="FAIL",
                    details={"field": field},
                )
        matches = [
            item
            for item in ledger.get("problems", [])
            if item.get("status") == "open"
            and item.get("key") == preview["problem_key"]
            and item.get("owner") == "plan"
        ]
        if len(matches) != 1:
            raise AssuranceError(
                "engineering correction problem is stale or ambiguous",
                code="ENGINEERING_CORRECTION_PROBLEM_MISMATCH",
                status="FAIL",
            )
        # Re-check the candidate immediately before mutating the ledger.  The
        # preview is intentionally read-only, but a Git conflict or branch
        # drift can appear between the driver decision and this transaction.
        engineering_correction_preview(repo, ledger, problem)
        old_digest = ledger["digests"]["assurance"]
        replacement = copy.deepcopy(ledger["facets"])
        replacement["assurance"] = copy.deepcopy(
            preview["replacement_assurance"]
        )
        replacement = validate_contract(replacement)
        ledger["facets"] = replacement
        ledger["digests"] = facet_digests(replacement)
        problem_record = matches[0]
        problem_record["status"] = "resolved"
        problem_record["resolution"] = (
            "automatic-engineering-correction:"
            + ledger["digests"]["assurance"]
        )
        problem_record["resolved_at"] = now()
        append_event(
            ledger,
            "automatic_engineering_correction_applied",
            {
                "key": problem_record["key"],
                "old_assurance_digest": old_digest,
                "new_assurance_digest": ledger["digests"]["assurance"],
                "invalidated_evidence": preview["invalidated_evidence"],
                "action_id": action_id,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


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
    decision_action_id: str | None = None,
    expected_facet_digest: str | None = None,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    if facet not in {"mission", "authority", "assurance", "execution"}:
        raise AssuranceError("unknown contract facet", code="ASSURANCE_FACET_INVALID")
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
        _assert_plan_decision_mutation_binding(
            repo,
            ledger,
            problem_key=resolve_plan_problem_key,
            facet=facet,
            decision_action_id=decision_action_id,
            expected_facet_digest=expected_facet_digest,
            owner_session_id=owner_session_id,
        )
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
        if facet == "mission":
            _reject_acceptance_observation_downgrade(old, candidate["mission"])
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
    *,
    resolve_plan_problem_key: str | None = None,
    decision_action_id: str | None = None,
    expected_facet_digest: str | None = None,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
        _assert_plan_decision_mutation_binding(
            repo,
            ledger,
            problem_key=resolve_plan_problem_key,
            facet="mission",
            decision_action_id=decision_action_id,
            expected_facet_digest=expected_facet_digest,
            owner_session_id=owner_session_id,
        )
        old = ledger["facets"]["mission"]
        old_digest = ledger["digests"]["mission"]
        plan_problem: dict[str, Any] | None = None
        if resolve_plan_problem_key is not None:
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
                expected_resolution = f"plan-decision:mission:{old_digest}"
                if (
                    mission_value == old
                    and resolved.get("status") == "resolved"
                    and resolved.get("resolution") == expected_resolution
                ):
                    return status(repo, run_id)
                raise AssuranceError(
                    "resolved plan problem conflicts with the requested mission decision",
                    code="PLAN_PROBLEM_DECISION_CONFLICT",
                    status="FAIL",
                    details={"key": resolve_plan_problem_key, "facet": "mission"},
                )
            else:
                raise AssuranceError(
                    "plan problem key was not found",
                    code="PLAN_PROBLEM_NOT_FOUND",
                    status="FAIL",
                    details={"key": resolve_plan_problem_key},
                )
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
        _reject_acceptance_observation_downgrade(old, candidate["mission"])
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
                source_lineage=source_lineage,
                mission_semantics_digest=digest(_mission_semantics_value(old)),
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
        if plan_problem is not None:
            new_digest = ledger["digests"]["mission"]
            plan_problem["status"] = "resolved"
            plan_problem["resolution"] = f"plan-decision:mission:{new_digest}"
            plan_problem["resolved_at"] = now()
            append_event(
                ledger,
                "plan_problem_decision_applied",
                {
                    "key": resolve_plan_problem_key,
                    "facet": "mission",
                    "old_digest": old_digest,
                    "new_digest": new_digest,
                    "facet_changed": True,
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


def _close_problem_key(ledger: dict[str, Any], key: str, resolution: str) -> None:
    for item in ledger.get("problems", []):
        if item.get("key") == key and item.get("status") == "open":
            item["status"] = "resolved"
            item["resolution"] = resolution
            item["resolved_at"] = now()


def _record_runtime_preparation_problem(
    ledger: dict[str, Any],
    *,
    candidate_head: str,
    error: AssuranceError,
) -> None:
    key = RUNTIME_PREPARATION_PROBLEM_KEY
    if any(
        item.get("key") == key and item.get("status") == "open"
        for item in ledger.get("problems", [])
        if isinstance(item, Mapping)
    ):
        return
    builder = ledger["facets"]["execution"].get("agents", {}).get("builder")
    producer = (
        {"role": "builder", **copy.deepcopy(builder)}
        if isinstance(builder, Mapping)
        else None
    )
    ledger.setdefault("problems", []).append(
        {
            "key": key,
            "summary": "Candidate changes require a protected runtime preparation",
            "details": json.dumps(
                _runtime_preparation_problem_details(error),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "owner": "builder_loop",
            "status": "open",
            "producer": producer,
            "candidate_head": candidate_head,
            "recorded_at": now(),
        }
    )
    append_event(
        ledger,
        "problems_recorded",
        {"role": "builder", "keys": [key]},
    )


def _record_public_prerequisite_classification_problem(
    ledger: dict[str, Any],
    *,
    previous_candidate_head: str | None,
    candidate_head: str,
    classification: Sequence[Mapping[str, str]],
) -> str:
    key = PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_KEY
    classification_value = [copy.deepcopy(dict(item)) for item in classification]
    paths = sorted(
        str(item["path"])
        for item in classification_value
        if item.get("status") != "ready"
    )
    details_value = {
        "code": "PUBLIC_PREREQUISITE_CLASSIFICATION_INVALID",
        "previous_candidate_head": previous_candidate_head,
        "candidate_head": candidate_head,
        "authority_digest": ledger["digests"]["authority"],
        "paths": paths,
        "classification": classification_value,
        "classification_digest": digest(classification_value),
    }
    details_digest = digest(details_value)
    existing = [
        item
        for item in ledger.get("problems", [])
        if isinstance(item, Mapping)
        and item.get("key") == key
        and item.get("status") == "open"
    ]
    if existing:
        if len(existing) != 1 or existing[0].get("candidate_head") != candidate_head:
            raise AssuranceError(
                "public prerequisite classification problem identity changed",
                code="PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_CONFLICT",
                status="NEEDS_USER",
                details={"candidate_head": candidate_head, "problem_count": len(existing)},
            )
        try:
            existing_details = json.loads(str(existing[0]["details"]))
        except json.JSONDecodeError as exc:
            raise AssuranceError(
                "public prerequisite classification problem details are malformed",
                code="PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_CONFLICT",
                status="NEEDS_USER",
            ) from exc
        if existing_details != details_value:
            raise AssuranceError(
                "public prerequisite classification facts changed",
                code="PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_CONFLICT",
                status="NEEDS_USER",
                details={
                    "candidate_head": candidate_head,
                    "recorded": existing_details,
                    "expected": details_value,
                },
            )
        return digest(existing_details)
    builder = ledger["facets"]["execution"].get("agents", {}).get("builder")
    producer = (
        {"role": "builder", **copy.deepcopy(builder)}
        if isinstance(builder, Mapping)
        else None
    )
    ledger.setdefault("problems", []).append(
        {
            "key": key,
            "summary": "Public prerequisite classification blocks Builder checkpoint",
            "details": json.dumps(
                details_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "owner": "builder_loop",
            "status": "open",
            "producer": producer,
            "candidate_head": candidate_head,
            "recorded_at": now(),
        }
    )
    append_event(
        ledger,
        "problems_recorded",
        {"role": "builder", "keys": [key]},
    )
    return details_digest


def _validated_builder_files(
    repo: Path,
    ledger: Mapping[str, Any],
    *,
    candidate: str,
    files: Sequence[str],
    trusted_tester_manifest: Mapping[str, str] | None = None,
) -> list[str]:
    execution = ledger["facets"]["execution"]
    authority = ledger["facets"]["authority"]
    ownership_files = set(files)
    checkpointed_candidate = execution.get("candidate_head")
    if (
        isinstance(checkpointed_candidate, str)
        and checkpointed_candidate != candidate
        and commit_exists(repo, checkpointed_candidate)
    ):
        ownership_files.update(
            changed_files(repo, checkpointed_candidate, candidate)
        )
    candidate_blobs = {
        path: _blob_at(repo, candidate, path)
        for path in ownership_files
    }
    tester_manifest = {
        item["path"]: item["blob"]
        for item in (execution.get("tester_source") or {}).get("files", [])
    }
    if trusted_tester_manifest is not None:
        tester_manifest.update(
            {
                str(path): str(blob)
                for path, blob in trusted_tester_manifest.items()
            }
        )
    tester_files = set(execution.get("tester_files", []))
    carryover_manifest = {
        item["path"]: item["blob"]
        for item in (execution.get("carryover") or {}).get("files", [])
    }
    tester_violations = sorted(
        path
        for path in ownership_files
        if _matches(path, authority["tester_write"])
        and not (
            path in tester_files
            and path in tester_manifest
            and candidate_blobs[path] == tester_manifest[path]
        )
        and not (
            path in carryover_manifest
            and candidate_blobs[path] == carryover_manifest[path]
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
            path in carryover_manifest
            and candidate_blobs[path] == carryover_manifest[path]
        )
        and not _matches(path, authority["tester_write"])
    )
    invalid = [
        path
        for path in builder_files
        if not _matches(path, authority["builder_write"])
    ]
    if invalid:
        raise AssuranceError(
            "Builder checkpoint changed files outside authority",
            code="BUILDER_AUTHORITY_VIOLATION",
            details={"paths": invalid},
        )
    return builder_files


def checkpoint_builder(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str | None = None,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
        if _builder_mode(ledger) == "root_session":
            _assert_builder_owner(ledger, owner_session_id=owner_session_id)
        root_dispatch = _root_builder_dispatch_for_application(
            ledger,
            action_id=action_id,
            owner_session_id=owner_session_id,
        )
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
        if (
            execution.get("candidate_head") is None
            and isinstance(execution.get("cost_ancestry"), Mapping)
            and candidate == ledger["target_start_head"]
        ):
            raise AssuranceError(
                "cost ancestry recovery root requires a new committed Builder candidate",
                code="COST_ANCESTRY_CANDIDATE_MISSING",
                status="NEEDS_USER",
            )
        files = changed_files(repo, ledger["target_start_head"], candidate)
        actual_runtime_support, required_independent = (
            _runtime_support_for_changed_paths(repo, ledger, files)
        )
        try:
            _assert_runtime_support_contract(
                ledger["facets"],
                actual_runtime_support,
                required_independent,
            )
        except AssuranceError as exc:
            if exc.code in RUNTIME_PREPARATION_ERROR_CODES:
                _record_runtime_preparation_problem(
                    ledger,
                    candidate_head=candidate,
                    error=exc,
                )
                save_ledger(repo, ledger)
            raise
        builder_files = _validated_builder_files(
            repo,
            ledger,
            candidate=candidate,
            files=files,
        )
        projected_execution = copy.deepcopy(execution)
        projected_execution["builder_files"] = builder_files
        public_classification = _public_prerequisite_classification(
            repo,
            projected_execution,
            ledger["facets"]["authority"].get("public_prerequisites", []),
            candidate=candidate,
        )
        unready_public = [
            item["path"]
            for item in public_classification
            if item["status"] != "ready"
        ]
        if unready_public:
            previous = execution.get("candidate_head")
            if previous != candidate or execution.get("builder_files") != builder_files:
                execution["version"] += 1
            execution["candidate_head"] = candidate
            execution["builder_files"] = builder_files
            ledger["builder_checkpointed"] = False
            ledger["digests"] = facet_digests(ledger["facets"])
            problem_details_digest = (
                _record_public_prerequisite_classification_problem(
                    ledger,
                    previous_candidate_head=previous,
                    candidate_head=candidate,
                    classification=public_classification,
                )
            )
            append_event(
                ledger,
                "builder_checkpoint_blocked",
                {
                    "code": "PUBLIC_PREREQUISITE_CLASSIFICATION_INVALID",
                    "old_head": previous,
                    "candidate_head": candidate,
                    "paths": unready_public,
                    "authority_digest": ledger["digests"]["authority"],
                    "classification": copy.deepcopy(public_classification),
                    "classification_digest": digest(public_classification),
                    "problem_details_digest": problem_details_digest,
                },
            )
            _mark_root_builder_result_applied(
                ledger, root_dispatch, "checkpoint_builder"
            )
            save_ledger(repo, ledger)
            return status(repo, run_id)
        publication = ledger.get("publication")
        if isinstance(publication, dict) and publication.get("head"):
            frozen = {item["path"]: item["blob"] for item in publication.get("files", [])}
            changed_public = sorted(
                path for path, blob in frozen.items() if _blob_at(repo, candidate, path) != blob
            )
            if changed_public:
                _mark_root_builder_result_applied(
                    ledger, root_dispatch, "checkpoint_builder"
                )
                _begin_recomposition(
                    repo,
                    ledger,
                    kind="publication_refresh",
                    incoming_candidate=candidate,
                    new_target=ledger["target_start_head"],
                )
                return status(repo, run_id)
        previous = execution.get("candidate_head")
        if (
            previous == candidate
            and execution.get("builder_files") == builder_files
            and ledger.get("builder_checkpointed") is True
        ):
            if _mark_root_builder_result_applied(
                ledger, root_dispatch, "checkpoint_builder"
            ):
                save_ledger(repo, ledger)
            return status(repo, run_id)
        execution["version"] += 1
        execution["candidate_head"] = candidate
        execution["builder_files"] = builder_files
        ledger["builder_checkpointed"] = True
        ledger["digests"] = facet_digests(ledger["facets"])
        _close_problem_key(
            ledger,
            RUNTIME_PREPARATION_PROBLEM_KEY,
            f"checkpoint:{candidate}",
        )
        _close_problem_key(
            ledger,
            PUBLIC_PREREQUISITE_CLASSIFICATION_PROBLEM_KEY,
            f"checkpoint:{candidate}",
        )
        _close_problems(ledger, "builder", f"checkpoint:{candidate}")
        append_event(
            ledger,
            "builder_checkpointed",
            {"old_head": previous, "candidate_head": candidate, "files": builder_files},
        )
        _mark_root_builder_result_applied(
            ledger, root_dispatch, "checkpoint_builder"
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
        _assert_no_candidate_residue_intent(ledger)
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
        execution = ledger["facets"]["execution"]
        public_classification = _public_prerequisite_classification(
            repo,
            execution,
            paths,
            candidate=candidate,
        )
        unready_public = [
            item["path"]
            for item in public_classification
            if item["status"] != "ready"
        ]
        if unready_public:
            ledger["builder_checkpointed"] = False
            append_event(
                ledger,
                "prerequisite_publication_blocked",
                {
                    "code": "PUBLIC_PREREQUISITE_CLASSIFICATION_INVALID",
                    "candidate_head": candidate,
                    "paths": unready_public,
                },
            )
            save_ledger(repo, ledger)
            return status(repo, run_id)
        head, tree, files = _materialize_publication(
            repo,
            run_id,
            base_head=ledger["target_start_head"],
            source_head=candidate,
            paths=paths,
        )
        publication.update(
            generation=int(publication.get("generation", 0)) + 1,
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
        _assert_no_candidate_residue_intent(ledger)
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


def scan_doc_references(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
        if ledger.get("doc_reference_contract_version") != DOC_REFERENCE_CONTRACT_VERSION:
            raise AssuranceError(
                "documentation reference scan is not enabled for this ledger",
                code="DOC_REFERENCE_SCAN_NOT_ENABLED",
            )
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if not isinstance(candidate, str):
            raise AssuranceError(
                "documentation reference scan requires a committed candidate",
                code="DOC_REFERENCE_CANDIDATE_MISSING",
            )
        target_start_head = ledger["target_start_head"]
        try:
            scanned = scan_repository(repo, target_start_head, candidate)
            record: dict[str, Any] = {
                "contract_version": DOC_REFERENCE_CONTRACT_VERSION,
                "target_start_head": scanned["base_head"],
                "candidate_head": scanned["candidate_head"],
                "status": "fail" if scanned["broken_references"] else "pass",
                "changed_paths": scanned["changed_paths"],
                "changed_definitions": scanned["changed_definitions"],
                "documents": scanned["documents"],
                "broken_references": scanned["broken_references"],
                "semantic_checks": scanned["semantic_checks"],
                "error": None,
            }
        except DocReferenceScanError as exc:
            record = {
                "contract_version": DOC_REFERENCE_CONTRACT_VERSION,
                "target_start_head": target_start_head,
                "candidate_head": candidate,
                "status": "error",
                "changed_paths": [],
                "changed_definitions": [],
                "documents": [],
                "broken_references": [],
                "semantic_checks": [],
                "error": {"code": exc.code, "message": str(exc)},
            }
        record["result_digest"] = digest(doc_reference_scan_digest_input(record))
        record["scanned_at"] = now()
        ledger["doc_reference_scan"] = record
        append_event(
            ledger,
            "doc_reference_scan_recorded",
            {
                "status": record["status"],
                "result_digest": record["result_digest"],
                "broken_reference_count": len(record["broken_references"]),
                "semantic_check_count": len(record["semantic_checks"]),
                "error_code": (
                    record["error"]["code"]
                    if isinstance(record.get("error"), Mapping)
                    else None
                ),
            },
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
    thread_id: str | None,
    action_id: str | None = None,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    report = validate_problem_report(report_value)
    if role not in {"builder", "tester", "reviewer"}:
        raise AssuranceError("problem producer role is invalid", code="PROBLEM_ROLE_INVALID")
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _assert_no_candidate_residue_intent(ledger)
        expected = None
        if role in {"builder", "tester", "reviewer"}:
            expected = ledger["facets"]["execution"]["agents"].get(role)
        root_dispatch = None
        if role == "builder" and _builder_mode(ledger) == "root_session":
            owner = _assert_builder_owner(
                ledger,
                owner_session_id=owner_session_id,
            )
            root_dispatch = _root_builder_dispatch_for_application(
                ledger,
                action_id=action_id,
                owner_session_id=owner_session_id,
            )
            if (
                agent_id != owner.get("agent_id")
                or thread_id is not None
            ):
                raise AssuranceError("problem producer identity mismatch", code="PROBLEM_PRODUCER_MISMATCH")
            producer = {"role": role, **owner}
        else:
            if not isinstance(thread_id, str) or not thread_id:
                raise AssuranceError(
                    "problem producer thread identity is required",
                    code="PROBLEM_PRODUCER_MISMATCH",
                )
            if expected is not None:
                if expected != {"agent_id": agent_id, "thread_id": thread_id}:
                    raise AssuranceError("problem producer identity mismatch", code="PROBLEM_PRODUCER_MISMATCH")
            producer = {"role": role, "agent_id": agent_id, "thread_id": thread_id}
        candidate = ledger["facets"]["execution"].get("candidate_head")
        added: list[str] = []
        for problem in report["problems"]:
            if problem.get("producer_continuity") == "invalid" and (
                role != "tester" or problem.get("owner") != "tester"
            ):
                raise AssuranceError(
                    "producer continuity invalidation is only valid for a Tester-owned problem",
                    code="PROBLEM_PRODUCER_CONTINUITY_INVALID",
                    status="FAIL",
                    details={"key": problem["key"], "role": role},
                )
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
                content = {
                    field: replay.get(field)
                    for field in (
                        "key", "summary", "details", "owner",
                        "producer_continuity", "decision_request",
                    )
                }
                replayed_problem = {
                    field: problem.get(field)
                    for field in (
                        "key", "summary", "details", "owner",
                        "producer_continuity", "decision_request",
                    )
                }
                if content != replayed_problem:
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
            if _mark_root_builder_result_applied(
                ledger, root_dispatch, "record_problems"
            ):
                save_ledger(repo, ledger)
            return status(repo, run_id)
        append_event(
            ledger,
            "problems_recorded",
            {"role": role, "keys": added},
        )
        _mark_root_builder_result_applied(
            ledger, root_dispatch, "record_problems"
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def _tester_replacement_problem(
    ledger: Mapping[str, Any], problem_key: str
) -> dict[str, Any]:
    matches = [
        item
        for item in ledger.get("problems", [])
        if isinstance(item, dict)
        and item.get("status") == "open"
        and item.get("key") == problem_key
    ]
    if len(matches) != 1:
        raise AssuranceError(
            "Tester replacement requires one exact open problem",
            code="TESTER_REPLACEMENT_PROBLEM_MISMATCH",
            status="FAIL",
            details={"problem_key": problem_key, "match_count": len(matches)},
        )
    problem = matches[0]
    if problem.get("owner") != "tester" or problem.get("producer_continuity") != "invalid":
        raise AssuranceError(
            "Tester replacement requires a continuity-invalid Tester problem",
            code="TESTER_REPLACEMENT_PROBLEM_INVALID",
            status="FAIL",
            details={"problem_key": problem_key},
        )
    return problem


def _tester_replacement_problem_snapshot(ledger: Mapping[str, Any]) -> str:
    problems = [
        item
        for item in ledger.get("problems", [])
        if isinstance(item, Mapping)
        and item.get("status") == "open"
        and item.get("owner") == "tester"
        and item.get("producer_continuity") == "invalid"
    ]
    problems.sort(
        key=lambda item: (
            str(item.get("key", "")),
            str(item.get("candidate_head", "")),
            str(item.get("producer", {}).get("agent_id", "")),
            str(item.get("producer", {}).get("thread_id", "")),
        )
    )
    return digest(problems)


def _assert_tester_source_exact(repo: Path, source: Mapping[str, Any]) -> None:
    worktree = Path(str(source.get("worktree", "")))
    branch = str(source.get("branch", ""))
    expected_head = source.get("head")
    branch_result = git(
        repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False
    )
    worktree_head = (
        git(worktree, "rev-parse", "HEAD", check=False)
        if worktree.is_dir()
        else None
    )
    worktree_branch = (
        git(worktree, "symbolic-ref", "-q", "--short", "HEAD", check=False)
        if worktree.is_dir()
        else None
    )
    residue = dirty_paths(worktree) if worktree.is_dir() else ["<missing>"]
    if (
        branch_result.returncode != 0
        or branch_result.stdout.strip() != expected_head
        or worktree_head is None
        or worktree_head.returncode != 0
        or worktree_head.stdout.strip() != expected_head
        or worktree_branch is None
        or worktree_branch.returncode != 0
        or worktree_branch.stdout.strip() != branch
        or residue
    ):
        raise AssuranceError(
            "Tester source drifted and was preserved",
            code="TESTER_REPLACEMENT_SOURCE_DRIFT",
            status="FAIL",
            details={
                "branch": branch,
                "expected_head": expected_head,
                "branch_head": branch_result.stdout.strip() or None,
                "worktree_head": (
                    worktree_head.stdout.strip()
                    if worktree_head is not None and worktree_head.returncode == 0
                    else None
                ),
                "dirty_paths": residue,
            },
        )


def _assert_tester_replacement_candidate(
    repo: Path, ledger: Mapping[str, Any]
) -> str:
    candidate = ledger["facets"]["execution"].get("candidate_head")
    candidate_worktree = Path(ledger["candidate_worktree"])
    candidate_ref = git(
        repo,
        "rev-parse",
        "--verify",
        f"refs/heads/{ledger['candidate_branch']}",
        check=False,
    )
    worktree_head = (
        git(candidate_worktree, "rev-parse", "HEAD", check=False)
        if candidate_worktree.is_dir()
        else None
    )
    residue = dirty_paths(candidate_worktree) if candidate_worktree.is_dir() else ["<missing>"]
    if (
        not isinstance(candidate, str)
        or candidate_ref.returncode != 0
        or candidate_ref.stdout.strip() != candidate
        or worktree_head is None
        or worktree_head.returncode != 0
        or worktree_head.stdout.strip() != candidate
        or residue
        or branch_head(repo, ledger["target_branch"]) != ledger["target_start_head"]
    ):
        raise AssuranceError(
            "candidate or target drifted before Tester replacement",
            code="TESTER_REPLACEMENT_CANDIDATE_DRIFT",
            status="FAIL",
            details={"candidate_head": candidate, "dirty_paths": residue},
        )
    return candidate


def _registered_worktree(
    repo: Path, worktree: Path
) -> dict[str, str] | None:
    listed = git(repo, "worktree", "list", "--porcelain", check=False)
    if listed.returncode != 0:
        raise AssuranceError(
            listed.stderr.strip() or "Git worktree registry cannot be read",
            code="TESTER_REPLACEMENT_SOURCE_CONFLICT",
            status="FAIL",
        )
    expected = str(worktree.resolve())
    for block in listed.stdout.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key in {"worktree", "HEAD", "branch"} and value:
                fields[key] = value
        if fields.get("worktree") == expected:
            return fields
    return None


def _assert_tester_replacement_available(ledger: Mapping[str, Any]) -> None:
    if ledger["phase"] != "active":
        raise AssuranceError(
            "Tester replacement requires an active run",
            code="ASSURANCE_RUN_NOT_ACTIVE",
            status="FAIL",
        )
    conflicts = {
        "dispatch_intent": ledger.get("dispatch_intent"),
        "recomposition_intent": ledger.get("recomposition_intent"),
        "finalize_intent": ledger.get("finalize_intent"),
        "deployment_transaction": ledger.get("deployment_transaction"),
        "environment_lease": ledger.get("environment_lease"),
        "supersede_intent": ledger.get("supersede_intent"),
        "abandon_intent": ledger.get("abandon_intent"),
    }
    active = sorted(name for name, value in conflicts.items() if value is not None)
    if active:
        raise AssuranceError(
            "Tester replacement cannot overlap another transaction",
            code="TESTER_REPLACEMENT_TRANSACTION_CONFLICT",
            status="FAIL",
            details={"transactions": active},
        )


def _ensure_tester_replacement_worktree(
    repo: Path, intent: Mapping[str, Any]
) -> None:
    worktree = Path(str(intent["worktree"]))
    branch = str(intent["branch"])
    base = str(intent["source_base_head"])
    branch_result = git(
        repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False
    )
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if branch_result.returncode == 0 and branch_result.stdout.strip() != base:
            raise AssuranceError(
                "replacement Tester branch replay drifted",
                code="TESTER_REPLACEMENT_SOURCE_CONFLICT",
                status="FAIL",
            )
        registered = _registered_worktree(repo, worktree)
        expected_registered = {
            "worktree": str(worktree.resolve()),
            "HEAD": base,
            "branch": f"refs/heads/{branch}",
        }
        if registered is not None and registered != expected_registered:
            raise AssuranceError(
                "replacement Tester worktree registry drifted",
                code="TESTER_REPLACEMENT_SOURCE_CONFLICT",
                status="FAIL",
            )
        args = ["worktree", "add"]
        if registered is not None:
            args.append("--force")
        if branch_result.returncode == 0:
            args.extend([str(worktree), branch])
        else:
            args.extend(["-b", branch, str(worktree), base])
        created = git(
            repo,
            *args,
            check=False,
        )
        if created.returncode != 0:
            raise AssuranceError(
                created.stderr.strip() or "Tester replacement worktree creation failed",
                code="TESTER_REPLACEMENT_WORKTREE_CREATE_FAILED",
            )
        branch_result = git(
            repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False
        )
    if branch_result.returncode != 0 or not worktree.is_dir():
        raise AssuranceError(
            "replacement Tester branch and worktree are incomplete",
            code="TESTER_REPLACEMENT_SOURCE_CONFLICT",
            status="FAIL",
        )
    worktree_head = git(worktree, "rev-parse", "HEAD", check=False)
    worktree_branch = git(
        worktree, "symbolic-ref", "-q", "--short", "HEAD", check=False
    )
    if (
        branch_result.stdout.strip() != base
        or worktree_head.returncode != 0
        or worktree_head.stdout.strip() != base
        or worktree_branch.returncode != 0
        or worktree_branch.stdout.strip() != branch
        or dirty_paths(worktree)
    ):
        raise AssuranceError(
            "replacement Tester worktree replay drifted",
            code="TESTER_REPLACEMENT_SOURCE_CONFLICT",
            status="FAIL",
        )


def begin_tester_replacement(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    problem_key: str,
    driver_runtime_kind: str,
    renew_bootstrap: bool = False,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        existing = ledger.get("tester_replacement_intent")
        if renew_bootstrap:
            if not isinstance(existing, dict) or existing.get("action_id") != action_id:
                raise AssuranceError(
                    "Tester replacement bootstrap renewal is stale",
                    code="TESTER_REPLACEMENT_ACTION_STALE",
                    status="FAIL",
                )
            if existing.get("problem_key") != problem_key:
                raise AssuranceError(
                    "Tester replacement bootstrap problem changed",
                    code="TESTER_REPLACEMENT_PROBLEM_MISMATCH",
                    status="FAIL",
                )
            if ledger.get("dispatch_intent") is not None:
                raise AssuranceError(
                    "Tester bootstrap renewal cannot overlap a dispatch",
                    code="TESTER_REPLACEMENT_TRANSACTION_CONFLICT",
                    status="FAIL",
                )
            candidate = _assert_tester_replacement_candidate(repo, ledger)
            source = ledger["facets"]["execution"].get("tester_source")
            if (
                candidate != existing.get("candidate_head")
                or existing.get("stage") not in {"source_switched", "awaiting_first_turn"}
                or not isinstance(source, Mapping)
                or source.get("agent") != existing.get("new_agent")
                or source.get("head") != source.get("base_head")
                or source.get("files") != []
            ):
                raise AssuranceError(
                    "replacement Tester bootstrap has already been used or drifted",
                    code="TESTER_REPLACEMENT_BOOTSTRAP_ALREADY_USED",
                    status="FAIL",
                )
            _assert_tester_source_exact(repo, source)
            attempt = int(existing.get("bootstrap_attempt", 1))
            append_event(
                ledger,
                "tester_replacement_bootstrap_lost",
                {
                    "action_id": action_id,
                    "problem_key": problem_key,
                    "bootstrap_attempt": attempt,
                    "agent": copy.deepcopy(existing.get("new_agent")),
                    "exhausted": attempt >= 3,
                },
            )
            if attempt >= 3:
                save_ledger(repo, ledger)
                raise AssuranceError(
                    "Tester bootstrap identity failed three times",
                    code="TESTER_REPLACEMENT_ARCHITECTURE_REVIEW_REQUIRED",
                    status="NEEDS_USER",
                )
            existing["bootstrap_attempt"] = attempt + 1
            existing["new_agent"] = None
            existing["stage"] = str(existing["stage"])
            existing["updated_at"] = now()
            save_ledger(repo, ledger)
            return status(repo, run_id)
        if existing is not None:
            if (
                isinstance(existing, dict)
                and existing.get("action_id") == action_id
                and existing.get("problem_key") == problem_key
            ):
                _ensure_tester_replacement_worktree(repo, existing)
                return status(repo, run_id)
            raise AssuranceError(
                "another Tester replacement is already active",
                code="TESTER_REPLACEMENT_ALREADY_ACTIVE",
                status="FAIL",
            )
        _assert_tester_replacement_available(ledger)
        from .driver import next_action

        current = next_action(repo, run_id)
        if current.get("action") != "replace_tester" or current.get("action_id") != action_id:
            raise AssuranceError(
                "Tester replacement action is stale",
                code="TESTER_REPLACEMENT_ACTION_STALE",
                status="FAIL",
            )
        problem = _tester_replacement_problem(ledger, problem_key)
        execution = ledger["facets"]["execution"]
        old_agent = execution["agents"].get("tester")
        old_source = execution.get("tester_source")
        if (
            not isinstance(old_agent, dict)
            or not isinstance(old_source, dict)
            or old_source.get("agent") != old_agent
            or problem.get("producer")
            != {"role": "tester", **old_agent}
        ):
            raise AssuranceError(
                "Tester replacement producer does not match the current source",
                code="TESTER_REPLACEMENT_PRODUCER_MISMATCH",
                status="FAIL",
            )
        occurrences = sum(
            1
            for item in ledger.get("problems", [])
            if isinstance(item, Mapping)
            and item.get("producer_continuity") == "invalid"
        )
        if occurrences >= 3:
            raise AssuranceError(
                "Tester continuity failed three times on one candidate",
                code="TESTER_REPLACEMENT_ARCHITECTURE_REVIEW_REQUIRED",
                status="NEEDS_USER",
            )
        candidate = _assert_tester_replacement_candidate(repo, ledger)
        _assert_tester_source_exact(repo, old_source)
        publication = ledger.get("publication")
        source_base_head = (
            publication["head"]
            if isinstance(publication, dict)
            and publication.get("required")
            and publication.get("head")
            else ledger["target_start_head"]
        )
        snapshot_digest = _tester_replacement_problem_snapshot(ledger)
        sequence = 1 + sum(
            1
            for event in ledger.get("events", [])
            if isinstance(event, Mapping)
            and event.get("kind") == "tester_replacement_started"
        )
        branch = f"assurance-v4/{run_id}/tester-replacement-{sequence}"
        worktree = run_dir(repo, run_id) / f"tester-replacement-{sequence}"
        if (
            git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
            or worktree.exists()
        ):
            raise AssuranceError(
                "deterministic Tester replacement source already exists",
                code="TESTER_REPLACEMENT_SOURCE_CONFLICT",
                status="FAIL",
            )
        created_at = now()
        ledger["tester_replacement_intent"] = {
            "action_id": action_id,
            "problem_key": problem_key,
            "problem_snapshot_digest": snapshot_digest,
            "candidate_head": candidate,
            "target_start_head": ledger["target_start_head"],
            "source_base_head": source_base_head,
            "expected_execution_digest": ledger["digests"]["execution"],
            "expected_source_digest": digest(old_source),
            "branch": branch,
            "worktree": str(worktree),
            "stage": "prepared",
            "bootstrap_attempt": 1,
            "new_agent": None,
            "old_agent": copy.deepcopy(old_agent),
            "old_source": copy.deepcopy(old_source),
            "created_at": created_at,
            "updated_at": created_at,
        }
        append_event(
            ledger,
            "tester_replacement_started",
            {
                "action_id": action_id,
                "problem_key": problem_key,
                "candidate_head": candidate,
                "branch": branch,
                "worktree": str(worktree),
            },
        )
        save_ledger(repo, ledger)
        _ensure_tester_replacement_worktree(
            repo, ledger["tester_replacement_intent"]
        )
    return status(repo, run_id)


def bind_tester_replacement(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    agent_id: str,
    thread_id: str,
    driver_runtime_kind: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    agent = {"agent_id": agent_id.strip(), "thread_id": thread_id.strip()}
    if not all(agent.values()):
        raise AssuranceError(
            "Tester replacement identity is required",
            code="TESTER_IDENTITY_REQUIRED",
            status="FAIL",
        )
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        intent = ledger.get("tester_replacement_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError(
                "Tester replacement identity bind is stale",
                code="TESTER_REPLACEMENT_ACTION_STALE",
                status="FAIL",
            )
        if ledger.get("dispatch_intent") is not None:
            raise AssuranceError(
                "Tester replacement identity cannot overlap a dispatch",
                code="TESTER_REPLACEMENT_TRANSACTION_CONFLICT",
                status="FAIL",
            )
        stage = str(intent.get("stage"))
        snapshot_digest = _tester_replacement_problem_snapshot(ledger)
        if snapshot_digest != intent.get("problem_snapshot_digest"):
            raise AssuranceError(
                "Tester replacement problem snapshot drifted",
                code="TESTER_REPLACEMENT_PROBLEM_DRIFT",
                status="FAIL",
            )
        if stage == "prepared" and intent.get("new_agent") is None:
            _assert_tester_replacement_candidate(repo, ledger)
            _ensure_tester_replacement_worktree(repo, intent)
            intent["new_agent"] = agent
            intent["stage"] = "identity_bound"
        elif stage in {"source_switched", "awaiting_first_turn"} and intent.get("new_agent") is None:
            _assert_tester_replacement_candidate(repo, ledger)
            source = ledger["facets"]["execution"].get("tester_source")
            if (
                not isinstance(source, dict)
                or str(source.get("branch")) != str(intent.get("branch"))
                or str(source.get("worktree")) != str(intent.get("worktree"))
                or source.get("head") != source.get("base_head")
                or source.get("files") != []
            ):
                raise AssuranceError(
                    "replacement Tester bootstrap has already been used or drifted",
                    code="TESTER_REPLACEMENT_BOOTSTRAP_ALREADY_USED",
                    status="FAIL",
                )
            _ensure_tester_replacement_worktree(repo, intent)
            _assert_tester_source_exact(repo, source)
            execution = ledger["facets"]["execution"]
            execution["agents"]["tester"] = agent
            source["agent"] = agent
            execution["version"] += 1
            ledger["digests"] = facet_digests(ledger["facets"])
            intent["new_agent"] = agent
            intent["stage"] = stage
        else:
            if intent.get("new_agent") == agent:
                return status(repo, run_id)
            raise AssuranceError(
                "Tester replacement identity cannot be overwritten",
                code="TESTER_REPLACEMENT_IDENTITY_CONFLICT",
                status="FAIL",
            )
        intent["updated_at"] = now()
        append_event(
            ledger,
            "tester_replacement_identity_bound",
            {
                "action_id": action_id,
                "bootstrap_attempt": intent["bootstrap_attempt"],
                "agent": agent,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def complete_tester_replacement(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str,
    driver_runtime_kind: str,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        _require_driver_runtime_owner(ledger, driver_runtime_kind)
        intent = ledger.get("tester_replacement_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            raise AssuranceError(
                "Tester replacement completion is stale",
                code="TESTER_REPLACEMENT_ACTION_STALE",
                status="FAIL",
            )
        if intent.get("stage") == "awaiting_first_turn":
            return status(repo, run_id)
        if intent.get("stage") not in {"identity_bound", "source_switched"} or not isinstance(intent.get("new_agent"), dict):
            raise AssuranceError(
                "Tester replacement identity is not bound",
                code="TESTER_REPLACEMENT_IDENTITY_MISSING",
                status="FAIL",
            )
        candidate = _assert_tester_replacement_candidate(repo, ledger)
        snapshot_digest = _tester_replacement_problem_snapshot(ledger)
        if snapshot_digest != intent.get("problem_snapshot_digest"):
            raise AssuranceError(
                "Tester replacement problem snapshot drifted",
                code="TESTER_REPLACEMENT_PROBLEM_DRIFT",
                status="FAIL",
            )
        execution = ledger["facets"]["execution"]
        old_source = intent["old_source"]
        old_agent = intent["old_agent"]
        if intent.get("stage") == "identity_bound" and (
            candidate != intent["candidate_head"]
            or ledger["target_start_head"] != intent["target_start_head"]
            or execution.get("agents", {}).get("tester") != old_agent
            or execution.get("tester_source") != old_source
            or ledger["digests"]["execution"] != intent["expected_execution_digest"]
            or digest(old_source) != intent["expected_source_digest"]
        ):
            raise AssuranceError(
                "Tester replacement binding drifted",
                code="TESTER_REPLACEMENT_EXECUTION_DRIFT",
                status="FAIL",
            )
        if intent.get("stage") == "identity_bound":
            _assert_tester_replacement_available(ledger)
            _assert_tester_source_exact(repo, old_source)
            tester_base = str(intent["source_base_head"])
            _ensure_tester_replacement_worktree(repo, intent)
            replacement = {
                "head": tester_base,
                "base_head": tester_base,
                "branch": intent["branch"],
                "worktree": intent["worktree"],
                "files": [],
                "replaces_files": copy.deepcopy(old_source.get("files", [])),
                "agent": copy.deepcopy(intent["new_agent"]),
            }
            execution["version"] += 1
            execution["agents"]["tester"] = copy.deepcopy(intent["new_agent"])
            execution["tester_source"] = replacement
            execution["tester_files"] = []
            ledger["retired_tester_sources"].append(copy.deepcopy(old_source))
            ledger["digests"] = facet_digests(ledger["facets"])
            intent["stage"] = "source_switched"
            intent["updated_at"] = now()
            append_event(
                ledger,
                "tester_replacement_source_switched",
                {
                    "action_id": action_id,
                    "old_agent": old_agent,
                    "new_agent": intent["new_agent"],
                    "branch": intent["branch"],
                    "worktree": intent["worktree"],
                },
            )
            save_ledger(repo, ledger)
        replacement_source = execution.get("tester_source")
        if (
            execution.get("agents", {}).get("tester") != intent.get("new_agent")
            or not isinstance(replacement_source, Mapping)
            or replacement_source.get("agent") != intent.get("new_agent")
            or replacement_source.get("head") != intent.get("source_base_head")
            or replacement_source.get("base_head") != intent.get("source_base_head")
            or replacement_source.get("branch") != intent.get("branch")
            or replacement_source.get("worktree") != intent.get("worktree")
            or replacement_source.get("files") != []
            or execution.get("tester_files") != []
        ):
            raise AssuranceError(
                "Tester replacement source switch drifted",
                code="TESTER_REPLACEMENT_EXECUTION_DRIFT",
                status="FAIL",
            )
        _ensure_tester_replacement_worktree(repo, intent)
        _assert_tester_source_exact(repo, replacement_source)
        old_worktree = Path(old_source["worktree"])
        worktree_error = ""
        if old_worktree.exists():
            removed_worktree = git(
                repo, "worktree", "remove", str(old_worktree), check=False
            )
            worktree_error = removed_worktree.stderr[-8000:]
            worktree_ok = removed_worktree.returncode == 0
        else:
            worktree_ok = True
        old_ref = git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/heads/{old_source['branch']}",
            check=False,
        )
        branch_error = ""
        if not worktree_ok:
            branch_ok = False
            branch_error = "branch removal was not attempted after worktree removal failed"
        elif old_ref.returncode == 0:
            if old_ref.stdout.strip() != old_source["head"]:
                raise AssuranceError(
                    "retired Tester branch drifted and was preserved",
                    code="TESTER_REPLACEMENT_SOURCE_DRIFT",
                    status="FAIL",
                )
            removed_branch = git(
                repo, "branch", "-D", old_source["branch"], check=False
            )
            branch_error = removed_branch.stderr[-8000:]
            branch_ok = removed_branch.returncode == 0
        else:
            branch_ok = True
        if not worktree_ok or not branch_ok:
            raise AssuranceError(
                "Tester replacement was persisted but retired source cleanup is pending",
                code="TESTER_RETIRED_CLEANUP_PENDING",
                status="NEEDS_USER",
                details={
                    "branch": old_source["branch"],
                    "worktree": str(old_worktree),
                    "worktree_remove_stderr": worktree_error,
                    "branch_remove_stderr": branch_error,
                },
            )
        ledger["retired_tester_sources"] = [
            item
            for item in ledger["retired_tester_sources"]
            if item.get("branch") != old_source["branch"]
        ]
        intent["stage"] = "awaiting_first_turn"
        intent["updated_at"] = now()
        append_event(
            ledger,
            "retired_tester_source_cleaned",
            {"branch": old_source["branch"]},
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
    identity_only: bool = False,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    if not agent_id.strip() or not thread_id.strip():
        raise AssuranceError("Tester identity is required", code="TESTER_IDENTITY_REQUIRED")
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
        execution = ledger["facets"]["execution"]
        agent = {"agent_id": agent_id.strip(), "thread_id": thread_id.strip()}
        existing_agent = execution["agents"].get("tester")
        existing = execution.get("tester_source")
        if identity_only:
            if replace:
                raise AssuranceError(
                    "Tester identity-only preparation cannot replace source continuity",
                    code="TESTER_IDENTITY_ONLY_REPLACEMENT_INVALID",
                    status="NEEDS_USER",
                )
            if isinstance(existing, dict):
                if existing["agent"] == agent:
                    return status(repo, run_id)
                raise AssuranceError(
                    "Tester continuity replacement must preserve its source transaction",
                    code="TESTER_CONTINUITY_REPLACEMENT_REQUIRED",
                    status="NEEDS_USER",
                )
            if isinstance(existing_agent, dict):
                if existing_agent == agent:
                    return status(repo, run_id)
                raise AssuranceError(
                    "Tester continuity replacement must be explicit",
                    code="TESTER_CONTINUITY_REPLACEMENT_REQUIRED",
                    status="NEEDS_USER",
                )
            execution["version"] += 1
            execution["agents"]["tester"] = agent
            ledger["digests"] = facet_digests(ledger["facets"])
            append_event(
                ledger,
                "tester_identity_prepared",
                {"agent": agent},
            )
            save_ledger(repo, ledger)
            return status(repo, run_id)
        if isinstance(existing, dict):
            if existing["agent"] == agent:
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
        elif isinstance(existing_agent, dict) and existing_agent != agent:
            raise AssuranceError(
                "Tester continuity replacement must be explicit",
                code="TESTER_CONTINUITY_REPLACEMENT_REQUIRED",
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
        _assert_no_candidate_residue_intent(ledger)
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
            retired_paths = sorted(
                {
                    item["path"]
                    for item in [
                        *source.get("files", []),
                        *source.get("replaces_files", []),
                    ]
                }
                - new_paths
            )
            for path in retired_paths:
                if _blob_at(repo, source_head, path) is None:
                    removed = git(
                        candidate_worktree,
                        "rm",
                        "-r",
                        "--ignore-unmatch",
                        "--",
                        path,
                        check=False,
                    )
                    if removed.returncode != 0:
                        raise AssuranceError(
                            "retired Tester source could not remove an old file",
                            code="TESTER_REPLACEMENT_REMOVE_FAILED",
                            details={"path": path, "stderr": removed.stderr[-8000:]},
                        )
                    continue
                restored = git(
                    candidate_worktree,
                    "checkout",
                    source_head,
                    "--",
                    path,
                    check=False,
                )
                if restored.returncode != 0:
                    raise AssuranceError(
                        "Tester source could not restore a retired path",
                        code="TESTER_INTEGRATION_CHECKOUT_FAILED",
                        details={"path": path, "stderr": restored.stderr[-8000:]},
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
    if kind not in EVIDENCE_KINDS or kind in {"preflight", "machine"}:
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
        _assert_no_candidate_residue_intent(ledger)
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if report.get("candidate_head") != candidate:
            raise AssuranceError(
                "evidence candidate does not match the execution manifest",
                code="EVIDENCE_CANDIDATE_MISMATCH",
            )
        if kind in {"reviewer_preflight", "reviewer", "doc_review"} and report[
            "status"
        ] == "pass":
            scan_state = doc_reference_scan_state(ledger)
            if scan_state not in {"pass", "not_required"}:
                raise AssuranceError(
                    "Reviewer evidence requires a current passing documentation reference scan",
                    code="DOC_REFERENCE_SCAN_BLOCKING",
                    status="NEEDS_USER",
                    details={"doc_reference_scan_state": scan_state},
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
            _validate_blackbox_report(ledger, report)
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


def _validate_blackbox_report(ledger: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    candidate = ledger["facets"]["execution"].get("candidate_head")
    producer = report["producer"]
    expected_producer = ledger["facets"]["execution"]["agents"].get("tester")
    if not isinstance(expected_producer, Mapping) or {
        "agent_id": producer["agent_id"],
        "thread_id": producer["thread_id"],
    } != expected_producer:
        raise AssuranceError(
            "evidence producer does not match the execution manifest",
            code="EVIDENCE_PRODUCER_MISMATCH",
        )
    if report.get("candidate_head") != candidate:
        raise AssuranceError(
            "evidence candidate does not match the execution manifest",
            code="EVIDENCE_CANDIDATE_MISMATCH",
        )
    details = report["details"]
    if details["result"] != report["status"]:
        raise AssuranceError(
            "blackbox result does not match evidence status",
            code="EVIDENCE_RESULT_MISMATCH",
        )
    if Path(details["worktree"]).resolve() != Path(ledger["candidate_worktree"]).resolve():
        raise AssuranceError(
            "blackbox worktree is not the candidate worktree",
            code="BLACKBOX_WORKTREE_MISMATCH",
        )
    if details["before_head"] != candidate or details["after_head"] != candidate:
        raise AssuranceError(
            "blackbox execution changed or missed the candidate HEAD",
            code="BLACKBOX_HEAD_MISMATCH",
        )
    declared_commands = [
        (item["id"], item["argv"])
        for item in ledger["facets"]["execution"]["commands"]
    ]
    observed_commands = [
        (item["id"], item["argv"])
        for item in details["executions"]
    ]
    if not declared_commands or observed_commands != declared_commands:
        raise AssuranceError(
            "blackbox executions are not frozen in Execution",
            code="BLACKBOX_COMMAND_MISMATCH",
        )
    expected_returncodes = {
        item["id"]: item["expected_returncodes"]
        for item in ledger["facets"]["execution"]["commands"]
    }
    if report["status"] == "pass" and any(
        item["timed_out"]
        or item["returncode"] not in expected_returncodes[item["id"]]
        for item in details["executions"]
    ):
        raise AssuranceError(
            "blackbox execution did not match its frozen expected return codes",
            code="BLACKBOX_EXECUTION_FAILED",
        )
    _validate_blackbox_case_bindings(ledger, report)


def _validate_blackbox_case_bindings(
    ledger: Mapping[str, Any], report: Mapping[str, Any]
) -> None:
    contract = ledger["facets"]
    mode = acceptance_observation_mode(contract)
    cases = report["details"].get("cases")
    if mode == "legacy":
        if cases is not None:
            raise AssuranceError(
                "legacy acceptance contracts cannot bind new blackbox case evidence",
                code="BLACKBOX_CASE_COVERAGE_MISMATCH",
            )
        return
    if not isinstance(cases, list):
        raise AssuranceError(
            "blackbox evidence must cover every frozen acceptance case",
            code="BLACKBOX_CASE_COVERAGE_MISMATCH",
        )
    expected_cases = {
        item["id"]: item for item in contract["mission"]["acceptance_cases"]
    }
    observed_ids = [item.get("case_id") for item in cases if isinstance(item, Mapping)]
    if len(observed_ids) != len(set(observed_ids)) or set(observed_ids) != set(expected_cases):
        raise AssuranceError(
            "blackbox case ids do not exactly cover the frozen acceptance cases",
            code="BLACKBOX_CASE_COVERAGE_MISMATCH",
            details={
                "expected": sorted(expected_cases),
                "observed": sorted(str(item) for item in observed_ids),
            },
        )
    executed_ids = {item["id"] for item in report["details"]["executions"]}
    outcomes: list[str] = []
    for observed in cases:
        expected = expected_cases[observed["case_id"]]["observation"]
        expected_target = expected.get("target_id")
        observed_target = observed.get("target_id")
        if (
            observed.get("surface_id") != expected["surface_id"]
            or set(observed.get("execution_ids", [])) != set(expected["execution_ids"])
            or observed_target != expected_target
            or not set(observed.get("execution_ids", [])).issubset(executed_ids)
        ):
            raise AssuranceError(
                "blackbox case evidence does not match its frozen observation surface",
                code="BLACKBOX_OBSERVATION_BINDING_MISMATCH",
                details={"case_id": observed["case_id"]},
            )
        statuses = {
            dimension: observed[dimension]["status"]
            for dimension in ("mechanical", "verify", "quality")
        }
        required = set(expected["required_dimensions"])
        derived = (
            "pass"
            if all(statuses[item] == "pass" for item in required)
            and all(value != "fail" for value in statuses.values())
            else "fail"
        )
        if observed["outcome"] != derived:
            raise AssuranceError(
                "blackbox case outcome does not match its required dimensions",
                code="BLACKBOX_CASE_RESULT_MISMATCH",
                details={"case_id": observed["case_id"], "derived": derived},
            )
        outcomes.append(derived)
    derived_report = "pass" if all(item == "pass" for item in outcomes) else "fail"
    if report["status"] != derived_report:
        raise AssuranceError(
            "blackbox report status does not match its case outcomes",
            code="BLACKBOX_CASE_RESULT_MISMATCH",
            details={"derived": derived_report},
        )


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
    from ..core import (
        parse_canonical_uv_proof_command,
        proof_pytest_args,
        proof_unittest_args,
        run_proof_argv,
    )

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
    executable = Path(requested[0]).name.lower()
    if executable == "uv":
        parsed = parse_canonical_uv_proof_command(requested)
        assert parsed is not None
        execution_argv = [
            resolved,
            *parsed["prefix"],
            *parsed["execution_tail"],
        ]
    elif executable in {"pytest", "py.test"}:
        execution_argv = [resolved, *proof_pytest_args(requested[1:])]
    elif framework == "pytest":
        execution_argv = [
            resolved,
            "-m",
            "pytest",
            *proof_pytest_args(requested[3:]),
        ]
    else:
        execution_argv = [
            resolved,
            "-m",
            "unittest",
            *proof_unittest_args(requested[3:]),
        ]
    proof_identity = copy.deepcopy(identity)
    if proof_identity.get("kind") == "system":
        proof_identity["size"] = int(Path(resolved).stat().st_size)
    result = run_proof_argv(
        execution_argv,
        framework=framework,
        test_ids=list(group["test_ids"]),
        worktree=worktree,
        timeout=int(group["timeout_seconds"]),
        log_path=artifact_root / f"{label}.log",
        cache_path=artifact_root / f"{label}-cache",
        include_supervisor_observation=True,
    )
    return {
        **result,
        "requested_argv": requested,
        "executable_identity": proof_identity,
        "project_identity": project_identity,
    }


def _proof_worktree_residue(
    worktree: Path, *, allowed_paths: Sequence[str] = ()
) -> list[str]:
    ignored = git(
        worktree,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    ).stdout.split("\0")
    allowed = set(allowed_paths)
    return sorted(
        path
        for path in set(dirty_paths(worktree)) | {item for item in ignored if item}
        if path not in allowed
    )


def _proof_failure_action_id(value: str | None) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def _stable_proof_failure_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        has_requested_argv = isinstance(value.get("requested_argv"), list)
        return {
            str(key): _stable_proof_failure_value(item)
            for key, item in value.items()
            if key not in PROOF_FAILURE_VOLATILE_DETAIL_KEYS
            and not (key == "argv" and has_requested_argv)
        }
    if isinstance(value, list):
        return [_stable_proof_failure_value(item) for item in value]
    return copy.deepcopy(value)


def _proof_failure_signature(error: AssuranceError) -> str:
    return digest(
        {
            "code": error.code,
            "details": _stable_proof_failure_value(error.details),
        }
    )


def _raise_recorded_proof_failure(record: Mapping[str, Any]) -> None:
    failure = record["failure"]
    raise AssuranceError(
        str(failure["message"]),
        code=str(failure["code"]),
        status=str(failure["status"]),
        details=copy.deepcopy(failure["details"]),
    )


def _record_proof_failure(
    ledger: dict[str, Any],
    *,
    spec: Mapping[str, Any],
    action_id: str | None,
    agent_id: str,
    thread_id: str,
    error: AssuranceError,
    artifact_root: Path | None,
    duration_ms: int,
) -> dict[str, Any]:
    execution = ledger["facets"]["execution"]
    source = execution.get("tester_source")
    failure = {
        "status": error.status,
        "code": error.code,
        "message": str(error),
        "details": copy.deepcopy(error.details),
    }
    recovery = (
        "tester_diagnosis"
        if error.code in PROOF_TESTER_DIAGNOSIS_CODES
        else "needs_user"
    )
    base = {
        "action_id": _proof_failure_action_id(action_id),
        "candidate_head": execution.get("candidate_head"),
        "tester_source_head": source.get("head") if isinstance(source, Mapping) else None,
        "producer": {"role": "tester", "agent_id": agent_id, "thread_id": thread_id},
        "spec": copy.deepcopy(spec),
        "spec_digest": digest(spec),
        "dependency_digest": evidence_dependency(ledger, "proof"),
        "failure": failure,
        "recovery": recovery,
        "failure_signature": _proof_failure_signature(error),
        "artifact_root": str(artifact_root) if artifact_root is not None else None,
    }
    record = {
        **base,
        "failure_digest": digest(base),
        "recorded_at": now(),
    }
    ledger["proof_failure"] = record
    append_event(
        ledger,
        "proof_failure_recorded",
        {
            "action_id": record["action_id"],
            "candidate_head": record["candidate_head"],
            "code": error.code,
            "recovery": recovery,
            "spec_digest": record["spec_digest"],
            "failure_signature": record["failure_signature"],
            "failure_digest": record["failure_digest"],
            "artifact_root": record["artifact_root"],
            "duration_ms": duration_ms,
        },
    )
    return record


def prove_tests(
    repo_value: str | Path,
    run_value: str,
    spec_value: Any,
    *,
    agent_id: str,
    thread_id: str,
    action_id: str | None = None,
) -> dict[str, Any]:
    spec = validate_test_proof_spec(spec_value)
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    started_at = time.monotonic()
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
        if "proof" not in ledger["facets"]["assurance"]["required"]:
            raise AssuranceError("test proof is not required", code="TEST_PROOF_NOT_REQUIRED")
        expected_agent = ledger["facets"]["execution"]["agents"].get("tester")
        if expected_agent != {"agent_id": agent_id, "thread_id": thread_id}:
            raise AssuranceError("proof producer identity mismatch", code="EVIDENCE_PRODUCER_MISMATCH")
        producer = {"role": "tester", "agent_id": agent_id, "thread_id": thread_id}
        existing_proof = ledger.get("evidence", {}).get("proof")
        if (
            isinstance(existing_proof, dict)
            and evidence_state(ledger, "proof") == "pass"
            and existing_proof.get("producer") == producer
            and existing_proof.get("details", {}).get("spec") == spec
        ):
            return status(repo, run_id)
        normalized_action_id = _proof_failure_action_id(action_id)
        existing_failure = current_proof_failure(ledger)
        if (
            isinstance(existing_failure, dict)
            and existing_failure.get("action_id") == normalized_action_id
        ):
            input_changed = (
                existing_failure.get("spec_digest") != digest(spec)
                or existing_failure.get("producer") != producer
            )
            if not input_changed:
                _raise_recorded_proof_failure(existing_failure)
            if normalized_action_id is not None:
                raise AssuranceError(
                    "proof failure replay changed the completed action input",
                    code="PROOF_FAILURE_REPLAY_MISMATCH",
                    status="NEEDS_USER",
                )

        artifact_root: Path | None = None

        def fail(error: AssuranceError) -> None:
            _record_proof_failure(
                ledger,
                spec=spec,
                action_id=action_id,
                agent_id=agent_id,
                thread_id=thread_id,
                error=error,
                artifact_root=artifact_root,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            save_ledger(repo, ledger)
            raise error

        def capture(call):
            try:
                return call()
            except AssuranceError as error:
                fail(error)

        if evidence_state(ledger, "tester") != "pass":
            fail(
                AssuranceError(
                    "test proof requires current Tester author evidence",
                    code="TEST_PROOF_TESTER_MISSING",
                    status="FAIL",
                )
            )
        execution = ledger["facets"]["execution"]
        candidate = execution.get("candidate_head")
        tester_source = execution.get("tester_source")
        if not isinstance(candidate, str) or not isinstance(tester_source, dict):
            fail(AssuranceError("test proof source is unavailable", code="TEST_PROOF_SOURCE_MISSING"))
        behavior_ids = [item["id"] for item in ledger["facets"]["mission"]["behaviors"]]
        observed_behaviors = [
            behavior
            for group in spec["groups"]
            for behavior in group["behavior_ids"]
        ]
        if sorted(observed_behaviors) != sorted(behavior_ids) or len(
            observed_behaviors
        ) != len(set(observed_behaviors)):
            fail(
                AssuranceError(
                    "test proof groups must cover every frozen behavior exactly once",
                    code="PROOF_BEHAVIOR_COVERAGE_MISMATCH",
                )
            )
        tester_patterns = ledger["facets"]["authority"]["tester_write"]
        tester_scope = _proof_tester_scope(
            repo,
            tester_source["head"],
            tester_patterns,
        )
        if tester_scope is None:
            fail(
                AssuranceError(
                    "Tester-owned proof scope cannot be derived from the frozen source",
                    code="TEST_PROOF_MANIFEST_MISMATCH",
                    status="FAIL",
                )
            )
        source_manifest = {item["path"]: item["blob"] for item in tester_scope}
        mismatched = [
            path
            for path, blob in source_manifest.items()
            if _regular_blob_at(repo, candidate, path) != blob
        ]
        if mismatched:
            fail(
                AssuranceError(
                    "Tester source differs from the integrated candidate",
                    code="TEST_PROOF_MANIFEST_MISMATCH",
                    status="FAIL",
                    details={"paths": mismatched},
                )
            )
        from ..core import proof_test_source_path

        unbound_tests: list[dict[str, str]] = []
        for group in spec["groups"]:
            framework = capture(lambda: _proof_framework(list(group["argv"])))
            if group["method"] == "reviewed-boundaries":
                boundary_ids = _proof_reviewed_boundary_ids(group)
                if boundary_ids != set(group["test_ids"]):
                    fail(
                        AssuranceError(
                            "reviewed-boundaries ids must exactly equal the executed Tester test ids",
                            code="TEST_PROOF_BOUNDARY_TEST_IDS_INVALID",
                            status="FAIL",
                            details={
                                "declared_test_ids": sorted(group["test_ids"]),
                                "reviewed_boundary_ids": sorted(boundary_ids or []),
                            },
                        )
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
            fail(
                AssuranceError(
                    "test proof ids are not bound to Tester-owned source",
                    code="TEST_PROOF_TEST_SOURCE_UNBOUND",
                    status="FAIL",
                    details={"tests": unbound_tests},
                )
            )
        launcher_identities: list[dict[str, Any] | None] = []
        candidate_worktree_path = Path(ledger["candidate_worktree"])
        for group in spec["groups"]:
            requested = list(group["argv"])
            capture(lambda: _proof_framework(requested))
            if requested and Path(requested[0]).name.lower() == "uv":
                resolved, identity = capture(
                    lambda: _resolve_machine_executable(
                        repo, candidate_worktree_path, candidate, requested[0]
                    )
                )
                if resolved is None:
                    fail(
                        AssuranceError(
                            "test proof uv launcher could not be resolved",
                            code="TEST_PROOF_EXECUTABLE_INVALID",
                            status="FAIL",
                            details={"identity": identity},
                        )
                    )
                capture(
                    lambda: _proof_uv_project_identity(
                        repo, candidate_worktree_path, candidate, requested
                    )
                )
                launcher_identities.append(identity)
            else:
                launcher_identities.append(None)
        artifact_root = run_dir(repo, run_id) / "proof-artifacts" / digest(spec)
        artifact_root.mkdir(parents=True, exist_ok=True)
        proof_root = Path(tempfile.mkdtemp(prefix=f"assurance-v4-{run_id}-proof-"))
        created_worktrees: list[Path] = []
        results: list[dict[str, Any]] = []
        candidate_results: dict[int, dict[str, Any]] = {}
        candidate_failures: list[dict[str, Any]] = []
        try:
            for index, group in enumerate(spec["groups"]):
                candidate_worktree = proof_root / f"candidate-{index}"
                added = git(repo, "worktree", "add", "--detach", str(candidate_worktree), candidate, check=False)
                if added.returncode != 0:
                    fail(
                        AssuranceError(
                            "proof candidate worktree creation failed",
                            code="TEST_PROOF_WORKTREE_CREATE_FAILED",
                        )
                    )
                created_worktrees.append(candidate_worktree)
                candidate_result = capture(
                    lambda: _proof_run(
                        repo,
                        candidate_worktree,
                        candidate,
                        group,
                        artifact_root,
                        f"group-{index}-candidate",
                        launcher_identities[index],
                    )
                )
                candidate_result["worktree_residue"] = _proof_worktree_residue(
                    candidate_worktree
                )
                candidate_results[index] = candidate_result
                if (
                    candidate_result["test_result"].get("classification") != "pass"
                    or candidate_result["worktree_residue"]
                ):
                    candidate_failures.append(
                        {"group": index, "result": candidate_result}
                    )

            if candidate_failures:
                failure_details = copy.deepcopy(candidate_failures[0])
                if len(candidate_failures) > 1:
                    failure_details["failures"] = copy.deepcopy(candidate_failures)
                fail(
                    AssuranceError(
                        "candidate tests did not pass before effectiveness proof",
                        code="TEST_PROOF_CANDIDATE_FAILED",
                        status="FAIL",
                        details=failure_details,
                    )
                )

            for index, group in enumerate(spec["groups"]):
                candidate_result = candidate_results[index]
                method = group["method"]
                counterexample: dict[str, Any] | None = None
                mutation_evidence: dict[str, Any] | None = None
                if method == "baseline-red":
                    baseline_worktree = proof_root / f"baseline-{index}"
                    added = git(repo, "worktree", "add", "--detach", str(baseline_worktree), tester_source["head"], check=False)
                    if added.returncode != 0:
                        fail(
                            AssuranceError(
                                "proof baseline worktree creation failed",
                                code="TEST_PROOF_WORKTREE_CREATE_FAILED",
                            )
                        )
                    created_worktrees.append(baseline_worktree)
                    counterexample = capture(
                        lambda: _proof_run(
                            repo,
                            baseline_worktree,
                            tester_source["head"],
                            group,
                            artifact_root,
                            f"group-{index}-baseline",
                            launcher_identities[index],
                        )
                    )
                    counterexample["worktree_residue"] = _proof_worktree_residue(
                        baseline_worktree
                    )
                elif method == "mutation":
                    mutation_worktree = proof_root / f"mutation-{index}"
                    added = git(repo, "worktree", "add", "--detach", str(mutation_worktree), candidate, check=False)
                    if added.returncode != 0:
                        fail(
                            AssuranceError(
                                "proof mutation worktree creation failed",
                                code="TEST_PROOF_WORKTREE_CREATE_FAILED",
                            )
                        )
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
                        fail(
                            AssuranceError(
                                "test proof mutation patch could not be applied",
                                code="TEST_PROOF_MUTATION_INVALID",
                                status="FAIL",
                                details={"stderr": applied.stderr[-8000:]},
                            )
                        )
                    mutation_paths = sorted(dirty_paths(mutation_worktree))
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
                        fail(
                            AssuranceError(
                                "test proof mutation escaped Builder-owned implementation files",
                                code="TEST_PROOF_MUTATION_AUTHORITY_VIOLATION",
                                status="FAIL",
                                details={"paths": invalid_paths or mutation_paths},
                            )
                        )
                    mutation_diff = git(
                        mutation_worktree,
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--binary",
                        "--full-index",
                        candidate,
                        "--",
                    ).stdout
                    mutation_diff_sha256 = hashlib.sha256(
                        mutation_diff.encode()
                    ).hexdigest()
                    counterexample = capture(
                        lambda: _proof_run(
                            repo,
                            mutation_worktree,
                            candidate,
                            group,
                            artifact_root,
                            f"group-{index}-mutation",
                            launcher_identities[index],
                        )
                    )
                    counterexample["worktree_residue"] = _proof_worktree_residue(
                        mutation_worktree,
                        allowed_paths=mutation_paths,
                    )
                    mutation_after_paths = sorted(dirty_paths(mutation_worktree))
                    mutation_after_diff = git(
                        mutation_worktree,
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--binary",
                        "--full-index",
                        candidate,
                        "--",
                    ).stdout
                    mutation_head_after = git(
                        mutation_worktree, "rev-parse", "HEAD"
                    ).stdout.strip()
                    if (
                        mutation_after_paths != mutation_paths
                        or mutation_after_diff != mutation_diff
                        or mutation_head_after != candidate
                    ):
                        fail(
                            AssuranceError(
                                "test proof mutation changed during execution",
                                code="TEST_PROOF_MUTATION_INVALID",
                                status="FAIL",
                                details={
                                    "paths_before": mutation_paths,
                                    "paths_after": mutation_after_paths,
                                    "diff_sha256_before": mutation_diff_sha256,
                                    "diff_sha256_after": hashlib.sha256(
                                        mutation_after_diff.encode()
                                    ).hexdigest(),
                                    "head_before": candidate,
                                    "head_after": mutation_head_after,
                                },
                            )
                        )
                    mutation_evidence = {
                        **counterexample,
                        "patch_sha256": hashlib.sha256(
                            str(group["patch"]).encode()
                        ).hexdigest(),
                        "applied_diff": mutation_diff,
                        "applied_diff_sha256": mutation_diff_sha256,
                        "changed_paths": mutation_paths,
                        "head_before": candidate,
                        "head_after": mutation_head_after,
                    }
                if counterexample is not None and (
                    counterexample["test_result"].get("classification")
                    != "assertion-failure"
                    or counterexample["worktree_residue"]
                ):
                    fail(
                        AssuranceError(
                            "test proof counterexample was not an assertion failure",
                            code="TEST_PROOF_COUNTEREXAMPLE_INVALID",
                            status="FAIL",
                            details={"group": index, "result": counterexample},
                        )
                    )
                if counterexample is not None and (
                    counterexample.get("executable_identity")
                    != candidate_result.get("executable_identity")
                ):
                    fail(
                        AssuranceError(
                            "test proof executable identity changed across worktrees",
                            code="TEST_PROOF_EXECUTABLE_IDENTITY_DRIFT",
                            status="FAIL",
                            details={"group": index},
                        )
                    )
                if counterexample is not None and (
                    counterexample.get("project_identity")
                    != candidate_result.get("project_identity")
                ):
                    fail(
                        AssuranceError(
                            "test proof project identity changed across worktrees",
                            code="TEST_PROOF_UV_PROJECT_INVALID",
                            status="FAIL",
                            details={"group": index},
                        )
                    )
                group_result = {
                    "behavior_ids": list(group["behavior_ids"]),
                    "method": method,
                    "argv": list(group["argv"]),
                    "execution_argv": list(candidate_result["argv"]),
                    "framework": candidate_result["test_result"]["framework"],
                    "executable_identity": copy.deepcopy(
                        candidate_result["executable_identity"]
                    ),
                    "project_identity": copy.deepcopy(
                        candidate_result["project_identity"]
                    ),
                    "test_ids": list(group["test_ids"]),
                    "timeout_seconds": group["timeout_seconds"],
                    "candidate": candidate_result,
                }
                if method == "mutation":
                    assert mutation_evidence is not None
                    group_result["mutation"] = mutation_evidence
                else:
                    group_result["counterexample"] = counterexample
                if method == "baseline-red":
                    group_result["claimed_failure_kind"] = group[
                        "claimed_failure_kind"
                    ]
                elif method == "reviewed-boundaries":
                    group_result["reason"] = group["reason"]
                    group_result["reviewed_boundaries"] = copy.deepcopy(
                        group["reviewed_boundaries"]
                    )
                results.append(group_result)
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
        ledger["proof_failure"] = None
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


def _resolve_host_machine_executable(
    value: str,
) -> tuple[str | None, dict[str, Any]]:
    requested = Path(value)
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
    try:
        executable_sha256 = _sha256_file(resolved)
    except OSError:
        return None, {"kind": "system", "requested": value, "reason": "not_found"}
    return str(resolved), {
        "kind": "system",
        "requested": value,
        "resolution": resolution,
        "path": str(resolved),
        "sha256": executable_sha256,
    }


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

    return _resolve_host_machine_executable(value)


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
    result = run_owned_command(
        [executable, *command["argv"][1:]],
        cwd=str(worktree),
        timeout=command["timeout_seconds"],
        executable_identity=identity,
        env=env,
    )
    return {
        "id": command["id"],
        "argv": command["argv"],
        "returncode": result["returncode"],
        "stdout": result["stdout"][-8000:],
        "stderr": result["stderr"][-8000:],
        "timed_out": result["timed_out"],
        "executable_identity": identity,
        "process_identity": result["process_identity"],
        "cleanup": result["cleanup"],
    }


def _command_passed(command: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    cleanup = result.get("cleanup")
    cleanup_ok = (
        not isinstance(cleanup, Mapping)
        or cleanup.get("process_group_gone") is True
    )
    return (
        not result["timed_out"]
        and result["returncode"] in command["expected_returncodes"]
        and cleanup_ok
    )


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
        if ledger.get("candidate_residue_intent") is not None:
            raise AssuranceError(
                "candidate residue recovery must finish before deployment",
                code="CANDIDATE_RESIDUE_TRANSACTION_PENDING",
                status="NEEDS_USER",
            )
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
        _assert_no_candidate_residue_intent(ledger)
        transaction = ledger.get("deployment_transaction")
        if not isinstance(transaction, dict) or transaction["state"] != "deployed":
            raise AssuranceError("deployment is not active", code="DEPLOYMENT_NOT_ACTIVE")
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if report["candidate_head"] != candidate:
            raise AssuranceError("blackbox candidate is stale", code="EVIDENCE_CANDIDATE_MISMATCH")
        _validate_blackbox_report(ledger, report)
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
        if (
            ledger.get("candidate_residue_intent") is not None
            and not _candidate_residue_safety_restore_only(ledger)
        ):
            _assert_no_candidate_residue_intent(ledger)
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
        _assert_no_candidate_residue_intent(target)
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
        _assert_no_candidate_residue_intent(ledger)
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
        _assert_no_candidate_residue_intent(ledger)
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


def machine_failure_state(ledger: Mapping[str, Any]) -> str:
    failure = ledger.get("machine_failure")
    if not isinstance(failure, Mapping):
        return "missing"
    stage = str(failure.get("stage"))
    kind = "preflight" if stage == "preflight" else "machine"
    if failure.get("dependency_digest") != evidence_dependency(ledger, kind):
        return "stale"
    execution = ledger["facets"]["execution"]
    source = execution.get("tester_source")
    source_head = source.get("head") if isinstance(source, Mapping) else None
    if failure.get("candidate_head") != execution.get("candidate_head"):
        return "stale"
    if failure.get("tester_source_head") != source_head:
        return "stale"
    return "current"


def current_machine_failure(ledger: Mapping[str, Any]) -> dict[str, Any] | None:
    if machine_failure_state(ledger) != "current":
        return None
    failure = ledger.get("machine_failure")
    return copy.deepcopy(failure) if isinstance(failure, dict) else None


def _machine_failure_signature(stage: str, results: Sequence[Mapping[str, Any]]) -> str:
    stable = [
        {
            "id": item.get("id"),
            "argv": item.get("argv"),
            "returncode": item.get("returncode"),
            "timed_out": item.get("timed_out"),
            "executable_identity": item.get("executable_identity"),
            "stdout": item.get("stdout"),
            "stderr": item.get("stderr"),
        }
        for item in results
    ]
    return digest({"stage": stage, "commands": stable})


def _record_machine_failure(
    ledger: dict[str, Any],
    *,
    stage: str,
    results: list[dict[str, Any]],
    action_id: str | None,
) -> None:
    execution = ledger["facets"]["execution"]
    source = execution.get("tester_source")
    recovery = (
        "tester_diagnosis"
        if isinstance(source, Mapping) and isinstance(execution.get("agents", {}).get("tester"), Mapping)
        else "needs_user"
    )
    kind = "preflight" if stage == "preflight" else "machine"
    base = {
        "action_id": action_id if isinstance(action_id, str) and re.fullmatch(r"[0-9a-f]{64}", action_id) else None,
        "stage": stage,
        "candidate_head": execution.get("candidate_head"),
        "tester_source_head": source.get("head") if isinstance(source, Mapping) else None,
        "dependency_digest": evidence_dependency(ledger, kind),
        "commands": copy.deepcopy(results),
        "recovery": recovery,
        "failure_signature": _machine_failure_signature(stage, results),
    }
    record = {**base, "failure_digest": digest(base), "recorded_at": now()}
    ledger["machine_failure"] = record
    append_event(
        ledger,
        "machine_failure_recorded",
        {
            "stage": stage,
            "action_id": record["action_id"],
            "failure_signature": record["failure_signature"],
            "recovery": recovery,
        },
    )


def _run_machine_commands(
    repo: Path,
    run_id: str,
    candidate: str,
    commands: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
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
                        "source": "runtime",
                    }
                )
                break
            argv = [executable, *command["argv"][1:]]
            result = run_owned_command(
                argv,
                cwd=str(verify_worktree),
                timeout=command["timeout_seconds"],
                executable_identity=executable_identity,
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
                    "returncode": result["returncode"],
                    "stdout": result["stdout"][-8000:],
                    "stderr": result["stderr"][-8000:],
                    "timed_out": result["timed_out"],
                    "process_identity": result["process_identity"],
                    "cleanup": result["cleanup"],
                    "source": "runtime",
                }
            )
            if (
                result["returncode"] not in command["expected_returncodes"]
                or result["timed_out"]
                or not result["cleanup"]["process_group_gone"]
            ):
                break
    finally:
        git(repo, "worktree", "remove", "--force", str(verify_worktree), check=False)
        shutil.rmtree(verify_worktree, ignore_errors=True)
    return results


def _machine_results_pass(
    commands: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]]
) -> bool:
    declared = {item["id"]: item for item in commands}
    return len(results) == len(commands) and all(
        not item["timed_out"]
        and item["returncode"] in declared[item["id"]]["expected_returncodes"]
        and (
            not isinstance(item.get("cleanup"), Mapping)
            or item["cleanup"].get("process_group_gone") is True
        )
        for item in results
    )


def verify_preflight(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    started_at = time.monotonic()
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
        candidate = ledger["facets"]["execution"].get("candidate_head")
        if not candidate or not commit_exists(repo, candidate):
            raise AssuranceError("candidate head is unavailable", code="CANDIDATE_HEAD_INVALID")
        commands = [
            item
            for item in ledger["facets"]["assurance"]["machine_commands"]
            if item.get("run_before_full_suite")
        ]
        if not commands:
            return status(repo, run_id)
        results = _run_machine_commands(repo, run_id, candidate, commands)
        passed = _machine_results_pass(commands, results)
        record = {
            "kind": "preflight",
            "status": "pass" if passed else "fail",
            "dependency_digest": "",
            "candidate_head": candidate,
            "producer": {
                "role": "runtime",
                "agent_id": "assurance-core-v4",
                "thread_id": "deterministic-preflight",
            },
            "details": {"commands": results},
            "recorded_at": now(),
        }
        ledger["evidence"]["preflight"] = record
        record["dependency_digest"] = evidence_dependency(ledger, "preflight")
        if passed:
            ledger["machine_failure"] = None
        else:
            _record_machine_failure(
                ledger,
                stage="preflight",
                results=results,
                action_id=action_id,
            )
        append_event(
            ledger,
            "preflight_verified",
            {
                "status": record["status"],
                "commands": len(results),
                "failure_signature": (
                    None if passed else ledger["machine_failure"]["failure_signature"]
                ),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def verify_machine(
    repo_value: str | Path,
    run_value: str,
    *,
    action_id: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    started_at = time.monotonic()
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
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
        results = _run_machine_commands(repo, run_id, candidate, commands) if commands else []
        passed = _machine_results_pass(commands, results)
        record = {
            "kind": "machine",
            "status": "pass" if passed else "fail",
            "dependency_digest": "",
            "candidate_head": candidate,
            "producer": {
                "role": "runtime",
                "agent_id": "assurance-core-v4",
                "thread_id": "deterministic-machine",
            },
            "details": {"commands": results},
            "recorded_at": now(),
        }
        ledger["evidence"]["machine"] = record
        record["dependency_digest"] = evidence_dependency(ledger, "machine", evidence=record)
        if passed:
            ledger["machine_failure"] = None
        else:
            _record_machine_failure(
                ledger,
                stage="machine",
                results=results,
                action_id=action_id,
            )
        append_event(
            ledger,
            "machine_verified",
            {
                "status": record["status"],
                "commands": len(results),
                "failure_signature": (
                    None if passed else ledger["machine_failure"]["failure_signature"]
                ),
                "duration_ms": int((time.monotonic() - started_at) * 1000),
                "preflight_reused": 0,
            },
        )
        save_ledger(repo, ledger)
    return status(repo, run_id)


def _intent_owned_path(intent: Mapping[str, Any], value: str) -> Path:
    root = Path(str(intent["staging_root"])).resolve()
    path = Path(value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise AssuranceError(
            "recomposition staging path escaped its persisted root",
            code="RECOMPOSITION_STAGING_PATH_INVALID",
            details={"root": str(root), "path": str(path)},
        ) from exc
    return path


def _cleanup_recomposition_staging(repo: Path, intent: Mapping[str, Any]) -> None:
    root = Path(str(intent["staging_root"])).resolve()
    for key in ("tester_worktree", "builder_worktree"):
        value = intent.get(key)
        if not isinstance(value, str):
            continue
        path = _intent_owned_path(intent, value)
        if path.exists():
            git(repo, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
    for key in ("tester_branch", "builder_branch"):
        branch = intent.get(key)
        if isinstance(branch, str):
            git(repo, "branch", "-D", branch, check=False)
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)


def _ensure_recomposition_worktree(
    repo: Path,
    *,
    branch: str,
    worktree: Path,
    base_head: str,
) -> None:
    if worktree.exists():
        live_branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        if live_branch.returncode != 0 or live_branch.stdout.strip() != branch:
            raise AssuranceError(
                "recomposition worktree identity changed",
                code="RECOMPOSITION_WORKTREE_IDENTITY_MISMATCH",
                details={"worktree": str(worktree), "branch": branch},
            )
        return
    worktree.parent.mkdir(parents=True, exist_ok=True)
    ref_exists = git(repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
    args = ["worktree", "add"]
    if ref_exists:
        args.extend([str(worktree), branch])
    else:
        args.extend(["-b", branch, str(worktree), base_head])
    created = git(repo, *args, check=False)
    if created.returncode != 0:
        raise AssuranceError(
            created.stderr.strip() or "recomposition worktree creation failed",
            code="RECOMPOSITION_WORKTREE_CREATE_FAILED",
            details={"branch": branch, "worktree": str(worktree)},
        )


def _apply_recomposition_delta(
    repo: Path,
    *,
    worktree: Path,
    staging_base: str,
    source_base: str,
    source_head: str,
    paths: Sequence[str],
    patch_path: Path,
    message: str,
) -> tuple[str | None, list[str]]:
    unique_paths = sorted(set(paths))
    live_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    residue = dirty_paths(worktree)
    if residue:
        git(worktree, "reset", "--hard", staging_base, check=False)
        if unique_paths:
            git(worktree, "clean", "-fd", "--", *unique_paths, check=False)
        residue = dirty_paths(worktree)
        if residue:
            raise AssuranceError(
                "recomposition staging worktree contains unexplained residue",
                code="RECOMPOSITION_STAGING_RESIDUE",
                status="NEEDS_USER",
                details={"worktree": str(worktree), "paths": residue},
            )
        live_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if live_head != staging_base:
        parent = git(repo, "rev-parse", f"{live_head}^", check=False)
        observed = changed_files(repo, staging_base, live_head)
        if (
            parent.returncode == 0
            and parent.stdout.strip() == staging_base
            and set(observed).issubset(set(unique_paths))
        ):
            return live_head, []
        raise AssuranceError(
            "recomposition staging branch moved outside its persisted operation",
            code="RECOMPOSITION_STAGING_HEAD_DRIFT",
            status="NEEDS_USER",
            details={
                "worktree": str(worktree),
                "expected_base": staging_base,
                "observed_head": live_head,
                "paths": observed,
            },
        )
    if not unique_paths:
        return live_head, []
    diff = git(
        repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-renames",
        source_base,
        source_head,
        "--",
        *unique_paths,
        check=False,
    )
    if diff.returncode != 0:
        raise AssuranceError(
            diff.stderr.strip() or "recomposition source diff failed",
            code="RECOMPOSITION_SOURCE_DIFF_FAILED",
        )
    if not diff.stdout:
        return git(worktree, "rev-parse", "HEAD").stdout.strip(), []
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(diff.stdout, encoding="utf-8")
    applied = subprocess.run(
        ["git", "-C", str(worktree), "apply", "--3way", "--index", str(patch_path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if applied.returncode != 0:
        conflicts = sorted(
            line
            for line in git(
                worktree, "diff", "--name-only", "--diff-filter=U", check=False
            ).stdout.splitlines()
            if line
        )
        if conflicts:
            return None, conflicts
        raise AssuranceError(
            applied.stderr.strip() or "recomposition patch application failed",
            code="RECOMPOSITION_PATCH_FAILED",
            details={"paths": unique_paths},
        )
    if dirty_paths(worktree):
        committed = git(
            worktree,
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            message,
            check=False,
        )
        if committed.returncode != 0:
            raise AssuranceError(
                committed.stderr.strip() or "recomposition commit failed",
                code="RECOMPOSITION_COMMIT_FAILED",
            )
    return git(worktree, "rev-parse", "HEAD").stdout.strip(), []


def _begin_recomposition(
    repo: Path,
    ledger: dict[str, Any],
    *,
    kind: str,
    incoming_candidate: str,
    new_target: str,
) -> dict[str, Any]:
    if kind not in {"publication_refresh", "target_rematerialization"}:
        raise AssuranceError("unknown recomposition kind", code="RECOMPOSITION_KIND_INVALID")
    if isinstance(ledger.get("recomposition_intent"), dict):
        return ledger["recomposition_intent"]
    execution = ledger["facets"]["execution"]
    old_candidate = execution.get("candidate_head")
    if not isinstance(old_candidate, str):
        raise AssuranceError(
            "candidate identity is required before recomposition",
            code="RECOMPOSITION_CANDIDATE_REQUIRED",
        )
    publication = ledger.get("publication")
    generation = (
        int(publication.get("generation", 1 if publication.get("head") else 0))
        if isinstance(publication, dict)
        else 0
    )
    identity = digest(
        {
            "run_id": ledger["run_id"],
            "kind": kind,
            "old_target_head": ledger["target_start_head"],
            "new_target_head": new_target,
            "old_candidate_head": old_candidate,
            "incoming_candidate_head": incoming_candidate,
            "publication_generation": generation,
            "updated_at": ledger["updated_at"],
        }
    )
    intent: dict[str, Any] = {
        "intent_id": identity,
        "kind": kind,
        "state": "prepared",
        "old_target_head": ledger["target_start_head"],
        "new_target_head": new_target,
        "old_candidate_head": old_candidate,
        "incoming_candidate_head": incoming_candidate,
        "source_builder_base": ledger["target_start_head"],
        "source_builder_head": incoming_candidate,
        "publication_generation": generation,
        "staging_root": str(run_dir(repo, ledger["run_id"]) / f"recomposition-{identity[:16]}"),
        "target_restart_count": 0,
        "created_at": now(),
        "updated_at": now(),
    }
    source = execution.get("tester_source")
    tester_paths = sorted(
        path
        for path in changed_files(repo, ledger["target_start_head"], incoming_candidate)
        if _matches(path, ledger["facets"]["authority"]["tester_write"])
    )
    if tester_paths:
        intent["source_tester_base"] = ledger["target_start_head"]
        intent["source_tester_head"] = incoming_candidate
    elif isinstance(source, dict):
        intent["source_tester_base"] = (
            source.get("base_head") or ledger["target_start_head"]
        )
        intent["source_tester_head"] = source["head"]
    ledger["recomposition_intent"] = intent
    append_event(
        ledger,
        "recomposition_started",
        {
            "intent_id": identity,
            "kind": kind,
            "old_target_head": ledger["target_start_head"],
            "new_target_head": new_target,
            "incoming_candidate_head": incoming_candidate,
        },
    )
    save_ledger(repo, ledger)
    return intent


def _clear_recomposition_stage_fields(intent: dict[str, Any]) -> None:
    for key in (
        "builder_branch",
        "builder_worktree",
        "builder_head",
        "publication_head",
        "publication_tree",
        "publication_files",
        "tester_branch",
        "tester_worktree",
        "tester_head",
        "candidate_head",
        "conflict_paths",
        "conflict_owner",
    ):
        intent.pop(key, None)


def _restart_recomposition_for_target(
    repo: Path,
    ledger: dict[str, Any],
    intent: dict[str, Any],
    live_target: str,
) -> None:
    if intent["state"] in {"waiting_builder", "waiting_tester"}:
        worktree_key = "builder_worktree" if intent["state"] == "waiting_builder" else "tester_worktree"
        worktree = Path(str(intent[worktree_key]))
        if dirty_paths(worktree):
            raise AssuranceError(
                "target advanced while an ownership conflict is unresolved",
                code="TARGET_MOVED_DURING_RECOMPOSITION_CONFLICT",
                status="NEEDS_USER",
                details={"intent_id": intent["intent_id"], "paths": intent.get("conflict_paths", [])},
            )
    if int(intent["target_restart_count"]) >= 2:
        raise AssuranceError(
            "target advanced three times during the same recomposition",
            code="RECOMPOSITION_TARGET_MOVED_THREE_TIMES",
            status="NEEDS_USER",
            details={"intent_id": intent["intent_id"], "target_head": live_target},
        )
    builder_head = intent.get("builder_head")
    if isinstance(builder_head, str):
        intent["source_builder_base"] = intent["new_target_head"]
        intent["source_builder_head"] = builder_head
    tester_head = intent.get("tester_head")
    if isinstance(tester_head, str):
        tester_base = intent.get("publication_head") or intent["new_target_head"]
        intent["source_tester_base"] = tester_base
        intent["source_tester_head"] = tester_head
    _cleanup_recomposition_staging(repo, intent)
    intent["new_target_head"] = live_target
    intent["target_restart_count"] = int(intent["target_restart_count"]) + 1
    intent["state"] = "prepared"
    intent["updated_at"] = now()
    _clear_recomposition_stage_fields(intent)
    append_event(
        ledger,
        "recomposition_restarted",
        {
            "intent_id": intent["intent_id"],
            "new_target_head": live_target,
            "restart_count": intent["target_restart_count"],
        },
    )
    save_ledger(repo, ledger)


def _recomposition_builder_paths(repo: Path, ledger: Mapping[str, Any], intent: Mapping[str, Any]) -> list[str]:
    source_base = str(intent.get("source_builder_base") or intent["old_target_head"])
    source_head = str(intent.get("source_builder_head") or intent["incoming_candidate_head"])
    files = changed_files(repo, source_base, source_head)
    authority = ledger["facets"]["authority"]
    builder_files = sorted(
        path
        for path in files
        if _matches(path, authority["builder_write"])
        and not _matches(path, authority["tester_write"])
    )
    invalid = [
        path
        for path in files
        if not _matches(path, authority["builder_write"])
        and not _matches(path, authority["tester_write"])
    ]
    if invalid:
        raise AssuranceError(
            "recomposition source contains files outside Builder authority",
            code="RECOMPOSITION_BUILDER_AUTHORITY_VIOLATION",
            details={"paths": invalid},
        )
    return builder_files


def _validate_recomposition_tester_inputs(
    repo: Path,
    ledger: Mapping[str, Any],
    intent: Mapping[str, Any],
    paths: Sequence[str],
) -> None:
    execution = ledger["facets"]["execution"]
    source = execution.get("tester_source")
    source_manifest = (
        {
            str(item["path"]): str(item["blob"])
            for item in source.get("files", [])
        }
        if isinstance(source, Mapping)
        else {}
    )
    carryover = execution.get("carryover")
    carryover_manifest = (
        {
            str(item["path"]): str(item["blob"])
            for item in carryover.get("files", [])
        }
        if isinstance(carryover, Mapping)
        else {}
    )
    source_head = str(intent["source_tester_head"])
    unsupported: list[str] = []
    unbound: list[str] = []
    for path in paths:
        blob = _blob_at(repo, source_head, path)
        if blob is None:
            unsupported.append(path)
            continue
        expected = {
            value
            for value in (source_manifest.get(path), carryover_manifest.get(path))
            if isinstance(value, str)
        }
        if blob not in expected:
            unbound.append(path)
    if unsupported:
        raise AssuranceError(
            "recomposition Tester input contains a deletion or non-file entry",
            code="RECOMPOSITION_TESTER_ENTRY_UNSUPPORTED",
            details={"paths": unsupported},
        )
    if unbound:
        raise AssuranceError(
            "recomposition Tester input is not bound by the current source or carryover manifest",
            code="RECOMPOSITION_TESTER_INPUT_UNBOUND",
            details={"paths": unbound},
        )


def _recomposition_target_tester_manifest(
    repo: Path,
    ledger: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> dict[str, str]:
    if intent.get("kind") != "target_rematerialization":
        return {}
    authority = ledger["facets"]["authority"]
    paths = sorted(
        path
        for path in changed_files(
            repo,
            str(intent["old_target_head"]),
            str(intent["new_target_head"]),
        )
        if _matches(path, authority["tester_write"])
    )
    manifest: dict[str, str] = {}
    unsupported: list[str] = []
    for path in paths:
        blob = _regular_blob_at(repo, str(intent["new_target_head"]), path)
        if blob is None:
            unsupported.append(path)
        else:
            manifest[path] = blob
    if unsupported:
        raise AssuranceError(
            "target drift Tester input contains a deletion or non-file entry",
            code="RECOMPOSITION_TARGET_TESTER_ENTRY_UNSUPPORTED",
            status="NEEDS_USER",
            details={"paths": unsupported},
        )
    return manifest


def _recomposition_tester_manifest(
    repo: Path,
    ledger: Mapping[str, Any],
    intent: Mapping[str, Any],
    *,
    tester_head: str,
) -> list[dict[str, str]]:
    execution = ledger["facets"]["execution"]
    authority = ledger["facets"]["authority"]
    expected: dict[str, set[str]] = {}

    def bind(path: str, blob: str) -> None:
        if _matches(path, authority["tester_write"]):
            expected.setdefault(path, set()).add(blob)

    source = execution.get("tester_source")
    if isinstance(source, Mapping):
        source_head = str(source["head"])
        for item in source.get("files", []):
            path = str(item["path"])
            blob = str(item["blob"])
            if _regular_blob_at(repo, source_head, path) != blob:
                raise AssuranceError(
                    "recomposition Tester source manifest drifted",
                    code="RECOMPOSITION_TESTER_INPUT_UNBOUND",
                    details={"paths": [path]},
                )
            bind(path, blob)

    carryover = execution.get("carryover")
    if isinstance(carryover, Mapping):
        carryover_head = str(carryover["source_candidate_head"])
        for item in carryover.get("files", []):
            path = str(item["path"])
            blob = str(item["blob"])
            if not _matches(path, authority["tester_write"]):
                continue
            if _regular_blob_at(repo, carryover_head, path) != blob:
                raise AssuranceError(
                    "recomposition Tester carryover manifest drifted",
                    code="RECOMPOSITION_TESTER_INPUT_UNBOUND",
                    details={"paths": [path]},
                )
            bind(path, blob)

    for path, blob in _recomposition_target_tester_manifest(
        repo, ledger, intent
    ).items():
        bind(path, blob)

    unbound_delta: list[str] = []
    for path in _recomposition_tester_paths(repo, ledger, intent):
        blob = _regular_blob_at(repo, tester_head, path)
        if blob is None:
            raise AssuranceError(
                "recomposed Tester source lost a regular file",
                code="RECOMPOSITION_TESTER_ENTRY_UNSUPPORTED",
                details={"paths": [path]},
            )
        if path not in expected:
            unbound_delta.append(path)
    if unbound_delta:
        raise AssuranceError(
            "recomposed Tester source introduced an unbound path",
            code="RECOMPOSITION_TESTER_INPUT_UNBOUND",
            details={"paths": sorted(unbound_delta)},
        )

    manifest: list[dict[str, str]] = []
    drifted: list[str] = []
    unsupported: list[str] = []
    for path in sorted(expected):
        blob = _regular_blob_at(repo, tester_head, path)
        if blob is None:
            unsupported.append(path)
            continue
        expected_blobs = expected[path]
        if len(expected_blobs) == 1 and blob not in expected_blobs:
            drifted.append(path)
            continue
        manifest.append({"path": path, "blob": blob})
    if unsupported:
        raise AssuranceError(
            "recomposed Tester manifest contains a deletion or non-file entry",
            code="RECOMPOSITION_TESTER_ENTRY_UNSUPPORTED",
            details={"paths": unsupported},
        )
    if drifted:
        raise AssuranceError(
            "recomposed Tester manifest blob drifted",
            code="RECOMPOSITION_TESTER_BLOB_DRIFT",
            details={"paths": drifted},
        )
    return manifest


def _recomposition_tester_paths(repo: Path, ledger: Mapping[str, Any], intent: Mapping[str, Any]) -> list[str]:
    source_base = intent.get("source_tester_base")
    source_head = intent.get("source_tester_head")
    if not isinstance(source_base, str) or not isinstance(source_head, str):
        return []
    source_files = changed_files(repo, source_base, source_head)
    authority = ledger["facets"]["authority"]
    files = sorted(
        path for path in source_files if _matches(path, authority["tester_write"])
    )
    invalid = [
        path
        for path in source_files
        if not _matches(path, authority["builder_write"])
        and not _matches(path, authority["tester_write"])
    ]
    if invalid:
        raise AssuranceError(
            "recomposition source contains files outside Tester authority",
            code="RECOMPOSITION_TESTER_AUTHORITY_VIOLATION",
            details={"paths": invalid},
        )
    current_source = ledger["facets"]["execution"].get("tester_source")
    bound_source_heads = {intent.get("incoming_candidate_head")}
    if isinstance(current_source, Mapping):
        bound_source_heads.add(current_source.get("head"))
    if source_head in bound_source_heads:
        _validate_recomposition_tester_inputs(repo, ledger, intent, files)
    return files


def _commit_recomposition(repo: Path, ledger: dict[str, Any], intent: dict[str, Any]) -> None:
    event_kind = (
        "prerequisites_republished"
        if intent["kind"] == "publication_refresh"
        else "target_rematerialized"
    )
    already_committed = any(
        event.get("kind") == event_kind
        and isinstance(event.get("details"), dict)
        and event["details"].get("intent_id") == intent["intent_id"]
        for event in ledger.get("events", [])
    )
    if already_committed:
        _cleanup_recomposition_staging(repo, intent)
        ledger["recomposition_intent"] = None
        save_ledger(repo, ledger)
        return
    execution = ledger["facets"]["execution"]
    candidate_head = str(intent["candidate_head"])
    candidate_worktree = Path(ledger["candidate_worktree"])
    live_candidate = git(candidate_worktree, "rev-parse", "HEAD").stdout.strip()
    if live_candidate not in {intent["incoming_candidate_head"], candidate_head}:
        raise AssuranceError(
            "candidate moved outside the persisted recomposition",
            code="RECOMPOSITION_CANDIDATE_DRIFT",
            status="NEEDS_USER",
        )
    if dirty_paths(candidate_worktree):
        raise AssuranceError(
            "candidate became dirty during recomposition",
            code="RECOMPOSITION_CANDIDATE_DIRTY",
            status="NEEDS_USER",
        )
    if live_candidate != candidate_head:
        reset = git(candidate_worktree, "reset", "--hard", candidate_head, check=False)
        if reset.returncode != 0:
            raise AssuranceError("candidate CAS failed", code="RECOMPOSITION_CANDIDATE_CAS_FAILED")

    old_source = execution.get("tester_source")
    tester_head = intent.get("tester_head")
    tester_base = intent.get("publication_head") or intent["new_target_head"]
    target_tester_manifest = _recomposition_target_tester_manifest(
        repo, ledger, intent
    )
    if isinstance(old_source, dict) and isinstance(tester_head, str):
        tester_worktree = Path(old_source["worktree"])
        if dirty_paths(tester_worktree):
            raise AssuranceError(
                "Tester source became dirty during recomposition",
                code="RECOMPOSITION_TESTER_DIRTY",
                status="NEEDS_USER",
            )
        live_tester = git(tester_worktree, "rev-parse", "HEAD").stdout.strip()
        if live_tester not in {old_source["head"], tester_head}:
            raise AssuranceError(
                "Tester source moved outside the persisted recomposition",
                code="RECOMPOSITION_TESTER_DRIFT",
                status="NEEDS_USER",
            )
        if live_tester != tester_head:
            reset = git(tester_worktree, "reset", "--hard", tester_head, check=False)
            if reset.returncode != 0:
                raise AssuranceError("Tester source CAS failed", code="RECOMPOSITION_TESTER_CAS_FAILED")
        manifest = _recomposition_tester_manifest(
            repo,
            ledger,
            intent,
            tester_head=tester_head,
        )
        tester_files = [item["path"] for item in manifest]
        ledger.setdefault("retired_tester_sources", []).append(copy.deepcopy(old_source))
        execution["tester_source"] = {
            **old_source,
            "base_head": tester_base,
            "head": tester_head,
            "files": manifest,
            "replaces_files": [],
        }
        execution["tester_files"] = tester_files
        carryover = execution.get("carryover")
        if isinstance(carryover, dict):
            carryover["files"] = [
                item for item in carryover["files"] if item["path"] not in set(tester_files)
            ]
    elif target_tester_manifest:
        execution["tester_files"] = sorted(target_tester_manifest)

    final_tester_manifest = {
        str(item["path"]): str(item["blob"])
        for item in (
            execution.get("tester_source", {}).get("files", [])
            if isinstance(execution.get("tester_source"), Mapping)
            else []
        )
    }
    if not final_tester_manifest:
        final_tester_manifest = target_tester_manifest
    files = changed_files(repo, intent["new_target_head"], candidate_head)
    _assert_authorized_files(ledger["facets"], files)
    execution["builder_files"] = _validated_builder_files(
        repo,
        ledger,
        candidate=candidate_head,
        files=files,
        trusted_tester_manifest=final_tester_manifest,
    )
    overlap = sorted(
        set(execution["builder_files"]) & set(execution["tester_files"])
    )
    if overlap:
        raise AssuranceError(
            "recomposition assigned paths to both Builder and Tester",
            code="RECOMPOSITION_OWNERSHIP_OVERLAP",
            details={"paths": overlap},
        )
    execution["candidate_head"] = candidate_head
    execution["version"] += 1
    ledger["target_start_head"] = intent["new_target_head"]
    publication = ledger.get("publication")
    if isinstance(publication, dict) and publication.get("required"):
        publication.update(
            generation=int(intent["publication_generation"]) + 1,
            head=intent.get("publication_head"),
            tree=intent.get("publication_tree"),
            files=copy.deepcopy(intent.get("publication_files", [])),
            manifest_digest=digest(intent.get("publication_files", [])),
            candidate_head=intent.get("builder_head"),
        )
    public_classification = _public_prerequisite_classification(
        repo,
        execution,
        ledger["facets"]["authority"].get("public_prerequisites", []),
        candidate=candidate_head,
    )
    ledger["builder_checkpointed"] = all(
        item["status"] == "ready" for item in public_classification
    )
    ledger["digests"] = facet_digests(ledger["facets"])
    if ledger["builder_checkpointed"]:
        _close_problems(ledger, "builder", f"recomposition:{candidate_head}")
    if isinstance(old_source, dict):
        _close_problems(ledger, "tester", f"recomposition:{tester_head}")
    append_event(
        ledger,
        event_kind,
        {
            "intent_id": intent["intent_id"],
            "old_target_head": intent["old_target_head"],
            "new_target_head": intent["new_target_head"],
            "candidate_head": candidate_head,
            "publication_head": intent.get("publication_head"),
            "publication_generation": (
                publication.get("generation") if isinstance(publication, dict) else 0
            ),
            "target_restart_count": intent["target_restart_count"],
        },
    )
    save_ledger(repo, ledger)
    _cleanup_recomposition_staging(repo, intent)
    ledger["recomposition_intent"] = None
    save_ledger(repo, ledger)


def _advance_recomposition(repo: Path, ledger: dict[str, Any]) -> None:
    intent = ledger.get("recomposition_intent")
    if not isinstance(intent, dict):
        raise AssuranceError("recomposition intent is missing", code="RECOMPOSITION_INTENT_MISSING")
    while True:
        live_target = branch_head(repo, ledger["target_branch"])
        if live_target != intent["new_target_head"]:
            _restart_recomposition_for_target(repo, ledger, intent, live_target)
            continue
        state = intent["state"]
        root = Path(intent["staging_root"])
        if state == "prepared":
            branch = f"assurance-v4/{ledger['run_id']}/recompose-{intent['intent_id'][:12]}-builder"
            worktree = root / "builder"
            intent.update(
                state="builder_staging",
                builder_branch=branch,
                builder_worktree=str(worktree),
                updated_at=now(),
            )
            save_ledger(repo, ledger)
            continue
        if state == "builder_staging":
            worktree = Path(intent["builder_worktree"])
            _ensure_recomposition_worktree(
                repo,
                branch=intent["builder_branch"],
                worktree=worktree,
                base_head=intent["new_target_head"],
            )
            builder_paths = _recomposition_builder_paths(repo, ledger, intent)
            head, conflicts = _apply_recomposition_delta(
                repo,
                worktree=worktree,
                staging_base=intent["new_target_head"],
                source_base=str(intent["source_builder_base"]),
                source_head=str(intent["source_builder_head"]),
                paths=builder_paths,
                patch_path=root / "builder.patch",
                message="fix(assurance): [cr_id_skip] Recompose Builder Candidate",
            )
            if conflicts:
                intent.update(
                    state="waiting_builder",
                    conflict_owner="builder",
                    conflict_paths=conflicts,
                    updated_at=now(),
                )
                append_event(
                    ledger,
                    "recomposition_conflict",
                    {
                        "intent_id": intent["intent_id"],
                        "owner": "builder",
                        "paths": conflicts,
                    },
                )
                save_ledger(repo, ledger)
                return
            intent.update(
                state="publication_staging",
                builder_head=head,
                source_builder_base=intent["new_target_head"],
                source_builder_head=head,
                updated_at=now(),
            )
            save_ledger(repo, ledger)
            continue
        if state == "waiting_builder":
            worktree = Path(intent["builder_worktree"])
            conflicts = git(worktree, "diff", "--name-only", "--diff-filter=U", check=False)
            if conflicts.stdout.strip() or dirty_paths(worktree):
                return
            head = git(worktree, "rev-parse", "HEAD").stdout.strip()
            intent.update(
                state="publication_staging",
                builder_head=head,
                source_builder_base=intent["new_target_head"],
                source_builder_head=head,
                updated_at=now(),
            )
            intent.pop("conflict_owner", None)
            intent.pop("conflict_paths", None)
            append_event(
                ledger,
                "recomposition_conflict_resolved",
                {"intent_id": intent["intent_id"], "owner": "builder", "head": head},
            )
            save_ledger(repo, ledger)
            continue
        if state == "publication_staging":
            publication = ledger.get("publication")
            if isinstance(publication, dict) and publication.get("required"):
                head, tree, files = _materialize_publication(
                    repo,
                    ledger["run_id"],
                    base_head=intent["new_target_head"],
                    source_head=intent["builder_head"],
                    paths=list(publication["paths"]),
                )
                intent.update(
                    publication_head=head,
                    publication_tree=tree,
                    publication_files=files,
                )
            intent.update(state="tester_staging", updated_at=now())
            save_ledger(repo, ledger)
            continue
        if state == "tester_staging":
            tester_paths = _recomposition_tester_paths(repo, ledger, intent)
            if not tester_paths:
                source = ledger["facets"]["execution"].get("tester_source")
                if isinstance(source, dict):
                    tester_base = intent.get("publication_head") or intent["new_target_head"]
                    intent.update(
                        state="candidate_staging",
                        tester_head=tester_base,
                        source_tester_base=tester_base,
                        source_tester_head=tester_base,
                        updated_at=now(),
                    )
                else:
                    intent.update(state="candidate_staging", updated_at=now())
                save_ledger(repo, ledger)
                continue
            tester_base = intent.get("publication_head") or intent["new_target_head"]
            branch = f"assurance-v4/{ledger['run_id']}/recompose-{intent['intent_id'][:12]}-tester"
            worktree = root / "tester"
            intent.update(tester_branch=branch, tester_worktree=str(worktree))
            save_ledger(repo, ledger)
            _ensure_recomposition_worktree(
                repo,
                branch=branch,
                worktree=worktree,
                base_head=tester_base,
            )
            head, conflicts = _apply_recomposition_delta(
                repo,
                worktree=worktree,
                staging_base=tester_base,
                source_base=str(intent["source_tester_base"]),
                source_head=str(intent["source_tester_head"]),
                paths=tester_paths,
                patch_path=root / "tester.patch",
                message="test(assurance): [cr_id_skip] Recompose Tester Source",
            )
            if conflicts:
                intent.update(
                    state="waiting_tester",
                    conflict_owner="tester",
                    conflict_paths=conflicts,
                    updated_at=now(),
                )
                append_event(
                    ledger,
                    "recomposition_conflict",
                    {
                        "intent_id": intent["intent_id"],
                        "owner": "tester",
                        "paths": conflicts,
                    },
                )
                save_ledger(repo, ledger)
                return
            intent.update(
                state="candidate_staging",
                tester_head=head,
                source_tester_base=tester_base,
                source_tester_head=head,
                updated_at=now(),
            )
            save_ledger(repo, ledger)
            continue
        if state == "waiting_tester":
            worktree = Path(intent["tester_worktree"])
            conflicts = git(worktree, "diff", "--name-only", "--diff-filter=U", check=False)
            if conflicts.stdout.strip() or dirty_paths(worktree):
                return
            head = git(worktree, "rev-parse", "HEAD").stdout.strip()
            tester_base = intent.get("publication_head") or intent["new_target_head"]
            intent.update(
                state="candidate_staging",
                tester_head=head,
                source_tester_base=tester_base,
                source_tester_head=head,
                updated_at=now(),
            )
            intent.pop("conflict_owner", None)
            intent.pop("conflict_paths", None)
            append_event(
                ledger,
                "recomposition_conflict_resolved",
                {"intent_id": intent["intent_id"], "owner": "tester", "head": head},
            )
            save_ledger(repo, ledger)
            continue
        if state == "candidate_staging":
            worktree = Path(intent["builder_worktree"])
            git(worktree, "reset", "--hard", intent["builder_head"])
            tester_head = intent.get("tester_head")
            if isinstance(tester_head, str):
                tester_base = intent.get("publication_head") or intent["new_target_head"]
                tester_paths = _recomposition_tester_paths(repo, ledger, intent)
                candidate, conflicts = _apply_recomposition_delta(
                    repo,
                    worktree=worktree,
                    staging_base=intent["builder_head"],
                    source_base=tester_base,
                    source_head=tester_head,
                    paths=tester_paths,
                    patch_path=root / "integration.patch",
                    message=f"test(assurance): [cr_id_skip] Integrate Tester Source {tester_head[:12]}",
                )
                if conflicts:
                    raise AssuranceError(
                        "ownership-separated recomposition produced an integration conflict",
                        code="RECOMPOSITION_INTEGRATION_CONFLICT",
                        status="NEEDS_USER",
                        details={"paths": conflicts},
                    )
            else:
                candidate = intent["builder_head"]
            intent.update(state="committing", candidate_head=candidate, updated_at=now())
            save_ledger(repo, ledger)
            continue
        if state == "committing":
            _commit_recomposition(repo, ledger, intent)
            return
        raise AssuranceError("unknown recomposition state", code="RECOMPOSITION_STATE_INVALID")


def recompose_candidate(
    repo_value: str | Path,
    run_value: str,
    *,
    kind: str | None = None,
    action_id: str | None = None,
    owner_session_id: str | None = None,
) -> dict[str, Any]:
    repo = resolve_repo(repo_value)
    run_id = ensure_run_id(run_value)
    with locked(repo):
        ledger = read_ledger(repo, run_id)
        if ledger["phase"] != "active":
            raise AssuranceError("run is not active", code="ASSURANCE_RUN_NOT_ACTIVE")
        _assert_no_candidate_residue_intent(ledger)
        root_dispatch = _root_builder_dispatch_for_application(
            ledger,
            action_id=action_id,
            owner_session_id=owner_session_id,
        )
        intent = ledger.get("recomposition_intent")
        if not isinstance(intent, dict):
            live_target = branch_head(repo, ledger["target_branch"])
            if kind is None and live_target != ledger["target_start_head"]:
                kind = "target_rematerialization"
            if kind != "target_rematerialization":
                raise AssuranceError("recomposition intent is missing", code="RECOMPOSITION_INTENT_MISSING")
            worktree = Path(ledger["candidate_worktree"])
            if dirty_paths(worktree):
                raise AssuranceError(
                    "candidate worktree must be clean before target rematerialization",
                    code="CANDIDATE_WORKTREE_DIRTY",
                    status="NEEDS_USER",
                )
            source = ledger["facets"]["execution"].get("tester_source")
            if isinstance(source, dict) and dirty_paths(Path(source["worktree"])):
                raise AssuranceError(
                    "Tester source must be clean before target rematerialization",
                    code="TESTER_WORKTREE_DIRTY",
                    status="NEEDS_USER",
                )
            incoming = git(worktree, "rev-parse", "HEAD").stdout.strip()
            if live_target == ledger["target_start_head"]:
                if _mark_root_builder_result_applied(
                    ledger, root_dispatch, "recompose_candidate"
                ):
                    save_ledger(repo, ledger)
                return status(repo, run_id)
            _begin_recomposition(
                repo,
                ledger,
                kind="target_rematerialization",
                incoming_candidate=incoming,
                new_target=live_target,
            )
            ledger = read_ledger(repo, run_id)
            root_dispatch = _root_builder_dispatch_for_application(
                ledger,
                action_id=action_id,
                owner_session_id=owner_session_id,
            )
        _advance_recomposition(repo, ledger)
        if _mark_root_builder_result_applied(
            ledger, root_dispatch, "recompose_candidate"
        ):
            save_ledger(repo, ledger)
    return status(repo, run_id)


def rematerialize_target(repo_value: str | Path, run_value: str) -> dict[str, Any]:
    return recompose_candidate(
        repo_value,
        run_value,
        kind="target_rematerialization",
    )


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
        _assert_no_candidate_residue_intent(ledger)
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
        scan_state = doc_reference_scan_state(ledger)
        if scan_state not in {"pass", "not_required"}:
            raise AssuranceError(
                "documentation reference scan is not ready for finalize",
                code="DOC_REFERENCE_SCAN_FINALIZE_BLOCKED",
                status="NEEDS_USER",
                details={"doc_reference_scan_state": scan_state},
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
        _assert_no_candidate_residue_intent(ledger)
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
        _assert_no_candidate_residue_intent(ledger)
        if ledger["phase"] == "finalized":
            raise AssuranceError("finalized run cannot be abandoned", code="ASSURANCE_RUN_FINALIZED")
        if ledger["phase"] == "failed":
            raise AssuranceError(
                "failed run cannot be rewritten as abandoned",
                code="ASSURANCE_RUN_FAILED",
            )
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
        if ledger["phase"] not in {"finalized", "failed", "abandoned", "superseded"}:
            raise AssuranceError(
                "only terminal assurance runs can be cleaned",
                code="ASSURANCE_CLEANUP_NOT_TERMINAL",
                status="NEEDS_USER",
            )
        if ledger.get("candidate_residue_intent") is not None:
            raise AssuranceError(
                "candidate residue recovery must finish before terminal cleanup",
                code="ASSURANCE_CLEANUP_RESIDUE_INTENT_PENDING",
                status="NEEDS_USER",
            )
        if any(
            item.get("kind") == "terminal_worktrees_cleaned"
            for item in ledger.get("events", [])
            if isinstance(item, Mapping)
        ):
            return status(repo, run_id)
        candidate_expected_head = ledger["facets"]["execution"].get("candidate_head")
        failed_blockers: list[dict[str, Any]] = []
        if ledger["phase"] == "failed":
            failure = ledger.get("driver_failure")
            if not isinstance(failure, dict) or failure.get("state") != "terminal":
                raise AssuranceError(
                    "failed run has no terminal driver failure observation",
                    code="ASSURANCE_CLEANUP_DRIFT",
                    status="NEEDS_USER",
                )
            observation = failure["observation"]
            if (
                observation.get("candidate_worktree") != ledger["candidate_worktree"]
                or observation.get("candidate_branch") != ledger["candidate_branch"]
                or observation.get("candidate_dirty_paths")
                or not observation.get("candidate_worktree_exists")
                or not isinstance(observation.get("candidate_worktree_head"), str)
                or observation.get("candidate_worktree_head")
                != observation.get("candidate_branch_head")
            ):
                failed_blockers.append(
                    {
                        "role": "candidate",
                        "failure_observation": copy.deepcopy(observation),
                    }
                )
            else:
                candidate_expected_head = observation["candidate_worktree_head"]
            candidate_path = Path(ledger["candidate_worktree"])
            if candidate_path.is_dir() != bool(
                observation.get("candidate_worktree_exists")
            ):
                failed_blockers.append(
                    {
                        "role": "candidate",
                        "worktree": str(candidate_path),
                        "expected_exists": observation.get(
                            "candidate_worktree_exists"
                        ),
                        "actual_exists": candidate_path.is_dir(),
                    }
                )
        owned = [
            {
                "role": "candidate",
                "worktree": ledger["candidate_worktree"],
                "branch": ledger["candidate_branch"],
                "expected_head": candidate_expected_head,
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
        blockers: list[dict[str, Any]] = failed_blockers
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
