from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterator, Sequence


class StoreError(RuntimeError):
    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise StoreError(
            result.stderr.strip() or result.stdout.strip() or "Git command failed",
            code="GIT_COMMAND_FAILED",
            details={"args": list(args), "returncode": result.returncode},
        )
    return result


def resolve_repo(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    result = git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        raise StoreError(
            "target is not a Git repository",
            code="NOT_GIT_REPOSITORY",
            details={"repo": str(path)},
        )
    return Path(result.stdout.strip()).resolve()


def state_root(repo: Path) -> Path:
    common = git(repo, "rev-parse", "--git-common-dir").stdout.strip()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = (repo / common_path).resolve()
    return common_path / "builder-loop-assurance-v4"


def run_dir(repo: Path, run_id: str) -> Path:
    return state_root(repo) / "runs" / run_id


def ledger_path(repo: Path, run_id: str) -> Path:
    return run_dir(repo, run_id) / "ledger.json"


def read_ledger(repo: Path, run_id: str) -> dict[str, Any]:
    path = ledger_path(repo, run_id)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise StoreError("assurance run not found", code="ASSURANCE_RUN_NOT_FOUND") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise StoreError(str(exc), code="ASSURANCE_LEDGER_INVALID") from exc
    try:
        from .models import ContractError, validate_ledger

        return validate_ledger(value)
    except ContractError as exc:
        raise StoreError(str(exc), code=exc.code, details=exc.details) from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(raw):
            os.unlink(raw)


def save_ledger(repo: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now()
    from .models import validate_ledger

    validate_ledger(ledger)
    atomic_write_json(ledger_path(repo, ledger["run_id"]), ledger)


@contextlib.contextmanager
def locked(repo: Path) -> Iterator[None]:
    root = state_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "repo.lock"
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def branch_head(repo: Path, branch: str) -> str:
    result = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    if result.returncode != 0:
        raise StoreError(
            "target branch does not exist",
            code="TARGET_BRANCH_NOT_FOUND",
            details={"target_branch": branch},
        )
    return result.stdout.strip()


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    result = git(repo, "diff", "--name-only", "--no-renames", base, head)
    return sorted(line for line in result.stdout.splitlines() if line)


def commit_exists(repo: Path, commit: str) -> bool:
    return git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0


def worktrees(repo: Path) -> list[dict[str, str]]:
    result = git(repo, "worktree", "list", "--porcelain")
    values: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines() + [""]:
        if not line:
            if current:
                values.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return values


def target_worktree(repo: Path, branch: str) -> Path | None:
    ref = f"refs/heads/{branch}"
    for item in worktrees(repo):
        if item.get("branch") == ref:
            return Path(item["worktree"]).resolve()
    return None


def dirty_paths(worktree: Path) -> list[str]:
    result = git(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    raw = result.stdout
    values: list[str] = []
    index = 0
    while index < len(raw):
        end = raw.find("\0", index)
        if end < 0:
            break
        entry = raw[index:end]
        index = end + 1
        if len(entry) < 4:
            continue
        status = entry[:2]
        path = entry[3:]
        if "R" in status or "C" in status:
            renamed_end = raw.find("\0", index)
            if renamed_end >= 0:
                path = raw[index:renamed_end]
                index = renamed_end + 1
        values.append(path)
    return sorted(set(values))


def dirty_paths_against(worktree: Path, treeish: str) -> list[str]:
    changed = git(worktree, "diff", "--name-only", "-z", treeish, "--").stdout
    untracked = git(worktree, "ls-files", "--others", "--exclude-standard", "-z").stdout
    return sorted({path for path in (changed + untracked).split("\0") if path})


def append_event(ledger: dict[str, Any], kind: str, details: dict[str, Any]) -> None:
    at = now()
    sequence = len(ledger.setdefault("events", [])) + 1
    details_digest = hashlib.sha256(
        json.dumps(
            details,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    event_id = hashlib.sha256(
        json.dumps(
            {
                "sequence": sequence,
                "at": at,
                "kind": kind,
                "details_digest": details_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    ledger["events"].append(
        {
            "at": at,
            "kind": kind,
            "details": details,
            "sequence": sequence,
            "event_id": event_id,
            "details_digest": details_digest,
        }
    )
