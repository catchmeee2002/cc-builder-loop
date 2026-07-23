from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class WorkspaceError(Exception):
    message: str
    code: str
    details: dict[str, Any]

    def __str__(self) -> str:
        return self.message


def _git(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if check and completed.returncode != 0:
        raise WorkspaceError(
            "Git workspace operation failed",
            "WORKSPACE_GIT_ERROR",
            {
                "command": ["git", "-C", str(repo), *args],
                "returncode": completed.returncode,
                "stdout": completed.stdout[-8000:],
                "stderr": completed.stderr[-8000:],
            },
        )
    return completed


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_exact_path(value: str) -> str:
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or raw in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(mark in raw for mark in "*?[")
        or any(ord(char) < 32 for char in raw)
    ):
        raise WorkspaceError(
            "workspace intake paths must be exact repository-relative paths",
            "WORKSPACE_PATH_INVALID",
            {"path": value},
        )
    return path.as_posix()


def _index_entry(repo: Path, path: str) -> dict[str, str] | None:
    result = _git(repo, "ls-files", "--stage", "-z", "--", path)
    entries = [entry for entry in result.stdout.split("\0") if entry]
    if not entries:
        return None
    if len(entries) != 1 or "\t" not in entries[0]:
        raise WorkspaceError(
            "workspace intake cannot capture an unmerged index entry",
            "WORKSPACE_INDEX_UNMERGED",
            {"path": path, "entries": entries},
        )
    metadata, recorded_path = entries[0].split("\t", 1)
    mode, oid, stage = metadata.split()
    if recorded_path != path or stage != "0":
        raise WorkspaceError(
            "workspace intake cannot capture an unmerged index entry",
            "WORKSPACE_INDEX_UNMERGED",
            {"path": path, "entries": entries},
        )
    if mode not in {"100644", "100755"}:
        raise WorkspaceError(
            "workspace intake only accepts regular files",
            "WORKSPACE_ENTRY_NOT_REGULAR",
            {"path": path, "mode": mode},
        )
    return {"mode": mode, "oid": oid}


def _worktree_entry(repo: Path, path: str) -> dict[str, str] | None:
    absolute = repo / path
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return None
    resolved_root = repo.resolve()
    if stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceError(
            "workspace intake rejects symlinks",
            "WORKSPACE_ENTRY_NOT_REGULAR",
            {"path": path, "kind": "symlink"},
        )
    if not stat.S_ISREG(metadata.st_mode) or resolved_root not in absolute.resolve().parents:
        raise WorkspaceError(
            "workspace intake only accepts regular files or tracked deletions",
            "WORKSPACE_ENTRY_NOT_REGULAR",
            {"path": path, "kind": "directory-or-special"},
        )
    digest = hashlib.sha256(absolute.read_bytes()).hexdigest()
    mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
    return {"mode": mode, "sha256": digest}


def path_manifest(repo: Path, value: str, *, require_dirty: bool) -> dict[str, Any]:
    path = normalize_exact_path(value)
    index = _index_entry(repo, path)
    worktree = _worktree_entry(repo, path)
    if index is None and worktree is None and require_dirty:
        raise WorkspaceError(
            "workspace intake path does not exist in the index or worktree",
            "WORKSPACE_PATH_MISSING",
            {"path": path},
        )
    ignored = _git(repo, "check-ignore", "-q", "--", path, check=False)
    if ignored.returncode == 0 and index is None:
        raise WorkspaceError(
            "workspace intake rejects ignored untracked files",
            "WORKSPACE_IGNORED_PATH",
            {"path": path},
        )
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        path,
    )
    dirty = bool(status.stdout)
    if require_dirty and not dirty:
        raise WorkspaceError(
            "workspace intake path is not dirty at the target checkout",
            "WORKSPACE_PATH_NOT_DIRTY",
            {"path": path},
        )
    state = {"path": path, "index": index, "worktree": worktree}
    return {
        **state,
        "state_sha256": canonical_digest(state),
        "dirty": dirty,
    }


