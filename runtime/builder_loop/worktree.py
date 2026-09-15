"""candidate worktree 与临时 worktree。

candidate 放仓库同级目录 `../builder-loop-worktrees/<repo-name>/<run_id>`（可由 loop.yml
`worktree.root` 覆盖），避免被 `ruff check .` 之类的全仓命令扫到。
临时 worktree 放 run 目录下 `tmp/`，用完即删。
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import gitx
from .errors import fatal

BRANCH_PREFIX = "builder-loop/"


def candidate_branch(run_id: str) -> str:
    return BRANCH_PREFIX + run_id


def candidate_root(repo_root: Path, override: str | None) -> Path:
    if override:
        p = Path(override)
        return (p if p.is_absolute() else repo_root / p).resolve()
    return (repo_root.parent / "builder-loop-worktrees" / repo_root.name).resolve()


def create_candidate(repo_root: Path, run_id: str, target_head: str, root_override: str | None) -> tuple[Path, str]:
    branch = candidate_branch(run_id)
    path = candidate_root(repo_root, root_override) / run_id
    if path.exists():
        raise fatal("WORKTREE_PATH_EXISTS", f"候选 worktree 路径已存在: {path}", path=str(path))
    if gitx.branch_exists(repo_root, branch):
        raise fatal("BRANCH_EXISTS", f"分支已存在: {branch}", branch=branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitx.git(repo_root, "worktree", "add", "-b", branch, str(path), target_head)
    return path, branch


def remove_candidate(repo_root: Path, path: Path | None, branch: str | None, *, delete_branch: bool) -> dict[str, bool]:
    result = {"worktree_removed": False, "branch_deleted": False}
    if path and Path(path).exists():
        gitx.git(repo_root, "worktree", "remove", "--force", str(path), check=False)
        if Path(path).exists():
            shutil.rmtree(path, ignore_errors=True)
        result["worktree_removed"] = not Path(path).exists()
    gitx.git(repo_root, "worktree", "prune", check=False)
    if delete_branch and branch and gitx.branch_exists(repo_root, branch):
        r = gitx.git(repo_root, "branch", "-D", branch, check=False)
        result["branch_deleted"] = r.ok
    return result


@contextmanager
def temp_worktree(repo_root: Path, head: str, base_dir: Path, name: str, overlay: tuple[str, list[str]] | None = None) -> Iterator[Path]:
    """在 base_dir/name 建 detached 临时 worktree。overlay=(commit, paths) 把指定提交里的路径叠上去。"""
    path = (base_dir / name).resolve()
    if path.exists():
        gitx.git(repo_root, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitx.git(repo_root, "worktree", "add", "--detach", str(path), head)
    try:
        if overlay:
            commit, paths = overlay
            if paths:
                gitx.git(path, "checkout", commit, "--", *paths)
        yield path
    finally:
        gitx.git(repo_root, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        gitx.git(repo_root, "worktree", "prune", check=False)


def residue(path: Path) -> list[str]:
    """未提交改动（tracked 修改 + 未忽略的 untracked）。"""
    return [p for _, p in gitx.status_porcelain(path)]


def assert_candidate_clean(path: Path) -> None:
    dirty = residue(path)
    if dirty:
        raise fatal("CANDIDATE_DIRTY", "候选 worktree 有未提交改动，先 checkpoint", paths=dirty[:50])


def assert_candidate_identity(path: Path, branch: str, expected_head: str | None) -> str:
    if not path.is_dir():
        raise fatal("CANDIDATE_WORKTREE_MISSING", f"候选 worktree 不存在: {path}", path=str(path))
    cur_branch = gitx.current_branch(path)
    if cur_branch != branch:
        raise fatal("CANDIDATE_BRANCH_MISMATCH", f"候选 worktree 当前分支 {cur_branch}，期望 {branch}")
    h = gitx.head(path)
    if expected_head and h != expected_head:
        raise fatal("CANDIDATE_HEAD_MISMATCH", "候选 worktree HEAD 与 ledger 记录不一致，请勿绕过 checkpoint 提交", ledger=expected_head, actual=h)
    return h
