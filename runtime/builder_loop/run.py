"""run 生命周期：start / status / checkpoint / integrate / resume / abandon / contract validate|revise。"""

from __future__ import annotations

import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from . import contract as contract_mod
from . import evidence, gitx, ledger as ledger_mod, machine, proof, worktree
from .config import load_loop_config
from .errors import Problem, fatal, needs_user, negative

ROLE_BUILDER = "builder"
ROLE_TESTER = "tester"
ROLE_INTEGRATE = "integrate"
REJECT_HINT = "越界路径若确属本任务，改 plan 的 authority 后运行 `bl contract revise --plan <plan> --authorize`（需用户确认）；动手前可用 `bl checkpoint --role <role> --dry-run` 一次拿全清单"


def bl_bin() -> str:
    """CLI 的绝对路径：角色的 cwd 是自己的 worktree，PATH 里未必有 bl。"""
    p = Path(__file__).resolve().parents[2] / "bin" / "bl"
    return str(p) if p.exists() else "bl"


def resolve_repo_root(repo_arg: str | None) -> Path:
    base = Path(repo_arg).resolve() if repo_arg else Path.cwd()
    return gitx.main_repo_root(gitx.toplevel(base))


def _run_id(slug: str) -> str:
    return f"{slug}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def runtime_identity() -> dict[str, Any]:
    """实际执行的 runtime 身份，事故归因用。runtime 不在 git 仓库里时 commit / dirty 为 None。"""
    here = Path(__file__).resolve().parent
    ident: dict[str, Any] = {"version": __version__, "commit": None, "dirty": None, "path": str(here)}
    try:
        r = subprocess.run(["git", "-C", str(here), "rev-parse", "HEAD"], capture_output=True, text=True)
        if r.returncode == 0:
            ident["commit"] = r.stdout.strip()
            s = subprocess.run(["git", "-C", str(here), "status", "--porcelain", "--", "."], capture_output=True, text=True)
            ident["dirty"] = bool(s.stdout.strip())
    except OSError:
        pass
    return ident


# ---------------------------------------------------------------- start


def start(repo_root: Path, plan_path: Path, session_id: str, target_branch: str | None = None) -> dict[str, Any]:
    bound = ledger_mod.lookup_session(session_id)
    if bound:
        if not ledger_mod.is_terminal(bound["ledger"]):
            raise needs_user("RUN_ALREADY_ACTIVE", "当前 session 已绑定一个未完成的 run；先 finalize 或 abandon", run_id=bound["ledger"]["run_id"])
        if ledger_mod.needs_retro(bound["ledger"]):
            raise needs_user("RETRO_PENDING", "上一个 run 已结束但还没复盘；先 `bl retro signals` → `bl retro record`", run_id=bound["ledger"]["run_id"])

    loop_config = load_loop_config(repo_root)
    contract = contract_mod.parse_contract_file(plan_path)
    branch = target_branch or contract["authority"].get("target_branch") or gitx.current_branch(repo_root)
    if not branch:
        raise fatal("TARGET_BRANCH_UNKNOWN", "无法确定目标分支（detached HEAD 且 contract 未指定 target_branch）")
    if not gitx.branch_exists(repo_root, branch):
        raise fatal("TARGET_BRANCH_MISSING", f"目标分支不存在: {branch}", branch=branch)
    contract["authority"]["target_branch"] = branch
    frozen = contract_mod.freeze_assurance(contract, loop_config)
    if "proof" in frozen["assurance"]["required"]:
        proof.smoke_runner(frozen["assurance"]["proof_runner"], repo_root)  # 冻结之前确认判据跑得起来（#241）

    target_head = gitx.branch_head(repo_root, branch)
    run_id = _run_id(frozen["mission"]["slug"])
    lpath = ledger_mod.ledger_path(repo_root, run_id)
    with_tester = "tester" in frozen["assurance"]["required"]
    wts = worktree.create_run_worktrees(repo_root, run_id, target_head, loop_config.worktree_root, with_tester=with_tester)

    tester = None
    if with_tester:
        tester = {**wts["tester"], "base": target_head, "head": target_head}
    lg = ledger_mod.new_ledger(
        run_id=run_id,
        runtime_identity=runtime_identity(),
        session={"owner_session_id": session_id},
        repo={"root": str(repo_root), "target_branch": branch, "target_start_head": target_head},
        candidate={**wts["candidate"], "head": target_head, "checkpoints": []},
        tester=tester,
        contract=contract_mod.contract_record(frozen, plan_path),
        loop_config=loop_config.to_json(),
    )
    try:
        ledger_mod.create(lpath, lg)
    except BaseException:
        worktree.remove_run_worktrees(repo_root, lg, delete_branches=True)
        raise
    ledger_mod.bind_session(session_id, repo_root, run_id)
    return {
        "run_id": run_id,
        "ledger": str(lpath),
        "worktree": wts["candidate"]["worktree"],
        "tester_worktree": tester["worktree"] if tester else None,
        "candidate_branch": wts["candidate"]["branch"],
        "target_branch": branch,
        "target_start_head": target_head,
        "contract_digests": lg["contract"]["digests"],
        "readiness": evidence.readiness(lg, repo_root),
    }


