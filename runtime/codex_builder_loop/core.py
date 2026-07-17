from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence


SCHEMA_VERSION = 1
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_FATAL = 2

ACTIVE_PHASES = {
    "active",
    "integration_conflict",
    "finalize_conflict",
    "continuity_failure",
    "iteration_limit",
    "finalized_cleanup",
}
EVIDENCE_FIELDS = (
    "verified_head",
    "e2e_verified_head",
    "reviewed_head",
    "doc_reviewed_head",
)
PUBLIC_EVIDENCE_FIELDS = {
    "e2e_verified": "e2e_verified_head",
    "reviewed": "reviewed_head",
    "doc_reviewed": "doc_reviewed_head",
}
AGENT_RESULTS = {
    "tester": {"tests_ready", "pass", "fail", "target_change_required", "blocked"},
    "reviewer": {"pass", "findings", "blocked"},
}
PROTECTED_RUNTIME_PATHS = {".claude/loop.yml"}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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
    level: str
    spec_head: str | None
    plan_revision: int | None
    parallel_ready: bool
    interfaces: tuple[Any, ...]
    target_test_dirs: tuple[str, ...]
    support_paths: tuple[str, ...]
    public_prerequisites: tuple[str, ...]
    runner: str | None
    builder_write: tuple[str, ...]
    tester_write: tuple[str, ...]
    behavior_ids: tuple[str, ...]
    supersedes_run_id: str | None
    supersedes_plan_sha256: str | None
    has_e2e_cases: bool
    tags: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


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


