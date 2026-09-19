"""evidence：四类判据结果与它们绑定的真实输入。

每条 evidence 记录 `dependency_digest` = 按 kind 定制的输入投影的 digest（不是输出哈希）。
stale 不落盘：每次用当前 ledger + git 重算投影，与记录不等即 stale。
readiness 每次从 evidence 派生下一步，ledger 不保存"下一步让谁做"，也不保存"角色是否在跑"。

tester 的测试文件集合从 git 派生：`tester.base..tester.head` 的差集（含删除）。
「候选是否已吸收 tester 的最新测试」同样从 git 派生（blob 比对），不另存 integrated_head。
"""

from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import gitx
from .errors import negative
from .jsonutil import digest
from .ledger import EVIDENCE_KINDS, events_of, now_iso, run_dir

STATE_MISSING = "missing"
STATE_PASS = "pass"
STATE_FAIL = "fail"
STATE_STALE = "stale"

ROLE_LEASE_SECONDS = 40 * 60
GATE_MACHINE, GATE_PROOF, GATE_PREFLIGHT = "machine", "proof", "preflight"
GATES = (GATE_MACHINE, GATE_PROOF, GATE_PREFLIGHT)  # machine / proof 排在前：readiness 只拿它俩与 next_action 对齐
NO_PROGRESS_REPEATS = 3
PROOF_STALL_REPEATS = 3


# ---------------------------------------------------------------- tester 文件（git 派生）


def tester_files(ledger: dict[str, Any], repo_root: Path) -> dict[str, list[str]]:
    t = ledger.get("tester")
    if not t or not t.get("head") or t["head"] == t["base"]:
        return {"present": [], "deleted": []}
    r = gitx.git(repo_root, "diff", "--name-status", "--no-renames", "-z", f"{t['base']}..{t['head']}")
    parts = [p for p in r.stdout.split("\0") if p]
    present, deleted = [], []
    for status, path in zip(parts[0::2], parts[1::2]):
        (deleted if status.startswith("D") else present).append(path)
    return {"present": sorted(present), "deleted": sorted(deleted)}


def _blobs(repo_root: Path, commit: str | None, paths: list[str]) -> dict[str, str | None]:
    if not commit or not paths:
        return {p: None for p in paths}
    found = gitx.ls_tree_blobs(repo_root, commit, paths)
    return {p: found.get(p) for p in paths}


def tester_blobs(ledger: dict[str, Any], repo_root: Path) -> list[dict[str, Any]]:
    """tester 分支上的测试内容（删除的文件 blob 为 None）。"""
    t = ledger.get("tester")
    files = tester_files(ledger, repo_root)
    paths = files["present"] + files["deleted"]
    blobs = _blobs(repo_root, t["head"] if t else None, files["present"])
    return [{"path": p, "blob": blobs.get(p)} for p in sorted(paths)]


def needs_integrate(ledger: dict[str, Any], repo_root: Path) -> bool:
    """候选上的 tester 文件与 tester 分支不一致（含：tester 删了而候选还在）。"""
    t = ledger.get("tester")
    if not t:
        return False
    files = tester_files(ledger, repo_root)
    paths = files["present"] + files["deleted"]
    if not paths:
        return False
    want = _blobs(repo_root, t["head"], files["present"])
    want.update({p: None for p in files["deleted"]})
    have = _blobs(repo_root, ledger["candidate"].get("head"), paths)
    return any(want.get(p) != have.get(p) for p in paths)


# ---------------------------------------------------------------- 投影 / 状态


