"""machine evidence：在候选 worktree（必须 clean、HEAD == ledger 记录）顺序执行冻结的 pass_cmd。

三态：PASS（全部 stage 退出码 0）/ FAIL（某 stage 非 0 或超时）/ FATAL（判据没跑起来：配置问题）。
失败签名对日志做归一化（去 ANSI、时间戳、临时路径、长 hex），用于识别"同一个失败反复出现"。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from . import evidence, gitx, ledger as ledger_mod, worktree
from .errors import fatal, needs_user
from .jsonutil import dumps, sha256_bytes

NO_PROGRESS_REPEATS = 3

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HEX = re.compile(r"\b[0-9a-f]{7,64}\b")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds|m|min)\b")
_TMP = re.compile(r"/(?:tmp|var/folders|private/tmp)/[^\s'\"]+")
_ADDR = re.compile(r"0x[0-9a-fA-F]+")


def failure_signature(text: str, stage: str, returncode: int) -> str:
    norm = _ANSI.sub("", text)
    norm = _TS.sub("<ts>", norm)
    norm = _TMP.sub("<tmp>", norm)
    norm = _ADDR.sub("<addr>", norm)
    norm = _HEX.sub("<hex>", norm)
    norm = _DURATION.sub("<dur>", norm)
    norm = norm[-24000:]
    return sha256_bytes(f"{stage}\n{returncode}\n{norm}".encode("utf-8", "replace"))[:16]


def _run_stage(cwd: Path, stage: dict[str, Any], log_path: Path, env: dict[str, str]) -> tuple[int, str, bool]:
    timed_out = False
    with open(log_path, "wb") as log:
        try:
            proc = subprocess.run(
                ["bash", "-c", stage["cmd"]],
                cwd=str(cwd),
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=stage["timeout"],
                env=env,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = 124
            log.write(f"\n[TIMEOUT] stage={stage['stage']} 超过 {stage['timeout']}s\n".encode())
    text = log_path.read_bytes().decode("utf-8", "replace")
    return rc, text, timed_out


def commands_digest(stages: list[dict[str, Any]]) -> str:
    return sha256_bytes(dumps([[s["stage"], s["cmd"]] for s in stages]).encode())[:16]


def run_preflight(ledger_path: Path, repo_root: Path) -> dict[str, Any]:
    """在 run 起点的临时 worktree 上把冻结的 pass_cmd 跑一遍：哪些 stage 在基线上本来就红。

    不写 evidence、不计 machine_iter——它证明的是判据本身有效，不是候选达标（原则五）。
    基线红不拦 run：bug 修复类任务基线红是预期。只在 machine 失败时用来免掉一次白查（#241）。
    """
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    stages = lg["contract"]["assurance"].get("machine_commands") or []
    if not stages:
        raise fatal("MACHINE_NO_STAGES", "assurance.machine_commands 为空，判据未执行")

    log_dir = ledger_path.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"BUILDER_LOOP_RUN_ID": lg["run_id"], "BUILDER_LOOP_MAIN_REPO": str(repo_root), "BUILDER_LOOP_PREFLIGHT": "1"})

    results: list[dict[str, Any]] = []
    with evidence.gate_lock(lg, evidence.GATE_PREFLIGHT):
        base = Path(lg["repo"]["root"]).parent / f".bl-preflight-{lg['run_id']}"
        with worktree.temp_worktree(repo_root, lg["repo"]["target_start_head"], base.parent, base.name) as wt:
            env["BUILDER_LOOP_CANDIDATE"] = str(wt)
            for stage in stages:
                log_path = log_dir / f"preflight-{stage['stage']}.log"
                rc, _text, timed_out = _run_stage(wt, stage, log_path, env)
                results.append({"stage": stage["stage"], "returncode": rc, "timed_out": timed_out, "log": str(log_path)})

    red = [r["stage"] for r in results if r["returncode"] != 0]
    with ledger_mod.mutate(ledger_path) as lg2:
        ledger_mod.log_event(lg2, "preflight", stages=results, commands_digest=commands_digest(stages), baseline_red=red)
    return {"result": "RED" if red else "GREEN", "baseline_red": red, "stages": results,
            "note": "基线上就失败的 stage 与候选无关：要么改 .claude/loop.yml 后 `bl contract revise --authorize`，要么本次任务本就要修好它"}


def baseline_red(ledger: dict[str, Any]) -> list[str] | None:
    """与当前 machine_commands 对得上的那次基线预跑里，哪些 stage 是红的；没跑过 → None。"""
    stages = ledger["contract"]["assurance"].get("machine_commands") or []
    want = commands_digest(stages)
    for ev in reversed(ledger_mod.events_of(ledger, "preflight")):
        if ev.get("commands_digest") == want:
            return list(ev.get("baseline_red") or [])
    return None


def run_machine(ledger_path: Path, repo_root: Path) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    cand = lg["candidate"]
    if not cand.get("head") or not cand.get("checkpoints"):
        raise fatal("CANDIDATE_NOT_CHECKPOINTED", "候选还没有 checkpoint，先 `checkpoint --role builder`")
    blocked = evidence.machine_blocked(lg, repo_root)
    if blocked:
        # 上限是真的停止点：越过它需要一次被记录的用户决定（bl resume），不是再跑一次
        raise needs_user("MACHINE_BLOCKED", "已触发迭代上限或无进展；用 AskUserQuestion 让用户决定，继续则 `bl resume --reason`", blockers=blocked)
    wt = Path(cand["worktree"])
    worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])
    worktree.assert_candidate_clean(wt)

    stages = lg["contract"]["assurance"].get("machine_commands") or []
    if not stages:
        raise fatal("MACHINE_NO_STAGES", "assurance.machine_commands 为空，判据未执行")

    run_dir = ledger_path.parent
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    it = int(lg["counters"]["machine_iter"]) + 1
    env = dict(os.environ)
    env.update({
        "BUILDER_LOOP_RUN_ID": lg["run_id"],
        "BUILDER_LOOP_CANDIDATE": str(wt),
        "BUILDER_LOOP_MAIN_REPO": str(repo_root),
        "BUILDER_LOOP_ITER": str(it),
    })

    results: list[dict[str, Any]] = []
    failed: dict[str, Any] | None = None
    with evidence.gate_lock(lg, evidence.GATE_MACHINE):
        for stage in stages:
            log_path = log_dir / f"iter-{it}-{stage['stage']}.log"
            rc, text, timed_out = _run_stage(wt, stage, log_path, env)
            entry = {"stage": stage["stage"], "returncode": rc, "log": str(log_path), "timed_out": timed_out}
            results.append(entry)
            if rc != 0:
                files = evidence.tester_files(lg, repo_root)["present"]
                mentioned = sorted(f for f in files if f in text)
                failed = dict(entry, signature=failure_signature(text, stage["stage"], rc), tail=text[-4000:], tester_files_mentioned=mentioned)
                break

    red = baseline_red(lg)
    if failed:
        if red is None:
            failed["baseline"] = "未做基线预跑：`bl preflight` 可确认这一段是不是本来就红（后台跑，不占你的时间）"
        elif failed.get("stage") in red:
            failed["baseline_red"] = True
            failed["baseline"] = "这一段在 run 起点（没有你的改动）上同样失败，多半与候选无关：要么改 .claude/loop.yml 后 `bl contract revise --authorize`，要么本次任务本就要修好它"

    mutated = worktree.residue(wt)
    head_after = gitx.head(wt)
    if mutated or head_after != cand["head"]:
        failed = failed or {"stage": None, "returncode": None, "log": None, "timed_out": False, "signature": None, "tail": ""}
        failed["worktree_mutated"] = {"paths": mutated[:50], "head_after": head_after}

    status = "fail" if failed else "pass"
    with ledger_mod.mutate(ledger_path) as lg2:
        lg2["counters"]["machine_iter"] = it
        details: dict[str, Any] = {"iter": it, "stages": results}
        if failed:
            details["failure"] = {k: v for k, v in failed.items() if k != "tail"}
            lg2["failures"]["machine"].append({"iter": it, "stage": failed.get("stage"), "signature": failed.get("signature"), "log": failed.get("log"), "at": ledger_mod.now_iso()})
        evidence.record(lg2, "machine", status, details, repo_root)
        readiness = evidence.readiness(lg2, repo_root)

    out: dict[str, Any] = {"result": "PASS" if status == "pass" else "FAIL", "iter": it, "stages": results, "readiness": readiness}
    if failed:
        sig = failed.get("signature")
        out["failure"] = failed
        out["repeat_count"] = sum(1 for f in lg2["failures"]["machine"] if sig and f["signature"] == sig)
        out["remaining_iterations"] = max(0, int(lg2["contract"]["assurance"].get("max_iterations", lg2["loop_config"]["max_iterations"])) - it)
    return out