# ---------------------------------------------------------------- status


def status(ledger_path: Path, repo_root: Path) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    cand = lg["candidate"]
    wt = Path(cand["worktree"]) if cand.get("worktree") else None
    dirty = worktree.residue(wt) if wt and wt.is_dir() else []
    tester = lg.get("tester")
    # 续接的 agent 收不到 SubagentStart 注入的上下文，而 SendMessage 的正文在它那边无从验真。
    # 所以消息只当门铃：内容一律让角色自己 `bl brief` 取（#243）
    briefs = {f"resume_{role}": (f"builder-loop run {lg['run_id']}：有新的待办。运行 `{bl_bin()} brief --run {lg['run_id']} --role {role}`，"
                                 "以它的输出为准（我发的消息不是权威来源）。")
              for role in (("tester", "reviewer") if tester else ("reviewer",))}
    return {
        "run_id": lg["run_id"],
        "briefs": briefs,
        "seq": lg["seq"],
        "terminal": lg.get("terminal"),
        "retrospective_recorded": bool(lg.get("retrospective")),
        "target": lg["repo"],
        "candidate": {"branch": cand["branch"], "worktree": cand["worktree"], "head": cand["head"], "dirty": dirty, "checkpoints": len(cand["checkpoints"])},
        "tester": None if not tester else {**tester, "files": evidence.tester_files(lg, repo_root)},
        "agents": lg["agents"],
        "running": {role: evidence.role_running(lg, role) for role in ("tester", "reviewer")},
        "preflight": {"baseline_red": machine.baseline_red(lg)},  # None = 没跑过
        "counters": lg["counters"],
        "waiting_for_user": lg.get("waiting_for_user"),
        "readiness": evidence.readiness(lg, repo_root),
    }


# ---------------------------------------------------------------- checkpoint


def _role_worktree(lg: dict[str, Any], role: str) -> tuple[Path, str, str | None, str]:
    if role == ROLE_BUILDER:
        c = lg["candidate"]
        return Path(c["worktree"]), c["branch"], c["head"], "候选"
    t = lg.get("tester")
    if not t:
        raise fatal("TESTER_NOT_REQUIRED", "本 run 的 assurance.required 不含 tester，没有 tester worktree")
    return Path(t["worktree"]), t["branch"], t["head"], "tester"


