"""finalize：把已审 candidate tree 以单个提交写回目标分支（expected-old CAS）。

顺序：前置检查 → commit-tree（或临时 worktree 跑 commit hook 后比对 tree）→ 落盘 finalize_intent
→ update-ref CAS → 同步目标 checkout → 删候选 worktree/分支 → 终态。
失败按"ref 是否回滚成功"决定 phase；中断后再次 finalize 会沿 intent 恢复。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import contract as contract_mod
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
    if readiness["next_action"] == evidence.ACTION_HELD:
        raise negative("HOLD_ACTIVE", "run 处于用户授权的 hold：外部条件满足后先 `bl hold --release`", hold=evidence.hold_state(lg))
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
    """先 rebase 候选，再在目标分支改过 tester 文件时把 tester 分支也 rebase 过去（#298）。
    两半各自可以停在冲突上；再次 `bl rebase` 从停下的那一半继续。"""
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    cand = lg["candidate"]
    wt = Path(cand["worktree"])
    target = lg["repo"]["target_branch"]
    live = gitx.branch_head(repo_root, target)

    onto = live
    resumed = _resumed_candidate_rebase(lg, repo_root, wt)
    if resumed:
        onto = resumed  # 冲突解完、`git rebase --continue` 之后：采纳的是当初起的那次 rebase，不是此刻的目标分支（原则七）
    elif not gitx.rebase_in_progress(wt):
        worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])
        if live == lg["repo"]["target_start_head"]:
            tester = sync_tester_rebase(ledger_path, repo_root, start=True)
            out: dict[str, Any] = {"rebased": False, "reason": "目标分支未前进", "conflicts": [], "tester_rebase": tester}
            if tester["status"] != "not_needed":
                out["readiness"] = evidence.readiness(ledger_mod.load(ledger_path), repo_root)
            return out
        worktree.assert_candidate_clean(wt)
        r = gitx.git(wt, "rebase", live, check=False)
        if not r.ok:
            if not gitx.rebase_in_progress(wt):
                raise fatal("GIT_COMMAND_FAILED", f"候选 rebase 失败: {r.stderr.strip()[-500:]}", worktree=str(wt))
            with ledger_mod.mutate(ledger_path) as lg2:
                ledger_mod.log_event(lg2, "candidate_rebase", status="conflict", onto=live, from_head=cand["head"])
            conflicts = _settle_tester_owned_conflicts(lg, wt)
            if conflicts:
                return {"rebased": False, "conflicts": conflicts, "hint": "在候选 worktree 内解决冲突后 `git add` + `git rebase --continue`，再运行 `bl rebase`", "stderr": r.stderr[-2000:]}
            onto = live
    else:
        conflicts = _settle_tester_owned_conflicts(lg, wt)
        if conflicts:
            return {"rebased": False, "conflicts": conflicts, "hint": "仍有冲突未解决"}
        if gitx.rebase_in_progress(wt):
            raise negative("REBASE_IN_PROGRESS", "rebase 尚未完成，先 `git rebase --continue`")
        onto = _resumed_candidate_rebase(lg, repo_root, wt) or live

    new_head = gitx.head(wt)
    if not gitx.is_ancestor(repo_root, onto, new_head):
        raise fatal("REBASE_RESULT_INVALID", "候选 HEAD 不在新目标 HEAD 之后", head=new_head, live=onto)
    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["repo"]["target_start_head"] = onto
        lg2["candidate"]["head"] = new_head
        lg2["candidate"]["checkpoints"].append({"head": new_head, "at": ledger_mod.now_iso(), "role": "rebase", "paths": []})
    tester = sync_tester_rebase(ledger_path, repo_root, start=True)
    readiness = evidence.readiness(ledger_mod.load(ledger_path), repo_root)
    return {"rebased": True, "new_target_start_head": onto, "candidate_head": new_head, "conflicts": [], "tester_rebase": tester, "readiness": readiness}


def _settle_tester_owned_conflicts(lg: dict[str, Any], wt: Path) -> list[str]:
    """候选 rebase 停在冲突上时，tester 拥有的路径直接取目标分支一侧并继续，只把 builder 的冲突留给 builder。

    候选里的测试文件只是 integrate 从 tester 分支派生的副本（原则二：测试实现的家是 tester 分支）；
    真正的合并由 tester 分支的 rebase 完成（冲突归 tester 解），之后 integrate 再按路径叠回来。
    builder 本来就不能写这些路径，让它解冲突既越权又会造出第二份测试事实。返回剩下的冲突（空 = rebase 已走完）。"""
    auth = lg["contract"]["authority"]
    while gitx.rebase_in_progress(wt):
        conflicts = gitx.unmerged_paths(wt)
        if not conflicts:
            return []  # 已由人解完、等 `git rebase --continue`：交还调用方判断
        mine = [p for p in conflicts if contract_mod.path_owner(auth, p) == contract_mod.OWNER_TESTER]
        if len(mine) != len(conflicts):
            return sorted(set(conflicts) - set(mine))
        for p in mine:
            # rebase 中 --ours 是目标分支一侧；目标分支上没有它（被删了）就跟着删
            if gitx.git(wt, "checkout", "--ours", "--", p, check=False).ok:
                gitx.git(wt, "add", "--", p)
            else:
                gitx.git(wt, "rm", "--quiet", "--", p)
        r = gitx.git(wt, "-c", "core.editor=true", "rebase", "--continue", check=False)
        if not r.ok and not gitx.rebase_in_progress(wt):
            raise fatal("GIT_COMMAND_FAILED", f"候选 rebase --continue 失败: {r.stderr.strip()[-500:]}", worktree=str(wt))
    return []


def _resumed_rebase(lg: dict[str, Any], repo_root: Path, wt: Path, kind: str, branch: str, head: str) -> str | None:
    """wt 里那次有冲突的 rebase 已经被 `git rebase --continue` 完成：返回它的 onto，否则 None。
    只认 runtime 自己起的那次（最近一条 kind 冲突事件的 from_head 就是 ledger 记录的 HEAD），且结果落在 onto 之上（原则七）。"""
    if not wt.is_dir() or gitx.rebase_in_progress(wt):
        return None
    cur = gitx.head(wt)
    if cur == head:
        return None
    started = [e for e in ledger_mod.events_of(lg, kind) if e.get("status") == "conflict"]
    if not started or started[-1].get("from_head") != head:
        return None
    onto = started[-1]["onto"]
    if gitx.current_branch(wt) != branch or not gitx.is_ancestor(repo_root, onto, cur):
        return None
    return onto


def _resumed_candidate_rebase(lg: dict[str, Any], repo_root: Path, wt: Path) -> str | None:
    cand = lg["candidate"]
    onto = _resumed_rebase(lg, repo_root, wt, "candidate_rebase", cand["branch"], cand["head"])
    if onto:
        worktree.assert_candidate_clean(wt)
    return onto


def sync_tester_rebase(ledger_path: Path, repo_root: Path, *, start: bool) -> dict[str, Any]:
    """让 tester 分支跟上 target_start_head——只在目标分支改过 tester 文件时（没有重叠就不动，tester evidence 不受漂移影响）。

    status：not_needed / rebased / conflict（停在 tester worktree 里等 tester 解）/ deferred（tester 在跑或 worktree 不干净）/
    pending（start=False 时只报告不动手）。tester 解完冲突 `git rebase --continue` 后 HEAD 已经前进：只有它确实是
    runtime 起的那次 rebase（事件里的 from_head / onto 对得上）且落在 onto 之上，才采纳为新的 tester.base / head（原则七）。
    tester 的 checkpoint 先调这里（start=False），所以它解完冲突直接交卷也能登记。"""
    lg = ledger_mod.load(ledger_path)
    t = lg.get("tester")
    overlap = evidence.tester_drift_overlap(lg, repo_root)
    if not t or not overlap:
        return {"status": "not_needed", "paths": []}
    onto = lg["repo"]["target_start_head"]
    wt = Path(t["worktree"])
    if gitx.rebase_in_progress(wt):
        return {"status": "conflict", "paths": gitx.unmerged_paths(wt) or overlap, "worktree": str(wt)}
    cur = gitx.head(wt)
    if cur != t["head"]:
        if _resumed_rebase(lg, repo_root, wt, "tester_rebase", t["branch"], t["head"]) != onto:
            worktree.assert_identity(wt, t["branch"], t["head"], "tester")  # → WORKTREE_HEAD_MISMATCH
        return _adopt_tester_rebase(ledger_path, onto, t["head"], cur)
    if not start:
        return {"status": "pending", "paths": overlap}
    if evidence.role_running(lg, "tester") or not gitx.is_clean(wt):
        return {"status": "deferred", "paths": overlap, "hint": "tester 在跑或它的 worktree 有未提交改动；它交卷后再运行 `bl rebase`"}
    worktree.assert_identity(wt, t["branch"], t["head"], "tester")
    r = gitx.git(wt, "-c", "core.editor=true", "rebase", onto, check=False)
    if not r.ok:
        if not gitx.rebase_in_progress(wt):
            raise fatal("GIT_COMMAND_FAILED", f"tester 分支 rebase 失败: {r.stderr.strip()[-500:]}", worktree=str(wt))
        paths = gitx.unmerged_paths(wt) or overlap
        with ledger_mod.mutate(ledger_path) as lg2:
            ledger_mod.log_event(lg2, "tester_rebase", status="conflict", onto=onto, from_head=t["head"], paths=paths)
        return {"status": "conflict", "paths": paths, "worktree": str(wt),
                "hint": "续接 tester：它在自己的 worktree 里解冲突、`git -c core.editor=true rebase --continue`，交卷后再运行 `bl rebase`"}
    return _adopt_tester_rebase(ledger_path, onto, t["head"], gitx.head(wt), paths=overlap)


def _adopt_tester_rebase(ledger_path: Path, onto: str, from_head: str, new_head: str, paths: list[str] | None = None) -> dict[str, Any]:
    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["tester"]["base"] = onto
        lg2["tester"]["head"] = new_head
        ledger_mod.log_event(lg2, "tester_rebase", status="rebased", onto=onto, from_head=from_head, tester_head=new_head)
    return {"status": "rebased", "paths": paths or [], "tester_head": new_head}
