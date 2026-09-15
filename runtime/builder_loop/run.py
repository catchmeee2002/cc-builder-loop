"""run 生命周期：start / status / checkpoint / abandon / contract validate|revise。"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import contract as contract_mod
from . import evidence, gitx, ledger as ledger_mod, worktree
from .config import load_loop_config
from .errors import fatal, needs_user, negative

ROLE_BUILDER = "builder"
ROLE_TESTER = "tester"


def resolve_repo_root(repo_arg: str | None) -> Path:
    base = Path(repo_arg).resolve() if repo_arg else Path.cwd()
    root = gitx.main_repo_root(gitx.toplevel(base))
    return root


def _run_id(slug: str) -> str:
    return f"{slug}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


# ---------------------------------------------------------------- start


def start(repo_root: Path, plan_path: Path, session_id: str, target_branch: str | None = None) -> dict[str, Any]:
    bound = ledger_mod.lookup_session(session_id)
    if bound and not ledger_mod.is_terminal(bound["ledger"]):
        raise needs_user("RUN_ALREADY_ACTIVE", "当前 session 已绑定一个未完成的 run；先 finalize 或 abandon", run_id=bound["ledger"]["run_id"])

    loop_config = load_loop_config(repo_root)
    contract = contract_mod.parse_contract_file(plan_path)
    branch = target_branch or contract["authority"].get("target_branch") or gitx.current_branch(repo_root)
    if not branch:
        raise fatal("TARGET_BRANCH_UNKNOWN", "无法确定目标分支（detached HEAD 且 contract 未指定 target_branch）")
    if not gitx.branch_exists(repo_root, branch):
        raise fatal("TARGET_BRANCH_MISSING", f"目标分支不存在: {branch}", branch=branch)
    contract["authority"]["target_branch"] = branch
    frozen = contract_mod.freeze_assurance(contract, loop_config)

    target_head = gitx.branch_head(repo_root, branch)
    run_id = _run_id(frozen["mission"]["slug"])
    lpath = ledger_mod.ledger_path(repo_root, run_id)
    wt_path, wt_branch = worktree.create_candidate(repo_root, run_id, target_head, loop_config.worktree_root)

    lg = ledger_mod.new_ledger(
        run_id=run_id,
        session={"owner_session_id": session_id},
        repo={"root": str(repo_root), "target_branch": branch, "target_start_head": target_head},
        candidate={"branch": wt_branch, "worktree": str(wt_path), "head": target_head, "checkpoints": []},
        contract=contract_mod.contract_record(frozen, plan_path),
        loop_config=loop_config.to_json(),
    )
    ledger_mod.create(lpath, lg)
    ledger_mod.bind_session(session_id, repo_root, run_id)
    return {
        "run_id": run_id,
        "ledger": str(lpath),
        "worktree": str(wt_path),
        "candidate_branch": wt_branch,
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
    return {
        "run_id": lg["run_id"],
        "seq": lg["seq"],
        "terminal": lg.get("terminal"),
        "target": lg["repo"],
        "candidate": {"branch": cand["branch"], "worktree": cand["worktree"], "head": cand["head"], "dirty": dirty, "checkpoints": len(cand["checkpoints"])},
        "agents": lg["agents"],
        "counters": lg["counters"],
        "waiting_for_user": lg.get("waiting_for_user"),
        "readiness": evidence.readiness(lg, repo_root),
    }


# ---------------------------------------------------------------- checkpoint


def _classify_paths(lg: dict[str, Any], role: str, paths: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    auth = lg["contract"]["authority"]
    allowed_patterns = auth["builder_write"] if role == ROLE_BUILDER else auth["tester_write"]
    forbidden_patterns = auth["tester_write"] if role == ROLE_BUILDER else auth["builder_write"]
    accepted, rejected = [], []
    for p in paths:
        if contract_mod.path_in(auth.get("protected_paths", []), p):
            rejected.append({"path": p, "reason": "protected"})
        elif role == ROLE_BUILDER and contract_mod.path_in(auth["tester_write"], p) and not contract_mod.path_in(auth["builder_write"], p):
            rejected.append({"path": p, "reason": "tester_owned"})
        elif role == ROLE_TESTER and contract_mod.path_in(forbidden_patterns, p) and not contract_mod.path_in(allowed_patterns, p):
            rejected.append({"path": p, "reason": "builder_owned"})
        elif not contract_mod.path_in(allowed_patterns, p):
            rejected.append({"path": p, "reason": "outside_authority"})
        else:
            accepted.append(p)
    return accepted, rejected


def checkpoint(ledger_path: Path, repo_root: Path, role: str, message: str | None = None) -> dict[str, Any]:
    if role not in (ROLE_BUILDER, ROLE_TESTER):
        raise fatal("ROLE_INVALID", f"未知角色 {role}")
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    cand = lg["candidate"]
    wt = Path(cand["worktree"])
    worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])

    paths = worktree.residue(wt)
    if not paths:
        return {"noop": True, "head": cand["head"], "committed_paths": [], "rejected": []}
    accepted, rejected = _classify_paths(lg, role, paths)
    if rejected:
        raise negative("CHECKPOINT_REJECTED", f"{role} 的改动越界，未提交任何文件", rejected=rejected, accepted=accepted)

    n = len(cand["checkpoints"]) + 1
    msg = message or f"chore(builder-loop): [cr_id_skip] Checkpoint {role} {n}"
    gitx.git(wt, "add", "-A", "--", *accepted)
    gitx.git(wt, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", msg)
    head = gitx.head(wt)
    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["candidate"]["head"] = head
        lg2["candidate"]["checkpoints"].append({"head": head, "at": ledger_mod.now_iso(), "role": role, "paths": accepted})
        readiness = evidence.readiness(lg2, repo_root)
    return {"noop": False, "head": head, "committed_paths": accepted, "rejected": [], "readiness": readiness}


# ---------------------------------------------------------------- abandon


def abandon(ledger_path: Path, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise fatal("REASON_REQUIRED", "abandon 必须给出 --reason")
    with ledger_mod.mutate(ledger_path) as lg:
        if ledger_mod.is_terminal(lg):
            raise fatal("RUN_TERMINAL", "run 已到终态，不能重复 abandon", terminal=lg["terminal"])
        lg["terminal"] = {"status": "abandoned", "reason": reason, "at": ledger_mod.now_iso(), "final_head": None}
        sid = lg["session"].get("owner_session_id")
        cand = lg["candidate"]
    if sid:
        ledger_mod.unbind_session(sid)
    return {"run_id": lg["run_id"], "terminal": lg["terminal"], "worktree_kept": cand.get("worktree"), "branch_kept": cand.get("branch")}


# ---------------------------------------------------------------- contract


def contract_validate(plan_path: Path) -> dict[str, Any]:
    c = contract_mod.parse_contract_file(plan_path)
    return {"valid": True, "slug": c["mission"]["slug"], "behaviors": [b["id"] for b in c["mission"]["behaviors"]], "required": c["assurance"]["required"], "digests": contract_mod.facet_digests(c)}


def contract_revise(ledger_path: Path, repo_root: Path, plan_path: Path, authorize: bool) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    old = {k: lg["contract"][k] for k in contract_mod.FACETS}
    new = contract_mod.parse_contract_file(plan_path)
    new["authority"]["target_branch"] = lg["repo"]["target_branch"]
    new = contract_mod.freeze_assurance(new, load_loop_config(repo_root))
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
