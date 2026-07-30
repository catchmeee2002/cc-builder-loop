from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tokenize
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence

from . import evidence as evidence_contract
from . import lifecycle as lifecycle_delivery
from . import workspace as workspace_contract

SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 3
LEGACY_PLAN_SCHEMA_VERSION = 2
BLACKBOX_REPORT_SCHEMA_VERSION = 2
LEGACY_BLACKBOX_REPORT_SCHEMA_VERSION = 1
PROBLEM_REPORT_SCHEMA_VERSION = 1
PRIOR_PROBLEMS_SCHEMA_VERSION = 1
VERIFICATION_PREPARATION_SCHEMA_VERSION = 1
CONTINUATION_FROM_SCHEMA_VERSION = 1
INTERFACE_PUBLICATION_CONTRACT_VERSION = 1
LEGACY_INTERFACE_PUBLICATION_CONTRACT_VERSION = 0
TESTER_CORRECTION_LIMIT = 3
PRIOR_PROBLEM_PLAN_REF_RE = re.compile(
    r"^(?:behavior:[a-z0-9]+(?:-[a-z0-9]+)*|checklist:[1-9][0-9]*)$"
)
CANONICAL_PLAN_DIGEST_KIND = "canonical-v2"
LEGACY_PLAN_DIGEST_KIND = "raw-v1"
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_FATAL = 2

ACTIVE_PHASES = {
    "active",
    "integration_conflict",
    "finalize_conflict",
    "continuity_failure",
    "iteration_limit",
    "no_progress",
    "architecture_review_required",
    "finalized_cleanup",
}
EVIDENCE_FIELDS = ("machine", "test_effectiveness", "blackbox", "review", "doc_review")
EVIDENCE_STATUS_FIELDS = {
    "machine": "verified_head",
    "test_effectiveness": "test_effectiveness_head",
    "blackbox": "e2e_verified_head",
    "review": "reviewed_head",
    "doc_review": "doc_reviewed_head",
}
PUBLIC_EVIDENCE_FIELDS = {
    "e2e_verified": "blackbox",
    "reviewed": "review",
    "doc_reviewed": "doc_review",
}
AGENT_RESULTS = {
    "tester": {"tests_ready", "pass", "fail", "target_change_required", "blocked"},
    "reviewer": {"pass", "findings", "blocked"},
}
PROBLEM_REPORT_RESULTS = {
    "tester": {"fail", "target_change_required", "blocked"},
    "reviewer": {"findings", "blocked"},
}
PROBLEM_OWNERS = {
    "builder",
    "tester",
    "plan",
    "current_project",
    "builder_loop",
    "external_platform",
}
FOLLOW_UP_PURPOSES = {
    "tester": {"author", "blackbox"},
    "reviewer": {"review"},
}
PROTECTED_RUNTIME_PATHS = {".claude/loop.yml"}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MANAGED_PLAN_HEADER_RE = re.compile(
    r"^\[保质期: run 完成, owner: builder-loop, 正向归宿: "
    r"(?:新 run ledger\.json|\.builder-loop/codex/runs/"
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/ledger\.json)\]$"
)


class RuntimeProblem(Exception):
    def __init__(
        self,
        message: str,
        *,
        result: str = "FATAL",
        code: str = "RUNTIME_ERROR",
        details: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.code = code
        self.details = details or {}
        if exit_code is not None:
            self.exit_code = exit_code
        elif result in {"FAIL", "NEEDS_USER", "CONFLICT"}:
            self.exit_code = EXIT_FAIL
        else:
            self.exit_code = EXIT_FATAL


class RuntimeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RuntimeProblem(message, code="CLI_USAGE_ERROR")


@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclasses.dataclass(frozen=True)
class PlanContract:
    source: str
    sha256: str
    source_sha256: str
    digest_kind: str
    schema_version: int
    level: str
    spec_head: str | None
    plan_revision: int | None
    parallel_ready: bool
    interfaces: tuple[Any, ...]
    interface_input_paths: tuple[str, ...]
    target_test_dirs: tuple[str, ...]
    support_paths: tuple[str, ...]
    public_prerequisites: tuple[str, ...]
    runner: str | None
    builder_write: tuple[str, ...]
    tester_write: tuple[str, ...]
    behavior_ids: tuple[str, ...]
    supersedes_run_id: str | None
    supersedes_plan_sha256: str | None
    prior_problem_snapshot_sha256: str | None
    prior_problem_items: tuple[dict[str, Any], ...]
    verification_preparation: dict[str, Any] | None
    continuation_from: dict[str, Any] | None
    has_e2e_cases: bool
    e2e_case_ids: tuple[str, ...]
    e2e_cases_sha256: str | None
    e2e_cases: tuple[dict[str, Any], ...]
    test_effectiveness_requirements: tuple[dict[str, str], ...]
    workspace_intake: tuple[dict[str, str], ...]
    evidence_scopes: dict[str, dict[str, tuple[str, ...]]]
    tags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["raw_sha256"] = self.source_sha256
        # The frozen Markdown remains the only complete test target. Validation
        # output exposes only the normalized ids/digest, not a second case copy.
        value.pop("e2e_cases", None)
        return value


@dataclasses.dataclass(frozen=True)
class PlanPreflight:
    spec_head: str
    target_branch: str
    target_start_head: str
    target_checkout: dict[str, Any]
    commands: tuple[dict[str, Any], ...]
    loop_config_sha256: str
    effective_verification_source: str
    max_iterations: int
    runner_paths: tuple[str, ...]
    workspace_intake: tuple[dict[str, Any], ...]
    prior_problem_snapshot: dict[str, Any] | None
    verification_preparation: dict[str, Any] | None
    continuation_from: dict[str, Any] | None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def emit(payload: dict[str, Any], exit_code: int = EXIT_PASS) -> int:
    if not str(payload.get("message", "")).strip():
        status = str(payload.get("status", "UNKNOWN"))
        payload["message"] = status.lower().replace("_", " ")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_json_sha256(value: Any) -> str:
    return sha256_text(canonical_json(value))


def empty_problem_inventory() -> dict[str, Any]:
    return {
        "schema_version": PROBLEM_REPORT_SCHEMA_VERSION,
        "items": [],
        "sources": [],
        "inherited_from": None,
        "snapshot": None,
    }


def decode_plan_bytes(value: bytes, source: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeProblem(
            f"plan is not valid UTF-8: {source}",
            code="PLAN_READ_ERROR",
        ) from exc


def canonicalize_plan_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines(keepends=True)
    if lines and MANAGED_PLAN_HEADER_RE.fullmatch(lines[0].rstrip("\n")):
        normalized = "".join(lines[1:])
        if normalized.startswith("\n"):
            normalized = normalized[1:]
    return normalized.rstrip("\n") + "\n"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tail_text(value: str, limit: int = 8000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def run_process(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int | float | None = None,
    check: bool = False,
) -> CommandResult:
    try:
        completed = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd else None,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeProblem(
            f"command timed out after {timeout}s",
            result="FAIL",
            code="COMMAND_TIMEOUT",
            details={
                "command": list(args),
                "stdout": tail_text(exc.stdout or ""),
                "stderr": tail_text(exc.stderr or ""),
            },
        ) from exc
    except OSError as exc:
        raise RuntimeProblem(
            f"cannot execute command: {exc}",
            code="COMMAND_EXEC_ERROR",
            details={"command": list(args)},
        ) from exc
    result = CommandResult(completed.returncode, completed.stdout, completed.stderr)
    if check and result.returncode != 0:
        raise RuntimeProblem(
            f"command failed with exit code {result.returncode}",
            code="COMMAND_ERROR",
            details={
                "command": list(args),
                "returncode": result.returncode,
                "stdout": tail_text(result.stdout),
                "stderr": tail_text(result.stderr),
            },
        )
    return result


def git(repo: Path, *args: str, check: bool = True, input_text: str | None = None) -> CommandResult:
    return run_process(["git", "-C", str(repo), *args], input_text=input_text, check=check)


def resolve_repo(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    result = git(candidate, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise RuntimeProblem(
            f"not a git worktree: {candidate}",
            code="NOT_GIT_REPOSITORY",
            details={"stderr": tail_text(result.stderr)},
        )
    return Path(result.stdout.strip()).resolve()


def full_head(repo: Path, ref: str = "HEAD") -> str:
    result = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if result.returncode != 0:
        raise RuntimeProblem(
            f"cannot resolve git ref: {ref}",
            code="INVALID_GIT_REF",
            details={"ref": ref, "stderr": tail_text(result.stderr)},
        )
    return result.stdout.strip()


def branch_head(repo: Path, branch: str) -> str:
    return full_head(repo, f"refs/heads/{branch}")


def unavailable_runtime_identity() -> dict[str, Any]:
    return {
        "adapter": "unknown",
        "adapter_commit": None,
        "adapter_dirty": None,
        "capture_status": "legacy-unavailable",
    }


def capture_runtime_identity() -> dict[str, Any]:
    source_root = Path(__file__).resolve().parents[2]
    head = git(source_root, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    if head.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip()):
        return {
            "adapter": "codex",
            "adapter_commit": None,
            "adapter_dirty": None,
            "capture_status": "unavailable",
        }
    status = git(
        source_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        check=False,
    )
    return {
        "adapter": "codex",
        "adapter_commit": head.stdout.strip(),
        "adapter_dirty": bool(status.stdout) if status.returncode == 0 else None,
        "capture_status": "captured" if status.returncode == 0 else "partial",
    }


def current_branch(repo: Path) -> str:
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeProblem(
            "repository preflight requires a named target branch",
            code="DETACHED_HEAD",
        )
    return result.stdout.strip()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    return result.returncode == 0


def ensure_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise RuntimeProblem(
            "run id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
            code="INVALID_RUN_ID",
        )
    return run_id


def state_root(repo: Path) -> Path:
    return repo / ".builder-loop" / "codex"


def common_git_dir(repo: Path) -> Path:
    value = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", check=True)
    return Path(value.stdout.strip()).resolve()


def repository_worktrees(repo: Path) -> list[Path]:
    result = git(repo, "worktree", "list", "--porcelain", "-z", check=True)
    paths = {
        Path(field[len("worktree ") :]).resolve()
        for field in result.stdout.split("\0")
        if field.startswith("worktree ")
    }
    paths.add(repo.resolve())
    return sorted(path for path in paths if path.exists())


@contextlib.contextmanager
def locked_repository_state(repo: Path) -> Iterator[None]:
    directory = common_git_dir(repo) / "codex-builder-loop"
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "runtime.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield


def run_dir(repo: Path, run_id: str) -> Path:
    return state_root(repo) / "runs" / ensure_run_id(run_id)


def ledger_path(repo: Path, run_id: str) -> Path:
    return run_dir(repo, run_id) / "ledger.json"


def resolve_run_selector(repo_arg: str | Path, selector: str | Path) -> tuple[Path, str, dict[str, Any]]:
    raw = Path(selector).expanduser()
    candidates: list[Path] = []
    if raw.is_absolute() or "/" in str(selector) or raw.exists():
        resolved = raw.resolve()
        candidates.append(resolved if resolved.name == "ledger.json" else resolved / "ledger.json")
    for candidate in candidates:
        if candidate.is_file():
            ledger = read_json(candidate)
            repo = resolve_repo(str(ledger["repo_root"]))
            expected = ledger_path(repo, str(ledger["run_id"])).resolve()
            if candidate.resolve() != expected:
                raise RuntimeProblem(
                    "run ledger is outside its repository state root",
                    code="RUN_PATH_MISMATCH",
                    details={"candidate": str(candidate), "expected": str(expected)},
                )
            return repo, str(ledger["run_id"]), ledger
        if raw.is_absolute() or "/" in str(selector):
            raise RuntimeProblem(
                f"run not found: {raw}",
                code="RUN_NOT_FOUND",
            )
    repo = resolve_repo(repo_arg)
    run_id = ensure_run_id(str(selector))
    matches = [
        (candidate_repo, ledger_path(candidate_repo, run_id))
        for candidate_repo in repository_worktrees(repo)
        if ledger_path(candidate_repo, run_id).is_file()
    ]
    if not matches:
        raise RuntimeProblem(f"ledger not found for run: {run_id}", code="RUN_NOT_FOUND")
    if len(matches) > 1:
        raise RuntimeProblem(
            "run id is ambiguous across repository worktrees",
            code="FATAL_AMBIGUOUS",
            details={"run_id": run_id, "ledgers": [str(path) for _repo, path in matches]},
        )
    owner_repo, path = matches[0]
    ledger = read_json(path)
    recorded_repo = resolve_repo(str(ledger["repo_root"]))
    if recorded_repo != owner_repo:
        raise RuntimeProblem(
            "run ledger repo_root does not match its state worktree",
            code="RUN_PATH_MISMATCH",
            details={"ledger": str(path), "repo_root": str(recorded_repo)},
        )
    return owner_repo, run_id, ledger


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError as exc:
        raise RuntimeProblem(
            f"ledger not found: {path}",
            code="RUN_NOT_FOUND",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeProblem(
            f"cannot read ledger: {exc}",
            code="LEDGER_INVALID",
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") not in {
        LEGACY_SCHEMA_VERSION,
        SCHEMA_VERSION,
    }:
        raise RuntimeProblem(
            "unsupported or invalid ledger schema",
            code="LEDGER_SCHEMA_ERROR",
        )
    if value.get("schema_version") == LEGACY_SCHEMA_VERSION:
        value = migrate_ledger_v1(value)
    value.setdefault("runtime_identity", unavailable_runtime_identity())
    plan = value.get("plan")
    if isinstance(plan, dict):
        plan.setdefault("contract_schema_version", LEGACY_PLAN_SCHEMA_VERSION)
        plan.setdefault("test_effectiveness_requirements", [])
        plan.setdefault("e2e_case_ids", [])
        plan.setdefault("e2e_cases_sha256", None)
        plan.setdefault("digest_kind", LEGACY_PLAN_DIGEST_KIND)
        plan.setdefault("source_sha256", plan.get("sha256"))
        plan.setdefault("frozen_sha256", plan.get("sha256"))
        plan.setdefault(
            "blackbox_report_schema_version",
            LEGACY_BLACKBOX_REPORT_SCHEMA_VERSION,
        )
        plan.setdefault(
            "interface_publication_contract_version",
            LEGACY_INTERFACE_PUBLICATION_CONTRACT_VERSION,
        )
        plan.setdefault("interface_input_paths", [])
        plan.setdefault("verification_preparation", None)
        plan.setdefault("continuation_from", None)
        plan.setdefault("revision", plan.get("plan_revision"))
    evidence = value.get("evidence")
    if isinstance(evidence, dict):
        evidence.setdefault("test_effectiveness", None)
    inventory = value.setdefault("problem_inventory", empty_problem_inventory())
    if isinstance(inventory, dict):
        inventory.setdefault("schema_version", PROBLEM_REPORT_SCHEMA_VERSION)
        inventory.setdefault("items", [])
        inventory.setdefault("sources", [])
        inventory.setdefault("inherited_from", None)
        inventory.setdefault("snapshot", None)
    return value


def migrate_ledger_v1(legacy: dict[str, Any]) -> dict[str, Any]:
    ledger = dict(legacy)
    evidence: dict[str, Any] = {}
    for key, legacy_field in EVIDENCE_STATUS_FIELDS.items():
        head = ledger.pop(legacy_field, None)
        evidence[key] = (
            evidence_contract.make_record(
                kind=key,
                observed_head=str(head),
                accepted_head=str(head),
                input_sha256=sha256_text(f"legacy-v1:{key}:{head}"),
                scope=["**"],
                provenance={"migration": "ledger-v1", "legacy_field": legacy_field},
            )
            if isinstance(head, str) and head
            else None
        )
    raw_attempts = ledger.pop("verification_attempts", 0)
    attempt_count = raw_attempts if type(raw_attempts) is int and raw_attempts >= 0 else 0
    ledger["verification"] = {
        "attempts": [
            {"attempt": index, "legacy": True}
            for index in range(1, attempt_count + 1)
        ],
        "resumes": [],
    }
    ledger["evidence"] = evidence
    ledger.setdefault("runtime_identity", unavailable_runtime_identity())
    ledger.setdefault(
        "workspace_intake",
        {
            "required": False,
            "paths": [],
            "entries": [],
            "snapshot_head": None,
            "snapshot_tree": None,
        },
    )
    ledger.setdefault("problem_inventory", empty_problem_inventory())
    plan = ledger.setdefault("plan", {})
    if isinstance(plan, dict):
        plan.setdefault(
            "evidence_scopes",
            {
                "machine": {"affects": ["**"], "exempt": []},
                "blackbox": {"affects": ["**"], "exempt": []},
            },
        )
        plan.setdefault("workspace_intake", [])
    ledger["schema_version"] = SCHEMA_VERSION
    events = ledger.setdefault("events", [])
    events.append(
        {
            "sequence": len(events) + 1,
            "type": "ledger_migrated",
            "at": utc_now(),
            "facts": {
                "from_schema_version": LEGACY_SCHEMA_VERSION,
                "to_schema_version": SCHEMA_VERSION,
            },
        }
    )
    ledger["updated_at"] = utc_now()
    return ledger


def evidence_record(ledger: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = ledger.get("evidence", {}).get(key)
    return value if isinstance(value, dict) else None


def active_test_effectiveness_record_is_current(
    ledger: Mapping[str, Any], record: Mapping[str, Any]
) -> bool:
    if ledger.get("phase") != "active" or not requires_test_effectiveness(dict(ledger)):
        return True
    requirements = ledger.get("plan", {}).get("test_effectiveness_requirements")
    if not isinstance(requirements, list):
        return False
    required_behaviors = [
        str(item.get("behavior_id"))
        for item in requirements
        if isinstance(item, dict) and isinstance(item.get("behavior_id"), str)
    ]
    provenance = record.get("provenance")
    groups = provenance.get("groups") if isinstance(provenance, Mapping) else None
    if not isinstance(groups, list):
        return False
    seen: list[str] = []
    for group in groups:
        if not isinstance(group, Mapping):
            return False
        behavior_ids = group.get("behavior_ids")
        if not isinstance(behavior_ids, list) or len(behavior_ids) != 1:
            return False
        behavior_id = behavior_ids[0]
        if not isinstance(behavior_id, str) or not behavior_id:
            return False
        seen.append(behavior_id)
        if group.get("method") == "mutation":
            mutation = group.get("mutation")
            if not isinstance(mutation, Mapping):
                return False
            applied_diff = mutation.get("applied_diff")
            applied_diff_sha256 = mutation.get("applied_diff_sha256")
            if (
                not isinstance(applied_diff, str)
                or not applied_diff
                or not isinstance(applied_diff_sha256, str)
                or sha256_text(applied_diff) != applied_diff_sha256
            ):
                return False
    return len(seen) == len(set(seen)) and sorted(seen) == sorted(required_behaviors)


def recorded_evidence_details(record: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    details = record.get("details")
    if isinstance(details, dict):
        return details
    legacy = record.get("provenance", {})
    if isinstance(legacy, dict) and isinstance(legacy.get("details"), dict):
        return legacy["details"]
    return {}


def plan_digest_context(ledger: Mapping[str, Any]) -> dict[str, str]:
    digest_kind = str(
        ledger.get("plan", {}).get("digest_kind") or LEGACY_PLAN_DIGEST_KIND
    )
    if digest_kind == CANONICAL_PLAN_DIGEST_KIND:
        return {"plan_digest_kind": digest_kind}
    return {}


def blackbox_report_schema_version(ledger: Mapping[str, Any]) -> int:
    raw = ledger.get("plan", {}).get(
        "blackbox_report_schema_version",
        LEGACY_BLACKBOX_REPORT_SCHEMA_VERSION,
    )
    if type(raw) is not int or raw not in {
        LEGACY_BLACKBOX_REPORT_SCHEMA_VERSION,
        BLACKBOX_REPORT_SCHEMA_VERSION,
    }:
        raise RuntimeProblem(
            "ledger has an unsupported blackbox report schema version",
            code="BLACKBOX_REPORT_VERSION_INVALID",
            details={"blackbox_report_schema_version": raw},
        )
    return raw


def accepted_blackbox_commands(
    ledger: Mapping[str, Any], details: Mapping[str, Any]
) -> list[str]:
    version = blackbox_report_schema_version(ledger)
    if version == LEGACY_BLACKBOX_REPORT_SCHEMA_VERSION:
        command = details.get("command")
        return [str(command)] if isinstance(command, str) and command.strip() else []
    executions = details.get("executions")
    if not isinstance(executions, list):
        return []
    return [
        str(item["command"])
        for item in executions
        if isinstance(item, dict)
        and item.get("method") == "command"
        and isinstance(item.get("command"), str)
        and str(item["command"]).strip()
    ]


def evidence_head(ledger: dict[str, Any], key: str) -> str | None:
    record = evidence_record(ledger, key)
    if (
        key == "test_effectiveness"
        and record is not None
        and not active_test_effectiveness_record_is_current(ledger, record)
    ):
        return None
    return evidence_contract.record_head(record)


def clear_evidence(ledger: dict[str, Any], key: str) -> dict[str, Any] | None:
    previous = evidence_record(ledger, key)
    ledger.setdefault("evidence", {})[key] = None
    return previous


def verification_attempt_records(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    verification = ledger.setdefault("verification", {"attempts": [], "resumes": []})
    attempts = verification.setdefault("attempts", [])
    return attempts if isinstance(attempts, list) else []


def verification_attempt_count(ledger: dict[str, Any]) -> int:
    return len(verification_attempt_records(ledger))


def tester_correction_progress(ledger: Mapping[str, Any]) -> dict[str, Any]:
    events = [item for item in ledger.get("events", []) if isinstance(item, dict)]
    completed: list[dict[str, Any]] = []
    reset_events: list[tuple[int, dict[str, Any], str]] = []
    for item in events:
        sequence = item.get("sequence")
        facts = item.get("facts")
        if type(sequence) is not int or not isinstance(facts, dict):
            continue
        event_type = item.get("type")
        if event_type == "machine_verification_passed":
            reset_events.append((sequence, item, "machine_pass"))
        elif (
            event_type == "verification_resumed"
            and facts.get("progress_source") == "tester_correction"
        ):
            reset_events.append((sequence, item, "explicit_resume"))
        if (
            event_type == "agent_event"
            and facts.get("role") == "tester"
            and facts.get("event") == "idle"
            and facts.get("follow_up_purpose") == "author"
        ):
            completed.append(
                {
                    "sequence": sequence,
                    "turn_id": facts.get("turn_id"),
                    "dispatch_id": facts.get("follow_up_dispatch_id"),
                    "result": facts.get("result"),
                    "candidate_head": facts.get("candidate_head"),
                    "tester_head": facts.get("role_head"),
                    "at": item.get("at") or facts.get("at"),
                }
            )
    if reset_events:
        reset_sequence, reset_event, reset_kind = max(reset_events, key=lambda item: item[0])
        reset_facts = reset_event.get("facts", {})
        window_start = {
            "kind": reset_kind,
            "sequence": reset_sequence,
            "at": reset_event.get("at") or reset_facts.get("at"),
            "candidate_head": (
                reset_facts.get("verified_head")
                if reset_kind == "machine_pass"
                else reset_facts.get("candidate_head")
            ),
        }
    else:
        reset_sequence = 0
        window_start = {
            "kind": "run_start",
            "sequence": 0,
            "at": ledger.get("created_at"),
            "candidate_head": ledger.get("spec_head"),
        }
    current = [item for item in completed if int(item["sequence"]) > reset_sequence]
    return {
        "limit": TESTER_CORRECTION_LIMIT,
        "current_window_count": len(current),
        "lifetime_count": len(completed),
        "window_start": window_start,
        "completed_turns": completed,
        "next_author_followup_blocked": len(current) >= TESTER_CORRECTION_LIMIT,
    }


def latest_progress_stop_source(ledger: Mapping[str, Any]) -> str:
    for item in reversed(ledger.get("events", [])):
        if not isinstance(item, dict):
            continue
        event_type = item.get("type")
        if event_type == "tester_correction_architecture_review_required":
            return "tester_correction"
        if event_type in {
            "machine_verification_failed",
            "machine_verification_tree_changed",
        }:
            facts = item.get("facts")
            if isinstance(facts, dict) and facts.get("stop_code") in {
                "NO_PROGRESS",
                "ARCHITECTURE_REVIEW_REQUIRED",
            }:
                return "machine_verification"
    return "unknown"


@contextlib.contextmanager
def locked_run(repo: Path, run_id: str) -> Iterator[dict[str, Any]]:
    directory = run_dir(repo, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "runtime.lock"
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        ledger = read_json(directory / "ledger.json")
        yield ledger


def append_event(ledger: dict[str, Any], event_type: str, facts: dict[str, Any]) -> None:
    events = ledger.setdefault("events", [])
    events.append(
        {
            "sequence": len(events) + 1,
            "type": event_type,
            "at": utc_now(),
            "facts": facts,
        }
    )
    ledger["updated_at"] = utc_now()


def save_ledger(repo: Path, ledger: dict[str, Any]) -> None:
    write_json_atomic(ledger_path(repo, str(ledger["run_id"])), ledger)


def evidence_scope_patterns(ledger: dict[str, Any], key: str) -> list[str]:
    scopes = ledger.get("plan", {}).get("evidence_scopes", {})
    scope_name = "machine" if key == "machine" else "blackbox"
    configured = scopes.get(scope_name, {}) if isinstance(scopes, dict) else {}
    if not isinstance(configured, dict) or "affects" not in configured:
        return ["**"]
    affects = configured.get("affects")
    if not isinstance(affects, list):
        return ["**"]
    patterns = [str(item) for item in affects if isinstance(item, str) and item]
    if key == "machine":
        patterns.extend(str(item) for item in ledger.get("loop_config", {}).get("runner_paths", []))
    else:
        tester_patterns = {
            str(item)
            for item in ledger.get("plan", {}).get("tester_write", [])
            if isinstance(item, str) and item
        }
        support_patterns = {
            str(item)
            for item in ledger.get("plan", {}).get("support_paths", [])
            if isinstance(item, str) and item
        }
        patterns = [
            item
            for item in patterns
            if item not in tester_patterns and item not in support_patterns
        ]
    normalized = sorted(set(patterns))
    if key == "blackbox":
        return normalized
    return normalized or ["**"]


def merge_evidence_scope_patterns(
    base_patterns: Iterable[str], command_paths: Iterable[str]
) -> list[str]:
    commands = set(command_paths)
    if "**" in commands:
        return ["**"]
    return sorted(set(base_patterns) | commands)


def evidence_digest_context(
    ledger: dict[str, Any],
    key: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    publication = ledger.get("prerequisite_publication", {})
    integration = ledger.get("tester_integration", {})
    context: dict[str, Any] = {
        "kind": key,
        "loop_config_sha256": ledger.get("loop_config", {}).get("spec_sha256"),
        "tester_source_head": integration.get("source_head"),
        "tester_integration_completed": integration.get("completed"),
        "publication_manifest_sha256": publication.get("manifest_sha256"),
        **plan_digest_context(ledger),
    }
    if key == "blackbox":
        report = dict(details) if details is not None else recorded_evidence_details(
            evidence_record(ledger, "blackbox")
        )
        version = blackbox_report_schema_version(ledger)
        if version == BLACKBOX_REPORT_SCHEMA_VERSION:
            context["blackbox_report_schema_version"] = version
            context["blackbox_report_sha256"] = evidence_contract.canonical_digest(
                report
            )
        else:
            context["command"] = report.get("command")
    return context


def scoped_input_digest(
    repo: Path, ledger: dict[str, Any], key: str, head: str
) -> tuple[str, list[str]]:
    patterns = evidence_scope_patterns(ledger, key)
    if key == "blackbox":
        record = evidence_record(ledger, "blackbox")
        details = recorded_evidence_details(record)
        commands = accepted_blackbox_commands(ledger, details)
        if commands:
            try:
                command_paths: set[str] = set()
                for command in commands:
                    command_paths.update(
                        runner_repository_paths(
                            command,
                            resolve_blackbox_dependencies=True,
                        )
                    )
                patterns = merge_evidence_scope_patterns(patterns, command_paths)
            except RuntimeProblem:
                patterns = ["**"]
        elif blackbox_report_schema_version(ledger) == BLACKBOX_REPORT_SCHEMA_VERSION:
            patterns = ["**"]
    try:
        digest = evidence_contract.input_digest(
            repo,
            head,
            patterns=patterns,
            plan_sha256=str(ledger.get("plan", {}).get("sha256") or ""),
            context=evidence_digest_context(ledger, key),
        )
    except RuntimeError as exc:
        raise RuntimeProblem(
            "cannot compute evidence input digest",
            code="EVIDENCE_DIGEST_FAILED",
            details={"kind": key, "head": head, "error": str(exc)},
        ) from exc
    return digest, patterns


def invalidate_evidence(
    repo: Path, ledger: dict[str, Any], previous_head: str, current_head: str
) -> None:
    if previous_head == current_head:
        return
    cleared: dict[str, Any] = {}
    reused: dict[str, Any] = {}
    for key in EVIDENCE_FIELDS:
        old = evidence_record(ledger, key)
        if old is None:
            continue
        if key in {"machine", "blackbox"}:
            digest, scope = scoped_input_digest(repo, ledger, key, current_head)
            if old.get("input_digest") == digest:
                old["accepted_head"] = current_head
                provenance = old.setdefault("provenance", {})
                reuses = provenance.setdefault("reuses", [])
                reuses.append(
                    {
                        "from_head": previous_head,
                        "accepted_head": current_head,
                        "at": utc_now(),
                    }
                )
                reused[key] = {"input_digest": digest, "scope": scope}
                continue
        cleared[key] = old
        clear_evidence(ledger, key)
    append_event(
        ledger,
        "evidence_invalidated",
        {
            "previous_candidate_head": previous_head,
            "candidate_head": current_head,
            "cleared": cleared,
            "reused": reused,
        },
    )


def invalidate_role_evidence(
    ledger: dict[str, Any],
    role: str,
    *,
    purpose: str | None = None,
    turn_id: str | None = None,
    dispatch_id: str | None = None,
) -> None:
    if role == "tester":
        fields = (
            ("test_effectiveness", "blackbox")
            if purpose == "author"
            else ("blackbox",)
        )
    else:
        fields = ("review", "doc_review")
    cleared = {field: evidence_record(ledger, field) for field in fields if evidence_record(ledger, field) is not None}
    if not cleared:
        return
    for field in fields:
        clear_evidence(ledger, field)
    facts: dict[str, Any] = {"role": role, "cleared": cleared}
    if turn_id is not None:
        facts["turn_id"] = turn_id
    if dispatch_id is not None:
        facts["dispatch_id"] = dispatch_id
    append_event(ledger, "agent_evidence_invalidated", facts)


def reject_during_finalize_intent(ledger: dict[str, Any], operation: str) -> None:
    if ledger.get("phase") == "active" and isinstance(
        ledger.get("finalize_intent"), dict
    ):
        raise RuntimeProblem(
            "a persisted finalize intent freezes run mutations until finalize recovers",
            result="NEEDS_USER",
            code="FINALIZE_INTENT_ACTIVE",
            details={"operation": operation, "finalize_intent": ledger["finalize_intent"]},
            exit_code=EXIT_FAIL,
        )


def reviewer_prerequisite_snapshot(
    repo: Path,
    ledger: dict[str, Any],
    *,
    candidate_head: str,
    candidate_dirty: bool,
) -> dict[str, Any]:
    plan = ledger.get("plan", {})
    level = str(plan.get("level") or "")
    raw_documentation_paths = plan.get("builder_write", [])
    documentation_paths = (
        [str(path) for path in raw_documentation_paths]
        if isinstance(raw_documentation_paths, list)
        and all(isinstance(path, str) and path for path in raw_documentation_paths)
        else []
    )
    integration = ledger.get("tester_integration", {})
    author_turn_id = (
        integration.get("author_turn_id") if isinstance(integration, dict) else None
    )
    integration_completed = bool(
        isinstance(integration, dict)
        and integration.get("completed") is True
        and isinstance(author_turn_id, str)
        and author_turn_id
    )
    verified_head = evidence_head(ledger, "machine")
    test_effectiveness_head = evidence_head(ledger, "test_effectiveness")
    e2e_verified_head = evidence_head(ledger, "blackbox")
    test_effectiveness_required = requires_test_effectiveness(ledger)
    publication = ledger.get("prerequisite_publication", {})
    publication_required = bool(
        isinstance(publication, dict) and publication.get("required") is True
    )
    publication_head = publication.get("head") if isinstance(publication, dict) else None
    publication_tree = publication.get("tree") if isinstance(publication, dict) else None
    publication_manifest = (
        publication.get("manifest_sha256") if isinstance(publication, dict) else None
    )
    publication_paths = publication.get("paths", []) if isinstance(publication, dict) else []
    author_publication_manifest = (
        integration.get("author_prerequisite_manifest_sha256")
        if isinstance(integration, dict)
        else None
    )
    publication_bound = not publication_required
    if (
        publication_required
        and isinstance(publication_head, str)
        and isinstance(publication_tree, str)
        and isinstance(publication_manifest, str)
        and isinstance(publication_paths, list)
        and publication_paths
        and integration.get("base_head") == publication_head
        and author_publication_manifest == publication_manifest
    ):
        unchanged = git(
            repo,
            "diff",
            "--quiet",
            publication_head,
            candidate_head,
            "--",
            *[str(path) for path in publication_paths],
            check=False,
        )
        publication_bound = unchanged.returncode == 0
    candidate_ready = not candidate_dirty and bool(documentation_paths)
    satisfied = candidate_ready and (
        level == "L1"
        or (
            integration_completed
            and publication_bound
            and verified_head == candidate_head
            and (
                not test_effectiveness_required
                or test_effectiveness_head == candidate_head
            )
            and e2e_verified_head == candidate_head
        )
    )
    return {
        "captured_at": utc_now(),
        "plan_level": level,
        "candidate_head": candidate_head,
        "candidate_dirty": candidate_dirty,
        "documentation_paths": documentation_paths,
        "tester_integration_completed": integration_completed,
        "tester_author_turn_id": author_turn_id,
        "prerequisite_required": publication_required,
        "prerequisite_head": publication_head,
        "prerequisite_tree": publication_tree,
        "prerequisite_manifest_sha256": publication_manifest,
        "tester_author_prerequisite_manifest_sha256": author_publication_manifest,
        "prerequisite_bound": publication_bound,
        "verified_head": verified_head,
        "test_effectiveness_required": test_effectiveness_required,
        "test_effectiveness_head": test_effectiveness_head,
        "e2e_verified_head": e2e_verified_head,
        "satisfied": satisfied,
    }


def reviewer_prerequisite_snapshot_matches(
    ledger: dict[str, Any], snapshot: Any, candidate_head: str
) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("satisfied") is not True:
        return False
    plan = ledger.get("plan", {})
    level = plan.get("level")
    documentation_paths = plan.get("builder_write", [])
    if (
        snapshot.get("plan_level") != level
        or snapshot.get("candidate_head") != candidate_head
        or snapshot.get("candidate_dirty") is not False
        or snapshot.get("documentation_paths") != documentation_paths
        or not isinstance(snapshot.get("captured_at"), str)
        or not snapshot.get("captured_at")
    ):
        return False
    if level == "L1":
        return bool(documentation_paths)
    publication = ledger.get("prerequisite_publication", {})
    publication_required = bool(
        isinstance(publication, dict) and publication.get("required") is True
    )
    if (
        snapshot.get("prerequisite_required") is not publication_required
        or snapshot.get("prerequisite_head") != publication.get("head")
        or snapshot.get("prerequisite_tree") != publication.get("tree")
        or snapshot.get("prerequisite_manifest_sha256")
        != publication.get("manifest_sha256")
        or snapshot.get("tester_author_prerequisite_manifest_sha256")
        != ledger.get("tester_integration", {}).get(
            "author_prerequisite_manifest_sha256"
        )
        or snapshot.get("prerequisite_bound") is not True
    ):
        return False
    test_effectiveness_required = requires_test_effectiveness(ledger)
    snapshot_requirement = snapshot.get("test_effectiveness_required")
    test_effectiveness_matches = (
        snapshot_requirement is True
        and snapshot.get("test_effectiveness_head") == candidate_head
        if test_effectiveness_required
        else snapshot_requirement is None or snapshot_requirement is False
    )
    return bool(
        snapshot.get("tester_integration_completed") is True
        and isinstance(snapshot.get("tester_author_turn_id"), str)
        and snapshot.get("tester_author_turn_id")
        and snapshot.get("verified_head") == candidate_head
        and test_effectiveness_matches
        and snapshot.get("e2e_verified_head") == candidate_head
    )


def reviewer_prerequisites_bound(
    ledger: dict[str, Any], agent_fact: Any, candidate_head: str
) -> bool:
    if not isinstance(agent_fact, dict):
        return False
    binding = agent_fact.get("review_prerequisites")
    if not isinstance(binding, dict) or binding.get("bound") is not True:
        return False
    return reviewer_prerequisite_snapshot_matches(
        ledger, binding.get("start"), candidate_head
    ) and reviewer_prerequisite_snapshot_matches(
        ledger, binding.get("completion"), candidate_head
    )


def read_plan_source(
    plan_arg: str | None,
) -> tuple[str, str, Path | None, str]:
    if plan_arg and plan_arg != "-":
        path = Path(plan_arg).expanduser().resolve()
        try:
            raw = path.read_bytes()
            return decode_plan_bytes(raw, str(path)), str(path), path, sha256_bytes(raw)
        except OSError as exc:
            raise RuntimeProblem(
                f"cannot read plan: {exc}",
                code="PLAN_READ_ERROR",
            ) from exc
    if sys.stdin.isatty():
        raise RuntimeProblem(
            "plan-validate requires --plan PATH or Markdown on stdin",
            code="PLAN_REQUIRED",
        )
    binary_stream = getattr(sys.stdin, "buffer", None)
    if binary_stream is not None:
        raw = binary_stream.read()
        return decode_plan_bytes(raw, "stdin"), "stdin", None, sha256_bytes(raw)
    text = sys.stdin.read()
    return text, "stdin", None, sha256_text(text)


def mask_markdown_fences(text: str) -> str:
    output: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r"^[ \t]*([\`~]{3,})", line)
        marker = match.group(1) if match else ""
        if fence_char is None and marker:
            fence_char = marker[0]
            fence_length = len(marker)
            output.append("".join("\n" if char == "\n" else "\r" if char == "\r" else " " for char in line))
            continue
        if fence_char is not None:
            output.append("".join("\n" if char == "\n" else "\r" if char == "\r" else " " for char in line))
            if (
                marker
                and marker[0] == fence_char
                and len(marker) >= fence_length
                and match is not None
                and not line[match.end(1) :].strip()
            ):
                fence_char = None
                fence_length = 0
            continue
        output.append(line)
    return "".join(output)


def extract_tag(text: str, name: str, *, required: bool) -> str | None:
    open_re = re.compile(rf"<!--\s*{re.escape(name)}\s*-->", re.IGNORECASE)
    close_re = re.compile(rf"<!--\s*/{re.escape(name)}\s*-->", re.IGNORECASE)
    searchable = mask_markdown_fences(text)
    opens = list(open_re.finditer(searchable))
    closes = list(close_re.finditer(searchable))
    if not opens and not closes and not required:
        return None
    if len(opens) != 1 or len(closes) != 1 or opens[0].end() > closes[0].start():
        raise RuntimeProblem(
            f"plan must contain exactly one well-formed {name} tag pair",
            result="NEEDS_USER",
            code="PLAN_TAG_INVALID",
            details={"tag": name, "open_count": len(opens), "close_count": len(closes)},
        )
    return text[opens[0].end() : closes[0].start()].strip()


def yaml_load(value: str) -> Any:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(value)
    except ImportError:
        return minimal_yaml_load(value)
    except Exception as exc:
        raise RuntimeProblem(
            f"invalid YAML in plan spec: {exc}",
            result="NEEDS_USER",
            code="PLAN_YAML_INVALID",
        ) from exc


def yaml_scalar(value: str) -> Any:
    raw = value.strip()
    if not raw:
        return None
    if raw in {"{}", "[]"}:
        return {} if raw == "{}" else []
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    if raw.lower() in {"null", "none", "~"}:
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if raw[0:1] in {'"', "'"} and raw[-1:] == raw[0:1]:
        try:
            return json.loads(raw) if raw[0] == '"' else raw[1:-1].replace("''", "'")
        except json.JSONDecodeError:
            return raw[1:-1]
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            if raw.startswith("[") and raw.endswith("]"):
                return [yaml_scalar(item) for item in raw[1:-1].split(",") if item.strip()]
    return raw


def minimal_yaml_load(value: str) -> Any:
    prepared: list[tuple[int, str]] = []
    for number, raw_line in enumerate(value.splitlines(), 1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise RuntimeProblem(
                f"tabs are not supported in YAML indentation at line {number}",
                result="NEEDS_USER",
                code="PLAN_YAML_INVALID",
            )
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        prepared.append((len(raw_line) - len(raw_line.lstrip(" ")), stripped))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        is_list = prepared[index][1].startswith("- ") or prepared[index][1] == "-"
        container: Any = [] if is_list else {}
        while index < len(prepared):
            current_indent, text = prepared[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise RuntimeProblem(
                    "invalid YAML indentation",
                    result="NEEDS_USER",
                    code="PLAN_YAML_INVALID",
                )
            if is_list:
                if not text.startswith("-"):
                    break
                item_text = text[1:].strip()
                if not item_text:
                    if index + 1 >= len(prepared) or prepared[index + 1][0] <= indent:
                        container.append(None)
                        index += 1
                    else:
                        child, index = parse_block(index + 1, prepared[index + 1][0])
                        container.append(child)
                    continue
                mapping = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", item_text)
                if mapping:
                    item: dict[str, Any] = {mapping.group(1).strip(): yaml_scalar(mapping.group(2))}
                    index += 1
                    while index < len(prepared) and prepared[index][0] > indent:
                        child_indent, child_text = prepared[index]
                        child_match = re.match(
                            r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$",
                            child_text,
                        )
                        if not child_match:
                            break
                        key = child_match.group(1).strip()
                        tail = child_match.group(2)
                        if tail:
                            item[key] = yaml_scalar(tail)
                            index += 1
                        elif index + 1 < len(prepared) and prepared[index + 1][0] > child_indent:
                            child, index = parse_block(index + 1, prepared[index + 1][0])
                            item[key] = child
                        else:
                            item[key] = None
                            index += 1
                    container.append(item)
                    continue
                container.append(yaml_scalar(item_text))
                index += 1
                continue
            if text.startswith("-"):
                break
            mapping = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", text)
            if not mapping:
                raise RuntimeProblem(
                    "invalid YAML mapping entry",
                    result="NEEDS_USER",
                    code="PLAN_YAML_INVALID",
                )
            key = mapping.group(1).strip()
            tail = mapping.group(2)
            if tail:
                container[key] = yaml_scalar(tail)
                index += 1
            elif index + 1 < len(prepared) and prepared[index + 1][0] > indent:
                child, index = parse_block(index + 1, prepared[index + 1][0])
                container[key] = child
            else:
                container[key] = None
                index += 1
        return container, index

    if not prepared:
        return None
    parsed, final_index = parse_block(0, prepared[0][0])
    if final_index != len(prepared):
        raise RuntimeProblem(
            "could not parse complete YAML document",
            result="NEEDS_USER",
            code="PLAN_YAML_INVALID",
        )
    return parsed


def recursive_key_values(value: Any, keys: set[str]) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text in keys:
                found.append((key_text, child))
            found.extend(recursive_key_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_key_values(child, keys))
    return found


def fallback_yaml_list(raw: str, keys: set[str]) -> list[tuple[str, list[str]]]:
    lines = raw.splitlines()
    found: list[tuple[str, list[str]]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$", line)
        if not match or match.group(2) not in keys:
            continue
        indent = len(match.group(1).replace("\t", "        "))
        key = match.group(2)
        tail = match.group(3).strip()
        values: list[str] = []
        if tail:
            if tail.startswith("[") and tail.endswith("]"):
                values = [item.strip().strip("'\"") for item in tail[1:-1].split(",") if item.strip()]
            else:
                values = [tail.strip("'\"")]
        else:
            for child in lines[index + 1 :]:
                if not child.strip() or child.lstrip().startswith("#"):
                    continue
                child_indent = len(child) - len(child.lstrip(" \t"))
                if child_indent <= indent:
                    break
                item = re.match(r"^\s*-\s*(.*?)\s*$", child)
                if item:
                    values.append(item.group(1).strip().strip("'\""))
        found.append((key, values))
    return found


def normalize_allowed_path(value: str, *, directory_hint: bool) -> str:
    candidate = value.strip().replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    if not candidate or candidate.startswith("/") or "\x00" in candidate:
        raise RuntimeProblem(
            f"invalid allowed test path: {value!r}",
            result="NEEDS_USER",
            code="PLAN_TEST_PATH_INVALID",
        )
    parts = PurePosixPath(candidate).parts
    if ".." in parts or parts[0] in {".git", ".builder-loop"}:
        raise RuntimeProblem(
            f"allowed test path escapes or targets runtime metadata: {value!r}",
            result="NEEDS_USER",
            code="PLAN_TEST_PATH_INVALID",
        )
    if candidate.endswith("/"):
        candidate += "**"
    elif directory_hint and not any(mark in candidate for mark in "*?["):
        candidate = candidate.rstrip("/") + "/**"
    return candidate


def string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise RuntimeProblem(
            f"{field} must be a {'possibly empty' if allow_empty else 'non-empty'} string list",
            result="NEEDS_USER",
            code="PLAN_FIELD_INVALID",
            details={"errors": [field]},
        )
    output = [str(item).strip() for item in value]
    if any(not item for item in output):
        raise RuntimeProblem(
            f"{field} contains an empty value",
            result="NEEDS_USER",
            code="PLAN_FIELD_INVALID",
            details={"errors": [field]},
        )
    return output


def patterns_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    left_has_glob = any(mark in left for mark in "*?[")
    right_has_glob = any(mark in right for mark in "*?[")
    if not left_has_glob and not right_has_glob:
        return False
    if not left_has_glob:
        return path_allowed(left, [right])
    if not right_has_glob:
        return path_allowed(right, [left])

    # Proving two arbitrary shell-style globs disjoint is surprisingly subtle.
    # Treat any shared static-prefix region as overlapping; false positives are
    # safer than authorizing Builder and Tester to the same possible path.
    left_prefix = re.split(r"[?*\[]", left, maxsplit=1)[0]
    right_prefix = re.split(r"[?*\[]", right, maxsplit=1)[0]
    if not left_prefix or not right_prefix:
        return True
    return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)


INTERFACE_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./-])((?:[^\s/:\"']+/)+[^\s:\"']+|[^\s/:\"']+\.[^\s/:\"']+|"
    r"Makefile|Dockerfile)(?![\w.-])"
)


def interface_text_values(interfaces: Sequence[Any]) -> list[str]:
    values: list[str] = []
    for interface in interfaces:
        if isinstance(interface, str):
            values.append(interface)
            continue
        if not isinstance(interface, dict):
            continue
        for key in ("module", "import", "signature", "output"):
            value = interface.get(key)
            if isinstance(value, str):
                values.append(value)
        errors = interface.get("errors")
        if isinstance(errors, list):
            values.extend(str(item) for item in errors if isinstance(item, str))
    return values


def extract_interface_input_paths(
    interfaces: Sequence[Any], builder_write: Sequence[str]
) -> tuple[str, ...]:
    paths: set[str] = set()
    for text in interface_text_values(interfaces):
        stripped = text.strip()
        whole_path = bool(
            stripped
            and any(char.isspace() for char in stripped)
            and re.search(r"\.[^/\s]+$", stripped)
        )
        candidates = (
            []
            if whole_path
            else [
                (match.group(1), match.end())
                for match in INTERFACE_PATH_TOKEN_RE.finditer(text)
            ]
        )
        if whole_path:
            candidates.append((stripped, len(text.rstrip())))
        for pattern in builder_write:
            if any(mark in pattern for mark in "*?["):
                continue
            exact = re.compile(
                rf"(?<![\w./-])({re.escape(pattern)})(?![\w./-])"
            )
            candidates.extend(
                (match.group(1), match.end()) for match in exact.finditer(text)
            )
        for raw, end in candidates:
            if text[end : end + 1] == ":":
                # Existing plans use path:symbol as a public interface locator;
                # it does not declare that Tester author needs the file bytes.
                continue
            try:
                candidate = normalize_allowed_path(raw, directory_hint=False)
            except RuntimeProblem:
                continue
            if any(patterns_overlap(candidate, pattern) for pattern in builder_write):
                paths.add(candidate)
    return tuple(sorted(paths))


def validate_interface_publication_contract(contract: PlanContract) -> None:
    if contract.level == "L1":
        return
    paths = list(contract.interface_input_paths)
    inexact = sorted(path for path in paths if any(mark in path for mark in "*?["))
    if inexact:
        raise RuntimeProblem(
            "Tester interface file inputs must be exact repository paths",
            result="NEEDS_USER",
            code="PLAN_INTERFACE_INPUT_NOT_EXACT",
            details={
                "interface_publication_contract_version": (
                    INTERFACE_PUBLICATION_CONTRACT_VERSION
                ),
                "interface_input_paths": paths,
                "inexact_paths": inexact,
            },
            exit_code=EXIT_FAIL,
        )
    if contract.parallel_ready and paths:
        raise RuntimeProblem(
            "Tester cannot start in parallel from Builder-owned interface files that are not published",
            result="NEEDS_USER",
            code="PLAN_PARALLEL_INTERFACE_INPUT_UNPUBLISHED",
            details={
                "interface_publication_contract_version": (
                    INTERFACE_PUBLICATION_CONTRACT_VERSION
                ),
                "interface_input_paths": paths,
                "action": (
                    "describe a public blackbox entry without implementation paths, or use "
                    "serial publication with parallel_ready=false for every listed file"
                ),
            },
            exit_code=EXIT_FAIL,
        )
    missing = sorted(set(paths) - set(contract.public_prerequisites))
    if missing:
        raise RuntimeProblem(
            "Tester interface file inputs are missing from serial public prerequisites",
            result="NEEDS_USER",
            code="PLAN_INTERFACE_INPUT_UNPUBLISHED",
            details={
                "interface_publication_contract_version": (
                    INTERFACE_PUBLICATION_CONTRACT_VERSION
                ),
                "interface_input_paths": paths,
                "missing_paths": missing,
            },
            exit_code=EXIT_FAIL,
        )


def revision_fields(
    parsed: dict[str, Any], errors: list[str]
) -> tuple[int | None, str | None, str | None]:
    raw_revision = parsed.get("plan_revision")
    revision = raw_revision if type(raw_revision) is int and raw_revision >= 1 else None
    if revision is None:
        errors.append("plan_revision must be a positive integer")

    supersedes = parsed.get("supersedes")
    supersedes_run_id: str | None = None
    supersedes_plan_sha256: str | None = None
    if revision == 1:
        if supersedes is not None:
            errors.append("plan_revision 1 cannot declare supersedes")
    elif revision is not None:
        if not isinstance(supersedes, dict):
            errors.append("plan_revision greater than 1 requires supersedes mapping")
        else:
            run_id = supersedes.get("run_id")
            plan_sha = supersedes.get("plan_sha256")
            if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
                errors.append("supersedes.run_id is invalid")
            else:
                supersedes_run_id = run_id
            if not isinstance(plan_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", plan_sha):
                errors.append("supersedes.plan_sha256 must be a 64-character SHA-256")
            else:
                supersedes_plan_sha256 = plan_sha.lower()
    return revision, supersedes_run_id, supersedes_plan_sha256


def checklist_items(checklist: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^\s*-\s*\[\s\]\s+(.+?)\s*$", checklist)
        if match.group(1).strip()
    ]


def parse_prior_problems_marker(
    raw_marker: str | None,
    *,
    plan_revision: int | None,
    behavior_ids: Sequence[str],
    checklist_count: int,
    level: str,
    allow_missing_for_legacy: bool = False,
    errors: list[str],
) -> tuple[str | None, tuple[dict[str, Any], ...]]:
    if plan_revision == 1:
        if raw_marker is not None:
            errors.append("plan_revision 1 cannot declare prior-problems")
        return None, ()
    if plan_revision is None:
        return None, ()
    if raw_marker is None:
        if allow_missing_for_legacy:
            return None, ()
        errors.append("plan_revision greater than 1 requires prior-problems")
        return None, ()
    try:
        parsed = yaml_load(raw_marker)
    except RuntimeProblem as exc:
        errors.append(f"prior-problems is invalid: {exc}")
        return None, ()
    if not isinstance(parsed, dict):
        errors.append("prior-problems must be a YAML mapping")
        return None, ()
    extra = sorted(set(parsed) - {"schema_version", "snapshot_sha256", "items"})
    if extra:
        errors.append("prior-problems has unknown fields: " + ", ".join(extra))
    if parsed.get("schema_version") != PRIOR_PROBLEMS_SCHEMA_VERSION:
        errors.append(
            f"prior-problems.schema_version must be {PRIOR_PROBLEMS_SCHEMA_VERSION}"
        )
    raw_sha = parsed.get("snapshot_sha256")
    snapshot_sha = (
        raw_sha
        if isinstance(raw_sha, str) and re.fullmatch(r"[0-9a-f]{64}", raw_sha)
        else None
    )
    if snapshot_sha is None:
        errors.append("prior-problems.snapshot_sha256 must be a 64-character SHA-256")
    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        errors.append("prior-problems.items must be a list")
        return snapshot_sha, ()
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_behaviors = set(behavior_ids)
    for index, raw in enumerate(raw_items):
        field = f"prior-problems.items[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{field} must be a mapping")
            continue
        problem_id = raw.get("problem_id")
        handling = raw.get("handling")
        if not isinstance(problem_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", problem_id
        ):
            errors.append(f"{field}.problem_id must be a 64-character SHA-256")
            continue
        if problem_id in seen:
            errors.append(f"prior-problems contains duplicate problem id: {problem_id}")
            continue
        seen.add(problem_id)
        if handling == "include":
            expected = {"problem_id", "handling", "plan_refs"}
            if set(raw) != expected:
                errors.append(f"{field} include must contain only problem_id, handling, plan_refs")
                continue
            refs = raw.get("plan_refs")
            if (
                not isinstance(refs, list)
                or not refs
                or any(
                    not isinstance(item, str)
                    or not PRIOR_PROBLEM_PLAN_REF_RE.fullmatch(item)
                    for item in refs
                )
            ):
                errors.append(
                    f"{field}.plan_refs must match behavior:<id> or checklist:<positive-index>"
                )
                continue
            refs = [str(item) for item in refs]
            if len(refs) != len(set(refs)):
                errors.append(f"{field}.plan_refs must be unique")
                continue
            for ref in refs:
                if ref.startswith("behavior:"):
                    behavior_id = ref.split(":", 1)[1]
                    if level == "L1" or behavior_id not in allowed_behaviors:
                        errors.append(f"{field}.plan_refs references unknown behavior: {ref}")
                elif ref.startswith("checklist:"):
                    raw_index = ref.split(":", 1)[1]
                    if not raw_index.isdigit() or not 1 <= int(raw_index) <= checklist_count:
                        errors.append(f"{field}.plan_refs references unknown checklist item: {ref}")
                else:
                    errors.append(f"{field}.plan_refs has invalid reference: {ref}")
            normalized.append(
                {"problem_id": problem_id, "handling": handling, "plan_refs": refs}
            )
        elif handling == "handled_elsewhere":
            expected = {"problem_id", "handling", "reference"}
            reference = raw.get("reference")
            if set(raw) != expected or not isinstance(reference, str) or not reference.strip():
                errors.append(
                    f"{field} handled_elsewhere requires only a non-empty reference"
                )
                continue
            normalized.append(
                {
                    "problem_id": problem_id,
                    "handling": handling,
                    "reference": reference.strip(),
                }
            )
        elif handling == "discard":
            expected = {"problem_id", "handling", "reason"}
            reason = raw.get("reason")
            if set(raw) != expected or not isinstance(reason, str) or not reason.strip():
                errors.append(f"{field} discard requires only a non-empty reason")
                continue
            normalized.append(
                {"problem_id": problem_id, "handling": handling, "reason": reason.strip()}
            )
        else:
            errors.append(f"{field}.handling is invalid")
    return snapshot_sha, tuple(normalized)


def parse_verification_preparation_marker(
    raw_marker: str | None,
    *,
    plan_revision: int | None,
    errors: list[str],
) -> dict[str, Any] | None:
    if raw_marker is None:
        return None
    try:
        parsed = yaml_load(raw_marker)
    except RuntimeProblem as exc:
        errors.append(f"verification-preparation is invalid: {exc}")
        return None
    if not isinstance(parsed, dict):
        errors.append("verification-preparation must be a YAML mapping")
        return None
    expected = {
        "schema_version",
        "business_run_id",
        "business_plan_sha256",
        "problem_snapshot_sha256",
        "problem_ids",
        "support_paths",
    }
    extra = sorted(set(parsed) - expected)
    missing = sorted(expected - set(parsed))
    if extra:
        errors.append("verification-preparation has unknown fields: " + ", ".join(extra))
    if missing:
        errors.append("verification-preparation is missing fields: " + ", ".join(missing))
    if plan_revision != 1:
        errors.append("verification-preparation requires plan_revision 1")
    if parsed.get("schema_version") != VERIFICATION_PREPARATION_SCHEMA_VERSION:
        errors.append(
            "verification-preparation.schema_version must be "
            f"{VERIFICATION_PREPARATION_SCHEMA_VERSION}"
        )
    run_id = parsed.get("business_run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        errors.append("verification-preparation.business_run_id is invalid")
    plan_sha = parsed.get("business_plan_sha256")
    if not isinstance(plan_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_sha):
        errors.append(
            "verification-preparation.business_plan_sha256 must be a 64-character SHA-256"
        )
    snapshot_sha = parsed.get("problem_snapshot_sha256")
    if not isinstance(snapshot_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha):
        errors.append(
            "verification-preparation.problem_snapshot_sha256 must be a 64-character SHA-256"
        )
    try:
        problem_ids = string_list(
            parsed.get("problem_ids"), "verification-preparation.problem_ids"
        )
        support_paths_raw = string_list(
            parsed.get("support_paths"), "verification-preparation.support_paths"
        )
    except RuntimeProblem as exc:
        errors.append(str(exc))
        return None
    if len(problem_ids) != len(set(problem_ids)) or any(
        not re.fullmatch(r"[0-9a-f]{64}", item) for item in problem_ids
    ):
        errors.append(
            "verification-preparation.problem_ids must be unique 64-character SHA-256 values"
        )
    support_paths: list[str] = []
    for raw in support_paths_raw:
        try:
            path = normalize_allowed_path(raw, directory_hint=False)
        except RuntimeProblem as exc:
            errors.append(f"verification-preparation.support_paths: {exc}")
            continue
        if any(mark in path for mark in "*?["):
            errors.append("verification-preparation.support_paths must be exact paths")
            continue
        support_paths.append(path)
    if len(support_paths) != len(set(support_paths)):
        errors.append("verification-preparation.support_paths must be unique")
    return {
        "business_run_id": run_id,
        "business_plan_sha256": plan_sha,
        "problem_snapshot_sha256": snapshot_sha,
        "problem_ids": sorted(set(problem_ids)),
        "support_paths": sorted(set(support_paths)),
    }


def parse_continuation_from_marker(
    raw_marker: str | None,
    *,
    plan_revision: int | None,
    errors: list[str],
) -> dict[str, Any] | None:
    if raw_marker is None:
        return None
    try:
        parsed = yaml_load(raw_marker)
    except RuntimeProblem as exc:
        errors.append(f"continuation-from is invalid: {exc}")
        return None
    if not isinstance(parsed, dict):
        errors.append("continuation-from must be a YAML mapping")
        return None
    expected = {"schema_version", "preparation_run_id"}
    if set(parsed) != expected:
        errors.append("continuation-from must contain only schema_version and preparation_run_id")
    if plan_revision is None or plan_revision <= 1:
        errors.append("continuation-from requires plan_revision greater than 1")
    if parsed.get("schema_version") != CONTINUATION_FROM_SCHEMA_VERSION:
        errors.append(
            f"continuation-from.schema_version must be {CONTINUATION_FROM_SCHEMA_VERSION}"
        )
    run_id = parsed.get("preparation_run_id")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        errors.append("continuation-from.preparation_run_id is invalid")
    return {
        "preparation_run_id": run_id,
    }


def is_template_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\s*<[^>]+>\s*", value))


def require_plan_schema(
    parsed: dict[str, Any],
    contract_name: str,
    *,
    allow_legacy_v2: bool = False,
) -> int:
    raw = parsed.get("schema_version")
    supported = (
        {PLAN_SCHEMA_VERSION, LEGACY_PLAN_SCHEMA_VERSION}
        if allow_legacy_v2
        else {PLAN_SCHEMA_VERSION}
    )
    if type(raw) is not int or raw not in supported:
        raise RuntimeProblem(
            (
                f"{contract_name} schema_version {raw!r} is unsupported; "
                f"regenerate the plan with schema_version {PLAN_SCHEMA_VERSION} using /plan"
            ),
            result="NEEDS_USER",
            code="PLAN_SCHEMA_UNSUPPORTED",
            details={
                "contract": contract_name,
                "schema_version": raw,
                "supported_schema_versions": sorted(supported),
            },
            exit_code=EXIT_FAIL,
        )
    return raw


def _non_empty_strings(value: Any, field: str, errors: list[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        errors.append(f"{field} must be a non-empty string list")
        return []
    normalized = [str(item).strip() for item in value]
    if len(normalized) != len(set(normalized)):
        errors.append(f"{field} must not contain duplicates")
    return normalized


def parse_test_effectiveness_requirements(
    parsed: dict[str, Any],
    behavior_ids: Sequence[str],
    errors: list[str],
) -> tuple[dict[str, str], ...]:
    raw = parsed.get("test_effectiveness")
    if not isinstance(raw, dict):
        errors.append("test_effectiveness must be a mapping")
        return ()
    unknown = sorted(set(raw) - {"requirements"})
    if unknown:
        errors.append("test_effectiveness has unknown fields: " + ", ".join(unknown))
    requirements = raw.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append("test_effectiveness.requirements must be a non-empty list")
        if behavior_ids:
            errors.append(
                "test_effectiveness requirements do not cover behavior ids: "
                + ", ".join(sorted(behavior_ids))
            )
        return ()
    normalized: list[dict[str, str]] = []
    seen: list[str] = []
    allowed_minimums = {"strong", "reviewed-boundaries"}
    for index, item in enumerate(requirements):
        field = f"test_effectiveness.requirements[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} must be a mapping")
            continue
        extra = sorted(set(item) - {"behavior_id", "minimum"})
        if extra:
            errors.append(f"{field} has unknown fields: " + ", ".join(extra))
        behavior_id = item.get("behavior_id")
        minimum = item.get("minimum")
        if not isinstance(behavior_id, str) or not behavior_id.strip():
            errors.append(f"{field}.behavior_id must be a non-empty string")
            continue
        behavior_id = behavior_id.strip()
        if behavior_id not in behavior_ids:
            errors.append(f"{field}.behavior_id is unknown: {behavior_id}")
        if minimum not in allowed_minimums:
            errors.append(
                f"{field}.minimum {minimum!r} must be strong or reviewed-boundaries"
            )
            continue
        seen.append(behavior_id)
        normalized.append({"behavior_id": behavior_id, "minimum": str(minimum)})
    duplicates = sorted(item for item in set(seen) if seen.count(item) > 1)
    missing = sorted(set(behavior_ids) - set(seen))
    if duplicates:
        errors.append(
            "test_effectiveness requirements duplicate behavior ids: "
            + ", ".join(duplicates)
        )
    if missing:
        errors.append(
            "test_effectiveness requirements do not cover behavior ids: "
            + ", ".join(missing)
        )
    return tuple(sorted(normalized, key=lambda item: item["behavior_id"]))


E2E_LIST_RULES = {
    "tools_called",
    "tools_not_called",
    "response_contains",
    "response_not_contains",
}
E2E_INTEGER_RULES = {"min_tools", "max_tools", "max_steps"}
E2E_HARD_RULES = E2E_LIST_RULES | E2E_INTEGER_RULES


def parse_e2e_cases(
    raw_marker: str | None,
    behavior_ids: Sequence[str],
    errors: list[str],
) -> tuple[tuple[dict[str, Any], ...], str | None]:
    if raw_marker is None:
        return (), evidence_contract.canonical_digest(
            {"schema_version": 1, "cases": []}
        )
    try:
        parsed = yaml_load(raw_marker)
    except RuntimeProblem as exc:
        if exc.code != "PLAN_YAML_INVALID":
            raise
        errors.append(
            "e2e-cases schema_version/cases contain invalid YAML: " + str(exc)
        )
        return (), None
    if not isinstance(parsed, dict):
        errors.append("e2e-cases.schema_version must be 1")
        errors.append("e2e-cases.cases must be a non-empty list")
        return (), None
    extra = sorted(set(parsed) - {"schema_version", "cases"})
    if extra:
        errors.append("e2e-cases has unknown fields: " + ", ".join(extra))
    schema_version = parsed.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        errors.append("e2e-cases.schema_version must be 1")
    cases = parsed.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("e2e-cases.cases must be a non-empty list")
        return (), None
    normalized: list[dict[str, Any]] = []
    case_ids: list[str] = []
    for index, item in enumerate(cases):
        field = f"e2e-cases.cases[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} must be a mapping")
            continue
        allowed = {"id", "covers", "input", "level", "hard_rules", "verify", "quality"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            errors.append(f"{field} has unknown fields: " + ", ".join(unknown))
        case_id = item.get("id")
        if not isinstance(case_id, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", case_id
        ):
            errors.append(f"{field}.id must be unique kebab-case")
            case_id = ""
        else:
            case_ids.append(case_id)
        field = f"e2e-cases.cases[{index}:{case_id or '?'}]"
        covers = _non_empty_strings(item.get("covers"), f"{field}.covers", errors)
        unknown_covers = sorted(set(covers) - set(behavior_ids))
        if unknown_covers:
            errors.append(
                f"{field}.covers references unknown behavior ids: "
                + ", ".join(unknown_covers)
            )
        input_value = item.get("input")
        if not isinstance(input_value, str) or not input_value.strip():
            errors.append(f"{field}.input must be a non-empty string")
            input_value = ""
        level = item.get("level")
        if level not in {"fast", "full"}:
            errors.append(f"{field}.level must be fast or full")
        hard_rules_raw = item.get("hard_rules")
        hard_rules: dict[str, Any] = {}
        if hard_rules_raw is not None:
            if not isinstance(hard_rules_raw, dict):
                errors.append(f"{field}.hard_rules must be a mapping")
            else:
                unknown_rules = sorted(set(hard_rules_raw) - E2E_HARD_RULES)
                if unknown_rules:
                    errors.append(
                        f"{field}.hard_rules has unknown fields: "
                        + ", ".join(unknown_rules)
                    )
                for key, value in hard_rules_raw.items():
                    if key in E2E_LIST_RULES:
                        hard_rules[key] = _non_empty_strings(
                            value, f"{field}.hard_rules.{key}", errors
                        )
                    elif key in E2E_INTEGER_RULES:
                        if type(value) is not int or value < 0:
                            errors.append(
                                f"{field}.hard_rules.{key} must be a non-negative integer"
                            )
                        else:
                            hard_rules[key] = value
        if (
            "min_tools" in hard_rules
            and "max_tools" in hard_rules
            and hard_rules["min_tools"] > hard_rules["max_tools"]
        ):
            errors.append(f"{field}.hard_rules min_tools cannot exceed max_tools")
        for positive, negative in (
            ("tools_called", "tools_not_called"),
            ("response_contains", "response_not_contains"),
        ):
            overlap = sorted(
                set(hard_rules.get(positive, []))
                & set(hard_rules.get(negative, []))
            )
            if overlap:
                errors.append(
                    f"{field}.hard_rules {positive}/{negative} conflict: "
                    + ", ".join(overlap)
                )

        normalized_case: dict[str, Any] = {
            "id": case_id,
            "covers": covers,
            "input": input_value.strip() if isinstance(input_value, str) else "",
            "level": level,
        }
        if hard_rules_raw is not None:
            normalized_case["hard_rules"] = hard_rules
        if level == "fast":
            if not hard_rules:
                errors.append(f"{field}.fast requires non-empty hard_rules")
            if item.get("verify") is not None or item.get("quality") is not None:
                errors.append(f"{field}.fast cannot declare verify or quality")
        elif level == "full":
            verify = item.get("verify")
            quality = item.get("quality")
            if not isinstance(verify, dict):
                errors.append(f"{field}.verify must be a mapping")
                verify = {}
            if not isinstance(quality, dict):
                errors.append(f"{field}.quality must be a mapping")
                quality = {}
            verify_extra = sorted(set(verify) - {"must", "must_not"})
            quality_extra = sorted(set(quality) - {"criteria"})
            if verify_extra:
                errors.append(
                    f"{field}.verify has unknown fields: " + ", ".join(verify_extra)
                )
            if quality_extra:
                errors.append(
                    f"{field}.quality has unknown fields: " + ", ".join(quality_extra)
                )
            verify_must = _non_empty_strings(
                    verify.get("must"), f"{field}.verify.must", errors
                )
            verify_must_not = _non_empty_strings(
                    verify.get("must_not"), f"{field}.verify.must_not", errors
                )
            verify_overlap = sorted(set(verify_must) & set(verify_must_not))
            if verify_overlap:
                errors.append(
                    f"{field}.verify must/must_not conflict: "
                    + ", ".join(verify_overlap)
                )
            normalized_case["verify"] = {
                "must": verify_must,
                "must_not": verify_must_not,
            }
            normalized_case["quality"] = {
                "criteria": _non_empty_strings(
                    quality.get("criteria"), f"{field}.quality.criteria", errors
                )
            }
        normalized.append(normalized_case)
    duplicates = sorted(item for item in set(case_ids) if case_ids.count(item) > 1)
    if duplicates:
        errors.append("e2e case ids must be unique: " + ", ".join(duplicates))
    canonical = {"schema_version": 1, "cases": normalized}
    return tuple(normalized), evidence_contract.canonical_digest(canonical)


def parse_workspace_intake_marker(text: str) -> tuple[dict[str, str], ...]:
    marker = extract_tag(text, "workspace-intake", required=False)
    if marker is None:
        return ()
    parsed = yaml_load(marker)
    errors: list[str] = []
    if not isinstance(parsed, dict):
        errors.append("workspace-intake must be a YAML mapping")
        parsed = {}
    schema_version = parsed.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        errors.append("workspace-intake schema_version must be 1")
    raw_files = parsed.get("files")
    files: list[dict[str, str]] = []
    if not isinstance(raw_files, list) or not raw_files:
        errors.append("workspace-intake.files must be a non-empty list")
    else:
        for index, item in enumerate(raw_files):
            if not isinstance(item, dict):
                errors.append(f"workspace-intake.files[{index}] must be a mapping")
                continue
            try:
                path = workspace_contract.normalize_exact_path(str(item.get("path", "")))
            except workspace_contract.WorkspaceError as exc:
                errors.append(f"workspace-intake.files[{index}]: {exc}")
                continue
            state_sha = str(item.get("state_sha256", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", state_sha):
                errors.append(
                    f"workspace-intake.files[{index}].state_sha256 must be a 64-character SHA-256"
                )
                continue
            files.append({"path": path, "state_sha256": state_sha})
    paths = [item["path"] for item in files]
    if len(paths) != len(set(paths)):
        errors.append("workspace-intake paths must be unique")
    if errors:
        raise RuntimeProblem(
            "workspace intake plan contract needs correction",
            result="NEEDS_USER",
            code="PLAN_WORKSPACE_INTAKE_INVALID",
            details={"errors": errors},
            exit_code=EXIT_FAIL,
        )
    return tuple(sorted(files, key=lambda item: item["path"]))


def parse_evidence_scopes(
    parsed: dict[str, Any],
    *,
    builder_write: Sequence[str],
    tester_write: Sequence[str],
    support_paths: Sequence[str],
    public_prerequisites: Sequence[str],
    errors: list[str],
) -> dict[str, dict[str, tuple[str, ...]]]:
    raw = parsed.get("evidence_scopes")
    if raw is None:
        return {
            "machine": {"affects": ("**",), "exempt": ()},
            "blackbox": {"affects": ("**",), "exempt": ()},
        }
    if not isinstance(raw, dict):
        errors.append("evidence_scopes must be a mapping")
        return {}
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    forced = tuple(sorted(set(tester_write) | set(support_paths) | set(public_prerequisites)))
    for key in ("machine", "blackbox"):
        item = raw.get(key)
        if not isinstance(item, dict):
            errors.append(f"evidence_scopes.{key} must be a mapping")
            continue
        try:
            affects = string_list(
                item.get("affects", []),
                f"evidence_scopes.{key}.affects",
                allow_empty=True,
            )
            exempt = string_list(
                item.get("exempt", []),
                f"evidence_scopes.{key}.exempt",
                allow_empty=True,
            )
        except RuntimeProblem as exc:
            errors.append(str(exc))
            continue
        affects = sorted(set(affects))
        exempt = sorted(set(exempt))
        overlap = sorted(set(affects) & set(exempt))
        if overlap:
            errors.append(f"evidence_scopes.{key} affects/exempt overlap: {', '.join(overlap)}")
        classified = set(affects) | set(exempt)
        missing = sorted(set(builder_write) - classified)
        extra = sorted(classified - set(builder_write))
        if missing or extra:
            errors.append(
                f"evidence_scopes.{key} must classify every builder_write entry exactly once"
            )
        result[key] = {
            "affects": tuple(sorted(set(affects) | set(forced))),
            "exempt": tuple(exempt),
        }
    return result


def parse_plan(
    text: str,
    source: str,
    *,
    allow_legacy_v2: bool = False,
    allow_missing_prior_problems: bool = False,
    source_sha256: str | None = None,
) -> PlanContract:
    raw_source_sha256 = source_sha256 or sha256_text(text)
    canonical_sha256 = sha256_text(canonicalize_plan_text(text))
    workspace_intake = parse_workspace_intake_marker(text)
    checklist = extract_tag(text, "plan-checklist", required=True)
    items = checklist_items(checklist or "")
    if not checklist or not items or any(re.search(r"<[^>]+>", item) for item in items):
        raise RuntimeProblem(
            "plan-checklist must contain unchecked executable items",
            result="NEEDS_USER",
            code="PLAN_CHECKLIST_REQUIRED",
            details={"errors": ["plan-checklist"]},
        )
    unit_spec = extract_tag(text, "unit-test-spec", required=False)
    documentation_spec = extract_tag(text, "documentation-spec", required=False)
    e2e = extract_tag(text, "e2e-cases", required=False)
    prior_problems = extract_tag(text, "prior-problems", required=False)
    verification_preparation_marker = extract_tag(
        text, "verification-preparation", required=False
    )
    continuation_from_marker = extract_tag(text, "continuation-from", required=False)
    if verification_preparation_marker is not None and continuation_from_marker is not None:
        raise RuntimeProblem(
            "a plan cannot be both verification preparation and business continuation",
            result="NEEDS_USER",
            code="PLAN_CONTINUATION_MARKER_CONFLICT",
            details={"errors": ["verification-preparation", "continuation-from"]},
        )
    if unit_spec is None:
        if documentation_spec is None:
            raise RuntimeProblem(
                "unit-test-spec may only be omitted when documentation-spec declares L1",
                result="NEEDS_USER",
                code="PLAN_UNIT_SPEC_REQUIRED",
                details={"errors": ["unit-test-spec or documentation-spec"]},
            )
        if e2e is not None:
            raise RuntimeProblem(
                "L1 documentation-only plans cannot declare e2e-cases",
                result="NEEDS_USER",
                code="PLAN_L1_E2E_INVALID",
                details={"errors": ["e2e-cases"]},
            )
        parsed_doc = yaml_load(documentation_spec)
        errors: list[str] = []
        if not re.search(r"预估改动级别\s*[：:]\s*L1\b", text, re.IGNORECASE):
            errors.append("documentation-spec requires explicit L1 level declaration")
        if not isinstance(parsed_doc, dict):
            raise RuntimeProblem(
                "documentation-spec must be a YAML mapping",
                result="NEEDS_USER",
                code="PLAN_DOCUMENTATION_SPEC_INVALID",
                details={"errors": ["documentation-spec"]},
            )
        schema_version = require_plan_schema(
            parsed_doc,
            "documentation-spec",
            allow_legacy_v2=allow_legacy_v2,
        )
        spec_head = str(parsed_doc.get("spec_head", "")).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", spec_head):
            errors.append("spec_head must be a full 40-character commit SHA")
        plan_revision, supersedes_run_id, supersedes_plan_sha256 = revision_fields(
            parsed_doc, errors
        )
        ownership = parsed_doc.get("ownership")
        if not isinstance(ownership, dict):
            errors.append("ownership must be a mapping")
            ownership = {}
        try:
            builder_raw = string_list(
                ownership.get("builder_write"), "ownership.builder_write"
            )
        except RuntimeProblem:
            builder_raw = []
            errors.append("ownership.builder_write is required")
        builder_write: list[str] = []
        for value in builder_raw:
            if is_template_placeholder(value) or "<" in value or ">" in value:
                errors.append("ownership.builder_write contains an unresolved placeholder")
                continue
            try:
                normalized = normalize_allowed_path(value, directory_hint=False)
            except RuntimeProblem as exc:
                errors.append(f"ownership.builder_write: {exc}")
                continue
            if any(mark in normalized for mark in "*?["):
                errors.append(
                    "documentation ownership.builder_write must name exact Markdown files"
                )
            elif not normalized.lower().endswith(".md"):
                errors.append(
                    "documentation ownership.builder_write may only name .md files"
                )
            else:
                builder_write.append(normalized)
        if len(items) < 2:
            errors.append("L1 plan-checklist must include implementation and review items")
        for item in workspace_intake:
            if not path_allowed(item["path"], builder_write):
                errors.append(
                    "workspace-intake path is outside ownership.builder_write: "
                    + item["path"]
                )
        prior_problem_snapshot_sha256, prior_problem_items = parse_prior_problems_marker(
            prior_problems,
            plan_revision=plan_revision,
            behavior_ids=(),
            checklist_count=len(items),
            level="L1",
            allow_missing_for_legacy=allow_missing_prior_problems,
            errors=errors,
        )
        verification_preparation = parse_verification_preparation_marker(
            verification_preparation_marker,
            plan_revision=plan_revision,
            errors=errors,
        )
        continuation_from = parse_continuation_from_marker(
            continuation_from_marker,
            plan_revision=plan_revision,
            errors=errors,
        )
        if errors:
            raise RuntimeProblem(
                "documentation plan contract needs correction",
                result="NEEDS_USER",
                code="PLAN_CONTRACT_INVALID",
                details={"errors": errors},
            )
        return PlanContract(
            source=source,
            sha256=canonical_sha256,
            source_sha256=raw_source_sha256,
            digest_kind=CANONICAL_PLAN_DIGEST_KIND,
            schema_version=schema_version,
            level="L1",
            spec_head=spec_head.lower(),
            plan_revision=plan_revision,
            parallel_ready=False,
            interfaces=(),
            interface_input_paths=(),
            target_test_dirs=(),
            support_paths=(),
            public_prerequisites=(),
            runner=None,
            builder_write=tuple(sorted(set(builder_write))),
            tester_write=(),
            behavior_ids=(),
            supersedes_run_id=supersedes_run_id,
            supersedes_plan_sha256=supersedes_plan_sha256,
            prior_problem_snapshot_sha256=prior_problem_snapshot_sha256,
            prior_problem_items=prior_problem_items,
            verification_preparation=verification_preparation,
            continuation_from=continuation_from,
            has_e2e_cases=e2e is not None,
            e2e_case_ids=(),
            e2e_cases_sha256=None,
            e2e_cases=(),
            test_effectiveness_requirements=(),
            workspace_intake=workspace_intake,
            evidence_scopes={
                "machine": {"affects": ("**",), "exempt": ()},
                "blackbox": {"affects": ("**",), "exempt": ()},
            },
            tags=tuple(
                ["documentation-spec", "plan-checklist"]
                + (["workspace-intake"] if workspace_intake else [])
                + (["prior-problems"] if prior_problems is not None else [])
                + (
                    ["verification-preparation"]
                    if verification_preparation_marker is not None
                    else []
                )
                + (["continuation-from"] if continuation_from_marker is not None else [])
            ),
        )

    if documentation_spec is not None:
        raise RuntimeProblem(
            "a plan cannot contain both unit-test-spec and documentation-spec",
            result="NEEDS_USER",
            code="PLAN_CONTRACT_INVALID",
            details={"errors": ["duplicate plan contract markers"]},
        )

    parsed = yaml_load(unit_spec)
    if not isinstance(parsed, dict):
        raise RuntimeProblem(
            "unit-test-spec must be a YAML mapping",
            result="NEEDS_USER",
            code="PLAN_UNIT_SPEC_INVALID",
            details={"errors": ["unit-test-spec"]},
        )
    schema_version = require_plan_schema(
        parsed,
        "unit-test-spec",
        allow_legacy_v2=allow_legacy_v2,
    )
    errors: list[str] = []
    spec_head = str(parsed.get("spec_head", "")).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", spec_head):
        errors.append("spec_head must be a full 40-character commit SHA")
    plan_revision, supersedes_run_id, supersedes_plan_sha256 = revision_fields(parsed, errors)
    parallel_ready = parsed.get("parallel_ready")
    if not isinstance(parallel_ready, bool):
        errors.append("parallel_ready must be boolean")

    interfaces_raw = parsed.get("interfaces")
    interfaces: list[Any] = []
    if not isinstance(interfaces_raw, list) or not interfaces_raw:
        errors.append("interfaces must be a non-empty list")
    else:
        for index, interface in enumerate(interfaces_raw):
            if isinstance(interface, str) and interface.strip():
                if is_template_placeholder(interface):
                    errors.append(f"interfaces[{index}] contains an unresolved placeholder")
                    continue
                interfaces.append(interface.strip())
                continue
            if isinstance(interface, dict):
                required_interface = {"module", "import", "signature", "output", "errors"}
                missing = sorted(required_interface - set(interface))
                if missing:
                    errors.append(f"interfaces[{index}] missing {', '.join(missing)}")
                else:
                    invalid_text_fields = [
                        field
                        for field in ("module", "import", "signature", "output")
                        if not isinstance(interface.get(field), str)
                        or not str(interface.get(field)).strip()
                    ]
                    interface_errors = interface.get("errors")
                    if invalid_text_fields:
                        errors.append(
                            f"interfaces[{index}] fields must be non-empty strings: "
                            + ", ".join(invalid_text_fields)
                        )
                    elif any(
                        is_template_placeholder(interface.get(field))
                        for field in ("module", "import", "signature", "output")
                    ):
                        errors.append(f"interfaces[{index}] contains an unresolved placeholder")
                    elif not isinstance(interface_errors, list) or any(
                        not isinstance(item, str) for item in interface_errors
                    ):
                        errors.append(f"interfaces[{index}].errors must be a string list")
                    else:
                        interfaces.append(
                            {
                                "module": str(interface["module"]).strip(),
                                "import": str(interface["import"]).strip(),
                                "signature": str(interface["signature"]).strip(),
                                "output": str(interface["output"]).strip(),
                                "errors": [str(item) for item in interface_errors],
                            }
                        )
                continue
            errors.append(f"interfaces[{index}] must be a string or structured mapping")

    test_context = parsed.get("test_context")
    if not isinstance(test_context, dict):
        errors.append("test_context must be a mapping")
        test_context = {}
    try:
        target_dirs_raw = string_list(test_context.get("target_test_dirs"), "test_context.target_test_dirs")
    except RuntimeProblem:
        target_dirs_raw = []
        errors.append("test_context.target_test_dirs is required")
    try:
        support_raw = string_list(
            test_context.get("support_paths", []),
            "test_context.support_paths",
            allow_empty=True,
        )
    except RuntimeProblem:
        support_raw = []
        errors.append("test_context.support_paths must be a string list")
    try:
        public_prerequisites_raw = string_list(
            test_context.get("public_prerequisites", []),
            "test_context.public_prerequisites",
            allow_empty=True,
        )
    except RuntimeProblem:
        public_prerequisites_raw = []
        errors.append("test_context.public_prerequisites must be a string list")
    if parallel_ready is False and not public_prerequisites_raw:
        errors.append("serial plans require test_context.public_prerequisites")
    if parallel_ready is True and public_prerequisites_raw:
        errors.append("parallel plans cannot declare public prerequisites")
    if any(is_template_placeholder(item) for item in public_prerequisites_raw):
        errors.append("test_context.public_prerequisites contains an unresolved placeholder")
    runner: str | None = None
    if "runner" in test_context:
        runner_raw = test_context.get("runner")
        runner = runner_raw.strip() if isinstance(runner_raw, str) else ""
        if not runner:
            errors.append("test_context.runner must be a non-empty string when declared")
            runner = None
        elif is_template_placeholder(runner):
            errors.append("test_context.runner contains an unresolved placeholder")

    ownership = parsed.get("ownership")
    if not isinstance(ownership, dict):
        errors.append("ownership must be a mapping")
        ownership = {}
    try:
        builder_raw = string_list(ownership.get("builder_write"), "ownership.builder_write")
    except RuntimeProblem:
        builder_raw = []
        errors.append("ownership.builder_write is required")
    try:
        tester_raw = string_list(ownership.get("tester_write"), "ownership.tester_write")
    except RuntimeProblem:
        tester_raw = []
        errors.append("ownership.tester_write is required")

    def normalize_many(values: list[str], field: str, *, directory_hint: bool = False) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if is_template_placeholder(value) or "<" in value or ">" in value:
                errors.append(f"{field}: unresolved placeholder")
                continue
            try:
                normalized.append(normalize_allowed_path(value, directory_hint=directory_hint))
            except RuntimeProblem as exc:
                errors.append(f"{field}: {exc}")
        return sorted(set(normalized))

    target_dirs = normalize_many(target_dirs_raw, "test_context.target_test_dirs", directory_hint=True)
    support_paths = normalize_many(support_raw, "test_context.support_paths")
    builder_write = normalize_many(builder_raw, "ownership.builder_write")
    tester_write = normalize_many(tester_raw, "ownership.tester_write")
    public_prerequisites = normalize_many(
        public_prerequisites_raw, "test_context.public_prerequisites"
    )
    interface_input_paths = extract_interface_input_paths(interfaces, builder_write)
    for prerequisite in public_prerequisites:
        if any(mark in prerequisite for mark in "*?["):
            errors.append(
                "test_context.public_prerequisites must name exact repository files"
            )
        elif not path_allowed(prerequisite, builder_write):
            errors.append(
                "public prerequisite is outside ownership.builder_write: " + prerequisite
            )
        elif path_allowed(prerequisite, tester_write):
            errors.append(
                "public prerequisite cannot be tester-owned: " + prerequisite
            )
    overlap = sorted(
        {f"{left} <-> {right}" for left in builder_write for right in tester_write if patterns_overlap(left, right)}
    )
    if overlap:
        errors.append("builder/tester ownership overlaps: " + ", ".join(overlap))
    protected_support_overlap = sorted(
        {
            f"{builder_pattern} <-> {support_path}"
            for builder_pattern in builder_write
            for support_path in support_paths
            if patterns_overlap(builder_pattern, support_path)
        }
    )
    if protected_support_overlap:
        errors.append(
            "builder ownership overlaps test support paths: "
            + ", ".join(protected_support_overlap)
        )
    for directory in target_dirs:
        probe = directory[:-3].rstrip("/") + "/__probe__.test" if directory.endswith("/**") else directory
        if not any(path_allowed(probe, [pattern]) for pattern in tester_write):
            errors.append(f"target_test_dirs entry is not tester-owned: {directory}")
    for item in workspace_intake:
        path = item["path"]
        if not path_allowed(path, builder_write):
            errors.append("workspace-intake path is outside ownership.builder_write: " + path)
        if path_allowed(path, tester_write):
            errors.append("workspace-intake path cannot be tester-owned: " + path)
        if path in support_paths or path in PROTECTED_RUNTIME_PATHS:
            errors.append("workspace-intake path cannot be a runner/control path: " + path)

    evidence_scopes = parse_evidence_scopes(
        parsed,
        builder_write=builder_write,
        tester_write=tester_write,
        support_paths=support_paths,
        public_prerequisites=public_prerequisites,
        errors=errors,
    )

    behaviors = parsed.get("behaviors")
    behavior_ids: list[str] = []
    if not isinstance(behaviors, list) or not behaviors:
        errors.append("behaviors must be a non-empty list")
    else:
        for index, behavior in enumerate(behaviors):
            if not isinstance(behavior, dict):
                errors.append(f"behaviors[{index}] must be a mapping")
                continue
            behavior_id_raw = behavior.get("id")
            what_raw = behavior.get("what")
            behavior_id = behavior_id_raw.strip() if isinstance(behavior_id_raw, str) else ""
            what = what_raw.strip() if isinstance(what_raw, str) else ""
            if not behavior_id or not what:
                errors.append(f"behaviors[{index}] requires id and what")
            else:
                behavior_ids.append(behavior_id)
                if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", behavior_id):
                    errors.append(f"behaviors[{index}].id must be kebab-case")
                if is_template_placeholder(what):
                    errors.append(f"behaviors[{index}].what contains an unresolved placeholder")
            for key in ("boundaries", "invariants"):
                value = behavior.get(key)
                if (
                    not isinstance(value, list)
                    or not value
                    or any(not isinstance(item, str) or not item.strip() for item in value)
                ):
                    errors.append(f"behaviors[{index}].{key} must be a non-empty string list")
                elif any(is_template_placeholder(item) for item in value):
                    errors.append(f"behaviors[{index}].{key} contains an unresolved placeholder")
    if not isinstance(parsed.get("mock_strategy"), dict):
        errors.append("mock_strategy must be a mapping")
    if len(items) < 3:
        errors.append("plan-checklist must contain at least three executable gate items")
    duplicate_behavior_ids = sorted(
        behavior_id for behavior_id in set(behavior_ids) if behavior_ids.count(behavior_id) > 1
    )
    if duplicate_behavior_ids:
        errors.append("behavior ids must be unique: " + ", ".join(duplicate_behavior_ids))
    if schema_version == PLAN_SCHEMA_VERSION:
        test_effectiveness_requirements = parse_test_effectiveness_requirements(
            parsed, behavior_ids, errors
        )
        e2e_cases, e2e_cases_sha256 = parse_e2e_cases(e2e, behavior_ids, errors)
    else:
        # Existing v2 ledgers retain their frozen legacy marker only for
        # continuation/diagnostics. New plan validation and start never enter
        # this branch.
        test_effectiveness_requirements = ()
        e2e_cases = ()
        e2e_cases_sha256 = None
    prior_problem_snapshot_sha256, prior_problem_items = parse_prior_problems_marker(
        prior_problems,
        plan_revision=plan_revision,
        behavior_ids=behavior_ids,
        checklist_count=len(items),
        level="L2/L3",
        allow_missing_for_legacy=allow_missing_prior_problems,
        errors=errors,
    )
    verification_preparation = parse_verification_preparation_marker(
        verification_preparation_marker,
        plan_revision=plan_revision,
        errors=errors,
    )
    continuation_from = parse_continuation_from_marker(
        continuation_from_marker,
        plan_revision=plan_revision,
        errors=errors,
    )
    if errors:
        raise RuntimeProblem(
            "plan contract needs correction",
            result="NEEDS_USER",
            code="PLAN_CONTRACT_INVALID",
            details={"errors": errors},
        )

    return PlanContract(
        source=source,
        sha256=canonical_sha256,
        source_sha256=raw_source_sha256,
        digest_kind=CANONICAL_PLAN_DIGEST_KIND,
        schema_version=schema_version,
        level="L2/L3",
        spec_head=spec_head.lower(),
        plan_revision=int(plan_revision),
        parallel_ready=bool(parallel_ready),
        interfaces=tuple(interfaces),
        interface_input_paths=interface_input_paths,
        target_test_dirs=tuple(target_dirs),
        support_paths=tuple(support_paths),
        public_prerequisites=tuple(public_prerequisites),
        runner=runner,
        builder_write=tuple(builder_write),
        tester_write=tuple(tester_write),
        behavior_ids=tuple(behavior_ids),
        supersedes_run_id=supersedes_run_id,
        supersedes_plan_sha256=supersedes_plan_sha256,
        prior_problem_snapshot_sha256=prior_problem_snapshot_sha256,
        prior_problem_items=prior_problem_items,
        verification_preparation=verification_preparation,
        continuation_from=continuation_from,
        has_e2e_cases=e2e is not None,
        e2e_case_ids=tuple(str(item["id"]) for item in e2e_cases),
        e2e_cases_sha256=e2e_cases_sha256,
        e2e_cases=e2e_cases,
        test_effectiveness_requirements=test_effectiveness_requirements,
        workspace_intake=workspace_intake,
        evidence_scopes=evidence_scopes,
        tags=tuple(
            ["unit-test-spec", "plan-checklist"]
            + (["e2e-cases"] if e2e is not None else [])
            + (["workspace-intake"] if workspace_intake else [])
            + (["prior-problems"] if prior_problems is not None else [])
            + (
                ["verification-preparation"]
                if verification_preparation_marker is not None
                else []
            )
            + (["continuation-from"] if continuation_from_marker is not None else [])
        ),
    )


def load_plan_file(
    path: Path,
    *,
    allow_legacy_v2: bool = False,
    allow_missing_prior_problems: bool = False,
) -> tuple[str, PlanContract]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeProblem(
            f"cannot read plan: {exc}",
            code="PLAN_READ_ERROR",
        ) from exc
    text = decode_plan_bytes(raw, str(path))
    return text, parse_plan(
        text,
        str(path),
        allow_legacy_v2=allow_legacy_v2,
        allow_missing_prior_problems=allow_missing_prior_problems,
        source_sha256=sha256_bytes(raw),
    )


def verify_plan_unchanged(ledger: dict[str, Any]) -> PlanContract:
    path = Path(str(ledger["plan"]["path"]))
    plan = ledger["plan"]
    contract_version = plan.get("contract_schema_version")
    if type(contract_version) is not int:
        contract_version = LEGACY_PLAN_SCHEMA_VERSION
    legacy_problem_contract = (
        "prior_problem_snapshot_sha256" not in plan
        and "prior_problem_items" not in plan
    )
    _, contract = load_plan_file(
        path,
        allow_legacy_v2=contract_version == LEGACY_PLAN_SCHEMA_VERSION,
        allow_missing_prior_problems=legacy_problem_contract,
    )
    digest_kind = str(plan.get("digest_kind") or LEGACY_PLAN_DIGEST_KIND)
    expected = str(plan["sha256"])
    actual = (
        contract.sha256
        if digest_kind == CANONICAL_PLAN_DIGEST_KIND
        else contract.source_sha256
    )
    expected_frozen = str(plan.get("frozen_sha256") or expected)
    if actual != expected or contract.source_sha256 != expected_frozen:
        raise RuntimeProblem(
            "plan changed after start; start a new run from the new contract",
            result="NEEDS_USER",
            code="PLAN_CHANGED",
            details={
                "digest_kind": digest_kind,
                "expected_sha256": expected,
                "actual_sha256": actual,
                "expected_frozen_sha256": expected_frozen,
                "actual_frozen_sha256": contract.source_sha256,
            },
            exit_code=EXIT_FAIL,
        )
    interface_contract_version = plan.get(
        "interface_publication_contract_version",
        LEGACY_INTERFACE_PUBLICATION_CONTRACT_VERSION,
    )
    if interface_contract_version == INTERFACE_PUBLICATION_CONTRACT_VERSION:
        validate_interface_publication_contract(contract)
        recorded_paths = sorted(str(path) for path in plan.get("interface_input_paths", []))
        if recorded_paths != list(contract.interface_input_paths):
            raise RuntimeProblem(
                "plan interface publication inputs changed after start",
                result="NEEDS_USER",
                code="PLAN_CHANGED",
                details={
                    "recorded_interface_input_paths": recorded_paths,
                    "actual_interface_input_paths": list(contract.interface_input_paths),
                },
                exit_code=EXIT_FAIL,
            )
    return contract


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    index = 0
    pieces = ["^"]
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    index += 1
                    pieces.append("(?:.*/)?")
                else:
                    pieces.append(".*")
                continue
            pieces.append("[^/]*")
        elif char == "?":
            pieces.append("[^/]")
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                pieces.append("\\[")
            else:
                content = pattern[index + 1 : end]
                if content.startswith("!"):
                    content = "^" + content[1:]
                pieces.append("[" + content + "]")
                index = end
        else:
            pieces.append(re.escape(char))
        index += 1
    pieces.append("$")
    return re.compile("".join(pieces))


def path_allowed(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return any(glob_to_regex(pattern).fullmatch(normalized) for pattern in patterns)


def git_changed_paths(worktree: Path, base: str) -> list[str]:
    result = git(worktree, "diff", "--name-only", "--no-renames", base, "--", check=True)
    paths = {line for line in result.stdout.splitlines() if line}
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard", "-z", check=True)
    paths.update(item for item in untracked.stdout.split("\0") if item)
    return sorted(paths)


def worktree_excluded_paths(
    worktree: Path, excluded_roots: Iterable[Path]
) -> list[str]:
    excluded: list[str] = []
    worktree_root = worktree.resolve()
    for root in excluded_roots:
        try:
            relative = root.resolve().relative_to(worktree_root).as_posix().rstrip("/")
        except ValueError:
            continue
        if relative and relative != ".":
            excluded.append(relative)
    return excluded


def ignored_untracked_paths(
    worktree: Path, *, excluded_roots: Iterable[Path] = ()
) -> list[str]:
    args = ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]
    excluded = worktree_excluded_paths(worktree, excluded_roots)
    if excluded:
        args.extend(["--", "."])
        for relative in excluded:
            args.extend(
                [
                    f":(exclude){relative}",
                    f":(exclude){relative}/**",
                ]
            )
    result = git(worktree, *args, check=True)
    return sorted(item for item in result.stdout.split("\0") if item)


def status_paths(worktree: Path, *, untracked: str) -> list[str]:
    result = git(
        worktree,
        "status",
        "--porcelain=v1",
        "-z",
        f"--untracked-files={untracked}",
        check=True,
    )
    entries = result.stdout.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:] if len(entry) >= 4 else ""
        if path:
            paths.add(path)
        if status[0] in {"R", "C"} and index < len(entries) and entries[index]:
            paths.add(entries[index])
            index += 1
    return sorted(paths)


def dirty_paths(worktree: Path) -> list[str]:
    return status_paths(worktree, untracked="all")


def categorized_worktree_paths(
    worktree: Path, *, excluded_roots: Iterable[Path] = ()
) -> dict[str, list[str]]:
    args = [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
    ]
    excluded = worktree_excluded_paths(worktree, excluded_roots)
    if excluded:
        args.extend(["--", "."])
        for relative in excluded:
            args.extend(
                [
                    f":(exclude){relative}",
                    f":(exclude){relative}/**",
                ]
            )
    result = git(worktree, *args, check=True)
    entries = result.stdout.split("\0")
    categorized: dict[str, set[str]] = {
        "tracked_dirty_paths": set(),
        "untracked_paths": set(),
        "ignored_paths": set(),
    }
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        status = entry[:2]
        path = (entry[3:] if len(entry) >= 4 else "").rstrip("/")
        if status == "!!":
            bucket = "ignored_paths"
        elif status == "??":
            bucket = "untracked_paths"
        else:
            bucket = "tracked_dirty_paths"
        if path:
            categorized[bucket].add(path)
        if status[0] in {"R", "C"} and index < len(entries) and entries[index]:
            categorized["tracked_dirty_paths"].add(entries[index].rstrip("/"))
            index += 1
    return {key: sorted(paths) for key, paths in categorized.items()}


def tracked_unstaged_paths(worktree: Path) -> list[str]:
    result = git(worktree, "diff-files", "--name-only", "-z", check=True)
    return sorted(item for item in result.stdout.split("\0") if item)


def without_runtime_state_paths(
    worktree: Path,
    paths: Iterable[str],
    *,
    runtime_state_roots: Iterable[Path] = (),
) -> list[str]:
    worktree_root = worktree.resolve()
    excluded: list[str] = []
    for root in runtime_state_roots:
        try:
            relative = root.resolve().relative_to(worktree_root).as_posix().rstrip("/")
        except ValueError:
            continue
        if relative and relative != ".":
            excluded.append(relative)
    return sorted(
        path
        for path in set(paths)
        if not any(path == prefix or path.startswith(f"{prefix}/") for prefix in excluded)
    )


def worktree_residue(
    worktree: Path, *, runtime_state_roots: Iterable[Path] = ()
) -> list[str]:
    residue = set(dirty_paths(worktree)) | set(ignored_untracked_paths(worktree))
    return without_runtime_state_paths(
        worktree, residue, runtime_state_roots=runtime_state_roots
    )


def target_worktree_unstaged_residue(repo: Path, worktree: Path) -> list[str]:
    residue = set(tracked_unstaged_paths(worktree))
    return without_runtime_state_paths(
        worktree, residue, runtime_state_roots=(state_root(repo),)
    )


def changed_destination_paths(repo: Path, old_head: str, new_head: str) -> list[str]:
    result = git(
        repo,
        "diff",
        "--name-status",
        "-z",
        old_head,
        new_head,
        "--",
        check=True,
    )
    entries = result.stdout.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        status = entries[index]
        index += 1
        if not status:
            continue
        kind = status[0]
        if kind in {"R", "C"}:
            if index + 1 >= len(entries):
                raise RuntimeProblem(
                    "cannot parse renamed verification path",
                    code="TARGET_DIFF_INVALID",
                )
            index += 1
            destination = entries[index]
            index += 1
            if destination:
                paths.add(destination)
            continue
        if index >= len(entries):
            raise RuntimeProblem(
                "cannot parse target verification path",
                code="TARGET_DIFF_INVALID",
            )
        path = entries[index]
        index += 1
        if kind != "D" and path:
            paths.add(path)
    return sorted(paths)


def paths_collide(left: str, right: str) -> bool:
    return (
        left == right
        or left.startswith(f"{right}/")
        or right.startswith(f"{left}/")
    )


def target_checkout_facts(
    repo: Path,
    branch: str,
    *,
    expected_head: str | None = None,
    desired_head: str | None = None,
    live_target_head: str | None = None,
    workspace_intake: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target = worktree_for_branch(repo, branch)
    empty_residue = {
        "tracked_dirty_paths": [],
        "untracked_paths": [],
        "ignored_paths": [],
    }
    if target is None:
        return {
            "target_worktree": None,
            "target_residue": empty_residue,
            "workspace_intake_allowed_paths": [],
            "workspace_intake_drift": [],
            "finalize_blockers": [],
        }

    runtime_roots = (state_root(repo),)
    categorized = categorized_worktree_paths(target, excluded_roots=runtime_roots)
    untracked = without_runtime_state_paths(
        target,
        categorized["untracked_paths"],
        runtime_state_roots=runtime_roots,
    )
    ignored = without_runtime_state_paths(
        target,
        categorized["ignored_paths"],
        runtime_state_roots=runtime_roots,
    )
    tracked = without_runtime_state_paths(
        target,
        categorized["tracked_dirty_paths"],
        runtime_state_roots=runtime_roots,
    )
    blockers: list[dict[str, Any]] = []
    allowed_intake: list[str] = []
    intake_drift: list[dict[str, Any]] = []
    raw_entries = (
        workspace_intake.get("entries", [])
        if isinstance(workspace_intake, dict) and workspace_intake.get("required") is True
        else []
    )
    if isinstance(raw_entries, list) and raw_entries:
        for captured in raw_entries:
            if not isinstance(captured, dict) or not isinstance(captured.get("path"), str):
                continue
            path = str(captured["path"])
            try:
                current = workspace_contract.path_manifest(target, path, require_dirty=False)
                final = (
                    workspace_contract.tree_path_state(repo, desired_head, path)
                    if desired_head
                    else captured
                )
            except workspace_contract.WorkspaceError as exc:
                intake_drift.append({"path": path, "code": exc.code, **exc.details})
                continue
            if workspace_contract.path_state_is_known(current, captured, final):
                allowed_intake.append(path)
            else:
                intake_drift.append(
                    {
                        "path": path,
                        "captured_state_sha256": captured.get("state_sha256"),
                        "current_state_sha256": current.get("state_sha256"),
                        "final_state_sha256": final.get("state_sha256"),
                    }
                )
        tracked = [path for path in tracked if path not in set(allowed_intake)]
        untracked = [path for path in untracked if path not in set(allowed_intake)]
        ignored = [path for path in ignored if path not in set(allowed_intake)]
        if intake_drift:
            blockers.append(
                {
                    "code": "TARGET_INTAKE_DRIFT",
                    "paths": [item["path"] for item in intake_drift],
                    "details": intake_drift,
                }
            )
    transition_required = bool(expected_head and desired_head and expected_head != desired_head)

    if transition_required and live_target_head == desired_head:
        expected_tree = git(
            repo, "rev-parse", f"{expected_head}^{{tree}}", check=True
        ).stdout.strip()
        desired_tree = git(
            repo, "rev-parse", f"{desired_head}^{{tree}}", check=True
        ).stdout.strip()
        index_result = git(target, "write-tree", check=False)
        index_tree = index_result.stdout.strip()
        if index_result.returncode != 0:
            blockers.append(
                {
                    "code": "TARGET_SYNC_UNSAFE",
                    "paths": tracked,
                    "details": {
                        "reason": "target index tree cannot be resolved",
                        "returncode": index_result.returncode,
                        "stdout": tail_text(index_result.stdout),
                        "stderr": tail_text(index_result.stderr),
                    },
                }
            )
            transition_required = False
        elif index_tree in {expected_tree, desired_tree}:
            tracked = without_runtime_state_paths(
                target,
                tracked_unstaged_paths(target),
                runtime_state_roots=runtime_roots,
            )
            tracked = [path for path in tracked if path not in set(allowed_intake)]
            transition_required = index_tree == expected_tree
        else:
            blockers.append(
                {
                    "code": "TARGET_SYNC_UNSAFE",
                    "paths": tracked,
                    "details": {
                        "reason": "target index matches neither side of finalize intent",
                        "index_tree": index_tree,
                        "expected_tree": expected_tree,
                        "desired_tree": desired_tree,
                    },
                }
            )
            transition_required = False

    if tracked:
        blockers.insert(
            0,
            {
                "code": "TARGET_TRACKED_DIRTY",
                "paths": tracked,
            },
        )

    if transition_required and expected_head and desired_head:
        destination_paths = changed_destination_paths(repo, expected_head, desired_head)
        collision_paths = sorted(
            path
            for path in set([*untracked, *ignored])
            if any(paths_collide(path, changed) for changed in destination_paths)
        )
        if collision_paths:
            blockers.append(
                {
                    "code": "TARGET_PATH_COLLISION",
                    "paths": collision_paths,
                }
            )
        if not blockers and not allowed_intake:
            dry_run = git(
                target,
                "read-tree",
                "-n",
                "-u",
                "-m",
                expected_head,
                desired_head,
                check=False,
            )
            if dry_run.returncode != 0:
                blockers.append(
                    {
                        "code": "TARGET_SYNC_UNSAFE",
                        "paths": destination_paths,
                        "details": {
                            "returncode": dry_run.returncode,
                            "stdout": tail_text(dry_run.stdout),
                            "stderr": tail_text(dry_run.stderr),
                        },
                    }
                )

    return {
        "target_worktree": str(target),
        "target_residue": {
            "tracked_dirty_paths": tracked,
            "untracked_paths": untracked,
            "ignored_paths": ignored,
        },
        "workspace_intake_allowed_paths": sorted(allowed_intake),
        "workspace_intake_drift": intake_drift,
        "finalize_blockers": blockers,
    }


def worktree_for_branch(repo: Path, branch: str) -> Path | None:
    result = git(repo, "worktree", "list", "--porcelain", check=True)
    path: Path | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree ") :]).resolve()
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def add_info_exclude(repo: Path) -> None:
    common = git(repo, "rev-parse", "--git-common-dir", check=True).stdout.strip()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (repo / common_path).resolve()
    exclude = common_path / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    rule = "/.builder-loop/"
    if rule not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as stream:
            if existing and not existing.endswith("\n"):
                stream.write("\n")
            stream.write(rule + "\n")


def load_loop_config(repo: Path, spec_head: str) -> tuple[list[dict[str, Any]], str, int]:
    result = git(repo, "show", f"{spec_head}:.claude/loop.yml", check=False)
    if result.returncode != 0:
        raise RuntimeProblem(
            ".claude/loop.yml must exist at spec_head",
            code="LOOP_CONFIG_MISSING",
            details={"spec_head": spec_head, "stderr": tail_text(result.stderr)},
        )
    text = result.stdout
    try:
        import yaml  # type: ignore

        parsed = yaml.safe_load(text)
    except ImportError:
        parsed = None
    except Exception as exc:
        raise RuntimeProblem(
            f"invalid .claude/loop.yml: {exc}",
            code="LOOP_CONFIG_INVALID",
        ) from exc

    raw_commands: Any = None
    raw_max_iterations: Any = 5
    if isinstance(parsed, dict):
        raw_commands = parsed.get("pass_cmd")
        raw_max_iterations = parsed.get("max_iterations", 5)
    if parsed is None:
        raw_commands = fallback_pass_commands(text)
        match = re.search(r"(?m)^\s*max_iterations\s*:\s*([^#\s]+)", text)
        if match:
            raw_max_iterations = match.group(1).strip().strip("'\"")
    if isinstance(raw_commands, str):
        raw_commands = [{"stage": "verify", "cmd": raw_commands, "timeout": 1800}]
    if not isinstance(raw_commands, list) or not raw_commands:
        raise RuntimeProblem(
            ".claude/loop.yml pass_cmd must be a non-empty list",
            code="LOOP_CONFIG_INVALID",
        )
    if isinstance(raw_max_iterations, bool):
        raise RuntimeProblem(
            ".claude/loop.yml max_iterations must be an integer",
            code="LOOP_CONFIG_INVALID",
        )
    try:
        max_iterations = int(raw_max_iterations)
    except (TypeError, ValueError) as exc:
        raise RuntimeProblem(
            ".claude/loop.yml max_iterations must be an integer",
            code="LOOP_CONFIG_INVALID",
        ) from exc
    if max_iterations <= 0:
        raise RuntimeProblem(
            ".claude/loop.yml max_iterations must be positive",
            code="LOOP_CONFIG_INVALID",
        )

    commands: list[dict[str, Any]] = []
    for index, item in enumerate(raw_commands):
        if not isinstance(item, dict):
            raise RuntimeProblem(
                f"pass_cmd[{index}] must be a mapping",
                code="LOOP_CONFIG_INVALID",
            )
        raw_command = item.get("cmd")
        command = raw_command.strip() if isinstance(raw_command, str) else ""
        if not command:
            raise RuntimeProblem(
                f"pass_cmd[{index}].cmd must be a non-empty string",
                code="LOOP_CONFIG_INVALID",
            )
        reject_tautological_command(command)
        raw_timeout = item.get("timeout", 1800)
        if isinstance(raw_timeout, bool):
            raise RuntimeProblem(
                f"pass_cmd[{index}].timeout must be an integer",
                code="LOOP_CONFIG_INVALID",
            )
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise RuntimeProblem(
                f"pass_cmd[{index}].timeout must be an integer",
                code="LOOP_CONFIG_INVALID",
            ) from exc
        if timeout <= 0:
            raise RuntimeProblem(
                f"pass_cmd[{index}].timeout must be positive",
                code="LOOP_CONFIG_INVALID",
            )
        raw_stage = item.get("stage", f"stage-{index + 1}")
        stage = raw_stage.strip() if isinstance(raw_stage, str) else ""
        if not stage:
            raise RuntimeProblem(
                f"pass_cmd[{index}].stage is empty",
                code="LOOP_CONFIG_INVALID",
            )
        commands.append({"stage": stage, "cmd": command, "timeout": timeout})
    return commands, sha256_text(text), max_iterations


def load_verification_commands(
    repo: Path, spec_head: str, contract: PlanContract
) -> tuple[list[dict[str, Any]], str, str, int]:
    if contract.level == "L1":
        return [], sha256_text("L1:no-machine-runner"), "none", 5
    exists = git(repo, "cat-file", "-e", f"{spec_head}:.claude/loop.yml", check=False)
    if exists.returncode == 0:
        if contract.runner is not None:
            raise RuntimeProblem(
                "v2 plans cannot declare test_context.runner when .claude/loop.yml exists at spec_head",
                result="NEEDS_USER",
                code="PLAN_RUNNER_DUPLICATE_SOURCE",
                details={
                    "spec_head": spec_head,
                    "repository_source": ".claude/loop.yml",
                    "plan_source": "test_context.runner",
                },
                exit_code=EXIT_FAIL,
            )
        try:
            commands, config_sha, max_iterations = load_loop_config(repo, spec_head)
        except RuntimeProblem as exc:
            exc.details.setdefault(
                "effective_verification_source", ".claude/loop.yml"
            )
            raise
        return commands, config_sha, ".claude/loop.yml", max_iterations
    if contract.runner:
        try:
            reject_tautological_command(contract.runner)
        except RuntimeProblem as exc:
            exc.details.setdefault(
                "effective_verification_source", "plan:test_context.runner"
            )
            raise
        return (
            [{"stage": "plan-runner", "cmd": contract.runner, "timeout": 1800}],
            sha256_text(contract.runner),
            "plan:test_context.runner",
            5,
        )
    raise RuntimeProblem(
        "v2 plans must declare test_context.runner when .claude/loop.yml is absent at spec_head",
        result="NEEDS_USER",
        code="VERIFICATION_RUNNER_MISSING",
        details={"spec_head": spec_head},
        exit_code=EXIT_FAIL,
    )


def validate_runner_dependencies_at_spec_head(
    repo: Path, spec_head: str, commands: Sequence[dict[str, Any]]
) -> None:
    missing: list[dict[str, str]] = []
    invalid_entries: list[dict[str, str]] = []
    for item in commands:
        command = str(item["cmd"])
        for path in runner_repository_paths(command):
            entry = git(repo, "ls-tree", spec_head, "--", path, check=False)
            metadata = entry.stdout.strip().split("\t", 1)[0].split()
            if entry.returncode != 0 or not metadata:
                missing.append({"path": path, "runner": command})
                continue
            mode = metadata[0]
            object_type = metadata[1] if len(metadata) > 1 else ""
            if mode not in {"100644", "100755"} or object_type != "blob":
                invalid_entries.append(
                    {
                        "path": path,
                        "runner": command,
                        "mode": mode,
                        "type": object_type,
                    }
                )
    if missing:
        raise RuntimeProblem(
            "repository runner dependencies must already exist at spec_head",
            result="NEEDS_USER",
            code="RUNNER_NOT_FROZEN",
            details={"spec_head": spec_head, "missing": missing},
            exit_code=EXIT_FAIL,
        )
    if invalid_entries:
        raise RuntimeProblem(
            "repository runner dependencies must be regular files at spec_head",
            result="NEEDS_USER",
            code="RUNNER_ENTRY_NOT_REGULAR",
            details={"spec_head": spec_head, "invalid": invalid_entries},
            exit_code=EXIT_FAIL,
        )


def fallback_pass_commands(text: str) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    in_pass = False
    current: dict[str, Any] | None = None
    pass_indent = 0
    for line in text.splitlines():
        if not in_pass:
            match = re.match(r"^(\s*)pass_cmd\s*:\s*(.*?)\s*$", line)
            if not match:
                continue
            in_pass = True
            pass_indent = len(match.group(1))
            scalar = match.group(2).strip().strip("'\"")
            if scalar:
                return [{"stage": "verify", "cmd": scalar, "timeout": 1800}]
            continue
        if line.strip() and len(line) - len(line.lstrip()) <= pass_indent and not line.lstrip().startswith("-"):
            break
        item = re.match(r"^\s*-\s*(?:stage\s*:\s*)?(.*?)\s*$", line)
        if item:
            if current:
                commands.append(current)
            current = {"stage": item.group(1).strip().strip("'\"") or f"stage-{len(commands) + 1}"}
            continue
        field = re.match(r"^\s+(stage|cmd|timeout)\s*:\s*(.*?)\s*$", line)
        if field and current is not None:
            value: Any = field.group(2).strip().strip("'\"")
            if field.group(1) == "timeout":
                with contextlib.suppress(ValueError):
                    value = int(value)
            current[field.group(1)] = value
    if current:
        commands.append(current)
    return commands


def shell_commands(body: str) -> tuple[list[list[str]], list[str]]:
    try:
        lexer = shlex.shlex(body, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError as exc:
        raise RuntimeProblem(
            f"cannot parse shell verification command: {exc}",
            code="RUNNER_INVALID",
        ) from exc
    commands: list[list[str]] = []
    operators: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token in {"&&", "||", ";", "|", "&", "(", ")"}:
            if current:
                commands.append(current)
                current = []
            if token not in {"(", ")"}:
                operators.append(token)
            continue
        current.append(token)
    if current:
        commands.append(current)
    return commands, operators[: max(0, len(commands) - 1)]


def unwrap_command_tokens(tokens: Sequence[str]) -> list[str]:
    remaining = list(tokens)
    while remaining and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0]):
        remaining.pop(0)
    while remaining and remaining[0] in {"command", "exec", "time"}:
        remaining.pop(0)
        while remaining and remaining[0].startswith("-"):
            remaining.pop(0)
    if remaining and remaining[0] == "env":
        remaining.pop(0)
        while remaining:
            token = remaining[0]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                remaining.pop(0)
                continue
            if token in {"-i", "--ignore-environment", "--"}:
                remaining.pop(0)
                continue
            if token in {"-u", "--unset"} and len(remaining) > 1:
                del remaining[:2]
                continue
            if token.startswith("--unset="):
                remaining.pop(0)
                continue
            break
    return remaining


def literal_test_truth(tokens: Sequence[str]) -> bool | None:
    values = list(tokens)
    if values and values[0] == "[" and values[-1:] == ["]"]:
        values = ["test", *values[1:-1]]
    if not values or values[0] != "test":
        return None
    args = values[1:]
    if len(args) == 1:
        return bool(args[0])
    if len(args) == 2 and args[0] in {"-n", "-z"}:
        return bool(args[1]) if args[0] == "-n" else not bool(args[1])
    if len(args) != 3:
        return None
    left, operator, right = args
    if operator in {"=", "=="}:
        return left == right
    if operator == "!=":
        return left != right
    if operator in {"-eq", "-ne", "-lt", "-le", "-gt", "-ge"}:
        try:
            first, second = int(left), int(right)
        except ValueError:
            return None
        return {
            "-eq": first == second,
            "-ne": first != second,
            "-lt": first < second,
            "-le": first <= second,
            "-gt": first > second,
            "-ge": first >= second,
        }[operator]
    return None


def python_snippet_is_tautological(body: str) -> bool:
    try:
        module = ast.parse(body, mode="exec")
    except SyntaxError:
        return False

    def is_zero(node: ast.AST | None) -> bool:
        return isinstance(node, ast.Constant) and node.value == 0

    def is_exit_zero(call: ast.AST) -> bool:
        if not isinstance(call, ast.Call) or len(call.args) > 1 or call.keywords:
            return False
        function = call.func
        named_exit = isinstance(function, ast.Name) and function.id in {"exit", "SystemExit"}
        sys_exit = (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "sys"
            and function.attr == "exit"
        )
        return (named_exit or sys_exit) and (not call.args or is_zero(call.args[0]))

    if not module.body:
        return True
    for statement in module.body:
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Import) and all(alias.name == "sys" for alias in statement.names):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            function = statement.value.func
            if is_exit_zero(statement.value):
                continue
            if isinstance(function, ast.Name) and function.id == "print":
                continue
        if isinstance(statement, ast.Raise) and is_exit_zero(statement.exc):
            continue
        return False
    return True


def is_python_executable_name(value: str) -> bool:
    name = Path(value).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?m?", name) is not None


PYTHON_INTERPRETER_FLAG_OPTIONS = {
    "-b",
    "-B",
    "-d",
    "-E",
    "-h",
    "--help",
    "-i",
    "-I",
    "-O",
    "-OO",
    "-P",
    "-q",
    "-R",
    "-s",
    "-S",
    "-u",
    "-v",
    "-V",
    "--version",
    "-x",
}
PYTHON_INTERPRETER_VALUE_OPTIONS = {"-W", "-X", "--check-hash-based-pycs"}


def split_python_invocation(
    values: Sequence[str], *, command: str, fail_closed_unknown: bool
) -> tuple[str, str | None, list[str]]:
    """Return the Python entrypoint kind, value and remaining arguments.

    Repository dependency and runner-control inspection must agree about where
    interpreter options end and a ``-m`` module begins.  Unknown options are
    either rejected by fail-closed callers or reported as an opaque invocation.
    """

    args = list(values)
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        if token == "-c":
            return "inline", None, args[index + 1 :]
        if token == "-m":
            if index + 1 >= len(args):
                raise RuntimeProblem(
                    "python -m has no module",
                    code="RUNNER_DEPENDENCY_UNRESOLVED",
                    details={"runner": command},
                )
            return "module", args[index + 1], args[index + 2 :]
        if token in PYTHON_INTERPRETER_FLAG_OPTIONS:
            index += 1
            continue
        if token in PYTHON_INTERPRETER_VALUE_OPTIONS:
            if index + 1 >= len(args):
                raise RuntimeProblem(
                    "python interpreter option has no value",
                    code="RUNNER_DEPENDENCY_UNRESOLVED",
                    details={"runner": command, "option": token},
                )
            index += 2
            continue
        if (
            token.startswith("-W")
            or token.startswith("-X")
            or token.startswith("--check-hash-based-pycs=")
        ):
            index += 1
            continue
        if token.startswith("-"):
            if fail_closed_unknown:
                raise RuntimeProblem(
                    "Python interpreter options cannot be determined statically",
                    code="RUNNER_DEPENDENCY_UNRESOLVED",
                    details={"runner": command, "option": token},
                )
            return "opaque", None, []
        break
    if index < len(args):
        return "script", args[index], args[index + 1 :]
    return "none", None, []


def reject_unsafe_runner_constructs(command: str) -> None:
    if re.search(r"\bexit\s+\$\(\(\s*0\s*\)\)", command):
        raise RuntimeProblem(
            "pass_cmd has a statically successful arithmetic exit",
            code="TAUTOLOGICAL_PASS_COMMAND",
            details={"cmd": command},
        )
    commands, _operators = shell_commands(command)
    control_words = {
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "while",
        "until",
        "for",
        "select",
        "do",
        "done",
        "case",
        "esac",
        "function",
        "coproc",
        "{",
        "}",
    }
    for tokens in commands:
        if any(re.fullmatch(r"PATH=.*", token) for token in tokens):
            raise RuntimeProblem(
                "verification PATH overrides are not allowed; use a protected repository wrapper",
                code="RUNNER_PATH_OVERRIDE_UNSUPPORTED",
                details={"cmd": command},
            )
        remaining = unwrap_command_tokens(tokens)
        if not remaining:
            continue
        if remaining[0] == "!":
            raise RuntimeProblem(
                "pass_cmd cannot invert a verification result",
                code="INVERTED_PASS_COMMAND",
                details={"cmd": command},
            )
        if remaining[0] in control_words:
            raise RuntimeProblem(
                "pass_cmd shell control flow must live in a protected repository script",
                code="RUNNER_CONTROL_FLOW_UNSUPPORTED",
                details={"cmd": command, "token": remaining[0]},
            )
        executable_name = Path(remaining[0]).name
        if executable_name in {"bash", "sh"} and "-c" in remaining:
            flag = remaining.index("-c")
            if len(remaining) > flag + 1:
                reject_unsafe_runner_constructs(remaining[flag + 1])
        if is_python_executable_name(executable_name) and len(remaining) >= 3 and remaining[1] == "-c":
            if python_snippet_is_tautological(remaining[2]):
                raise RuntimeProblem(
                    "pass_cmd Python snippet is tautological",
                    code="TAUTOLOGICAL_PASS_COMMAND",
                    details={"cmd": command},
                )
            raise RuntimeProblem(
                "inline Python verification must live in a protected repository script",
                code="RUNNER_INLINE_CODE_UNSUPPORTED",
                details={"cmd": command},
            )
        if remaining[0] == "exit" and (
            len(remaining) != 2 or not re.fullmatch(r"\d+", remaining[1])
        ):
            raise RuntimeProblem(
                "pass_cmd has an unresolved dynamic exit status",
                code="RUNNER_EXIT_STATUS_UNRESOLVED",
                details={"cmd": command},
            )


def command_truth(tokens: Sequence[str]) -> bool | None:
    remaining = unwrap_command_tokens(tokens)
    if not remaining:
        return True
    executable = remaining[0]
    if executable in {"true", ":", "/bin/true", "/usr/bin/true", "echo", "printf"}:
        return True
    if executable in {"false", "/bin/false", "/usr/bin/false"}:
        return False
    if executable == "exit" and len(remaining) == 2 and re.fullmatch(r"\d+", remaining[1]):
        return int(remaining[1]) == 0
    tested = literal_test_truth(remaining)
    if tested is not None:
        return tested
    if executable in {"bash", "sh", "/bin/bash", "/bin/sh"}:
        with contextlib.suppress(ValueError):
            flag = remaining.index("-c")
            if len(remaining) > flag + 1:
                return shell_truth(remaining[flag + 1])
    if is_python_executable_name(executable) and len(remaining) >= 3 and remaining[1] == "-c":
        python_body = re.sub(r"\s+", " ", remaining[2].strip()).rstrip(";")
        if re.fullmatch(r"(?:pass|(?:sys\.)?exit\(0\)|print\(.*\))", python_body):
            return True
    return None


def shell_truth(body: str) -> bool | None:
    commands, operators = shell_commands(body.strip().rstrip(";"))
    if not commands:
        return True
    value = command_truth(commands[0])
    for operator, command in zip(operators, commands[1:]):
        right = command_truth(command)
        if operator in {";", "|", "&"}:
            value = right
        elif operator == "&&":
            if value is False:
                value = False
            elif value is True:
                value = right
            elif right is False:
                value = False
            else:
                value = None
        elif operator == "||":
            if value is True:
                value = True
            elif value is False:
                value = right
            elif right is True:
                value = True
            else:
                value = None
    return value


def reject_tautological_command(command: str) -> None:
    normalized = re.sub(r"\s+", " ", command.strip())
    try:
        tokens = shlex.split(normalized, comments=True)
    except ValueError as exc:
        raise RuntimeProblem(
            f"cannot parse pass command: {exc}",
            code="LOOP_CONFIG_INVALID",
        ) from exc
    if not tokens:
        raise RuntimeProblem("empty pass command", code="LOOP_CONFIG_INVALID")
    reject_unsafe_runner_constructs(normalized)
    if shell_truth(normalized) is True:
        raise RuntimeProblem(
            "pass_cmd is tautological and cannot provide independent evidence",
            code="TAUTOLOGICAL_PASS_COMMAND",
            details={"cmd": command},
        )


def runner_repository_paths(
    command: str, *, resolve_blackbox_dependencies: bool = False
) -> list[str]:
    candidates: list[str] = []

    def combine(directory: str, value: str) -> str:
        if os.path.isabs(value):
            raise RuntimeProblem(
                "verification scripts must be repository-relative",
                code="RUNNER_EXTERNAL_SCRIPT",
                details={"runner": command, "script": value},
            )
        joined = str(PurePosixPath(directory) / value) if directory else value
        return normalize_allowed_path(joined, directory_hint=False)

    def next_directory(directory: str, value: str) -> str:
        if "$" in value or "`" in value or os.path.isabs(value):
            raise RuntimeProblem(
                "verification working directory cannot be determined statically",
                code="RUNNER_DEPENDENCY_UNRESOLVED",
                details={"runner": command, "directory": value},
            )
        joined = str(PurePosixPath(directory) / value) if directory else value
        if str(PurePosixPath(joined)) == ".":
            return ""
        normalized = normalize_allowed_path(joined, directory_hint=True)
        return normalized[:-3].rstrip("/") if normalized.endswith("/**") else normalized

    def add_directory_target(directory: str, value: str) -> None:
        resolved = next_directory(directory, value)
        candidates.append(f"{resolved}/**" if resolved else "**")

    def add_unittest_target(directory: str, value: str) -> None:
        if "$" in value or "`" in value:
            raise RuntimeProblem(
                "blackbox unittest target cannot be determined statically",
                code="RUNNER_DEPENDENCY_UNRESOLVED",
                details={"runner": command, "target": value},
            )
        target = value.split("::", 1)[0]
        if target.endswith(".py"):
            candidates.append(combine(directory, target))
            return
        raise RuntimeProblem(
            "blackbox unittest target cannot be distinguished as a file or package",
            code="RUNNER_DEPENDENCY_UNRESOLVED",
            details={"runner": command, "target": value},
        )

    def inspect_unittest(values: Sequence[str], directory: str) -> None:
        args = list(values)
        if args and args[0] == "discover":
            start = "."
            for index, token in enumerate(args[1:]):
                if token in {"-s", "--start-directory"} and index + 2 < len(args):
                    start = args[index + 2]
                    break
                if token.startswith("--start-directory="):
                    start = token.split("=", 1)[1]
                    break
            add_directory_target(directory, start)
            return
        skip_next = False
        targets = 0
        for token in args:
            if skip_next:
                skip_next = False
                continue
            if token in {"-k", "--durations"}:
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            add_unittest_target(directory, token)
            targets += 1
        if targets == 0:
            add_directory_target(directory, ".")

    def inspect_pytest(values: Sequence[str], directory: str) -> None:
        value_options = {
            "-k",
            "-m",
            "-o",
            "-p",
            "--basetemp",
            "--capture",
            "--color",
            "--durations",
            "--durations-min",
            "--junit-prefix",
            "--junit-suite-name",
            "--junitxml",
            "--maxfail",
            "--tb",
            "--verbosity",
        }
        flag_options = {
            "-q",
            "--quiet",
            "-v",
            "--verbose",
            "-x",
            "--exitfirst",
            "-s",
            "--collect-only",
            "--co",
            "--disable-warnings",
            "--strict-config",
            "--strict-markers",
        }
        directory_options = {"--confcutdir", "--rootdir"}
        targets = 0
        index = 0
        positional_only = False
        while index < len(values):
            token = values[index]
            if not positional_only and token == "--":
                positional_only = True
                index += 1
                continue
            if not positional_only and token == "-c":
                if index + 1 >= len(values):
                    raise RuntimeProblem(
                        "pytest config option has no path",
                        code="RUNNER_DEPENDENCY_UNRESOLVED",
                        details={"runner": command},
                    )
                candidates.append(combine(directory, values[index + 1]))
                index += 2
                continue
            if not positional_only and token.startswith("-c="):
                candidates.append(combine(directory, token.split("=", 1)[1]))
                index += 1
                continue
            matched_directory = (
                next(
                    (
                        name
                        for name in directory_options
                        if token == name or token.startswith(name + "=")
                    ),
                    None,
                )
                if not positional_only
                else None
            )
            if matched_directory is not None:
                if token == matched_directory:
                    if index + 1 >= len(values):
                        raise RuntimeProblem(
                            "pytest directory option has no path",
                            code="RUNNER_DEPENDENCY_UNRESOLVED",
                            details={"runner": command, "option": matched_directory},
                        )
                    value = values[index + 1]
                    index += 2
                else:
                    value = token.split("=", 1)[1]
                    index += 1
                add_directory_target(directory, value)
                continue
            if not positional_only and token in value_options:
                if index + 1 >= len(values):
                    raise RuntimeProblem(
                        "pytest option has no value",
                        code="RUNNER_DEPENDENCY_UNRESOLVED",
                        details={"runner": command, "option": token},
                    )
                index += 2
                continue
            if not positional_only and any(
                token.startswith(option + "=") for option in value_options
            ):
                index += 1
                continue
            if not positional_only and token in flag_options:
                index += 1
                continue
            if not positional_only and token.startswith("-"):
                raise RuntimeProblem(
                    "pytest option arity cannot be determined statically",
                    code="RUNNER_DEPENDENCY_UNRESOLVED",
                    details={"runner": command, "option": token},
                )
            target = token.split("::", 1)[0]
            if "$" in target or "`" in target:
                raise RuntimeProblem(
                    "blackbox pytest target cannot be determined statically",
                    code="RUNNER_DEPENDENCY_UNRESOLVED",
                    details={"runner": command, "target": target},
                )
            if target.endswith(".py"):
                candidates.append(combine(directory, target))
            else:
                add_directory_target(directory, target)
            targets += 1
            index += 1
        if targets == 0:
            add_directory_target(directory, ".")

    def inspect_python(values: Sequence[str], directory: str) -> None:
        kind, entrypoint, entrypoint_args = split_python_invocation(
            values,
            command=command,
            fail_closed_unknown=resolve_blackbox_dependencies,
        )
        if kind == "inline":
            if resolve_blackbox_dependencies:
                raise RuntimeProblem(
                    "inline blackbox code dependencies cannot be determined statically",
                    code="RUNNER_DEPENDENCY_UNRESOLVED",
                    details={"runner": command},
                )
            return
        if kind == "module":
            if not resolve_blackbox_dependencies:
                return
            if entrypoint == "unittest":
                inspect_unittest(entrypoint_args, directory)
                return
            if entrypoint in {"pytest", "py.test"}:
                inspect_pytest(entrypoint_args, directory)
                return
            raise RuntimeProblem(
                "blackbox Python module dependencies cannot be determined statically",
                code="RUNNER_DEPENDENCY_UNRESOLVED",
                details={"runner": command, "module": entrypoint},
            )
        if kind == "script" and entrypoint is not None:
            candidates.append(combine(directory, entrypoint))
            return
        if resolve_blackbox_dependencies:
            raise RuntimeProblem(
                "blackbox Python command has no repository entry point",
                code="RUNNER_DEPENDENCY_UNRESOLVED",
                details={"runner": command},
            )

    def inspect_body(body: str, directory: str) -> None:
        commands, _operators = shell_commands(body)
        current = directory
        for child in commands:
            remaining = unwrap_command_tokens(child)
            if remaining and remaining[0] == "cd":
                if len(remaining) != 2:
                    raise RuntimeProblem(
                        "verification cd must name one static repository directory",
                        code="RUNNER_DEPENDENCY_UNRESOLVED",
                        details={"runner": command, "tokens": remaining},
                    )
                current = next_directory(current, remaining[1])
                continue
            inspect(child, current)

    def inspect(tokens: Sequence[str], directory: str) -> None:
        remaining = unwrap_command_tokens(tokens)
        if not remaining:
            return
        executable = remaining[0]
        executable_name = Path(executable).name
        if "$" in executable or "`" in executable:
            raise RuntimeProblem(
                "verification executable cannot be determined statically",
                code="RUNNER_DEPENDENCY_UNRESOLVED",
                details={"runner": command, "token": executable},
            )
        if executable in {"source", "."} and len(remaining) > 1:
            candidates.append(combine(directory, remaining[1]))
            return
        if executable_name in {"bash", "sh"}:
            if "-c" in remaining:
                flag = remaining.index("-c")
                if len(remaining) <= flag + 1:
                    raise RuntimeProblem("shell -c runner has no body", code="RUNNER_INVALID")
                inspect_body(remaining[flag + 1], directory)
                return
            script = next((item for item in remaining[1:] if not item.startswith("-")), None)
            if script:
                candidates.append(combine(directory, script))
            return
        if is_python_executable_name(executable_name):
            inspect_python(remaining[1:], directory)
            return
        if executable_name in {"ruby", "node"}:
            if len(remaining) <= 1:
                return
            if remaining[1] in {"-c", "-e"}:
                if resolve_blackbox_dependencies:
                    raise RuntimeProblem(
                        "inline blackbox code dependencies cannot be determined statically",
                        code="RUNNER_DEPENDENCY_UNRESOLVED",
                        details={"runner": command},
                    )
                return
            if remaining[1] == "-m":
                if resolve_blackbox_dependencies and len(remaining) >= 3:
                    module = remaining[2]
                    if module == "unittest":
                        inspect_unittest(remaining[3:], directory)
                    elif module in {"pytest", "py.test"}:
                        inspect_pytest(remaining[3:], directory)
                return
            script = next((item for item in remaining[1:] if not item.startswith("-")), None)
            if script:
                candidates.append(combine(directory, script))
            return
        if resolve_blackbox_dependencies and executable_name == "unittest":
            inspect_unittest(remaining[1:], directory)
            return
        if resolve_blackbox_dependencies and executable_name in {"pytest", "py.test"}:
            inspect_pytest(remaining[1:], directory)
            return
        if "/" in executable:
            candidates.append(combine(directory, executable))
            return
        if resolve_blackbox_dependencies:
            raise RuntimeProblem(
                "blackbox command dependencies cannot be determined statically",
                code="RUNNER_DEPENDENCY_UNRESOLVED",
                details={"runner": command, "executable": executable},
            )

    inspect_body(command, "")
    normalized: list[str] = []
    for candidate in candidates:
        if "$" in candidate or "`" in candidate:
            raise RuntimeProblem(
                "verification script path cannot be determined statically",
                code="RUNNER_DEPENDENCY_UNRESOLVED",
                details={"runner": command, "script": candidate},
            )
        if os.path.isabs(candidate):
            raise RuntimeProblem(
                "verification scripts must be repository-relative",
                code="RUNNER_EXTERNAL_SCRIPT",
                details={"runner": command, "script": candidate},
            )
        normalized.append(normalize_allowed_path(candidate, directory_hint=False))
    return sorted(set(normalized))


def runner_control_paths(command: str) -> list[str]:
    controls: set[str] = set()

    def controlled_path(directory: str, name: str) -> str:
        value = str(PurePosixPath(directory) / name) if directory else name
        return normalize_allowed_path(value, directory_hint=False)

    def option_directory(
        values: Sequence[str], names: set[str], *, default: str = ""
    ) -> str:
        directory: str | None = None
        index = 0
        while index < len(values):
            token = values[index]
            if token in names and index + 1 < len(values):
                directory = values[index + 1]
                index += 2
                continue
            matched = next((name for name in names if token.startswith(name + "=")), None)
            if matched:
                directory = token.split("=", 1)[1]
            index += 1
        if directory is None:
            return default
        if "$" in directory or "`" in directory or os.path.isabs(directory):
            raise RuntimeProblem(
                "verification working directory cannot be determined statically",
                code="RUNNER_DEPENDENCY_UNRESOLVED",
                details={"runner": command, "directory": directory},
            )
        joined = str(PurePosixPath(default) / directory) if default else directory
        normalized = normalize_allowed_path(joined, directory_hint=True)
        return normalized[:-3].rstrip("/") if normalized.endswith("/**") else normalized

    def inspect_body(body: str, base_dir: str) -> None:
        commands, _operators = shell_commands(body)
        current = base_dir
        for child in commands:
            remaining = unwrap_command_tokens(child)
            if remaining and remaining[0] == "cd":
                if len(remaining) != 2:
                    raise RuntimeProblem(
                        "verification cd must name one static repository directory",
                        code="RUNNER_DEPENDENCY_UNRESOLVED",
                        details={"runner": command, "tokens": remaining},
                    )
                current = option_directory(
                    ["--directory", remaining[1]], {"--directory"}, default=current
                )
                continue
            inspect(child, current)

    def inspect(tokens: Sequence[str], base_dir: str = "") -> None:
        remaining = unwrap_command_tokens(tokens)
        if not remaining:
            return
        executable = Path(remaining[0]).name
        if executable in {"bash", "sh"} and "-c" in remaining:
            flag = remaining.index("-c")
            if len(remaining) > flag + 1:
                inspect_body(remaining[flag + 1], base_dir)
            return
        if is_python_executable_name(executable):
            kind, entrypoint, entrypoint_args = split_python_invocation(
                remaining[1:], command=command, fail_closed_unknown=True
            )
            if kind == "module" and entrypoint is not None:
                inspect([entrypoint, *entrypoint_args], base_dir)
            return
        if executable in {"poetry", "pipenv"} and "run" in remaining[1:]:
            run_index = remaining.index("run")
            inspect(remaining[run_index + 1 :], base_dir)
            return
        if executable == "uv" and "run" in remaining[1:]:
            run_index = remaining.index("run")
            run_args = list(remaining[run_index + 1 :])
            project_dir = option_directory(run_args, {"--project"}, default=base_dir)
            index = 0
            while index < len(run_args):
                if run_args[index] == "--project" and index + 1 < len(run_args):
                    del run_args[index : index + 2]
                    continue
                if run_args[index].startswith("--project="):
                    del run_args[index]
                    continue
                if run_args[index] == "--":
                    del run_args[index]
                    break
                index += 1
            inspect(run_args, project_dir)
            return
        if executable in {"make", "gmake"}:
            make_dir = option_directory(remaining[1:], {"-C", "--directory"}, default=base_dir)
            makefile: str | None = None
            for index, token in enumerate(remaining[1:]):
                if token in {"-f", "--file", "--makefile"} and index + 2 < len(remaining):
                    makefile = remaining[index + 2]
                    break
                if token.startswith(("--file=", "--makefile=")):
                    makefile = token.split("=", 1)[1]
                    break
            if makefile:
                controls.add(controlled_path(make_dir, makefile))
            else:
                controls.update(
                    controlled_path(make_dir, name)
                    for name in ("GNUmakefile", "makefile", "Makefile")
                )
        elif executable in {"npm", "npx", "yarn", "pnpm", "bun", "bunx"}:
            package_dir = option_directory(
                remaining[1:],
                {"--prefix", "--cwd", "--dir", "-C"},
                default=base_dir,
            )
            controls.update(
                controlled_path(package_dir, name)
                for name in (
                    "package.json",
                    "package-lock.json",
                    "yarn.lock",
                    "pnpm-lock.yaml",
                    "bun.lockb",
                )
            )
        elif executable in {"pytest", "py.test"}:
            controls.update(
                controlled_path(base_dir, name)
                for name in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py")
            )
        elif executable in {"ruff", "mypy", "tox"}:
            controls.update(
                controlled_path(base_dir, name)
                for name in (
                    "pyproject.toml",
                    "setup.cfg",
                    "tox.ini",
                    "ruff.toml",
                    ".ruff.toml",
                    "mypy.ini",
                    ".mypy.ini",
                )
            )
        elif executable == "cargo":
            controls.update(controlled_path(base_dir, name) for name in ("Cargo.toml", "Cargo.lock"))
        elif executable == "go" and len(remaining) > 1 and remaining[1] == "test":
            controls.update(controlled_path(base_dir, name) for name in ("go.mod", "go.sum"))
        elif executable in {"mvn", "mvnw"}:
            controls.add(controlled_path(base_dir, "pom.xml"))
        elif executable in {"gradle", "gradlew"}:
            controls.update(
                controlled_path(base_dir, name)
                for name in (
                    "build.gradle",
                    "build.gradle.kts",
                    "settings.gradle",
                    "settings.gradle.kts",
                )
            )

    inspect_body(command, "")
    return sorted(controls)


def verification_protected_paths(commands: Iterable[dict[str, Any]]) -> list[str]:
    paths: set[str] = set()
    for item in commands:
        command = str(item["cmd"])
        paths.update(runner_repository_paths(command))
        paths.update(runner_control_paths(command))
    return sorted(paths)


def validate_runner_ownership(contract: PlanContract, commands: Iterable[dict[str, Any]]) -> None:
    if contract.level == "L1":
        return
    for item in commands:
        command = str(item["cmd"])
        for path in runner_repository_paths(command):
            if not path_allowed(path, contract.support_paths):
                raise RuntimeProblem(
                    "repository runner scripts must be declared in test_context.support_paths",
                    result="NEEDS_USER",
                    code="RUNNER_SUPPORT_PATH_MISSING",
                    details={"runner": command, "path": path},
                    exit_code=EXIT_FAIL,
                )
            if path_allowed(path, contract.builder_write):
                raise RuntimeProblem(
                    "builder ownership cannot include a verification runner script",
                    result="NEEDS_USER",
                    code="RUNNER_BUILDER_OWNED",
                    details={"runner": command, "path": path},
                    exit_code=EXIT_FAIL,
                )
            if path_allowed(path, contract.tester_write):
                raise RuntimeProblem(
                    "tester ownership cannot include a verification runner script",
                    result="NEEDS_USER",
                    code="RUNNER_TESTER_OWNED",
                    details={"runner": command, "path": path},
                    exit_code=EXIT_FAIL,
                )
        for path in runner_control_paths(command):
            builder_overlap = [
                pattern for pattern in contract.builder_write if patterns_overlap(pattern, path)
            ]
            tester_overlap = [
                pattern for pattern in contract.tester_write if patterns_overlap(pattern, path)
            ]
            if builder_overlap or tester_overlap:
                raise RuntimeProblem(
                    "verification control files cannot be role-owned",
                    result="NEEDS_USER",
                    code="RUNNER_CONTROL_OWNED",
                    details={
                        "runner": command,
                        "path": path,
                        "builder_overlap": builder_overlap,
                        "tester_overlap": tester_overlap,
                    },
                    exit_code=EXIT_FAIL,
                )


_UNKNOWN = object()


def constant_python_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values = [constant_python_value(item) for item in node.elts]
        if any(value is _UNKNOWN for value in values):
            return _UNKNOWN
        return tuple(values)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        value = constant_python_value(node.operand)
        return _UNKNOWN if value is _UNKNOWN else not bool(value)
    if isinstance(node, ast.BoolOp):
        values = [constant_python_value(item) for item in node.values]
        if any(value is _UNKNOWN for value in values):
            return _UNKNOWN
        return all(bool(value) for value in values) if isinstance(node.op, ast.And) else any(
            bool(value) for value in values
        )
    if isinstance(node, ast.Compare):
        values = [constant_python_value(node.left)] + [
            constant_python_value(item) for item in node.comparators
        ]
        if any(value is _UNKNOWN for value in values):
            return _UNKNOWN
        result = True
        for operator, left, right in zip(node.ops, values, values[1:]):
            try:
                if isinstance(operator, ast.Eq):
                    current = left == right
                elif isinstance(operator, ast.NotEq):
                    current = left != right
                elif isinstance(operator, ast.Lt):
                    current = left < right
                elif isinstance(operator, ast.LtE):
                    current = left <= right
                elif isinstance(operator, ast.Gt):
                    current = left > right
                elif isinstance(operator, ast.GtE):
                    current = left >= right
                elif isinstance(operator, ast.Is):
                    current = left is right
                elif isinstance(operator, ast.IsNot):
                    current = left is not right
                elif isinstance(operator, ast.In):
                    current = left in right
                elif isinstance(operator, ast.NotIn):
                    current = left not in right
                else:
                    return _UNKNOWN
            except (TypeError, ValueError):
                return _UNKNOWN
            result = result and current
        return result
    return _UNKNOWN


def assertion_count(tree: ast.AST) -> int:
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            count += 1
        elif isinstance(node, ast.Call):
            name = ""
            if isinstance(node.func, ast.Attribute):
                name = node.func.attr
            elif isinstance(node.func, ast.Name):
                name = node.func.id
            if name.lower().startswith("assert") or name in {"raises", "fail"}:
                count += 1
    return count


def python_test_findings(path: str, base_text: str | None, current_text: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    try:
        current_tree = ast.parse(current_text, filename=path)
    except SyntaxError:
        return findings

    for node in ast.walk(current_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(
                body[0].value.value, str
            ):
                body = body[1:]
            if not body or all(
                isinstance(item, ast.Pass)
                or (
                    isinstance(item, ast.Expr)
                    and isinstance(item.value, ast.Constant)
                    and item.value.value is Ellipsis
                )
                for item in body
            ):
                findings.append(
                    {"kind": "reward_hacking", "path": path, "reason": f"empty test body: {node.name}"}
                )
        if isinstance(node, ast.Assert):
            value = constant_python_value(node.test)
            if value is not _UNKNOWN and bool(value):
                findings.append(
                    {"kind": "reward_hacking", "path": path, "reason": "constant-true assertion"}
                )
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else ""
            )
            if name == "assertTrue" and node.args:
                value = constant_python_value(node.args[0])
                if value is not _UNKNOWN and bool(value):
                    findings.append(
                        {"kind": "reward_hacking", "path": path, "reason": "constant assertTrue call"}
                    )
            if name in {"assertEqual", "assertIs"} and len(node.args) >= 2:
                left = constant_python_value(node.args[0])
                right = constant_python_value(node.args[1])
                if (left is not _UNKNOWN and right is not _UNKNOWN and left == right) or ast.dump(
                    node.args[0], include_attributes=False
                ) == ast.dump(node.args[1], include_attributes=False):
                    findings.append(
                        {"kind": "reward_hacking", "path": path, "reason": f"tautological {name} call"}
                    )
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.body and all(
                    isinstance(item, ast.Pass)
                    or (
                        isinstance(item, ast.Expr)
                        and isinstance(item.value, ast.Constant)
                        and item.value.value is Ellipsis
                    )
                    for item in handler.body
                ):
                    findings.append(
                        {"kind": "reward_hacking", "path": path, "reason": "exception is swallowed in test"}
                    )

    if base_text is not None:
        try:
            base_tree = ast.parse(base_text, filename=path)
        except SyntaxError:
            base_tree = None
        if base_tree is not None and assertion_count(current_tree) < assertion_count(base_tree):
            findings.append(
                {"kind": "reward_hacking", "path": path, "reason": "existing test assertions were removed"}
            )
    return findings


def python_weakening_marker(current_text: str) -> str | None:
    def literal_strings(node: ast.AST) -> list[str]:
        return [
            str(item.value)
            for item in ast.walk(node)
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]

    def contains_reruns_argument(value: str) -> bool:
        if value == "--reruns" or value.startswith("--reruns="):
            return True
        with contextlib.suppress(ValueError):
            return any(
                token == "--reruns" or token.startswith("--reruns=")
                for token in shlex.split(value)
            )
        return False

    def contains_test_runner_argument(values: Sequence[str]) -> bool:
        tokens: list[str] = []
        for value in values:
            with contextlib.suppress(ValueError):
                tokens.extend(shlex.split(value))
            tokens.append(value)
        normalized = {Path(token).name.lower() for token in tokens}
        return bool(normalized & {"pytest", "py.test", "unittest"})

    try:
        tree = ast.parse(current_text)
    except SyntaxError:
        try:
            tokens = [
                token.string
                for token in tokenize.generate_tokens(io.StringIO(current_text).readline)
                if token.type
                not in {
                    tokenize.COMMENT,
                    tokenize.STRING,
                    tokenize.ENCODING,
                    tokenize.ENDMARKER,
                    tokenize.INDENT,
                    tokenize.DEDENT,
                    tokenize.NEWLINE,
                    tokenize.NL,
                }
            ]
        except (tokenize.TokenError, IndentationError):
            return None
        executable = "".join(tokens)
        aliases: dict[str, str] = {}
        for match in re.finditer(
            r"import(pytest|unittest|subprocess)(?:as([A-Za-z_]\w*))?",
            executable,
            re.IGNORECASE,
        ):
            module = match.group(1).lower()
            aliases[(match.group(2) or module).lower()] = module
        for alias, module in aliases.items():
            if module == "pytest" and re.search(
                rf"{re.escape(alias)}\.mark\.(?:skip|skipif|xfail|flaky)",
                executable,
                re.IGNORECASE,
            ):
                return f"{alias}.mark"
            if module == "unittest" and re.search(
                rf"{re.escape(alias)}\.skip(?:if|unless)?",
                executable,
                re.IGNORECASE,
            ):
                return f"{alias}.skip"
        match = re.search(
            r"pytest\.mark\.(?:skip|skipif|xfail|flaky)|"
            r"unittest\.skip(?:If|Unless)?|@skip\b|"
            r"(?:pytest\.)?xfail\s*\(|\bflaky\s*\(",
            executable,
            re.IGNORECASE,
        )
        return match.group(0) if match else None

    class LocalBindingCollector(ast.NodeVisitor):
        def __init__(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
            self.names: set[str] = set()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                arguments = [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ]
                if node.args.vararg is not None:
                    arguments.append(node.args.vararg)
                if node.args.kwarg is not None:
                    arguments.append(node.args.kwarg)
                self.names.update(argument.arg for argument in arguments)
            for statement in node.body:
                self.visit(statement)

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                self.names.add(node.id)

        def visit_Import(self, node: ast.Import) -> None:
            self.names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            self.names.update(alias.asname or alias.name for alias in node.names if alias.name != "*")

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.names.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.names.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.names.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name:
                self.names.add(node.name)
            for statement in node.body:
                self.visit(statement)

    class WeakeningVisitor(ast.NodeVisitor):
        supported_roots = {"pytest", "unittest", "subprocess", "flaky"}
        suspicious_targets = {
            "pytest.skip",
            "pytest.xfail",
            "pytest.mark.skip",
            "pytest.mark.skipif",
            "pytest.mark.xfail",
            "pytest.mark.flaky",
            "unittest.skip",
            "unittest.skipif",
            "unittest.skipunless",
            "flaky.flaky",
        }
        runner_targets = {
            "pytest.main",
            "subprocess.run",
            "subprocess.popen",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "trusted.run_process",
            "trusted.run_cli",
        }
        pytestmark_targets = {
            "pytest.mark.skip",
            "pytest.mark.skipif",
            "pytest.mark.xfail",
            "pytest.mark.flaky",
        }

        def __init__(self) -> None:
            self.scopes: list[dict[str, str | None]] = [{}]
            self.marker: str | None = None

        def bind(self, name: str, target: str | None) -> None:
            self.scopes[-1][name] = target

        def bind_target(self, node: ast.AST) -> None:
            if isinstance(node, ast.Name):
                self.bind(node.id, None)
            elif isinstance(node, (ast.Tuple, ast.List)):
                for item in node.elts:
                    self.bind_target(item)

        def lookup(self, name: str) -> str:
            for scope in reversed(self.scopes):
                if name in scope:
                    return scope[name] or ""
            if name in {"run_process", "run_cli"}:
                return f"trusted.{name}"
            return ""

        def resolve(self, node: ast.AST) -> str:
            if isinstance(node, ast.Name):
                return self.lookup(node.id)
            if isinstance(node, ast.Attribute):
                prefix = self.resolve(node.value)
                return f"{prefix}.{node.attr}" if prefix else ""
            return ""

        def import_target(self, module: str) -> str | None:
            return module if module.split(".", 1)[0] in self.supported_roots else None

        def enter_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
            local_names = LocalBindingCollector(node).names
            self.scopes.append({name: None for name in local_names})

        def leave_scope(self) -> None:
            self.scopes.pop()

        def inspect_decorator(self, node: ast.AST) -> None:
            target = node.func if isinstance(node, ast.Call) else node
            resolved = self.resolve(target).lower()
            if resolved in self.suspicious_targets and self.marker is None:
                self.marker = resolved

        def inspect_pytestmark_assignment(
            self, targets: Sequence[ast.AST], value: ast.AST
        ) -> None:
            if len(self.scopes) != 1 or not any(
                isinstance(target, ast.Name) and target.id == "pytestmark"
                for target in targets
            ):
                return
            values = (
                value.elts
                if isinstance(value, (ast.List, ast.Tuple, ast.Set))
                else [value]
            )
            for item in values:
                target = item.func if isinstance(item, ast.Call) else item
                resolved = self.resolve(target).lower()
                if resolved in self.pytestmark_targets and self.marker is None:
                    self.marker = resolved
                    return

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                self.bind(local, self.import_target(alias.name))

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                self.bind(local, self.import_target(f"{module}.{alias.name}"))

        def visit_Assign(self, node: ast.Assign) -> None:
            self.inspect_pytestmark_assignment(node.targets, node.value)
            self.visit(node.value)
            for target in node.targets:
                self.bind_target(target)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            self.visit(node.annotation)
            if node.value is not None:
                self.inspect_pytestmark_assignment([node.target], node.value)
                self.visit(node.value)
            self.bind_target(node.target)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            self.visit(node.target)
            self.visit(node.value)
            self.bind_target(node.target)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
            self.visit(node.value)
            self.bind_target(node.target)

        def visit_For(self, node: ast.For) -> None:
            self.visit(node.iter)
            self.bind_target(node.target)
            for statement in [*node.body, *node.orelse]:
                self.visit(statement)

        visit_AsyncFor = visit_For

        def visit_With(self, node: ast.With) -> None:
            for item in node.items:
                self.visit(item.context_expr)
                if item.optional_vars is not None:
                    self.bind_target(item.optional_vars)
            for statement in node.body:
                self.visit(statement)

        visit_AsyncWith = visit_With

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.type is not None:
                self.visit(node.type)
            if node.name:
                self.bind(node.name, None)
            for statement in node.body:
                self.visit(statement)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            for decorator in node.decorator_list:
                self.inspect_decorator(decorator)
                self.visit(decorator)
            for value in [*node.args.defaults, *node.args.kw_defaults]:
                if value is not None:
                    self.visit(value)
            if node.returns is not None:
                self.visit(node.returns)
            self.bind(node.name, None)
            self.enter_scope(node)
            for statement in node.body:
                self.visit(statement)
            self.leave_scope()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            for decorator in node.decorator_list:
                self.inspect_decorator(decorator)
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
            self.bind(node.name, None)
            self.enter_scope(node)
            for statement in node.body:
                self.visit(statement)
            self.leave_scope()

        def visit_Lambda(self, node: ast.Lambda) -> None:
            local = {
                argument.arg: None
                for argument in [
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                ]
            }
            if node.args.vararg is not None:
                local[node.args.vararg.arg] = None
            if node.args.kwarg is not None:
                local[node.args.kwarg.arg] = None
            self.scopes.append(local)
            self.visit(node.body)
            self.leave_scope()

        def visit_Call(self, node: ast.Call) -> None:
            target = self.resolve(node.func).lower()
            if target in self.suspicious_targets and self.marker is None:
                self.marker = target
            if target in self.runner_targets and self.marker is None:
                argument_nodes = list(node.args)
                argument_nodes.extend(
                    keyword.value
                    for keyword in node.keywords
                    if keyword.arg in {"args", "argv", "command", "cmd"}
                )
                literals = [
                    value
                    for argument in argument_nodes
                    for value in literal_strings(argument)
                ]
                if any(contains_reruns_argument(value) for value in literals):
                    self.marker = "--reruns"
                elif target.startswith("subprocess.") and contains_test_runner_argument(
                    literals
                ):
                    self.marker = target
            self.generic_visit(node)

    visitor = WeakeningVisitor()
    visitor.visit(tree)
    return visitor.marker


def reward_hacking_findings(worktree: Path, base: str, paths: Iterable[str]) -> list[dict[str, str]]:
    suspicious = re.compile(
        r"pytest\.mark\.(?:skip|skipif|xfail|flaky)|"
        r"unittest\.skip|@skip\b|--reruns\b|"
        r"xfail\s*\(|\bflaky\b",
        re.IGNORECASE,
    )
    findings: list[dict[str, str]] = []
    for path in paths:
        base_blob = git(worktree, "show", f"{base}:{path}", check=False)
        current_path = worktree / path
        if base_blob.returncode == 0 and not current_path.exists():
            findings.append(
                {"kind": "reward_hacking", "path": path, "reason": "existing test file was deleted"}
            )
            continue
        if path.endswith(".py") and current_path.is_file():
            try:
                current_text = current_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            marker = python_weakening_marker(current_text)
            if marker:
                findings.append(
                    {
                        "kind": "reward_hacking",
                        "path": path,
                        "reason": f"suspicious test weakening marker: {marker}",
                    }
                )
            findings.extend(
                python_test_findings(
                    path,
                    base_blob.stdout if base_blob.returncode == 0 else None,
                    current_text,
                )
            )
            continue
        diff = git(worktree, "diff", "--no-ext-diff", "--unified=0", base, "--", path, check=False)
        if diff.returncode not in {0, 1}:
            continue
        for line in diff.stdout.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            match = suspicious.search(line[1:])
            if match:
                findings.append(
                    {
                        "kind": "reward_hacking",
                        "path": path,
                        "reason": f"suspicious test weakening marker: {match.group(0)}",
                    }
                )
                break
    return findings


def role_check_facts(repo: Path, ledger: dict[str, Any], role: str) -> dict[str, Any]:
    verify_plan_unchanged(ledger)
    spec_head = str(ledger["spec_head"])
    if role == "tester":
        worktree = Path(str(ledger["worktrees"]["tester"]["path"]))
        head = full_head(worktree)
        if not is_ancestor(repo, spec_head, head):
            raise RuntimeProblem(
                "tester branch is not descended from spec_head",
                code="TESTER_HISTORY_DIVERGED",
                details={"spec_head": spec_head, "tester_head": head},
            )
        integration = ledger["tester_integration"]
        author_base = str(integration.get("base_head") or spec_head)
        if not is_ancestor(repo, author_base, head):
            raise RuntimeProblem(
                "tester branch is not descended from its frozen author baseline",
                code="TESTER_BASELINE_DIVERGED",
                details={"base_head": author_base, "tester_head": head},
            )
        changed = git_changed_paths(worktree, author_base)
        reward_base = str(integration.get("source_head") or author_base)
        if not is_ancestor(repo, reward_base, head):
            raise RuntimeProblem(
                "tester branch is not descended from its last integrated source head",
                code="TESTER_HISTORY_REWRITTEN",
                details={"source_head": reward_base, "tester_head": head},
            )
        reward_changed = git_changed_paths(worktree, reward_base)
        allowed = list(ledger["plan"]["tester_write"])
        runner_paths = list(ledger.get("loop_config", {}).get("runner_paths", []))
        violations: list[dict[str, str]] = [
            {
                "kind": "ownership",
                "path": path,
                "reason": "tester path is outside ownership.tester_write",
            }
            for path in changed
            if not path_allowed(path, allowed)
        ]
        violations.extend(
            {
                "kind": "protected",
                "path": path,
                "reason": "verification runner entry is immutable",
            }
            for path in changed
            if path_allowed(path, runner_paths)
        )
        ignored = [path for path in ignored_untracked_paths(worktree) if path_allowed(path, allowed)]
        violations.extend(
            {
                "kind": "ignored_untracked",
                "path": path,
                "reason": "tester-owned ignored file cannot become Git evidence",
            }
            for path in ignored
        )
        violations.extend(reward_hacking_findings(worktree, reward_base, reward_changed))
        return {
            "role": role,
            "head": head,
            "checked_head": head,
            "base_head": author_base,
            "changed_paths": changed,
            "ignored_owned_paths": ignored,
            "allowed_paths": allowed,
            "violations": violations,
        }
    if role == "builder":
        worktree = Path(str(ledger["worktrees"]["builder"]["path"]))
        head = full_head(worktree)
        if not is_ancestor(repo, spec_head, head):
            raise RuntimeProblem(
                "builder branch is not descended from spec_head",
                code="BUILDER_HISTORY_DIVERGED",
                details={"spec_head": spec_head, "builder_head": head},
            )
        protected = set(PROTECTED_RUNTIME_PATHS)
        plan_path = Path(str(ledger["plan"]["path"]))
        with contextlib.suppress(ValueError):
            protected.add(plan_path.relative_to(repo).as_posix())
        all_changed = git_changed_paths(worktree, spec_head)
        builder_allowed = list(ledger["plan"]["builder_write"])
        tester_owned_patterns = list(ledger["plan"]["tester_write"])
        support_patterns = list(ledger["plan"].get("support_paths", []))
        ownership = ledger["tester_integration"]
        baseline = ownership.get("ownership_baseline_head")
        owned = list(ownership.get("owned_paths", []))
        violations: list[dict[str, str]] = []
        for path in all_changed:
            if path in protected:
                violations.append(
                    {"kind": "protected", "path": path, "reason": "runtime contract path is immutable"}
                )
            elif path_allowed(path, support_patterns):
                unchanged_integrated_support = False
                if baseline and path in owned:
                    compare = git(worktree, "diff", "--quiet", str(baseline), "--", path, check=False)
                    unchanged_integrated_support = compare.returncode == 0
                if not unchanged_integrated_support:
                    violations.append(
                        {
                            "kind": "protected",
                            "path": path,
                            "reason": "verification support path is independent of Builder",
                        }
                    )
            elif path_allowed(path, tester_owned_patterns):
                unchanged_integrated_test = False
                if baseline and path in owned:
                    compare = git(worktree, "diff", "--quiet", str(baseline), "--", path, check=False)
                    unchanged_integrated_test = compare.returncode == 0
                if not unchanged_integrated_test:
                    violations.append(
                        {"kind": "ownership", "path": path, "reason": "path is tester-owned"}
                    )
            elif ledger["plan"].get("level") == "L1" and not path.lower().endswith(".md"):
                violations.append(
                    {
                        "kind": "ownership",
                        "path": path,
                        "reason": "L1 plans may only change Markdown files",
                    }
                )
            elif not path_allowed(path, builder_allowed):
                violations.append(
                    {"kind": "ownership", "path": path, "reason": "path is outside ownership.builder_write"}
                )

        publication = ledger.get("prerequisite_publication", {})
        published_head = publication.get("head") if isinstance(publication, dict) else None
        if published_head:
            for path in publication.get("paths", []):
                compare = git(
                    worktree, "diff", "--quiet", str(published_head), "--", str(path), check=False
                )
                if compare.returncode == 1:
                    violations.append(
                        {
                            "kind": "published_prerequisite_changed",
                            "path": str(path),
                            "reason": "published Tester prerequisite is immutable in this run",
                        }
                    )
                elif compare.returncode != 0:
                    raise RuntimeProblem(
                        "cannot compare a published prerequisite with the Builder candidate",
                        code="PREREQUISITE_COMPARE_ERROR",
                        details={"path": str(path), "stderr": tail_text(compare.stderr)},
                    )

        ignored = [
            path
            for path in ignored_untracked_paths(worktree)
            if path_allowed(path, builder_allowed + tester_owned_patterns + support_patterns)
        ]
        for path in ignored:
            violations.append(
                {
                    "kind": "ignored_untracked",
                    "path": path,
                    "reason": "ignored file in an owned or verification path cannot become Git evidence",
                }
            )

        owned_violations: list[str] = []
        if baseline and owned:
            for path in owned:
                result = git(worktree, "diff", "--quiet", str(baseline), "--", path, check=False)
                if result.returncode == 1:
                    owned_violations.append(path)
                elif result.returncode not in {0, 1}:
                    raise RuntimeProblem(
                        "cannot compare tester-owned path",
                        code="OWNERSHIP_CHECK_ERROR",
                        details={"path": path, "stderr": tail_text(result.stderr)},
                    )
        existing_violation_paths = {item["path"] for item in violations}
        for path in sorted(set(owned_violations) - existing_violation_paths):
            violations.append(
                {"kind": "ownership", "path": path, "reason": "builder modified tester-owned integrated evidence"}
            )
        return {
            "role": role,
            "head": head,
            "checked_head": head,
            "base_head": spec_head,
            "changed_paths": all_changed,
            "ignored_owned_paths": ignored,
            "allowed_paths": builder_allowed,
            "protected_paths": sorted(protected),
            "tester_owned_paths": owned,
            "violations": violations,
        }
    raise RuntimeProblem(f"unknown role: {role}", code="ROLE_INVALID")


def ensure_role_pass(repo: Path, ledger: dict[str, Any], role: str) -> dict[str, Any]:
    facts = role_check_facts(repo, ledger, role)
    if facts["violations"]:
        raise RuntimeProblem(
            f"{role} write boundary violated",
            result="NEEDS_USER",
            code="ROLE_BOUNDARY_VIOLATION",
            details=facts,
            exit_code=EXIT_FAIL,
        )
    return facts


def checkpoint(worktree: Path, run_id: str, role: str) -> tuple[str, str, list[str]]:
    before = full_head(worktree)
    dirty = dirty_paths(worktree)
    if not dirty:
        return before, before, []
    git(worktree, "add", "-A", check=True)
    result = git(
        worktree,
        "-c",
        "user.name=Codex Builder Loop",
        "-c",
        "user.email=codex-builder-loop@localhost",
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "-m",
        f"chore(codex-loop): {role} checkpoint {run_id}",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeProblem(
            f"cannot checkpoint {role} changes",
            code="CHECKPOINT_COMMIT_FAILED",
            details={"stdout": tail_text(result.stdout), "stderr": tail_text(result.stderr)},
        )
    return before, full_head(worktree), dirty


def preflight_runner(worktree: Path, command: str) -> None:
    root = worktree.resolve()
    for path in runner_repository_paths(command):
        entry = worktree / path
        candidate = entry.resolve()
        if entry.is_symlink() or root not in candidate.parents:
            raise RuntimeProblem(
                "verification runner script must be a regular file inside the candidate worktree",
                code="RUNNER_ENTRY_NOT_REGULAR",
                details={"path": str(entry), "resolved_path": str(candidate), "runner": command},
            )
        if not entry.is_file():
            raise RuntimeProblem(
                "verification runner script is missing",
                code="RUNNER_MISSING",
                details={"path": str(entry), "runner": command},
            )

    builtins = {"cd", "source", ".", "test", "[", "export", "readonly", "local"}

    def inspect_body(body: str, directory: Path) -> None:
        commands, _operators = shell_commands(body)
        current = directory
        for child in commands:
            remaining = unwrap_command_tokens(child)
            if not remaining:
                continue
            if remaining[0] == "cd":
                if len(remaining) != 2:
                    raise RuntimeProblem(
                        "verification cd must name one static repository directory",
                        code="RUNNER_DEPENDENCY_UNRESOLVED",
                    )
                current = (current / remaining[1]).resolve()
                if not current.is_dir() or worktree.resolve() not in {current, *current.parents}:
                    raise RuntimeProblem(
                        "verification working directory is missing or outside the repository",
                        code="RUNNER_MISSING",
                        details={"path": str(current), "runner": command},
                    )
                continue
            inspect(child, current)

    def inspect(tokens: Sequence[str], directory: Path) -> None:
        remaining = unwrap_command_tokens(tokens)
        if not remaining:
            return
        executable = remaining[0]
        executable_name = Path(executable).name
        if executable_name in {"bash", "sh"} and "-c" in remaining:
            flag = remaining.index("-c")
            if len(remaining) <= flag + 1:
                raise RuntimeProblem("shell -c runner has no body", code="RUNNER_INVALID")
            inspect_body(remaining[flag + 1], directory)
            return
        if executable in builtins:
            return
        if "/" in executable:
            executable_path = (
                (directory / executable).resolve()
                if not os.path.isabs(executable)
                else Path(executable)
            )
            if not executable_path.exists():
                raise RuntimeProblem(
                    "verification executable is missing",
                    code="RUNNER_MISSING",
                    details={"path": str(executable_path), "runner": command},
                )
        elif shutil.which(executable) is None:
            raise RuntimeProblem(
                "verification executable is not available",
                code="RUNNER_MISSING",
                details={"executable": executable, "runner": command},
            )

    inspect_body(command, worktree.resolve())


def unmerged_paths(worktree: Path) -> list[str]:
    result = git(worktree, "diff", "--name-only", "--diff-filter=U", check=True)
    return sorted(line for line in result.stdout.splitlines() if line)


def active_ledgers_for_session(
    repo: Path, session_id: str
) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for candidate_repo in repository_worktrees(repo):
        runs = state_root(candidate_repo) / "runs"
        if not runs.exists():
            continue
        for path in sorted(runs.glob("*/ledger.json")):
            try:
                ledger = read_json(path)
                owner_repo = resolve_repo(str(ledger["repo_root"]))
                recorded_run_id = ensure_run_id(str(ledger["run_id"]))
                expected = ledger_path(owner_repo, recorded_run_id).resolve()
            except (KeyError, RuntimeProblem) as exc:
                raise RuntimeProblem(
                    "cannot prove session state because a run ledger is invalid",
                    code="LEDGER_SCAN_INVALID",
                    details={"ledger": str(path), "error": str(exc)},
                ) from exc
            if path.resolve() != expected or owner_repo != candidate_repo:
                raise RuntimeProblem(
                    "run ledger is outside its recorded repository state root",
                    code="LEDGER_SCAN_INVALID",
                    details={
                        "ledger": str(path),
                        "expected": str(expected),
                        "repo_root": str(owner_repo),
                    },
                )
            if ledger.get("owner_session_id") == session_id and ledger.get("phase") in ACTIVE_PHASES:
                found.append((owner_repo, ledger))
    return found


def requires_test_effectiveness(ledger: dict[str, Any]) -> bool:
    plan = ledger.get("plan", {})
    version = plan.get("contract_schema_version")
    return (
        plan.get("level") != "L1"
        and type(version) is int
        and version >= PLAN_SCHEMA_VERSION
    )


def strong_test_proof_precedes_machine(ledger: dict[str, Any]) -> bool:
    if not requires_test_effectiveness(ledger):
        return False
    requirements = ledger.get("plan", {}).get("test_effectiveness_requirements")
    return bool(requirements) and all(
        isinstance(item, dict) and item.get("minimum") == "strong"
        for item in requirements
    )


def required_evidence_keys(ledger: dict[str, Any]) -> list[str]:
    if ledger["plan"].get("level") == "L1":
        return ["review", "doc_review"]
    keys = ["machine"]
    if requires_test_effectiveness(ledger):
        keys.append("test_effectiveness")
    return [*keys, "blackbox", "review", "doc_review"]


def required_evidence_fields(ledger: dict[str, Any]) -> list[str]:
    return [EVIDENCE_STATUS_FIELDS[key] for key in required_evidence_keys(ledger)]


def preparation_continuation_facts(
    repo: Path, ledger: Mapping[str, Any]
) -> dict[str, Any] | None:
    marker = ledger.get("plan", {}).get("verification_preparation")
    if not isinstance(marker, dict):
        return None
    final_head = ledger.get("final_head")
    try:
        target_head = branch_head(repo, str(ledger.get("target_branch")))
    except RuntimeProblem:
        target_head = None
    final_commit_exists = bool(
        isinstance(final_head, str)
        and re.fullmatch(r"[0-9a-f]{40}", final_head)
        and git(repo, "cat-file", "-e", f"{final_head}^{{commit}}", check=False).returncode
        == 0
    )
    facts = {
        "schema_version": 1,
        "preparation_run_id": ledger.get("run_id"),
        "owner_session_id": ledger.get("owner_session_id"),
        "business_run_id": marker.get("business_run_id"),
        "business_plan_sha256": marker.get("business_plan_sha256"),
        "problem_snapshot_sha256": marker.get("problem_snapshot_sha256"),
        "problem_ids": list(marker.get("problem_ids", [])),
        "support_paths": list(marker.get("support_paths", [])),
        "final_head": final_head,
        "preparation_final_head": final_head,
        "target_branch": ledger.get("target_branch"),
        "target_head": target_head,
        "ready": bool(
            ledger.get("phase") == "finalized"
            and final_commit_exists
            and final_head == target_head
        ),
    }
    facts["binding_sha256"] = canonical_json_sha256(facts)
    return facts


def continuation_ready_marker(
    ledger: Mapping[str, Any], continuation: Mapping[str, Any] | None
) -> str | None:
    if isinstance(continuation, Mapping) and continuation.get("ready") is True:
        return f"BUILDER_CONTINUATION_READY:{ledger['run_id']}"
    return None


def status_facts(repo: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    builder = Path(str(ledger["worktrees"]["builder"]["path"]))
    tester = Path(str(ledger["worktrees"]["tester"]["path"]))
    builder_head = full_head(builder) if builder.exists() else None
    tester_head = full_head(tester) if tester.exists() else None
    builder_dirty = worktree_residue(builder) if builder.exists() else []
    tester_dirty = worktree_residue(tester) if tester.exists() else []
    evidence = {
        EVIDENCE_STATUS_FIELDS[key]: evidence_head(ledger, key)
        for key in EVIDENCE_FIELDS
    }
    required_keys = required_evidence_keys(ledger)
    required = [EVIDENCE_STATUS_FIELDS[key] for key in required_keys]
    missing = [
        EVIDENCE_STATUS_FIELDS[key]
        for key in required_keys
        if evidence_head(ledger, key) is None
    ]
    stale = [
        EVIDENCE_STATUS_FIELDS[key]
        for key in required_keys
        if evidence_head(ledger, key) not in {None, builder_head}
    ]
    current_evidence = bool(builder_head) and not missing and not stale
    problem_facts = problem_inventory_facts(ledger)
    correction_progress = tester_correction_progress(ledger)
    plan = ledger.get("plan", {})
    interface_contract_version = plan.get(
        "interface_publication_contract_version",
        LEGACY_INTERFACE_PUBLICATION_CONTRACT_VERSION,
    )
    interface_input_paths = sorted(
        str(path) for path in plan.get("interface_input_paths", [])
    )
    tester_source = ledger["tester_integration"].get("source_head")
    prerequisite_publication = ledger.get("prerequisite_publication", {})
    prerequisites_ready = (
        prerequisite_publication.get("required") is not True
        or bool(prerequisite_publication.get("manifest_sha256"))
    )
    tester_fully_integrated = (
        tester_head == tester_source
        and not tester_dirty
        and ledger["tester_integration"].get("completed") is True
        and bool(ledger["tester_integration"].get("author_turn_id"))
    )
    if ledger["plan"].get("level") == "L1":
        tester_fully_integrated = not tester_dirty
    try:
        target_head = branch_head(repo, str(ledger["target_branch"]))
    except RuntimeProblem as exc:
        if exc.code != "INVALID_GIT_REF":
            raise
        target_head = None
    intent = ledger.get("finalize_intent")
    staged_final_head = (
        str(intent.get("final_head")) if isinstance(intent, dict) else None
    )
    expected_target_head = (
        ledger.get("final_head")
        if ledger.get("phase") in {"finalized_cleanup", "finalized"}
        else ledger["target_start_head"]
    )
    accepted_target_heads = {expected_target_head}
    if ledger.get("phase") == "active" and isinstance(intent, dict):
        accepted_target_heads.add(intent.get("final_head"))
    target_continuous = bool(target_head) and target_head in accepted_target_heads
    desired_target_head = (
        str(ledger.get("final_head") or "")
        if ledger.get("phase") in {"finalized_cleanup", "finalized"}
        else staged_final_head or builder_head
    )
    checkout_expected_head = (
        str(intent.get("expected_target_head"))
        if isinstance(intent, dict)
        else str(expected_target_head or "")
    )
    delivery_gates_ready = bool(
        ledger["phase"] == "active"
        and builder_head
        and not builder_dirty
        and prerequisites_ready
        and tester_fully_integrated
        and target_continuous
        and current_evidence
        and not problem_facts["missing_problem_sources"]
    )
    probe_desired_head = (
        desired_target_head
        if delivery_gates_ready or isinstance(intent, dict)
        else None
    )
    target_checkout = target_checkout_facts(
        repo,
        str(ledger["target_branch"]),
        expected_head=checkout_expected_head or None,
        desired_head=probe_desired_head or None,
        live_target_head=target_head,
        workspace_intake=ledger.get("workspace_intake"),
    )
    lifecycle_facts = lifecycle_delivery.delivery_facts(
        session_id=str(ledger["owner_session_id"]),
        repo_root=str(repo),
        run_id=str(ledger["run_id"]),
    )
    continuation = preparation_continuation_facts(repo, ledger)
    continuation_marker = continuation_ready_marker(ledger, continuation)
    return {
        "run_id": ledger["run_id"],
        "runtime_identity": ledger.get("runtime_identity"),
        "owner_session_id": ledger["owner_session_id"],
        "phase": ledger["phase"],
        "spec_head": ledger["spec_head"],
        "candidate_head": builder_head,
        "tester_head": tester_head,
        "target_branch": ledger["target_branch"],
        "target_start_head": ledger["target_start_head"],
        "target_head": target_head,
        "expected_target_head": expected_target_head,
        "target_continuous": target_continuous,
        "verification_attempts": verification_attempt_count(ledger),
        "max_iterations": ledger.get("loop_config", {}).get("max_iterations"),
        "interface_publication_contract_version": interface_contract_version,
        "interface_input_paths": interface_input_paths,
        "tester_correction_progress": correction_progress,
        "builder_dirty_paths": builder_dirty,
        "tester_dirty_paths": tester_dirty,
        "prerequisites_ready": prerequisites_ready,
        "prerequisite_publication": prerequisite_publication,
        "workspace_intake": ledger.get("workspace_intake"),
        "lifecycle_delivery": lifecycle_facts,
        "problem_inventory": problem_facts,
        "pending_problem_sources": [
            str(item.get("source_id"))
            for item in problem_facts["missing_problem_sources"]
        ],
        "problem_count": problem_facts["problem_count"],
        "inherited_problem_count": problem_facts["inherited_problem_count"],
        "problem_snapshot_sha256": problem_facts["snapshot_sha256"],
        "problem_ids": problem_facts["snapshot_problem_ids"],
        "legacy_problem_snapshot_required": bool(
            ledger.get("phase") == "abandoned"
            and problem_facts["snapshot_sha256"] is None
        ),
        "evidence_records": ledger.get("evidence"),
        "tester_fully_integrated": tester_fully_integrated,
        **evidence,
        "required_gates": required,
        "missing_gates": missing,
        "stale_gates": stale,
        "evidence_current": current_evidence,
        "delivery_gates_ready": delivery_gates_ready,
        "ready_to_stage_final": bool(delivery_gates_ready and not isinstance(intent, dict)),
        "staged_final_head": staged_final_head,
        "ready_to_finalize": bool(
            delivery_gates_ready and not target_checkout["finalize_blockers"]
        ),
        **target_checkout,
        "worktrees": {
            "builder": ledger["worktrees"]["builder"]["path"],
            "tester": ledger["worktrees"]["tester"]["path"],
        },
        "final_head": ledger.get("final_head"),
        "continuation": continuation,
        "marker": continuation_marker,
    }


def cmd_plan_validate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo = resolve_repo(args.repo)
    text, source, _, source_sha256 = read_plan_source(args.plan)
    contract = parse_plan(text, source, source_sha256=source_sha256)
    preflight = preflight_plan(
        repo,
        contract,
        target_branch=args.target_branch,
    )
    return {
        "status": "READY",
        "message": "plan contract and repository preflight are valid",
        "spec_head": preflight.spec_head,
        "target_branch": preflight.target_branch,
        "parallel_ready": contract.parallel_ready,
        "interface_publication_contract_version": (
            INTERFACE_PUBLICATION_CONTRACT_VERSION
        ),
        "interface_input_paths": list(contract.interface_input_paths),
        "contract_schema_version": contract.schema_version,
        "plan_sha256": contract.sha256,
        "plan_source_sha256": contract.source_sha256,
        "plan_digest_kind": contract.digest_kind,
        "blackbox_report_schema_version": BLACKBOX_REPORT_SCHEMA_VERSION,
        "e2e_case_ids": list(contract.e2e_case_ids),
        "e2e_cases_sha256": contract.e2e_cases_sha256,
        "effective_verification_source": preflight.effective_verification_source,
        "workspace_intake": list(preflight.workspace_intake),
        "verification_preparation": contract.verification_preparation,
        "continuation_from": contract.continuation_from,
        "continuation_from_run_id": (
            preflight.continuation_from.get("preparation_run_id")
            if isinstance(preflight.continuation_from, dict)
            else None
        ),
        "supersedes_run_id": contract.supersedes_run_id,
        **preflight.target_checkout,
        "contract": contract.as_dict(),
    }, EXIT_PASS


def cmd_plan_preflight(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo = resolve_repo(args.repo)
    target_branch = current_branch(repo)
    target_head = branch_head(repo, target_branch)
    requested_paths: list[str] = []
    path_blobs: dict[str, str] = {}
    for raw in args.path:
        path = normalize_allowed_path(str(raw), directory_hint=False)
        if any(mark in path for mark in "*?["):
            raise RuntimeProblem(
                "plan-preflight paths must be exact",
                result="NEEDS_USER",
                code="PREFLIGHT_PATH_INVALID",
                details={"path": raw},
                exit_code=EXIT_FAIL,
            )
        if path in requested_paths:
            raise RuntimeProblem(
                "plan-preflight paths must be unique",
                result="NEEDS_USER",
                code="VERIFICATION_PATH_DUPLICATE",
                details={"path": path},
                exit_code=EXIT_FAIL,
            )
        requested_paths.append(path)
        try:
            path_blobs[path] = exact_regular_blob(repo, target_head, path)
        except RuntimeProblem as exc:
            raise RuntimeProblem(
                "plan-preflight path must be an exact tracked regular file",
                result="NEEDS_USER",
                code="PREFLIGHT_PATH_INVALID",
                details={"path": path, "reason": exc.code},
                exit_code=EXIT_FAIL,
            ) from exc

    commands: list[dict[str, Any]] = []
    verification_source = "none"
    loop_exists = git(
        repo, "cat-file", "-e", f"{target_head}:.claude/loop.yml", check=False
    ).returncode == 0
    if loop_exists:
        commands, _config_sha, _max_iterations = load_loop_config(repo, target_head)
        verification_source = ".claude/loop.yml"
    machine_paths = sorted(
        set(PROTECTED_RUNTIME_PATHS) | set(verification_protected_paths(commands))
    )
    abandoned: dict[str, Any] | None = None
    old_support_paths: list[str] = []
    problem_snapshot: dict[str, Any] | None = None
    if args.run:
        linked_repo, _linked_run_id, abandoned = resolve_run_selector(repo, args.run)
        if linked_repo != repo:
            raise RuntimeProblem(
                "preflight run belongs to another repository",
                result="NEEDS_USER",
                code="PREFLIGHT_RUN_REPOSITORY_MISMATCH",
                details={"run_id": abandoned.get("run_id"), "repo_root": str(linked_repo)},
                exit_code=EXIT_FAIL,
            )
        problem_snapshot = abandoned.get("problem_inventory", {}).get("snapshot")
        live_target = branch_head(repo, str(abandoned.get("target_branch")))
        if abandoned.get("phase") != "abandoned":
            raise RuntimeProblem(
                "plan-preflight requires an abandoned business run",
                result="NEEDS_USER",
                code="PREFLIGHT_RUN_NOT_ABANDONED",
                details={"run_id": abandoned.get("run_id"), "phase": abandoned.get("phase")},
                exit_code=EXIT_FAIL,
            )
        if (
            abandoned.get("target_branch") != target_branch
            or live_target != target_head
            or target_head != abandoned.get("target_start_head")
        ):
            raise RuntimeProblem(
                "target branch drifted after the abandoned business snapshot",
                result="NEEDS_USER",
                code="PREFLIGHT_TARGET_DRIFT",
                details={
                    "run_id": args.run,
                    "phase": abandoned.get("phase"),
                    "recorded_target_branch": abandoned.get("target_branch"),
                    "recorded_target_head": abandoned.get("target_start_head"),
                    "target_branch": target_branch,
                    "target_head": target_head,
                    "problem_snapshot_sha256": (
                        problem_snapshot.get("sha256")
                        if isinstance(problem_snapshot, dict)
                        else None
                    ),
                },
                exit_code=EXIT_FAIL,
            )
        if not isinstance(problem_snapshot, dict) or not problem_snapshot.get("problem_ids"):
            raise RuntimeProblem(
                "abandoned business run lacks a sealed non-empty problem snapshot",
                result="NEEDS_USER",
                code="PREFLIGHT_PROBLEM_SNAPSHOT_REQUIRED",
                details={"run_id": abandoned.get("run_id")},
                exit_code=EXIT_FAIL,
            )
        old_support_paths = list(abandoned.get("plan", {}).get("support_paths", []))
        machine_paths = sorted(
            set(machine_paths)
            | set(abandoned.get("loop_config", {}).get("runner_paths", []))
        )

    machine_overlap = sorted(
        path
        for path in requested_paths
        if path in PROTECTED_RUNTIME_PATHS or path_allowed(path, machine_paths)
    )

    eligible = sorted(
        path
        for path in requested_paths
        if not path_allowed(path, machine_paths)
        and path not in PROTECTED_RUNTIME_PATHS
        and path_allowed(path, old_support_paths)
    )
    base = {
        "message": "verification write paths were classified without changing repository state",
        "repo_root": str(repo),
        "target_branch": target_branch,
        "target_head": target_head,
        "effective_verification_source": verification_source,
        "requested_paths": requested_paths,
        "paths": requested_paths,
        "path_blobs": path_blobs,
        "machine_runner_control_paths": machine_paths,
        "business_run_id": abandoned.get("run_id") if abandoned else None,
        "business_plan_sha256": (
            abandoned.get("plan", {}).get("sha256") if abandoned else None
        ),
        "problem_snapshot_sha256": (
            problem_snapshot.get("sha256") if isinstance(problem_snapshot, dict) else None
        ),
        "problem_ids": (
            list(problem_snapshot.get("problem_ids", []))
            if isinstance(problem_snapshot, dict)
            else []
        ),
        "old_support_paths": old_support_paths,
        "eligible_support_paths": eligible,
    }
    base["binding_sha256"] = canonical_json_sha256(base)
    if machine_overlap:
        return {
            "status": "NEEDS_USER",
            "code": "VERIFICATION_BOOTSTRAP_REQUIRED",
            "message": "requested writes overlap the current machine runner or control source",
            "bootstrap_paths": machine_overlap,
            **{key: value for key, value in base.items() if key != "message"},
        }, EXIT_FAIL
    if eligible:
        verification_preparation = {
            "business_run_id": abandoned.get("run_id") if abandoned else None,
            "business_plan_sha256": (
                abandoned.get("plan", {}).get("sha256") if abandoned else None
            ),
            "problem_snapshot_sha256": (
                problem_snapshot.get("sha256") if isinstance(problem_snapshot, dict) else None
            ),
            "problem_ids": (
                list(problem_snapshot.get("problem_ids", []))
                if isinstance(problem_snapshot, dict)
                else []
            ),
            "support_paths": eligible,
            "repo_root": str(repo),
            "target_branch": target_branch,
        }
        return {
            "status": "NEEDS_USER",
            "code": "VERIFICATION_PREPARATION_REQUIRED",
            "verification_preparation": verification_preparation,
            **base,
        }, EXIT_FAIL
    return {"status": "READY", **base}, EXIT_PASS


def cmd_workspace_scan(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo = resolve_repo(args.repo)
    try:
        entries = workspace_contract.scan_paths(repo, args.path, require_dirty=True)
    except workspace_contract.WorkspaceError as exc:
        raise RuntimeProblem(
            str(exc),
            result="NEEDS_USER",
            code=exc.code,
            details=exc.details,
            exit_code=EXIT_FAIL,
        ) from exc
    return {
        "status": "READY",
        "message": "workspace intake paths were scanned without changing the checkout",
        "repo": str(repo),
        "entries": entries,
    }, EXIT_PASS


def generated_run_id(task: str, plan_sha: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task.strip()).strip("-._").lower()
    if not slug:
        slug = "run"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = plan_sha[:8]
    available = 63 - len(stamp) - len(suffix) - 2
    return ensure_run_id(f"{slug[:available]}-{stamp}-{suffix}")


def validate_supersession(repo: Path, contract: PlanContract) -> dict[str, Any] | None:
    if contract.plan_revision == 1:
        return None
    run_id = contract.supersedes_run_id
    expected_sha = contract.supersedes_plan_sha256
    if run_id is None or expected_sha is None:
        raise RuntimeProblem(
            "revised plan is missing its superseded run identity",
            result="NEEDS_USER",
            code="PLAN_SUPERSESSION_INVALID",
            exit_code=EXIT_FAIL,
        )
    matches = [
        (candidate_repo, ledger_path(candidate_repo, run_id))
        for candidate_repo in repository_worktrees(repo)
        if ledger_path(candidate_repo, run_id).is_file()
    ]
    if len(matches) != 1:
        raise RuntimeProblem(
            "superseded run must resolve uniquely in this repository",
            result="NEEDS_USER",
            code="PLAN_SUPERSESSION_INVALID",
            details={"run_id": run_id, "ledgers": [str(path) for _repo, path in matches]},
            exit_code=EXIT_FAIL,
        )
    owner_repo, path = matches[0]
    previous = read_json(path)
    previous_revision = previous.get("plan", {}).get("plan_revision")
    valid = (
        resolve_repo(str(previous.get("repo_root", ""))) == owner_repo
        and previous.get("phase") == "abandoned"
        and previous.get("plan", {}).get("sha256") == expected_sha
        and type(previous_revision) is int
        and contract.plan_revision is not None
        and contract.plan_revision > previous_revision
    )
    if not valid:
        raise RuntimeProblem(
            "revised plan does not supersede the recorded abandoned contract",
            result="NEEDS_USER",
            code="PLAN_SUPERSESSION_INVALID",
            details={
                "run_id": run_id,
                "ledger": str(path),
                "phase": previous.get("phase"),
                "recorded_plan_sha256": previous.get("plan", {}).get("sha256"),
                "recorded_plan_revision": previous_revision,
                "requested_plan_revision": contract.plan_revision,
            },
            exit_code=EXIT_FAIL,
        )
    inventory = previous.get("problem_inventory", {})
    snapshot = inventory.get("snapshot") if isinstance(inventory, dict) else None
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sha256"), str):
        raise RuntimeProblem(
            "旧一轮没有问题清单，必须先补录后才能创建新一轮",
            result="NEEDS_USER",
            code="LEGACY_PROBLEM_SNAPSHOT_REQUIRED",
            details={"run_id": run_id, "ledger": str(path)},
            exit_code=EXIT_FAIL,
        )
    if contract.prior_problem_snapshot_sha256 != snapshot.get("sha256"):
        raise RuntimeProblem(
            "新一轮引用的问题清单与老一轮不一致",
            result="NEEDS_USER",
            code="PRIOR_PROBLEM_SNAPSHOT_MISMATCH",
            details={
                "run_id": run_id,
                "recorded_snapshot_sha256": snapshot.get("sha256"),
                "requested_snapshot_sha256": contract.prior_problem_snapshot_sha256,
            },
            exit_code=EXIT_FAIL,
        )
    recorded_ids = {
        str(item.get("problem_id"))
        for item in snapshot.get("problems", [])
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    requested_ids = {str(item.get("problem_id")) for item in contract.prior_problem_items}
    if recorded_ids != requested_ids:
        raise RuntimeProblem(
            "新一轮没有逐条处理老一轮的全部问题",
            result="NEEDS_USER",
            code="PRIOR_PROBLEM_SET_MISMATCH",
            details={
                "missing_problem_ids": sorted(recorded_ids - requested_ids),
                "unknown_problem_ids": sorted(requested_ids - recorded_ids),
            },
            exit_code=EXIT_FAIL,
        )
    return previous


def load_unique_run_ledger(repo: Path, run_id: str, *, purpose: str) -> dict[str, Any]:
    matches = [
        (candidate_repo, ledger_path(candidate_repo, run_id))
        for candidate_repo in repository_worktrees(repo)
        if ledger_path(candidate_repo, run_id).is_file()
    ]
    if len(matches) != 1:
        raise RuntimeProblem(
            f"{purpose} run must resolve uniquely in this repository",
            result="NEEDS_USER",
            code="PLAN_LINKED_RUN_INVALID",
            details={"run_id": run_id, "ledgers": [str(path) for _repo, path in matches]},
            exit_code=EXIT_FAIL,
        )
    owner_repo, path = matches[0]
    ledger = read_json(path)
    if resolve_repo(str(ledger.get("repo_root", ""))) != owner_repo:
        raise RuntimeProblem(
            f"{purpose} run repository identity is invalid",
            result="NEEDS_USER",
            code="PLAN_LINKED_RUN_INVALID",
            details={"run_id": run_id, "ledger": str(path)},
            exit_code=EXIT_FAIL,
        )
    return ledger


def exact_regular_blob(repo: Path, head: str, path: str) -> str:
    if any(mark in path for mark in "*?["):
        raise RuntimeProblem(
            "linked verification paths must be exact repository paths",
            result="NEEDS_USER",
            code="VERIFICATION_PATH_NOT_EXACT",
            details={"path": path},
            exit_code=EXIT_FAIL,
        )
    entry = git(repo, "ls-tree", head, "--", path, check=False)
    fields = entry.stdout.strip().split()
    if entry.returncode != 0 or len(fields) < 3 or fields[0] not in {"100644", "100755"}:
        raise RuntimeProblem(
            "linked verification path must be a regular file at spec_head",
            result="NEEDS_USER",
            code="VERIFICATION_PATH_NOT_REGULAR",
            details={"path": path, "spec_head": head},
            exit_code=EXIT_FAIL,
        )
    return fields[2]


def validate_verification_preparation(
    repo: Path,
    contract: PlanContract,
    *,
    target_branch: str,
    spec_head: str,
    runner_paths: Sequence[str],
) -> dict[str, Any] | None:
    marker = contract.verification_preparation
    if marker is None:
        return None
    business = load_unique_run_ledger(
        repo, str(marker["business_run_id"]), purpose="abandoned business"
    )
    snapshot = business.get("problem_inventory", {}).get("snapshot")
    recorded_problem_ids = (
        set(str(item) for item in snapshot.get("problem_ids", []))
        if isinstance(snapshot, dict)
        else set()
    )
    requested_problem_ids = set(str(item) for item in marker["problem_ids"])
    valid = (
        business.get("phase") == "abandoned"
        and business.get("plan", {}).get("sha256") == marker["business_plan_sha256"]
        and isinstance(snapshot, dict)
        and snapshot.get("sha256") == marker["problem_snapshot_sha256"]
        and bool(requested_problem_ids)
        and requested_problem_ids <= recorded_problem_ids
        and business.get("target_branch") == target_branch
    )
    if not valid:
        raise RuntimeProblem(
            "verification preparation marker does not match the abandoned business run",
            result="NEEDS_USER",
            code="VERIFICATION_PREPARATION_LINK_INVALID",
            details={
                "business_run_id": marker["business_run_id"],
                "phase": business.get("phase"),
                "recorded_plan_sha256": business.get("plan", {}).get("sha256"),
                "recorded_problem_snapshot_sha256": (
                    snapshot.get("sha256") if isinstance(snapshot, dict) else None
                ),
                "recorded_problem_ids": sorted(recorded_problem_ids),
                "target_branch": business.get("target_branch"),
            },
            exit_code=EXIT_FAIL,
        )
    old_support = list(business.get("plan", {}).get("support_paths", []))
    current_protected = set(PROTECTED_RUNTIME_PATHS) | set(runner_paths)
    current_support = list(contract.support_paths)
    path_blobs: dict[str, str] = {}
    for path in marker["support_paths"]:
        if not path_allowed(path, old_support):
            raise RuntimeProblem(
                "verification preparation path was not protected by the abandoned run",
                result="NEEDS_USER",
                code="VERIFICATION_PREPARATION_PATH_INELIGIBLE",
                details={"path": path, "old_support_paths": old_support},
                exit_code=EXIT_FAIL,
            )
        if not path_allowed(path, contract.builder_write):
            raise RuntimeProblem(
                "verification preparation path is outside Builder ownership",
                result="NEEDS_USER",
                code="VERIFICATION_PREPARATION_PATH_INELIGIBLE",
                details={"path": path},
                exit_code=EXIT_FAIL,
            )
        if path in current_protected or path_allowed(path, current_support):
            raise RuntimeProblem(
                "current machine runner or support paths require an external bootstrap",
                result="NEEDS_USER",
                code="VERIFICATION_BOOTSTRAP_REQUIRED",
                details={"path": path, "protected_paths": sorted(current_protected)},
                exit_code=EXIT_FAIL,
            )
        path_blobs[path] = exact_regular_blob(repo, spec_head, path)
    return {
        **marker,
        "path_blobs": path_blobs,
        "business_owner_session_id": business.get("owner_session_id"),
    }


def validate_business_continuation(
    repo: Path,
    contract: PlanContract,
    *,
    target_branch: str,
    spec_head: str,
    previous: dict[str, Any] | None,
) -> dict[str, Any] | None:
    marker = contract.continuation_from
    if marker is None:
        return None
    preparation = load_unique_run_ledger(
        repo, str(marker["preparation_run_id"]), purpose="verification preparation"
    )
    preparation_link = preparation.get("plan", {}).get("verification_preparation")
    final_head = preparation.get("final_head")
    valid = (
        preparation.get("phase") == "finalized"
        and isinstance(preparation_link, dict)
        and final_head == spec_head
        and preparation.get("target_branch") == target_branch
        and isinstance(previous, dict)
        and contract.supersedes_run_id == preparation_link.get("business_run_id")
        and contract.supersedes_plan_sha256
        == preparation_link.get("business_plan_sha256")
        and contract.prior_problem_snapshot_sha256
        == preparation_link.get("problem_snapshot_sha256")
    )
    if not valid:
        raise RuntimeProblem(
            "business continuation does not match a finalized preparation run",
            result="NEEDS_USER",
            code="BUSINESS_CONTINUATION_INVALID",
            details={
                "preparation_run_id": marker["preparation_run_id"],
                "phase": preparation.get("phase"),
                "preparation_final_head": final_head,
                "requested_spec_head": spec_head,
                "target_branch": preparation.get("target_branch"),
            },
            exit_code=EXIT_FAIL,
        )
    decisions = {
        str(item.get("problem_id")): item
        for item in contract.prior_problem_items
        if isinstance(item, dict)
    }
    invalid_problem_ids = [
        problem_id
        for problem_id in preparation_link.get("problem_ids", [])
        if decisions.get(str(problem_id), {}).get("handling") != "handled_elsewhere"
        or str(final_head) not in str(decisions.get(str(problem_id), {}).get("reference", ""))
    ]
    if invalid_problem_ids:
        raise RuntimeProblem(
            "prepared problems must be handled_elsewhere at the preparation final commit",
            result="NEEDS_USER",
            code="BUSINESS_CONTINUATION_PROBLEM_MAPPING_INVALID",
            details={
                "problem_ids": invalid_problem_ids,
                "preparation_final_head": final_head,
            },
            exit_code=EXIT_FAIL,
        )
    replayed_by: list[str] = []
    for candidate_repo in repository_worktrees(repo):
        runs_root = state_root(candidate_repo) / "runs"
        if not runs_root.is_dir():
            continue
        for path in runs_root.glob("*/ledger.json"):
            with contextlib.suppress(RuntimeProblem):
                candidate = read_json(path)
                linked = candidate.get("plan", {}).get("continuation_from")
                if (
                    isinstance(linked, dict)
                    and linked.get("preparation_run_id") == marker["preparation_run_id"]
                ):
                    replayed_by.append(str(candidate.get("run_id")))
    if replayed_by:
        raise RuntimeProblem(
            "continuation marker was already consumed by another run",
            result="NEEDS_USER",
            code="BUSINESS_CONTINUATION_REPLAYED",
            details={"preparation_run_id": marker["preparation_run_id"], "runs": sorted(set(replayed_by))},
            exit_code=EXIT_FAIL,
        )
    return {
        "preparation_run_id": marker["preparation_run_id"],
        "preparation_owner_session_id": preparation.get("owner_session_id"),
        "preparation_final_head": final_head,
        "business_run_id": preparation_link["business_run_id"],
    }


def preflight_plan(
    repo: Path,
    contract: PlanContract,
    *,
    target_branch: str | None = None,
    explicit_spec_head: str | None = None,
) -> PlanPreflight:
    validate_interface_publication_contract(contract)
    previous = validate_supersession(repo, contract)
    contract_spec_head = contract.spec_head or full_head(repo, "HEAD")
    if explicit_spec_head:
        resolved_explicit_head = full_head(repo, explicit_spec_head)
        if resolved_explicit_head != contract_spec_head:
            raise RuntimeProblem(
                "explicit --spec-head does not match plan spec_head",
                result="NEEDS_USER",
                code="SPEC_HEAD_MISMATCH",
                details={
                    "plan_spec_head": contract_spec_head,
                    "explicit_spec_head": resolved_explicit_head,
                },
                exit_code=EXIT_FAIL,
            )
    spec_head = full_head(repo, contract_spec_head)
    resolved_target_branch = target_branch or current_branch(repo)
    target_start_head = branch_head(repo, resolved_target_branch)
    if target_start_head != spec_head:
        raise RuntimeProblem(
            "plan spec_head is stale relative to target branch",
            result="NEEDS_USER",
            code="TARGET_SPEC_MISMATCH",
            details={"target_head": target_start_head, "spec_head": spec_head},
            exit_code=EXIT_FAIL,
        )
    commands, config_sha256, verification_source, max_iterations = (
        load_verification_commands(repo, spec_head, contract)
    )
    try:
        validate_runner_ownership(contract, commands)
        validate_runner_dependencies_at_spec_head(repo, spec_head, commands)
    except RuntimeProblem as exc:
        exc.details.setdefault(
            "effective_verification_source", verification_source
        )
        raise
    workspace_entries: list[dict[str, Any]] = []
    if contract.workspace_intake:
        target = worktree_for_branch(repo, resolved_target_branch)
        if target is None:
            raise RuntimeProblem(
                "workspace intake requires a checked-out target branch",
                result="NEEDS_USER",
                code="WORKSPACE_TARGET_NOT_CHECKED_OUT",
                details={"target_branch": resolved_target_branch},
                exit_code=EXIT_FAIL,
            )
        try:
            workspace_entries = workspace_contract.scan_paths(
                target,
                [item["path"] for item in contract.workspace_intake],
                require_dirty=True,
            )
        except workspace_contract.WorkspaceError as exc:
            raise RuntimeProblem(
                str(exc),
                result="NEEDS_USER",
                code=exc.code,
                details=exc.details,
                exit_code=EXIT_FAIL,
            ) from exc
        expected = {item["path"]: item["state_sha256"] for item in contract.workspace_intake}
        drift = [
            {
                "path": item["path"],
                "expected_state_sha256": expected[item["path"]],
                "actual_state_sha256": item["state_sha256"],
            }
            for item in workspace_entries
            if expected.get(item["path"]) != item["state_sha256"]
        ]
        if drift:
            raise RuntimeProblem(
                "workspace intake changed after planning",
                result="NEEDS_USER",
                code="WORKSPACE_INTAKE_DRIFT",
                details={"drift": drift},
                exit_code=EXIT_FAIL,
            )
    runner_paths = tuple(verification_protected_paths(commands))
    verification_preparation = validate_verification_preparation(
        repo,
        contract,
        target_branch=resolved_target_branch,
        spec_head=spec_head,
        runner_paths=runner_paths,
    )
    continuation_from = validate_business_continuation(
        repo,
        contract,
        target_branch=resolved_target_branch,
        spec_head=spec_head,
        previous=previous,
    )
    return PlanPreflight(
        spec_head=spec_head,
        target_branch=resolved_target_branch,
        target_start_head=target_start_head,
        target_checkout=target_checkout_facts(
            repo,
            resolved_target_branch,
            live_target_head=target_start_head,
        ),
        commands=tuple(commands),
        loop_config_sha256=config_sha256,
        effective_verification_source=verification_source,
        max_iterations=max_iterations,
        runner_paths=runner_paths,
        workspace_intake=tuple(workspace_entries),
        prior_problem_snapshot=(
            previous.get("problem_inventory", {}).get("snapshot")
            if isinstance(previous, dict)
            else None
        ),
        verification_preparation=verification_preparation,
        continuation_from=continuation_from,
    )


def cmd_start(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo = resolve_repo(args.repo)
    with locked_repository_state(repo):
        return cmd_start_locked(args, repo)


def cmd_start_locked(args: argparse.Namespace, repo: Path) -> tuple[dict[str, Any], int]:
    if not args.run and not args.task:
        raise RuntimeProblem(
            "start requires --run ID or --task TEXT",
            code="RUN_OR_TASK_REQUIRED",
        )
    source_plan_path = (
        Path(args.plan).expanduser().resolve()
        if args.plan
        else (state_root(repo) / "inbox" / f"{args.run or 'plan'}.md").resolve()
    )
    plan_text, source_contract = load_plan_file(source_plan_path)
    run_id = ensure_run_id(args.run) if args.run else generated_run_id(args.task or "run", source_contract.sha256)
    session_id = str(args.session_id).strip()
    if not session_id:
        raise RuntimeProblem(
            "start requires a non-empty --session-id",
            code="SESSION_ID_REQUIRED",
        )
    try:
        routed = lifecycle_delivery.load_route(session_id)
    except lifecycle_delivery.LifecycleDeliveryError as exc:
        raise RuntimeProblem(str(exc), code=exc.code) from exc
    if routed is not None:
        routed_ledger_path = Path(str(routed["ledger_path"]))
        routed_active = False
        if routed_ledger_path.is_file() and not routed_ledger_path.is_symlink():
            try:
                routed_ledger = read_json(routed_ledger_path)
                routed_active = bool(
                    routed_ledger.get("owner_session_id") == session_id
                    and routed_ledger.get("run_id") == routed.get("run_id")
                    and routed_ledger.get("phase") in ACTIVE_PHASES
                )
            except RuntimeProblem:
                routed_active = False
        if routed_active:
            raise RuntimeProblem(
                "session already owns an active run",
                code="SESSION_ALREADY_ACTIVE",
                details={
                    "run_ids": [routed["run_id"]],
                    "repo_root": routed["repo_root"],
                },
            )
        if not lifecycle_delivery.remove_route(session_id, require_empty=True):
            raise RuntimeProblem(
                "session still owns undrained lifecycle delivery intent",
                result="NEEDS_USER",
                code="LIFECYCLE_DELIVERY_PENDING",
                details={
                    "run_ids": [routed["run_id"]],
                    "repo_root": routed["repo_root"],
                },
                exit_code=EXIT_FAIL,
            )
    existing_ledgers = [
        ledger_path(candidate_repo, run_id)
        for candidate_repo in repository_worktrees(repo)
        if ledger_path(candidate_repo, run_id).exists()
    ]
    if existing_ledgers:
        raise RuntimeProblem(
            f"run already exists: {run_id}",
            code="RUN_ALREADY_EXISTS",
            details={"ledgers": [str(path) for path in existing_ledgers]},
        )
    same_session = active_ledgers_for_session(repo, session_id)
    if same_session:
        raise RuntimeProblem(
            "session already owns an active run",
            code="SESSION_ALREADY_ACTIVE",
            details={"run_ids": [item[1]["run_id"] for item in same_session]},
        )
    preflight = preflight_plan(
        repo,
        source_contract,
        target_branch=args.target_branch,
        explicit_spec_head=args.spec_head,
    )
    spec_head = preflight.spec_head
    target_branch = preflight.target_branch
    target_start_head = preflight.target_start_head
    commands = list(preflight.commands)
    loop_config_sha256 = preflight.loop_config_sha256
    loop_config_source = preflight.effective_verification_source
    max_iterations = preflight.max_iterations
    runner_paths = list(preflight.runner_paths)
    linked_owner_session_id = None
    if preflight.continuation_from is not None:
        linked_owner_session_id = preflight.continuation_from.get(
            "preparation_owner_session_id"
        )
    if linked_owner_session_id is not None and linked_owner_session_id != session_id:
        raise RuntimeProblem(
            "linked verification continuation must stay in the owning session",
            result="NEEDS_USER",
            code="CONTINUATION_SESSION_MISMATCH",
            details={
                "owner_session_id": linked_owner_session_id,
                "requested_session_id": session_id,
            },
            exit_code=EXIT_FAIL,
        )
    add_info_exclude(repo)

    snapshot_head = spec_head
    snapshot_tree = git(repo, "rev-parse", f"{spec_head}^{{tree}}", check=True).stdout.strip()
    if preflight.workspace_intake:
        target_checkout = worktree_for_branch(repo, target_branch)
        if target_checkout is None:
            raise RuntimeProblem(
                "workspace intake target checkout disappeared before start",
                code="WORKSPACE_TARGET_NOT_CHECKED_OUT",
            )
        try:
            snapshot_head, snapshot_tree = workspace_contract.create_snapshot_commit(
                target_checkout,
                spec_head,
                preflight.workspace_intake,
                message=f"chore(codex-loop): workspace intake {run_id}",
            )
            after_snapshot = workspace_contract.scan_paths(
                target_checkout,
                [str(item["path"]) for item in preflight.workspace_intake],
                require_dirty=True,
            )
        except workspace_contract.WorkspaceError as exc:
            raise RuntimeProblem(str(exc), code=exc.code, details=exc.details) from exc
        before_digests = {
            str(item["path"]): str(item["state_sha256"])
            for item in preflight.workspace_intake
        }
        after_digests = {
            str(item["path"]): str(item["state_sha256"])
            for item in after_snapshot
        }
        if after_digests != before_digests:
            raise RuntimeProblem(
                "workspace intake changed while creating its immutable snapshot",
                result="NEEDS_USER",
                code="WORKSPACE_INTAKE_DRIFT",
                details={"before": before_digests, "after": after_digests},
                exit_code=EXIT_FAIL,
            )

    root = state_root(repo)
    current_run_dir = run_dir(repo, run_id)
    current_run_dir.mkdir(parents=True, exist_ok=False)
    plan_path = current_run_dir / "plan.md"
    plan_path.write_text(plan_text, encoding="utf-8")
    _, contract = load_plan_file(plan_path)
    if contract.sha256 != source_contract.sha256:
        shutil.rmtree(current_run_dir, ignore_errors=True)
        raise RuntimeProblem(
            "frozen plan copy hash does not match source",
            code="PLAN_COPY_MISMATCH",
        )
    worktree_root = root / "worktrees" / run_id
    builder_path = worktree_root / "builder"
    tester_path = worktree_root / "tester"
    builder_branch = f"codex-loop/{run_id}/builder"
    tester_branch = f"codex-loop/{run_id}/tester"
    created: list[tuple[Path, str]] = []
    try:
        for path, branch, base in (
            (builder_path, builder_branch, snapshot_head),
            (tester_path, tester_branch, spec_head),
        ):
            if path.exists():
                raise RuntimeProblem(
                    f"worktree path already exists: {path}",
                    code="WORKTREE_PATH_EXISTS",
                )
            result = git(repo, "worktree", "add", "-b", branch, str(path), base, check=False)
            if result.returncode != 0:
                raise RuntimeProblem(
                    f"cannot create {branch} worktree",
                    code="WORKTREE_CREATE_FAILED",
                    details={"stdout": tail_text(result.stdout), "stderr": tail_text(result.stderr)},
                )
            created.append((path, branch))
    except Exception:
        for path, branch in reversed(created):
            git(repo, "worktree", "remove", "--force", str(path), check=False)
            git(repo, "branch", "-D", branch, check=False)
        shutil.rmtree(current_run_dir, ignore_errors=True)
        raise

    now = utc_now()
    prior_snapshot = preflight.prior_problem_snapshot
    prior_decisions = [dict(item) for item in contract.prior_problem_items]
    prior_problem_by_id = {
        str(item.get("problem_id")): dict(item)
        for item in (
            prior_snapshot.get("problems", [])
            if isinstance(prior_snapshot, dict)
            else []
        )
        if isinstance(item, dict) and isinstance(item.get("problem_id"), str)
    }
    inherited_problems = [
        prior_problem_by_id[str(item["problem_id"])]
        for item in prior_decisions
        if item.get("handling") == "include"
    ]
    ledger: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "runtime_identity": capture_runtime_identity(),
        "run_id": run_id,
        "owner_session_id": session_id,
        "phase": "active",
        "repo_root": str(repo),
        "spec_head": spec_head,
        "target_branch": target_branch,
        "target_start_head": target_start_head,
        "plan": {
            "path": str(plan_path),
            "source_path": str(source_plan_path),
            "sha256": contract.sha256,
            "source_sha256": source_contract.source_sha256,
            "frozen_sha256": contract.source_sha256,
            "digest_kind": contract.digest_kind,
            "blackbox_report_schema_version": BLACKBOX_REPORT_SCHEMA_VERSION,
            "contract_schema_version": contract.schema_version,
            "level": contract.level,
            "spec_head": contract.spec_head,
            "plan_revision": contract.plan_revision,
            "revision": contract.plan_revision,
            "parallel_ready": contract.parallel_ready,
            "interfaces": list(contract.interfaces),
            "interface_publication_contract_version": INTERFACE_PUBLICATION_CONTRACT_VERSION,
            "interface_input_paths": list(contract.interface_input_paths),
            "target_test_dirs": list(contract.target_test_dirs),
            "support_paths": list(contract.support_paths),
            "public_prerequisites": list(contract.public_prerequisites),
            "runner": contract.runner,
            "builder_write": list(contract.builder_write),
            "tester_write": list(contract.tester_write),
            "behavior_ids": list(contract.behavior_ids),
            "supersedes_run_id": contract.supersedes_run_id,
            "supersedes_plan_sha256": contract.supersedes_plan_sha256,
            "prior_problem_snapshot_sha256": contract.prior_problem_snapshot_sha256,
            "prior_problem_items": prior_decisions,
            "verification_preparation": (
                dict(contract.verification_preparation)
                if contract.verification_preparation is not None
                else None
            ),
            "continuation_from": (
                dict(contract.continuation_from)
                if contract.continuation_from is not None
                else None
            ),
            "has_e2e_cases": contract.has_e2e_cases,
            "e2e_case_ids": list(contract.e2e_case_ids),
            "e2e_cases_sha256": contract.e2e_cases_sha256,
            "test_effectiveness_requirements": [
                dict(item) for item in contract.test_effectiveness_requirements
            ],
            "workspace_intake": [dict(item) for item in contract.workspace_intake],
            "evidence_scopes": {
                key: {
                    "affects": sorted(
                        set(value.get("affects", ()))
                        | (set(runner_paths) if key == "machine" else set())
                    ),
                    "exempt": list(value.get("exempt", ())),
                }
                for key, value in contract.evidence_scopes.items()
            },
        },
        "loop_config": {
            "path": loop_config_source,
            "spec_sha256": loop_config_sha256,
            "stages": [item["stage"] for item in commands],
            "runner_paths": runner_paths,
            "max_iterations": max_iterations,
        },
        "verification": {"attempts": [], "resumes": []},
        "worktrees": {
            "builder": {"path": str(builder_path), "branch": builder_branch},
            "tester": {"path": str(tester_path), "branch": tester_branch},
        },
        "branches": {
            "builder": builder_branch,
            "tester": tester_branch,
        },
        "prerequisite_publication": {
            "required": contract.level != "L1" and not contract.parallel_ready,
            "builder_head": None,
            "head": None,
            "tree": None,
            "manifest_sha256": None,
            "paths": list(contract.public_prerequisites),
            "files": {},
        },
        "tester_integration": {
            "base_head": spec_head,
            "source_head": spec_head,
            "owned_paths": [],
            "ownership_baseline_head": None,
            "pending": None,
            "author_agent_id": None,
            "author_turn_id": None,
            "author_head": None,
            "author_prerequisite_manifest_sha256": None,
            "completed": False,
        },
        "agents": {
            "tester": None,
            "reviewer": None,
        },
        "pending_agent_turns": {
            "tester": None,
            "reviewer": None,
        },
        "completed_agent_turns": {
            "tester": [],
            "reviewer": [],
        },
        "evidence": {
            "machine": None,
            "test_effectiveness": None,
            "blackbox": None,
            "review": None,
            "doc_review": None,
        },
        "workspace_intake": {
            "required": bool(preflight.workspace_intake),
            "paths": [str(item["path"]) for item in preflight.workspace_intake],
            "entries": [dict(item) for item in preflight.workspace_intake],
            "snapshot_head": snapshot_head if preflight.workspace_intake else None,
            "snapshot_tree": snapshot_tree if preflight.workspace_intake else None,
        },
        "problem_inventory": {
            "schema_version": PROBLEM_REPORT_SCHEMA_VERSION,
            "items": inherited_problems,
            "sources": [],
            "inherited_from": (
                {
                    "run_id": contract.supersedes_run_id,
                    "snapshot_sha256": contract.prior_problem_snapshot_sha256,
                    "decisions": prior_decisions,
                }
                if contract.plan_revision and contract.plan_revision > 1
                else None
            ),
            "snapshot": None,
        },
        "finalize_intent": None,
        "final_head": None,
        "created_at": now,
        "updated_at": now,
        "events": [],
    }
    append_event(
        ledger,
        "run_started",
        {
            "owner_session_id": session_id,
            "spec_head": spec_head,
            "builder_head": full_head(builder_path),
            "tester_head": full_head(tester_path),
            "workspace_intake_snapshot_head": (
                snapshot_head if preflight.workspace_intake else None
            ),
        },
    )
    save_ledger(repo, ledger)
    try:
        route = lifecycle_delivery.register_route(
            session_id=session_id,
            repo_root=str(repo),
            run_id=run_id,
            ledger_path=str(ledger_path(repo, run_id)),
            tester_start_attestation=(
                initial_tester_start_attestation(ledger)
                if contract.level != "L1"
                else None
            ),
        )
    except lifecycle_delivery.LifecycleDeliveryError as exc:
        for path, branch in reversed(created):
            git(repo, "worktree", "remove", "--force", str(path), check=False)
            git(repo, "branch", "-D", branch, check=False)
        shutil.rmtree(current_run_dir, ignore_errors=True)
        raise RuntimeProblem(str(exc), code=exc.code) from exc
    return {
        "status": "READY",
        "message": "run started from a frozen plan contract",
        "run_id": run_id,
        "run_path": str(current_run_dir),
        "ledger_path": str(ledger_path(repo, run_id)),
        "owner_session_id": session_id,
        "spec_head": spec_head,
        "plan_sha256": contract.sha256,
        "plan_source_sha256": source_contract.source_sha256,
        "plan_frozen_sha256": contract.source_sha256,
        "plan_digest_kind": contract.digest_kind,
        "blackbox_report_schema_version": BLACKBOX_REPORT_SCHEMA_VERSION,
        "runtime_identity": ledger["runtime_identity"],
        "interface_publication_contract_version": (
            INTERFACE_PUBLICATION_CONTRACT_VERSION
        ),
        "interface_input_paths": list(contract.interface_input_paths),
        "lifecycle_delivery": {
            "locator": "ready",
            "binding_sha256": route["binding_sha256"],
        },
        "frozen_plan": str(plan_path),
        "parallel_ready": contract.parallel_ready,
        "continuation_from_run_id": (
            preflight.continuation_from.get("preparation_run_id")
            if isinstance(preflight.continuation_from, dict)
            else None
        ),
        "supersedes_run_id": contract.supersedes_run_id,
        "prerequisite_publication_required": (
            contract.level != "L1" and not contract.parallel_ready
        ),
        "workspace_intake": ledger["workspace_intake"],
        "worktrees": {
            "builder": str(builder_path),
            "tester": str(tester_path),
        },
        "branches": {
            "builder": builder_branch,
            "tester": tester_branch,
        },
        **preflight.target_checkout,
    }, EXIT_PASS


def cmd_role_check(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, _run_id, ledger = resolve_run_selector(args.repo, args.run)
    facts = role_check_facts(repo, ledger, args.role)
    if facts["violations"]:
        return {
            "status": "NEEDS_USER",
            "message": f"{args.role} write boundary needs user resolution",
            "code": "ROLE_BOUNDARY_VIOLATION",
            **facts,
        }, EXIT_FAIL
    return {
        "status": "READY",
        "message": f"{args.role} write boundary is valid",
        **facts,
    }, EXIT_PASS


def initial_tester_start_attestation(ledger: dict[str, Any]) -> dict[str, Any]:
    tester = Path(str(ledger["worktrees"]["tester"]["path"]))
    expected_head = str(ledger["tester_integration"]["base_head"])
    tester_head = full_head(tester)
    dirty_paths = worktree_residue(tester)
    if tester_head != expected_head or dirty_paths:
        raise RuntimeProblem(
            "Tester author worktree does not match its frozen baseline",
            result="NEEDS_USER",
            code="TESTER_AUTHOR_BASELINE_MISMATCH",
            details={
                "expected_head": expected_head,
                "tester_head": tester_head,
                "dirty_paths": dirty_paths,
            },
            exit_code=EXIT_FAIL,
        )
    return {
        "kind": "initial-author",
        "expected_head": expected_head,
        "tester_head": tester_head,
        "dirty_paths": [],
    }


def sync_initial_tester_start_attestation(ledger: dict[str, Any]) -> dict[str, Any] | None:
    if ledger.get("plan", {}).get("level") == "L1":
        return None
    current = ledger.get("agents", {}).get("tester")
    if current is not None:
        return None
    pending = ledger.get("pending_agent_turns", {}).get("tester")
    if isinstance(pending, dict):
        raise RuntimeProblem(
            "initial Tester attestation cannot replace a prepared follow-up",
            result="NEEDS_USER",
            code="TESTER_FOLLOW_UP_ATTESTATION_MISMATCH",
            details={"pending": pending},
            exit_code=EXIT_FAIL,
        )
    attestation = initial_tester_start_attestation(ledger)
    try:
        route = lifecycle_delivery.set_tester_start_attestation(
            str(ledger["owner_session_id"]), attestation
        )
    except lifecycle_delivery.LifecycleDeliveryError as exc:
        raise RuntimeProblem(str(exc), code=exc.code) from exc
    if route.get("tester_start_attestation") != attestation:
        raise RuntimeProblem(
            "lifecycle route did not retain the frozen Tester baseline",
            code="LIFECYCLE_ROUTE_ATTESTATION_MISMATCH",
            details={
                "expected": attestation,
                "observed": route.get("tester_start_attestation"),
            },
        )
    return attestation


def cmd_publish_prerequisites(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        reject_during_finalize_intent(ledger, "publish-prerequisites")
        verify_plan_unchanged(ledger)
        publication = ledger["prerequisite_publication"]
        if publication.get("required") is not True:
            return {
                "status": "NOOP",
                "message": "this plan does not require serial prerequisite publication",
                "run_id": run_id,
            }, EXIT_PASS
        if publication.get("head") is not None:
            sync_initial_tester_start_attestation(ledger)
            return {
                "status": "NOOP",
                "message": "serial prerequisites were already published",
                "run_id": run_id,
                "builder_head": publication["builder_head"],
                "head": publication["head"],
                "tree": publication["tree"],
                "manifest_sha256": publication["manifest_sha256"],
                "paths": publication["paths"],
                "files": publication["files"],
            }, EXIT_PASS
        if ledger.get("phase") != "active":
            raise RuntimeProblem(
                "prerequisites can only be published for an active run",
                result="NEEDS_USER",
                code="PHASE_NOT_ACTIVE",
                details={"phase": ledger.get("phase")},
                exit_code=EXIT_FAIL,
            )
        if ledger.get("agents", {}).get("tester") is not None:
            raise RuntimeProblem(
                "serial prerequisites must be published before the Tester starts",
                result="NEEDS_USER",
                code="PREREQUISITES_TOO_LATE",
                exit_code=EXIT_FAIL,
            )

        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        tester = Path(str(ledger["worktrees"]["tester"]["path"]))
        tester_residue = worktree_residue(tester)
        tester_head_before = full_head(tester)
        expected_tester_head = str(ledger["tester_integration"]["base_head"])
        if tester_residue or expected_tester_head != str(ledger["spec_head"]):
            raise RuntimeProblem(
                "prerequisite publication requires the untouched frozen Tester baseline",
                result="NEEDS_USER",
                code="TESTER_AUTHOR_BASELINE_MISMATCH",
                details={
                    "expected_head": expected_tester_head,
                    "tester_head": tester_head_before,
                    "spec_head": ledger["spec_head"],
                    "dirty_paths": tester_residue,
                },
                exit_code=EXIT_FAIL,
            )
        ensure_role_pass(repo, ledger, "builder")
        spec_head = str(ledger["spec_head"])
        declared = sorted(str(path) for path in publication.get("paths", []))
        changed = git_changed_paths(builder, spec_head)
        intake_paths = set(ledger.get("workspace_intake", {}).get("paths", []))
        publication_changes = sorted(set(changed) - (intake_paths - set(declared)))
        if publication_changes != declared:
            raise RuntimeProblem(
                "the prerequisite snapshot must change exactly the declared public files",
                result="NEEDS_USER",
                code="PREREQUISITE_PATH_MISMATCH",
                details={
                    "declared_paths": declared,
                    "changed_paths": changed,
                    "workspace_intake_paths": sorted(intake_paths),
                },
                exit_code=EXIT_FAIL,
            )
        _, builder_head, checkpointed = checkpoint(
            builder, str(ledger["run_id"]), "prerequisites"
        )
        if worktree_residue(builder):
            raise RuntimeProblem(
                "Builder worktree remained dirty after prerequisite checkpoint",
                code="PREREQUISITE_CHECKPOINT_DIRTY",
                details={"dirty_paths": worktree_residue(builder)},
            )
        ensure_role_pass(repo, ledger, "builder")
        invalid_types: list[dict[str, str]] = []
        files: dict[str, str] = {}
        for path in declared:
            entry = git(builder, "ls-tree", builder_head, "--", path, check=False)
            metadata = entry.stdout.strip().split("\t", 1)[0].split()
            mode = metadata[0] if metadata else ""
            object_type = metadata[1] if len(metadata) > 1 else ""
            object_id = metadata[2] if len(metadata) > 2 else ""
            if (
                entry.returncode != 0
                or mode not in {"100644", "100755"}
                or object_type != "blob"
                or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
            ):
                invalid_types.append(
                    {"path": path, "mode": mode or "missing", "type": object_type or "missing"}
                )
            else:
                files[path] = object_id
        if invalid_types:
            raise RuntimeProblem(
                "public prerequisites must be regular files present in the published commit",
                result="NEEDS_USER",
                code="PREREQUISITE_FILE_INVALID",
                details={"invalid_paths": invalid_types},
                exit_code=EXIT_FAIL,
            )

        try:
            publication_entries = workspace_contract.scan_paths(
                builder, declared, require_dirty=False
            )
            isolated_head, tree = workspace_contract.create_snapshot_commit(
                builder,
                spec_head,
                publication_entries,
                message=f"chore(codex-loop): publish prerequisites {ledger['run_id']}",
            )
        except workspace_contract.WorkspaceError as exc:
            raise RuntimeProblem(str(exc), code=exc.code, details=exc.details) from exc
        publication_head: str
        if tester_head_before == expected_tester_head:
            publication_head = isolated_head
            reset = git(tester, "reset", "--hard", publication_head, check=False)
            if reset.returncode != 0:
                raise RuntimeProblem(
                    "cannot publish the prerequisite commit to the Tester baseline",
                    code="PREREQUISITE_PUBLISH_FAILED",
                    details={
                        "stdout": tail_text(reset.stdout),
                        "stderr": tail_text(reset.stderr),
                    },
                )
        else:
            ancestry = git(
                tester, "rev-list", "--parents", "-n", "1", tester_head_before, check=True
            ).stdout.split()
            tester_tree = git(
                tester, "rev-parse", f"{tester_head_before}^{{tree}}", check=True
            ).stdout.strip()
            tester_changed = git_changed_paths(tester, spec_head)
            if (
                len(ancestry) != 2
                or ancestry[1] != spec_head
                or tester_tree != tree
                or tester_changed != declared
            ):
                raise RuntimeProblem(
                    "Tester history moved before prerequisite publication",
                    result="NEEDS_USER",
                    code="TESTER_AUTHOR_BASELINE_MISMATCH",
                    details={
                        "expected_head": expected_tester_head,
                        "tester_head": tester_head_before,
                        "tester_parents": ancestry[1:],
                        "declared_paths": declared,
                        "tester_changed_paths": tester_changed,
                    },
                    exit_code=EXIT_FAIL,
                )
            publication_head = tester_head_before
        tester_head = full_head(tester)
        if tester_head != publication_head or worktree_residue(tester):
            raise RuntimeProblem(
                "Tester baseline does not match the published prerequisite commit",
                code="PREREQUISITE_PUBLISH_POSTCONDITION",
                details={"published_head": publication_head, "tester_head": tester_head},
            )
        tester_root = tester.resolve()
        unsafe_checkout_paths: list[dict[str, str]] = []
        for path in declared:
            entry = tester / path
            resolved = entry.resolve()
            if entry.is_symlink() or not entry.is_file() or tester_root not in resolved.parents:
                unsafe_checkout_paths.append(
                    {"path": path, "resolved_path": str(resolved)}
                )
        if unsafe_checkout_paths:
            raise RuntimeProblem(
                "published prerequisite checkout contains a non-regular or external file",
                code="PREREQUISITE_PUBLISH_POSTCONDITION",
                details={"invalid_paths": unsafe_checkout_paths},
            )
        manifest = {
            "head": publication_head,
            "paths": declared,
            "files": files,
        }
        manifest_sha256 = sha256_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        publication["builder_head"] = builder_head
        publication["head"] = publication_head
        publication["tree"] = tree
        publication["manifest_sha256"] = manifest_sha256
        publication["files"] = files
        integration = ledger["tester_integration"]
        integration["base_head"] = publication_head
        integration["source_head"] = publication_head
        append_event(
            ledger,
            "prerequisites_published",
            {
                "builder_head": builder_head,
                "head": publication_head,
                "tree": tree,
                "manifest_sha256": manifest_sha256,
                "paths": declared,
                "files": files,
                "checkpointed_paths": checkpointed,
            },
        )
        save_ledger(repo, ledger)
        sync_initial_tester_start_attestation(ledger)
        return {
            "status": "READY",
            "message": "serial prerequisites were published to the Tester baseline",
            "run_id": run_id,
            "builder_head": builder_head,
            "head": publication_head,
            "tree": tree,
            "manifest_sha256": manifest_sha256,
            "paths": declared,
            "files": files,
            "tester_head": tester_head,
        }, EXIT_PASS


def verify_machine(repo: Path, ledger: dict[str, Any]) -> tuple[dict[str, Any], int]:
    reject_during_finalize_intent(ledger, "verify")
    if ledger["phase"] in {
        "iteration_limit",
        "no_progress",
        "architecture_review_required",
    }:
        raise RuntimeProblem(
            "verification is waiting for an explicit user decision",
            result="NEEDS_USER",
            code={
                "iteration_limit": "ITERATION_LIMIT_REACHED",
                "no_progress": "NO_PROGRESS",
                "architecture_review_required": "ARCHITECTURE_REVIEW_REQUIRED",
            }[str(ledger["phase"])],
            details={
                "phase": ledger["phase"],
                "verification_attempts": verification_attempt_count(ledger),
                "max_iterations": ledger.get("loop_config", {}).get("max_iterations"),
            },
            exit_code=EXIT_FAIL,
        )
    if ledger["phase"] != "active":
        raise RuntimeProblem(
            "machine verification requires active phase",
            result="FAIL",
            code="PHASE_NOT_ACTIVE",
            details={"phase": ledger["phase"]},
        )
    builder = Path(str(ledger["worktrees"]["builder"]["path"]))
    contract = verify_plan_unchanged(ledger)
    commands, config_sha, _config_source, max_iterations = load_verification_commands(
        repo, str(ledger["spec_head"]), contract
    )
    validate_runner_ownership(contract, commands)
    if not commands:
        raise RuntimeProblem(
            "L1 plan has no machine verification runner",
            code="RUNNER_NOT_APPLICABLE",
        )
    ensure_role_pass(repo, ledger, "builder")
    before, candidate, checkpointed = checkpoint(builder, str(ledger["run_id"]), "builder")
    invalidate_evidence(repo, ledger, before, candidate)
    if before != candidate:
        append_event(
            ledger,
            "builder_checkpointed",
            {"previous_head": before, "candidate_head": candidate, "paths": checkpointed},
        )
    ensure_role_pass(repo, ledger, "builder")
    candidate_residue = worktree_residue(builder)
    if candidate_residue:
        raise RuntimeProblem(
            "candidate worktree contains ignored or uncommitted residue",
            result="NEEDS_USER",
            code="CANDIDATE_DIRTY",
            details={"dirty_paths": candidate_residue},
            exit_code=EXIT_FAIL,
        )
    current_runner_paths = verification_protected_paths(commands)
    if (
        config_sha != ledger["loop_config"]["spec_sha256"]
        or max_iterations != ledger["loop_config"]["max_iterations"]
        or current_runner_paths != ledger["loop_config"].get("runner_paths", [])
    ):
        raise RuntimeProblem(
            "loop config at spec_head does not match start facts",
            code="LOOP_CONFIG_DRIFT",
        )

    current_machine = evidence_record(ledger, "machine")
    if current_machine is not None and evidence_head(ledger, "machine") == candidate:
        save_ledger(repo, ledger)
        return {
            "status": "PASS",
            "message": "machine evidence inputs are unchanged and the prior result remains valid",
            "head": candidate,
            "verified_head": candidate,
            "reused": True,
            "evidence": current_machine,
            "attempt": verification_attempt_count(ledger),
            "max_iterations": max_iterations,
        }, EXIT_PASS

    attempts = verification_attempt_records(ledger)
    previous_attempts = len(attempts)
    if previous_attempts >= max_iterations:
        ledger["phase"] = "iteration_limit"
        append_event(
            ledger,
            "iteration_limit_reached",
            {"verification_attempts": previous_attempts, "max_iterations": max_iterations},
        )
        save_ledger(repo, ledger)
        raise RuntimeProblem(
            "verification iteration limit was reached",
            result="NEEDS_USER",
            code="ITERATION_LIMIT_REACHED",
            details={
                "verification_attempts": previous_attempts,
                "max_iterations": max_iterations,
            },
            exit_code=EXIT_FAIL,
        )
    attempt = previous_attempts + 1
    attempt_record: dict[str, Any] = {
        "attempt": attempt,
        "candidate_head": candidate,
        "started_at": utc_now(),
        "outcome": "running",
        "stages": [],
    }
    attempts.append(attempt_record)
    append_event(
        ledger,
        "verification_attempt_started",
        {"attempt": attempt, "max_iterations": max_iterations, "candidate_head": candidate},
    )
    save_ledger(repo, ledger)

    evidence_dir = (
        run_dir(repo, str(ledger["run_id"]))
        / "evidence"
        / "machine"
        / candidate
        / f"attempt-{attempt:04d}"
    )
    evidence_dir.mkdir(parents=True, exist_ok=True)
    verify_path = state_root(repo) / "worktrees" / str(ledger["run_id"]) / "verify"
    if verify_path.exists():
        raise RuntimeProblem(
            "verification worktree path already exists",
            result="NEEDS_USER",
            code="VERIFY_WORKTREE_EXISTS",
            details={"path": str(verify_path)},
            exit_code=EXIT_FAIL,
        )
    added = git(repo, "worktree", "add", "--detach", str(verify_path), candidate, check=False)
    if added.returncode != 0:
        raise RuntimeProblem(
            "cannot create clean verification worktree",
            code="VERIFY_WORKTREE_CREATE_FAILED",
            details={"stdout": tail_text(added.stdout), "stderr": tail_text(added.stderr)},
        )

    stage_results: list[dict[str, Any]] = []
    outcome: tuple[dict[str, Any], int] | None = None
    try:
        for item in commands:
            preflight_runner(verify_path, item["cmd"])
        for index, item in enumerate(commands):
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    item["cmd"],
                    cwd=verify_path,
                    shell=True,
                    executable="/bin/bash",
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=item["timeout"],
                    env={
                        **os.environ,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "PYTHONPYCACHEPREFIX": str(evidence_dir / "pycache"),
                    },
                    check=False,
                )
                timed_out = False
            except subprocess.TimeoutExpired as exc:
                completed = subprocess.CompletedProcess(
                    item["cmd"],
                    124,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                )
                timed_out = True
            duration_ms = int((time.monotonic() - started) * 1000)
            safe_stage = (
                re.sub(r"[^A-Za-z0-9._-]+", "-", item["stage"]).strip("-")
                or f"stage-{index + 1}"
            )
            log_path = evidence_dir / f"{index + 1:02d}-{safe_stage}.log"
            log_path.write_text(
                f"$ {item['cmd']}\n\n[stdout]\n{completed.stdout}\n\n[stderr]\n{completed.stderr}\n",
                encoding="utf-8",
            )
            stage_fact = {
                "stage": item["stage"],
                "returncode": completed.returncode,
                "timed_out": timed_out,
                "duration_ms": duration_ms,
                "log": str(log_path),
            }
            stage_results.append(stage_fact)
            attempt_record["stages"] = stage_results

            verification_tree_changes = git(
                verify_path,
                "diff",
                "--name-only",
                candidate,
                "--",
                check=True,
            ).stdout.splitlines()
            verification_head = full_head(verify_path)
            live_builder_head = full_head(builder)
            live_builder_dirty = worktree_residue(builder)
            post_role = role_check_facts(repo, ledger, "builder")
            if (
                verification_head != candidate
                or verification_tree_changes
                or live_builder_head != candidate
                or live_builder_dirty
                or post_role["violations"]
            ):
                clear_evidence(ledger, "machine")
                failure_text = json.dumps(
                    {
                        "code": "VERIFY_MUTATED_CANDIDATE",
                        "stage": item["stage"],
                        "returncode": completed.returncode,
                        "verification_head_changed": verification_head != candidate,
                        "verification_tree_changes": verification_tree_changes,
                        "builder_head_changed": live_builder_head != candidate,
                        "builder_dirty_paths": live_builder_dirty,
                        "role_violations": post_role["violations"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                raw_digest, failure_fingerprint = evidence_contract.failure_digests(
                    failure_text,
                    stage=str(item["stage"]),
                    returncode=int(completed.returncode),
                )
                attempt_record.update(
                    {
                        "outcome": "fail",
                        "code": "VERIFY_MUTATED_CANDIDATE",
                        "stage": item["stage"],
                        "returncode": completed.returncode,
                        "raw_log_digest": raw_digest,
                        "failure_fingerprint": failure_fingerprint,
                        "log_path": str(log_path),
                        "completed_at": utc_now(),
                    }
                )
                same_candidate_failures = [
                    prior
                    for prior in attempts
                    if prior.get("outcome") == "fail"
                    and prior.get("candidate_head") == candidate
                ]
                repeated_candidates = {
                    str(prior.get("candidate_head"))
                    for prior in attempts
                    if prior.get("outcome") == "fail"
                    and prior.get("failure_fingerprint") == failure_fingerprint
                    and prior.get("candidate_head")
                }
                limit_reached = attempt >= max_iterations
                if limit_reached:
                    ledger["phase"] = "iteration_limit"
                elif len(repeated_candidates) >= 3:
                    ledger["phase"] = "architecture_review_required"
                elif len(same_candidate_failures) >= 2:
                    ledger["phase"] = "no_progress"
                stop_code = {
                    "iteration_limit": "ITERATION_LIMIT_REACHED",
                    "architecture_review_required": "ARCHITECTURE_REVIEW_REQUIRED",
                    "no_progress": "NO_PROGRESS",
                }.get(str(ledger.get("phase")))
                append_event(
                    ledger,
                    "machine_verification_tree_changed",
                    {
                        "candidate_head": candidate,
                        "verification_head": verification_head,
                        "verification_tree_changes": verification_tree_changes,
                        "builder_head_after": live_builder_head,
                        "builder_dirty_paths": live_builder_dirty,
                        "role_violations": post_role["violations"],
                        "attempt": attempt,
                        "iteration_limit_reached": limit_reached,
                        "failure_fingerprint": failure_fingerprint,
                        "stop_code": stop_code,
                        **stage_fact,
                    },
                )
                outcome = (
                    {
                        "status": "FAIL",
                        "message": "verification did not preserve the candidate tree",
                        "code": "VERIFY_MUTATED_CANDIDATE",
                        "candidate_head": candidate,
                        "verification_head": verification_head,
                        "verification_tree_changes": verification_tree_changes,
                        "builder_head_after": live_builder_head,
                        "builder_dirty_paths": live_builder_dirty,
                        "role_violations": post_role["violations"],
                        "log_path": str(log_path),
                        "stages": stage_results,
                        "attempt": attempt,
                        "max_iterations": max_iterations,
                        "iteration_limit_reached": limit_reached,
                        "failure_fingerprint": failure_fingerprint,
                        "progress_stop": stop_code,
                    },
                    EXIT_FAIL,
                )
                break
            if completed.returncode != 0:
                clear_evidence(ledger, "machine")
                log_text = log_path.read_text(encoding="utf-8")
                raw_digest, failure_fingerprint = evidence_contract.failure_digests(
                    log_text,
                    stage=str(item["stage"]),
                    returncode=int(completed.returncode),
                )
                attempt_record.update(
                    {
                        "outcome": "fail",
                        "code": "PASS_COMMAND_FAILED",
                        "stage": item["stage"],
                        "returncode": completed.returncode,
                        "raw_log_digest": raw_digest,
                        "failure_fingerprint": failure_fingerprint,
                        "log_path": str(log_path),
                        "completed_at": utc_now(),
                    }
                )
                same_candidate_failures = [
                    item
                    for item in attempts
                    if item.get("outcome") == "fail"
                    and item.get("candidate_head") == candidate
                ]
                repeated_candidates = {
                    str(item.get("candidate_head"))
                    for item in attempts
                    if item.get("outcome") == "fail"
                    and item.get("failure_fingerprint") == failure_fingerprint
                    and item.get("candidate_head")
                }
                limit_reached = attempt >= max_iterations
                if limit_reached:
                    ledger["phase"] = "iteration_limit"
                elif len(repeated_candidates) >= 3:
                    ledger["phase"] = "architecture_review_required"
                elif len(same_candidate_failures) >= 2:
                    ledger["phase"] = "no_progress"
                stop_code = {
                    "iteration_limit": "ITERATION_LIMIT_REACHED",
                    "architecture_review_required": "ARCHITECTURE_REVIEW_REQUIRED",
                    "no_progress": "NO_PROGRESS",
                }.get(str(ledger.get("phase")))
                append_event(
                    ledger,
                    "machine_verification_failed",
                    {
                        "candidate_head": candidate,
                        "attempt": attempt,
                        "max_iterations": max_iterations,
                        "iteration_limit_reached": limit_reached,
                        "failure_fingerprint": failure_fingerprint,
                        "stop_code": stop_code,
                        **stage_fact,
                    },
                )
                outcome = (
                    {
                        "status": "FAIL",
                        "message": f"verification stage failed: {item['stage']}",
                        "code": "PASS_COMMAND_FAILED",
                        "candidate_head": candidate,
                        "head": candidate,
                        "stage": item["stage"],
                        "runner": item["cmd"],
                        "log_path": str(log_path),
                        "stages": stage_results,
                        "attempt": attempt,
                        "max_iterations": max_iterations,
                        "iteration_limit_reached": limit_reached,
                        "failure_fingerprint": failure_fingerprint,
                        "progress_stop": stop_code,
                    },
                    EXIT_FAIL,
                )
                break
    finally:
        removed = git(repo, "worktree", "remove", "--force", str(verify_path), check=False)
        if removed.returncode != 0:
            clear_evidence(ledger, "machine")
            ledger["phase"] = "continuity_failure"
            append_event(
                ledger,
                "verification_worktree_cleanup_failed",
                {"path": str(verify_path), "stderr": tail_text(removed.stderr)},
            )
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "clean verification worktree could not be removed",
                result="CONTINUITY_FAILURE",
                code="VERIFY_WORKTREE_CLEANUP_FAILED",
                details={"path": str(verify_path), "stderr": tail_text(removed.stderr)},
                exit_code=EXIT_FAIL,
            )

    if outcome is not None:
        save_ledger(repo, ledger)
        return outcome
    digest, scope = scoped_input_digest(repo, ledger, "machine", candidate)
    machine_record = evidence_contract.make_record(
        kind="machine",
        observed_head=candidate,
        accepted_head=candidate,
        input_sha256=digest,
        scope=scope,
        provenance={"attempt": attempt, "stages": stage_results},
    )
    ledger.setdefault("evidence", {})["machine"] = machine_record
    attempt_record.update(
        {
            "outcome": "pass",
            "completed_at": utc_now(),
            "input_digest": digest,
        }
    )
    append_event(
        ledger,
        "machine_verification_passed",
        {"verified_head": candidate, "input_digest": digest, "scope": scope, "stages": stage_results},
    )
    save_ledger(repo, ledger)
    return {
        "status": "PASS",
        "message": "all deterministic verification stages passed",
        "head": candidate,
        "verified_head": candidate,
        "evidence": machine_record,
        "stages": stage_results,
        "attempt": attempt,
        "max_iterations": max_iterations,
        "tester_correction_progress": tester_correction_progress(ledger),
    }, EXIT_PASS


def cmd_verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        return verify_machine(repo, ledger)


def reject_nonstandard_json_number(value: str) -> Any:
    raise ValueError(f"non-standard JSON number is not allowed: {value}")


def read_json_input(path_value: str) -> dict[str, Any]:
    if path_value == "-":
        raw = sys.stdin.read()
    else:
        path = Path(path_value).expanduser().resolve()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeProblem(
                f"cannot read proof spec: {exc}",
                code="TEST_PROOF_SPEC_READ_ERROR",
            ) from exc
    try:
        value = json.loads(raw, parse_constant=reject_nonstandard_json_number)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeProblem(
            f"test proof spec is not valid JSON: {exc}",
            result="NEEDS_USER",
            code="TEST_PROOF_SPEC_INVALID",
            details={"errors": [f"invalid JSON: {exc}"]},
            exit_code=EXIT_FAIL,
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeProblem(
            "test proof spec must be a JSON object",
            result="NEEDS_USER",
            code="TEST_PROOF_SPEC_INVALID",
            details={"errors": ["proof spec must be a JSON object"]},
            exit_code=EXIT_FAIL,
        )
    return value


def read_problem_manifest(path_value: str, *, allow_empty: bool) -> dict[str, Any]:
    if path_value == "-":
        raw = sys.stdin.read()
    else:
        path = Path(path_value).expanduser().resolve()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeProblem(
                f"cannot read problem report: {exc}",
                code="PROBLEM_REPORT_READ_ERROR",
            ) from exc
    try:
        value = json.loads(raw, parse_constant=reject_nonstandard_json_number)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeProblem(
            f"problem report is not valid JSON: {exc}",
            result="NEEDS_USER",
            code="PROBLEM_REPORT_INVALID",
            details={"errors": [f"invalid JSON: {exc}"]},
            exit_code=EXIT_FAIL,
        ) from exc
    errors: list[str] = []
    if not isinstance(value, dict):
        errors.append("problem report must be a JSON object")
        value = {}
    if set(value) != {"schema_version", "problems"}:
        errors.append("problem report must contain only schema_version and problems")
    if value.get("schema_version") != PROBLEM_REPORT_SCHEMA_VERSION:
        errors.append(f"schema_version must be {PROBLEM_REPORT_SCHEMA_VERSION}")
    raw_problems = value.get("problems")
    if not isinstance(raw_problems, list) or (not allow_empty and not raw_problems):
        errors.append(
            "problems must be a list" + ("" if allow_empty else " with at least one item")
        )
        raw_problems = []
    normalized: list[dict[str, str]] = []
    keys: set[str] = set()
    for index, item in enumerate(raw_problems):
        field = f"problems[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} must be an object")
            continue
        expected = {"key", "summary", "details", "owner"}
        if set(item) != expected:
            errors.append(f"{field} must contain only key, summary, details, owner")
            continue
        key = item.get("key")
        summary = item.get("summary")
        details = item.get("details")
        owner = item.get("owner")
        if not isinstance(key, str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", key
        ):
            errors.append(f"{field}.key must be kebab-case")
            continue
        if key in keys:
            errors.append(f"problem keys must be unique: {key}")
            continue
        keys.add(key)
        if not isinstance(summary, str) or not summary.strip():
            errors.append(f"{field}.summary must be non-empty")
        if not isinstance(details, str) or not details.strip():
            errors.append(f"{field}.details must be non-empty")
        if owner not in PROBLEM_OWNERS:
            errors.append(f"{field}.owner is invalid")
        if (
            isinstance(summary, str)
            and summary.strip()
            and isinstance(details, str)
            and details.strip()
            and owner in PROBLEM_OWNERS
        ):
            normalized.append(
                {
                    "key": key,
                    "summary": summary.strip(),
                    "details": details.strip(),
                    "owner": str(owner),
                }
            )
    if errors:
        raise RuntimeProblem(
            "problem report does not match codex-problem-report-v1",
            result="NEEDS_USER",
            code="PROBLEM_REPORT_INVALID",
            details={"errors": errors},
            exit_code=EXIT_FAIL,
        )
    return {
        "schema_version": PROBLEM_REPORT_SCHEMA_VERSION,
        "problems": sorted(normalized, key=lambda item: item["key"]),
    }


def completed_problem_source(
    ledger: Mapping[str, Any], role: str, source_id: str
) -> dict[str, Any] | None:
    for event in reversed(list(ledger.get("events", []))):
        if not isinstance(event, dict) or event.get("type") != "agent_event":
            continue
        facts = event.get("facts")
        if (
            isinstance(facts, dict)
            and facts.get("role") == role
            and facts.get("turn_id") == source_id
            and facts.get("event") == "idle"
            and facts.get("result") in PROBLEM_REPORT_RESULTS.get(role, set())
        ):
            return dict(facts)
    return None


def problem_inventory(ledger: dict[str, Any]) -> dict[str, Any]:
    value = ledger.setdefault("problem_inventory", empty_problem_inventory())
    if not isinstance(value, dict):
        raise RuntimeProblem("problem inventory is invalid", code="LEDGER_SCHEMA_ERROR")
    return value


def missing_problem_sources(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    inventory = ledger.get("problem_inventory", {})
    raw_sources = inventory.get("sources", []) if isinstance(inventory, dict) else []
    recorded = {
        (str(item.get("source")), str(item.get("source_id")))
        for item in raw_sources
        if isinstance(item, dict) and item.get("source_id")
    }
    missing: list[dict[str, Any]] = []
    for event in ledger.get("events", []):
        if not isinstance(event, dict) or event.get("type") != "agent_event":
            continue
        facts = event.get("facts")
        if not isinstance(facts, dict):
            continue
        role = str(facts.get("role") or "")
        result = facts.get("result")
        source_id = str(facts.get("turn_id") or "")
        if (
            facts.get("event") == "idle"
            and result in PROBLEM_REPORT_RESULTS.get(role, set())
            and (role, source_id) not in recorded
        ):
            missing.append(
                {
                    "source": role,
                    "source_id": source_id,
                    "agent_id": str(facts.get("agent_id") or ""),
                    "result": result,
                }
            )
    return missing


def require_problem_sources_recorded(ledger: Mapping[str, Any]) -> None:
    missing = missing_problem_sources(ledger)
    if missing:
        raise RuntimeProblem(
            "有角色已经报出问题，但逐条问题尚未写入问题清单",
            result="NEEDS_USER",
            code="PROBLEM_REPORT_REQUIRED",
            details={"missing_problem_sources": missing},
            exit_code=EXIT_FAIL,
        )


def problem_snapshot_value(
    ledger: Mapping[str, Any], problems: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    payload = {
        "schema_version": PROBLEM_REPORT_SCHEMA_VERSION,
        "run_id": str(ledger.get("run_id")),
        "problems": [dict(item) for item in problems],
    }
    return {
        **payload,
        "sha256": canonical_json_sha256(payload),
        "problem_ids": [str(item["problem_id"]) for item in payload["problems"]],
        "sealed_at": utc_now(),
    }


def seal_problem_snapshot(
    ledger: dict[str, Any], *, backfilled: bool, manifest_sha256: str | None = None
) -> dict[str, Any]:
    inventory = problem_inventory(ledger)
    existing = inventory.get("snapshot")
    if isinstance(existing, dict):
        return existing
    snapshot = problem_snapshot_value(ledger, inventory.get("items", []))
    snapshot["backfilled"] = backfilled
    snapshot["manifest_sha256"] = manifest_sha256
    inventory["snapshot"] = snapshot
    append_event(
        ledger,
        "problem_snapshot_sealed",
        {
            "sha256": snapshot["sha256"],
            "problem_ids": snapshot["problem_ids"],
            "backfilled": backfilled,
        },
    )
    return snapshot


def problem_inventory_facts(ledger: Mapping[str, Any]) -> dict[str, Any]:
    inventory = ledger.get("problem_inventory", {})
    items = inventory.get("items", []) if isinstance(inventory, dict) else []
    inherited = inventory.get("inherited_from") if isinstance(inventory, dict) else None
    snapshot = inventory.get("snapshot") if isinstance(inventory, dict) else None
    return {
        "problem_count": len(items) if isinstance(items, list) else 0,
        "inherited_problem_count": (
            len(
                [
                    item
                    for item in items
                    if isinstance(item, dict)
                    and item.get("origin_run_id") != ledger.get("run_id")
                ]
            )
            if isinstance(items, list)
            else 0
        ),
        "missing_problem_sources": missing_problem_sources(ledger),
        "inherited_from": inherited,
        "snapshot_sha256": snapshot.get("sha256") if isinstance(snapshot, dict) else None,
        "snapshot_problem_ids": (
            snapshot.get("problem_ids", []) if isinstance(snapshot, dict) else []
        ),
    }


def problem_records_for_manifest(
    ledger: Mapping[str, Any],
    *,
    source: str,
    source_id: str,
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in manifest.get("problems", []):
        key = str(item["key"])
        problem_id = sha256_text(
            "\0".join(
                [
                    str(ledger.get("run_id")),
                    source,
                    source_id,
                    key,
                ]
            )
        )
        records.append(
            {
                "problem_id": problem_id,
                "key": key,
                "summary": str(item["summary"]),
                "details": str(item["details"]),
                "owner": str(item["owner"]),
                "origin_run_id": str(ledger.get("run_id")),
                "source": source,
                "source_id": source_id,
            }
        )
    return records


def cmd_record_problems(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    manifest = read_problem_manifest(str(args.manifest), allow_empty=False)
    manifest_sha256 = canonical_json_sha256(manifest)
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    source = str(args.source)
    source_id = str(args.source_id).strip()
    if not source_id or len(source_id) > 128:
        raise RuntimeProblem(
            "problem source id must be 1-128 characters",
            result="NEEDS_USER",
            code="PROBLEM_SOURCE_INVALID",
            exit_code=EXIT_FAIL,
        )
    with locked_run(repo, run_id) as ledger:
        reject_during_finalize_intent(ledger, "record-problems")
        if ledger.get("phase") not in ACTIVE_PHASES:
            raise RuntimeProblem(
                "problem reports can only be recorded before a run becomes terminal",
                result="NEEDS_USER",
                code="PHASE_NOT_ACTIVE",
                details={"phase": ledger.get("phase")},
                exit_code=EXIT_FAIL,
            )
        source_result: str | None = None
        if source in {"tester", "reviewer"}:
            fact = completed_problem_source(ledger, source, source_id)
            if fact is None:
                raise RuntimeProblem(
                    "问题报告没有绑定该角色已完成的问题结果",
                    result="NEEDS_USER",
                    code="PROBLEM_SOURCE_INVALID",
                    details={"source": source, "source_id": source_id},
                    exit_code=EXIT_FAIL,
                )
            source_result = str(fact.get("result"))
        inventory = problem_inventory(ledger)
        if isinstance(inventory.get("snapshot"), dict):
            raise RuntimeProblem(
                "sealed problem snapshot cannot accept new reports",
                result="NEEDS_USER",
                code="PROBLEM_SNAPSHOT_SEALED",
                exit_code=EXIT_FAIL,
            )
        for recorded in inventory.get("sources", []):
            if not isinstance(recorded, dict):
                continue
            same_source = (
                recorded.get("source") == source
                and recorded.get("source_id") == source_id
            )
            if same_source:
                if recorded.get("manifest_sha256") == manifest_sha256:
                    return {
                        "status": "NOOP",
                        "message": "同一来源的问题已经写入问题清单",
                        "run_id": run_id,
                        "source": source,
                        "source_id": source_id,
                        "manifest_sha256": manifest_sha256,
                        "problem_ids": recorded.get("problem_ids", []),
                    }, EXIT_PASS
                raise RuntimeProblem(
                    "同一来源不能改写成另一份问题报告",
                    result="NEEDS_USER",
                    code="PROBLEM_REPORT_CONFLICT",
                    details={
                        "source": source,
                        "source_id": source_id,
                        "recorded_manifest_sha256": recorded.get("manifest_sha256"),
                        "incoming_manifest_sha256": manifest_sha256,
                    },
                    exit_code=EXIT_FAIL,
                )
        records = problem_records_for_manifest(
            ledger,
            source=source,
            source_id=source_id,
            manifest=manifest,
        )
        existing = {
            str(item.get("problem_id")): item
            for item in inventory.get("items", [])
            if isinstance(item, dict)
        }
        for record in records:
            current = existing.get(record["problem_id"])
            if current is not None and current != record:
                raise RuntimeProblem(
                    "problem id collides with different recorded content",
                    code="PROBLEM_REPORT_CONFLICT",
                    details={"problem_id": record["problem_id"]},
                )
            if current is None:
                inventory.setdefault("items", []).append(record)
        source_record = {
            "source": source,
            "source_id": source_id,
            "result": source_result,
            "manifest_sha256": manifest_sha256,
            "problem_ids": [item["problem_id"] for item in records],
            "recorded_at": utc_now(),
        }
        inventory.setdefault("sources", []).append(source_record)
        append_event(ledger, "problems_recorded", source_record)
        save_ledger(repo, ledger)
        return {
            "status": "READY",
            "message": "逐条问题已写入当前 run 的问题清单",
            "run_id": run_id,
            "source": source,
            "source_id": source_id,
            "manifest_sha256": manifest_sha256,
            "problem_ids": source_record["problem_ids"],
            "problem_count": len(records),
        }, EXIT_PASS


def cmd_backfill_problems(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    manifest = read_problem_manifest(str(args.manifest), allow_empty=True)
    manifest_sha256 = canonical_json_sha256(manifest)
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        if ledger.get("phase") != "abandoned":
            raise RuntimeProblem(
                "旧问题补录只适用于已经作废的 run",
                result="NEEDS_USER",
                code="PROBLEM_BACKFILL_NOT_ALLOWED",
                details={"phase": ledger.get("phase")},
                exit_code=EXIT_FAIL,
            )
        inventory = problem_inventory(ledger)
        existing_snapshot = inventory.get("snapshot")
        if isinstance(existing_snapshot, dict):
            if (
                existing_snapshot.get("backfilled") is True
                and existing_snapshot.get("manifest_sha256") == manifest_sha256
            ):
                return {
                    "status": "NOOP",
                    "message": "旧一轮的问题清单已经按同一内容补录",
                    "run_id": run_id,
                    "problem_snapshot_sha256": existing_snapshot.get("sha256"),
                    "problem_ids": existing_snapshot.get("problem_ids", []),
                    "problem_count": len(existing_snapshot.get("problem_ids", [])),
                }, EXIT_PASS
            raise RuntimeProblem(
                "已经封存的问题清单不能被补录覆盖",
                result="NEEDS_USER",
                code="PROBLEM_BACKFILL_CONFLICT",
                details={"snapshot_sha256": existing_snapshot.get("sha256")},
                exit_code=EXIT_FAIL,
            )
        if inventory.get("items") or inventory.get("sources"):
            raise RuntimeProblem(
                "旧 ledger 已含未封存的问题事实，不能用补录覆盖",
                result="NEEDS_USER",
                code="PROBLEM_BACKFILL_CONFLICT",
                exit_code=EXIT_FAIL,
            )
        records = problem_records_for_manifest(
            ledger,
            source="coordinator",
            source_id="legacy-backfill",
            manifest=manifest,
        )
        inventory["items"] = records
        inventory["sources"] = [
            {
                "source": "coordinator",
                "source_id": "legacy-backfill",
                "result": None,
                "manifest_sha256": manifest_sha256,
                "problem_ids": [item["problem_id"] for item in records],
                "recorded_at": utc_now(),
            }
        ]
        snapshot = seal_problem_snapshot(
            ledger, backfilled=True, manifest_sha256=manifest_sha256
        )
        append_event(
            ledger,
            "legacy_problems_backfilled",
            {
                "manifest_sha256": manifest_sha256,
                "snapshot_sha256": snapshot["sha256"],
                "problem_ids": snapshot["problem_ids"],
            },
        )
        save_ledger(repo, ledger)
        return {
            "status": "READY",
            "message": "旧一轮的问题清单已补录并封存",
            "run_id": run_id,
            "manifest_sha256": manifest_sha256,
            "problem_snapshot_sha256": snapshot["sha256"],
            "problem_ids": snapshot["problem_ids"],
            "problem_count": len(records),
        }, EXIT_PASS


def json_schema_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def executable_identity(path: Path, *, requested: str, kind: str) -> dict[str, Any]:
    try:
        resolved = path.expanduser().resolve(strict=True)
        mode = os.stat(resolved).st_mode
    except OSError as exc:
        raise RuntimeProblem(
            f"cannot resolve proof executable: {exc}",
            result="NEEDS_USER",
            code="TEST_PROOF_EXECUTABLE_INVALID",
            details={"requested": requested},
            exit_code=EXIT_FAIL,
        ) from exc
    if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
        raise RuntimeProblem(
            "proof executable must resolve to an executable regular file",
            result="NEEDS_USER",
            code="TEST_PROOF_EXECUTABLE_INVALID",
            details={"requested": requested, "resolved": str(resolved)},
            exit_code=EXIT_FAIL,
        )
    return {
        "kind": kind,
        "requested": requested,
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size": int(os.stat(resolved).st_size),
    }


def proof_pytest_args(args: Sequence[str]) -> list[str]:
    values = [
        item
        for item in args
        if item != "--quiet" and not re.fullmatch(r"-q+", item)
    ]
    cache_disabled = any(
        item == "-pno:cacheprovider"
        or (
            item == "-p"
            and index + 1 < len(values)
            and values[index + 1] == "no:cacheprovider"
        )
        for index, item in enumerate(values)
    )
    values = ["-vv", *values]
    return values if cache_disabled else ["-p", "no:cacheprovider", *values]


def proof_unittest_args(args: Sequence[str]) -> list[str]:
    values = [item for item in args if item not in {"-q", "--quiet"}]
    return values if any(item in {"-v", "--verbose"} for item in values) else ["-v", *values]


def allowlisted_proof_runner(argv: Sequence[str]) -> dict[str, Any] | None:
    executable = Path(argv[0]).name.lower()
    normalized = executable[:-4] if executable.endswith(".exe") else executable
    python_match = re.fullmatch(
        r"python(?P<version>[0-9]+(?:\.[0-9]+)*)?m?", normalized
    )
    if python_match:
        requested_version = python_match.group("version")
        if requested_version:
            parts = tuple(int(item) for item in requested_version.split("."))
            current = (sys.version_info.major, sys.version_info.minor)
            if parts[0] != current[0] or (len(parts) > 1 and parts[1] != current[1]):
                return None
        trusted_python = executable_identity(
            Path(sys.executable), requested=argv[0], kind="trusted-python"
        )
        module_positions = [index for index, item in enumerate(argv[1:], 1) if item == "-m"]
        if len(module_positions) != 1:
            return None
        module_index = module_positions[0]
        harmless_flags = {
            "-B",
            "-E",
            "-I",
            "-O",
            "-OO",
            "-P",
            "-S",
            "-s",
            "-u",
        }
        if any(item not in harmless_flags for item in argv[1:module_index]):
            return None
        if len(argv) <= module_index + 1:
            return None
        module = argv[module_index + 1]
        if module not in {"pytest", "unittest"}:
            return None
        flags = list(argv[1:module_index])
        args = list(argv[module_index + 2 :])
        execution_args = (
            proof_pytest_args(args) if module == "pytest" else proof_unittest_args(args)
        )
        return {
            "argv": list(argv),
            "execution_argv": [
                trusted_python["path"],
                *flags,
                "-m",
                module,
                *execution_args,
            ],
            "control_argv": [module, *execution_args],
            "framework": module,
            "executable_identity": trusted_python,
        }
    if normalized in {"py.test", "pytest"}:
        trusted_python = executable_identity(
            Path(sys.executable), requested=argv[0], kind="trusted-python"
        )
        args = proof_pytest_args(argv[1:])
        return {
            "argv": list(argv),
            "execution_argv": [trusted_python["path"], "-m", "pytest", *args],
            "control_argv": ["pytest", *args],
            "framework": "pytest",
            "executable_identity": trusted_python,
        }
    return None


def repository_wrapper_runner(
    argv: Sequence[str], repository_paths: Sequence[str]
) -> dict[str, Any] | None:
    frozen_paths = set(repository_paths)
    for index, token in enumerate(argv):
        if token.startswith("-") or "$" in token or "`" in token:
            continue
        try:
            normalized = normalize_allowed_path(token, directory_hint=False)
        except RuntimeProblem:
            continue
        if normalized not in frozen_paths:
            continue
        nested_start = index + 1
        if list(argv[nested_start : nested_start + 1]) == ["--"]:
            nested_start += 1
        nested = list(argv[nested_start:])
        if not nested or "/" in nested[0] or "\\" in nested[0]:
            return None
        try:
            runner = allowlisted_proof_runner(nested)
        except RuntimeProblem:
            return None
        if runner is None:
            return None
        framework = str(runner.get("framework", ""))
        if framework not in {"unittest", "pytest"}:
            return None
        return {**runner, "nested_start": nested_start}
    return None


def validate_allowlisted_test_selectors(
    argv: Sequence[str],
    framework: str,
    test_ids: Sequence[str],
    field: str,
    errors: list[str],
) -> None:
    if framework not in {"unittest", "pytest"}:
        return
    declared = {normalize_proof_test_id(item) for item in test_ids}
    values = list(argv)
    try:
        module_index = values.index(framework)
    except ValueError:
        module_index = 0
    args = values[module_index + 1 :]
    if framework == "unittest" and "discover" in args:
        errors.append(f"{field} unittest discovery is not content-bound to declared test_ids")
    path_override_options = {
        "--basetemp",
        "--confcutdir",
        "--rootdir",
        "--pyargs",
        "-c",
    }
    for index, item in enumerate(args):
        option = item.split("=", 1)[0]
        attached_pytest_config = (
            framework == "pytest"
            and item.startswith("-c")
            and item != "-c"
            and not item.startswith("--")
        )
        if framework == "pytest" and (
            option in path_override_options or attached_pytest_config
        ):
            errors.append(f"{field} cannot redirect pytest discovery or configuration: {item}")
            continue
        candidate = normalize_proof_test_id(item)
        if item.startswith("-"):
            continue
        if Path(candidate).is_absolute() or ".." in PurePosixPath(candidate).parts:
            errors.append(f"{field} test selector escapes the repository: {item}")
            continue
        if framework == "unittest" or ".py" in candidate or "::" in candidate:
            if candidate not in declared:
                errors.append(
                    f"{field} test selector is not an exact declared test id: {item}"
                )
    selected = {normalize_proof_test_id(item) for item in args if not item.startswith("-")}
    missing = sorted(declared - selected)
    if missing:
        errors.append(
            f"{field} must explicitly select every declared test id: " + ", ".join(missing)
        )


def repository_runner_identity(
    repo: Path,
    spec_head: str,
    argv: Sequence[str],
    repository_paths: Sequence[str],
) -> tuple[list[str], dict[str, Any]]:
    frozen_paths: list[dict[str, str]] = []
    for path in repository_paths:
        entry = git(repo, "ls-tree", spec_head, "--", path, check=False).stdout.strip()
        fields = entry.split(None, 3)
        if len(fields) < 3:
            raise RuntimeProblem(
                "proof repository runner is missing from spec_head",
                result="NEEDS_USER",
                code="TEST_PROOF_EXECUTABLE_INVALID",
                details={"path": path, "spec_head": spec_head},
                exit_code=EXIT_FAIL,
            )
        frozen_paths.append({"path": path, "blob": fields[2]})

    execution_argv = list(argv)
    requested = argv[0]
    requested_path = Path(requested)
    if "/" not in requested and "\\" not in requested:
        resolved = shutil.which(requested, path=os.defpath)
        if resolved is None:
            raise RuntimeProblem(
                "proof repository runner launcher is not available on the trusted system path",
                result="NEEDS_USER",
                code="TEST_PROOF_EXECUTABLE_INVALID",
                details={"requested": requested},
                exit_code=EXIT_FAIL,
            )
        launcher = executable_identity(
            Path(resolved), requested=requested, kind="trusted-system-launcher"
        )
        execution_argv[0] = launcher["path"]
    elif requested_path.is_absolute():
        launcher = executable_identity(
            requested_path, requested=requested, kind="absolute-launcher"
        )
        execution_argv[0] = launcher["path"]
    else:
        launcher = {
            "kind": "frozen-repository-entry",
            "requested": requested,
        }
    return execution_argv, {
        **launcher,
        "repository_paths": frozen_paths,
    }


def validate_proof_argv(
    value: Any,
    field: str,
    errors: list[str],
    *,
    repo: Path,
    spec_head: str,
    contract: PlanContract,
) -> dict[str, Any]:
    fallback = {
        "argv": [],
        "execution_argv": [],
        "framework": "unknown",
        "executable_identity": {},
    }
    initial_error_count = len(errors)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item or "\x00" in item for item in value)
    ):
        errors.append(f"{field} must be a non-empty string array")
        return fallback
    argv = [str(item) for item in value]
    executable = Path(argv[0]).name.lower()
    normalized_executable = (
        executable[:-4] if executable.endswith(".exe") else executable
    )
    if normalized_executable == "env":
        errors.append(f"{field} cannot override process environment or PATH")

    command_dispatchers = {
        "busybox",
        "chroot",
        "command",
        "doas",
        "exec",
        "ionice",
        "nice",
        "nohup",
        "parallel",
        "script",
        "setsid",
        "stdbuf",
        "sudo",
        "taskset",
        "timeout",
        "toybox",
        "unbuffer",
        "watch",
        "xargs",
    }
    if normalized_executable in command_dispatchers:
        errors.append(
            f"{field} cannot use a command dispatcher to hide nested inline control "
            "flow; invoke the test command directly or move complex control flow "
            "into a protected repository script"
        )

    lowered_args = [item.lower() for item in argv[1:]]

    def short_inline_option(item: str, options: set[str]) -> bool:
        return any(item == option or item.startswith(option) for option in options)

    def long_inline_option(item: str, options: set[str]) -> bool:
        return any(item == option or item.startswith(option + "=") for option in options)

    shell_executable = re.fullmatch(
        r"(?:"
        r"(?:sh|ash|bash|csh|dash|elvish|es|fish|ksh|lksh|mksh|nu|oksh|"
        r"osh|pdksh|rc|rksh|tcsh|xonsh|yash|ysh|zsh)"
        r"(?:[0-9]+(?:\.[0-9]+)*)?|"
        r"cmd|powershell(?:[0-9]+(?:\.[0-9]+)*)?|"
        r"pwsh(?:[0-9]+(?:\.[0-9]+)*)?"
        r")",
        normalized_executable,
    )
    shell_inline = any(
        short_inline_option(item, {"-c", "/c", "/k"})
        or long_inline_option(
            item,
            {
                "-command",
                "--command",
                "--commands",
                "-encodedcommand",
                "--encoded-command",
            },
        )
        or (
            item.startswith("-")
            and not item.startswith("--")
            and "c" in item[1:]
            and item[1:].isalpha()
        )
        for item in lowered_args
    )
    if shell_executable and shell_inline:
        errors.append(f"{field} cannot use inline shell control flow")

    interpreter_family: str | None = None
    interpreter_patterns = {
        "python": r"(?:python|pypy)(?:[0-9]+(?:\.[0-9]+)*)?m?",
        "node": r"node(?:js)?(?:[0-9]+(?:\.[0-9]+)*)?",
        "ruby": r"ruby(?:[0-9]+(?:\.[0-9]+)*)?",
        "perl": r"perl(?:[0-9]+(?:\.[0-9]+)*)?",
        "php": r"php(?:[0-9]+(?:\.[0-9]+)*)?",
        "eval": r"(?:lua|rscript|julia)(?:[0-9]+(?:\.[0-9]+)*)?",
    }
    for family, pattern in interpreter_patterns.items():
        if re.fullmatch(pattern, normalized_executable):
            interpreter_family = family
            break
    inline_options = {
        "python": ({"-c"}, {"--command"}),
        "node": ({"-e", "-p"}, {"--eval", "--print", "--command"}),
        "ruby": ({"-e"}, {"--eval", "--command"}),
        "perl": ({"-e"}, {"--eval", "--command"}),
        "php": ({"-r"}, {"--run", "--command"}),
        "eval": ({"-e"}, {"--eval", "--expression", "--command"}),
    }
    interpreter_inline = False
    if interpreter_family is not None:
        short_options, long_options = inline_options[interpreter_family]
        interpreter_inline = any(
            short_inline_option(item, short_options)
            or long_inline_option(item, long_options)
            for item in lowered_args
        )
    if interpreter_inline:
        errors.append(f"{field} cannot use inline interpreter control flow")

    if len(errors) > initial_error_count:
        return {**fallback, "argv": argv, "execution_argv": argv}

    allowlisted_runner = None
    if "/" not in argv[0] and "\\" not in argv[0]:
        try:
            allowlisted_runner = allowlisted_proof_runner(argv)
        except RuntimeProblem as exc:
            errors.append(f"{field} executable is not trusted: {exc} [{exc.code}]")
    if allowlisted_runner is not None:
        try:
            validate_runner_ownership(
                contract,
                [
                    {
                        "stage": "test-proof",
                        "cmd": shlex.join(allowlisted_runner["control_argv"]),
                        "timeout": 1,
                    }
                ],
            )
        except RuntimeProblem as exc:
            errors.append(
                f"{field} test runner control files are not protected: "
                f"{exc} [{exc.code}]"
            )
        return {
            **allowlisted_runner,
            "selector_argv": allowlisted_runner["control_argv"],
        }

    command = shlex.join(argv)
    try:
        repository_paths = runner_repository_paths(command)
    except RuntimeProblem as exc:
        errors.append(
            f"{field} repository command is unsafe: {exc} [{exc.code}] "
            + json.dumps(exc.details, ensure_ascii=False, sort_keys=True)
        )
        return {**fallback, "argv": argv, "execution_argv": argv}
    commands = [{"stage": "test-proof", "cmd": command, "timeout": 1}]
    if repository_paths:
        try:
            validate_runner_dependencies_at_spec_head(repo, spec_head, commands)
            validate_runner_ownership(contract, commands)
        except RuntimeProblem as exc:
            errors.append(
                f"{field} repository test wrapper is not frozen and protected: "
                f"{exc} [{exc.code}] "
                + json.dumps(exc.details, ensure_ascii=False, sort_keys=True)
            )
        try:
            execution_argv, identity = repository_runner_identity(
                repo, spec_head, argv, repository_paths
            )
        except RuntimeProblem as exc:
            errors.append(f"{field} executable is not trusted: {exc} [{exc.code}]")
            execution_argv = argv
            identity = {}
        nested_runner = repository_wrapper_runner(argv, repository_paths)
        if nested_runner is None:
            errors.append(
                f"{field} repository wrapper must explicitly forward one supported "
                "unittest or pytest command with a trusted executable"
            )
            return {
                "argv": argv,
                "execution_argv": execution_argv,
                "framework": "unknown",
                "executable_identity": identity,
                "selector_argv": argv,
            }
        try:
            validate_runner_ownership(
                contract,
                [
                    {
                        "stage": "test-proof",
                        "cmd": shlex.join(nested_runner["control_argv"]),
                        "timeout": 1,
                    }
                ],
            )
        except RuntimeProblem as exc:
            errors.append(
                f"{field} nested test runner control files are not protected: "
                f"{exc} [{exc.code}]"
            )
        nested_start = int(nested_runner["nested_start"])
        execution_argv = [
            *execution_argv[:nested_start],
            *list(nested_runner["execution_argv"]),
        ]
        return {
            "argv": argv,
            "execution_argv": execution_argv,
            "framework": nested_runner["framework"],
            "executable_identity": identity,
            "selector_argv": nested_runner["control_argv"],
        }

    errors.append(
        f"{field} must use an allowlisted non-inline test runner "
        "(python -m unittest/pytest or pytest) or a test_context.support_paths "
        "repository script that already exists at spec_head"
    )
    return {**fallback, "argv": argv, "execution_argv": argv}


def normalize_test_proof_spec(
    value: dict[str, Any],
    requirements: Sequence[dict[str, str]],
    *,
    repo: Path,
    spec_head: str,
    contract: PlanContract,
) -> dict[str, Any]:
    errors: list[str] = []
    unknown_root = sorted(set(value) - {"schema_version", "groups"})
    if unknown_root:
        errors.append("proof spec has unknown fields: " + ", ".join(unknown_root))
    schema_version = json_schema_integer(value.get("schema_version"))
    if schema_version != 1:
        errors.append("proof spec schema_version must be 1")
    groups = value.get("groups")
    if not isinstance(groups, list) or not groups:
        errors.append("proof spec groups must be a non-empty list")
        groups = []
    required_by_behavior = {
        str(item["behavior_id"]): str(item["minimum"]) for item in requirements
    }
    normalized_groups: list[dict[str, Any]] = []
    seen_behaviors: list[str] = []
    for index, item in enumerate(groups):
        field = f"groups[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} must be a mapping")
            continue
        method = item.get("method")
        if method not in {"baseline-red", "mutation", "reviewed-boundaries"}:
            errors.append(
                f"{field}.method must be baseline-red, mutation, or reviewed-boundaries"
            )
            continue
        common = {
            "behavior_ids",
            "method",
            "argv",
            "test_ids",
            "timeout_seconds",
        }
        method_fields = {
            "baseline-red": {"claimed_failure_kind"},
            "mutation": {"patch"},
            "reviewed-boundaries": {"reason", "reviewed_boundaries"},
        }[str(method)]
        unknown = sorted(set(item) - common - method_fields)
        if unknown:
            errors.append(f"{field} has unknown fields: " + ", ".join(unknown))
        behavior_ids = _non_empty_strings(
            item.get("behavior_ids"), f"{field}.behavior_ids", errors
        )
        if len(behavior_ids) != 1:
            errors.append(f"{field}.behavior_ids must contain exactly one behavior id")
        for behavior_id in behavior_ids:
            if behavior_id not in required_by_behavior:
                errors.append(f"{field} references unknown behavior id: {behavior_id}")
            seen_behaviors.append(behavior_id)
        if method == "reviewed-boundaries":
            strong = sorted(
                behavior_id
                for behavior_id in behavior_ids
                if required_by_behavior.get(behavior_id) == "strong"
            )
            if strong:
                errors.append(
                    f"{field} cannot downgrade strong behaviors: " + ", ".join(strong)
                )
        runner = validate_proof_argv(
            item.get("argv"),
            f"{field}.argv",
            errors,
            repo=repo,
            spec_head=spec_head,
            contract=contract,
        )
        selector_argv = runner.get("selector_argv", runner["argv"])
        timeout = json_schema_integer(item.get("timeout_seconds"))
        if timeout is None or not 1 <= timeout <= 600:
            errors.append(f"{field}.timeout_seconds must be an integer from 1 to 600")
            timeout = 1
        normalized: dict[str, Any] = {
            "behavior_ids": behavior_ids,
            "method": method,
            "argv": runner["argv"],
            "execution_argv": runner["execution_argv"],
            "framework": runner["framework"],
            "executable_identity": runner["executable_identity"],
            "timeout_seconds": timeout,
        }
        if method == "reviewed-boundaries":
            reason = item.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{field}.reason must be a non-empty string")
                reason = ""
            normalized["test_ids"] = _non_empty_strings(
                item.get("test_ids"), f"{field}.test_ids", errors
            )
            raw_boundaries = item.get("reviewed_boundaries")
            categories = (
                "positive_test_ids",
                "negative_test_ids",
                "boundary_test_ids",
                "invariant_test_ids",
            )
            if not isinstance(raw_boundaries, dict):
                errors.append(f"{field}.reviewed_boundaries must be a mapping")
                raw_boundaries = {}
            extra_categories = sorted(set(raw_boundaries) - set(categories))
            if extra_categories:
                errors.append(
                    f"{field}.reviewed_boundaries has unknown fields: "
                    + ", ".join(extra_categories)
                )
            normalized["reviewed_boundaries"] = {
                category: _non_empty_strings(
                    raw_boundaries.get(category),
                    f"{field}.reviewed_boundaries.{category}",
                    errors,
                )
                for category in categories
            }
            normalized["reason"] = reason.strip()
        else:
            normalized["test_ids"] = _non_empty_strings(
                item.get("test_ids"), f"{field}.test_ids", errors
            )
            if method == "baseline-red":
                claimed = item.get("claimed_failure_kind")
                if claimed != "assertion-failure":
                    errors.append(
                        f"{field}.claimed_failure_kind must be assertion-failure"
                    )
                    claimed = "assertion-failure"
                normalized["claimed_failure_kind"] = claimed
            else:
                patch = item.get("patch")
                if (
                    not isinstance(patch, str)
                    or not patch.strip()
                    or not patch.lstrip().startswith("diff --git ")
                ):
                    errors.append(f"{field}.patch must be a non-empty unified Git patch")
                    patch = ""
                normalized["patch"] = patch
        validate_allowlisted_test_selectors(
            selector_argv,
            str(normalized["framework"]),
            normalized["test_ids"],
            f"{field}.argv",
            errors,
        )
        normalized_groups.append(normalized)
    missing = sorted(set(required_by_behavior) - set(seen_behaviors))
    if missing:
        errors.append("proof groups do not cover behavior ids: " + ", ".join(missing))
    duplicate_behaviors = sorted(
        behavior_id
        for behavior_id in set(seen_behaviors)
        if seen_behaviors.count(behavior_id) > 1
    )
    if duplicate_behaviors:
        errors.append(
            "proof groups duplicate behavior ids: " + ", ".join(duplicate_behaviors)
        )
    if errors:
        raise RuntimeProblem(
            "test proof spec does not match the frozen requirements",
            result="NEEDS_USER",
            code="TEST_PROOF_SPEC_INVALID",
            details={"errors": errors},
            exit_code=EXIT_FAIL,
        )
    return {"schema_version": 1, "groups": normalized_groups}


def proof_manifest(
    repo: Path,
    head: str,
    patterns: Sequence[str],
) -> tuple[list[dict[str, str]], str]:
    try:
        entries = evidence_contract.tree_entries(repo, head, patterns)
    except RuntimeError as exc:
        raise RuntimeProblem(
            "cannot compute Tester-owned proof manifest",
            code="TEST_PROOF_MANIFEST_ERROR",
            details={"head": head, "error": str(exc)},
        ) from exc
    return entries, evidence_contract.canonical_digest(entries)


def proof_test_source_path(
    repo: Path,
    tester_head: str,
    framework: str,
    test_id: str,
    tester_patterns: Sequence[str],
) -> str | None:
    normalized = normalize_proof_test_id(test_id)
    candidates: list[str] = []
    if framework == "pytest":
        candidates.append(normalized.split("::", 1)[0])
    elif framework == "unittest":
        parts = normalized.split(".")
        candidates.extend(
            "/".join(parts[:index]) + ".py"
            for index in range(len(parts), 0, -1)
        )
    for path in candidates:
        if (
            not path
            or Path(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or not path_allowed(path, tester_patterns)
        ):
            continue
        entry = git(repo, "ls-tree", tester_head, "--", path, check=False).stdout.strip()
        fields = entry.split(None, 3)
        if len(fields) >= 3 and fields[0] in {"100644", "100755"} and fields[1] == "blob":
            return path
    return None


def validate_proof_test_sources(
    repo: Path,
    tester_head: str,
    tester_patterns: Sequence[str],
    groups: Sequence[dict[str, Any]],
) -> None:
    errors: list[str] = []
    for index, group in enumerate(groups):
        framework = str(group["framework"])
        if framework not in {"unittest", "pytest"}:
            continue
        for test_id in group["test_ids"]:
            if proof_test_source_path(
                repo, tester_head, framework, str(test_id), tester_patterns
            ) is None:
                errors.append(
                    f"groups[{index}].test_ids is not bound to a Tester-owned "
                    f"regular file at {tester_head}: {test_id}"
                )
    if errors:
        raise RuntimeProblem(
            "proof test ids are outside the frozen Tester source manifest",
            result="NEEDS_USER",
            code="TEST_PROOF_SPEC_INVALID",
            details={"errors": errors},
            exit_code=EXIT_FAIL,
        )


def add_detached_worktree(repo: Path, path: Path, head: str) -> None:
    if path.exists():
        raise RuntimeProblem(
            "test proof worktree path already exists",
            code="TEST_PROOF_WORKTREE_EXISTS",
            details={"path": str(path)},
        )
    result = git(repo, "worktree", "add", "--detach", str(path), head, check=False)
    if result.returncode != 0:
        raise RuntimeProblem(
            "cannot create isolated test proof worktree",
            code="TEST_PROOF_WORKTREE_CREATE_FAILED",
            details={
                "path": str(path),
                "head": head,
                "stdout": tail_text(result.stdout),
                "stderr": tail_text(result.stderr),
            },
        )


def remove_proof_worktrees(repo: Path, paths: Sequence[Path]) -> None:
    failures: list[dict[str, str]] = []
    for path in reversed(paths):
        if not path.exists():
            continue
        result = git(repo, "worktree", "remove", "--force", str(path), check=False)
        if result.returncode != 0:
            failures.append({"path": str(path), "stderr": tail_text(result.stderr)})
    if failures:
        raise RuntimeProblem(
            "isolated test proof worktree cleanup failed",
            result="CONTINUITY_FAILURE",
            code="TEST_PROOF_WORKTREE_CLEANUP_FAILED",
            details={"failures": failures},
            exit_code=EXIT_FAIL,
        )


def normalize_proof_test_id(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def exact_declared_test_matches(
    test_ids: Sequence[str], reported_ids: Iterable[str]
) -> tuple[list[str], bool]:
    declared: dict[str, list[str]] = {}
    for test_id in test_ids:
        declared.setdefault(normalize_proof_test_id(test_id), []).append(test_id)
    reported = {normalize_proof_test_id(item) for item in reported_ids if item.strip()}
    matched = sorted(
        originals[0]
        for normalized, originals in declared.items()
        if normalized in reported and len(originals) == 1
    )
    fully_mapped = all(
        normalized in declared and len(declared[normalized]) == 1
        for normalized in reported
    )
    return matched, fully_mapped


def unittest_failure_ids(output: str) -> set[str]:
    reported: set[str] = set()
    for line in output.splitlines():
        match = re.match(
            r"^FAIL:\s+(?:[^\s(]+\s+\(([^)]+)\)|([^\s(]+))\s*$",
            line.strip(),
        )
        if match:
            reported.add(match.group(1) or match.group(2))
    return reported


def pytest_failure_ids(output: str) -> set[str]:
    return set(pytest_failure_reasons(output))


def pytest_failure_reasons(output: str) -> dict[str, str]:
    reported: dict[str, str] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            raw = stripped[len("FAILED ") :]
            node_id, separator, reason = raw.partition(" - ")
            node_id = node_id.strip()
            if ".py::" in node_id:
                reported[node_id] = reason.strip() if separator else ""
    return reported


def unittest_pass_ids(output: str) -> set[str]:
    reported: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"^[^\s(]+\s+\(([^)]+)\)\s+\.\.\.\s+ok\s*$", line.strip())
        if match:
            reported.add(match.group(1))
    return reported


def pytest_pass_ids(output: str) -> set[str]:
    reported: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        marker = " PASSED"
        if marker in stripped:
            node_id = stripped.split(marker, 1)[0].strip()
            if ".py::" in node_id:
                reported.add(node_id)
    return reported


def declared_tests_all_passed(
    output: str, framework: str, test_ids: Sequence[str]
) -> bool:
    reported = unittest_pass_ids(output) if framework == "unittest" else pytest_pass_ids(output)
    matched, _ = exact_declared_test_matches(test_ids, reported)
    return set(matched) == set(test_ids)


def pytest_declared_tests_passed_cleanly(
    output: str, counts: Mapping[str, int], test_ids: Sequence[str]
) -> bool:
    declared = {normalize_proof_test_id(item) for item in test_ids}
    disallowed = (
        "failed",
        "errors",
        "skipped",
        "xfailed",
        "xpassed",
        "deselected",
    )
    return (
        counts.get("passed", 0) == len(declared)
        and all(counts.get(label, 0) == 0 for label in disallowed)
        and declared_tests_all_passed(output, "pytest", test_ids)
    )


def classify_text_proof_test_result(
    output: str,
    *,
    framework: str,
    returncode: int,
    timed_out: bool,
    test_ids: Sequence[str],
    launch_error: bool = False,
) -> dict[str, Any]:
    lowered = output.lower()
    unittest_runs = list(re.finditer(r"Ran\s+(\d+)\s+tests?\b", output))
    selected_framework = framework
    if selected_framework == "auto":
        selected_framework = "unittest" if unittest_runs else "pytest"
    if launch_error:
        return {
            "framework": selected_framework,
            "classification": "launch-error",
            "counts": {},
        }
    if timed_out:
        return {
            "framework": selected_framework,
            "classification": "timeout",
            "counts": {},
        }

    syntax_error = bool(re.search(r"\bSyntaxError\b|invalid syntax", output))
    import_error = bool(
        re.search(
            r"\b(?:ImportError|ModuleNotFoundError)\b|"
            r"Failed to import test module|import file mismatch",
            output,
        )
    )
    configuration_error = bool(
        re.search(
            r"(?:unknown config option|error loading plugin|"
            r"could not load initial conftests?|"
            r"(?:error|failed).*?(?:pytest\.ini|pyproject\.toml|"
            r"setup\.cfg|tox\.ini|configuration|config file)|"
            r"error:\s+.*?\.ini:\d+:\s+unexpected line)",
            lowered,
        )
    )

    if selected_framework == "unittest":
        if syntax_error:
            return {
                "framework": "unittest",
                "classification": "syntax-error",
                "counts": {},
            }
        if import_error:
            return {
                "framework": "unittest",
                "classification": "import-error",
                "counts": {},
            }
        if not unittest_runs:
            return {
                "framework": "unittest",
                "classification": (
                    "usage-error"
                    if "usage:" in lowered
                    else "zero-effective-tests"
                    if returncode == 0
                    else "unclassified-failure"
                ),
                "counts": {},
            }
        tests = int(unittest_runs[-1].group(1))
        counts = {"tests": tests, "failures": 0, "errors": 0}
        failed = list(re.finditer(r"FAILED\s*\(([^)]*)\)", output))
        if failed:
            for key, raw_count in re.findall(
                r"([A-Za-z_ ]+)=(\d+)", failed[-1].group(1)
            ):
                normalized_key = key.strip().lower().replace(" ", "_")
                counts[normalized_key] = int(raw_count)
        if tests == 0:
            classification = "zero-tests"
        elif (
            returncode == 0
            and re.search(r"(?m)^OK(?:\s|$)", output)
            and declared_tests_all_passed(output, "unittest", test_ids)
        ):
            classification = "pass"
        elif counts.get("failures", 0) > 0 and counts.get("errors", 0) == 0:
            classification = "assertion-failure"
        elif counts.get("errors", 0) > 0:
            classification = "non-assertion-test-failure"
        else:
            classification = "unclassified-failure"
        result = {
            "framework": "unittest",
            "classification": classification,
            "counts": counts,
        }
        if classification == "assertion-failure":
            matched, fully_mapped = exact_declared_test_matches(
                test_ids, unittest_failure_ids(output)
            )
            result["matched_test_ids"] = matched
            if not matched or not fully_mapped:
                result["classification"] = "unmapped-assertion-failure"
        return result

    counts: dict[str, int] = {}
    for raw_count, label in re.findall(
        r"(\d+)\s+(failed|passed|errors?|skipped|xfailed|xpassed|deselected)\b",
        lowered,
    ):
        key = "errors" if label in {"error", "errors"} else label
        counts[key] = int(raw_count)
    if syntax_error:
        classification = "syntax-error"
    elif import_error:
        classification = "import-error"
    elif configuration_error:
        classification = "configuration-error"
    elif returncode == 5 or "no tests ran" in lowered:
        classification = "zero-tests"
    elif returncode == 4 or "usageerror" in lowered:
        classification = "usage-error"
    elif returncode == 3 or "internalerror" in lowered:
        classification = "unclassified-failure"
    elif (
        returncode == 2
        or "error collecting" in lowered
        or "error during collection" in lowered
        or "errors during collection" in lowered
    ):
        classification = "collection-error"
    elif counts.get("errors", 0) > 0:
        classification = "non-assertion-test-failure"
    elif (
        returncode == 0
        and counts.get("passed", 0) > 0
        and pytest_declared_tests_passed_cleanly(output, counts, test_ids)
    ):
        classification = "pass"
    elif returncode != 0 and counts.get("failed", 0) > 0:
        failure_reasons = pytest_failure_reasons(output)
        assertion_signal = bool(failure_reasons) and all(
            re.match(r"(?:AssertionError\b|assert\b|Failed:)", reason)
            for reason in failure_reasons.values()
        )
        classification = (
            "assertion-failure" if assertion_signal else "non-assertion-test-failure"
        )
    elif returncode == 0:
        classification = "zero-effective-tests"
    else:
        classification = "unclassified-failure"
    result = {
        "framework": "pytest",
        "classification": classification,
        "counts": counts,
    }
    if classification == "assertion-failure":
        matched, fully_mapped = exact_declared_test_matches(
            test_ids, pytest_failure_ids(output)
        )
        result["matched_test_ids"] = matched
        if not matched or not fully_mapped:
            result["classification"] = "unmapped-assertion-failure"
    return result


PROOF_FINAL_FD_ENV = "CODEX_BUILDER_PROOF_FINAL_FD"
PROOF_RAW_FD_ENV = "CODEX_BUILDER_PROOF_RAW_FD"
PROOF_SUPERVISOR_ENV = "CODEX_BUILDER_INTERNAL_PROOF_SUPERVISOR"

TRUSTED_PROOF_INHERITED_ENV = (
    "HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)


def trusted_proof_environment() -> dict[str, str]:
    environment = {
        key: value
        for key in TRUSTED_PROOF_INHERITED_ENV
        if (value := os.environ.get(key))
    }
    environment.update(
        {
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    [
                        str(Path(sys.executable).resolve().parent),
                        *[item for item in os.defpath.split(os.pathsep) if item],
                    ]
                )
            ),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }
    )
    return environment

PYTEST_PROOF_PLUGIN_SOURCE = r'''
from __future__ import annotations

import json
import os
import pytest


def pytest_configure(config):
    raw_fd = int(os.environ.pop("CODEX_BUILDER_PROOF_RAW_FD"))
    records = {}
    event_counts = {}
    count_keys = {
        "passed": "passed",
        "failed": "failed",
        "error": "errors",
        "skipped": "skipped",
        "xfailed": "xfailed",
        "xpassed": "xpassed",
    }
    priority = {
        "passed": 1,
        "failed:assertion": 3,
        "skipped": 4,
        "xfailed": 4,
        "xpassed": 4,
        "failed:non-assertion": 5,
        "error": 5,
    }

    def merge(node_id, outcome, failure_kind=""):
        count_key = count_keys[outcome]
        node_counts = event_counts.setdefault(node_id, {})
        node_counts[count_key] = node_counts.get(count_key, 0) + 1
        key = outcome + ((":" + failure_kind) if outcome == "failed" else "")
        current = records.get(node_id)
        if current is not None:
            current_key = current["outcome"] + (
                (":" + current.get("failure_kind", ""))
                if current["outcome"] == "failed"
                else ""
            )
            if priority[key] < priority[current_key]:
                return
        item = {"id": node_id, "outcome": outcome}
        if failure_kind:
            item["failure_kind"] = failure_kind
        records[node_id] = item

    class Recorder:
        @pytest.hookimpl(hookwrapper=True, trylast=True)
        def pytest_runtest_makereport(self, item, call):
            outcome = yield
            report = outcome.get_result()
            if report.when == "setup":
                if report.skipped:
                    merge(report.nodeid, "skipped")
                elif report.failed:
                    merge(report.nodeid, "error", "non-assertion")
                return
            if report.when == "call":
                was_xfail = bool(getattr(report, "wasxfail", False))
                if report.passed:
                    merge(report.nodeid, "xpassed" if was_xfail else "passed")
                elif report.skipped:
                    merge(report.nodeid, "xfailed" if was_xfail else "skipped")
                elif report.failed:
                    exception_type = (
                        call.excinfo.type if call.excinfo is not None else None
                    )
                    assertion = (
                        isinstance(exception_type, type)
                        and (
                            issubclass(exception_type, AssertionError)
                            or exception_type.__name__ == "Failed"
                        )
                    )
                    merge(
                        report.nodeid,
                        "failed",
                        "assertion" if assertion else "non-assertion",
                    )
                return
            if report.when == "teardown":
                if report.skipped:
                    merge(report.nodeid, "skipped")
                elif report.failed:
                    merge(report.nodeid, "error", "non-assertion")

        @pytest.hookimpl(trylast=True)
        def pytest_sessionfinish(self, session, exitstatus):
            payload = {
                "schema_version": 1,
                "framework": "pytest",
                "exitstatus": int(exitstatus),
                "tests": [
                    {**records[node_id], "counts": event_counts[node_id]}
                    for node_id in sorted(records)
                ],
            }
            encoded = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            try:
                os.write(raw_fd, encoded)
            finally:
                os.close(raw_fd)

    config.pluginmanager.register(Recorder(), "codex-proof-recorder")
'''

UNITTEST_PROOF_CHILD_SOURCE = r'''
from __future__ import annotations

import json
import os
import sys
import unittest


def main():
    raw_fd = int(os.environ.pop("CODEX_BUILDER_PROOF_RAW_FD"))
    records = {}
    event_counts = {}
    count_keys = {
        "passed": "passed",
        "failed": "failures",
        "error": "errors",
        "skipped": "skipped",
        "xfailed": "xfailed",
        "xpassed": "xpassed",
    }
    priority = {
        "passed": 1,
        "failed:assertion": 3,
        "skipped": 4,
        "xfailed": 4,
        "xpassed": 4,
        "failed:non-assertion": 5,
        "error": 5,
    }

    def merge(test, outcome, failure_kind=""):
        node_id = test.id()
        count_key = count_keys[outcome]
        node_counts = event_counts.setdefault(node_id, {})
        node_counts[count_key] = node_counts.get(count_key, 0) + 1
        key = outcome + ((":" + failure_kind) if outcome == "failed" else "")
        current = records.get(node_id)
        if current is not None:
            current_key = current["outcome"] + (
                (":" + current.get("failure_kind", ""))
                if current["outcome"] == "failed"
                else ""
            )
            if priority[key] < priority[current_key]:
                return
        item = {"id": node_id, "outcome": outcome}
        if failure_kind:
            item["failure_kind"] = failure_kind
        records[node_id] = item

    class Result(unittest.TextTestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            merge(test, "passed")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            assertion = isinstance(err[0], type) and issubclass(
                err[0], AssertionError
            )
            merge(
                test,
                "failed",
                "assertion" if assertion else "non-assertion",
            )

        def addError(self, test, err):
            super().addError(test, err)
            merge(test, "error", "non-assertion")

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            merge(test, "skipped")

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            merge(test, "xfailed")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            merge(test, "xpassed")

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is None:
                return
            assertion = isinstance(err[0], type) and issubclass(
                err[0], AssertionError
            )
            merge(
                test,
                "failed" if assertion else "error",
                "assertion" if assertion else "non-assertion",
            )

    class Runner(unittest.TextTestRunner):
        resultclass = Result

    cwd = os.getcwd()
    if not sys.path or sys.path[0] != cwd:
        sys.path.insert(0, cwd)
    exitstatus = 2
    try:
        program = unittest.main(
            module=None,
            argv=["unittest", *sys.argv[1:]],
            testRunner=Runner,
            exit=False,
        )
        result = program.result
        exitstatus = 0 if result.wasSuccessful() else 1
    except SystemExit as exc:
        exitstatus = exc.code if isinstance(exc.code, int) else 2
    payload = {
        "schema_version": 1,
        "framework": "unittest",
        "exitstatus": exitstatus,
        "tests": [
            {**records[node_id], "counts": event_counts[node_id]}
            for node_id in sorted(records)
        ],
    }
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    try:
        os.write(raw_fd, encoded)
    finally:
        os.close(raw_fd)
    return exitstatus


raise SystemExit(main())
'''


def proof_supervisor_argv(argv: Sequence[str], framework: str) -> list[str]:
    return [
        str(Path(sys.executable).resolve()),
        "-B",
        "-m",
        "codex_builder_loop.cli",
        "--framework",
        framework,
        "--",
        *list(argv),
    ]


def proof_supervised_child_argv(
    argv: Sequence[str], framework: str
) -> list[str]:
    values = list(argv)
    if framework != "unittest":
        return values
    for index, item in enumerate(values[:-1]):
        if item == "-m" and values[index + 1] == "unittest":
            return [
                *values[:index],
                "-c",
                UNITTEST_PROOF_CHILD_SOURCE,
                *values[index + 2 :],
            ]
    return values


def parse_structured_proof_payloads(raw: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return []
        if not isinstance(value, dict):
            return []
        payloads.append(value)
    return payloads


def cmd_internal_proof_supervisor(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], int]:
    raw_final_fd = os.environ.pop(PROOF_FINAL_FD_ENV, "")
    try:
        final_fd = int(raw_final_fd)
    except ValueError as exc:
        raise RuntimeProblem(
            "internal proof supervisor is missing its final result channel",
            code="TEST_PROOF_SUPERVISOR_INVALID",
        ) from exc
    requested_argv = list(args.argv)
    if requested_argv[:1] == ["--"]:
        requested_argv = requested_argv[1:]
    if not requested_argv:
        os.close(final_fd)
        raise RuntimeProblem(
            "internal proof supervisor is missing the test command",
            code="TEST_PROOF_SUPERVISOR_INVALID",
        )

    child_argv = proof_supervised_child_argv(requested_argv, str(args.framework))
    child_env = trusted_proof_environment()
    cache_prefix = os.environ.get("PYTHONPYCACHEPREFIX")
    if cache_prefix:
        child_env["PYTHONPYCACHEPREFIX"] = cache_prefix

    completed: subprocess.CompletedProcess[str]
    structured_raw = ""
    with tempfile.TemporaryDirectory(prefix="codex-proof-plugin-") as plugin_dir:
        plugin_name = "_codex_proof_" + uuid.uuid4().hex
        Path(plugin_dir, plugin_name + ".py").write_text(
            PYTEST_PROOF_PLUGIN_SOURCE,
            encoding="utf-8",
        )
        with tempfile.TemporaryFile(mode="w+b") as raw_channel:
            child_env.update(
                {
                    PROOF_RAW_FD_ENV: str(raw_channel.fileno()),
                    "PYTHONPATH": plugin_dir,
                    "PYTEST_PLUGINS": plugin_name,
                }
            )
            try:
                completed = subprocess.run(
                    child_argv,
                    shell=False,
                    text=True,
                    env=child_env,
                    pass_fds=(raw_channel.fileno(),),
                    check=False,
                )
            except OSError as exc:
                completed = subprocess.CompletedProcess(
                    child_argv,
                    126,
                    stdout="",
                    stderr=f"{type(exc).__name__}: {exc}",
                )
                print(completed.stderr, file=sys.stderr)
            raw_channel.seek(0)
            structured_raw = raw_channel.read().decode(
                "utf-8", errors="replace"
            )

    payloads = parse_structured_proof_payloads(structured_raw)
    accepted_payload: dict[str, Any] | None = None
    if len(payloads) == 1:
        payload_exitstatus = payloads[0].get("exitstatus")
        if (
            not isinstance(payload_exitstatus, bool)
            and isinstance(payload_exitstatus, int)
            and payload_exitstatus == int(completed.returncode)
        ):
            accepted_payload = payloads[0]
    try:
        if accepted_payload is not None:
            encoded = (
                json.dumps(
                    accepted_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            os.write(final_fd, encoded)
    finally:
        os.close(final_fd)
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "message": "internal proof supervisor completed",
        "framework": args.framework,
        "structured_result": accepted_payload is not None,
    }, int(completed.returncode)


def internal_proof_supervisor_main(argv: Sequence[str]) -> int:
    parser = RuntimeArgumentParser(prog="codex-builder-loop-proof-supervisor")
    parser.add_argument(
        "--framework",
        choices=["auto", "pytest", "unittest"],
        required=True,
    )
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    try:
        args = parser.parse_args(argv)
        payload, exit_code = cmd_internal_proof_supervisor(args)
        return emit(payload, exit_code)
    except RuntimeProblem as exc:
        payload = {
            "status": exc.result,
            "code": exc.code,
            "message": str(exc),
        }
        if exc.details:
            payload.update(exc.details)
        return emit(payload, exc.exit_code)
    except Exception as exc:
        return emit(
            {
                "status": "FATAL",
                "code": "UNEXPECTED_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
            },
            EXIT_FATAL,
        )


def without_textual_positive(result: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(result)
    if sanitized.get("classification") in {
        "pass",
        "assertion-failure",
        "unmapped-assertion-failure",
    }:
        sanitized["classification"] = "unclassified-failure"
        sanitized.pop("matched_test_ids", None)
    return sanitized


def classify_structured_proof_test_result(
    payloads: Sequence[Mapping[str, Any]],
    *,
    framework: str,
    returncode: int,
    test_ids: Sequence[str],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    if len(payloads) != 1:
        return without_textual_positive(fallback)
    payload = payloads[0]
    payload_framework = payload.get("framework")
    payload_exitstatus = payload.get("exitstatus")
    raw_tests = payload.get("tests")
    if (
        payload.get("schema_version") != 1
        or payload_framework not in {"unittest", "pytest"}
        or (framework != "auto" and payload_framework != framework)
        or isinstance(payload_exitstatus, bool)
        or not isinstance(payload_exitstatus, int)
        or payload_exitstatus != returncode
        or not isinstance(raw_tests, list)
    ):
        return without_textual_positive(fallback)

    declared: dict[str, str] = {}
    for test_id in test_ids:
        normalized = normalize_proof_test_id(test_id)
        if normalized in declared:
            return without_textual_positive(fallback)
        declared[normalized] = test_id

    allowed_outcomes = {
        "passed",
        "failed",
        "error",
        "skipped",
        "xfailed",
        "xpassed",
    }
    count_keys = {
        "passed": "passed",
        "failed": "failed",
        "error": "errors",
        "skipped": "skipped",
        "xfailed": "xfailed",
        "xpassed": "xpassed",
    }
    allowed_count_keys = {*count_keys.values(), "failures"}
    records: dict[str, dict[str, Any]] = {}
    for raw_item in raw_tests:
        if not isinstance(raw_item, dict):
            return without_textual_positive(fallback)
        raw_id = raw_item.get("id")
        outcome = raw_item.get("outcome")
        failure_kind = raw_item.get("failure_kind", "")
        raw_counts = raw_item.get("counts")
        if (
            not isinstance(raw_id, str)
            or not raw_id.strip()
            or outcome not in allowed_outcomes
            or failure_kind not in {"", "assertion", "non-assertion"}
            or not isinstance(raw_counts, dict)
            or not raw_counts
        ):
            return without_textual_positive(fallback)
        item_counts: dict[str, int] = {}
        for key, value in raw_counts.items():
            if (
                key not in allowed_count_keys
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                return without_textual_positive(fallback)
            item_counts[str(key)] = value
        expected_count_key = (
            "failures"
            if payload_framework == "unittest" and outcome == "failed"
            else count_keys[str(outcome)]
        )
        if item_counts.get(expected_count_key, 0) == 0:
            return without_textual_positive(fallback)
        normalized = normalize_proof_test_id(raw_id)
        if normalized in records:
            return without_textual_positive(fallback)
        records[normalized] = {
            "id": raw_id,
            "outcome": str(outcome),
            "failure_kind": str(failure_kind),
            "counts": item_counts,
        }

    counts: dict[str, int] = {}
    for item in records.values():
        for key, value in item["counts"].items():
            counts[key] = counts.get(key, 0) + value
    base = {"framework": payload_framework, "counts": counts}

    if set(records) != set(declared):
        return without_textual_positive(fallback)
    outcomes = [item["outcome"] for item in records.values()]
    if returncode == 0:
        non_pass_count = sum(
            value for key, value in counts.items() if key != "passed"
        )
        return {
            **base,
            "classification": (
                "pass"
                if (
                    outcomes
                    and all(outcome == "passed" for outcome in outcomes)
                    and counts.get("passed", 0) == len(declared)
                    and non_pass_count == 0
                )
                else "zero-effective-tests"
            ),
        }

    failed = [
        normalized
        for normalized, item in records.items()
        if item["outcome"] == "failed"
    ]
    non_assertion = counts.get("errors", 0) > 0 or any(
        item["outcome"] == "error"
        or (
            item["outcome"] == "failed"
            and item["failure_kind"] != "assertion"
        )
        or item["outcome"] in {"skipped", "xfailed", "xpassed"}
        for item in records.values()
    )
    if failed and not non_assertion:
        return {
            **base,
            "classification": "assertion-failure",
            "matched_test_ids": sorted(declared[item] for item in failed),
        }
    if non_assertion:
        return {**base, "classification": "non-assertion-test-failure"}
    return without_textual_positive(fallback)


def classify_proof_test_result(
    output: str,
    *,
    framework: str,
    returncode: int,
    timed_out: bool,
    test_ids: Sequence[str],
    structured_payloads: Sequence[Mapping[str, Any]],
    launch_error: bool = False,
) -> dict[str, Any]:
    fallback = classify_text_proof_test_result(
        output,
        framework=framework,
        returncode=returncode,
        timed_out=timed_out,
        test_ids=test_ids,
        launch_error=launch_error,
    )
    if launch_error or timed_out:
        return fallback
    return classify_structured_proof_test_result(
        structured_payloads,
        framework=framework,
        returncode=returncode,
        test_ids=test_ids,
        fallback=fallback,
    )


def run_proof_argv(
    argv: Sequence[str],
    *,
    framework: str,
    test_ids: Sequence[str],
    worktree: Path,
    timeout: int,
    log_path: Path,
    cache_path: Path,
) -> dict[str, Any]:
    started = time.monotonic()
    timed_out = False
    launch_error = False
    requested_argv = list(argv)
    actual_argv = proof_supervisor_argv(requested_argv, framework)
    proof_env = trusted_proof_environment()
    structured_raw = ""
    with tempfile.TemporaryFile(mode="w+b") as result_channel:
        proof_env.update(
            {
                PROOF_FINAL_FD_ENV: str(result_channel.fileno()),
                PROOF_SUPERVISOR_ENV: "1",
                "PYTHONPYCACHEPREFIX": str(cache_path),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            }
        )
        try:
            completed = subprocess.run(
                actual_argv,
                cwd=worktree,
                shell=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=proof_env,
                pass_fds=(result_channel.fileno(),),
                check=False,
            )
        except OSError as exc:
            completed = subprocess.CompletedProcess(
                actual_argv,
                126,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
            )
            launch_error = True
        except subprocess.TimeoutExpired as exc:
            completed = subprocess.CompletedProcess(
                actual_argv,
                124,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            )
            timed_out = True
        result_channel.seek(0)
        structured_raw = result_channel.read().decode("utf-8", errors="replace")
    duration_ms = int((time.monotonic() - started) * 1000)
    output = (
        f"$ {shlex.join(requested_argv)}\n"
        f"[returncode={completed.returncode} timeout={str(timed_out).lower()}]\n"
        f"{completed.stdout}{completed.stderr}"
    )
    test_result = classify_proof_test_result(
        f"{completed.stdout}{completed.stderr}",
        framework=framework,
        returncode=int(completed.returncode),
        timed_out=timed_out,
        test_ids=test_ids,
        structured_payloads=parse_structured_proof_payloads(structured_raw),
        launch_error=launch_error,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    return {
        "argv": requested_argv,
        "returncode": int(completed.returncode),
        "timed_out": timed_out,
        "test_result": test_result,
        "duration_ms": duration_ms,
        "log_path": str(log_path),
        "log_sha256": sha256_text(output),
        "log_tail": tail_text(output),
    }


def proof_worktree_residue(
    worktree: Path, *, allowed_tracked_paths: Sequence[str] = ()
) -> list[str]:
    allowed = set(allowed_tracked_paths)
    return sorted(path for path in worktree_residue(worktree) if path not in allowed)


def validate_mutation_paths(
    worktree: Path,
    candidate: str,
    ledger: dict[str, Any],
) -> list[str]:
    changed = git_changed_paths(worktree, candidate)
    if not changed:
        raise RuntimeProblem(
            "mutation patch did not change any files",
            result="NEEDS_USER",
            code="TEST_PROOF_SPEC_INVALID",
            details={"errors": ["mutation patch did not change any files"]},
            exit_code=EXIT_FAIL,
        )
    builder_patterns = list(ledger.get("plan", {}).get("builder_write", []))
    tester_patterns = list(ledger.get("plan", {}).get("tester_write", []))
    protected = (
        set(ledger.get("plan", {}).get("support_paths", []))
        | set(ledger.get("loop_config", {}).get("runner_paths", []))
        | PROTECTED_RUNTIME_PATHS
    )
    violations: list[dict[str, str]] = []
    for path in changed:
        if (
            not path_allowed(path, builder_patterns)
            or path_allowed(path, tester_patterns)
            or path in protected
        ):
            violations.append({"path": path, "reason": "ownership or control path"})
            continue
        entry = git(worktree, "ls-tree", candidate, "--", path, check=False).stdout.strip()
        if not entry:
            violations.append({"path": path, "reason": "path was not a candidate file"})
            continue
        mode = entry.split(None, 1)[0]
        target = worktree / path
        try:
            target_mode = os.lstat(target).st_mode
        except OSError:
            violations.append({"path": path, "reason": "mutation removed the file"})
            continue
        if mode not in {"100644", "100755"} or not stat.S_ISREG(target_mode):
            violations.append({"path": path, "reason": "path is not a regular file"})
    if violations:
        raise RuntimeProblem(
            "mutation patch escaped Builder-owned regular implementation files",
            result="NEEDS_USER",
            code="TEST_PROOF_SPEC_INVALID",
            details={
                "errors": [
                    "mutation patch escaped Builder-owned regular implementation files: "
                    + json.dumps(violations, ensure_ascii=False, sort_keys=True)
                ]
            },
            exit_code=EXIT_FAIL,
        )
    return changed


def proof_failure_details(
    index: int,
    group: dict[str, Any],
    result: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "group": index,
        "execution_argv": list(group["execution_argv"]),
        "executable_identity": group["executable_identity"],
        "test_ids": list(group["test_ids"]),
        "result": result,
        **extra,
    }


def cmd_prove_tests(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    raw_spec = read_json_input(str(args.spec))
    with locked_run(repo, run_id) as ledger:
        reject_during_finalize_intent(ledger, "prove-tests")
        if ledger.get("phase") != "active":
            raise RuntimeProblem(
                "test proof requires an active run",
                result="NEEDS_USER",
                code="PHASE_NOT_ACTIVE",
                details={"phase": ledger.get("phase")},
                exit_code=EXIT_FAIL,
            )
        if not requires_test_effectiveness(ledger):
            return {
                "status": "NOOP",
                "message": "test proof is not applicable to this run",
                "code": "TEST_PROOF_NOT_APPLICABLE",
                "run_id": run_id,
                "contract_schema_version": ledger.get("plan", {}).get(
                    "contract_schema_version"
                ),
                "level": ledger.get("plan", {}).get("level"),
            }, EXIT_PASS
        contract = verify_plan_unchanged(ledger)
        spec = normalize_test_proof_spec(
            raw_spec,
            contract.test_effectiveness_requirements,
            repo=repo,
            spec_head=str(ledger["spec_head"]),
            contract=contract,
        )
        spec_digest = evidence_contract.canonical_digest(spec)
        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        if worktree_residue(builder):
            raise RuntimeProblem(
                "candidate worktree is dirty",
                result="NEEDS_USER",
                code="CANDIDATE_DIRTY",
                exit_code=EXIT_FAIL,
            )
        candidate = full_head(builder)
        integration = ledger.get("tester_integration", {})
        tester_source_head = integration.get("source_head")
        if (
            integration.get("completed") is not True
            or not isinstance(tester_source_head, str)
            or not tester_source_head
            or not integration.get("author_turn_id")
        ):
            raise RuntimeProblem(
                "test proof requires completed Tester author integration",
                result="NEEDS_USER",
                code="TEST_PROOF_INTEGRATION_MISSING",
                details={"tester_integration": integration},
                exit_code=EXIT_FAIL,
            )
        if (
            not strong_test_proof_precedes_machine(ledger)
            and evidence_head(ledger, "machine") != candidate
        ):
            raise RuntimeProblem(
                "test proof requires current machine verification",
                result="NEEDS_USER",
                code="TEST_PROOF_MACHINE_MISSING",
                details={
                    "candidate_head": candidate,
                    "verified_head": evidence_head(ledger, "machine"),
                },
                exit_code=EXIT_FAIL,
            )
        existing = evidence_record(ledger, "test_effectiveness")
        if existing is not None and evidence_head(
            ledger, "test_effectiveness"
        ) == candidate:
            previous_digest = existing.get("provenance", {}).get("spec_sha256")
            if previous_digest == spec_digest:
                return {
                    "status": "NOOP",
                    "message": "the same test-effectiveness proof is already recorded",
                    "run_id": run_id,
                    "head": candidate,
                    "test_effectiveness_head": candidate,
                    "evidence": existing,
                }, EXIT_PASS
            raise RuntimeProblem(
                "a different test proof is already frozen for this candidate",
                result="NEEDS_USER",
                code="TEST_PROOF_ALREADY_RECORDED",
                exit_code=EXIT_FAIL,
            )

        tester_patterns = list(ledger.get("plan", {}).get("tester_write", []))
        source_manifest, source_manifest_sha = proof_manifest(
            repo, tester_source_head, tester_patterns
        )
        validate_proof_test_sources(
            repo, tester_source_head, tester_patterns, spec["groups"]
        )
        candidate_manifest, candidate_manifest_sha = proof_manifest(
            repo, candidate, tester_patterns
        )
        if (
            source_manifest_sha != candidate_manifest_sha
            or source_manifest != candidate_manifest
        ):
            raise RuntimeProblem(
                "Tester-owned manifest differs between author source and candidate",
                result="FAIL",
                code="TEST_PROOF_MANIFEST_MISMATCH",
                details={
                    "tester_source_head": tester_source_head,
                    "candidate_head": candidate,
                    "source_manifest_sha256": source_manifest_sha,
                    "candidate_manifest_sha256": candidate_manifest_sha,
                },
                exit_code=EXIT_FAIL,
            )

        proof_root = (
            state_root(repo)
            / "worktrees"
            / run_id
            / ("proof-" + uuid.uuid4().hex)
        )
        evidence_dir = (
            run_dir(repo, run_id)
            / "evidence"
            / "test-effectiveness"
            / candidate
            / spec_digest[:16]
        )
        created: list[Path] = []
        group_results: list[dict[str, Any]] = []
        failure: RuntimeProblem | None = None
        try:
            for index, group in enumerate(spec["groups"]):
                method = str(group["method"])
                result: dict[str, Any] = {
                    "behavior_ids": list(group["behavior_ids"]),
                    "method": method,
                    "argv": list(group["argv"]),
                    "execution_argv": list(group["execution_argv"]),
                    "framework": group["framework"],
                    "executable_identity": group["executable_identity"],
                    "test_ids": group["test_ids"],
                    "timeout_seconds": group["timeout_seconds"],
                }
                if method == "reviewed-boundaries":
                    result["reason"] = group["reason"]
                    result["reviewed_boundaries"] = group[
                        "reviewed_boundaries"
                    ]
                    result["machine_evidence_head"] = candidate
                    group_results.append(result)
                    continue

                candidate_path = proof_root / f"{index + 1:02d}-candidate"
                add_detached_worktree(repo, candidate_path, candidate)
                created.append(candidate_path)
                candidate_run = run_proof_argv(
                    group["execution_argv"],
                    framework=str(group["framework"]),
                    test_ids=group["test_ids"],
                    worktree=candidate_path,
                    timeout=int(group["timeout_seconds"]),
                    log_path=evidence_dir / f"{index + 1:02d}-{method}-candidate.log",
                    cache_path=evidence_dir / "pycache" / f"{index + 1:02d}-candidate",
                )
                candidate_run["worktree_residue"] = proof_worktree_residue(
                    candidate_path
                )
                if (
                    candidate_run["timed_out"]
                    or candidate_run["returncode"] != 0
                    or candidate_run["test_result"].get("classification") != "pass"
                    or full_head(candidate_path) != candidate
                    or candidate_run["worktree_residue"]
                ):
                    raise RuntimeProblem(
                        "test proof candidate command did not pass cleanly",
                        result="FAIL",
                        code="TEST_PROOF_CANDIDATE_FAILED",
                        details=proof_failure_details(
                            index, group, candidate_run
                        ),
                        exit_code=EXIT_FAIL,
                    )
                result["candidate"] = candidate_run

                if method == "baseline-red":
                    result["claimed_failure_kind"] = group[
                        "claimed_failure_kind"
                    ]
                    baseline_path = proof_root / f"{index + 1:02d}-baseline"
                    add_detached_worktree(repo, baseline_path, tester_source_head)
                    created.append(baseline_path)
                    baseline_run = run_proof_argv(
                        group["execution_argv"],
                        framework=str(group["framework"]),
                        test_ids=group["test_ids"],
                        worktree=baseline_path,
                        timeout=int(group["timeout_seconds"]),
                        log_path=evidence_dir
                        / f"{index + 1:02d}-{method}-baseline.log",
                        cache_path=evidence_dir
                        / "pycache"
                        / f"{index + 1:02d}-baseline",
                    )
                    baseline_run["worktree_residue"] = proof_worktree_residue(
                        baseline_path
                    )
                    if (
                        baseline_run["timed_out"]
                        or baseline_run["returncode"] == 0
                        or baseline_run["test_result"].get("classification")
                        != group["claimed_failure_kind"]
                        or full_head(baseline_path) != tester_source_head
                        or baseline_run["worktree_residue"]
                    ):
                        raise RuntimeProblem(
                            "baseline-red did not distinguish baseline from candidate",
                            result="FAIL",
                            code="TEST_BASELINE_RED_NOT_PROVEN",
                            details=proof_failure_details(
                                index, group, baseline_run
                            ),
                            exit_code=EXIT_FAIL,
                        )
                    result["baseline"] = baseline_run
                else:
                    mutation_path = proof_root / f"{index + 1:02d}-mutation"
                    add_detached_worktree(repo, mutation_path, candidate)
                    created.append(mutation_path)
                    patch_result = run_process(
                        ["git", "-C", str(mutation_path), "apply", "--check", "-"],
                        input_text=str(group["patch"]),
                        check=False,
                    )
                    if patch_result.returncode != 0:
                        raise RuntimeProblem(
                            "mutation patch does not apply to the candidate",
                            result="NEEDS_USER",
                            code="TEST_PROOF_SPEC_INVALID",
                            details={
                                "errors": [
                                    "mutation patch does not apply to the candidate: "
                                    + tail_text(patch_result.stderr)
                                ]
                            },
                            exit_code=EXIT_FAIL,
                        )
                    applied = run_process(
                        ["git", "-C", str(mutation_path), "apply", "-"],
                        input_text=str(group["patch"]),
                        check=False,
                    )
                    if applied.returncode != 0:
                        raise RuntimeProblem(
                            "mutation patch could not be applied",
                            result="NEEDS_USER",
                            code="TEST_PROOF_SPEC_INVALID",
                            details={
                                "errors": [
                                    "mutation patch could not be applied: "
                                    + tail_text(applied.stderr)
                                ]
                            },
                            exit_code=EXIT_FAIL,
                        )
                    changed_paths = validate_mutation_paths(
                        mutation_path, candidate, ledger
                    )
                    mutation_diff = git(
                        mutation_path,
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--binary",
                        "--full-index",
                        candidate,
                        "--",
                        check=True,
                    ).stdout
                    mutation_diff_sha = sha256_text(mutation_diff)
                    mutation_run = run_proof_argv(
                        group["execution_argv"],
                        framework=str(group["framework"]),
                        test_ids=group["test_ids"],
                        worktree=mutation_path,
                        timeout=int(group["timeout_seconds"]),
                        log_path=evidence_dir
                        / f"{index + 1:02d}-{method}-mutation.log",
                        cache_path=evidence_dir
                        / "pycache"
                        / f"{index + 1:02d}-mutation",
                    )
                    mutation_run["worktree_residue"] = proof_worktree_residue(
                        mutation_path, allowed_tracked_paths=changed_paths
                    )
                    mutation_after_paths = git_changed_paths(
                        mutation_path, candidate
                    )
                    mutation_after_diff = git(
                        mutation_path,
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--binary",
                        "--full-index",
                        candidate,
                        "--",
                        check=True,
                    ).stdout
                    mutation_after_sha = sha256_text(mutation_after_diff)
                    if (
                        mutation_run["timed_out"]
                        or mutation_run["returncode"] == 0
                        or mutation_run["test_result"].get("classification")
                        != "assertion-failure"
                        or mutation_run["worktree_residue"]
                        or mutation_after_paths != changed_paths
                        or mutation_after_diff != mutation_diff
                        or mutation_after_sha != mutation_diff_sha
                    ):
                        raise RuntimeProblem(
                            "controlled mutation was not detected by the tests",
                            result="FAIL",
                            code="TEST_MUTATION_SURVIVED",
                            details=proof_failure_details(
                                index,
                                group,
                                mutation_run,
                                changed_paths_before=changed_paths,
                                changed_paths_after=mutation_after_paths,
                                diff_sha256_before=mutation_diff_sha,
                                diff_sha256_after=mutation_after_sha,
                            ),
                            exit_code=EXIT_FAIL,
                        )
                    result["mutation"] = {
                        **mutation_run,
                        "patch_sha256": sha256_text(str(group["patch"])),
                        "applied_diff": mutation_diff,
                        "applied_diff_sha256": mutation_diff_sha,
                        "changed_paths": changed_paths,
                        "head_before": candidate,
                        "head_after": full_head(mutation_path),
                    }
                group_results.append(result)
        except RuntimeProblem as exc:
            failure = exc
        finally:
            try:
                remove_proof_worktrees(repo, created)
            except RuntimeProblem as cleanup_error:
                ledger["phase"] = "continuity_failure"
                append_event(
                    ledger,
                    "test_proof_worktree_cleanup_failed",
                    cleanup_error.details,
                )
                save_ledger(repo, ledger)
                raise
        if failure is not None:
            append_event(
                ledger,
                "test_effectiveness_proof_failed",
                {
                    "candidate_head": candidate,
                    "tester_source_head": tester_source_head,
                    "spec_sha256": spec_digest,
                    "code": failure.code,
                    "details": failure.details,
                },
            )
            save_ledger(repo, ledger)
            raise failure
        if full_head(builder) != candidate or worktree_residue(builder):
            raise RuntimeProblem(
                "live candidate changed while test proof was running",
                result="CONTINUITY_FAILURE",
                code="TEST_PROOF_CANDIDATE_DRIFT",
                exit_code=EXIT_FAIL,
            )
        if integration.get("source_head") != tester_source_head:
            raise RuntimeProblem(
                "Tester integration changed while test proof was running",
                result="CONTINUITY_FAILURE",
                code="TEST_PROOF_INTEGRATION_DRIFT",
                exit_code=EXIT_FAIL,
            )
        scope = ["**"]
        digest = evidence_contract.input_digest(
            repo,
            candidate,
            patterns=scope,
            plan_sha256=str(ledger.get("plan", {}).get("sha256") or ""),
            context={
                "kind": "test_effectiveness",
                "spec_sha256": spec_digest,
                "tester_source_head": tester_source_head,
                "tester_manifest_sha256": source_manifest_sha,
                **plan_digest_context(ledger),
            },
        )
        record = evidence_contract.make_record(
            kind="test_effectiveness",
            observed_head=candidate,
            accepted_head=candidate,
            input_sha256=digest,
            scope=scope,
            provenance={
                "spec_sha256": spec_digest,
                "groups": group_results,
            },
        )
        ledger.setdefault("evidence", {})["test_effectiveness"] = record
        append_event(
            ledger,
            "test_effectiveness_proven",
            {
                "candidate_head": candidate,
                "tester_source_head": tester_source_head,
                "tester_manifest_sha256": source_manifest_sha,
                "spec_sha256": spec_digest,
                "methods": [item["method"] for item in group_results],
            },
        )
        save_ledger(repo, ledger)
        return {
            "status": "READY",
            "message": "test-effectiveness requirements are proven on isolated inputs",
            "run_id": run_id,
            "head": candidate,
            "test_effectiveness_head": candidate,
            "tester_source_head": tester_source_head,
            "tester_manifest_sha256": source_manifest_sha,
            "spec_sha256": spec_digest,
            "groups": group_results,
            "evidence": record,
        }, EXIT_PASS


def parse_details(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def blackbox_report_schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "schema" / "codex-blackbox-report.schema.json"


def validate_blackbox_report_v2_schema(details: dict[str, Any]) -> None:
    path = blackbox_report_schema_path()
    if not path.is_file():
        raise RuntimeProblem(
            "blackbox report schema is unavailable",
            code="BLACKBOX_REPORT_SCHEMA_UNAVAILABLE",
            details={"schema_path": str(path)},
        )
    errors: list[str] = []
    required = {
        "schema_version",
        "candidate_worktree",
        "head_before",
        "head_after",
        "candidate_dirty",
        "executions",
    }
    allowed = required | {"normal_residue", "ignored_residue", "cases"}
    unknown = sorted(set(details) - allowed)
    missing = sorted(required - set(details))
    if unknown:
        errors.append("unknown fields: " + ", ".join(unknown))
    if missing:
        errors.append("missing fields: " + ", ".join(missing))
    if details.get("schema_version") != BLACKBOX_REPORT_SCHEMA_VERSION:
        errors.append("schema_version must be 2")
    if not isinstance(details.get("candidate_worktree"), str) or not str(
        details.get("candidate_worktree", "")
    ).strip():
        errors.append("candidate_worktree must be a non-empty string")
    for field in ("head_before", "head_after"):
        if not isinstance(details.get(field), str) or not re.fullmatch(
            r"[0-9a-f]{40}", str(details.get(field, ""))
        ):
            errors.append(f"{field} must be a full lowercase commit SHA")
    if details.get("candidate_dirty") is not False:
        errors.append("candidate_dirty must be false")
    for field in ("normal_residue", "ignored_residue"):
        if field in details and details.get(field) != []:
            errors.append(f"{field} must be an empty array")

    executions = details.get("executions")
    if not isinstance(executions, list) or not executions:
        errors.append("executions must be a non-empty array")
        executions = []
    def is_schema_integer(value: Any) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
        ) or (
            isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
        )

    for index, execution in enumerate(executions):
        field = f"executions[{index}]"
        if not isinstance(execution, dict):
            errors.append(f"{field} must be an object")
            continue
        method = execution.get("method")
        if method == "command":
            item_allowed = {
                "method",
                "command",
                "returncode",
                "duration_ms",
                "timed_out",
                "log_sha256",
            }
            item_required = {"method", "command", "returncode", "timed_out"}
        else:
            item_allowed = {"method", "reason", "duration_ms", "log_sha256"}
            item_required = {"method", "reason"}
        item_unknown = sorted(set(execution) - item_allowed)
        item_missing = sorted(item_required - set(execution))
        if item_unknown:
            errors.append(f"{field} has unknown fields: " + ", ".join(item_unknown))
        if item_missing:
            errors.append(f"{field} is missing fields: " + ", ".join(item_missing))
        if not isinstance(method, str) or not method.strip():
            errors.append(f"{field}.method must be a non-empty string")
        reason = execution.get("reason")
        if method == "command":
            command = execution.get("command")
            if not isinstance(command, str) or not command.strip():
                errors.append(f"{field}.command must be a non-empty string")
            if not is_schema_integer(execution.get("returncode")):
                errors.append(f"{field}.returncode must be an integer")
            if not isinstance(execution.get("timed_out"), bool):
                errors.append(f"{field}.timed_out must be boolean")
        elif not isinstance(reason, str) or not reason.strip():
            errors.append(f"{field}.method error requires a non-empty reason")
        if "duration_ms" in execution and (
            not is_schema_integer(execution.get("duration_ms"))
            or execution["duration_ms"] < 0
        ):
            errors.append(f"{field}.duration_ms must be a non-negative integer")
        if "log_sha256" in execution and (
            not isinstance(execution.get("log_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(execution["log_sha256"]))
        ):
            errors.append(f"{field}.log_sha256 must be a SHA-256 digest")

    cases = details.get("cases", [])
    if "cases" in details and not isinstance(cases, list):
        errors.append("cases must be an array")
        cases = []
    case_required = {"case_id", "mechanical", "verify", "quality", "outcome"}
    for index, case in enumerate(cases):
        field = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{field} must be an object")
            continue
        item_unknown = sorted(set(case) - case_required)
        item_missing = sorted(case_required - set(case))
        if item_unknown:
            errors.append(f"{field} has unknown fields: " + ", ".join(item_unknown))
        if item_missing:
            errors.append(f"{field} is missing fields: " + ", ".join(item_missing))
        if not isinstance(case.get("case_id"), str) or not re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", str(case.get("case_id", ""))
        ):
            errors.append(f"{field}.case_id is invalid")
        for dimension in ("mechanical", "verify", "quality"):
            value = case.get(dimension)
            dimension_field = f"{field}.{dimension}"
            if not isinstance(value, dict):
                errors.append(f"{dimension_field} must be an object")
                continue
            if set(value) != {"status", "observation"}:
                errors.append(
                    f"{dimension_field} must contain only status and observation"
                )
            if value.get("status") not in {"pass", "fail", "not_applicable"}:
                errors.append(f"{dimension_field}.status is invalid")
            if not isinstance(value.get("observation"), str) or not str(
                value.get("observation", "")
            ).strip():
                errors.append(
                    f"{dimension_field}.observation must be a non-empty string"
                )
        if case.get("outcome") not in {"pass", "fail"}:
            errors.append(f"{field}.outcome is invalid")
    if errors:
        raise RuntimeProblem(
            "blackbox report does not match schema v2",
            code="E2E_DETAILS_INVALID",
            details={
                "schema_path": str(path),
                "errors": errors,
            },
        )


def case_dimension_contract(expected: Mapping[str, Any]) -> dict[str, str]:
    level = str(expected.get("level"))
    if level == "fast":
        return {
            "mechanical": "required",
            "verify": "not_applicable",
            "quality": "not_applicable",
        }
    return {
        "mechanical": "required" if expected.get("hard_rules") else "not_applicable",
        "verify": "required",
        "quality": "required",
    }


def derived_blackbox_report_contract(
    ledger: Mapping[str, Any], contract: PlanContract
) -> dict[str, Any]:
    version = blackbox_report_schema_version(ledger)
    return {
        "schema_version": version,
        "schema_path": (
            "schema/codex-blackbox-report.schema.json"
            if version == BLACKBOX_REPORT_SCHEMA_VERSION
            else None
        ),
        "summary": (
            "record every real execution; accepted executions cover every frozen "
            "case while method_error executions remain visible but excluded"
            if version == BLACKBOX_REPORT_SCHEMA_VERSION
            else "legacy single-command report retained for this active run"
        ),
        "dimension_shape": (
            "status-observation-object"
            if version == BLACKBOX_REPORT_SCHEMA_VERSION
            else "legacy-status-string"
        ),
        "cases": [
            {
                "case_id": str(expected["id"]),
                "level": str(expected["level"]),
                **case_dimension_contract(expected),
            }
            for expected in contract.e2e_cases
        ],
    }


def dimension_status(
    value: Any,
    *,
    report_version: int,
    field: str,
    errors: list[str],
) -> str | None:
    if report_version == BLACKBOX_REPORT_SCHEMA_VERSION:
        if not isinstance(value, dict):
            errors.append(f"{field} must contain status and observation")
            return None
        status = value.get("status")
        observation = value.get("observation")
        if not isinstance(observation, str) or not observation.strip():
            errors.append(f"{field}.observation must be a non-empty string")
    else:
        status = value
    if not isinstance(status, str) or status not in {
        "pass",
        "fail",
        "not_applicable",
    }:
        errors.append(f"{field} status is invalid")
        return None
    return status


def validate_blackbox_execution_coverage(
    contract: PlanContract,
    details: dict[str, Any],
) -> None:
    executions = details.get("executions")
    if not isinstance(executions, list):
        return
    errors: list[str] = []
    accepted_count = 0
    for index, execution in enumerate(executions):
        if not isinstance(execution, dict):
            continue
        if execution.get("method") == "command":
            accepted_count += 1
            if execution.get("returncode") != 0:
                errors.append(f"executions[{index}] accepted returncode must be zero")
            if execution.get("timed_out", False) is not False:
                errors.append(f"executions[{index}] accepted execution must not time out")
    if accepted_count == 0:
        errors.append("blackbox report requires at least one accepted execution")
    if errors:
        raise RuntimeProblem(
            "blackbox executions do not cover the frozen cases",
            code="E2E_EXECUTION_COVERAGE_INVALID",
            details={"errors": errors, "required_case_ids": list(contract.e2e_case_ids)},
        )


def validate_structured_e2e_results(
    contract: PlanContract,
    details: dict[str, Any],
    *,
    report_version: int,
) -> None:
    if not contract.e2e_cases:
        raw_cases = details.get("cases", [])
        if raw_cases:
            raise RuntimeProblem(
                "blackbox report cannot introduce cases absent from the frozen plan",
                code="E2E_CASE_RESULTS_INVALID",
                details={"required_case_ids": [], "supplied_cases": raw_cases},
            )
        return
    raw_cases = details.get("cases")
    if not isinstance(raw_cases, list):
        raise RuntimeProblem(
            "structured blackbox evidence requires per-case results",
            code="E2E_CASE_RESULTS_INVALID",
            details={"required_case_ids": list(contract.e2e_case_ids)},
        )
    errors: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    seen: list[str] = []
    for index, item in enumerate(raw_cases):
        field = f"cases[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} must be a mapping")
            continue
        unknown = sorted(
            set(item)
            - {
                "case_id",
                "mechanical",
                "verify",
                "quality",
                "outcome",
                *(
                    {"level", "replay"}
                    if report_version == LEGACY_BLACKBOX_REPORT_SCHEMA_VERSION
                    else set()
                ),
            }
        )
        if unknown:
            errors.append(f"{field} has unknown fields: " + ", ".join(unknown))
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{field}.case_id must be a non-empty string")
            continue
        seen.append(case_id)
        by_id[case_id] = item
    duplicates = sorted(item for item in set(seen) if seen.count(item) > 1)
    missing = sorted(set(contract.e2e_case_ids) - set(seen))
    unknown_ids = sorted(set(seen) - set(contract.e2e_case_ids))
    if duplicates:
        errors.append("duplicate case ids: " + ", ".join(duplicates))
    if missing:
        errors.append("missing case ids: " + ", ".join(missing))
    if unknown_ids:
        errors.append("unknown case ids: " + ", ".join(unknown_ids))

    for expected in contract.e2e_cases:
        case_id = str(expected["id"])
        actual = by_id.get(case_id)
        if not isinstance(actual, dict):
            continue
        field = f"cases[{case_id}]"
        level = expected.get("level")
        if (
            report_version == LEGACY_BLACKBOX_REPORT_SCHEMA_VERSION
            and actual.get("level") != level
        ):
            errors.append(f"{field}.level does not match frozen plan")
        requirements = case_dimension_contract(expected)
        statuses = {
            dimension: dimension_status(
                actual.get(dimension),
                report_version=report_version,
                field=f"{field}.{dimension}",
                errors=errors,
            )
            for dimension in ("mechanical", "verify", "quality")
        }
        derived_outcome = "pass"
        for dimension, requirement in requirements.items():
            expected_status = "pass" if requirement == "required" else "not_applicable"
            if statuses.get(dimension) != expected_status:
                errors.append(f"{field}.{dimension} must be {expected_status}")
                derived_outcome = "fail"
        if actual.get("outcome") != derived_outcome:
            errors.append(
                f"{field}.outcome must equal the runtime-derived {derived_outcome}"
            )
        if derived_outcome != "pass":
            errors.append(f"{field} does not satisfy passing blackbox evidence")
    if errors:
        raise RuntimeProblem(
            "structured blackbox case results do not match the frozen plan",
            code="E2E_CASE_RESULTS_INVALID",
            details={
                "errors": errors,
                "required_case_ids": list(contract.e2e_case_ids),
                "supplied_cases": raw_cases,
            },
        )


def cmd_record_evidence(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        reject_during_finalize_intent(ledger, "record-evidence")
        if ledger["phase"] != "active":
            raise RuntimeProblem(
                "evidence can only be recorded for an active run",
                code="PHASE_NOT_ACTIVE",
                details={"phase": ledger["phase"]},
            )
        contract = verify_plan_unchanged(ledger)
        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        if worktree_residue(builder):
            raise RuntimeProblem(
                "candidate worktree is dirty",
                code="CANDIDATE_DIRTY",
            )
        candidate = full_head(builder)
        supplied = full_head(repo, args.head)
        if supplied != candidate:
            raise RuntimeProblem(
                "evidence head must equal the live candidate",
                code="EVIDENCE_HEAD_MISMATCH",
                details={"candidate_head": candidate, "evidence_head": supplied},
            )
        required_role = {
            "e2e_verified": "tester",
            "reviewed": "reviewer",
            "doc_reviewed": "reviewer",
        }[args.kind]
        pending_role_turn = ledger.get("pending_agent_turns", {}).get(required_role)
        if isinstance(pending_role_turn, dict):
            raise RuntimeProblem(
                f"{args.kind} cannot be recorded while a {required_role} follow-up is pending",
                code="EVIDENCE_ROLE_TURN_PENDING",
                details={"required_role": required_role, "pending": pending_role_turn},
            )
        value = ledger.get("agents", {}).get(required_role)
        if not isinstance(value, dict) or value.get("event") != "idle":
            raise RuntimeProblem(
                f"{args.kind} requires a completed {required_role} agent turn",
                code="EVIDENCE_ROLE_MISSING",
                details={"required_role": required_role},
            )
        if value.get("agent_id") != args.agent_id:
            raise RuntimeProblem(
                "evidence agent id does not match ledger role owner",
                code="EVIDENCE_AGENT_MISMATCH",
                details={"required_role": required_role, "ledger_agent": value.get("agent_id")},
            )
        if value.get("result") != "pass" or not value.get("turn_id"):
            raise RuntimeProblem(
                f"{args.kind} requires an explicit passing {required_role} result",
                code="EVIDENCE_ROLE_RESULT_NOT_PASS",
                details={
                    "required_role": required_role,
                    "agent_result": value.get("result"),
                    "turn_id": value.get("turn_id"),
                },
            )
        if value.get("candidate_head") != candidate or value.get("candidate_dirty") is not False:
            raise RuntimeProblem(
                "evidence agent turn did not complete on the live clean candidate",
                code="EVIDENCE_AGENT_HEAD_MISMATCH",
                details={
                    "required_role": required_role,
                    "agent_candidate_head": value.get("candidate_head"),
                    "agent_candidate_dirty": value.get("candidate_dirty"),
                    "candidate_head": candidate,
                },
            )
        if args.kind in {"reviewed", "doc_reviewed"} and not reviewer_prerequisites_bound(
            ledger, value, candidate
        ):
            raise RuntimeProblem(
                "review evidence requires verification prerequisites at both "
                "turn start and completion",
                code="REVIEW_PREREQUISITES_NOT_BOUND",
                details={
                    "plan_level": ledger.get("plan", {}).get("level"),
                    "candidate_head": candidate,
                    "review_prerequisites": value.get("review_prerequisites"),
                },
            )
        if args.kind == "e2e_verified" and not (
            ledger["tester_integration"].get("completed") is True
            and ledger["tester_integration"].get("author_turn_id")
        ):
            raise RuntimeProblem(
                "blackbox evidence requires completed Tester author integration",
                code="E2E_AUTHOR_INTEGRATION_MISSING",
                details={"tester_integration": ledger["tester_integration"]},
            )
        if (
            args.kind == "e2e_verified"
            and requires_test_effectiveness(ledger)
            and evidence_head(ledger, "test_effectiveness") != candidate
        ):
            raise RuntimeProblem(
                "blackbox evidence requires current test-effectiveness proof",
                code="TEST_EFFECTIVENESS_MISSING",
                details={
                    "candidate_head": candidate,
                    "test_effectiveness_head": evidence_head(
                        ledger, "test_effectiveness"
                    ),
                },
            )
        agent_fact = value
        field = PUBLIC_EVIDENCE_FIELDS[args.kind]
        details = parse_details(args.details)
        if args.kind == "e2e_verified":
            report_version = blackbox_report_schema_version(ledger)
            required_details = (
                {
                    "candidate_worktree",
                    "head_before",
                    "head_after",
                    "candidate_dirty",
                    "executions",
                }
                if report_version == BLACKBOX_REPORT_SCHEMA_VERSION
                else {
                    "candidate_worktree",
                    "head_before",
                    "head_after",
                    "command",
                    "returncode",
                }
            )
            if not isinstance(details, dict) or not required_details.issubset(details):
                raise RuntimeProblem(
                    "blackbox evidence requires replayable candidate-worktree details",
                    code="E2E_DETAILS_REQUIRED",
                    details={
                        "blackbox_report_schema_version": report_version,
                        "required": sorted(required_details),
                    },
                )
            if report_version == BLACKBOX_REPORT_SCHEMA_VERSION:
                validate_blackbox_report_v2_schema(details)
                validate_blackbox_execution_coverage(contract, details)
            try:
                evidence_worktree = Path(str(details["candidate_worktree"])).resolve()
            except OSError as exc:
                raise RuntimeProblem(
                    "blackbox evidence candidate worktree is invalid",
                    code="E2E_DETAILS_INVALID",
                ) from exc
            if (
                evidence_worktree != builder.resolve()
                or details.get("head_before") != candidate
                or details.get("head_after") != candidate
                or (
                    report_version == LEGACY_BLACKBOX_REPORT_SCHEMA_VERSION
                    and (
                        not isinstance(details.get("command"), str)
                        or not str(details.get("command")).strip()
                        or type(details.get("returncode")) is not int
                        or details.get("returncode") != 0
                    )
                )
                or (
                    (
                        "candidate_dirty" in details
                        or requires_test_effectiveness(ledger)
                    )
                    and details.get("candidate_dirty") is not False
                )
            ):
                raise RuntimeProblem(
                    "blackbox evidence is not bound to a successful run on the live candidate",
                    code="E2E_DETAILS_INVALID",
                    details={
                        "candidate_worktree": str(builder.resolve()),
                        "candidate_head": candidate,
                        "supplied": details,
                    },
                )
            validate_structured_e2e_results(
                contract,
                details,
                report_version=report_version,
            )
        if field == "blackbox":
            scope = evidence_scope_patterns(ledger, "blackbox")
            commands = accepted_blackbox_commands(ledger, details)
            if not commands:
                raise RuntimeProblem(
                    "blackbox report has no accepted command",
                    code="E2E_EXECUTION_COVERAGE_INVALID",
                )
            try:
                command_paths: set[str] = set()
                for command in commands:
                    command_paths.update(
                        runner_repository_paths(
                            command,
                            resolve_blackbox_dependencies=True,
                        )
                    )
            except RuntimeProblem:
                command_paths = set()
                scope = ["**"]
            scope = merge_evidence_scope_patterns(scope, command_paths)
            digest = evidence_contract.input_digest(
                repo,
                supplied,
                patterns=scope,
                plan_sha256=str(ledger.get("plan", {}).get("sha256") or ""),
                context={
                    **evidence_digest_context(
                        ledger,
                        "blackbox",
                        details=details,
                    ),
                },
            )
        else:
            scope = ["**"]
            digest = evidence_contract.input_digest(
                repo,
                supplied,
                patterns=scope,
                plan_sha256=str(ledger.get("plan", {}).get("sha256") or ""),
                context={"kind": field, **plan_digest_context(ledger)},
            )
        record = evidence_contract.make_record(
            kind=field,
            observed_head=supplied,
            accepted_head=supplied,
            input_sha256=digest,
            scope=scope,
            provenance={"agent": agent_fact},
        )
        if field == "blackbox":
            record["details"] = details
        ledger.setdefault("evidence", {})[field] = record
        append_event(
            ledger,
            "evidence_recorded",
            {
                "kind": args.kind,
                "field": EVIDENCE_STATUS_FIELDS[field],
                "head": supplied,
                "input_digest": digest,
                "scope": scope,
                "candidate_head_at_record": candidate,
                "agent": agent_fact,
                "details": details,
            },
        )
        save_ledger(repo, ledger)
        return {
            "status": "READY",
            "run_id": ledger["run_id"],
            "kind": args.kind,
            "field": EVIDENCE_STATUS_FIELDS[field],
            "head": supplied,
            "candidate_head": candidate,
            "test_effectiveness_head": evidence_head(
                ledger, "test_effectiveness"
            ),
            "evidence": record,
        }, EXIT_PASS


def cmd_prepare_follow_up(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    role = str(args.role)
    purpose = str(args.purpose)
    if purpose not in FOLLOW_UP_PURPOSES[role]:
        raise RuntimeProblem(
            "follow-up purpose is not valid for this role",
            code="AGENT_FOLLOW_UP_PURPOSE_INVALID",
            details={"role": role, "purpose": purpose},
    )
    with locked_run(repo, run_id) as ledger:
        reject_during_finalize_intent(ledger, "prepare-follow-up")
        contract = verify_plan_unchanged(ledger)
        report_contract = (
            derived_blackbox_report_contract(ledger, contract)
            if purpose == "blackbox"
            else None
        )
        if ledger.get("phase") != "active":
            raise RuntimeProblem(
                "agent follow-up can only be prepared for an active run",
                result="NEEDS_USER",
                code="PHASE_NOT_ACTIVE",
                details={"phase": ledger.get("phase")},
                exit_code=EXIT_FAIL,
            )
        require_problem_sources_recorded(ledger)
        current = ledger.setdefault("agents", {}).get(role)
        if not isinstance(current, dict) or current.get("event") != "idle":
            raise RuntimeProblem(
                "agent follow-up requires a completed role turn",
                result="NEEDS_USER",
                code="AGENT_FOLLOW_UP_ROLE_NOT_IDLE",
                details={"role": role, "current": current},
                exit_code=EXIT_FAIL,
            )
        if current.get("agent_id") != args.agent_id:
            raise RuntimeProblem(
                "agent follow-up must resume the existing role thread",
                result="NEEDS_USER",
                code="ROLE_AGENT_CONFLICT",
                details={
                    "role": role,
                    "current_agent_id": current.get("agent_id"),
                    "incoming_agent_id": args.agent_id,
                },
                exit_code=EXIT_FAIL,
            )
        pending_turns = ledger.setdefault(
            "pending_agent_turns", {"tester": None, "reviewer": None}
        )
        existing = pending_turns.get(role)
        if isinstance(existing, dict):
            if (
                existing.get("agent_id") == args.agent_id
                and existing.get("purpose") == purpose
                and existing.get("previous_turn_id") == current.get("turn_id")
            ):
                if role == "tester":
                    try:
                        lifecycle_delivery.set_tester_start_attestation(
                            str(ledger["owner_session_id"]),
                            {
                                "kind": "follow-up",
                                "agent_id": str(existing["agent_id"]),
                                "dispatch_id": str(existing["dispatch_id"]),
                                "previous_turn_id": str(existing["previous_turn_id"]),
                                "purpose": str(existing["purpose"]),
                                "role_head": str(existing["role_head"]),
                                "dirty_paths": (
                                    ["prepared-worktree-dirty"]
                                    if existing.get("role_dirty")
                                    else []
                                ),
                            },
                        )
                    except lifecycle_delivery.LifecycleDeliveryError as exc:
                        raise RuntimeProblem(str(exc), code=exc.code) from exc
                return {
                    "status": "NOOP",
                    "message": "the same agent follow-up is already prepared",
                    "run_id": run_id,
                    "role": role,
                    **(
                        {"blackbox_report_contract": report_contract}
                        if report_contract is not None
                        else {}
                    ),
                    **(
                        {
                            "tester_correction_progress": (
                                tester_correction_progress(ledger)
                            )
                        }
                        if role == "tester" and purpose == "author"
                        else {}
                    ),
                    **existing,
                }, EXIT_PASS
            raise RuntimeProblem(
                "another agent follow-up is already pending for this role",
                result="NEEDS_USER",
                code="AGENT_FOLLOW_UP_ALREADY_PENDING",
                details={"role": role, "pending": existing},
                exit_code=EXIT_FAIL,
            )

        if role == "tester" and purpose == "author":
            correction_progress = tester_correction_progress(ledger)
            if correction_progress["next_author_followup_blocked"]:
                ledger["phase"] = "architecture_review_required"
                append_event(
                    ledger,
                    "tester_correction_architecture_review_required",
                    {"tester_correction_progress": correction_progress},
                )
                save_ledger(repo, ledger)
                raise RuntimeProblem(
                    "three Tester author corrections completed without a newer machine pass; review the frozen inputs or architecture before continuing",
                    result="NEEDS_USER",
                    code="ARCHITECTURE_REVIEW_REQUIRED",
                    details={
                        "phase": ledger["phase"],
                        "tester_correction_progress": correction_progress,
                    },
                    exit_code=EXIT_FAIL,
                )

        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        role_worktree = (
            Path(str(ledger["worktrees"]["tester"]["path"]))
            if role == "tester"
            else builder
        )
        candidate_head = full_head(builder)
        candidate_dirty = bool(worktree_residue(builder))
        if purpose == "blackbox":
            integration = ledger.get("tester_integration", {})
            test_effectiveness_ready = (
                not requires_test_effectiveness(ledger)
                or evidence_head(ledger, "test_effectiveness") == candidate_head
            )
            if not test_effectiveness_ready:
                raise RuntimeProblem(
                    "Tester blackbox follow-up requires current test-effectiveness proof",
                    result="NEEDS_USER",
                    code="TEST_EFFECTIVENESS_MISSING",
                    details={
                        "candidate_head": candidate_head,
                        "test_effectiveness_head": evidence_head(
                            ledger, "test_effectiveness"
                        ),
                    },
                    exit_code=EXIT_FAIL,
                )
            if (
                candidate_dirty
                or integration.get("completed") is not True
                or evidence_head(ledger, "machine") != candidate_head
            ):
                raise RuntimeProblem(
                    "Tester blackbox follow-up requires integrated tests, current proof, and a verified clean candidate",
                    result="NEEDS_USER",
                    code="TESTER_BLACKBOX_PREREQUISITES_MISSING",
                    details={
                        "candidate_head": candidate_head,
                        "candidate_dirty": candidate_dirty,
                        "tester_integration_completed": integration.get("completed"),
                        "verified_head": evidence_head(ledger, "machine"),
                        "test_effectiveness_required": requires_test_effectiveness(
                            ledger
                        ),
                        "test_effectiveness_head": evidence_head(
                            ledger, "test_effectiveness"
                        ),
                    },
                    exit_code=EXIT_FAIL,
                )

        review_start: dict[str, Any] | None = None
        if role == "reviewer":
            review_start = reviewer_prerequisite_snapshot(
                repo,
                ledger,
                candidate_head=candidate_head,
                candidate_dirty=candidate_dirty,
            )
            if review_start.get("satisfied") is not True:
                raise RuntimeProblem(
                    "Reviewer follow-up prerequisites are not bound to the live candidate",
                    result="NEEDS_USER",
                    code="REVIEW_PREREQUISITES_NOT_BOUND",
                    details={"review_prerequisites": review_start},
                    exit_code=EXIT_FAIL,
                )

        dispatch_id = uuid.uuid4().hex
        invalidate_role_evidence(
            ledger,
            role,
            purpose=purpose,
            dispatch_id=dispatch_id,
        )
        if role == "tester" and purpose == "author":
            ledger["tester_integration"]["completed"] = False
        prepared = {
            "dispatch_id": dispatch_id,
            "agent_id": str(args.agent_id),
            "purpose": purpose,
            "previous_turn_id": str(current["turn_id"]),
            "candidate_head": candidate_head,
            "candidate_dirty": candidate_dirty,
            "role_head": full_head(role_worktree),
            "role_dirty": bool(worktree_residue(role_worktree)),
            "prerequisite_manifest_sha256": ledger.get(
                "prerequisite_publication", {}
            ).get("manifest_sha256"),
            "review_prerequisites_start": review_start,
            "prepared_at": utc_now(),
        }
        pending_turns[role] = prepared
        append_event(
            ledger,
            "agent_follow_up_prepared",
            {"role": role, **prepared},
        )
        save_ledger(repo, ledger)
        if role == "tester":
            try:
                lifecycle_delivery.set_tester_start_attestation(
                    str(ledger["owner_session_id"]),
                    {
                        "kind": "follow-up",
                        "agent_id": str(prepared["agent_id"]),
                        "dispatch_id": str(prepared["dispatch_id"]),
                        "previous_turn_id": str(prepared["previous_turn_id"]),
                        "purpose": str(prepared["purpose"]),
                        "role_head": str(prepared["role_head"]),
                        "dirty_paths": (
                            ["prepared-worktree-dirty"]
                            if prepared.get("role_dirty")
                            else []
                        ),
                    },
                )
            except lifecycle_delivery.LifecycleDeliveryError as exc:
                raise RuntimeProblem(str(exc), code=exc.code) from exc
        return {
            "status": "READY",
            "message": "agent follow-up prepared and previous role evidence invalidated",
            "run_id": run_id,
            "role": role,
            **(
                {"blackbox_report_contract": report_contract}
                if report_contract is not None
                else {}
            ),
            **(
                {"tester_correction_progress": tester_correction_progress(ledger)}
                if role == "tester" and purpose == "author"
                else {}
            ),
            **prepared,
        }, EXIT_PASS


def _cmd_agent_event_apply(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if os.environ.get("BUILDER_LOOP_HOOK_EVENT") != "1":
        raise RuntimeProblem(
            "agent-event is an internal lifecycle-hook surface",
            code="AGENT_EVENT_HOOK_REQUIRED",
        )
    repo = resolve_repo(args.repo)
    session_id = str(args.session_id).strip()
    turn_id = str(args.turn_id).strip()
    result_value = str(args.result).strip() if args.result is not None else None
    if not turn_id:
        raise RuntimeProblem("agent-event requires a non-empty turn id", code="AGENT_TURN_ID_REQUIRED")
    if args.event == "idle":
        if result_value not in AGENT_RESULTS[args.role]:
            raise RuntimeProblem(
                "idle agent-event requires an allowed explicit result",
                code="AGENT_RESULT_INVALID",
                details={"role": args.role, "result": result_value},
            )
    elif result_value is not None:
        raise RuntimeProblem(
            "agent result is only valid for idle events",
            code="AGENT_RESULT_INVALID",
            details={"event": args.event, "result": result_value},
        )
    ledgers = active_ledgers_for_session(repo, session_id)
    if not ledgers:
        return {
            "status": "READY",
            "recorded": False,
            "code": "NO_ACTIVE_RUN",
            "owner_session_id": session_id,
        }, EXIT_PASS
    if len(ledgers) > 1:
        raise RuntimeProblem(
            "multiple active ledgers match owner_session_id",
            code="FATAL_AMBIGUOUS",
            details={
                "owner_session_id": session_id,
                "run_ids": [item[1]["run_id"] for item in ledgers],
            },
        )
    repo, selected = ledgers[0]
    run_id = str(selected["run_id"])
    with locked_run(repo, run_id) as ledger:
        reject_during_finalize_intent(ledger, "agent-event")
        current = ledger.setdefault("agents", {}).get(args.role)
        completed_turns = ledger.setdefault("completed_agent_turns", {}).setdefault(
            args.role, []
        )
        pending_turns = ledger.setdefault(
            "pending_agent_turns", {"tester": None, "reviewer": None}
        )
        pending = pending_turns.get(args.role)
        if ledger.get("phase") == "continuity_failure":
            raise RuntimeProblem(
                "run already lost agent or target continuity",
                result="CONTINUITY_FAILURE",
                code="CONTINUITY_FAILURE",
                details={"role": args.role, "current": current},
                exit_code=EXIT_FAIL,
            )
        if current is not None and current.get("agent_id") != args.agent_id:
            if current.get("event") == "closed":
                ledger["phase"] = "continuity_failure"
                append_event(
                    ledger,
                    "agent_continuity_failure",
                    {
                        "role": args.role,
                        "closed_agent_id": current.get("agent_id"),
                        "incoming_agent_id": args.agent_id,
                    },
                )
                save_ledger(repo, ledger)
                raise RuntimeProblem(
                    "a closed role thread cannot be replaced in the same run",
                    result="CONTINUITY_FAILURE",
                    code="ROLE_AGENT_CONTINUITY_LOST",
                    details={"role": args.role, "current": current, "incoming_agent_id": args.agent_id},
                    exit_code=EXIT_FAIL,
                )
            raise RuntimeProblem(
                "role is already owned by another live agent",
                result="NEEDS_USER",
                code="ROLE_AGENT_CONFLICT",
                details={"role": args.role, "current": current, "incoming_agent_id": args.agent_id},
                exit_code=EXIT_FAIL,
            )
        if current is not None and current.get("event") == "closed" and args.event != "closed":
            ledger["phase"] = "continuity_failure"
            append_event(
                ledger,
                "agent_continuity_failure",
                {"role": args.role, "closed_agent_id": args.agent_id},
            )
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "a closed role thread cannot be resumed",
                result="CONTINUITY_FAILURE",
                code="ROLE_AGENT_CONTINUITY_LOST",
                details={"role": args.role, "agent_id": args.agent_id},
                exit_code=EXIT_FAIL,
            )

        # Journal delivery is at-least-once. Classify a replay of an already
        # accepted role/turn fact before interpreting a Tester start as a new
        # follow-up that needs fresh attestation.
        if (
            args.event == "start"
            and current is not None
            and current.get("agent_id") == args.agent_id
        ):
            if current.get("event") == "start" and current.get("turn_id") == turn_id:
                return {
                    "status": "NOOP",
                    "message": "agent turn start was already recorded",
                    "recorded": True,
                    "run_id": run_id,
                    "role": args.role,
                    **current,
                }, EXIT_PASS
            if turn_id in completed_turns:
                return {
                    "status": "NOOP",
                    "message": "completed agent turn cannot be replayed",
                    "recorded": False,
                    "code": "STALE_AGENT_TURN",
                    "run_id": run_id,
                    "role": args.role,
                    "agent_id": args.agent_id,
                    "turn_id": turn_id,
                }, EXIT_PASS
        if (
            current is not None
            and current.get("event") == "idle"
            and args.event == "idle"
            and current.get("turn_id") == turn_id
        ):
            if current.get("result") != result_value:
                ledger["phase"] = "continuity_failure"
                append_event(
                    ledger,
                    "agent_turn_result_conflict",
                    {
                        "role": args.role,
                        "agent_id": args.agent_id,
                        "turn_id": turn_id,
                        "previous_result": current.get("result"),
                        "incoming_result": result_value,
                    },
                )
                save_ledger(repo, ledger)
                raise RuntimeProblem(
                    "one completed agent turn cannot report two different results",
                    result="CONTINUITY_FAILURE",
                    code="AGENT_TURN_RESULT_CONFLICT",
                    details={"role": args.role, "turn_id": turn_id, "current": current},
                    exit_code=EXIT_FAIL,
                )
            return {
                "status": "NOOP",
                "message": "agent turn result was already recorded",
                "recorded": True,
                "run_id": run_id,
                "role": args.role,
                **current,
            }, EXIT_PASS

        if args.role == "tester" and args.event == "start":
            if ledger.get("plan", {}).get("level") == "L1":
                raise RuntimeProblem(
                    "L1 documentation runs do not start a Tester",
                    result="NEEDS_USER",
                    code="L1_TESTER_FORBIDDEN",
                    exit_code=EXIT_FAIL,
                )
            publication = ledger.get("prerequisite_publication", {})
            if publication.get("required") is True and not publication.get(
                "manifest_sha256"
            ):
                raise RuntimeProblem(
                    "serial Tester cannot start before prerequisites are published",
                    result="NEEDS_USER",
                    code="PREREQUISITES_NOT_PUBLISHED",
                    exit_code=EXIT_FAIL,
                )
            if current is None:
                expected_base = str(ledger["tester_integration"]["base_head"])
                if os.environ.get("BUILDER_LOOP_DELIVERY_FOLD") == "1":
                    attestation = getattr(args, "tester_baseline", None)
                    baseline_valid = bool(
                        isinstance(attestation, dict)
                        and attestation.get("kind") == "initial-author"
                        and set(attestation)
                        == {
                            "kind",
                            "expected_head",
                            "tester_head",
                            "dirty_paths",
                        }
                        and attestation.get("expected_head") == expected_base
                        and attestation.get("tester_head") == expected_base
                        and attestation.get("dirty_paths") == []
                    )
                    live_head = (
                        str(attestation.get("tester_head"))
                        if isinstance(attestation, dict)
                        else None
                    )
                    residue = (
                        attestation.get("dirty_paths")
                        if isinstance(attestation, dict)
                        else None
                    )
                else:
                    tester = Path(str(ledger["worktrees"]["tester"]["path"]))
                    live_head = full_head(tester)
                    residue = worktree_residue(tester)
                    baseline_valid = live_head == expected_base and not residue
                if not baseline_valid:
                    raise RuntimeProblem(
                        "Tester author worktree does not match its frozen baseline",
                        result="NEEDS_USER",
                        code="TESTER_AUTHOR_BASELINE_MISMATCH",
                        details={
                            "expected_head": expected_base,
                            "tester_head": live_head,
                            "dirty_paths": residue,
                        },
                        exit_code=EXIT_FAIL,
                    )
        prepared_same_agent = bool(
            isinstance(pending, dict)
            and current is not None
            and current.get("event") == "idle"
            and current.get("agent_id") == args.agent_id
            and pending.get("agent_id") == args.agent_id
            and pending.get("previous_turn_id") == current.get("turn_id")
            and turn_id != current.get("turn_id")
            and turn_id not in completed_turns
        )
        prepared_start = prepared_same_agent and args.event == "start"
        prepared_terminal = prepared_same_agent and args.event in {"idle", "closed"}
        if args.role == "tester" and args.event == "start" and current is not None:
            attestation = getattr(args, "tester_baseline", None)
            delivery_fold = os.environ.get("BUILDER_LOOP_DELIVERY_FOLD") == "1"
            if (
                not delivery_fold
                and isinstance(pending, dict)
                and not isinstance(attestation, dict)
            ):
                try:
                    route = lifecycle_delivery.load_route(session_id)
                except lifecycle_delivery.LifecycleDeliveryError as exc:
                    raise RuntimeProblem(str(exc), code=exc.code) from exc
                if route is not None:
                    attestation = route.get("tester_start_attestation")
            follow_up_valid = bool(
                prepared_start
                and isinstance(attestation, dict)
                and set(attestation)
                == {
                    "kind",
                    "agent_id",
                    "dispatch_id",
                    "previous_turn_id",
                    "purpose",
                    "role_head",
                    "dirty_paths",
                }
                and attestation.get("kind") == "follow-up"
                and attestation.get("agent_id") == pending.get("agent_id")
                and attestation.get("dispatch_id") == pending.get("dispatch_id")
                and attestation.get("previous_turn_id")
                == pending.get("previous_turn_id")
                and attestation.get("purpose") == pending.get("purpose")
                and attestation.get("role_head") == pending.get("role_head")
                and attestation.get("dirty_paths") == []
            )
            if (delivery_fold or isinstance(pending, dict)) and not follow_up_valid:
                raise RuntimeProblem(
                    "Tester follow-up Start does not match its prepared attestation",
                    result="NEEDS_USER",
                    code="TESTER_FOLLOW_UP_ATTESTATION_MISMATCH",
                    details={"pending": pending, "attestation": attestation},
                    exit_code=EXIT_FAIL,
                )
        if args.event == "start" and current is not None and current.get("event") == "start":
            raise RuntimeProblem(
                "a new agent turn cannot start before the current turn completes",
                result="NEEDS_USER",
                code="AGENT_TURN_OVERLAP",
                details={"role": args.role, "current": current, "incoming_turn_id": turn_id},
                exit_code=EXIT_FAIL,
            )
        if args.event in {"idle", "closed"} and (
            current is None or current.get("agent_id") != args.agent_id
        ):
            return {
                "status": "READY",
                "recorded": False,
                "code": "UNOWNED_AGENT_EVENT",
                "run_id": run_id,
                "role": args.role,
                "agent_id": args.agent_id,
                "event": args.event,
            }, EXIT_PASS
        if args.event in {"idle", "closed"} and not prepared_terminal and (
            current.get("event") != "start" or current.get("turn_id") != turn_id
        ):
            raise RuntimeProblem(
                "agent terminal event does not match the currently running turn",
                result="NEEDS_USER",
                code="AGENT_TURN_MISMATCH",
                details={"role": args.role, "current": current, "incoming_turn_id": turn_id},
                exit_code=EXIT_FAIL,
            )
        if not (prepared_start or prepared_terminal) and (
            args.event == "start" or result_value != "pass"
        ):
            invalidation_purpose = (
                "author"
                if args.role == "tester"
                and args.event in {"idle", "closed"}
                and result_value != "pass"
                else None
            )
            invalidate_role_evidence(
                ledger,
                args.role,
                purpose=invalidation_purpose,
                turn_id=turn_id,
            )
        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        role_worktree = (
            Path(str(ledger["worktrees"]["tester"]["path"]))
            if args.role == "tester"
            else builder
        )
        candidate_head = full_head(builder)
        candidate_dirty = bool(worktree_residue(builder))
        follow_up_dispatch_id: str | None = None
        follow_up_purpose: str | None = None
        if prepared_start or prepared_terminal:
            follow_up_dispatch_id = str(pending["dispatch_id"])
            follow_up_purpose = str(pending["purpose"])
        elif current is not None and args.event in {"idle", "closed"}:
            follow_up_dispatch_id = current.get("follow_up_dispatch_id")
            follow_up_purpose = current.get("follow_up_purpose")
        review_prerequisites: dict[str, Any] | None = None
        if args.role == "reviewer":
            snapshot = reviewer_prerequisite_snapshot(
                repo,
                ledger,
                candidate_head=candidate_head,
                candidate_dirty=candidate_dirty,
            )
            if args.event == "start":
                start_snapshot: Any = (
                    pending.get("review_prerequisites_start")
                    if prepared_start
                    else snapshot
                )
                completion_snapshot: Any = None
            else:
                if prepared_terminal:
                    start_snapshot = pending.get("review_prerequisites_start")
                else:
                    previous_binding = current.get("review_prerequisites", {})
                    start_snapshot = (
                        previous_binding.get("start")
                        if isinstance(previous_binding, dict)
                        else None
                    )
                completion_snapshot = snapshot
            review_prerequisites = {
                "start": start_snapshot,
                "completion": completion_snapshot,
                "bound": bool(
                    args.event == "idle"
                    and result_value == "pass"
                    and reviewer_prerequisite_snapshot_matches(
                        ledger, start_snapshot, candidate_head
                    )
                    and reviewer_prerequisite_snapshot_matches(
                        ledger, completion_snapshot, candidate_head
                    )
                ),
            }
        fact = {
            "agent_id": args.agent_id,
            "event": args.event,
            "turn_id": turn_id,
            "result": result_value,
            "candidate_head": candidate_head,
            "candidate_dirty": candidate_dirty,
            "role_head": full_head(role_worktree),
            "role_dirty": bool(worktree_residue(role_worktree)),
            "prerequisite_manifest_sha256": ledger.get(
                "prerequisite_publication", {}
            ).get("manifest_sha256"),
            "review_prerequisites": review_prerequisites,
            "follow_up_dispatch_id": follow_up_dispatch_id,
            "follow_up_purpose": follow_up_purpose,
            "at": utc_now(),
        }
        ledger["agents"][args.role] = fact
        if prepared_start or prepared_terminal:
            pending_turns[args.role] = None
        if args.role == "tester" and args.event == "idle" and result_value == "tests_ready":
            integration = ledger["tester_integration"]
            integration["author_agent_id"] = args.agent_id
            integration["author_turn_id"] = turn_id
            integration["author_head"] = fact["role_head"]
            integration["author_prerequisite_manifest_sha256"] = fact[
                "prerequisite_manifest_sha256"
            ]
            integration["completed"] = False
        if args.event in {"idle", "closed"} and turn_id not in completed_turns:
            completed_turns.append(turn_id)
        append_event(
            ledger,
            "agent_event",
            {
                "role": args.role,
                **fact,
            },
        )
        if args.event == "closed":
            ledger["phase"] = "continuity_failure"
            append_event(
                ledger,
                "agent_continuity_failure",
                {"role": args.role, "closed_agent_id": args.agent_id},
            )
        save_ledger(repo, ledger)
        if args.event == "closed":
            return {
                "status": "CONTINUITY_FAILURE",
                "message": f"{args.role} thread closed before run completion",
                "run_id": run_id,
                "role": args.role,
                **fact,
            }, EXIT_FAIL
        if (
            args.role == "reviewer"
            and args.event == "idle"
            and result_value == "pass"
            and not review_prerequisites.get("bound")
        ):
            return {
                "status": "NEEDS_USER",
                "message": (
                    "Reviewer pass was recorded but is not bound to required "
                    "start/completion evidence"
                ),
                "code": "REVIEW_PREREQUISITES_NOT_BOUND",
                "recorded": True,
                "run_id": run_id,
                "role": args.role,
                **fact,
            }, EXIT_FAIL
        return {
            "status": "READY",
            "recorded": True,
            "run_id": run_id,
            "role": args.role,
            **fact,
        }, EXIT_PASS


def _agent_event_argv(args: argparse.Namespace) -> list[str]:
    values = [
        "agent-event",
        "--repo",
        str(args.repo),
        "--session-id",
        str(args.session_id),
        "--role",
        str(args.role),
        "--agent-id",
        str(args.agent_id),
        "--turn-id",
        str(args.turn_id),
        "--event",
        str(args.event),
    ]
    if args.result is not None:
        values.extend(["--result", str(args.result)])
    return values


def cmd_agent_event(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if os.environ.get("BUILDER_LOOP_AGENT_EVENT_APPLY") == "1":
        return _cmd_agent_event_apply(args)
    if os.environ.get("BUILDER_LOOP_HOOK_EVENT") != "1":
        raise RuntimeProblem(
            "agent-event is an internal lifecycle-hook surface",
            code="AGENT_EVENT_HOOK_REQUIRED",
        )
    try:
        envelope, event_path = lifecycle_delivery.enqueue_event(
            session_id=str(args.session_id).strip(),
            role=str(args.role),
            agent_id=str(args.agent_id),
            turn_id=str(args.turn_id),
            event=str(args.event),
            result=str(args.result).strip() if args.result is not None else None,
        )
    except lifecycle_delivery.LifecycleDeliveryError as exc:
        raise RuntimeProblem(str(exc), code=exc.code) from exc
    if envelope is None or event_path is None:
        return _cmd_agent_event_apply(args)

    command = [sys.executable, str(Path(sys.argv[0]).resolve()), *_agent_event_argv(args)]
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1.0,
            env={
                **os.environ,
                "BUILDER_LOOP_HOOK_EVENT": "1",
                "BUILDER_LOOP_AGENT_EVENT_APPLY": "1",
            },
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "ACCEPTED",
            "message": "agent event is durably queued for ledger folding",
            "event_id": envelope["event_id"],
            "run_id": envelope["run_id"],
            "role": envelope["role"],
            "agent_id": envelope["agent_id"],
            "turn_id": envelope["turn_id"],
            "queued": True,
            "recorded": False,
        }, EXIT_PASS
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    payload: dict[str, Any] | None = None
    if lines:
        try:
            parsed = json.loads(lines[-1])
            payload = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            payload = None
    if payload is not None and (
        payload.get("recorded") is True
        or payload.get("status") in {"NOOP", "CONTINUITY_FAILURE"}
    ):
        event_path.unlink(missing_ok=True)
        payload.setdefault("event_id", envelope["event_id"])
        payload.setdefault("queued", False)
        return payload, completed.returncode
    return {
        "status": "ACCEPTED",
        "message": "agent event is durably queued for ledger folding",
        "event_id": envelope["event_id"],
        "run_id": envelope["run_id"],
        "role": envelope["role"],
        "agent_id": envelope["agent_id"],
        "turn_id": envelope["turn_id"],
        "queued": True,
        "recorded": False,
    }, EXIT_PASS


def _reject_delivery_event(
    repo: Path,
    run_id: str,
    *,
    event_path: Path,
    code: str,
    message: str,
    event: dict[str, Any] | None,
) -> None:
    with locked_run(repo, run_id) as ledger:
        already_recorded = any(
            item.get("type") == "agent_event_rejected"
            and item.get("facts", {}).get("event_file") == event_path.name
            for item in ledger.get("events", [])
            if isinstance(item, dict) and isinstance(item.get("facts"), dict)
        )
        if already_recorded:
            return
        ledger["phase"] = "continuity_failure"
        append_event(
            ledger,
            "agent_event_rejected",
            {
                "event_file": event_path.name,
                "event_id": event.get("event_id") if isinstance(event, dict) else None,
                "code": code,
                "message": message,
            },
        )
        save_ledger(repo, ledger)
    if event is not None:
        event_path.unlink(missing_ok=True)


def _validate_delivery_envelope(
    event: dict[str, Any], *, repo: Path, run_id: str, session_id: str
) -> None:
    required = {
        "schema_version",
        "event_id",
        "binding_sha256",
        "session_id",
        "run_id",
        "repo_root",
        "role",
        "agent_id",
        "turn_id",
        "event",
        "result",
        "tester_baseline",
        "captured_at",
    }
    if set(event) != required or event.get("schema_version") != 1:
        raise RuntimeProblem(
            "queued agent event does not match schema version 1",
            code="LIFECYCLE_EVENT_INVALID",
        )
    if (
        event.get("session_id") != session_id
        or event.get("run_id") != run_id
        or Path(str(event.get("repo_root"))).resolve() != repo.resolve()
    ):
        raise RuntimeProblem(
            "queued agent event route does not match the selected run",
            code="LIFECYCLE_EVENT_ROUTE_MISMATCH",
        )
    expected_binding = lifecycle_delivery.route_binding(session_id, str(repo), run_id)
    if event.get("binding_sha256") != expected_binding:
        raise RuntimeProblem(
            "queued agent event binding digest does not match the selected run",
            code="LIFECYCLE_EVENT_BINDING_MISMATCH",
        )
    if event.get("event_id") != lifecycle_delivery.event_id(event):
        raise RuntimeProblem(
            "queued agent event id does not match its content",
            code="LIFECYCLE_EVENT_ID_MISMATCH",
        )
    role = event.get("role")
    lifecycle = event.get("event")
    result = event.get("result")
    if role not in AGENT_RESULTS or lifecycle not in {"start", "idle", "closed"}:
        raise RuntimeProblem(
            "queued agent event has an unsupported role or lifecycle",
            code="LIFECYCLE_EVENT_INVALID",
        )
    if lifecycle == "idle":
        if result not in AGENT_RESULTS[role]:
            raise RuntimeProblem(
                "queued idle event has an invalid role result",
                code="LIFECYCLE_EVENT_RESULT_INVALID",
            )
    elif result is not None:
        raise RuntimeProblem(
            "queued non-idle event cannot carry a result",
            code="LIFECYCLE_EVENT_RESULT_INVALID",
        )
    tester_baseline = event.get("tester_baseline")
    if role == "tester" and lifecycle == "start":
        initial_fields = {
            "kind",
            "expected_head",
            "tester_head",
            "dirty_paths",
        }
        follow_up_fields = {
            "kind",
            "agent_id",
            "dispatch_id",
            "previous_turn_id",
            "purpose",
            "role_head",
            "dirty_paths",
        }
        if (
            not isinstance(tester_baseline, dict)
            or (
                tester_baseline.get("kind") == "initial-author"
                and set(tester_baseline) != initial_fields
            )
            or (
                tester_baseline.get("kind") == "follow-up"
                and set(tester_baseline) != follow_up_fields
            )
            or tester_baseline.get("kind") not in {"initial-author", "follow-up"}
        ):
            raise RuntimeProblem(
                "queued Tester start lacks its frozen baseline attestation",
                code="LIFECYCLE_EVENT_INVALID",
            )
    elif tester_baseline is not None:
        raise RuntimeProblem(
            "only an initial Tester start may carry a baseline attestation",
            code="LIFECYCLE_EVENT_INVALID",
        )


def ensure_lifecycle_route(repo: Path, run_id: str, ledger: dict[str, Any]) -> None:
    session_id = str(ledger["owner_session_id"])
    try:
        route = lifecycle_delivery.load_route(session_id)
        if route is None:
            tester = Path(str(ledger["worktrees"]["tester"]["path"]))
            current = ledger.get("agents", {}).get("tester")
            pending = ledger.get("pending_agent_turns", {}).get("tester")
            tester_attestation: dict[str, Any] | None = None
            if isinstance(pending, dict):
                tester_attestation = {
                    "kind": "follow-up",
                    "agent_id": str(pending["agent_id"]),
                    "dispatch_id": str(pending["dispatch_id"]),
                    "previous_turn_id": str(pending["previous_turn_id"]),
                    "purpose": str(pending["purpose"]),
                    "role_head": str(pending["role_head"]),
                    "dirty_paths": (
                        ["prepared-worktree-dirty"]
                        if pending.get("role_dirty")
                        else []
                    ),
                }
            elif current is None and ledger.get("plan", {}).get("level") != "L1":
                tester_attestation = initial_tester_start_attestation(ledger)
            lifecycle_delivery.register_route(
                session_id=session_id,
                repo_root=str(repo),
                run_id=run_id,
                ledger_path=str(ledger_path(repo, run_id)),
                tester_start_attestation=tester_attestation,
            )
            return
        if (
            route.get("repo_root") != str(repo.resolve())
            or route.get("run_id") != run_id
            or route.get("ledger_path") != str(ledger_path(repo, run_id).resolve())
        ):
            raise RuntimeProblem(
                "session lifecycle route points at another run",
                code="LIFECYCLE_ROUTE_STALE",
                details={"route": route, "run_id": run_id},
            )
    except lifecycle_delivery.LifecycleDeliveryError as exc:
        raise RuntimeProblem(str(exc), code=exc.code) from exc


def drain_lifecycle_events(repo: Path, run_id: str) -> None:
    ledger = read_json(ledger_path(repo, run_id))
    if ledger.get("phase") not in ACTIVE_PHASES:
        return
    ensure_lifecycle_route(repo, run_id, ledger)
    session_id = str(ledger["owner_session_id"])
    try:
        paths = lifecycle_delivery.queued_event_paths(session_id)
    except lifecycle_delivery.LifecycleDeliveryError as exc:
        raise RuntimeProblem(str(exc), code=exc.code) from exc
    for event_path in paths:
        event: dict[str, Any] | None = None
        try:
            event = lifecycle_delivery.read_json_file(event_path)
            _validate_delivery_envelope(
                event, repo=repo, run_id=run_id, session_id=session_id
            )
            event_args = argparse.Namespace(
                repo=str(repo),
                session_id=session_id,
                role=event["role"],
                agent_id=event["agent_id"],
                turn_id=event["turn_id"],
                event=event["event"],
                result=event["result"],
                tester_baseline=event["tester_baseline"],
            )
            previous = os.environ.get("BUILDER_LOOP_HOOK_EVENT")
            previous_fold = os.environ.get("BUILDER_LOOP_DELIVERY_FOLD")
            os.environ["BUILDER_LOOP_HOOK_EVENT"] = "1"
            os.environ["BUILDER_LOOP_DELIVERY_FOLD"] = "1"
            try:
                payload, _exit_code = _cmd_agent_event_apply(event_args)
            finally:
                if previous is None:
                    os.environ.pop("BUILDER_LOOP_HOOK_EVENT", None)
                else:
                    os.environ["BUILDER_LOOP_HOOK_EVENT"] = previous
                if previous_fold is None:
                    os.environ.pop("BUILDER_LOOP_DELIVERY_FOLD", None)
                else:
                    os.environ["BUILDER_LOOP_DELIVERY_FOLD"] = previous_fold
            if payload.get("recorded") is True or payload.get("status") in {
                "NOOP",
                "CONTINUITY_FAILURE",
            }:
                event_path.unlink(missing_ok=True)
                continue
            raise RuntimeProblem(
                str(payload.get("message") or payload.get("status")),
                code=str(payload.get("code") or "LIFECYCLE_EVENT_REJECTED"),
            )
        except (RuntimeProblem, lifecycle_delivery.LifecycleDeliveryError) as exc:
            code = getattr(exc, "code", "LIFECYCLE_EVENT_REJECTED")
            _reject_delivery_event(
                repo,
                run_id,
                event_path=event_path,
                code=str(code),
                message=str(exc),
                event=event,
            )
            return


def finalize_integration(
    repo: Path,
    ledger: dict[str, Any],
    *,
    source_head: str,
    builder_before: str,
    changed_paths: list[str],
) -> str:
    builder = Path(str(ledger["worktrees"]["builder"]["path"]))
    unmerged = unmerged_paths(builder)
    if unmerged:
        raise RuntimeProblem(
            "integration still has unresolved conflicts",
            result="CONFLICT",
            code="INTEGRATION_CONFLICT",
            details={"unmerged_paths": unmerged},
        )
    current_changes = git_changed_paths(builder, builder_before)
    allowed = list(ledger["plan"]["tester_write"])
    violations = [path for path in current_changes if not path_allowed(path, allowed)]
    if violations:
        raise RuntimeProblem(
            "integration resolution changed paths outside plan-authorized tests",
            result="NEEDS_USER",
            code="INTEGRATION_RESOLUTION_BOUNDARY",
            details={"changed_paths": current_changes, "violations": violations},
        )
    git(builder, "add", "-A", check=True)
    commit = git(
        builder,
        "-c",
        "user.name=Codex Builder Loop",
        "-c",
        "user.email=codex-builder-loop@localhost",
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "-m",
        f"test(codex-loop): integrate tester evidence {ledger['run_id']}",
        check=False,
    )
    if commit.returncode != 0:
        raise RuntimeProblem(
            "cannot commit integrated tester changes",
            code="INTEGRATION_COMMIT_FAILED",
            details={"stdout": tail_text(commit.stdout), "stderr": tail_text(commit.stderr)},
        )
    candidate = full_head(builder)
    ownership = ledger["tester_integration"]
    ownership["source_head"] = source_head
    ownership["owned_paths"] = sorted(set(ownership.get("owned_paths", [])) | set(changed_paths))
    ownership["ownership_baseline_head"] = candidate
    ownership["pending"] = None
    ownership["completed"] = True
    ledger["phase"] = "active"
    invalidate_evidence(repo, ledger, builder_before, candidate)
    append_event(
        ledger,
        "tester_integrated",
        {
            "tester_source_head": source_head,
            "builder_previous_head": builder_before,
            "candidate_head": candidate,
            "owned_paths": ownership["owned_paths"],
        },
    )
    save_ledger(repo, ledger)
    return candidate


def cmd_integrate_tests(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        reject_during_finalize_intent(ledger, "integrate-tests")
        verify_plan_unchanged(ledger)
        tester = Path(str(ledger["worktrees"]["tester"]["path"]))
        pending_tester_turn = ledger.get("pending_agent_turns", {}).get("tester")
        if isinstance(pending_tester_turn, dict):
            raise RuntimeProblem(
                "test integration cannot use an author result while a Tester follow-up is pending",
                result="NEEDS_USER",
                code="TESTER_TURN_PENDING",
                details={"pending": pending_tester_turn},
                exit_code=EXIT_FAIL,
            )
        tester_agent = ledger.get("agents", {}).get("tester")
        if (
            not isinstance(tester_agent, dict)
            or tester_agent.get("event") != "idle"
            or tester_agent.get("result") != "tests_ready"
            or not tester_agent.get("turn_id")
        ):
            raise RuntimeProblem(
                "test integration requires a completed Tester author turn with tests_ready",
                result="NEEDS_USER",
                code="TESTER_AUTHOR_RESULT_MISSING",
                details={"tester_agent": tester_agent},
                exit_code=EXIT_FAIL,
            )
        publication = ledger.get("prerequisite_publication", {})
        if publication.get("required") is True:
            manifest_sha256 = publication.get("manifest_sha256")
            if (
                not manifest_sha256
                or tester_agent.get("prerequisite_manifest_sha256") != manifest_sha256
                or ledger["tester_integration"].get(
                    "author_prerequisite_manifest_sha256"
                )
                != manifest_sha256
                or ledger["tester_integration"].get("base_head")
                != publication.get("head")
            ):
                raise RuntimeProblem(
                    "Tester author result is not bound to the published prerequisites",
                    result="NEEDS_USER",
                    code="TESTER_PREREQUISITE_ATTESTATION_MISMATCH",
                    details={
                        "publication": publication,
                        "tester_agent_manifest_sha256": tester_agent.get(
                            "prerequisite_manifest_sha256"
                        ),
                        "tester_integration": ledger["tester_integration"],
                    },
                    exit_code=EXIT_FAIL,
                )
        live_tester_head = full_head(tester)
        live_tester_dirty = bool(worktree_residue(tester))
        if (
            tester_agent.get("role_head") != live_tester_head
            or tester_agent.get("role_dirty") is not False
            or live_tester_dirty
        ):
            raise RuntimeProblem(
                "Tester author result is not bound to the live clean tester worktree",
                result="NEEDS_USER",
                code="TESTER_AUTHOR_HEAD_MISMATCH",
                details={
                    "agent_role_head": tester_agent.get("role_head"),
                    "tester_head": live_tester_head,
                    "agent_role_dirty": tester_agent.get("role_dirty"),
                    "tester_dirty": live_tester_dirty,
                },
                exit_code=EXIT_FAIL,
            )
        ownership = ledger["tester_integration"]
        if (
            ownership.get("author_agent_id") != tester_agent.get("agent_id")
            or ownership.get("author_turn_id") != tester_agent.get("turn_id")
            or ownership.get("author_head") != tester_agent.get("role_head")
        ):
            raise RuntimeProblem(
                "Tester author attestation does not match the completed tests_ready turn",
                result="NEEDS_USER",
                code="TESTER_AUTHOR_ATTESTATION_MISMATCH",
                details={"tester_agent": tester_agent, "tester_integration": ownership},
                exit_code=EXIT_FAIL,
            )
        pending = ownership.get("pending")
        if args.continue_integration:
            if ledger["phase"] != "integration_conflict" or not isinstance(pending, dict):
                raise RuntimeProblem(
                    "no pending integration conflict to continue",
                    result="NEEDS_USER",
                    code="NO_PENDING_INTEGRATION",
                )
            candidate = finalize_integration(
                repo,
                ledger,
                source_head=str(pending["source_head"]),
                builder_before=str(pending["builder_before_head"]),
                changed_paths=list(pending["changed_paths"]),
            )
            return {
                "status": "READY",
                "message": "resolved tester integration is committed",
                "candidate_head": candidate,
                "tester_owned_paths": ownership["owned_paths"],
            }, EXIT_PASS

        if ledger["phase"] != "active":
            raise RuntimeProblem(
                "test integration requires active phase",
                result="CONFLICT" if ledger["phase"] == "integration_conflict" else "NEEDS_USER",
                code="PHASE_NOT_ACTIVE",
                details={"phase": ledger["phase"]},
            )
        tester_facts = ensure_role_pass(repo, ledger, "tester")
        ensure_role_pass(repo, ledger, "builder")
        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        _, tester_head, _ = checkpoint(tester, str(ledger["run_id"]), "tester")
        tester_facts = ensure_role_pass(repo, ledger, "tester")
        source_base = str(ownership["source_head"])
        if tester_head == source_base:
            ownership["completed"] = True
            ownership["ownership_baseline_head"] = full_head(builder)
            append_event(
                ledger,
                "tester_integration_noop",
                {
                    "tester_source_head": tester_head,
                    "candidate_head": full_head(builder),
                    "author_agent_id": ownership.get("author_agent_id"),
                    "author_turn_id": ownership.get("author_turn_id"),
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "NOOP",
                "message": "tester has no new changes to integrate",
                "code": "NO_TEST_CHANGES",
                "candidate_head": full_head(builder),
                "tester_head": tester_head,
            }, EXIT_PASS
        if not is_ancestor(repo, source_base, tester_head):
            raise RuntimeProblem(
                "tester history was rewritten after previous integration",
                code="TESTER_HISTORY_REWRITTEN",
                details={"source_base": source_base, "tester_head": tester_head},
            )
        changed = git_changed_paths(tester, source_base)
        if not changed:
            builder = Path(str(ledger["worktrees"]["builder"]["path"]))
            ownership["source_head"] = tester_head
            ownership["ownership_baseline_head"] = full_head(builder)
            ownership["pending"] = None
            ownership["completed"] = True
            append_event(
                ledger,
                "tester_integration_empty_commit",
                {
                    "previous_source_head": source_base,
                    "tester_source_head": tester_head,
                    "candidate_head": full_head(builder),
                    "author_agent_id": ownership.get("author_agent_id"),
                    "author_turn_id": ownership.get("author_turn_id"),
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "NOOP",
                "message": "tester history advanced without tree changes; author evidence recorded",
                "code": "NO_TEST_TREE_CHANGES",
                "candidate_head": full_head(builder),
                "tester_head": tester_head,
            }, EXIT_PASS
        allowed = list(ledger["plan"]["tester_write"])
        violations = [path for path in changed if not path_allowed(path, allowed)]
        if violations:
            raise RuntimeProblem(
                "tester changed paths outside plan authorization",
                result="NEEDS_USER",
                code="ROLE_BOUNDARY_VIOLATION",
                details={"changed_paths": changed, "violations": violations},
                exit_code=EXIT_FAIL,
            )

        before, builder_head, _ = checkpoint(builder, str(ledger["run_id"]), "builder")
        invalidate_evidence(repo, ledger, before, builder_head)
        ensure_role_pass(repo, ledger, "builder")
        overlap = sorted(set(changed) & set(git_changed_paths(builder, str(ledger["spec_head"]))))
        prior_owned = set(ownership.get("owned_paths", []))
        illegal_overlap = [path for path in overlap if path not in prior_owned]
        if illegal_overlap:
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "builder and tester both changed the same not-yet-owned test paths",
                result="NEEDS_USER",
                code="TEST_OWNERSHIP_OVERLAP",
                details={"paths": illegal_overlap},
                exit_code=EXIT_FAIL,
            )

        artifact_dir = run_dir(repo, str(ledger["run_id"])) / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        patch_path = artifact_dir / f"tester-{source_base[:12]}-{tester_head[:12]}.patch"
        patch = git(tester, "diff", "--binary", "--full-index", source_base, tester_head, "--", check=True).stdout
        patch_path.write_text(patch, encoding="utf-8")
        apply_result = run_process(
            ["git", "-C", str(builder), "apply", "--3way", "--index", str(patch_path)],
            check=False,
        )
        if apply_result.returncode != 0:
            conflicts = unmerged_paths(builder)
            ownership["pending"] = {
                "source_base_head": source_base,
                "source_head": tester_head,
                "builder_before_head": builder_head,
                "changed_paths": changed,
                "patch_path": str(patch_path),
                "unmerged_paths": conflicts,
            }
            ledger["phase"] = "integration_conflict"
            append_event(
                ledger,
                "tester_integration_conflict",
                {
                    "tester_source_head": tester_head,
                    "builder_before_head": builder_head,
                    "patch_path": str(patch_path),
                    "unmerged_paths": conflicts,
                    "stderr": tail_text(apply_result.stderr),
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "CONFLICT",
                "message": "tester patch conflicted; worktrees and index are preserved",
                "code": "INTEGRATION_CONFLICT",
                "builder_worktree": str(builder),
                "patch_path": str(patch_path),
                "unmerged_paths": conflicts,
                "preserved": True,
            }, EXIT_FAIL

        candidate = finalize_integration(
            repo,
            ledger,
            source_head=tester_head,
            builder_before=builder_head,
            changed_paths=changed,
        )
        return {
            "status": "READY",
            "message": "tester changes integrated into the builder candidate",
            "candidate_head": candidate,
            "tester_head": tester_head,
            "tester_owned_paths": ownership["owned_paths"],
            "patch_path": str(patch_path),
        }, EXIT_PASS


def status_with_persisted_continuity(repo: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    if ledger.get("phase") == "active":
        builder = Path(str(ledger.get("worktrees", {}).get("builder", {}).get("path", "")))
        if builder.exists():
            candidate = full_head(builder)
            stale_heads = [
                head
                for key in EVIDENCE_FIELDS
                if (head := evidence_head(ledger, key)) is not None
                and head != candidate
            ]
            if stale_heads:
                invalidate_evidence(repo, ledger, stale_heads[0], candidate)
                save_ledger(repo, ledger)
    facts = status_facts(repo, ledger)
    intent = ledger.get("finalize_intent")
    target_matches_intent = bool(
        ledger.get("phase") == "active"
        and isinstance(intent, dict)
        and facts.get("target_head") == intent.get("final_head")
    )
    if (
        ledger.get("phase") in {"active", "finalized_cleanup"}
        and not facts["target_continuous"]
        and not target_matches_intent
    ):
        ledger["phase"] = "continuity_failure"
        append_event(
            ledger,
            "target_continuity_failure",
            {
                "target_head": facts["target_head"],
                "expected_target_head": facts["expected_target_head"],
                "candidate_head": facts["candidate_head"],
            },
        )
        save_ledger(repo, ledger)
        facts = status_facts(repo, ledger)
    return facts


def cmd_status(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.run:
        repo, run_id, _ledger = resolve_run_selector(args.repo, args.run)
        with locked_run(repo, run_id) as ledger:
            facts = status_with_persisted_continuity(repo, ledger)
        if ledger["phase"] in {"integration_conflict", "finalize_conflict"}:
            return {
                "status": "CONFLICT",
                "message": "run is stopped with preserved conflict state",
                **facts,
            }, EXIT_FAIL
        if ledger["phase"] == "continuity_failure":
            return {
                "status": "CONTINUITY_FAILURE",
                "message": "run is stopped because required continuity was lost",
                **facts,
            }, EXIT_FAIL
        if ledger["phase"] == "iteration_limit":
            return {
                "status": "NEEDS_USER",
                "message": "verification iteration limit was reached",
                **facts,
            }, EXIT_FAIL
        if ledger["phase"] in {"no_progress", "architecture_review_required"}:
            return {
                "status": "NEEDS_USER",
                "message": "run progress requires an explicit user decision",
                "code": (
                    "NO_PROGRESS"
                    if ledger["phase"] == "no_progress"
                    else "ARCHITECTURE_REVIEW_REQUIRED"
                ),
                **facts,
            }, EXIT_FAIL
        if ledger["phase"] == "finalized_cleanup":
            return {
                "status": "NEEDS_USER",
                "message": "final commit exists but worktree cleanup is incomplete",
                **facts,
            }, EXIT_FAIL
        if ledger["phase"] in {"finalized", "abandoned"}:
            return {
                "status": "COMPLETE",
                "message": f"run is {ledger['phase']}",
                **facts,
            }, EXIT_PASS
        delivery = facts.get("lifecycle_delivery", {})
        if (
            delivery.get("locator") != "ready"
            or delivery.get("blocked_event")
        ):
            return {
                "status": "NEEDS_USER",
                "message": "run lifecycle delivery requires inspection",
                "code": "LIFECYCLE_DELIVERY_NOT_READY",
                **facts,
            }, EXIT_FAIL
        return {
            "status": "ACTIVE",
            "message": "run is active",
            **facts,
        }, EXIT_PASS
    session_id = str(args.session_id or "").strip()
    if not session_id:
        raise RuntimeProblem(
            "status requires --run or --session-id",
            code="STATUS_SELECTOR_REQUIRED",
        )
    try:
        route = lifecycle_delivery.load_route(session_id)
    except lifecycle_delivery.LifecycleDeliveryError as exc:
        raise RuntimeProblem(str(exc), code=exc.code) from exc
    repo = (
        resolve_repo(str(route["repo_root"]))
        if route is not None
        else resolve_repo(args.repo)
    )
    ledgers = active_ledgers_for_session(repo, session_id)
    if not ledgers:
        return {
            "status": "NOOP",
            "owner_session_id": session_id,
            "active_runs": [],
        }, EXIT_PASS
    if len(ledgers) > 1:
        raise RuntimeProblem(
            "multiple active ledgers match owner_session_id",
            code="FATAL_AMBIGUOUS",
            details={
                "owner_session_id": session_id,
                "run_ids": [item[1]["run_id"] for item in ledgers],
            },
        )
    repo, selected = ledgers[0]
    run_id = str(selected["run_id"])
    with locked_run(repo, run_id) as ledger:
        facts = status_with_persisted_continuity(repo, ledger)
    if ledger["phase"] in {"integration_conflict", "finalize_conflict"}:
        return {"status": "CONFLICT", "message": "active run has preserved conflicts", **facts}, EXIT_FAIL
    if ledger["phase"] == "finalized_cleanup":
        return {
            "status": "NEEDS_USER",
            "message": "final commit exists but worktree cleanup is incomplete",
            **facts,
        }, EXIT_FAIL
    if ledger["phase"] == "continuity_failure":
        return {
            "status": "CONTINUITY_FAILURE",
            "message": "active run lost required agent or target continuity",
            **facts,
        }, EXIT_FAIL
    if ledger["phase"] == "iteration_limit":
        return {
            "status": "NEEDS_USER",
            "message": "verification iteration limit was reached",
            **facts,
        }, EXIT_FAIL
    if ledger["phase"] in {"no_progress", "architecture_review_required"}:
        return {
            "status": "NEEDS_USER",
            "message": "run progress requires an explicit user decision",
            "code": (
                "NO_PROGRESS"
                if ledger["phase"] == "no_progress"
                else "ARCHITECTURE_REVIEW_REQUIRED"
            ),
            **facts,
        }, EXIT_FAIL
    delivery = facts.get("lifecycle_delivery", {})
    if delivery.get("locator") != "ready" or delivery.get("blocked_event"):
        return {
            "status": "NEEDS_USER",
            "message": "run lifecycle delivery requires inspection",
            "code": "LIFECYCLE_DELIVERY_NOT_READY",
            **facts,
        }, EXIT_FAIL
    return {"status": "ACTIVE", "message": "one active run matched the session", **facts}, EXIT_PASS


def target_worktree(repo: Path, ledger: dict[str, Any]) -> tuple[Path, bool]:
    branch = str(ledger["target_branch"])
    existing = worktree_for_branch(repo, branch)
    if existing is not None:
        return existing, False
    path = state_root(repo) / "worktrees" / str(ledger["run_id"]) / "target"
    if path.exists():
        raise RuntimeProblem(
            f"target worktree path already exists: {path}",
            code="TARGET_WORKTREE_EXISTS",
        )
    result = git(repo, "worktree", "add", str(path), branch, check=False)
    if result.returncode != 0:
        raise RuntimeProblem(
            "cannot create target worktree",
            code="TARGET_WORKTREE_FAILED",
            details={"stdout": tail_text(result.stdout), "stderr": tail_text(result.stderr)},
        )
    return path, True


def staging_worktree(repo: Path, ledger: dict[str, Any], target_head: str) -> tuple[Path, str]:
    path = state_root(repo) / "worktrees" / str(ledger["run_id"]) / "finalize"
    branch = f"codex-loop/{ledger['run_id']}/finalize"
    if path.exists() or worktree_for_branch(repo, branch) is not None:
        raise RuntimeProblem(
            f"finalize staging worktree already exists: {path}",
            code="FINALIZE_WORKTREE_EXISTS",
        )
    result = git(repo, "worktree", "add", "-b", branch, str(path), target_head, check=False)
    if result.returncode != 0:
        raise RuntimeProblem(
            "cannot create finalize staging worktree",
            code="FINALIZE_WORKTREE_FAILED",
            details={"stdout": tail_text(result.stdout), "stderr": tail_text(result.stderr)},
        )
    return path, branch


def predicted_conflicts(repo: Path, target_head: str, candidate_head: str) -> list[str]:
    result = git(
        repo,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "--messages",
        target_head,
        candidate_head,
        check=False,
    )
    if result.returncode == 0:
        return []
    lines = result.stdout.splitlines()
    conflicts: list[str] = []
    for line in lines[1:]:
        value = line.strip()
        if not value:
            if conflicts:
                break
            continue
        if value.startswith(("CONFLICT", "Auto-merging", "hint:")):
            continue
        if " " not in value and not re.fullmatch(r"[0-9a-f]{40}", value):
            conflicts.append(value)
    return sorted(set(conflicts))


def cleanup_role_worktrees(
    repo: Path, ledger: dict[str, Any], *, force: bool = True
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for role in ("builder", "tester"):
        entry = ledger["worktrees"][role]
        path = Path(str(entry["path"]))
        branch = str(entry["branch"])
        if path.exists():
            remove_args = ["worktree", "remove"]
            if force:
                remove_args.append("--force")
            remove_args.append(str(path))
            removed = git(repo, *remove_args, check=False)
            if removed.returncode != 0:
                failures.append(
                    {"role": role, "operation": "worktree_remove", "stderr": tail_text(removed.stderr)}
                )
                continue
        deleted = git(repo, "branch", "-D", branch, check=False)
        if deleted.returncode != 0 and "not found" not in deleted.stderr.lower():
            failures.append(
                {"role": role, "operation": "branch_delete", "stderr": tail_text(deleted.stderr)}
            )
    return failures


def cleanup_finalized_worktrees(repo: Path, ledger: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    worktree_root = state_root(repo) / "worktrees" / str(ledger["run_id"])
    finalize_path = worktree_root / "finalize"
    finalize_branch = f"codex-loop/{ledger['run_id']}/finalize"
    if finalize_path.exists():
        removed = git(repo, "worktree", "remove", str(finalize_path), check=False)
        if removed.returncode != 0:
            failures.append(
                {
                    "role": "finalize",
                    "operation": "worktree_remove",
                    "stderr": tail_text(removed.stderr),
                }
            )
            return failures
    if not finalize_path.exists():
        deleted = git(repo, "branch", "-D", finalize_branch, check=False)
        if deleted.returncode != 0 and "not found" not in deleted.stderr.lower():
            failures.append(
                {
                    "role": "finalize",
                    "operation": "branch_delete",
                    "stderr": tail_text(deleted.stderr),
                }
            )
    target_path = worktree_root / "target"
    if target_path.exists():
        removed = git(repo, "worktree", "remove", str(target_path), check=False)
        if removed.returncode != 0:
            failures.append(
                {
                    "role": "target",
                    "operation": "worktree_remove",
                    "stderr": tail_text(removed.stderr),
                }
            )
    failures.extend(cleanup_role_worktrees(repo, ledger))
    return failures


def staged_target_blocked_result(
    intent: dict[str, Any], target_facts: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    return {
        "status": "NEEDS_USER",
        "message": "final commit is staged but target checkout blockers must be resolved",
        "code": "FINAL_COMMIT_STAGED_TARGET_BLOCKED",
        "candidate_head": intent.get("candidate_head"),
        "staged_final_head": intent.get("final_head"),
        "target_worktree": target_facts.get("target_worktree"),
        "target_residue": target_facts.get("target_residue"),
        "finalize_blockers": target_facts.get("finalize_blockers", []),
        "preserved": True,
    }, EXIT_FAIL


def recover_finalize_intent(
    repo: Path, ledger: dict[str, Any]
) -> tuple[dict[str, Any], int] | None:
    intent = ledger.get("finalize_intent")
    if ledger.get("phase") != "active" or not isinstance(intent, dict):
        return None
    candidate = str(intent.get("candidate_head") or "")
    candidate_tree = str(intent.get("candidate_tree") or "")
    target_head = str(intent.get("expected_target_head") or "")
    final_head = str(intent.get("final_head") or "")
    target_branch_name = str(intent.get("target_branch") or "")
    builder = Path(str(ledger["worktrees"]["builder"]["path"]))
    try:
        verify_plan_unchanged(ledger)
        ensure_role_pass(repo, ledger, "builder")
        if ledger.get("plan", {}).get("level") != "L1":
            ensure_role_pass(repo, ledger, "tester")
        gate_facts = status_facts(repo, ledger)
    except RuntimeProblem as exc:
        return {
            "status": "NEEDS_USER",
            "message": "finalize intent gates can no longer be proven",
            "code": "FINALIZE_INTENT_GATES_STALE",
            "error": str(exc),
            "intent": intent,
            "preserved": True,
        }, EXIT_FAIL
    stale_gates = [
        EVIDENCE_STATUS_FIELDS[key]
        for key in required_evidence_keys(ledger)
        if evidence_head(ledger, key) != candidate
    ]
    reviewer = ledger.get("agents", {}).get("reviewer")
    gate_errors: list[str] = []
    if gate_facts.get("builder_dirty_paths"):
        gate_errors.append("builder_dirty")
    if gate_facts.get("tester_dirty_paths"):
        gate_errors.append("tester_dirty")
    if gate_facts.get("prerequisites_ready") is not True:
        gate_errors.append("prerequisites")
    if ledger.get("plan", {}).get("level") != "L1" and gate_facts.get(
        "tester_fully_integrated"
    ) is not True:
        gate_errors.append("tester_integration")
    if stale_gates:
        gate_errors.append("evidence")
    if not reviewer_prerequisites_bound(ledger, reviewer, candidate):
        gate_errors.append("reviewer_prerequisites")
    if gate_errors:
        return {
            "status": "NEEDS_USER",
            "message": "finalize intent is preserved but its delivery gates are stale",
            "code": "FINALIZE_INTENT_GATES_STALE",
            "gate_errors": gate_errors,
            "stale_gates": stale_gates,
            "intent": intent,
            "preserved": True,
        }, EXIT_FAIL
    try:
        live_candidate = full_head(builder)
        final_parent = git(repo, "rev-parse", f"{final_head}^", check=True).stdout.strip()
        final_tree = git(repo, "rev-parse", f"{final_head}^{{tree}}", check=True).stdout.strip()
    except RuntimeProblem as exc:
        ledger["phase"] = "finalize_conflict"
        append_event(
            ledger,
            "finalize_intent_invalid",
            {"intent": intent, "error": str(exc)},
        )
        save_ledger(repo, ledger)
        return {
            "status": "CONFLICT",
            "message": "persisted finalize intent cannot be validated",
            "code": "FINALIZE_INTENT_INVALID",
            "intent": intent,
            "preserved": True,
        }, EXIT_FAIL
    if (
        target_branch_name != str(ledger["target_branch"])
        or live_candidate != candidate
        or worktree_residue(builder)
        or final_parent != target_head
        or final_tree != candidate_tree
    ):
        ledger["phase"] = "finalize_conflict"
        append_event(
            ledger,
            "finalize_intent_mismatch",
            {
                "intent": intent,
                "live_candidate": live_candidate,
                "builder_dirty_paths": worktree_residue(builder),
                "final_parent": final_parent,
                "final_tree": final_tree,
            },
        )
        save_ledger(repo, ledger)
        return {
            "status": "CONFLICT",
            "message": "persisted finalize intent no longer matches the preserved candidate",
            "code": "FINALIZE_INTENT_MISMATCH",
            "intent": intent,
            "preserved": True,
        }, EXIT_FAIL
    try:
        live_target_head = branch_head(repo, target_branch_name)
    except RuntimeProblem:
        live_target_head = None
    if live_target_head not in {target_head, final_head}:
        ledger["phase"] = "continuity_failure"
        append_event(
            ledger,
            "finalize_intent_target_diverged",
            {
                "expected_target_head": target_head,
                "final_head": final_head,
                "target_head": live_target_head,
            },
        )
        save_ledger(repo, ledger)
        return {
            "status": "CONTINUITY_FAILURE",
            "message": "target branch diverged from the persisted finalize intent",
            "code": "FINALIZE_INTENT_TARGET_DIVERGED",
            "expected_target_head": target_head,
            "final_head": final_head,
            "target_head": live_target_head,
            "preserved": True,
        }, EXIT_FAIL

    target, target_temporary = target_worktree(repo, ledger)
    target_facts = target_checkout_facts(
        repo,
        target_branch_name,
        expected_head=target_head,
        desired_head=final_head,
        live_target_head=live_target_head,
        workspace_intake=ledger.get("workspace_intake"),
    )
    if target_facts["finalize_blockers"]:
        return staged_target_blocked_result(intent, target_facts)

    cas_performed = False
    if live_target_head == target_head:
        cas = git(
            repo,
            "update-ref",
            f"refs/heads/{target_branch_name}",
            final_head,
            target_head,
            check=False,
        )
        if cas.returncode != 0:
            current = branch_head(repo, target_branch_name)
            ledger["phase"] = "continuity_failure"
            append_event(
                ledger,
                "finalize_intent_cas_failed",
                {
                    "expected_target_head": target_head,
                    "final_head": final_head,
                    "target_head": current,
                    "stderr": tail_text(cas.stderr),
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "CONTINUITY_FAILURE",
                "message": "target changed while recovering the finalize intent",
                "code": "FINALIZE_FAST_FORWARD_CAS_FAILED",
                "expected_target_head": target_head,
                "final_head": final_head,
                "target_head": current,
                "preserved": True,
            }, EXIT_FAIL
        cas_performed = True

    intake_sync = bool(target_facts.get("workspace_intake_allowed_paths"))
    if intake_sync:
        for captured in ledger.get("workspace_intake", {}).get("entries", []):
            if not isinstance(captured, dict) or not isinstance(captured.get("path"), str):
                continue
            try:
                final_state = workspace_contract.tree_path_state(
                    repo, final_head, str(captured["path"])
                )
            except workspace_contract.WorkspaceError as exc:
                return {
                    "status": "NEEDS_USER",
                    "message": "final workspace intake state cannot be proven",
                    "code": exc.code,
                    "path": captured.get("path"),
                    "details": exc.details,
                    "preserved": True,
                }, EXIT_FAIL
            if final_state.get("worktree") is None:
                entry_path = target / str(captured["path"])
                if entry_path.is_file() and not entry_path.is_symlink():
                    entry_path.unlink()
        checkout = git(target, "reset", "--hard", final_head, check=False)
        if checkout.returncode != 0:
            append_event(
                ledger,
                "finalize_intake_sync_incomplete",
                {
                    "final_head": final_head,
                    "target_worktree": str(target),
                    "paths": target_facts.get("workspace_intake_allowed_paths"),
                    "stdout": tail_text(checkout.stdout),
                    "stderr": tail_text(checkout.stderr),
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "NEEDS_USER",
                "message": "authorized workspace intake could not yet be synchronized",
                "code": "FINALIZE_INTAKE_SYNC_INCOMPLETE",
                "final_head": final_head,
                "target_worktree": str(target),
                "paths": target_facts.get("workspace_intake_allowed_paths"),
                "stderr": tail_text(checkout.stderr),
                "preserved": True,
            }, EXIT_FAIL
    else:
        expected_tree = git(
            repo, "rev-parse", f"{target_head}^{{tree}}", check=True
        ).stdout.strip()
        index_result = git(target, "write-tree", check=False)
        if index_result.returncode != 0:
            return {
                "status": "NEEDS_USER",
                "message": "target index changed while applying the finalize intent",
                "code": "FINALIZE_RECOVERY_INDEX_MISMATCH",
                "final_head": final_head,
                "target_worktree": str(target),
                "stdout": tail_text(index_result.stdout),
                "stderr": tail_text(index_result.stderr),
                "preserved": True,
            }, EXIT_FAIL
        index_tree = index_result.stdout.strip()
        unstaged = target_worktree_unstaged_residue(repo, target)
        if unstaged:
            return {
                "status": "NEEDS_USER",
                "message": "target worktree changed while recovering the finalize intent",
                "code": "TARGET_DIRTY_AFTER_FINALIZE",
                "final_head": final_head,
                "target_worktree": str(target),
                "dirty_paths": unstaged,
            }, EXIT_FAIL
        if index_tree == expected_tree:
            checkout = git(
                target,
                "read-tree",
                "-u",
                "-m",
                target_head,
                final_head,
                check=False,
            )
            if checkout.returncode != 0:
                if cas_performed:
                    rollback = git(
                        repo,
                        "update-ref",
                        f"refs/heads/{target_branch_name}",
                        target_head,
                        final_head,
                        check=False,
                    )
                    ledger["phase"] = (
                        "finalize_conflict"
                        if rollback.returncode == 0
                        else "continuity_failure"
                    )
                    append_event(
                        ledger,
                        "finalize_worktree_sync_failed",
                        {
                            "candidate_head": candidate,
                            "staged_final_head": final_head,
                            "target_head": branch_head(repo, target_branch_name),
                            "target_worktree": str(target),
                            "checkout_stdout": tail_text(checkout.stdout),
                            "checkout_stderr": tail_text(checkout.stderr),
                            "rollback_returncode": rollback.returncode,
                            "rollback_stderr": tail_text(rollback.stderr),
                        },
                    )
                    save_ledger(repo, ledger)
                    return {
                        "status": (
                            "CONFLICT"
                            if rollback.returncode == 0
                            else "CONTINUITY_FAILURE"
                        ),
                        "message": "target ref update could not be synchronized to its worktree",
                        "code": "FINALIZE_WORKTREE_SYNC_FAILED",
                        "staged_final_head": final_head,
                        "target_head": branch_head(repo, target_branch_name),
                        "target_worktree": str(target),
                        "ref_rolled_back": rollback.returncode == 0,
                        "preserved": True,
                    }, EXIT_FAIL
                return {
                    "status": "NEEDS_USER",
                    "message": "final ref could not be synchronized to its target worktree",
                    "code": "FINALIZE_RECOVERY_SYNC_FAILED",
                    "final_head": final_head,
                    "target_worktree": str(target),
                    "stderr": tail_text(checkout.stderr),
                    "preserved": True,
                }, EXIT_FAIL
        elif index_tree != candidate_tree:
            return {
                "status": "NEEDS_USER",
                "message": "target index no longer matches either side of the finalize intent",
                "code": "FINALIZE_RECOVERY_INDEX_MISMATCH",
                "final_head": final_head,
                "target_worktree": str(target),
                "index_tree": index_tree,
                "expected_tree": expected_tree,
                "candidate_tree": candidate_tree,
                "preserved": True,
            }, EXIT_FAIL
    post_target_head = branch_head(repo, target_branch_name)
    post_index_result = git(target, "write-tree", check=False)
    if post_index_result.returncode != 0:
        return {
            "status": "NEEDS_USER",
            "message": "target index cannot prove the finalize postcondition",
            "code": "FINALIZE_RECOVERY_POSTCONDITION",
            "final_head": final_head,
            "target_worktree": str(target),
            "stdout": tail_text(post_index_result.stdout),
            "stderr": tail_text(post_index_result.stderr),
            "preserved": True,
        }, EXIT_FAIL
    post_index_tree = post_index_result.stdout.strip()
    post_facts = target_checkout_facts(
        repo,
        target_branch_name,
        expected_head=target_head,
        desired_head=final_head,
        live_target_head=post_target_head,
        workspace_intake=ledger.get("workspace_intake"),
    )
    if post_target_head != final_head or post_index_tree != candidate_tree:
        ledger["phase"] = "finalize_conflict"
        append_event(
            ledger,
            "finalize_fast_forward_postcondition_failed",
            {
                "candidate_tree": candidate_tree,
                "final_head": final_head,
                "target_head": post_target_head,
                "target_tree": post_index_tree,
            },
        )
        save_ledger(repo, ledger)
        return {
            "status": "CONFLICT",
            "message": "target fast-forward postcondition failed",
            "code": "FINALIZE_FAST_FORWARD_POSTCONDITION",
            "final_head": final_head,
            "target_head": post_target_head,
            "target_worktree": str(target),
            "preserved": True,
        }, EXIT_FAIL
    if post_facts["finalize_blockers"]:
        return {
            "status": "NEEDS_USER",
            "message": "finalize intent recovery postcondition is not clean",
            "code": "FINALIZE_RECOVERY_POSTCONDITION",
            "final_head": final_head,
            "target_worktree": str(target),
            "target_residue": post_facts["target_residue"],
            "finalize_blockers": post_facts["finalize_blockers"],
            "preserved": True,
        }, EXIT_FAIL
    ledger["phase"] = "finalized_cleanup"
    ledger["final_head"] = final_head
    append_event(
        ledger,
        "finalize_intent_recovered",
        {
            "candidate_head": candidate,
            "final_head": final_head,
            "target_previous_head": target_head,
            "target_worktree": str(target),
            "target_worktree_temporary": target_temporary,
        },
    )
    save_ledger(repo, ledger)
    return None


def finish_finalized_cleanup(
    repo: Path, ledger: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    final_head = str(ledger.get("final_head") or "")
    try:
        live_target_head = branch_head(repo, str(ledger["target_branch"]))
    except RuntimeProblem:
        live_target_head = None
    if not final_head or live_target_head != final_head:
        ledger["phase"] = "continuity_failure"
        append_event(
            ledger,
            "finalized_target_continuity_failure",
            {
                "expected_final_head": final_head or None,
                "target_head": live_target_head,
                "target_branch": ledger["target_branch"],
            },
        )
        save_ledger(repo, ledger)
        return {
            "status": "CONTINUITY_FAILURE",
            "message": "target branch no longer points to the staged final commit",
            "code": "FINALIZED_TARGET_CONTINUITY_FAILURE",
            "final_head": final_head or None,
            "target_head": live_target_head,
        }, EXIT_FAIL

    target_facts = target_checkout_facts(
        repo,
        str(ledger["target_branch"]),
        expected_head=final_head,
        desired_head=final_head,
        live_target_head=live_target_head,
        workspace_intake=ledger.get("workspace_intake"),
    )
    if target_facts["finalize_blockers"]:
        return {
            "status": "NEEDS_USER",
            "message": "final commit exists but the target worktree has tracked residue",
            "code": "TARGET_DIRTY_AFTER_FINALIZE",
            "final_head": final_head,
            **target_facts,
        }, EXIT_FAIL

    cleanup_failures = cleanup_finalized_worktrees(repo, ledger)
    if cleanup_failures:
        append_event(ledger, "finalize_cleanup_retry_failed", {"failures": cleanup_failures})
        save_ledger(repo, ledger)
        return {
            "status": "NEEDS_USER",
            "message": "final commit exists but worktree cleanup is still incomplete",
            "code": "FINALIZE_CLEANUP_INCOMPLETE",
            "final_head": final_head,
            "cleanup_failures": cleanup_failures,
        }, EXIT_FAIL

    live_target_head = branch_head(repo, str(ledger["target_branch"]))
    if live_target_head != final_head:
        ledger["phase"] = "continuity_failure"
        append_event(
            ledger,
            "finalized_target_continuity_failure",
            {
                "expected_final_head": final_head,
                "target_head": live_target_head,
                "target_branch": ledger["target_branch"],
                "after_cleanup": True,
            },
        )
        save_ledger(repo, ledger)
        return {
            "status": "CONTINUITY_FAILURE",
            "message": "target branch moved during finalize cleanup",
            "code": "FINALIZED_TARGET_CONTINUITY_FAILURE",
            "final_head": final_head,
            "target_head": live_target_head,
        }, EXIT_FAIL

    ledger["phase"] = "finalized"
    append_event(ledger, "finalize_cleanup_completed", {})
    save_ledger(repo, ledger)
    lifecycle_delivery.deactivate_route(str(ledger["owner_session_id"]))
    lifecycle_delivery.remove_route(
        str(ledger["owner_session_id"]), require_empty=True
    )
    continuation = preparation_continuation_facts(repo, ledger)
    return {
        "status": "COMPLETE",
        "message": "candidate finalized as one squash commit",
        "candidate_head": (
            ledger.get("finalize_intent", {}).get("candidate_head")
            if isinstance(ledger.get("finalize_intent"), dict)
            else None
        ),
        "final_head": final_head,
        "target_branch": ledger["target_branch"],
        "commit_count": 1,
        "cleanup_failures": [],
        "continuation": continuation,
        "marker": continuation_ready_marker(ledger, continuation),
    }, EXIT_PASS


def cmd_finalize(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        if ledger["phase"] == "finalized":
            continuation = preparation_continuation_facts(repo, ledger)
            return {
                "status": "NOOP",
                "message": "run was already finalized",
                "run_id": ledger["run_id"],
                "final_head": ledger.get("final_head"),
                "continuation": continuation,
                "marker": continuation_ready_marker(ledger, continuation),
            }, EXIT_PASS
        recovered = recover_finalize_intent(repo, ledger)
        if recovered is not None:
            return recovered
        if ledger["phase"] == "finalized_cleanup":
            return finish_finalized_cleanup(repo, ledger)
        if ledger["phase"] != "active":
            phase_result = "NEEDS_USER"
            if ledger["phase"] in {"integration_conflict", "finalize_conflict"}:
                phase_result = "CONFLICT"
            elif ledger["phase"] == "continuity_failure":
                phase_result = "CONTINUITY_FAILURE"
            raise RuntimeProblem(
                "finalize requires active phase",
                result=phase_result,
                code="PHASE_NOT_ACTIVE",
                details={"phase": ledger["phase"]},
                exit_code=EXIT_FAIL,
            )
        verify_plan_unchanged(ledger)
        ensure_role_pass(repo, ledger, "builder")
        if ledger["plan"].get("level") != "L1":
            ensure_role_pass(repo, ledger, "tester")
        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        tester = Path(str(ledger["worktrees"]["tester"]["path"]))
        if worktree_residue(builder):
            raise RuntimeProblem(
                "builder worktree is dirty; verify the candidate first",
                result="NEEDS_USER",
                code="CANDIDATE_DIRTY",
            )
        tester_dirty = worktree_residue(tester)
        if tester_dirty:
            raise RuntimeProblem(
                "tester worktree has unintegrated dirty changes",
                result="NEEDS_USER",
                code="TESTER_DIRTY",
                details={"tester_dirty_paths": tester_dirty},
                exit_code=EXIT_FAIL,
            )
        candidate = full_head(builder)
        tester_head = full_head(tester)
        if (
            ledger["plan"].get("level") != "L1"
            and tester_head != ledger["tester_integration"]["source_head"]
        ):
            raise RuntimeProblem(
                "tester has changes that are not integrated",
                result="NEEDS_USER",
                code="TESTER_NOT_INTEGRATED",
                details={
                    "tester_head": tester_head,
                    "integrated_source_head": ledger["tester_integration"]["source_head"],
                },
            )
        if ledger["plan"].get("level") != "L1":
            integration = ledger["tester_integration"]
            if not (
                integration.get("completed") is True
                and integration.get("author_agent_id")
                and integration.get("author_turn_id")
                and integration.get("author_head")
            ):
                raise RuntimeProblem(
                    "finalize requires persisted Tester author and integration evidence",
                    result="NEEDS_USER",
                    code="TESTER_AUTHOR_INTEGRATION_MISSING",
                    details={"tester_integration": integration},
                    exit_code=EXIT_FAIL,
                )
        required_keys = required_evidence_keys(ledger)
        required = [EVIDENCE_STATUS_FIELDS[key] for key in required_keys]
        stale = {
            EVIDENCE_STATUS_FIELDS[key]: evidence_head(ledger, key)
            for key in required_keys
            if evidence_head(ledger, key) != candidate
        }
        if stale:
            raise RuntimeProblem(
                "all evidence heads must equal candidate before finalize",
                result="NEEDS_USER",
                code="EVIDENCE_STALE_OR_MISSING",
                details={
                    "candidate_head": candidate,
                    "required_gates": required,
                    "missing_gates": [field for field, value in stale.items() if value is None],
                    "stale_gates": [field for field, value in stale.items() if value is not None],
                    "evidence": stale,
                },
                exit_code=EXIT_FAIL,
            )
        target_branch_name = str(ledger["target_branch"])
        try:
            target_head = branch_head(repo, target_branch_name)
        except RuntimeProblem as exc:
            if exc.code != "INVALID_GIT_REF":
                raise
            ledger["phase"] = "continuity_failure"
            append_event(
                ledger,
                "target_continuity_failure",
                {
                    "candidate_head": candidate,
                    "target_head": None,
                    "target_start_head": ledger["target_start_head"],
                    "target_branch": target_branch_name,
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "CONTINUITY_FAILURE",
                "message": "target branch was deleted after the run started",
                "code": "TARGET_CONTINUITY_FAILURE",
                "candidate_head": candidate,
                "target_head": None,
                "target_start_head": ledger["target_start_head"],
                "preserved": True,
            }, EXIT_FAIL
        if target_head != ledger["target_start_head"] or target_head != ledger["spec_head"]:
            conflicts = predicted_conflicts(repo, target_head, candidate)
            if conflicts:
                ledger["phase"] = "finalize_conflict"
                append_event(
                    ledger,
                    "finalize_conflict_predicted",
                    {
                        "candidate_head": candidate,
                        "target_head": target_head,
                        "conflict_files": conflicts,
                        "mutation_attempted": False,
                    },
                )
                save_ledger(repo, ledger)
                return {
                    "status": "CONFLICT",
                    "message": "target moved and conflicts with the verified candidate; no worktree was mutated",
                    "code": "TARGET_CONFLICT",
                    "candidate_head": candidate,
                    "target_head": target_head,
                    "conflict_files": conflicts,
                    "preserved": True,
                }, EXIT_FAIL
            ledger["phase"] = "continuity_failure"
            append_event(
                ledger,
                "target_continuity_failure",
                {
                    "candidate_head": candidate,
                    "target_head": target_head,
                    "target_start_head": ledger["target_start_head"],
                    "spec_head": ledger["spec_head"],
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "CONTINUITY_FAILURE",
                "message": "target branch moved after evidence was recorded",
                "code": "TARGET_CONTINUITY_FAILURE",
                "candidate_head": candidate,
                "target_head": target_head,
                "target_start_head": ledger["target_start_head"],
                "preserved": True,
            }, EXIT_FAIL
        artifact_dir = run_dir(repo, str(ledger["run_id"])) / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        patch_path = artifact_dir / f"candidate-{candidate[:12]}.patch"
        patch = git(builder, "diff", "--binary", "--full-index", str(ledger["spec_head"]), candidate, "--", check=True).stdout
        patch_path.write_text(patch, encoding="utf-8")
        intake_required = ledger.get("workspace_intake", {}).get("required") is True
        if not patch and not intake_required:
            raise RuntimeProblem(
                "candidate has no diff from spec_head",
                result="NEEDS_USER",
                code="EMPTY_CANDIDATE",
            )
        staging, finalize_branch = staging_worktree(repo, ledger, target_head)
        apply_result = (
            run_process(
                ["git", "-C", str(staging), "apply", "--3way", "--index", str(patch_path)],
                check=False,
            )
            if patch
            else CommandResult(0, "", "")
        )
        if apply_result.returncode != 0:
            conflicts = unmerged_paths(staging)
            ledger["phase"] = "finalize_conflict"
            append_event(
                ledger,
                "finalize_conflict",
                {
                    "candidate_head": candidate,
                    "target_head": target_head,
                    "staging_worktree": str(staging),
                    "staging_branch": finalize_branch,
                    "patch_path": str(patch_path),
                    "unmerged_paths": conflicts,
                    "stderr": tail_text(apply_result.stderr),
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "CONFLICT",
                "message": "finalize patch conflicted in staging; target branch was not moved",
                "code": "FINALIZE_CONFLICT",
                "staging_worktree": str(staging),
                "staging_branch": finalize_branch,
                "patch_path": str(patch_path),
                "unmerged_paths": conflicts,
                "preserved": True,
            }, EXIT_FAIL

        index_tree = git(staging, "write-tree", check=True).stdout.strip()
        candidate_tree = git(builder, "rev-parse", f"{candidate}^{{tree}}", check=True).stdout.strip()
        if index_tree != candidate_tree:
            ledger["phase"] = "finalize_conflict"
            append_event(
                ledger,
                "finalize_tree_mismatch",
                {
                    "candidate_head": candidate,
                    "candidate_tree": candidate_tree,
                    "index_tree": index_tree,
                    "staging_worktree": str(staging),
                    "patch_path": str(patch_path),
                },
            )
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "staged finalize tree does not equal candidate tree; scene preserved",
                result="CONFLICT",
                code="FINALIZE_TREE_MISMATCH",
                details={
                    "candidate_tree": candidate_tree,
                    "index_tree": index_tree,
                    "staging_worktree": str(staging),
                },
            )
        message = args.message or f"feat: codex builder loop {ledger['run_id']}"
        commit_args = ["commit"]
        if not patch:
            commit_args.append("--allow-empty")
        commit_args.extend(["-m", message])
        commit = git(staging, *commit_args, check=False)
        if commit.returncode != 0:
            ledger["phase"] = "finalize_conflict"
            append_event(
                ledger,
                "finalize_commit_failed",
                {
                    "candidate_head": candidate,
                    "staging_worktree": str(staging),
                    "staging_branch": finalize_branch,
                    "stdout": tail_text(commit.stdout),
                    "stderr": tail_text(commit.stderr),
                },
            )
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "squash commit failed; staged scene preserved",
                result="CONFLICT",
                code="FINALIZE_COMMIT_FAILED",
                details={"staging_worktree": str(staging), "stderr": tail_text(commit.stderr)},
            )
        final_head = full_head(staging)
        final_tree = git(staging, "rev-parse", f"{final_head}^{{tree}}", check=True).stdout.strip()
        if final_tree != candidate_tree:
            ledger["phase"] = "finalize_conflict"
            append_event(
                ledger,
                "finalize_commit_tree_mismatch",
                {
                    "candidate_head": candidate,
                    "candidate_tree": candidate_tree,
                    "final_head": final_head,
                    "final_tree": final_tree,
                    "staging_worktree": str(staging),
                },
            )
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "repository commit hooks changed the staged tree; target branch was not moved",
                result="CONFLICT",
                code="FINAL_COMMIT_TREE_MISMATCH",
                details={"candidate_tree": candidate_tree, "final_tree": final_tree},
                exit_code=EXIT_FAIL,
            )
        parent = git(staging, "rev-parse", f"{final_head}^", check=True).stdout.strip()
        if parent != target_head:
            ledger["phase"] = "finalize_conflict"
            append_event(
                ledger,
                "finalize_commit_parent_mismatch",
                {
                    "candidate_head": candidate,
                    "final_head": final_head,
                    "parent": parent,
                    "target_head": target_head,
                    "staging_worktree": str(staging),
                },
            )
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "final commit is not a single child of target head",
                result="CONFLICT",
                code="FINALIZE_NOT_SINGLE_COMMIT",
                details={"final_head": final_head, "parent": parent, "target_head": target_head},
                exit_code=EXIT_FAIL,
            )
        live_target_head = branch_head(repo, target_branch_name)
        if live_target_head != target_head:
            ledger["phase"] = "continuity_failure"
            append_event(
                ledger,
                "target_continuity_failure",
                {
                    "candidate_head": candidate,
                    "staged_final_head": final_head,
                    "target_head": live_target_head,
                    "target_start_head": target_head,
                    "mutation_attempted": False,
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "CONTINUITY_FAILURE",
                "message": "target branch moved while final commit was staged; target was not updated",
                "code": "TARGET_CONTINUITY_FAILURE",
                "candidate_head": candidate,
                "staged_final_head": final_head,
                "target_head": live_target_head,
                "target_start_head": target_head,
                "preserved": True,
            }, EXIT_FAIL

        ledger["finalize_intent"] = {
            "candidate_head": candidate,
            "candidate_tree": candidate_tree,
            "expected_target_head": target_head,
            "final_head": final_head,
            "target_branch": target_branch_name,
            "created_at": utc_now(),
        }
        append_event(
            ledger,
            "finalize_intent_recorded",
            dict(ledger["finalize_intent"]),
        )
        save_ledger(repo, ledger)
        recovered = recover_finalize_intent(repo, ledger)
        if recovered is not None:
            return recovered
        if ledger["phase"] != "finalized_cleanup":
            raise RuntimeProblem(
                "finalize intent did not reach cleanup state",
                code="FINALIZE_INTENT_STATE_INVALID",
                details={"phase": ledger["phase"], "finalize_intent": ledger["finalize_intent"]},
            )
        return finish_finalized_cleanup(repo, ledger)


def cmd_abandon(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        if ledger["phase"] == "abandoned":
            snapshot = problem_inventory(ledger).get("snapshot")
            return {
                "status": "COMPLETE",
                "message": "run was already abandoned",
                "run_id": ledger["run_id"],
                "phase": "abandoned",
                "worktrees": ledger["worktrees"],
                "worktrees_preserved": True,
                "problem_snapshot_sha256": (
                    snapshot.get("sha256") if isinstance(snapshot, dict) else None
                ),
                "problem_ids": (
                    snapshot.get("problem_ids", []) if isinstance(snapshot, dict) else []
                ),
                "problem_count": (
                    len(snapshot.get("problem_ids", []))
                    if isinstance(snapshot, dict)
                    else 0
                ),
            }, EXIT_PASS
        if ledger["phase"] in {"finalized", "finalized_cleanup"}:
            raise RuntimeProblem(
                f"run is already {ledger['phase']}",
                result="NEEDS_USER",
                code="RUN_TERMINAL",
                exit_code=EXIT_FAIL,
            )
        intent = ledger.get("finalize_intent")
        if isinstance(intent, dict):
            try:
                live_target = branch_head(repo, str(ledger["target_branch"]))
            except RuntimeProblem:
                live_target = None
            if live_target != intent.get("expected_target_head"):
                raise RuntimeProblem(
                    "cannot abandon while a finalize intent may already own the target ref",
                    result="NEEDS_USER",
                    code="FINALIZE_INTENT_ACTIVE",
                    details={"target_head": live_target, "finalize_intent": intent},
                    exit_code=EXIT_FAIL,
                )
        require_problem_sources_recorded(ledger)
        snapshot = seal_problem_snapshot(ledger, backfilled=False)
        previous = str(ledger["phase"])
        ledger["phase"] = "abandoned"
        append_event(
            ledger,
            "abandoned",
            {
                "previous_phase": previous,
                "reason": args.reason or "",
                "builder_head": full_head(Path(str(ledger["worktrees"]["builder"]["path"]))),
                "tester_head": full_head(Path(str(ledger["worktrees"]["tester"]["path"]))),
                "worktrees_preserved": True,
            },
        )
        save_ledger(repo, ledger)
        lifecycle_delivery.deactivate_route(str(ledger["owner_session_id"]))
        lifecycle_delivery.remove_route(
            str(ledger["owner_session_id"]), require_empty=True
        )
        return {
            "status": "COMPLETE",
            "message": "run abandoned with all worktrees preserved",
            "run_id": ledger["run_id"],
            "phase": "abandoned",
            "worktrees": ledger["worktrees"],
            "worktrees_preserved": True,
            "problem_snapshot_sha256": snapshot["sha256"],
            "problem_ids": snapshot["problem_ids"],
            "problem_count": len(snapshot["problem_ids"]),
        }, EXIT_PASS


def cmd_resume(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    reason = str(args.reason or "").strip()
    if not reason:
        raise RuntimeProblem("resume requires a non-empty --reason", code="RESUME_REASON_REQUIRED")
    with locked_run(repo, run_id) as ledger:
        phase = str(ledger.get("phase"))
        if phase not in {"no_progress", "architecture_review_required"}:
            raise RuntimeProblem(
                "resume is only valid for a progress stop",
                result="NEEDS_USER",
                code="RESUME_NOT_APPLICABLE",
                details={"phase": phase},
                exit_code=EXIT_FAIL,
            )
        attempts = verification_attempt_count(ledger)
        maximum = int(ledger.get("loop_config", {}).get("max_iterations", 0))
        if attempts >= maximum:
            ledger["phase"] = "iteration_limit"
            append_event(
                ledger,
                "iteration_limit_reached",
                {"verification_attempts": attempts, "max_iterations": maximum},
            )
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "verification iteration limit was reached",
                result="NEEDS_USER",
                code="ITERATION_LIMIT_REACHED",
                details={"verification_attempts": attempts, "max_iterations": maximum},
                exit_code=EXIT_FAIL,
            )
        progress_source = latest_progress_stop_source(ledger)
        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        resume = {
            "from_phase": phase,
            "progress_source": progress_source,
            "reason": reason,
            "attempts": attempts,
            "candidate_head": full_head(builder),
            "at": utc_now(),
        }
        ledger.setdefault("verification", {}).setdefault("resumes", []).append(resume)
        ledger["phase"] = "active"
        append_event(ledger, "verification_resumed", resume)
        save_ledger(repo, ledger)
        correction_progress = tester_correction_progress(ledger)
        return {
            "status": "READY",
            "message": "verification progress stop was explicitly resumed",
            "run_id": run_id,
            "reason": reason,
            "progress_source": progress_source,
            "verification_attempts": attempts,
            "max_iterations": maximum,
            "tester_correction_progress": correction_progress,
        }, EXIT_PASS


def _doctor_ledgers(repo: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reports: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    referenced_worktrees: set[str] = set()
    runs_root = state_root(repo) / "runs"
    for path in sorted(runs_root.glob("*/ledger.json")) if runs_root.exists() else []:
        try:
            ledger = read_json(path)
            recorded_repo = resolve_repo(str(ledger["repo_root"]))
            if recorded_repo != repo:
                raise RuntimeProblem("ledger repo_root mismatch", code="RUN_PATH_MISMATCH")
            run_id = str(ledger["run_id"])
            terminal_cleaned = any(
                item.get("type") == "terminal_worktrees_cleaned"
                for item in ledger.get("events", [])
                if isinstance(item, dict)
            )
            role_facts: dict[str, Any] = {}
            for role in ("builder", "tester"):
                entry = ledger.get("worktrees", {}).get(role, {})
                raw_role_path = str(entry.get("path") or "")
                role_path = Path(raw_role_path) if raw_role_path else None
                if raw_role_path:
                    referenced_worktrees.add(str(role_path.resolve()))
                exists = bool(role_path and role_path.exists())
                role_facts[role] = {
                    "path": raw_role_path or None,
                    "branch": entry.get("branch"),
                    "exists": exists,
                    "head": full_head(role_path) if exists and role_path is not None else None,
                    "residue": worktree_residue(role_path) if exists and role_path is not None else [],
                }
                if not exists and ledger.get("phase") not in {"finalized"} and not terminal_cleaned:
                    issues.append(
                        {
                            "code": "OWNED_WORKTREE_MISSING",
                            "run_id": run_id,
                            "role": role,
                            "path": raw_role_path,
                        }
                    )
            problem_facts = problem_inventory_facts(ledger)
            lifecycle_facts = lifecycle_delivery.delivery_facts(
                session_id=str(ledger["owner_session_id"]),
                repo_root=str(repo),
                run_id=run_id,
            )
            if ledger.get("phase") in ACTIVE_PHASES and (
                lifecycle_facts.get("locator") != "ready"
                or lifecycle_facts.get("queued_count")
                or lifecycle_facts.get("blocked_event")
            ):
                issues.append(
                    {
                        "code": "LIFECYCLE_DELIVERY_NOT_READY",
                        "run_id": run_id,
                        "lifecycle_delivery": lifecycle_facts,
                    }
                )
            if ledger.get("phase") == "continuity_failure":
                issues.append(
                    {
                        "code": "RUN_CONTINUITY_FAILURE",
                        "run_id": run_id,
                        "action": "preserve the run and inspect its structured ledger events",
                    }
                )
            reports.append(
                {
                    "run_id": run_id,
                    "phase": ledger.get("phase"),
                    "schema_version": ledger.get("schema_version"),
                    "runtime_identity": ledger.get("runtime_identity"),
                    "ledger": str(path),
                    "worktrees": role_facts,
                    "workspace_intake": ledger.get("workspace_intake"),
                    "lifecycle_delivery": lifecycle_facts,
                    "evidence": ledger.get("evidence"),
                    "finalize_intent": ledger.get("finalize_intent"),
                    "verification_attempts": verification_attempt_count(ledger),
                    "interface_publication_contract_version": ledger.get(
                        "plan", {}
                    ).get(
                        "interface_publication_contract_version",
                        LEGACY_INTERFACE_PUBLICATION_CONTRACT_VERSION,
                    ),
                    "interface_input_paths": sorted(
                        str(interface_path)
                        for interface_path in ledger.get("plan", {}).get(
                            "interface_input_paths", []
                        )
                    ),
                    "tester_correction_progress": tester_correction_progress(ledger),
                    "problem_inventory": problem_facts,
                    "pending_problem_sources": [
                        str(item.get("source_id"))
                        for item in problem_facts["missing_problem_sources"]
                    ],
                    "problem_count": problem_facts["problem_count"],
                    "inherited_problem_count": problem_facts[
                        "inherited_problem_count"
                    ],
                    "problem_snapshot_sha256": problem_facts["snapshot_sha256"],
                    "problem_ids": problem_facts["snapshot_problem_ids"],
                    "legacy_problem_snapshot_required": bool(
                        ledger.get("phase") == "abandoned"
                        and not isinstance(
                            ledger.get("problem_inventory", {}).get("snapshot"), dict
                        )
                    ),
                }
            )
        except (KeyError, RuntimeProblem) as exc:
            issues.append(
                {"code": "LEDGER_INVALID", "ledger": str(path), "error": str(exc)}
            )
    for candidate in repository_worktrees(repo):
        resolved = str(candidate.resolve())
        if candidate == repo or resolved in referenced_worktrees:
            continue
        try:
            branch = current_branch(candidate) if candidate.exists() else None
        except RuntimeProblem:
            branch = None
        if branch and branch.startswith("codex-loop/"):
            issues.append(
                {
                    "code": "ORPHAN_LOOP_WORKTREE",
                    "path": resolved,
                    "branch": branch,
                    "head": full_head(candidate),
                    "residue": worktree_residue(candidate),
                    "action": "inspect manually; unknown worktrees are never adopted or deleted automatically",
                }
            )
    return reports, issues


def cmd_doctor(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    selected_run_id: str | None = None
    if args.run:
        repo, selected_run_id, _ledger = resolve_run_selector(args.repo, args.run)
    else:
        repo = resolve_repo(args.repo)
    reports, issues = _doctor_ledgers(repo)
    if selected_run_id is not None:
        reports = [
            item for item in reports if item.get("run_id") == selected_run_id
        ]
        issues = [
            item
            for item in issues
            if item.get("run_id") in {None, selected_run_id}
        ]
        if not reports:
            raise RuntimeProblem(
                f"run not found: {selected_run_id}", code="RUN_NOT_FOUND"
            )
    selected_lifecycle = (
        reports[0].get("lifecycle_delivery")
        if selected_run_id is not None and len(reports) == 1
        else None
    )
    selected_contract = (
        {
            "interface_publication_contract_version": reports[0].get(
                "interface_publication_contract_version"
            ),
            "interface_input_paths": reports[0].get("interface_input_paths", []),
        }
        if selected_run_id is not None and len(reports) == 1
        else {}
    )
    return {
        "status": "READY" if not issues else "NEEDS_USER",
        "message": (
            "repository builder-loop state is healthy"
            if not issues
            else "repository builder-loop state has preserved issues"
        ),
        "repo": str(repo),
        "runs": reports,
        "issues": issues,
        "lifecycle_delivery": selected_lifecycle,
        "read_only": True,
        **selected_contract,
    }, EXIT_PASS if not issues else EXIT_FAIL


def cmd_recover(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        recovered = recover_finalize_intent(repo, ledger)
        if recovered is not None:
            return recovered
        if ledger.get("phase") == "finalized_cleanup":
            return finish_finalized_cleanup(repo, ledger)
        return {
            "status": "NOOP",
            "message": "run has no persisted safe recovery transaction",
            "run_id": run_id,
            "phase": ledger.get("phase"),
        }, EXIT_PASS


def _terminal_expected_heads(ledger: dict[str, Any]) -> dict[str, str | None]:
    if ledger.get("phase") == "abandoned":
        for event in reversed(ledger.get("events", [])):
            if event.get("type") == "abandoned":
                facts = event.get("facts", {})
                return {
                    "builder": facts.get("builder_head"),
                    "tester": facts.get("tester_head"),
                }
    return {
        "builder": ledger.get("finalize_intent", {}).get("candidate_head")
        if isinstance(ledger.get("finalize_intent"), dict)
        else None,
        "tester": ledger.get("tester_integration", {}).get("source_head"),
    }


def cmd_cleanup(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        if ledger.get("phase") not in {"finalized", "abandoned"}:
            raise RuntimeProblem(
                "safe cleanup only accepts terminal runs",
                result="NEEDS_USER",
                code="CLEANUP_NOT_TERMINAL",
                details={"phase": ledger.get("phase")},
                exit_code=EXIT_FAIL,
            )
        expected = _terminal_expected_heads(ledger)
        blockers: list[dict[str, Any]] = []
        for role in ("builder", "tester"):
            entry = ledger.get("worktrees", {}).get(role, {})
            path = Path(str(entry.get("path") or ""))
            if not path.exists():
                continue
            live_head = full_head(path)
            residue = worktree_residue(path)
            if residue or (expected.get(role) and live_head != expected.get(role)):
                blockers.append(
                    {
                        "role": role,
                        "path": str(path),
                        "expected_head": expected.get(role),
                        "head": live_head,
                        "residue": residue,
                    }
                )
        if blockers:
            return {
                "status": "NEEDS_USER",
                "message": "terminal worktrees drifted and were preserved",
                "code": "CLEANUP_WORKTREE_DRIFT",
                "run_id": run_id,
                "blockers": blockers,
            }, EXIT_FAIL
        failures = cleanup_role_worktrees(repo, ledger, force=False)
        if failures:
            return {
                "status": "NEEDS_USER",
                "message": "safe terminal cleanup is incomplete",
                "code": "CLEANUP_INCOMPLETE",
                "run_id": run_id,
                "failures": failures,
            }, EXIT_FAIL
        append_event(ledger, "terminal_worktrees_cleaned", {})
        save_ledger(repo, ledger)
        return {
            "status": "COMPLETE",
            "message": "ledger-owned terminal worktrees were safely cleaned",
            "run_id": run_id,
        }, EXIT_PASS


def build_parser() -> argparse.ArgumentParser:
    parser = RuntimeArgumentParser(prog="codex-builder-loop")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=RuntimeArgumentParser,
    )

    plan = subparsers.add_parser("plan-validate")
    plan.add_argument("--repo", default=".", help="target Git repository")
    plan.add_argument(
        "--target-branch",
        help="target branch; defaults to the repository's current branch",
    )
    plan.add_argument("--plan", help="Markdown plan path; omit or use - to read stdin")
    plan.set_defaults(handler=cmd_plan_validate)

    plan_preflight = subparsers.add_parser("plan-preflight")
    plan_preflight.add_argument("--repo", default=".", help="target Git repository")
    plan_preflight.add_argument(
        "--run", help="abandoned business run whose protected support paths may need preparation"
    )
    plan_preflight.add_argument("--path", action="append", required=True)
    plan_preflight.set_defaults(handler=cmd_plan_preflight)

    workspace_scan = subparsers.add_parser("workspace-scan")
    workspace_scan.add_argument("--repo", default=".")
    workspace_scan.add_argument("--path", action="append", required=True)
    workspace_scan.set_defaults(handler=cmd_workspace_scan)

    start = subparsers.add_parser("start")
    start.add_argument("--repo", default=".")
    start.add_argument("--run")
    start.add_argument("--task")
    start.add_argument("--session-id", required=True)
    start.add_argument("--plan", help="defaults to .builder-loop/codex/inbox/<run>.md")
    start.add_argument("--spec-head")
    start.add_argument("--target-branch")
    start.set_defaults(handler=cmd_start)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo", default=".")
    verify.add_argument("--run", required=True)
    verify.set_defaults(handler=cmd_verify)

    prove_tests = subparsers.add_parser("prove-tests")
    prove_tests.add_argument("--repo", default=".")
    prove_tests.add_argument("--run", required=True)
    prove_tests.add_argument(
        "--spec",
        required=True,
        help=(
            "JSON matching schema/codex-test-proof.schema.json; "
            "use - to read stdin"
        ),
    )
    prove_tests.set_defaults(handler=cmd_prove_tests)

    evidence = subparsers.add_parser("record-evidence")
    evidence.add_argument("--repo", default=".")
    evidence.add_argument("--run", required=True)
    evidence.add_argument("--kind", choices=sorted(PUBLIC_EVIDENCE_FIELDS), required=True)
    evidence.add_argument("--head", required=True)
    evidence.add_argument("--agent-id", required=True)
    evidence.add_argument("--details")
    evidence.set_defaults(handler=cmd_record_evidence)

    record_problems = subparsers.add_parser("record-problems")
    record_problems.add_argument("--repo", default=".")
    record_problems.add_argument("--run", required=True)
    record_problems.add_argument(
        "--source", choices=["tester", "reviewer", "coordinator"], required=True
    )
    record_problems.add_argument("--source-id", required=True)
    record_problems.add_argument(
        "--manifest",
        required=True,
        help=(
            "JSON matching schema/codex-problem-report.schema.json; "
            "use - to read stdin"
        ),
    )
    record_problems.set_defaults(handler=cmd_record_problems)

    backfill_problems = subparsers.add_parser("backfill-problems")
    backfill_problems.add_argument("--repo", default=".")
    backfill_problems.add_argument("--run", required=True)
    backfill_problems.add_argument(
        "--manifest",
        required=True,
        help=(
            "JSON matching schema/codex-problem-report.schema.json; "
            "use - to read stdin"
        ),
    )
    backfill_problems.set_defaults(handler=cmd_backfill_problems)

    prepare_follow_up = subparsers.add_parser("prepare-follow-up")
    prepare_follow_up.add_argument("--repo", default=".")
    prepare_follow_up.add_argument("--run", required=True)
    prepare_follow_up.add_argument(
        "--role", choices=["tester", "reviewer"], required=True
    )
    prepare_follow_up.add_argument("--agent-id", required=True)
    prepare_follow_up.add_argument(
        "--purpose", choices=["author", "blackbox", "review"], required=True
    )
    prepare_follow_up.set_defaults(handler=cmd_prepare_follow_up)

    agent_event = subparsers.add_parser("agent-event")
    agent_event.add_argument("--repo", default=".")
    agent_event.add_argument("--session-id", required=True)
    agent_event.add_argument("--role", choices=["tester", "reviewer"], required=True)
    agent_event.add_argument("--agent-id", required=True)
    agent_event.add_argument("--turn-id", required=True)
    agent_event.add_argument("--event", choices=["start", "idle", "closed"], required=True)
    agent_event.add_argument("--result")
    agent_event.set_defaults(handler=cmd_agent_event)

    role = subparsers.add_parser("role-check")
    role.add_argument("--repo", default=".")
    role.add_argument("--run", required=True)
    role.add_argument("--role", choices=["builder", "tester"], required=True)
    role.set_defaults(handler=cmd_role_check)

    publish = subparsers.add_parser("publish-prerequisites")
    publish.add_argument("--repo", default=".")
    publish.add_argument("--run", required=True)
    publish.set_defaults(handler=cmd_publish_prerequisites)

    integrate = subparsers.add_parser("integrate-tests")
    integrate.add_argument("--repo", default=".")
    integrate.add_argument("--run", required=True)
    integrate.add_argument("--continue", dest="continue_integration", action="store_true")
    integrate.set_defaults(handler=cmd_integrate_tests)

    status = subparsers.add_parser("status")
    status.add_argument("--repo", default=".")
    selector = status.add_mutually_exclusive_group(required=True)
    selector.add_argument("--run")
    selector.add_argument("--session-id")
    status.set_defaults(handler=cmd_status)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument("--repo", default=".")
    doctor.add_argument("--run")
    doctor.set_defaults(handler=cmd_doctor)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--repo", default=".")
    recover.add_argument("--run", required=True)
    recover.set_defaults(handler=cmd_recover)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--repo", default=".")
    resume.add_argument("--run", required=True)
    resume.add_argument("--reason", required=True)
    resume.set_defaults(handler=cmd_resume)

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--repo", default=".")
    cleanup.add_argument("--run", required=True)
    cleanup.set_defaults(handler=cmd_cleanup)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--repo", default=".")
    finalize.add_argument("--run", required=True)
    finalize.add_argument("--message")
    finalize.set_defaults(handler=cmd_finalize)

    abandon = subparsers.add_parser("abandon")
    abandon.add_argument("--repo", default=".")
    abandon.add_argument("--run", required=True)
    abandon.add_argument("--reason")
    abandon.set_defaults(handler=cmd_abandon)
    return parser


def _drain_before_command(args: argparse.Namespace) -> None:
    command = str(getattr(args, "command", ""))
    if command in {
        "plan-validate",
        "plan-preflight",
        "workspace-scan",
        "start",
        "agent-event",
    }:
        return
    selector = getattr(args, "run", None)
    if selector:
        repo, run_id, ledger = resolve_run_selector(
            getattr(args, "repo", "."), selector
        )
        if ledger.get("phase") in ACTIVE_PHASES:
            try:
                drain_lifecycle_events(repo, run_id)
            except RuntimeProblem as exc:
                if command not in {"status", "doctor"} or not exc.code.startswith(
                    "LIFECYCLE_"
                ):
                    raise
        return
    session_id = str(getattr(args, "session_id", "") or "").strip()
    if command == "status" and session_id:
        try:
            route = lifecycle_delivery.load_route(session_id)
        except lifecycle_delivery.LifecycleDeliveryError as exc:
            raise RuntimeProblem(str(exc), code=exc.code) from exc
        if route is not None:
            repo = resolve_repo(str(route["repo_root"]))
            run_id = ensure_run_id(str(route["run_id"]))
            args.repo = str(repo)
            try:
                drain_lifecycle_events(repo, run_id)
            except RuntimeProblem as exc:
                if exc.code != "LEDGER_INVALID":
                    raise
                # Preserve the mature session-scan diagnostic contract. The
                # status handler will scan the routed repository and report
                # the invalid ledger as LEDGER_SCAN_INVALID with its path.
                return


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        _drain_before_command(args)
        payload, exit_code = args.handler(args)
        return emit(payload, exit_code)
    except RuntimeProblem as exc:
        payload = {
            "status": exc.result,
            "code": exc.code,
            "message": str(exc),
        }
        if exc.details:
            payload.update(exc.details)
        return emit(payload, exc.exit_code)
    except KeyboardInterrupt:
        return emit(
            {"status": "FATAL", "code": "INTERRUPTED", "message": "interrupted"},
            EXIT_FATAL,
        )
    except Exception as exc:
        return emit(
            {
                "status": "FATAL",
                "code": "UNEXPECTED_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
            },
            EXIT_FATAL,
        )