def scan_paths(
    repo: Path, paths: Iterable[str], *, require_dirty: bool = True
) -> list[dict[str, Any]]:
    normalized = [normalize_exact_path(path) for path in paths]
    if len(normalized) != len(set(normalized)):
        raise WorkspaceError(
            "workspace intake paths must be unique",
            "WORKSPACE_PATH_DUPLICATE",
            {"paths": normalized},
        )
    for left in normalized:
        for right in normalized:
            if left != right and right.startswith(left.rstrip("/") + "/"):
                raise WorkspaceError(
                    "workspace intake paths cannot contain parent/child overlaps",
                    "WORKSPACE_PATH_OVERLAP",
                    {"left": left, "right": right},
                )
    return [path_manifest(repo, path, require_dirty=require_dirty) for path in normalized]


def create_snapshot_commit(
    repo: Path,
    spec_head: str,
    entries: Sequence[dict[str, Any]],
    *,
    message: str,
) -> tuple[str, str]:
    if not entries:
        tree = _git(repo, "rev-parse", f"{spec_head}^{{tree}}").stdout.strip()
        return spec_head, tree
    with tempfile.TemporaryDirectory(prefix="codex-intake-index-") as raw:
        index_path = Path(raw) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index_path)}
        _git(repo, "read-tree", spec_head, env=env)
        for entry in entries:
            path = str(entry["path"])
            worktree = entry.get("worktree")
            if worktree is None:
                _git(repo, "update-index", "--force-remove", "--", path, env=env)
                continue
            hashed = _git(repo, "hash-object", "-w", "--", path).stdout.strip()
            _git(
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                str(worktree["mode"]),
                hashed,
                path,
                env=env,
            )
        tree = _git(repo, "write-tree", env=env).stdout.strip()
    # commit-tree reads the message from stdin; use subprocess directly to retain binary-safe helpers above.
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Codex Builder Loop",
            "-c",
            "user.email=codex-builder-loop@localhost",
            "commit-tree",
            tree,
            "-p",
            spec_head,
        ],
        input=message.rstrip() + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise WorkspaceError(
            "cannot create workspace intake snapshot commit",
            "WORKSPACE_SNAPSHOT_COMMIT_FAILED",
            {"stdout": completed.stdout[-8000:], "stderr": completed.stderr[-8000:]},
        )
    return completed.stdout.strip(), tree


def tree_path_state(repo: Path, ref: str, value: str) -> dict[str, Any]:
    path = normalize_exact_path(value)
    result = _git(repo, "ls-tree", "-z", ref, "--", path)
    entries = [entry for entry in result.stdout.split("\0") if entry]
    if not entries:
        state = {"path": path, "index": None, "worktree": None}
        return {**state, "state_sha256": canonical_digest(state)}
    metadata, recorded_path = entries[0].split("\t", 1)
    mode, object_type, oid = metadata.split()
    if recorded_path != path or object_type != "blob" or mode not in {"100644", "100755"}:
        raise WorkspaceError(
            "final workspace intake path is not a regular file",
            "WORKSPACE_ENTRY_NOT_REGULAR",
            {"path": path, "mode": mode, "type": object_type},
        )
    content_result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", oid],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if content_result.returncode != 0:
        raise WorkspaceError(
            "cannot read final workspace intake blob",
            "WORKSPACE_GIT_ERROR",
            {"path": path, "oid": oid, "stderr": content_result.stderr[-8000:].decode(errors="replace")},
        )
    content = content_result.stdout
    worktree = {"mode": mode, "sha256": hashlib.sha256(content).hexdigest()}
    state = {"path": path, "index": {"mode": mode, "oid": oid}, "worktree": worktree}
    return {**state, "state_sha256": canonical_digest(state)}


def path_state_is_known(
    current: dict[str, Any], captured: dict[str, Any], final: dict[str, Any]
) -> bool:
    index = current.get("index")
    worktree = current.get("worktree")
    return (
        (index == captured.get("index") or index == final.get("index"))
        and (
            worktree == captured.get("worktree")
            or worktree == final.get("worktree")
        )
    )