def projection(ledger: dict[str, Any], kind: str, repo_root: Path) -> dict[str, Any]:
    c = ledger["contract"]
    facets = c["digests"]
    cand = ledger["candidate"].get("head")
    if kind == "machine":
        return {"kind": kind, "facets": facets, "candidate_head": cand}
    if kind == "tester":
        return {"kind": kind, "tester_files": tester_blobs(ledger, repo_root), "mission": facets["mission"]}
    if kind == "proof":
        return {
            "kind": kind,
            "candidate_head": cand,
            "tester_files": tester_blobs(ledger, repo_root),
            "behaviors": sorted(b["id"] for b in c["mission"]["behaviors"]),
            "proof_spec": digest(ledger.get("proof_spec")),
            "assurance": facets["assurance"],
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


def input_changes(before: dict[str, Any], after: dict[str, Any], kind: str, repo_root: Path) -> list[str]:
    """门禁起跑与收尾时，同一 kind 的输入投影里哪些顶层键变了（升序）；空 = 结论对应的输入没变。
    machine / proof 共用这一条判据：观察期间输入变了，结论就对应不到任何确定输入，不能记成 evidence。"""
    a, b = projection(before, kind, repo_root), projection(after, kind, repo_root)
    return sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def state(ledger: dict[str, Any], kind: str, repo_root: Path) -> str:
    rec = ledger["evidence"].get(kind)
    if not rec:
        return STATE_MISSING
    if rec.get("dependency_digest") != dependency_digest(ledger, kind, repo_root):
        return STATE_STALE
    return STATE_PASS if rec.get("status") == "pass" else STATE_FAIL


def record(ledger: dict[str, Any], kind: str, status: str, details: dict[str, Any], repo_root: Path, agent_id: str | None = None) -> dict[str, Any]:
    """在 mutate 上下文内调用；投影依赖的 ledger 字段（tester.head、proof_spec 等）需先写好。"""
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


# ---------------------------------------------------------------- 角色是否在跑（派生，不落盘）


def _heartbeat_path(ledger: dict[str, Any], role: str) -> Path:
    return run_dir(Path(ledger["repo"]["root"]), ledger["run_id"]) / f"heartbeat-{role}"


def touch_heartbeat(ledger: dict[str, Any], role: str) -> None:
    try:
        p = _heartbeat_path(ledger, role)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch()
    except OSError:
        pass


def role_running(ledger: dict[str, Any], role: str) -> bool:
    """最近一条生命周期事件是 start（或要求重发的 malformed）且租约未过期。只用于抑制 Stop 回拉。"""
    life = [e for e in events_of(ledger, "role_start", "role_result", "role_malformed") if e.get("role") == role]
    if not life:
        return False
    last = life[-1]
    if last["kind"] == "role_result" or (last["kind"] == "role_malformed" and last.get("final")):
        return False
    try:
        from datetime import datetime

        beat = datetime.fromisoformat(last["at"]).timestamp()
    except ValueError:
        beat = 0.0
    hb = _heartbeat_path(ledger, role)
    if hb.exists():
        beat = max(beat, hb.stat().st_mtime)
    return (time.time() - beat) < ROLE_LEASE_SECONDS


# ---------------------------------------------------------------- 门禁是否在跑（派生，不落盘）


def _gate_path(ledger: dict[str, Any], holder: str) -> Path:
    return run_dir(Path(ledger["repo"]["root"]), ledger["run_id"]) / f"gate-{holder}.lock"


def _held_by_other(p: Path) -> bool:
    """p 上的 flock 是否被别的进程持有。本进程自己持有不算——machine 跑完要在锁内算 readiness。"""
    if not p.exists():
        return False
    try:
        fd = os.open(str(p), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                return int(p.read_text(encoding="utf-8").strip() or -1) != os.getpid()
            except (OSError, ValueError):
                return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def gate_running(ledger: dict[str, Any]) -> str | None:
    """哪个门禁正在这个 run 上执行。进程死掉锁自动释放，无残留状态可清理。"""
    for holder in GATES:
        if _held_by_other(_gate_path(ledger, holder)):
            return holder
    return None


@contextmanager
def gate_lock(ledger: dict[str, Any], holder: str) -> Iterator[None]:
    """门禁执行期间持有。同一门禁重复启动 → GATE_BUSY；基线预跑在跑就排队等它——
    两套全量测试并发会把彼此挤成假超时。等待期间本门禁的锁已持有，所以 Stop 看得到它在排队。"""
    p = _gate_path(ledger, holder)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise negative("GATE_BUSY", f"`bl {holder}` 已经在这个 run 上执行，等它结束（后台任务完成时你会被唤醒）", holder=holder) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        if holder != GATE_PREFLIGHT:
            _wait_released(_gate_path(ledger, GATE_PREFLIGHT))
        yield
    finally:
        os.close(fd)  # 关闭即释放 flock


def _wait_released(p: Path) -> None:
    if not p.exists():
        return
    try:
        fd = os.open(str(p), os.O_RDWR)
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def last_role_result_at(ledger: dict[str, Any], role: str) -> str:
    res = [e for e in events_of(ledger, "role_result") if e.get("role") == role]
    return res[-1]["at"] if res else ""


# ---------------------------------------------------------------- blockers（按最近一次用户授权以来的窗口算）


def _window(ledger: dict[str, Any]) -> dict[str, int]:
    auths = ledger.get("authorizations") or []
    if not auths:
        return {"machine_iter_at": 0, "machine_failures_index": 0, "proof_failures_index": 0}
    return auths[-1]


def blockers(ledger: dict[str, Any], states: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if ledger.get("waiting_for_user"):
        out.append({"code": "WAITING_FOR_USER", **ledger["waiting_for_user"]})
    win = _window(ledger)
    max_iter = ledger["contract"]["assurance"].get("max_iterations") or ledger["loop_config"]["max_iterations"]
    if states.get("machine") != STATE_PASS:
        used = ledger["counters"]["machine_iter"] - win["machine_iter_at"]
        if used >= max_iter:
            out.append({"code": "MAX_ITERATIONS", "used_in_window": used, "max_iterations": max_iter})
        fails = ledger["failures"]["machine"][win["machine_failures_index"]:]
        if fails:
            sig = fails[-1].get("signature")
            repeats = sum(1 for f in fails if sig and f.get("signature") == sig)
            if repeats >= NO_PROGRESS_REPEATS:
                out.append({"code": "NO_PROGRESS", "signature": sig, "repeats": repeats})
    if states.get("proof") not in (STATE_PASS, "not_required"):
        fails = ledger["failures"]["proof"][win["proof_failures_index"]:]
        if fails:
            sig = fails[-1]["signature"]
            repeats = sum(1 for f in fails if f["signature"] == sig)
            if repeats >= PROOF_STALL_REPEATS:
                out.append({"code": "PROOF_STALL", "signature": sig, "repeats": repeats})
    if states.get("reviewer") == STATE_FAIL:
        findings = (ledger["evidence"]["reviewer"].get("details") or {}).get("findings", [])
        contract_owned = [f for f in findings if f.get("owner") == "contract" and f.get("severity") in ("blocking", "major")]
        if contract_owned:
            out.append({"code": "REVIEW_CONTRACT", "findings": contract_owned})
    return out


def machine_blocked(ledger: dict[str, Any], repo_root: Path) -> list[dict[str, Any]]:
    states = {"machine": state(ledger, "machine", repo_root)}
    return [b for b in blockers(ledger, states) if b["code"] in ("MAX_ITERATIONS", "NO_PROGRESS")]


def proof_blocked(ledger: dict[str, Any], repo_root: Path) -> list[dict[str, Any]]:
    states = {"machine": STATE_PASS, "proof": state(ledger, "proof", repo_root)}
    return [b for b in blockers(ledger, states) if b["code"] == "PROOF_STALL"]


# ---------------------------------------------------------------- readiness

ACTION_DONE = "done"
ACTION_RETRO = "retro"
ACTION_CHECKPOINT = "checkpoint"
ACTION_INTEGRATE = "integrate"
ACTION_MACHINE = "machine"
ACTION_SPAWN_TESTER = "spawn_tester"
ACTION_RESUME_TESTER = "resume_tester"
ACTION_AWAITING_TESTER = "awaiting_tester"
ACTION_PROOF = "proof"
ACTION_SPAWN_REVIEWER = "spawn_reviewer"
ACTION_RESUME_REVIEWER = "resume_reviewer"
ACTION_AWAITING_REVIEWER = "awaiting_reviewer"
ACTION_AWAITING_GATE = "awaiting_gate"
ACTION_FINALIZE = "finalize"
ACTION_NEEDS_USER = "needs_user"
AWAITING_ACTIONS = (ACTION_AWAITING_TESTER, ACTION_AWAITING_REVIEWER, ACTION_AWAITING_GATE)


def missing_patch(ledger: dict[str, Any]) -> bool:
    groups = (ledger.get("proof_spec") or {}).get("groups", [])
    return any(g.get("kind") == "mutation" and not (g.get("patch") or "").strip() for g in groups)


def _first_integrate_at(ledger: dict[str, Any]) -> str:
    stamps = [cp["at"] for cp in ledger["candidate"]["checkpoints"] if cp.get("role") == "integrate"]
    return stamps[0] if stamps else ""


def implementation_readable_by_tester(ledger: dict[str, Any]) -> bool:
    """首次 integrate 之后 tester 的读隔离解除（它要读候选才能写 mutation patch）。"""
    return bool(_first_integrate_at(ledger))


def _tester_owned_findings(ledger: dict[str, Any]) -> bool:
    findings = ((ledger["evidence"].get("reviewer") or {}).get("details") or {}).get("findings", [])
    return any(f.get("owner") == "tester" and f.get("severity") in ("blocking", "major") for f in findings)


def readiness(ledger: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    required = ledger["contract"]["assurance"]["required"]
    states = {k: (state(ledger, k, repo_root) if k in required else "not_required") for k in EVIDENCE_KINDS}
    found = blockers(ledger, states)
    has_builder_cp = any(cp.get("role") == "builder" for cp in ledger["candidate"]["checkpoints"])
    tester_run = "tester" in required and role_running(ledger, "tester")
    tester_known = bool(ledger["agents"].get("tester"))
    integrate_needed = "tester" in required and states["tester"] == STATE_PASS and needs_integrate(ledger, repo_root)

    def tester_action() -> str:
        if tester_run:
            return ACTION_AWAITING_TESTER
        return ACTION_RESUME_TESTER if tester_known else ACTION_SPAWN_TESTER

    if ledger.get("terminal"):
        action = ACTION_DONE if ledger.get("retrospective") else ACTION_RETRO
    elif found:
        action = ACTION_NEEDS_USER
    elif "tester" in required and states["tester"] != STATE_PASS and not tester_run:
        action = tester_action()  # 先把 tester 放出去（后台），builder 再干自己的活
    elif not has_builder_cp:
        action = ACTION_CHECKPOINT
    elif tester_run:
        # 首轮并行写测试，或被续接去修测试 / 补 patch：它交卷之前没有别的事可推进
        action = ACTION_AWAITING_TESTER
    elif integrate_needed:
        action = ACTION_INTEGRATE
    elif states["machine"] != STATE_PASS:
        action = ACTION_MACHINE
    elif "proof" in required and states["proof"] != STATE_PASS:
        rec = ledger["evidence"].get("proof") or {}
        owner = ((rec.get("details") or {}).get("failure") or {}).get("suggested_owner")
        replied = last_role_result_at(ledger, "tester") > rec.get("at", "")
        if tester_run:
            action = ACTION_AWAITING_TESTER
        elif missing_patch(ledger) and last_role_result_at(ledger, "tester") <= _first_integrate_at(ledger):
            # 两段式：tester 首轮盲写时给不出 mutation patch；集成后实现可读了，续接它补上
            action = ACTION_RESUME_TESTER
        elif states["proof"] == STATE_FAIL and owner == "tester" and not replied:
            action = ACTION_RESUME_TESTER
        else:
            action = ACTION_PROOF
    elif "reviewer" in required and states["reviewer"] != STATE_PASS:
        rec = ledger["evidence"].get("reviewer") or {}
        if role_running(ledger, "reviewer"):
            action = ACTION_AWAITING_REVIEWER
        elif tester_run:
            action = ACTION_AWAITING_TESTER
        elif states["reviewer"] == STATE_FAIL and _tester_owned_findings(ledger) and last_role_result_at(ledger, "tester") <= rec.get("at", ""):
            action = ACTION_RESUME_TESTER
        else:
            action = ACTION_RESUME_REVIEWER if ledger["agents"].get("reviewer") else ACTION_SPAWN_REVIEWER
    else:
        action = ACTION_FINALIZE

    gate = gate_running(ledger)
    if gate == action:  # 要跑的门禁已经在跑（多半是后台 Bash）：等它，别再催一遍（#242）
        action = ACTION_AWAITING_GATE

    return {"required": required, "states": states, "next_action": action, "blockers": found,
            "integrate_needed": integrate_needed, "gate_running": gate}
