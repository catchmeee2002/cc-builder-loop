"""run 起点的基线事实：目标分支与它的上游是否一致（#385）。

判定只读本地已有的 ref，不执行 fetch——runtime 不做网络操作。上游 ref 从未 fetch 过时如实
报 `unverified`，不把「没看到」写成「一致」（原则一）。

基线仍是本地 `refs/heads/<target>`：它是 finalize 的 CAS 目标、rebase 的 onto、checkout 同步的
起点，换掉它会让这三个事务失去同一个事实源。上游只参与准入判定。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import gitx
from .errors import fatal, negative

IN_SYNC = "in_sync"
AHEAD = "ahead"
BEHIND = "behind"
DIVERGED = "diverged"
LOCAL_ONLY = "local_only"
UNVERIFIED = "unverified"
STALE = (BEHIND, DIVERGED)


def _pick_upstream(repo_root: Path, branch: str) -> tuple[str | None, str]:
    """(上游 ref, 取法)。按 upstream 配置 → origin/<b> → 唯一 remote 的顺序找可用且已 fetch 的。"""
    configured = gitx.upstream_ref(repo_root, branch)
    if configured and gitx.remote_branch_head(repo_root, configured):
        return configured, "configured"
    origin = f"origin/{branch}"
    if gitx.remote_branch_head(repo_root, origin):
        return origin, "origin"
    names = gitx.remotes(repo_root)
    if len(names) == 1:
        single = f"{names[0]}/{branch}"
        if gitx.remote_branch_head(repo_root, single):
            return single, "single_remote"
    return None, "unavailable"


def target_baseline(repo_root: Path, branch: str) -> dict[str, Any]:
    """目标分支与上游的关系。纯观察，不抛错，调用方决定要不要拦。"""
    local = gitx.branch_head(repo_root, branch)
    names = gitx.remotes(repo_root)
    upstream, source = _pick_upstream(repo_root, branch)
    facts: dict[str, Any] = {
        "branch": branch,
        "local_head": local,
        "remotes": names,
        "upstream_ref": upstream,
        "upstream_source": source,
        "remote_head": None,
        "relation": LOCAL_ONLY if not names else UNVERIFIED,
        "verified": False,
        "acknowledged": False,
        "warning": None,
    }
    if not names:
        facts["warning"] = (f"仓库没有配置任何 git remote：{branch} 的本地 ref 无从与上游比对，"
                            "run 基线就是本地 ref")
        return facts
    if upstream is None:
        facts["warning"] = (f"无法确定 {branch} 的上游 ref（未配置 upstream，也没有可用的 origin/{branch}）："
                            "run 基线取本地 ref，未与远端比对；建议先 `git fetch` 并设置 upstream")
        return facts
    remote_head = gitx.remote_branch_head(repo_root, upstream)
    if remote_head is None:
        facts["warning"] = (f"上游 {upstream} 在本地不存在（从未 fetch 过该分支）："
                            "run 基线取本地 ref，未与远端比对；建议先 `git fetch`")
        return facts
    facts["remote_head"] = remote_head
    facts["relation"] = gitx.relation(repo_root, local, remote_head)
    facts["verified"] = True
    if facts["relation"] == AHEAD:
        facts["note"] = f"本地 {branch} 比 {upstream} 多提交；写回后仍可快进推送，不拦截"
    return facts


def require_target_baseline(repo_root: Path, branch: str, *, allow_stale: bool = False,
                            negative_exit: bool = False, context: str = "") -> dict[str, Any]:
    """落后或分叉时拒绝——不是静默采用旧基线（#385）。

    `allow_stale` 只给 `bl start` 用：承认旧基线开工是 run 开始时的决定，不改变写回条件。
    `negative_exit` 区分出口：start 是用法错误（fatal），rebase / finalize 是 run 状态为负。
    """
    facts = target_baseline(repo_root, branch)
    if facts["relation"] not in STALE:
        return facts
    if allow_stale:
        facts["acknowledged"] = True
        return facts
    details = {
        "relation": facts["relation"],
        "branch": branch,
        "local_head": facts["local_head"],
        "remote_head": facts["remote_head"],
        "upstream_ref": facts["upstream_ref"],
        "context": context,
    }
    message = (f"目标分支 {branch} 与上游 {facts['upstream_ref']} 不一致（{facts['relation']}）："
               "先 `git fetch` 并把本地分支快进/合并到上游，再重试")
    if context == "start":
        message += "；确认要以本地旧基线开工时加 --allow-stale-target"
    if negative_exit:
        raise negative("TARGET_NOT_IN_SYNC", message, **details)
    raise fatal("TARGET_NOT_IN_SYNC", message, **details)
