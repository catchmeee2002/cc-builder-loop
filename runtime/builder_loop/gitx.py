"""git 包装。所有命令显式传 cwd，不依赖进程 cwd（hook 的 cwd 不可信）。"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import fatal


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def git(cwd: Path | str, *args: str, check: bool = True, input_text: str | None = None) -> GitResult:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        input=input_text,
    )
    result = GitResult(proc.returncode, proc.stdout, proc.stderr)
    if check and not result.ok:
        raise fatal("GIT_COMMAND_FAILED", f"git {' '.join(args)} 失败: {proc.stderr.strip()}", args=list(args), cwd=str(cwd))
    return result


def toplevel(cwd: Path | str) -> Path:
    r = git(cwd, "rev-parse", "--show-toplevel", check=False)
    if not r.ok:
        raise fatal("NOT_A_GIT_REPO", f"{cwd} 不在 git 仓库内")
    return Path(r.stdout.strip()).resolve()


def common_dir(cwd: Path | str) -> Path:
    """主仓的 .git 目录（worktree 内也返回主仓）。"""
    r = git(cwd, "rev-parse", "--git-common-dir")
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve()


def main_repo_root(cwd: Path | str) -> Path:
    """从任意 worktree 回到主仓根目录。"""
    return common_dir(cwd).parent


def rev_parse(cwd: Path | str, ref: str) -> str:
    r = git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if not r.ok:
        raise fatal("GIT_REF_UNKNOWN", f"无法解析 {ref}", ref=ref)
    return r.stdout.strip()


def branch_exists(cwd: Path | str, branch: str) -> bool:
    return git(cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).ok


def branch_head(cwd: Path | str, branch: str) -> str:
    return rev_parse(cwd, f"refs/heads/{branch}")


def current_branch(cwd: Path | str) -> str | None:
    r = git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return r.stdout.strip() if r.ok else None


def head(cwd: Path | str) -> str:
    return rev_parse(cwd, "HEAD")


def tree_of(cwd: Path | str, commit: str) -> str:
    return git(cwd, "rev-parse", f"{commit}^{{tree}}").stdout.strip()


def is_ancestor(cwd: Path | str, ancestor: str, descendant: str) -> bool:
    return git(cwd, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def status_porcelain(cwd: Path | str) -> list[tuple[str, str]]:
    """返回 [(XY, path)]，不含 ignored 文件。"""
    r = git(cwd, "status", "--porcelain=v1", "--untracked-files=all", "-z")
    out: list[tuple[str, str]] = []
    parts = r.stdout.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        xy, path = entry[:2], entry[3:]
        if xy[0] in ("R", "C"):
            i += 1  # rename 的原路径占下一个字段
        out.append((xy, path))
        i += 1
    return out


def is_clean(cwd: Path | str) -> bool:
    return not status_porcelain(cwd)


def changed_paths(cwd: Path | str, base: str, target: str) -> list[str]:
    r = git(cwd, "diff", "--name-only", "-z", f"{base}..{target}")
    return [p for p in r.stdout.split("\0") if p]


def ls_tree_blobs(cwd: Path | str, commit: str, paths: list[str] | None = None) -> dict[str, str]:
    """{path: blob_sha}；paths 为 None 时列全树。"""
    args = ["ls-tree", "-r", "-z", commit]
    if paths:
        args += ["--", *paths]
    r = git(cwd, *args)
    out: dict[str, str] = {}
    for entry in r.stdout.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, typ, sha = meta.split()
        if typ == "blob":
            out[path] = sha
    return out


def blob_mode(cwd: Path | str, commit: str, path: str) -> str | None:
    r = git(cwd, "ls-tree", "-z", commit, "--", path, check=False)
    if not r.ok or not r.stdout.strip("\0"):
        return None
    return r.stdout.split()[0]


def commit_tree(cwd: Path | str, tree: str, parent: str, message: str) -> str:
    r = git(cwd, "-c", "commit.gpgSign=false", "commit-tree", tree, "-p", parent, input_text=message)
    return r.stdout.strip()


def update_ref_cas(cwd: Path | str, ref: str, new: str, expected_old: str) -> GitResult:
    """带 expected-old 的原子更新；失败不抛，交给调用方决定。"""
    return git(cwd, "update-ref", ref, new, expected_old, check=False)


def read_tree_um(cwd: Path | str, old: str, new: str) -> GitResult:
    return git(cwd, "read-tree", "-u", "-m", old, new, check=False)


def worktree_list(cwd: Path | str) -> list[dict[str, str]]:
    r = git(cwd, "worktree", "list", "--porcelain")
    out: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
                cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val
    if cur:
        out.append(cur)
    return out


def config_get(cwd: Path | str, key: str) -> str | None:
    r = git(cwd, "config", "--get", key, check=False)
    return r.stdout.strip() if r.ok else None
