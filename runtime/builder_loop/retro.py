"""复盘（硬闸门）与清理。

run 到终态后 session 不解绑，`retrospective` 写进 ledger 之前 Stop hook 会拦住。runtime 只做两件事：
从 ledger 派生确定性信号（只读 ledger，不碰 git——abandon 的现场可能已经损坏），以及校验复盘记录
恰好覆盖全部信号。判断每个信号是不是事故、该去哪个仓库立项，是执行者和用户的事。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import gitx, ledger as ledger_mod, worktree
from .errors import fatal, negative

ROUTES = ("business_issue", "builder_loop_issue", "not_incident")


def _signal(sid: str, summary: str, **facts: Any) -> dict[str, Any]:
    return {"id": sid, "summary": summary, "facts": facts}


def derive_signals(lg: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ev = lambda *kinds: ledger_mod.events_of(lg, *kinds)  # noqa: E731
    term = lg.get("terminal") or {}

    machine_fails = lg["failures"]["machine"]
    if machine_fails:
        sigs = [f.get("signature") for f in machine_fails]
        worst = max((sigs.count(s) for s in set(sigs) if s), default=0)
        out.append(_signal("S-machine-failures", f"machine 失败 {len(machine_fails)} 次（共跑 {lg['counters']['machine_iter']} 次）", count=len(machine_fails), max_same_signature=worst, stages=sorted({str(f.get('stage')) for f in machine_fails})))
    proof_fails = lg["failures"]["proof"]
    if proof_fails:
        out.append(_signal("S-proof-failures", f"proof 失败 {len(proof_fails)} 次", codes=[f["code"] for f in proof_fails]))
    auths = lg.get("authorizations") or []
    if auths:
        out.append(_signal("S-user-resume", f"触发上限/无进展后经用户授权续跑 {len(auths)} 次", blockers=[a.get("blockers") for a in auths], reasons=[a.get("reason") for a in auths]))
    rejected = ev("checkpoint_rejected")
    if rejected:
        reasons = sorted({r["reason"] for e in rejected for r in e.get("rejected", [])})
        out.append(_signal("S-checkpoint-rejected", f"checkpoint 因越界被拒 {len(rejected)} 次", reasons=reasons, paths=sorted({r["path"] for e in rejected for r in e.get("rejected", [])})[:30]))
    history = (lg.get("contract") or {}).get("history") or []
    if history:
        out.append(_signal("S-contract-revised", f"run 内修订 contract {len(history)} 次", changes=[h.get("changes") for h in history]))
    malformed = ev("role_malformed")
    if malformed:
        out.append(_signal("S-role-malformed", f"角色结果不合规 {len(malformed)} 次", roles=sorted({e["role"] for e in malformed}), final=any(e.get("final") for e in malformed), reasons=[e.get("reason", "")[:160] for e in malformed][:5]))
    for role in ("tester", "reviewer"):
        starts = [e for e in ev("role_start") if e.get("role") == role]
        if len(starts) > 1:
            out.append(_signal(f"S-{role}-rounds", f"{role} 共 {len(starts)} 轮（被续接 {len(starts) - 1} 次）", turns=len(starts)))
    replaced = ev("role_replaced")
    if replaced:
        out.append(_signal("S-role-replaced", f"角色 agent 被重新 spawn 顶替 {len(replaced)} 次", roles=[e["role"] for e in replaced]))
    review_fail = [e for e in ev("role_result") if e.get("role") == "reviewer" and e.get("status") == "fail"]
    if review_fail:
        out.append(_signal("S-review-rejected", f"reviewer 未通过 {len(review_fail)} 次", verdicts=[e.get("verdict") for e in review_fail], candidate_moved=any(e.get("candidate_moved") for e in review_fail)))
    rebases = [cp for cp in lg["candidate"]["checkpoints"] if cp.get("role") == "rebase"]
    if rebases:
        out.append(_signal("S-rebase", f"目标分支前进，候选 rebase {len(rebases)} 次"))
    stalls = ev("stall_escape")
    if stalls:
        out.append(_signal("S-stall-escape", f"Stop hook 因连续无进展放行 {len(stalls)} 次"))
    if term.get("status") == "abandoned":
        out.append(_signal("S-abandoned", "run 被放弃", reason=term.get("reason")))
    if term.get("status") == "finalize_failed":
        out.append(_signal("S-finalize-failed", "finalize 写回失败且未能回滚", reason=term.get("reason")))
    return out


def signals(ledger_path: Path) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if not ledger_mod.is_terminal(lg):
        raise negative("RUN_NOT_TERMINAL", "run 还没结束，复盘在 finalize / abandon 之后做")
    sigs = derive_signals(lg)
    return {
        "run_id": lg["run_id"], "terminal": lg["terminal"], "runtime_identity": lg.get("runtime_identity"),
        "recorded": bool(lg.get("retrospective")), "signals": sigs,
        "how_to_record": {
            "file": {"dispositions": [{"signal_id": "<id>", "route": "|".join(ROUTES), "reason": "not_incident 必填", "issue_url": "issue 路由必填（或 declined_by_user: true）"}], "observations": [{"summary": "信号之外你自己遇到的问题", "route": "同上", "issue_url": ""}]},
            "zero_signals": "没有信号也没有观察到问题时：bl retro record --no-incident",
        },
    }


def _check_entry(entry: dict[str, Any], where: str) -> None:
    route = entry.get("route")
    if route not in ROUTES:
        raise fatal("RETRO_INVALID", f"{where}.route 必须是 {ROUTES}", got=route)
    if route == "not_incident":
        if not str(entry.get("reason", "")).strip():
            raise fatal("RETRO_INVALID", f"{where}: not_incident 必须写 reason")
    elif not (str(entry.get("issue_url", "")).strip() or entry.get("declined_by_user") is True):
        raise fatal("RETRO_INVALID", f"{where}: issue 路由需要 issue_url（先用 file-issue 立项），或用户明确不立项时 declined_by_user: true")


def record(ledger_path: Path, payload: dict[str, Any] | None, *, no_incident: bool = False) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if not ledger_mod.is_terminal(lg):
        raise negative("RUN_NOT_TERMINAL", "run 还没结束")
    expected = [s["id"] for s in derive_signals(lg)]
    if no_incident:
        if expected:
            raise negative("RETRO_SIGNALS_PRESENT", "存在信号，不能用 --no-incident；请逐条给出去向", signals=expected)
        payload = {"dispositions": [], "observations": []}
    if not isinstance(payload, dict):
        raise fatal("RETRO_INVALID", "复盘记录必须是 JSON 对象")
    disp = payload.get("dispositions") or []
    obs = payload.get("observations") or []
    got = [d.get("signal_id") for d in disp]
    if sorted(got) != sorted(expected) or len(got) != len(set(got)):
        raise negative("RETRO_COVERAGE", "dispositions 必须恰好覆盖全部信号", missing=sorted(set(expected) - set(got)), unknown=sorted(set(got) - set(expected)))
    for i, d in enumerate(disp):
        _check_entry(d, f"dispositions[{i}]")
    for i, o in enumerate(obs):
        if not str(o.get("summary", "")).strip():
            raise fatal("RETRO_INVALID", f"observations[{i}].summary 不能为空")
        _check_entry(o, f"observations[{i}]")

    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["retrospective"] = {"at": ledger_mod.now_iso(), "dispositions": disp, "observations": obs}
        ledger_mod.log_event(lg2, "retro", signals=len(disp), observations=len(obs))
        sid = lg2["session"].get("owner_session_id")
    if sid:
        ledger_mod.unbind_session(sid)
    issues = [e["issue_url"] for e in disp + obs if e.get("issue_url")]
    return {"run_id": lg["run_id"], "recorded": True, "signals": len(disp), "observations": len(obs), "issues": issues}


# ---------------------------------------------------------------- cleanup


def cleanup(repo_root: Path, run_id: str | None = None) -> dict[str, Any]:
    """回收 abandoned / finalize_failed 且已复盘的 run 留下的 worktree 与分支。
    只删 clean 且 HEAD 与 ledger 记录一致的；其余原样保留并说明原因。"""
    results: list[dict[str, Any]] = []
    for item in ledger_mod.list_runs(repo_root):
        if run_id and item["run_id"] != run_id:
            continue
        if item.get("legacy") or item.get("unreadable") or not item.get("terminal"):
            if run_id:
                results.append({"run_id": item["run_id"], "skipped": "不是可清理的终态 run（旧版 / 不可读 / 未结束）"})
            continue
        lg = ledger_mod.load(Path(item["ledger_path"]))
        if ledger_mod.needs_retro(lg):
            results.append({"run_id": lg["run_id"], "skipped": "尚未复盘"})
            continue
        entry: dict[str, Any] = {"run_id": lg["run_id"], "removed": [], "kept": []}
        for key in ("candidate", "tester"):
            info = lg.get(key)
            if not info or not info.get("worktree") or not Path(info["worktree"]).exists():
                continue
            wt = Path(info["worktree"])
            dirty = worktree.residue(wt)
            head = gitx.head(wt)
            if dirty or head != info.get("head"):
                entry["kept"].append({"worktree": str(wt), "reason": "有未提交改动" if dirty else "HEAD 与 ledger 不一致"})
                continue
            worktree.remove_worktree(repo_root, wt, info.get("branch"), delete_branch=True)
            entry["removed"].append(str(wt))
            try:
                wt.parent.rmdir()
            except OSError:
                pass
        if entry["removed"] or entry["kept"] or run_id:
            results.append(entry)
    return {"cleaned": results}
