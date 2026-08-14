"""Lightweight lifecycle management for Codex-native development worktrees."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


VERSION = 1
OWNER = "codex_native_dev"
PHASES = {"creating", "active", "preserved", "finishing"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
POISON_PARTS = {".git", ".claude", ".codex"}
RUNTIME_MARKERS = (
    "/.builder-loop/codex/worktrees/",
    "/.git/builder-loop-assurance-v4/",
    "/.git/builder-loop-native/",
)
STATE_FIELDS = {
    "schema_version",
    "id",
    "task",
    "owner_kind",
    "repo_identity",
    "git_common_dir",
    "managed_root",
    "path",
    "branch",
    "target_branch",
    "base_head",
    "phase",
    "intent",
    "created_at",
    "updated_at",
    "preserve_reason",
}


@dataclass
class DevWorktreeError(Exception):
    message: str
    code: str
    details: dict[str, Any] | None = None
    exit_code: int = 1

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str | None
    branch: str | None
    locked: bool = False
    lock_reason: str | None = None
    prunable: bool = False


@dataclass(frozen=True)
class Context:
    repo: Path
    common_dir: Path
    primary: Path
    repo_id: str
    slug: str
    worktrees: tuple[Worktree, ...]


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DevWorktreeError(message, "DEV_WORKTREE_ARGUMENT_INVALID", exit_code=2)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise DevWorktreeError(
            "Git development-worktree operation failed",
            "DEV_WORKTREE_GIT_ERROR",
            {
                "command": ["git", "-C", str(repo), *args],
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            },
        )
    return result


def _parse_worktrees(raw: str) -> tuple[Worktree, ...]:
    records: list[Worktree] = []
    fields: dict[str, str | bool] = {}
    for token in raw.split("\0"):
        if not token:
            if fields:
                records.append(_record(fields))
                fields = {}
            continue
        key, sep, value = token.partition(" ")
        if key == "worktree" and fields:
            records.append(_record(fields))
            fields = {}
        fields[key] = value if sep else True
    if fields:
        records.append(_record(fields))
    return tuple(records)


def _record(fields: Mapping[str, str | bool]) -> Worktree:
    raw_path = fields.get("worktree")
    if not isinstance(raw_path, str) or not raw_path:
        raise DevWorktreeError(
            "Git returned an invalid worktree record",
            "DEV_WORKTREE_REGISTRY_INVALID",
            {"record": dict(fields)},
        )
    raw_branch = fields.get("branch")
    branch = (
        raw_branch.removeprefix("refs/heads/")
        if isinstance(raw_branch, str) and raw_branch.startswith("refs/heads/")
        else None
    )
    locked = fields.get("locked")
    return Worktree(
        path=Path(raw_path).resolve(strict=False),
        head=fields.get("HEAD") if isinstance(fields.get("HEAD"), str) else None,
        branch=branch,
        locked="locked" in fields,
        lock_reason=locked if isinstance(locked, str) and locked else None,
        prunable="prunable" in fields,
    )


def _slug(value: str, limit: int = 48) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "task"
    return result[:limit].rstrip("-") or "task"


def _context(repo_value: str | Path) -> Context:
    requested = Path(repo_value).expanduser().resolve(strict=False)
    repo = Path(_git(requested, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    common_raw = Path(_git(repo, "rev-parse", "--git-common-dir").stdout.strip())
    common = (common_raw if common_raw.is_absolute() else repo / common_raw).resolve()
    worktrees = _parse_worktrees(_git(repo, "worktree", "list", "--porcelain", "-z").stdout)
    if not worktrees or not worktrees[0].path.is_dir():
        raise DevWorktreeError(
            "Repository has no available primary worktree",
            "DEV_WORKTREE_PRIMARY_MISSING",
            {"repo": str(repo)},
        )
    primary = worktrees[0].path
    repo_id = hashlib.sha256(str(common).encode()).hexdigest()
    return Context(repo, common, primary, repo_id, _slug(primary.name, 40), worktrees)


def _refresh(context: Context) -> Context:
    return _context(context.repo)


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _state_dir(context: Context) -> Path:
    return context.common_dir / "codex-builder-loop" / "dev-worktrees"


def _state_path(context: Context, worktree_id: str) -> Path:
    if not ID_RE.fullmatch(worktree_id):
        raise DevWorktreeError(
            "Development worktree id is invalid",
            "DEV_WORKTREE_ID_INVALID",
            {"id": worktree_id},
        )
    return _state_dir(context) / f"{worktree_id}.json"


@contextmanager
def _lock(context: Context) -> Iterator[None]:
    root = _state_dir(context)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_state(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != STATE_FIELDS:
        raise DevWorktreeError(
            "Development worktree state fields are invalid",
            "DEV_WORKTREE_STATE_INVALID",
            {"state": str(path)},
        )
    worktree_id = value.get("id")
    string_fields = STATE_FIELDS - {"schema_version", "intent", "preserve_reason"}
    if (
        value.get("schema_version") != VERSION
        or value.get("owner_kind") != OWNER
        or value.get("phase") not in PHASES
        or not isinstance(worktree_id, str)
        or not ID_RE.fullmatch(worktree_id)
        or any(not isinstance(value.get(key), str) or not value[key] for key in string_fields)
        or not SHA256_RE.fullmatch(str(value.get("repo_identity")))
        or not SHA1_RE.fullmatch(str(value.get("base_head")))
    ):
        raise DevWorktreeError(
            "Development worktree state values are invalid",
            "DEV_WORKTREE_STATE_INVALID",
            {"state": str(path)},
        )
    if value.get("preserve_reason") is not None and not isinstance(
        value.get("preserve_reason"), str
    ):
        raise DevWorktreeError(
            "Development worktree preserve reason is invalid",
            "DEV_WORKTREE_STATE_INVALID",
            {"state": str(path)},
        )
    root = Path(value["managed_root"])
    expected_path = root / worktree_id
    if (
        not all(Path(value[key]).is_absolute() for key in ("git_common_dir", "managed_root", "path"))
        or Path(value["path"]).resolve(strict=False) != expected_path.resolve(strict=False)
        or value["branch"] != f"codex-native/{worktree_id}"
        or path.stem != worktree_id
    ):
        raise DevWorktreeError(
            "Development worktree state identity is invalid",
            "DEV_WORKTREE_STATE_INVALID",
            {"state": str(path)},
        )
    intent = value.get("intent")
    expected_kind = {"creating": "create", "finishing": "finish"}.get(value["phase"])
    if expected_kind is None:
        if intent is not None:
            raise DevWorktreeError(
                "Active development worktree cannot retain an intent",
                "DEV_WORKTREE_STATE_INVALID",
                {"state": str(path)},
            )
    elif (
        not isinstance(intent, dict)
        or set(intent) != {"kind", "head", "target_head", "created_at"}
        or intent.get("kind") != expected_kind
        or not SHA1_RE.fullmatch(str(intent.get("head")))
        or (expected_kind == "create" and intent.get("target_head") is not None)
        or (
            expected_kind == "finish"
            and not SHA1_RE.fullmatch(str(intent.get("target_head")))
        )
        or not isinstance(intent.get("created_at"), str)
        or (expected_kind == "create" and intent.get("head") != value["base_head"])
    ):
        raise DevWorktreeError(
            "Development worktree intent is invalid",
            "DEV_WORKTREE_STATE_INVALID",
            {"state": str(path)},
        )
    return dict(value)


def _read_state(path: Path) -> dict[str, Any]:
    try:
        return _validate_state(json.loads(path.read_text()), path)
    except (OSError, json.JSONDecodeError) as exc:
        raise DevWorktreeError(
            "Development worktree state cannot be read",
            "DEV_WORKTREE_STATE_INVALID",
            {"state": str(path), "error": str(exc)},
        ) from exc


def _write_state(context: Context, state: Mapping[str, Any]) -> None:
    path = _state_path(context, str(state["id"]))
    value = _validate_state(dict(state), path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=f".{state['id']}.", dir=path.parent)
    temp = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _remove_state(context: Context, worktree_id: str) -> None:
    path = _state_path(context, worktree_id)
    path.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _state(context: Context, worktree_id: str) -> dict[str, Any]:
    path = _state_path(context, worktree_id)
    if not path.is_file():
        raise DevWorktreeError(
            "Development worktree is not managed",
            "DEV_WORKTREE_NOT_MANAGED",
            {"id": worktree_id},
        )
    return _read_state(path)


def _states(context: Context) -> list[Path]:
    root = _state_dir(context)
    return sorted(root.glob("*.json")) if root.is_dir() else []


def _symlink_component(path: Path) -> Path | None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return current
        if not current.exists():
            break
    return None


def _managed_root(context: Context, raw: str | None) -> Path:
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise DevWorktreeError(
                "Managed worktree root must be absolute",
                "DEV_WORKTREE_ROOT_INVALID",
                {"root": raw},
            )
    else:
        candidate = (
            context.primary.parent
            / "codex-worktrees"
            / f"{context.slug}-{context.repo_id[:12]}"
        )
    symlink = _symlink_component(candidate)
    root = candidate.resolve(strict=False)
    if symlink or POISON_PARTS.intersection(root.parts):
        raise DevWorktreeError(
            "Managed worktree root uses an unsafe control path",
            "DEV_WORKTREE_ROOT_INVALID",
            {"root": str(candidate), "symlink": str(symlink) if symlink else None},
        )
    for registered in context.worktrees:
        if _within(root, registered.path):
            raise DevWorktreeError(
                "Managed worktree root cannot be inside a registered worktree",
                "DEV_WORKTREE_ROOT_INVALID",
                {"root": str(root), "worktree": str(registered.path)},
            )
    return root


def _assert_owner(context: Context, state: Mapping[str, Any]) -> None:
    expected_root = _managed_root(context, str(state["managed_root"]))
    if (
        state["repo_identity"] != context.repo_id
        or Path(state["git_common_dir"]).resolve() != context.common_dir
        or Path(state["path"]).resolve(strict=False)
        != (expected_root / state["id"]).resolve(strict=False)
        or state["branch"] != f"codex-native/{state['id']}"
    ):
        raise DevWorktreeError(
            "Development worktree identity drifted",
            "DEV_WORKTREE_IDENTITY_DRIFT",
            {"id": state["id"]},
        )


def _branch_head(context: Context, branch: str) -> str | None:
    result = _git(
        context.primary,
        "rev-parse",
        "--verify",
        f"refs/heads/{branch}^{{commit}}",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _registered_path(context: Context, path: Path) -> Worktree | None:
    resolved = path.resolve(strict=False)
    return next((item for item in context.worktrees if item.path == resolved), None)


def _registered_branch(context: Context, branch: str) -> Worktree | None:
    return next((item for item in context.worktrees if item.branch == branch), None)


def _ancestor(context: Context, ancestor: str, descendant: str) -> bool:
    return (
        _git(
            context.primary,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        ).returncode
        == 0
    )


def _target(context: Context, requested: str | None) -> tuple[str, str]:
    if requested:
        branch = requested.strip()
    else:
        result = _git(
            context.primary, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        )
        if result.returncode or not result.stdout.strip():
            raise DevWorktreeError(
                "Primary worktree is detached; --target-branch is required",
                "DEV_WORKTREE_TARGET_DETACHED",
            )
        branch = result.stdout.strip()
    if _git(context.primary, "check-ref-format", "--branch", branch, check=False).returncode:
        raise DevWorktreeError(
            "Target branch name is invalid",
            "DEV_WORKTREE_TARGET_INVALID",
            {"target_branch": branch},
        )
    head = _branch_head(context, branch)
    if head is None:
        raise DevWorktreeError(
            "Target branch does not exist",
            "DEV_WORKTREE_TARGET_INVALID",
            {"target_branch": branch},
        )
    return branch, head


def _ordinary(path: Path) -> list[str]:
    result = _git(
        path, "status", "--porcelain=v1", "-z", "--untracked-files=all", check=False
    )
    return (
        ["<unreadable-status>"]
        if result.returncode
        else [item for item in result.stdout.split("\0") if item]
    )


def _ignored(path: Path) -> list[str]:
    result = _git(
        path,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        check=False,
    )
    return (
        ["<unreadable-ignored>"]
        if result.returncode
        else sorted(item for item in result.stdout.split("\0") if item)
    )


def _residue(path: Path) -> dict[str, Any]:
    ordinary, ignored = _ordinary(path), _ignored(path)
    sample = ordinary + [f"ignored:{item}" for item in ignored]
    return {
        "ordinary_count": len(ordinary),
        "ignored_count": len(ignored),
        "sample": sample[:50],
        "truncated": len(sample) > 50,
        "sha256": _digest({"ordinary": ordinary, "ignored": ignored}),
        "clean": not ordinary and not ignored,
    }


def _usage(path: Path) -> dict[str, Any]:
    users: list[dict[str, Any]] = []
    resolved = path.resolve(strict=False)
    try:
        cwd = Path.cwd().resolve(strict=False)
    except OSError:
        cwd = None
    if cwd and _within(cwd, resolved):
        users.append({"kind": "current_cwd", "pid": os.getpid(), "cwd": str(cwd)})
    proc = Path("/proc")
    if proc.is_dir():
        for item in proc.iterdir():
            if not item.name.isdigit() or int(item.name) == os.getpid():
                continue
            try:
                process_cwd = (item / "cwd").resolve(strict=True)
            except (OSError, PermissionError):
                continue
            if _within(process_cwd, resolved):
                users.append(
                    {"kind": "process_cwd", "pid": int(item.name), "cwd": str(process_cwd)}
                )
                if len(users) == 20:
                    break
    return {"in_use": bool(users), "users": users}


def _fault(point: str) -> None:
    if (
        os.environ.get("CODEX_BUILDER_LOOP_TESTING") == "1"
        and os.environ.get("CODEX_BUILDER_LOOP_DEV_WORKTREE_FAULT") == point
    ):
        raise SystemExit(97)


def _target_state(context: Context, branch: str) -> dict[str, Any]:
    checkout = _registered_branch(context, branch)
    entries = _ordinary(checkout.path) if checkout and checkout.path.is_dir() else None
    return {
        "checkout": str(checkout.path) if checkout else None,
        "ordinary_count": len(entries) if entries is not None else None,
        "sample": entries[:20] if entries is not None else [],
        "truncated": bool(entries and len(entries) > 20),
        "excluded_from_worktree": True,
    }


def _context_policy(context: Context, path: Path) -> dict[str, Any]:
    return {
        "candidate": {
            "root": str(path),
            "writes": "delivery code, tests and tracked documentation",
        },
        "host_readonly": {
            "root": str(context.primary),
            "writes": "not authorized by worktree creation",
        },
        "shared_runtime": {
            "owner": "existing deployment probe/build/deploy/restore transaction",
            "writes": "requires its own explicit authorization and recovery contract",
        },
    }


def _verify_live(context: Context, state: Mapping[str, Any], head: str) -> Worktree:
    _assert_owner(context, state)
    path = Path(state["path"])
    registered = _registered_path(context, path)
    live_head = (
        _git(path, "rev-parse", "HEAD", check=False).stdout.strip()
        if path.is_dir()
        else None
    )
    live_branch = (
        _git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        if path.is_dir()
        else None
    )
    if (
        registered is None
        or not path.is_dir()
        or registered.branch != state["branch"]
        or registered.head != head
        or live_head != head
        or live_branch is None
        or live_branch.returncode
        or live_branch.stdout.strip() != state["branch"]
    ):
        raise DevWorktreeError(
            "Managed development worktree identity drifted",
            "DEV_WORKTREE_IDENTITY_DRIFT",
            {"id": state["id"], "expected_head": head},
        )
    return registered


def create_worktree(
    repo: str | Path,
    *,
    task: str,
    target_branch: str | None = None,
    root: str | None = None,
) -> dict[str, Any]:
    if not task.strip():
        raise DevWorktreeError(
            "Development worktree task is required", "DEV_WORKTREE_TASK_REQUIRED"
        )
    context = _context(repo)
    managed_root = _managed_root(context, root)
    target, base = _target(context, target_branch)
    target_state = _target_state(context, target)
    with _lock(context):
        worktree_id = (
            f"{_slug(task)}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}-"
            f"{secrets.token_hex(3)}"
        )
        branch, path = f"codex-native/{worktree_id}", managed_root / worktree_id
        if _branch_head(context, branch) or path.exists() or _registered_path(context, path):
            raise DevWorktreeError(
                "Development worktree branch or path already exists",
                "DEV_WORKTREE_PATH_COLLISION",
                {"branch": branch, "path": str(path)},
            )
        managed_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        now = _now()
        state = {
            "schema_version": VERSION,
            "id": worktree_id,
            "task": task.strip(),
            "owner_kind": OWNER,
            "repo_identity": context.repo_id,
            "git_common_dir": str(context.common_dir),
            "managed_root": str(managed_root),
            "path": str(path),
            "branch": branch,
            "target_branch": target,
            "base_head": base,
            "phase": "creating",
            "intent": {"kind": "create", "head": base, "target_head": None, "created_at": now},
            "created_at": now,
            "updated_at": now,
            "preserve_reason": None,
        }
        _write_state(context, state)
        _fault("after_create_intent")
        result = _git(
            context.primary,
            "worktree",
            "add",
            "-b",
            branch,
            str(path),
            base,
            check=False,
        )
        if result.returncode:
            raise DevWorktreeError(
                "Development worktree creation is incomplete and recoverable",
                "DEV_WORKTREE_CREATE_INCOMPLETE",
                {"id": worktree_id, "stderr": result.stderr[-4000:]},
            )
        _fault("after_worktree_add")
        context = _refresh(context)
        _verify_live(context, state, base)
        state.update(phase="active", intent=None, updated_at=_now())
        _write_state(context, state)
    return {
        "status": "READY",
        "message": "managed development worktree created",
        "id": worktree_id,
        "task": task.strip(),
        "path": str(path),
        "branch": branch,
        "target_branch": target,
        "base_head": base,
        "managed_root": str(managed_root),
        "target_state": target_state,
        "context_policy": _context_policy(context, path),
    }


def _finish_ready(context: Context, state: Mapping[str, Any]) -> tuple[str, str]:
    path = Path(state["path"])
    _assert_owner(context, state)
    registered = _registered_path(context, path)
    if registered is None or not path.is_dir():
        raise DevWorktreeError(
            "Managed development worktree is missing",
            "DEV_WORKTREE_OWNED_MISSING",
            {"id": state["id"]},
        )
    head = _git(path, "rev-parse", "HEAD").stdout.strip()
    _verify_live(context, state, head)
    target_head = _branch_head(context, state["target_branch"])
    if target_head is None or not _ancestor(context, head, target_head):
        raise DevWorktreeError(
            "Development worktree HEAD is not contained in the target branch",
            "DEV_WORKTREE_NOT_MERGED",
            {"id": state["id"], "head": head, "target_head": target_head},
        )
    residue, usage = _residue(path), _usage(path)
    if not residue["clean"]:
        raise DevWorktreeError(
            "Development worktree has residue and was preserved",
            "DEV_WORKTREE_RESIDUE",
            {"id": state["id"], "residue": residue},
        )
    if registered.locked:
        raise DevWorktreeError(
            "Development worktree is Git-locked and was preserved",
            "DEV_WORKTREE_LOCKED",
            {"id": state["id"], "reason": registered.lock_reason},
        )
    if usage["in_use"]:
        raise DevWorktreeError(
            "Development worktree is still in use and was preserved",
            "DEV_WORKTREE_IN_USE",
            {"id": state["id"], **usage},
        )
    return head, target_head


def _continue_finish(context: Context, state: dict[str, Any]) -> dict[str, Any]:
    _assert_owner(context, state)
    intent = state.get("intent")
    if state["phase"] != "finishing" or not isinstance(intent, dict):
        raise DevWorktreeError(
            "Development worktree has no finish intent",
            "DEV_WORKTREE_STATE_INVALID",
            {"id": state["id"]},
        )
    head = intent["head"]
    target_head = _branch_head(context, state["target_branch"])
    if target_head is None or not _ancestor(context, head, target_head):
        raise DevWorktreeError(
            "Target branch no longer contains the finishing worktree HEAD",
            "DEV_WORKTREE_FINISH_DRIFT",
            {"id": state["id"], "head": head, "target_head": target_head},
        )
    context = _refresh(context)
    path = Path(state["path"])
    registered = _registered_path(context, path)
    if registered:
        try:
            registered = _verify_live(context, state, head)
        except DevWorktreeError as exc:
            raise DevWorktreeError(
                "Finishing worktree identity drifted",
                "DEV_WORKTREE_FINISH_DRIFT",
                exc.details,
            ) from exc
        residue, usage = _residue(path), _usage(path)
        if not residue["clean"] or registered.locked or usage["in_use"]:
            raise DevWorktreeError(
                "Finishing worktree is no longer safe to remove",
                "DEV_WORKTREE_FINISH_DRIFT",
                {"id": state["id"], "residue": residue, "usage": usage},
            )
        result = _git(context.primary, "worktree", "remove", str(path), check=False)
        if result.returncode:
            raise DevWorktreeError(
                "Development worktree removal is incomplete and recoverable",
                "DEV_WORKTREE_FINISH_INCOMPLETE",
                {"id": state["id"], "stderr": result.stderr[-4000:]},
            )
        _fault("after_worktree_remove")
    elif path.exists():
        raise DevWorktreeError(
            "Unregistered directory occupies the finishing path",
            "DEV_WORKTREE_FINISH_DRIFT",
            {"id": state["id"], "path": str(path)},
        )
    context = _refresh(context)
    branch_head = _branch_head(context, state["branch"])
    if branch_head:
        if branch_head != head or _registered_branch(context, state["branch"]):
            raise DevWorktreeError(
                "Finishing branch identity drifted",
                "DEV_WORKTREE_FINISH_DRIFT",
                {"id": state["id"], "branch_head": branch_head},
            )
        result = _git(
            context.primary,
            "update-ref",
            "-d",
            f"refs/heads/{state['branch']}",
            head,
            check=False,
        )
        if result.returncode:
            raise DevWorktreeError(
                "Development worktree branch cleanup is incomplete and recoverable",
                "DEV_WORKTREE_FINISH_INCOMPLETE",
                {"id": state["id"], "stderr": result.stderr[-4000:]},
            )
        _fault("after_branch_delete")
    _remove_state(context, state["id"])
    return {
        "status": "COMPLETE",
        "message": "managed development worktree safely finished",
        "id": state["id"],
        "path": state["path"],
        "branch": state["branch"],
        "worktree_head": head,
        "target_branch": state["target_branch"],
        "target_head": target_head,
    }


def finish_worktree(repo: str | Path, *, worktree_id: str) -> dict[str, Any]:
    context = _context(repo)
    with _lock(context):
        state = _state(context, worktree_id)
        _assert_owner(context, state)
        if state["phase"] == "finishing":
            return _continue_finish(context, state)
        if state["phase"] not in {"active", "preserved"}:
            raise DevWorktreeError(
                "Development worktree cannot finish from its current phase",
                "DEV_WORKTREE_PHASE_INVALID",
                {"id": worktree_id, "phase": state["phase"]},
            )
        head, target_head = _finish_ready(context, state)
        now = _now()
        state.update(
            phase="finishing",
            intent={
                "kind": "finish",
                "head": head,
                "target_head": target_head,
                "created_at": now,
            },
            updated_at=now,
        )
        _write_state(context, state)
        _fault("after_finish_intent")
        return _continue_finish(context, state)


def preserve_worktree(
    repo: str | Path, *, worktree_id: str, reason: str
) -> dict[str, Any]:
    if not reason.strip():
        raise DevWorktreeError(
            "Preserve reason is required", "DEV_WORKTREE_PRESERVE_REASON_REQUIRED"
        )
    context = _context(repo)
    with _lock(context):
        state = _state(context, worktree_id)
        _assert_owner(context, state)
        if state["phase"] not in {"active", "preserved"}:
            raise DevWorktreeError(
                "Only active development worktrees can be preserved",
                "DEV_WORKTREE_PHASE_INVALID",
                {"id": worktree_id, "phase": state["phase"]},
            )
        state.update(
            phase="preserved",
            intent=None,
            preserve_reason=reason.strip(),
            updated_at=_now(),
        )
        _write_state(context, state)
    return {
        "status": "COMPLETE",
        "message": "managed development worktree marked for preservation",
        "id": worktree_id,
        "path": state["path"],
        "branch": state["branch"],
        "reason": reason.strip(),
    }


def _recover_create(context: Context, state: dict[str, Any]) -> dict[str, Any]:
    _assert_owner(context, state)
    path, branch, base = Path(state["path"]), state["branch"], state["base_head"]
    context = _refresh(context)
    registered, branch_head = _registered_path(context, path), _branch_head(context, branch)
    if registered:
        _verify_live(context, state, base)
    elif path.exists():
        raise DevWorktreeError(
            "Create recovery found an unregistered directory",
            "DEV_WORKTREE_CREATE_DRIFT",
            {"id": state["id"]},
        )
    elif branch_head is None:
        result = _git(
            context.primary, "worktree", "add", "-b", branch, str(path), base, check=False
        )
        if result.returncode:
            raise DevWorktreeError(
                "Create intent replay failed",
                "DEV_WORKTREE_CREATE_INCOMPLETE",
                {"id": state["id"], "stderr": result.stderr[-4000:]},
            )
    elif branch_head == base and _registered_branch(context, branch) is None:
        result = _git(context.primary, "worktree", "add", str(path), branch, check=False)
        if result.returncode:
            raise DevWorktreeError(
                "Create intent replay could not attach its exact branch",
                "DEV_WORKTREE_CREATE_INCOMPLETE",
                {"id": state["id"], "stderr": result.stderr[-4000:]},
            )
    else:
        raise DevWorktreeError(
            "Create recovery branch identity drifted",
            "DEV_WORKTREE_CREATE_DRIFT",
            {"id": state["id"], "branch_head": branch_head},
        )
    context = _refresh(context)
    _verify_live(context, state, base)
    state.update(phase="active", intent=None, updated_at=_now())
    _write_state(context, state)
    return {
        "status": "READY",
        "message": "development worktree create intent recovered",
        "id": state["id"],
        "path": state["path"],
        "branch": state["branch"],
    }


def recover_worktrees(
    repo: str | Path, *, worktree_id: str | None = None
) -> dict[str, Any]:
    context = _context(repo)
    recovered: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    with _lock(context):
        paths = [_state_path(context, worktree_id)] if worktree_id else _states(context)
        if worktree_id and not paths[0].is_file():
            raise DevWorktreeError(
                "Development worktree is not managed",
                "DEV_WORKTREE_NOT_MANAGED",
                {"id": worktree_id},
            )
        for path in paths:
            try:
                state = _read_state(path)
                if state["phase"] == "creating":
                    recovered.append(_recover_create(context, state))
                elif state["phase"] == "finishing":
                    recovered.append(_continue_finish(context, state))
                else:
                    recovered.append(
                        {
                            "status": "NOOP",
                            "id": state["id"],
                            "phase": state["phase"],
                            "message": "development worktree has no recovery intent",
                        }
                    )
            except DevWorktreeError as exc:
                failures.append(
                    {
                        "state": str(path),
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details or {},
                    }
                )
    return {
        "status": "NEEDS_USER" if failures else ("COMPLETE" if recovered else "NOOP"),
        "message": (
            "development worktree recovery retained unresolved findings"
            if failures
            else "development worktree recovery completed"
        ),
        "recovered": recovered,
        "failures": failures,
    }


def _external_kind(path: Path) -> str:
    value = path.as_posix()
    if any(marker in value for marker in RUNTIME_MARKERS):
        return "runtime_owned"
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve(
        strict=False
    )
    return "codex_managed" if _within(path, codex_home / "worktrees") else "external_registered"


def _state_fact(context: Context, state: Mapping[str, Any]) -> dict[str, Any]:
    path = Path(state["path"])
    registered, branch_head = _registered_path(context, path), _branch_head(
        context, state["branch"]
    )
    finding = None
    if state["repo_identity"] != context.repo_id:
        finding = "owned_identity_drift"
    elif state["phase"] in {"creating", "finishing"}:
        finding = f"recoverable_{state['phase']}"
    elif registered is None or not path.is_dir():
        finding = "owned_missing"
    elif registered.branch != state["branch"]:
        finding = "owned_branch_drift"
    elif branch_head is None:
        finding = "owned_branch_missing"
    fact: dict[str, Any] = {
        "id": state["id"],
        "task": state["task"],
        "phase": state["phase"],
        "path": state["path"],
        "branch": state["branch"],
        "target_branch": state["target_branch"],
        "registered": registered is not None,
        "path_exists": path.is_dir(),
        "head": registered.head if registered else branch_head,
        "locked": registered.locked if registered else False,
        "lock_reason": registered.lock_reason if registered else None,
        "finding": finding,
        "preserve_reason": state["preserve_reason"],
    }
    if registered and path.is_dir():
        fact.update(residue=_residue(path), usage=_usage(path))
    return fact


def inventory(repo: str | Path, *, worktree_id: str | None = None) -> dict[str, Any]:
    context = _context(repo)
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    paths: set[Path] = set()
    branches: set[str] = set()
    roots = {_managed_root(context, None)}
    for path in _states(context):
        if worktree_id and path.stem != worktree_id:
            continue
        try:
            state = _read_state(path)
            valid.append(state)
            paths.add(Path(state["path"]).resolve(strict=False))
            branches.add(state["branch"])
            roots.add(Path(state["managed_root"]).resolve(strict=False))
        except DevWorktreeError as exc:
            invalid.append(
                {"state": str(path), "code": exc.code, "message": str(exc)}
            )
    managed = [_state_fact(context, state) for state in valid]
    unowned: list[dict[str, Any]] = []
    for item in context.worktrees:
        if item.path == context.primary or item.path in paths:
            continue
        unknown = any(_within(item.path, root) for root in roots) or (
            item.branch or ""
        ).startswith("codex-native/")
        kind = "unknown_managed_root" if unknown else _external_kind(item.path)
        fact: dict[str, Any] = {
            "kind": kind,
            "path": str(item.path),
            "branch": item.branch,
            "head": item.head,
            "path_exists": item.path.is_dir(),
            "locked": item.locked,
            "lock_reason": item.lock_reason,
            "prunable": item.prunable,
        }
        if unknown and item.path.is_dir():
            fact.update(residue=_residue(item.path), usage=_usage(item.path))
        unowned.append(fact)
    registered_paths = {item.path for item in context.worktrees}
    unregistered: list[dict[str, Any]] = []
    for root in sorted(roots):
        if root.is_dir():
            for child in sorted(root.iterdir()):
                resolved = child.resolve(strict=False)
                if child.is_dir() and resolved not in registered_paths and resolved not in paths:
                    unregistered.append(
                        {"kind": "unregistered_directory", "path": str(resolved)}
                    )
    registered_branches = {item.branch for item in context.worktrees if item.branch}
    branch_only: list[dict[str, Any]] = []
    raw_refs = _git(
        context.primary,
        "for-each-ref",
        "--format=%(refname:short)%00%(objectname)",
        "refs/heads/codex-native/",
    ).stdout
    for line in raw_refs.splitlines():
        branch, sep, head = line.partition("\0")
        if sep and branch not in registered_branches and branch not in branches:
            branch_only.append({"kind": "branch_only", "branch": branch, "head": head})
    findings = [item for item in managed if item["finding"]]
    findings += invalid
    findings += [item for item in unowned if item["kind"] == "unknown_managed_root"]
    findings += unregistered + branch_only
    return {
        "status": "READY",
        "message": "development worktree inventory completed without mutation",
        "repo": str(context.primary),
        "repo_identity": context.repo_id,
        "default_managed_root": str(_managed_root(context, None)),
        "read_only": True,
        "counts": {
            "managed": len(managed),
            "invalid_states": len(invalid),
            "unowned_registered": len(unowned),
            "unregistered_directories": len(unregistered),
            "branch_only": len(branch_only),
        },
        "managed": managed,
        "invalid_states": invalid,
        "unowned_registered": unowned,
        "unregistered_directories": unregistered,
        "branch_only": branch_only,
        "findings": findings,
    }


def doctor_snapshot(repo: str | Path) -> dict[str, Any]:
    result = inventory(repo)
    blocking_findings = {
        "recoverable_creating",
        "recoverable_finishing",
        "owned_identity_drift",
        "owned_missing",
        "owned_branch_drift",
        "owned_branch_missing",
    }
    blocking = [
        item for item in result["managed"] if item["finding"] in blocking_findings
    ] + result["invalid_states"]
    return {
        "read_only": True,
        "default_managed_root": result["default_managed_root"],
        "counts": result["counts"],
        "blocking": blocking,
        "findings": result["findings"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = Parser(prog="codex-builder-loop dev-worktree")
    subs = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    create = subs.add_parser("create")
    create.add_argument("--repo", default=".")
    create.add_argument("--task", required=True)
    create.add_argument("--target-branch")
    create.add_argument("--root")
    create.set_defaults(
        handler=lambda args: create_worktree(
            args.repo, task=args.task, target_branch=args.target_branch, root=args.root
        )
    )
    status = subs.add_parser("status")
    status.add_argument("--repo", default=".")
    status.add_argument("--id")
    status.set_defaults(handler=lambda args: inventory(args.repo, worktree_id=args.id))
    finish = subs.add_parser("finish")
    finish.add_argument("--repo", default=".")
    finish.add_argument("--id", required=True)
    finish.set_defaults(
        handler=lambda args: finish_worktree(args.repo, worktree_id=args.id)
    )
    preserve = subs.add_parser("preserve")
    preserve.add_argument("--repo", default=".")
    preserve.add_argument("--id", required=True)
    preserve.add_argument("--reason", required=True)
    preserve.set_defaults(
        handler=lambda args: preserve_worktree(
            args.repo, worktree_id=args.id, reason=args.reason
        )
    )
    recover = subs.add_parser("recover")
    recover.add_argument("--repo", default=".")
    recover.add_argument("--id")
    recover.set_defaults(
        handler=lambda args: recover_worktrees(args.repo, worktree_id=args.id)
    )
    return parser


def _emit(payload: Mapping[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return code


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        payload = args.handler(args)
        return _emit(payload, 1 if payload.get("status") == "NEEDS_USER" else 0)
    except DevWorktreeError as exc:
        return _emit(
            {
                "status": "NEEDS_USER",
                "code": exc.code,
                "message": str(exc),
                "details": exc.details or {},
            },
            exc.exit_code,
        )


if __name__ == "__main__":
    raise SystemExit(main())
