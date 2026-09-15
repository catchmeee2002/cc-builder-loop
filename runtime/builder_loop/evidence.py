"""evidence：四类判据结果与它们绑定的真实输入。

每条 evidence 记录 `dependency_digest` = 按 kind 定制的输入投影的 digest（不是输出哈希）。
stale 不落盘：每次用当前 ledger 重算投影，与记录不等即 stale。
readiness 每次从 evidence 派生下一步，ledger 不保存"下一步让谁做"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import gitx
from .jsonutil import digest
from .ledger import EVIDENCE_KINDS, now_iso

STATE_MISSING = "missing"
STATE_PASS = "pass"
STATE_FAIL = "fail"
STATE_STALE = "stale"


def _tester_files(ledger: dict[str, Any]) -> list[str]:
    rec = ledger["evidence"].get("tester")
    if not rec:
        return []
    return sorted(rec.get("details", {}).get("files", []))


def _blobs_at(repo_root: Path, commit: str | None, paths: list[str]) -> list[dict[str, Any]]:
    if not commit or not paths:
        return [{"path": p, "blob": None} for p in paths]
    blobs = gitx.ls_tree_blobs(repo_root, commit, paths)
    return [{"path": p, "blob": blobs.get(p)} for p in paths]


def projection(ledger: dict[str, Any], kind: str, repo_root: Path) -> dict[str, Any]:
    c = ledger["contract"]
    facets = c["digests"]
    cand = ledger["candidate"].get("head")
    tester_files = _blobs_at(repo_root, cand, _tester_files(ledger))
    if kind == "machine":
        return {"kind": kind, "facets": facets, "candidate_head": cand, "tester_files": tester_files}
    if kind == "tester":
        return {"kind": kind, "tester_files": tester_files, "mission": facets["mission"]}
    if kind == "proof":
        return {
            "kind": kind,
            "candidate_head": cand,
            "tester_files": tester_files,
            "behaviors": sorted(b["id"] for b in c["mission"]["behaviors"]),
            "proof_spec": digest(ledger.get("proof_spec")),
        }
    if kind == "reviewer":
        prereq = {}
        for k in ("machine", "tester", "proof"):
            rec = ledger["evidence"].get(k)
            prereq[k] = None if not rec else {"status": rec["status"], "dependency_digest": rec["dependency_digest"], "state": state(ledger, k, repo_root)}
        return {"kind": kind, "candidate_head": cand, "facets": facets, "prerequisites": prereq}
    raise ValueError(f"unknown evidence kind {kind}")


def dependency_digest(ledger: dict[str, Any], kind: str, repo_root: Path) -> str:
    return digest(projection(ledger, kind, repo_root))


def state(ledger: dict[str, Any], kind: str, repo_root: Path) -> str:
    rec = ledger["evidence"].get(kind)
    if not rec:
        return STATE_MISSING
    if rec.get("dependency_digest") != dependency_digest(ledger, kind, repo_root):
        return STATE_STALE
    return STATE_PASS if rec.get("status") == "pass" else STATE_FAIL


def record(ledger: dict[str, Any], kind: str, status: str, details: dict[str, Any], repo_root: Path, agent_id: str | None = None) -> dict[str, Any]:
    """在 mutate 上下文内调用。tester 的 files 需先写入 details 再算投影。"""
    rec = {
        "status": status,
        "at": now_iso(),
        "candidate_head": ledger["candidate"].get("head"),
        "agent_id": agent_id,
        "details": details,
        "dependency_digest": None,
    }
    ledger["evidence"][kind] = rec
    rec["dependency_digest"] = dependency_digest(ledger, kind, repo_root)
    return rec


# ---------------------------------------------------------------- readiness

ACTION_DONE = "done"
ACTION_CHECKPOINT = "checkpoint"
ACTION_MACHINE = "machine"
ACTION_SPAWN_TESTER = "spawn_tester"
ACTION_RESUME_TESTER = "resume_tester"
ACTION_PROOF = "proof"
ACTION_SPAWN_REVIEWER = "spawn_reviewer"
ACTION_RESUME_REVIEWER = "resume_reviewer"
ACTION_FINALIZE = "finalize"
ACTION_NEEDS_USER = "needs_user"


def readiness(ledger: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    required = ledger["contract"]["assurance"]["required"]
    states = {k: (state(ledger, k, repo_root) if k in required else "not_required") for k in EVIDENCE_KINDS}
    blockers: list[dict[str, Any]] = []
    if ledger.get("waiting_for_user"):
        blockers.append({"code": "WAITING_FOR_USER", **ledger["waiting_for_user"]})
    max_iter = ledger["contract"]["assurance"].get("max_iterations") or ledger["loop_config"]["max_iterations"]
    if ledger["counters"]["machine_iter"] >= max_iter and states["machine"] != STATE_PASS:
        blockers.append({"code": "MAX_ITERATIONS", "machine_iter": ledger["counters"]["machine_iter"], "max_iterations": max_iter})
    machine_failures = ledger["failures"]["machine"]
    if machine_failures and states["machine"] != STATE_PASS:
        last_sig = machine_failures[-1].get("signature")
        repeats = sum(1 for f in machine_failures if last_sig and f.get("signature") == last_sig)
        if repeats >= 3:
            blockers.append({"code": "NO_PROGRESS", "signature": last_sig, "repeats": repeats})
    proof_failures = ledger["failures"]["proof"]
    if proof_failures:
        last_sig = proof_failures[-1]["signature"]
        repeats = sum(1 for f in proof_failures if f["signature"] == last_sig)
        if repeats >= 3 and states["proof"] != STATE_PASS:
            blockers.append({"code": "PROOF_STALL", "signature": last_sig, "repeats": repeats})

    if ledger.get("terminal"):
        action = ACTION_DONE
    elif blockers:
        action = ACTION_NEEDS_USER
    elif not ledger["candidate"].get("head") or ledger["candidate"]["head"] == ledger["repo"]["target_start_head"]:
        action = ACTION_CHECKPOINT
    elif states["machine"] != STATE_PASS:
        action = ACTION_MACHINE
    elif "tester" in required and states["tester"] != STATE_PASS:
        action = ACTION_RESUME_TESTER if (ledger["agents"].get("tester") and states["tester"] != STATE_MISSING) else ACTION_SPAWN_TESTER
    elif "proof" in required and states["proof"] != STATE_PASS:
        proof_rec = ledger["evidence"].get("proof") or {}
        owner = ((proof_rec.get("details") or {}).get("failure") or {}).get("suggested_owner")
        action = ACTION_RESUME_TESTER if (states["proof"] == STATE_FAIL and owner == "tester" and ledger["agents"].get("tester")) else ACTION_PROOF
    elif "reviewer" in required and states["reviewer"] != STATE_PASS:
        action = ACTION_RESUME_REVIEWER if ledger["agents"].get("reviewer") else ACTION_SPAWN_REVIEWER
    else:
        action = ACTION_FINALIZE

    return {"required": required, "states": states, "next_action": action, "blockers": blockers}
