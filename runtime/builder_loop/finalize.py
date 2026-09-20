"""finalize：把已审 candidate tree 以单个提交写回目标分支（expected-old CAS）。

顺序：前置检查 → commit-tree（或临时 worktree 跑 commit hook 后比对 tree）→ 落盘 finalize_intent
→ update-ref CAS → 同步目标 checkout → 删候选 worktree/分支 → 终态。
失败按"ref 是否回滚成功"决定 phase；中断后再次 finalize 会沿 intent 恢复。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import evidence, gitx, ledger as ledger_mod, worktree
from .errors import fatal, negative


def _default_message(lg: dict[str, Any]) -> str:
    m = lg["contract"]["mission"]
    return f"feat({m['slug']}): [cr_id_skip] {m['objective']}"


def _changed_paths(repo_root: Path, lg: dict[str, Any]) -> list[str]:
    return gitx.changed_paths(repo_root, lg["repo"]["target_start_head"], lg["candidate"]["head"])


def _checkout_on_target(repo_root: Path, target: str) -> bool:
    return gitx.current_branch(repo_root) == target


def _dirty_overlap(repo_root: Path, changed: list[str]) -> list[str]:
    dirty = {p for _, p in gitx.status_porcelain(repo_root)}
    return sorted(dirty.intersection(changed))


def _make_final_commit(repo_root: Path, lg: dict[str, Any], message: str, run_commit_hook: bool, run_dir: Path) -> str:
    target_head = lg["repo"]["target_start_head"]
    cand_head = lg["candidate"]["head"]
    cand_tree = gitx.tree_of(repo_root, cand_head)
    if not run_commit_hook:
        return gitx.commit_tree(repo_root, cand_tree, target_head, message)
    with worktree.temp_worktree(repo_root, target_head, run_dir / "tmp", "finalize") as wt:
        gitx.git(wt, "read-tree", "--reset", "-u", cand_tree)
        gitx.git(wt, "-c", "commit.gpgSign=false", "commit", "--quiet", "--allow-empty", "-m", message, hooks=True)
        final = gitx.head(wt)
        if gitx.tree_of(repo_root, final) != cand_tree:
            raise negative("FINAL_COMMIT_TREE_MISMATCH", "仓库 commit hook 改写了 tree，与已审 candidate 不一致，拒绝写回", candidate_tree=cand_tree, final_tree=gitx.tree_of(repo_root, final))
        return final


def _complete(ledger_path: Path, repo_root: Path, lg: dict[str, Any], final_head: str) -> dict[str, Any]:
    target = lg["repo"]["target_branch"]
    removed = worktree.remove_run_worktrees(repo_root, lg, delete_branches=True)
    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["finalize_intent"] = None
        lg2["terminal"] = {"status": "finalized", "final_head": final_head, "reason": None, "at": ledger_mod.now_iso()}
    # session 保持绑定：复盘记录写进 ledger 之前 Stop hook 会拦住（复盘硬闸门）
    return {"final_head": final_head, "target_branch": target, "cleanup": removed, "terminal": "finalized", "next": "retro",
            "unaddressed_findings": _unaddressed_findings(lg)}


def _unaddressed_findings(lg: dict[str, Any]) -> list[dict[str, Any]]:
    """reviewer 判 pass 时给的 owner=tester 的 finding。readiness 只派发 blocking / major 且 reviewer 为 fail 的，
    其余到这里就没有去向了（#249）——不阻塞 finalize，但必须出现在收尾输出里，不能悄悄消失。"""
    findings = ((lg["evidence"].get("reviewer") or {}).get("details") or {}).get("findings") or []
    return [f for f in findings if f.get("owner") == "tester"]


def _sync_checkout(repo_root: Path, lg: dict[str, Any], old: str, new: str) -> dict[str, Any] | None:
    """目标分支若被主仓 checkout，用 read-tree -u -m 同步工作区；失败尝试回滚 ref。"""
    if not _checkout_on_target(repo_root, lg["repo"]["target_branch"]):
        return None
    r = gitx.read_tree_um(repo_root, old, new)
    if r.ok:
        return {"synced": True}
    rb = gitx.update_ref_cas(repo_root, f"refs/heads/{lg['repo']['target_branch']}", old, new)
    return {"synced": False, "stderr": r.stderr[-2000:], "ref_rolled_back": rb.ok}


def recover(ledger_path: Path, repo_root: Path, lg: dict[str, Any]) -> dict[str, Any]:
    intent = lg["finalize_intent"]
    target = lg["repo"]["target_branch"]
    ref = f"refs/heads/{target}"
    live = gitx.branch_head(repo_root, target)
    if live == intent["final_head"]:
        return _complete(ledger_path, repo_root, lg, intent["final_head"])
    if live != intent["expected_target_head"]:
        raise negative("FINALIZE_INTENT_TARGET_DIVERGED", "目标分支既不在 intent 起点也不在终点，需人工处理", live=live, intent=intent)
    r = gitx.update_ref_cas(repo_root, ref, intent["final_head"], intent["expected_target_head"])
    if not r.ok:
        raise negative("FINALIZE_CAS_FAILED", "重放 CAS 失败", stderr=r.stderr[-2000:])
    sync = _sync_checkout(repo_root, lg, intent["expected_target_head"], intent["final_head"])
    if sync and not sync["synced"]:
        return _sync_failed(ledger_path, lg, sync)
    return _complete(ledger_path, repo_root, lg, intent["final_head"])