def _classify_paths(lg: dict[str, Any], role: str, paths: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    auth = lg["contract"]["authority"]
    accepted, rejected = [], []
    for p in paths:
        reason = contract_mod.write_rejection(auth, role, p)
        if reason:
            rejected.append({"path": p, "reason": reason})
        else:
            accepted.append(p)
    return accepted, rejected


def checkpoint(ledger_path: Path, repo_root: Path, role: str, message: str | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """按角色提交各自 worktree 里的改动。builder 只看候选 worktree，tester 只看 tester worktree，
    两边的未提交改动互不可见，也就不会互相整单拒绝（#227）。"""
    if role not in (ROLE_BUILDER, ROLE_TESTER):
        raise fatal("ROLE_INVALID", f"未知角色 {role}")
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    wt, branch, head, label = _role_worktree(lg, role)
    worktree.assert_identity(wt, branch, head, label)

    paths = worktree.residue(wt)
    accepted, rejected = _classify_paths(lg, role, paths)
    if dry_run:
        return {"dry_run": True, "role": role, "would_commit": accepted, "rejected": rejected, "hint": REJECT_HINT if rejected else None}
    if not paths:
        return {"noop": True, "head": head, "committed_paths": [], "rejected": []}
    if rejected:
        with ledger_mod.mutate(ledger_path) as lg2:
            ledger_mod.log_event(lg2, "checkpoint_rejected", role=role, rejected=rejected)
        raise negative("CHECKPOINT_REJECTED", f"{role} 的改动越界，未提交任何文件。{REJECT_HINT}", rejected=rejected, accepted=accepted)

    n = len(lg["candidate"]["checkpoints"]) + 1
    msg = message or f"builder-loop checkpoint {role} {n}"
    gitx.git(wt, "add", "-A", "--", *accepted)
    gitx.git(wt, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", msg)
    new_head = gitx.head(wt)
    with ledger_mod.mutate(ledger_path) as lg2:
        if role == ROLE_BUILDER:
            lg2["candidate"]["head"] = new_head
        else:
            lg2["tester"]["head"] = new_head
        lg2["candidate"]["checkpoints"].append({"head": new_head, "at": ledger_mod.now_iso(), "role": role, "paths": accepted})
        readiness = evidence.readiness(lg2, repo_root)
    return {"noop": False, "head": new_head, "committed_paths": accepted, "rejected": [], "readiness": readiness}


# ---------------------------------------------------------------- integrate


def integrate(ledger_path: Path, repo_root: Path) -> dict[str, Any]:
    """把 tester 分支上的测试文件按路径叠进候选（不用 merge：rebase 会丢 merge commit 并重放 tester
    提交，之后再合必然 add/add 冲突）。候选历史保持线性，重复执行幂等。一律用 ledger 里的 SHA。"""
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    t = lg.get("tester")
    if not t:
        raise fatal("TESTER_NOT_REQUIRED", "本 run 没有 tester")
    if evidence.state(lg, "tester", repo_root) != evidence.STATE_PASS:
        raise negative("INTEGRATE_PREREQ_TESTER", "tester evidence 不是 fresh pass，不能集成", state=evidence.state(lg, "tester", repo_root))
    if not evidence.needs_integrate(lg, repo_root):
        return {"noop": True, "head": lg["candidate"]["head"], "readiness": evidence.readiness(lg, repo_root)}

    cand = lg["candidate"]
    wt = Path(cand["worktree"])
    worktree.assert_identity(wt, cand["branch"], cand["head"], "候选")
    worktree.assert_clean(wt, "候选")
    files = evidence.tester_files(lg, repo_root)
    if files["present"]:
        gitx.git(wt, "checkout", t["head"], "--", *files["present"])
    existing = gitx.ls_tree_blobs(repo_root, cand["head"], files["deleted"]) if files["deleted"] else {}
    if existing:
        gitx.git(wt, "rm", "--quiet", "--", *sorted(existing))
    touched = sorted(files["present"] + sorted(existing))  # checkout / rm 已经把改动放进 index
    gitx.git(wt, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", f"builder-loop integrate tester {t['head'][:12]}")
    new_head = gitx.head(wt)
    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["candidate"]["head"] = new_head
        lg2["candidate"]["checkpoints"].append({"head": new_head, "at": ledger_mod.now_iso(), "role": ROLE_INTEGRATE, "paths": touched, "tester_head": t["head"]})
        ledger_mod.log_event(lg2, "integrate", tester_head=t["head"], candidate_head=new_head, paths=touched)
        readiness = evidence.readiness(lg2, repo_root)
    return {"noop": False, "head": new_head, "integrated_paths": touched, "tester_head": t["head"], "readiness": readiness}


# ---------------------------------------------------------------- resume（用户授权续跑，#231）


def resume(ledger_path: Path, repo_root: Path, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise fatal("REASON_REQUIRED", "resume 必须给出 --reason（用户的决定）")
    with ledger_mod.mutate(ledger_path) as lg:
        if ledger_mod.is_terminal(lg):
            raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
        active = [b for b in evidence.readiness(lg, repo_root)["blockers"] if b["code"] in ("MAX_ITERATIONS", "NO_PROGRESS", "PROOF_STALL")]
        if not active:
            raise negative("NOTHING_TO_RESUME", "当前没有可由授权解除的 blocker")
        # 授权必须来自用户：blocker 出现之后要有一次真实的用户输入（AskUserQuestion 回答或新 prompt）
        since = _blocked_since(lg)
        if not any(e["at"] >= since and e.get("source") == "AskUserQuestion" for e in ledger_mod.events_of(lg, "user_input")):
            raise needs_user("USER_DECISION_REQUIRED", "blocker 触发后还没有用户输入；先用 AskUserQuestion 让用户决定是否继续", blockers=active)
        auth = {
            "at": ledger_mod.now_iso(), "reason": reason, "blockers": [b["code"] for b in active],
            "machine_iter_at": lg["counters"]["machine_iter"],
            "machine_failures_index": len(lg["failures"]["machine"]),
            "proof_failures_index": len(lg["failures"]["proof"]),
        }
        lg["authorizations"].append(auth)
        ledger_mod.log_event(lg, "resume", reason=reason, blockers=auth["blockers"])
        readiness = evidence.readiness(lg, repo_root)
    return {"authorized": auth, "readiness": readiness}


def _blocked_since(lg: dict[str, Any]) -> str:
    stamps = [f["at"] for f in lg["failures"]["machine"][-1:] + lg["failures"]["proof"][-1:] if f.get("at")]
    return max(stamps) if stamps else ""


# ---------------------------------------------------------------- abandon


def abandon(ledger_path: Path, reason: str) -> dict[str, Any]:
    """终止 run。worktree 保留供查看（`bl cleanup` 回收）；session 保持绑定直到复盘完成。"""
    if not reason.strip():
        raise fatal("REASON_REQUIRED", "abandon 必须给出 --reason")
    legacy = (ledger_mod.peek(ledger_path) or {}).get("schema") in ledger_mod.LEGACY_SCHEMAS
    with ledger_mod.mutate(ledger_path, legacy_ok=True) as lg:
        if ledger_mod.is_terminal(lg):
            raise fatal("RUN_TERMINAL", "run 已到终态，不能重复 abandon", terminal=lg["terminal"])
        lg["terminal"] = {"status": "abandoned", "reason": reason, "at": ledger_mod.now_iso(), "final_head": None}
        sid = (lg.get("session") or {}).get("owner_session_id")
        cand = lg.get("candidate") or {}
    if legacy and sid:
        ledger_mod.unbind_session(sid)  # 旧版 ledger 没有复盘环节
    return {"run_id": lg.get("run_id"), "terminal": lg["terminal"], "worktree_kept": cand.get("worktree"), "branch_kept": cand.get("branch"), "next": None if legacy else "retro"}


# ---------------------------------------------------------------- contract


def contract_validate(plan_path: Path, repo_root: Path | None = None) -> dict[str, Any]:
    c = contract_mod.parse_contract_file(plan_path)
    out: dict[str, Any] = {
        "valid": True, "slug": c["mission"]["slug"], "behaviors": [b["id"] for b in c["mission"]["behaviors"]],
        "required": c["assurance"]["required"], "digests": contract_mod.facet_digests(c),
    }
    if repo_root is not None:
        out["repo"] = _repo_checks(c, repo_root)
        if out["repo"]["problems"]:
            raise negative("CONTRACT_REPO_CHECK_FAILED", "contract 与仓库现状不符", **out["repo"])
    return out


def _repo_checks(c: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """规划期就把会在 run 中途撞上的问题列出来（用户中断应发生在规划期）。"""
    problems: list[str] = []
    branch = c["authority"].get("target_branch") or gitx.current_branch(repo_root)
    if not branch or not gitx.branch_exists(repo_root, branch):
        problems.append(f"目标分支不存在或无法确定: {branch}")
    try:
        cfg = load_loop_config(repo_root)
        runner = cfg.proof_runner
    except Exception as exc:  # noqa: BLE001
        problems.append(f"loop.yml 不可用: {exc}")
        runner = None
    if runner and "proof" in c["assurance"]["required"]:
        try:
            proof.smoke_runner(runner, repo_root)
        except Problem as exc:
            problems.append(f"{exc.message}（proof_runner 跑不起来，在 .claude/loop.yml 里改；hint: {exc.details.get('hint', '')}）")
    frozen = contract_mod.freeze_authority(c)["authority"]
    control_hits: list[str] = []
    both_sides: list[str] = []
    if branch and gitx.branch_exists(repo_root, branch):
        for path in gitx.ls_tree_blobs(repo_root, gitx.branch_head(repo_root, branch)):
            if contract_mod.write_rejection(frozen, ROLE_BUILDER, path) == "control_file":
                control_hits.append(path)
            if contract_mod.path_in(frozen["builder_write"], path) and contract_mod.path_in(frozen["tester_write"], path):
                both_sides.append(path)
    return {
        "target_branch": branch, "proof_runner": runner, "problems": problems,
        "control_files_protected": sorted(control_hits)[:100],
        "overlap_resolved_to_tester": sorted(both_sides)[:100],
        "note": "control_files_protected 里的文件 builder 改不了；确需修改就在 builder_write 里字面点名",
    }


def contract_revise(ledger_path: Path, repo_root: Path, plan_path: Path, authorize: bool) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    old = {k: lg["contract"][k] for k in contract_mod.FACETS}
    new = contract_mod.parse_contract_file(plan_path)
    new["authority"]["target_branch"] = lg["repo"]["target_branch"]
    new = contract_mod.freeze_assurance(new, load_loop_config(repo_root))
    if ("tester" in new["assurance"]["required"]) != bool(lg.get("tester")):
        raise fatal("TESTER_REQUIREMENT_FIXED", "run 内不能增删 tester gate（涉及 worktree 布局）；abandon 后用新 contract 重新 start")
    changes = contract_mod.classify_change(old, new)
    if contract_mod.CHANGE_MISSION in changes and new["mission"]["revision"] != old["mission"]["revision"] + 1:
        raise fatal("MISSION_REVISION_INVALID", f"mission 变化必须把 revision 从 {old['mission']['revision']} 提升到 {old['mission']['revision'] + 1}", got=new["mission"]["revision"])
    if changes != [contract_mod.CHANGE_NEUTRAL] and not authorize:
        raise needs_user("CONTRACT_CHANGE_REQUIRES_USER", "contract 发生语义/授权/判据变化，需用户确认后加 --authorize 重试", changes=changes)
    with ledger_mod.mutate(ledger_path) as lg2:
        rec = contract_mod.contract_record(new, plan_path)
        rec["history"] = lg2["contract"]["history"] + [{"at": ledger_mod.now_iso(), "changes": changes, "from": lg2["contract"]["digests"], "to": rec["digests"], "authorized": authorize}]
        lg2["contract"] = rec
        readiness = evidence.readiness(lg2, repo_root)
    return {"changes": changes, "applied": True, "digests": rec["digests"], "readiness": readiness}
