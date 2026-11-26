"""worktree 管理。

每个 run 在仓库同级目录 `../builder-loop-worktrees/<repo-name>/<run_id>/` 下有两个 worktree（可由
loop.yml `worktree.root` 覆盖根目录），放仓库外是为了不被 `ruff check .` 之类的全仓命令扫到：

- `builder/`  候选，分支 `builder-loop/<run_id>/candidate`
- `tester/`   tester 的冻结基线，分支 `builder-loop/<run_id>/tester`，永不 rebase

两者都从 start 时的目标分支 HEAD 起。tester 写测试时看不到候选（原则一）。
临时 worktree 放 run 目录下 `tmp/`，用完即删。
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from . import gitx
from .errors import fatal

BRANCH_PREFIX = "builder-loop/"
ROLE_DIRS = {"candidate": "builder", "tester": "tester"}


def branch_name(run_id: str, kind: str) -> str:
    return f"{BRANCH_PREFIX}{run_id}/{kind}"


def run_root(repo_root: Path, override: str | None, run_id: str) -> Path:
    if override:
        p = Path(override)
        base = (p if p.is_absolute() else repo_root / p).resolve()
    else:
        base = (repo_root.parent / "builder-loop-worktrees" / repo_root.name).resolve()
    return base / run_id


def _add(repo_root: Path, path: Path, branch: str, head: str) -> None:
    if path.exists():
        raise fatal("WORKTREE_PATH_EXISTS", f"worktree 路径已存在: {path}", path=str(path))
    if gitx.branch_exists(repo_root, branch):
        raise fatal("BRANCH_EXISTS", f"分支已存在: {branch}", branch=branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitx.git(repo_root, "worktree", "add", "-b", branch, str(path), head)


# ---------------------------------------------------------------- 依赖链接（#383）


def link_paths(src_root: Path, dst_root: Path, paths: list[str]) -> list[str]:
    """把声明路径从源树链接进目标 worktree，返回实际建立的相对路径。

    只建符号链接：不复制、不安装、不清理链接目标——依赖目录归声明方负责（与 proof 现场链接同一语义）。
    git 已跟踪的同名内容优先，不覆盖。
    """
    made: list[str] = []
    for rel in paths:
        dst = dst_root / rel
        if dst.exists() or dst.is_symlink():
            continue
        src = src_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src, target_is_directory=src.is_dir())
        made.append(rel)
    return made


def missing_link_paths(src_root: Path, paths: list[str]) -> list[str]:
    return [rel for rel in paths if not (src_root / rel).exists()]


def under_link(path: str, links: list[str]) -> bool:
    """链接本身不算现场被改动：目录型 .gitignore 规则不忽略符号链接写出的 untracked 条目。

    只认精确路径——git 对符号链接只报一条 `?? <path>`，不展开链接内部；声明的路径若其实是被
    跟踪的真实目录，其内部新写的文件仍算现场改动。
    """
    return path in links


def link_ignores(ledger: dict[str, Any]) -> list[str]:
    """本 run 声明的依赖链接路径；旧 ledger 没有该键时为空（行为与升级前一致）。"""
    cfg = ledger.get("loop_config") or {}
    return list(cfg.get("worktree_link_paths") or [])


def prepare_links(ledger: dict[str, Any], dst_root: Path) -> list[str]:
    """按 ledger 里的声明把依赖链接进 dst_root。源缺失由调用方在 start 时先拒绝。"""
    links = link_ignores(ledger)
    if not links:
        return []
    root = Path(ledger["repo"]["root"])
    source = (ledger.get("loop_config") or {}).get("worktree_link_source")
    return link_paths(root / source if source else root, dst_root, links)


def create_run_worktrees(repo_root: Path, run_id: str, head: str, root_override: str | None, *, with_tester: bool,
                         link_paths_: list[str] | None = None, link_source: str | None = None) -> dict[str, dict[str, str]]:
    """建候选（以及可选的 tester）worktree；第二个失败时回滚第一个，不留孤儿。"""
    root = run_root(repo_root, root_override, run_id)
    src_root = repo_root / link_source if link_source else repo_root
    created: dict[str, dict[str, str]] = {}
    try:
        for kind in (("candidate", "tester") if with_tester else ("candidate",)):
            path, branch = root / ROLE_DIRS[kind], branch_name(run_id, kind)
            _add(repo_root, path, branch, head)
            if link_paths_:
                link_paths(src_root, path, link_paths_)
            created[kind] = {"worktree": str(path), "branch": branch}
    except BaseException:
        for info in created.values():
            remove_worktree(repo_root, Path(info["worktree"]), info["branch"], delete_branch=True)
        _rmdir_if_empty(root)
        raise
    return created


def remove_worktree(repo_root: Path, path: Path | None, branch: str | None, *, delete_branch: bool) -> dict[str, bool]:
    result = {"worktree_removed": False, "branch_deleted": False}
    if path and Path(path).exists():
        gitx.git(repo_root, "worktree", "remove", "--force", str(path), check=False)
        if Path(path).exists():
            shutil.rmtree(path, ignore_errors=True)
    result["worktree_removed"] = not (path and Path(path).exists())
    gitx.git(repo_root, "worktree", "prune", check=False)
    if delete_branch and branch and gitx.branch_exists(repo_root, branch):
        result["branch_deleted"] = gitx.git(repo_root, "branch", "-D", branch, check=False).ok
    return result


def remove_run_worktrees(repo_root: Path, lg: dict[str, Any], *, delete_branches: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    parents: set[Path] = set()
    for key in ("candidate", "tester"):
        info = lg.get(key)
        if not info or not info.get("worktree"):
            continue
        path = Path(info["worktree"])
        parents.add(path.parent)
        out[key] = remove_worktree(repo_root, path, info.get("branch"), delete_branch=delete_branches)
    for parent in parents:
        _rmdir_if_empty(parent)
    return out


def _rmdir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


@contextmanager
def temp_worktree(repo_root: Path, head: str, base_dir: Path, name: str, overlay: tuple[str, list[str], list[str]] | None = None) -> Iterator[Path]:
    """base_dir/name 下的 detached 临时 worktree。
    overlay=(commit, present_paths, deleted_paths)：把 commit 里的 present_paths 叠上来，并删掉 deleted_paths。"""
    path = (base_dir / name).resolve()
    if path.exists():
        gitx.git(repo_root, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitx.git(repo_root, "worktree", "add", "--detach", str(path), head)
    try:
        if overlay:
            commit, present, deleted = overlay
            if present:
                gitx.git(path, "checkout", commit, "--", *present)
            for rel in deleted:
                target = path / rel
                if target.is_file() or target.is_symlink():
                    target.unlink()
        yield path
    finally:
        gitx.git(repo_root, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        gitx.git(repo_root, "worktree", "prune", check=False)


def residue(path: Path, ignore: Sequence[str] = ()) -> list[str]:
    """未提交改动（tracked 修改 + 未忽略的 untracked）。

    `ignore` 是 run 起点声明的依赖链接：由 runtime 建、不是写入者的改动（#383）。只放行**真的是
    符号链接**的那几条——声明了同名但被 git 跟踪的真实目录时，它内部的新文件仍算改动。
    """
    declared = set(ignore)
    return [p for _, p in gitx.status_porcelain(path) if not (p in declared and (path / p).is_symlink())]


def assert_clean(path: Path, label: str = "候选", ignore: Sequence[str] = ()) -> None:
    dirty = residue(path, ignore)
    if dirty:
        raise fatal("WORKTREE_DIRTY", f"{label} worktree 有未提交改动，先 checkpoint", paths=dirty[:50], worktree=str(path))


def assert_identity(path: Path, branch: str, expected_head: str | None, label: str = "候选") -> str:
    if not path.is_dir():
        raise fatal("WORKTREE_MISSING", f"{label} worktree 不存在: {path}", path=str(path))
    cur_branch = gitx.current_branch(path)
    if cur_branch != branch:
        raise fatal("WORKTREE_BRANCH_MISMATCH", f"{label} worktree 当前分支 {cur_branch}，期望 {branch}")
    h = gitx.head(path)
    if expected_head and h != expected_head:
        raise fatal("WORKTREE_HEAD_MISMATCH", f"{label} worktree HEAD 与 ledger 记录不一致，请勿绕过 checkpoint 提交", ledger=expected_head, actual=h)
    return h


# 旧名字保留给 machine / proof / finalize 调用点
def assert_candidate_clean(path: Path, ignore: Sequence[str] = ()) -> None:
    assert_clean(path, "候选", ignore)


def assert_candidate_identity(path: Path, branch: str, expected_head: str | None) -> str:
    return assert_identity(path, branch, expected_head, "候选")