def _sync_failed(ledger_path: Path, lg: dict[str, Any], sync: dict[str, Any]) -> dict[str, Any]:
    with ledger_mod.mutate(ledger_path) as lg2:
        if sync.get("ref_rolled_back"):
            lg2["finalize_intent"] = None
            status = "active"
        else:
            lg2["terminal"] = {"status": "finalize_failed", "final_head": lg2["finalize_intent"]["final_head"], "reason": "checkout sync failed and ref rollback failed", "at": ledger_mod.now_iso()}
            status = "finalize_failed"
    raise negative("FINALIZE_CHECKOUT_SYNC_FAILED", "目标分支 ref 已更新但工作区同步失败", ref_rolled_back=sync.get("ref_rolled_back"), status=status, stderr=sync.get("stderr"))


def finalize(ledger_path: Path, repo_root: Path, message: str | None, *, run_commit_hook: bool = False) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    if lg.get("finalize_intent"):
        return recover(ledger_path, repo_root, lg)

    readiness = evidence.readiness(lg, repo_root)
    if readiness["next_action"] != evidence.ACTION_FINALIZE:
        raise negative("EVIDENCE_NOT_READY", "四项 evidence 尚未全部 fresh pass", readiness=readiness)
    cand = lg["candidate"]
    wt = Path(cand["worktree"])
    worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])
    worktree.assert_candidate_clean(wt)

    target = lg["repo"]["target_branch"]
    live = gitx.branch_head(repo_root, target)
    if live != lg["repo"]["target_start_head"]:
        raise negative("TARGET_DRIFT", f"目标分支 {target} 已前进，先 `bl rebase` 再重验", live=live, target_start_head=lg["repo"]["target_start_head"])
    changed = _changed_paths(repo_root, lg)
    if not changed:
        raise negative("NO_CHANGES", "candidate 与目标分支没有差异")
    if _checkout_on_target(repo_root, target):
        overlap = _dirty_overlap(repo_root, changed)
        if overlap:
            raise negative("DIRTY_OVERLAP", "主仓工作区的未提交改动与本次变更路径重叠，先处理主仓 dirty", paths=overlap)

    final_head = _make_final_commit(repo_root, lg, message or _default_message(lg), run_commit_hook, ledger_path.parent)
    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["finalize_intent"] = {"expected_target_head": live, "candidate_head": cand["head"], "final_head": final_head, "at": ledger_mod.now_iso()}

    r = gitx.update_ref_cas(repo_root, f"refs/heads/{target}", final_head, live)
    if not r.ok:
        with ledger_mod.mutate(ledger_path) as lg2:
            lg2["finalize_intent"] = None
        raise negative("FINALIZE_CAS_FAILED", "目标分支在 finalize 期间被移动，未写回", stderr=r.stderr[-2000:])
    sync = _sync_checkout(repo_root, lg, live, final_head)
    if sync and not sync["synced"]:
        return _sync_failed(ledger_path, lg, sync)
    out = _complete(ledger_path, repo_root, lg, final_head)
    out["changed_paths"] = changed
    return out


# ---------------------------------------------------------------- rebase


def rebase_candidate(ledger_path: Path, repo_root: Path) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    cand = lg["candidate"]
    wt = Path(cand["worktree"])
    target = lg["repo"]["target_branch"]
    live = gitx.branch_head(repo_root, target)
    gitdir = Path(gitx.git(wt, "rev-parse", "--git-dir").stdout.strip())
    if not gitdir.is_absolute():
        gitdir = wt / gitdir
    in_progress = (gitdir / "rebase-merge").exists() or (gitdir / "rebase-apply").exists()

    if not in_progress:
        worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])
        if live == lg["repo"]["target_start_head"]:
            return {"rebased": False, "reason": "目标分支未前进", "conflicts": []}
        worktree.assert_candidate_clean(wt)
        r = gitx.git(wt, "rebase", live, check=False)
        if not r.ok:
            conflicts = [p for xy, p in gitx.status_porcelain(wt) if "U" in xy]
            return {"rebased": False, "conflicts": conflicts, "hint": "在候选 worktree 内解决冲突后 `git add` + `git rebase --continue`，再运行 `bl rebase`", "stderr": r.stderr[-2000:]}
    else:
        conflicts = [p for xy, p in gitx.status_porcelain(wt) if "U" in xy]
        if conflicts:
            return {"rebased": False, "conflicts": conflicts, "hint": "仍有冲突未解决"}
        raise negative("REBASE_IN_PROGRESS", "rebase 尚未完成，先 `git rebase --continue`")

    new_head = gitx.head(wt)
    if not gitx.is_ancestor(repo_root, live, new_head):
        raise fatal("REBASE_RESULT_INVALID", "候选 HEAD 不在新目标 HEAD 之后", head=new_head, live=live)
    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["repo"]["target_start_head"] = live
        lg2["candidate"]["head"] = new_head
        lg2["candidate"]["checkpoints"].append({"head": new_head, "at": ledger_mod.now_iso(), "role": "rebase", "paths": []})
        readiness = evidence.readiness(lg2, repo_root)
    return {"rebased": True, "new_target_start_head": live, "candidate_head": new_head, "conflicts": [], "readiness": readiness}