def current_branch(repo: Path) -> str:
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeProblem(
            "start must run from a named branch",
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
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeProblem(
            "unsupported or invalid ledger schema",
            code="LEDGER_SCHEMA_ERROR",
        )
    return value


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


def invalidate_evidence(ledger: dict[str, Any], previous_head: str, current_head: str) -> None:
    if previous_head == current_head:
        return
    cleared: dict[str, str] = {}
    for field in EVIDENCE_FIELDS:
        old = ledger.get(field)
        if old is not None:
            cleared[field] = str(old)
        ledger[field] = None
    append_event(
        ledger,
        "evidence_invalidated",
        {
            "previous_candidate_head": previous_head,
            "candidate_head": current_head,
            "cleared": cleared,
        },
    )


def invalidate_role_evidence(ledger: dict[str, Any], role: str, turn_id: str) -> None:
    fields = {
        "tester": ("e2e_verified_head",),
        "reviewer": ("reviewed_head", "doc_reviewed_head"),
    }[role]
    cleared = {field: ledger.get(field) for field in fields if ledger.get(field) is not None}
    if not cleared:
        return
    for field in fields:
        ledger[field] = None
    append_event(
        ledger,
        "agent_evidence_invalidated",
        {"role": role, "turn_id": turn_id, "cleared": cleared},
    )


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
    verified_head = ledger.get("verified_head")
    e2e_verified_head = ledger.get("e2e_verified_head")
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
    return bool(
        snapshot.get("tester_integration_completed") is True
        and isinstance(snapshot.get("tester_author_turn_id"), str)
        and snapshot.get("tester_author_turn_id")
        and snapshot.get("verified_head") == candidate_head
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


def read_plan_source(plan_arg: str | None) -> tuple[str, str, Path | None]:
    if plan_arg and plan_arg != "-":
        path = Path(plan_arg).expanduser().resolve()
        try:
            return path.read_text(encoding="utf-8"), str(path), path
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
    return sys.stdin.read(), "stdin", None


def extract_tag(text: str, name: str, *, required: bool) -> str | None:
    open_re = re.compile(rf"<!--\s*{re.escape(name)}\s*-->", re.IGNORECASE)
    close_re = re.compile(rf"<!--\s*/{re.escape(name)}\s*-->", re.IGNORECASE)
    opens = list(open_re.finditer(text))
    closes = list(close_re.finditer(text))
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


def is_template_placeholder(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\s*<[^>]+>\s*", value))


def parse_plan(text: str, source: str) -> PlanContract:
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
        if type(parsed_doc.get("schema_version")) is not int or parsed_doc.get("schema_version") != 1:
            errors.append("schema_version must equal 1")
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
        if errors:
            raise RuntimeProblem(
                "documentation plan contract needs correction",
                result="NEEDS_USER",
                code="PLAN_CONTRACT_INVALID",
                details={"errors": errors},
            )
        return PlanContract(
            source=source,
            sha256=sha256_text(text),
            level="L1",
            spec_head=spec_head.lower(),
            plan_revision=plan_revision,
            parallel_ready=False,
            interfaces=(),
            target_test_dirs=(),
            support_paths=(),
            public_prerequisites=(),
            runner=None,
            builder_write=tuple(sorted(set(builder_write))),
            tester_write=(),
            behavior_ids=(),
            supersedes_run_id=supersedes_run_id,
            supersedes_plan_sha256=supersedes_plan_sha256,
            has_e2e_cases=e2e is not None,
            tags=("documentation-spec", "plan-checklist"),
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
    errors: list[str] = []
    if type(parsed.get("schema_version")) is not int or parsed.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
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
    runner_raw = test_context.get("runner")
    runner = runner_raw.strip() if isinstance(runner_raw, str) else ""
    if not runner:
        errors.append("test_context.runner is required")
    elif is_template_placeholder(runner):
        errors.append("test_context.runner contains an unresolved placeholder")
    else:
        try:
            reject_tautological_command(runner)
        except RuntimeProblem as exc:
            errors.append(str(exc))

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
    if runner:
        try:
            runner_paths = runner_repository_paths(runner)
        except RuntimeProblem as exc:
            errors.append(str(exc))
            runner_paths = []
        for runner_path in runner_paths:
            if not path_allowed(runner_path, support_paths):
                errors.append(
                    "runner repository script is missing from test_context.support_paths: "
                    + runner_path
                )
            if path_allowed(runner_path, builder_write):
                errors.append("verification runner script is builder-owned: " + runner_path)
            if path_allowed(runner_path, tester_write):
                errors.append("verification runner script is tester-owned: " + runner_path)
        for control_path in runner_control_paths(runner):
            if any(patterns_overlap(pattern, control_path) for pattern in builder_write):
                errors.append("verification control file is builder-owned: " + control_path)
            if any(patterns_overlap(pattern, control_path) for pattern in tester_write):
                errors.append("verification control file is tester-owned: " + control_path)
    for directory in target_dirs:
        probe = directory[:-3].rstrip("/") + "/__probe__.test" if directory.endswith("/**") else directory
        if not any(path_allowed(probe, [pattern]) for pattern in tester_write):
            errors.append(f"target_test_dirs entry is not tester-owned: {directory}")

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
    if errors:
        raise RuntimeProblem(
            "plan contract needs correction",
            result="NEEDS_USER",
            code="PLAN_CONTRACT_INVALID",
            details={"errors": errors},
        )

    return PlanContract(
        source=source,
        sha256=sha256_text(text),
        level="L2/L3",
        spec_head=spec_head.lower(),
        plan_revision=int(plan_revision),
        parallel_ready=bool(parallel_ready),
        interfaces=tuple(interfaces),
        target_test_dirs=tuple(target_dirs),
        support_paths=tuple(support_paths),
        public_prerequisites=tuple(public_prerequisites),
        runner=runner,
        builder_write=tuple(builder_write),
        tester_write=tuple(tester_write),
        behavior_ids=tuple(behavior_ids),
        supersedes_run_id=supersedes_run_id,
        supersedes_plan_sha256=supersedes_plan_sha256,
        has_e2e_cases=e2e is not None,
        tags=tuple(["unit-test-spec", "plan-checklist"] + (["e2e-cases"] if e2e is not None else [])),
    )


def load_plan_file(path: Path) -> tuple[str, PlanContract]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeProblem(
            f"cannot read plan: {exc}",
            code="PLAN_READ_ERROR",
        ) from exc
    return text, parse_plan(text, str(path))


def verify_plan_unchanged(ledger: dict[str, Any]) -> PlanContract:
    path = Path(str(ledger["plan"]["path"]))
    _, contract = load_plan_file(path)
    expected = str(ledger["plan"]["sha256"])
    if contract.sha256 != expected:
        raise RuntimeProblem(
            "plan changed after start; start a new run from the new contract",
            result="NEEDS_USER",
            code="PLAN_CHANGED",
            details={"expected_sha256": expected, "actual_sha256": contract.sha256},
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


def ignored_untracked_paths(worktree: Path) -> list[str]:
    result = git(
        worktree,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        check=True,
    )
    return sorted(item for item in result.stdout.split("\0") if item)


def dirty_paths(worktree: Path) -> list[str]:
    result = git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all", check=True)
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


def target_worktree_residue(repo: Path, worktree: Path) -> list[str]:
    return worktree_residue(worktree, runtime_state_roots=(state_root(repo),))


def target_worktree_unstaged_residue(repo: Path, worktree: Path) -> list[str]:
    unstaged = git(
        worktree, "diff-files", "--name-only", "-z", check=True
    ).stdout.split("\0")
    untracked = git(
        worktree, "ls-files", "--others", "--exclude-standard", "-z", check=True
    ).stdout.split("\0")
    residue = {
        path for path in [*unstaged, *untracked, *ignored_untracked_paths(worktree)] if path
    }
    return without_runtime_state_paths(
        worktree, residue, runtime_state_roots=(state_root(repo),)
    )


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
        commands, config_sha, max_iterations = load_loop_config(repo, spec_head)
        return commands, config_sha, ".claude/loop.yml", max_iterations
    if contract.runner:
        reject_tautological_command(contract.runner)
        return (
            [{"stage": "plan-runner", "cmd": contract.runner, "timeout": 1800}],
            sha256_text(contract.runner),
            "plan:test_context.runner",
            5,
        )
    raise RuntimeProblem(
        "no .claude/loop.yml at spec_head and plan has no runner",
        code="VERIFICATION_RUNNER_MISSING",
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
        if executable_name in {"python", "python3"} and len(remaining) >= 3 and remaining[1] == "-c":
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
    if executable in {"python", "python3"} and len(remaining) >= 3 and remaining[1] == "-c":
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


def runner_repository_paths(command: str) -> list[str]:
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
        normalized = normalize_allowed_path(joined, directory_hint=True)
        return normalized[:-3].rstrip("/") if normalized.endswith("/**") else normalized

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
        if executable_name in {"python", "python3", "ruby", "node"}:
            if len(remaining) <= 1 or remaining[1] in {"-c", "-m", "-e"}:
                return
            script = next((item for item in remaining[1:] if not item.startswith("-")), None)
            if script:
                candidates.append(combine(directory, script))
            return
        if "/" in executable:
            candidates.append(combine(directory, executable))

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
        if executable in {"python", "python3"} and len(remaining) >= 3 and remaining[1] == "-m":
            inspect([remaining[2], *remaining[3:]], base_dir)
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
        if path.endswith(".py") and current_path.is_file():
            try:
                current_text = current_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            match = suspicious.search(current_text)
            if match and not any(
                item.get("path") == path and "suspicious test weakening marker" in item.get("reason", "")
                for item in findings
            ):
                findings.append(
                    {
                        "kind": "reward_hacking",
                        "path": path,
                        "reason": f"suspicious test weakening marker: {match.group(0)}",
                    }
                )
            findings.extend(
                python_test_findings(
                    path,
                    base_blob.stdout if base_blob.returncode == 0 else None,
                    current_text,
                )
            )
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


def required_evidence_fields(ledger: dict[str, Any]) -> list[str]:
    if ledger["plan"].get("level") == "L1":
        return ["reviewed_head", "doc_reviewed_head"]
    return ["verified_head", "e2e_verified_head", "reviewed_head", "doc_reviewed_head"]


def status_facts(repo: Path, ledger: dict[str, Any]) -> dict[str, Any]:
    builder = Path(str(ledger["worktrees"]["builder"]["path"]))
    tester = Path(str(ledger["worktrees"]["tester"]["path"]))
    builder_head = full_head(builder) if builder.exists() else None
    tester_head = full_head(tester) if tester.exists() else None
    builder_dirty = worktree_residue(builder) if builder.exists() else []
    tester_dirty = worktree_residue(tester) if tester.exists() else []
    evidence = {field: ledger.get(field) for field in EVIDENCE_FIELDS}
    required = required_evidence_fields(ledger)
    missing = [field for field in required if ledger.get(field) is None]
    stale = [field for field in required if ledger.get(field) not in {None, builder_head}]
    current_evidence = bool(builder_head) and not missing and not stale
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
    expected_target_head = (
        ledger.get("final_head")
        if ledger.get("phase") in {"finalized_cleanup", "finalized"}
        else ledger["target_start_head"]
    )
    target_continuous = bool(expected_target_head) and target_head == expected_target_head
    return {
        "run_id": ledger["run_id"],
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
        "verification_attempts": ledger.get("verification_attempts", 0),
        "max_iterations": ledger.get("loop_config", {}).get("max_iterations"),
        "builder_dirty_paths": builder_dirty,
        "tester_dirty_paths": tester_dirty,
        "prerequisites_ready": prerequisites_ready,
        "prerequisite_publication": prerequisite_publication,
        "tester_fully_integrated": tester_fully_integrated,
        **evidence,
        "required_gates": required,
        "missing_gates": missing,
        "stale_gates": stale,
        "evidence_current": current_evidence,
        "ready_to_finalize": bool(
            ledger["phase"] == "active"
            and builder_head
            and not builder_dirty
            and prerequisites_ready
            and tester_fully_integrated
            and target_continuous
            and current_evidence
        ),
        "worktrees": {
            "builder": ledger["worktrees"]["builder"]["path"],
            "tester": ledger["worktrees"]["tester"]["path"],
        },
        "final_head": ledger.get("final_head"),
    }


def cmd_plan_validate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    text, source, _ = read_plan_source(args.plan)
    contract = parse_plan(text, source)
    return {
        "status": "READY",
        "message": "plan contract is valid",
        "spec_head": contract.spec_head,
        "parallel_ready": contract.parallel_ready,
        "contract": contract.as_dict(),
    }, EXIT_PASS


def generated_run_id(task: str, plan_sha: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task.strip()).strip("-._").lower()
    if not slug:
        slug = "run"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    suffix = plan_sha[:8]
    available = 63 - len(stamp) - len(suffix) - 2
    return ensure_run_id(f"{slug[:available]}-{stamp}-{suffix}")


def validate_supersession(repo: Path, contract: PlanContract) -> None:
    if contract.plan_revision == 1:
        return
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
    validate_supersession(repo, source_contract)

    contract_spec_head = source_contract.spec_head
    if contract_spec_head is None:
        contract_spec_head = full_head(repo, "HEAD")
    if args.spec_head:
        explicit_spec_head = full_head(repo, args.spec_head)
        if explicit_spec_head != contract_spec_head:
            raise RuntimeProblem(
                "explicit --spec-head does not match plan spec_head",
                result="NEEDS_USER",
                code="SPEC_HEAD_MISMATCH",
                details={"plan_spec_head": contract_spec_head, "explicit_spec_head": explicit_spec_head},
            )
    spec_head = full_head(repo, contract_spec_head)
    target_branch = args.target_branch or current_branch(repo)
    target_start_head = branch_head(repo, target_branch)
    if target_start_head != spec_head:
        raise RuntimeProblem(
            "plan spec_head is stale relative to target branch",
            result="NEEDS_USER",
            code="TARGET_SPEC_MISMATCH",
            details={"target_head": target_start_head, "spec_head": spec_head},
        )
    commands, loop_config_sha256, loop_config_source, max_iterations = load_verification_commands(
        repo, spec_head, source_contract
    )
    validate_runner_ownership(source_contract, commands)
    validate_runner_dependencies_at_spec_head(repo, spec_head, commands)
    runner_paths = verification_protected_paths(commands)
    add_info_exclude(repo)

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
        for path, branch in ((builder_path, builder_branch), (tester_path, tester_branch)):
            if path.exists():
                raise RuntimeProblem(
                    f"worktree path already exists: {path}",
                    code="WORKTREE_PATH_EXISTS",
                )
            result = git(repo, "worktree", "add", "-b", branch, str(path), spec_head, check=False)
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
    ledger: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
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
            "level": contract.level,
            "spec_head": contract.spec_head,
            "plan_revision": contract.plan_revision,
            "parallel_ready": contract.parallel_ready,
            "interfaces": list(contract.interfaces),
            "target_test_dirs": list(contract.target_test_dirs),
            "support_paths": list(contract.support_paths),
            "public_prerequisites": list(contract.public_prerequisites),
            "runner": contract.runner,
            "builder_write": list(contract.builder_write),
            "tester_write": list(contract.tester_write),
            "behavior_ids": list(contract.behavior_ids),
            "supersedes_run_id": contract.supersedes_run_id,
            "supersedes_plan_sha256": contract.supersedes_plan_sha256,
            "has_e2e_cases": contract.has_e2e_cases,
        },
        "loop_config": {
            "path": loop_config_source,
            "spec_sha256": loop_config_sha256,
            "stages": [item["stage"] for item in commands],
            "runner_paths": runner_paths,
            "max_iterations": max_iterations,
        },
        "verification_attempts": 0,
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
        "completed_agent_turns": {
            "tester": [],
            "reviewer": [],
        },
        "verified_head": None,
        "e2e_verified_head": None,
        "reviewed_head": None,
        "doc_reviewed_head": None,
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
        },
    )
    save_ledger(repo, ledger)
    return {
        "status": "READY",
        "message": "run started from a frozen plan contract",
        "run_id": run_id,
        "run_path": str(current_run_dir),
        "ledger_path": str(ledger_path(repo, run_id)),
        "owner_session_id": session_id,
        "spec_head": spec_head,
        "plan_sha256": contract.sha256,
        "frozen_plan": str(plan_path),
        "parallel_ready": contract.parallel_ready,
        "prerequisite_publication_required": (
            contract.level != "L1" and not contract.parallel_ready
        ),
        "worktrees": {
            "builder": str(builder_path),
            "tester": str(tester_path),
        },
        "branches": {
            "builder": builder_branch,
            "tester": tester_branch,
        },
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
        if changed != declared:
            raise RuntimeProblem(
                "the prerequisite snapshot must change exactly the declared public files",
                result="NEEDS_USER",
                code="PREREQUISITE_PATH_MISMATCH",
                details={"declared_paths": declared, "changed_paths": changed},
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

        tree = git(builder, "rev-parse", f"{builder_head}^{{tree}}", check=True).stdout.strip()
        publication_head: str
        if tester_head_before == expected_tester_head:
            created = git(
                repo,
                "-c",
                "user.name=Codex Builder Loop",
                "-c",
                "user.email=codex-builder-loop@localhost",
                "commit-tree",
                tree,
                "-p",
                spec_head,
                input_text=f"chore(codex-loop): publish prerequisites {ledger['run_id']}\n",
                check=False,
            )
            if created.returncode != 0:
                raise RuntimeProblem(
                    "cannot create isolated prerequisite commit",
                    code="PREREQUISITE_COMMIT_FAILED",
                    details={
                        "stdout": tail_text(created.stdout),
                        "stderr": tail_text(created.stderr),
                    },
                )
            publication_head = created.stdout.strip()
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
    if ledger["phase"] == "iteration_limit":
        raise RuntimeProblem(
            "verification iteration limit was reached",
            result="NEEDS_USER",
            code="ITERATION_LIMIT_REACHED",
            details={
                "verification_attempts": ledger.get("verification_attempts", 0),
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
    invalidate_evidence(ledger, before, candidate)
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

    previous_attempts = int(ledger.get("verification_attempts", 0))
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
    ledger["verification_attempts"] = attempt
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
                ledger["verified_head"] = None
                limit_reached = attempt >= max_iterations
                if limit_reached:
                    ledger["phase"] = "iteration_limit"
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
                    },
                    EXIT_FAIL,
                )
                break
            if completed.returncode != 0:
                ledger["verified_head"] = None
                limit_reached = attempt >= max_iterations
                if limit_reached:
                    ledger["phase"] = "iteration_limit"
                append_event(
                    ledger,
                    "machine_verification_failed",
                    {
                        "candidate_head": candidate,
                        "attempt": attempt,
                        "max_iterations": max_iterations,
                        "iteration_limit_reached": limit_reached,
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
                    },
                    EXIT_FAIL,
                )
                break
    finally:
        removed = git(repo, "worktree", "remove", "--force", str(verify_path), check=False)
        if removed.returncode != 0:
            ledger["verified_head"] = None
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
    ledger["verified_head"] = candidate
    append_event(
        ledger,
        "machine_verification_passed",
        {"verified_head": candidate, "stages": stage_results},
    )
    save_ledger(repo, ledger)
    return {
        "status": "PASS",
        "message": "all deterministic verification stages passed",
        "head": candidate,
        "verified_head": candidate,
        "stages": stage_results,
        "attempt": attempt,
        "max_iterations": max_iterations,
    }, EXIT_PASS


def cmd_verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        return verify_machine(repo, ledger)


def parse_details(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


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
        agent_fact = value
        field = PUBLIC_EVIDENCE_FIELDS[args.kind]
        details = parse_details(args.details)
        if args.kind == "e2e_verified":
            required_details = {
                "candidate_worktree",
                "head_before",
                "head_after",
                "command",
                "returncode",
            }
            if not isinstance(details, dict) or not required_details.issubset(details):
                raise RuntimeProblem(
                    "blackbox evidence requires replayable candidate-worktree details",
                    code="E2E_DETAILS_REQUIRED",
                    details={"required": sorted(required_details)},
                )
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
                or not isinstance(details.get("command"), str)
                or not str(details.get("command")).strip()
                or type(details.get("returncode")) is not int
                or details.get("returncode") != 0
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
        ledger[field] = supplied
        append_event(
            ledger,
            "evidence_recorded",
            {
                "kind": args.kind,
                "field": field,
                "head": supplied,
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
            "field": field,
            "head": supplied,
            "candidate_head": candidate,
        }, EXIT_PASS


def cmd_agent_event(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
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
        if ledger.get("phase") == "continuity_failure":
            raise RuntimeProblem(
                "run already lost agent or target continuity",
                result="CONTINUITY_FAILURE",
                code="CONTINUITY_FAILURE",
                details={"role": args.role, "current": current},
                exit_code=EXIT_FAIL,
            )
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
                tester = Path(str(ledger["worktrees"]["tester"]["path"]))
                expected_base = str(ledger["tester_integration"]["base_head"])
                live_head = full_head(tester)
                if live_head != expected_base or worktree_residue(tester):
                    raise RuntimeProblem(
                        "Tester author worktree does not match its frozen baseline",
                        result="NEEDS_USER",
                        code="TESTER_AUTHOR_BASELINE_MISMATCH",
                        details={
                            "expected_head": expected_base,
                            "tester_head": live_head,
                            "dirty_paths": worktree_residue(tester),
                        },
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
        if args.event == "start" and turn_id in completed_turns:
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
        if args.event == "start" and current is not None and current.get("event") == "start":
            if current.get("turn_id") == turn_id:
                return {
                    "status": "NOOP",
                    "message": "agent turn start was already recorded",
                    "recorded": True,
                    "run_id": run_id,
                    "role": args.role,
                    **current,
                }, EXIT_PASS
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
        if args.event in {"idle", "closed"} and (
            current.get("event") != "start" or current.get("turn_id") != turn_id
        ):
            raise RuntimeProblem(
                "agent terminal event does not match the currently running turn",
                result="NEEDS_USER",
                code="AGENT_TURN_MISMATCH",
                details={"role": args.role, "current": current, "incoming_turn_id": turn_id},
                exit_code=EXIT_FAIL,
            )
        if args.event == "start" or result_value != "pass":
            invalidate_role_evidence(ledger, args.role, turn_id)
        builder = Path(str(ledger["worktrees"]["builder"]["path"]))
        role_worktree = (
            Path(str(ledger["worktrees"]["tester"]["path"]))
            if args.role == "tester"
            else builder
        )
        candidate_head = full_head(builder)
        candidate_dirty = bool(worktree_residue(builder))
        review_prerequisites: dict[str, Any] | None = None
        if args.role == "reviewer":
            snapshot = reviewer_prerequisite_snapshot(
                repo,
                ledger,
                candidate_head=candidate_head,
                candidate_dirty=candidate_dirty,
            )
            if args.event == "start":
                start_snapshot: Any = snapshot
                completion_snapshot: Any = None
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
            "at": utc_now(),
        }
        ledger["agents"][args.role] = fact
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
    invalidate_evidence(ledger, builder_before, candidate)
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
        invalidate_evidence(ledger, before, builder_head)
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
        return {
            "status": "ACTIVE",
            "message": "run is active",
            **facts,
        }, EXIT_PASS
    repo = resolve_repo(args.repo)
    session_id = str(args.session_id or "").strip()
    if not session_id:
        raise RuntimeProblem(
            "status requires --run or --session-id",
            code="STATUS_SELECTOR_REQUIRED",
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


def cleanup_role_worktrees(repo: Path, ledger: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for role in ("builder", "tester"):
        entry = ledger["worktrees"][role]
        path = Path(str(entry["path"]))
        branch = str(entry["branch"])
        if path.exists():
            removed = git(repo, "worktree", "remove", "--force", str(path), check=False)
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
        field
        for field in required_evidence_fields(ledger)
        if ledger.get(field) != candidate
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
    if live_target_head == target_head:
        target_dirty = target_worktree_residue(repo, target)
        if target_dirty:
            return {
                "status": "NEEDS_USER",
                "message": "target worktree is dirty before finalize intent recovery",
                "code": "TARGET_DIRTY",
                "target_worktree": str(target),
                "dirty_paths": target_dirty,
            }, EXIT_FAIL
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

    expected_tree = git(
        repo, "rev-parse", f"{target_head}^{{tree}}", check=True
    ).stdout.strip()
    index_tree = git(target, "write-tree", check=True).stdout.strip()
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
            return {
                "status": "NEEDS_USER",
                "message": "final ref exists but its target worktree could not be synchronized",
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
    if (
        branch_head(repo, target_branch_name) != final_head
        or git(target, "write-tree", check=True).stdout.strip() != candidate_tree
        or target_worktree_residue(repo, target)
    ):
        return {
            "status": "NEEDS_USER",
            "message": "finalize intent recovery postcondition is not clean",
            "code": "FINALIZE_RECOVERY_POSTCONDITION",
            "final_head": final_head,
            "target_worktree": str(target),
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


def cmd_finalize(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        if ledger["phase"] == "finalized":
            return {
                "status": "NOOP",
                "message": "run was already finalized",
                "run_id": ledger["run_id"],
                "final_head": ledger.get("final_head"),
            }, EXIT_PASS
        recovered = recover_finalize_intent(repo, ledger)
        if recovered is not None:
            return recovered
        if ledger["phase"] == "finalized_cleanup":
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
            target_checkout = worktree_for_branch(repo, str(ledger["target_branch"]))
            target_dirty = (
                target_worktree_residue(repo, target_checkout)
                if target_checkout is not None
                else []
            )
            if target_dirty:
                return {
                    "status": "NEEDS_USER",
                    "message": "final commit exists but the target worktree is dirty",
                    "code": "TARGET_DIRTY_AFTER_FINALIZE",
                    "final_head": ledger.get("final_head"),
                    "target_worktree": str(target_checkout),
                    "dirty_paths": target_dirty,
                }, EXIT_FAIL
            cleanup_failures = cleanup_finalized_worktrees(repo, ledger)
            if cleanup_failures:
                append_event(ledger, "finalize_cleanup_retry_failed", {"failures": cleanup_failures})
                save_ledger(repo, ledger)
                return {
                    "status": "NEEDS_USER",
                    "message": "final commit exists but worktree cleanup is still incomplete",
                    "code": "FINALIZE_CLEANUP_INCOMPLETE",
                    "final_head": ledger.get("final_head"),
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
            return {
                "status": "COMPLETE",
                "message": "final commit cleanup completed",
                "final_head": ledger.get("final_head"),
                "cleanup_failures": [],
            }, EXIT_PASS
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
        required = required_evidence_fields(ledger)
        stale = {field: ledger.get(field) for field in required if ledger.get(field) != candidate}
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
        existing_target = worktree_for_branch(repo, target_branch_name)
        existing_target_dirty = (
            target_worktree_residue(repo, existing_target)
            if existing_target is not None
            else []
        )
        if existing_target_dirty:
            raise RuntimeProblem(
                "target worktree is dirty; no finalize mutation was attempted",
                result="NEEDS_USER",
                code="TARGET_DIRTY",
                details={
                    "target_worktree": str(existing_target),
                    "dirty_paths": existing_target_dirty,
                },
            )

        artifact_dir = run_dir(repo, str(ledger["run_id"])) / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        patch_path = artifact_dir / f"candidate-{candidate[:12]}.patch"
        patch = git(builder, "diff", "--binary", "--full-index", str(ledger["spec_head"]), candidate, "--", check=True).stdout
        patch_path.write_text(patch, encoding="utf-8")
        if not patch:
            raise RuntimeProblem(
                "candidate has no diff from spec_head",
                result="NEEDS_USER",
                code="EMPTY_CANDIDATE",
            )
        staging, finalize_branch = staging_worktree(repo, ledger, target_head)
        apply_result = run_process(
            ["git", "-C", str(staging), "apply", "--3way", "--index", str(patch_path)],
            check=False,
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
        commit = git(
            staging,
            "commit",
            "-m",
            message,
            check=False,
        )
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

        target, target_temporary = target_worktree(repo, ledger)
        target_dirty = target_worktree_residue(repo, target)
        if target_dirty:
            raise RuntimeProblem(
                "target worktree became dirty before fast-forward; staged commit was preserved",
                result="NEEDS_USER",
                code="TARGET_DIRTY",
                details={"target_worktree": str(target), "dirty_paths": target_dirty},
            )
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
        target_ref = f"refs/heads/{target_branch_name}"
        fast_forward = git(
            repo,
            "update-ref",
            target_ref,
            final_head,
            target_head,
            check=False,
        )
        if fast_forward.returncode != 0:
            ledger["phase"] = "continuity_failure"
            current_target_head = branch_head(repo, target_branch_name)
            append_event(
                ledger,
                "finalize_fast_forward_cas_failed",
                {
                    "candidate_head": candidate,
                    "staged_final_head": final_head,
                    "expected_target_head": target_head,
                    "target_head": current_target_head,
                    "target_worktree": str(target),
                    "stdout": tail_text(fast_forward.stdout),
                    "stderr": tail_text(fast_forward.stderr),
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "CONTINUITY_FAILURE",
                "message": "target branch changed before the compare-and-swap update",
                "code": "FINALIZE_FAST_FORWARD_CAS_FAILED",
                "staged_final_head": final_head,
                "expected_target_head": target_head,
                "target_head": current_target_head,
                "target_worktree": str(target),
                "preserved": True,
            }, EXIT_FAIL
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
            rollback = git(
                repo,
                "update-ref",
                target_ref,
                target_head,
                final_head,
                check=False,
            )
            ledger["phase"] = (
                "finalize_conflict" if rollback.returncode == 0 else "continuity_failure"
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
                    "CONFLICT" if rollback.returncode == 0 else "CONTINUITY_FAILURE"
                ),
                "message": "target ref update could not be synchronized to its worktree",
                "code": "FINALIZE_WORKTREE_SYNC_FAILED",
                "staged_final_head": final_head,
                "target_head": branch_head(repo, target_branch_name),
                "target_worktree": str(target),
                "ref_rolled_back": rollback.returncode == 0,
                "preserved": True,
            }, EXIT_FAIL
        moved_target_head = branch_head(repo, target_branch_name)
        moved_target_tree = git(target, "write-tree", check=True).stdout.strip()
        if moved_target_head != final_head or moved_target_tree != candidate_tree:
            ledger["phase"] = "finalize_conflict"
            append_event(
                ledger,
                "finalize_fast_forward_postcondition_failed",
                {
                    "candidate_tree": candidate_tree,
                    "final_head": final_head,
                    "target_head": moved_target_head,
                    "target_tree": moved_target_tree,
                },
            )
            save_ledger(repo, ledger)
            raise RuntimeProblem(
                "target fast-forward postcondition failed",
                result="CONFLICT",
                code="FINALIZE_FAST_FORWARD_POSTCONDITION",
                details={"final_head": final_head, "target_head": moved_target_head},
                exit_code=EXIT_FAIL,
            )
        moved_target_dirty = target_worktree_residue(repo, target)
        if moved_target_dirty:
            ledger["phase"] = "finalized_cleanup"
            ledger["final_head"] = final_head
            append_event(
                ledger,
                "finalize_target_worktree_dirty",
                {
                    "candidate_head": candidate,
                    "final_head": final_head,
                    "target_worktree": str(target),
                    "dirty_paths": moved_target_dirty,
                },
            )
            save_ledger(repo, ledger)
            return {
                "status": "NEEDS_USER",
                "message": "target moved to the final commit but its worktree became dirty",
                "code": "TARGET_DIRTY_AFTER_FINALIZE",
                "candidate_head": candidate,
                "final_head": final_head,
                "target_branch": target_branch_name,
                "target_worktree": str(target),
                "dirty_paths": moved_target_dirty,
            }, EXIT_FAIL
        ledger["phase"] = "finalized_cleanup"
        ledger["final_head"] = final_head
        append_event(
            ledger,
            "finalized",
            {
                "candidate_head": candidate,
                "final_head": final_head,
                "target_previous_head": target_head,
                "target_branch": target_branch_name,
                "target_worktree_temporary": target_temporary,
                "commit_count": 1,
            },
        )
        save_ledger(repo, ledger)
        cleanup_failures = cleanup_finalized_worktrees(repo, ledger)
        if cleanup_failures:
            append_event(ledger, "finalize_cleanup_incomplete", {"failures": cleanup_failures})
            save_ledger(repo, ledger)
            return {
                "status": "NEEDS_USER",
                "message": "final commit exists but worktree cleanup is incomplete",
                "code": "FINALIZE_CLEANUP_INCOMPLETE",
                "candidate_head": candidate,
                "final_head": final_head,
                "target_branch": target_branch_name,
                "commit_count": 1,
                "cleanup_failures": cleanup_failures,
            }, EXIT_FAIL
        live_target_head = branch_head(repo, target_branch_name)
        if live_target_head != final_head:
            ledger["phase"] = "continuity_failure"
            append_event(
                ledger,
                "finalized_target_continuity_failure",
                {
                    "expected_final_head": final_head,
                    "target_head": live_target_head,
                    "target_branch": target_branch_name,
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
        return {
            "status": "COMPLETE",
            "message": "candidate finalized as one squash commit",
            "candidate_head": candidate,
            "final_head": final_head,
            "target_branch": target_branch_name,
            "commit_count": 1,
            "cleanup_failures": cleanup_failures,
        }, EXIT_PASS


def cmd_abandon(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    repo, run_id, _ = resolve_run_selector(args.repo, args.run)
    with locked_run(repo, run_id) as ledger:
        if ledger["phase"] == "abandoned":
            return {
                "status": "COMPLETE",
                "message": "run was already abandoned",
                "run_id": ledger["run_id"],
                "phase": "abandoned",
                "worktrees": ledger["worktrees"],
                "worktrees_preserved": True,
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
        return {
            "status": "COMPLETE",
            "message": "run abandoned with all worktrees preserved",
            "run_id": ledger["run_id"],
            "phase": "abandoned",
            "worktrees": ledger["worktrees"],
            "worktrees_preserved": True,
        }, EXIT_PASS


def build_parser() -> argparse.ArgumentParser:
    parser = RuntimeArgumentParser(prog="codex-builder-loop")
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=RuntimeArgumentParser,
    )

    plan = subparsers.add_parser("plan-validate")
    plan.add_argument("--plan", help="Markdown plan path; omit or use - to read stdin")
    plan.set_defaults(handler=cmd_plan_validate)

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

    evidence = subparsers.add_parser("record-evidence")
    evidence.add_argument("--repo", default=".")
    evidence.add_argument("--run", required=True)
    evidence.add_argument("--kind", choices=sorted(PUBLIC_EVIDENCE_FIELDS), required=True)
    evidence.add_argument("--head", required=True)
    evidence.add_argument("--agent-id", required=True)
    evidence.add_argument("--details")
    evidence.set_defaults(handler=cmd_record_evidence)

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
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
