"""machine evidence：在候选 worktree（必须 clean、HEAD == ledger 记录）顺序执行冻结的 pass_cmd。

三态：PASS（全部 stage 退出码 0）/ FAIL（某 stage 非 0 或超时）/ FATAL（判据没跑起来：配置问题）。
失败签名对日志做归一化（去 ANSI、时间戳、临时路径、长 hex），用于识别"同一个失败反复出现"。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import contract as contract_mod
from . import evidence, gitx, ledger as ledger_mod, worktree
from .errors import fatal, needs_user, negative
from .jsonutil import dumps, sha256_bytes

NO_PROGRESS_REPEATS = 3

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HEX = re.compile(r"\b[0-9a-f]{7,64}\b")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds|m|min)\b")
_TMP = re.compile(r"/(?:tmp|var/folders|private/tmp)/[^\s'\"]+")
_ADDR = re.compile(r"0x[0-9a-fA-F]+")


@contextmanager
def private_pycache(env: dict[str, str], run_dir: Path) -> Iterator[Path]:
    """本次调用独占的 PYTHONPYCACHEPREFIX：子进程不读、不写工作树里的 __pycache__（#275）。
    工作树里遗留的字节码不在任何 digest 里，变异 apply/revert 保持字节数且同秒完成时 CPython 会当它新鲜（原则一）。
    每次调用新建：proof 的临时 worktree 路径跨调用复用，共用前缀会把上一次的字节码带进来。只删自己建的目录（原则三）。"""
    base = run_dir / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    prefix = Path(tempfile.mkdtemp(prefix="pycache-", dir=str(base)))
    env["PYTHONPYCACHEPREFIX"] = str(prefix)
    try:
        yield prefix
    finally:
        shutil.rmtree(prefix, ignore_errors=True)


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
    with evidence.gate_lock(lg, evidence.GATE_PREFLIGHT), private_pycache(env, ledger_path.parent):
        base = Path(lg["repo"]["root"]).parent / f".bl-preflight-{lg['run_id']}"
        with worktree.temp_worktree(repo_root, lg["repo"]["target_start_head"], base.parent, base.name) as wt:
            env["BUILDER_LOOP_CANDIDATE"] = str(wt)
            for stage in stages:
                log_path = log_dir / f"preflight-{stage['stage']}.log"
                rc, _text, timed_out = _run_stage(wt, stage, log_path, env)
                results.append({"stage": stage["stage"], "returncode": rc, "timed_out": timed_out, "log": str(log_path)})

    red = [r["stage"] for r in results if _is_red(r)]
    timed = [r["stage"] for r in results if r["timed_out"]]
    with ledger_mod.mutate(ledger_path) as lg2:
        ledger_mod.log_event(lg2, "preflight", stages=results, commands_digest=commands_digest(stages), baseline_red=red)
    return {"result": "RED" if red else ("INCONCLUSIVE" if timed else "GREEN"), "baseline_red": red, "baseline_timed_out": timed, "stages": results,
            "note": "基线上就失败的 stage 与候选无关：要么改 .claude/loop.yml 后 `bl contract revise --authorize`，要么本次任务本就要修好它"}


def _tester_paths_in(ledger: dict[str, Any], repo_root: Path, candidate_head: str, text: str) -> list[str]:
    """失败日志里出现的、按写边界归 tester 的路径。

    归属只问 `contract.path_owner`（原则二：路径归属只有一个判定入口）。此前取的是
    `evidence.tester_files()`——tester 本 run 改动过的文件——于是因本次契约变更而失效的**既有**测试
    永远匹配不上，那条失败在 runtime 里没有归属也没有出口（#249）。
    候选树 ∪ tester 分支，覆盖 tester 已交卷但还没 integrate 的情形。
    """
    auth = ledger["contract"]["authority"]
    known = set(gitx.ls_tree_blobs(repo_root, candidate_head)) if candidate_head else set()
    known |= set(evidence.tester_files(ledger, repo_root)["present"])
    owned = (p for p in known if contract_mod.path_owner(auth, p) == contract_mod.OWNER_TESTER)
    return sorted(p for p in owned if p in text)


def _is_red(stage: dict[str, Any]) -> bool:
    return stage.get("returncode") not in (0, None) and not stage.get("timed_out")


def _latest_preflight(ledger: dict[str, Any]) -> dict[str, Any] | None:
    want = commands_digest(ledger["contract"]["assurance"].get("machine_commands") or [])
    for ev in reversed(ledger_mod.events_of(ledger, "preflight")):
        if ev.get("commands_digest") == want:
            return ev
    return None


def baseline_red(ledger: dict[str, Any]) -> list[str] | None:
    """与当前 machine_commands 对得上的最近一次基线预跑里，真正失败（非 0 且未超时）的 stage；没跑过 → None。
    从 stages[] 派生而不读 event 的 baseline_red 字段（原则二）：旧 event 把超时也写成了红（#272）。"""
    ev = _latest_preflight(ledger)
    return None if ev is None else [s["stage"] for s in ev.get("stages", []) if _is_red(s)]


def baseline_timed_out(ledger: dict[str, Any]) -> list[str] | None:
    """同一次基线预跑里超时的 stage。超时是没观察到结果，不是观察到失败（原则一）：常见于资源争抢。"""
    ev = _latest_preflight(ledger)
    return None if ev is None else [s["stage"] for s in ev.get("stages", []) if s.get("timed_out")]


def _annotate_baseline(failed: dict[str, Any], ledger: dict[str, Any]) -> None:
    red, timed = baseline_red(ledger), baseline_timed_out(ledger)
    if red is None:
        failed["baseline"] = "未做基线预跑：`bl preflight` 可确认这一段是不是本来就红（后台跑，不占你的时间）"
    elif failed.get("stage") in red:
        failed["baseline_red"] = True
        failed["baseline"] = "这一段在 run 起点（没有你的改动）上同样失败，多半与候选无关：要么改 .claude/loop.yml 后 `bl contract revise --authorize`，要么本次任务本就要修好它"
    elif failed.get("stage") in (timed or []):
        failed["baseline_timed_out"] = True
        failed["baseline"] = "这一段在 run 起点上超时了，可能是资源争抢（有别的全量测试或重负载在跑），不能据此判定失败出在起点；需要时在本机空闲时重跑 `bl preflight`"


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
    with evidence.gate_lock(lg, evidence.GATE_MACHINE), private_pycache(env, run_dir):
        for stage in stages:
            log_path = log_dir / f"iter-{it}-{stage['stage']}.log"
            rc, text, timed_out = _run_stage(wt, stage, log_path, env)
            entry = {"stage": stage["stage"], "returncode": rc, "log": str(log_path), "timed_out": timed_out}
            results.append(entry)
            if rc != 0:
                mentioned = _tester_paths_in(lg, repo_root, cand["head"], text)
                failed = dict(entry, signature=failure_signature(text, stage["stage"], rc), tail=text[-4000:], tester_files_mentioned=mentioned)
                break

    mutated = worktree.residue(wt)
    head_after = gitx.head(wt)
    if mutated or head_after != cand["head"]:
        failed = failed or {"stage": None, "returncode": None, "log": None, "timed_out": False, "signature": None, "tail": ""}
        failed["worktree_mutated"] = {"paths": mutated[:50], "head_after": head_after}

    status = "fail" if failed else "pass"
    head_now: str | None = None
    changed: list[str] = []
    with ledger_mod.mutate(ledger_path) as lg2:
        changed = evidence.input_changes(lg, lg2, "machine", repo_root)
        if changed:
            # 观察期间 bl 自己登记的输入变了（checkpoint / integrate / contract revise）：这次 stage 结果对应不到任何确定输入，
            # 既不是 pass 也不是 fail（原则一）。判据是输入投影而非 git HEAD——pass_cmd 自己 commit 仍走 worktree_mutated。
            # 只作废这一次：不写 evidence / failures / machine_iter，只留一条 event
            head_now = lg2["candidate"]["head"]
            ledger_mod.log_event(lg2, "machine_input_changed", head_at_start=cand["head"], head_now=head_now, iter=it, changed_inputs=changed)
        else:
            if failed and failed.get("stage"):
                _annotate_baseline(failed, lg2)  # 收尾时的 ledger：排队 / 执行期间 preflight 写下的新结论要算数（#270）
            lg2["counters"]["machine_iter"] = it
            details: dict[str, Any] = {"iter": it, "stages": results}
            if failed:
                details["failure"] = {k: v for k, v in failed.items() if k != "tail"}
                lg2["failures"]["machine"].append({"iter": it, "stage": failed.get("stage"), "signature": failed.get("signature"), "log": failed.get("log"), "at": ledger_mod.now_iso()})
            evidence.record(lg2, "machine", status, details, repo_root)
            readiness = evidence.readiness(lg2, repo_root)
    if head_now is not None:
        raise negative("MACHINE_INPUT_CHANGED", "machine 执行期间输入变化（checkpoint / integrate / contract revise），本次观察作废：不记 evidence、不计失败、不占迭代；在新输入上直接重跑 `bl machine`", head_at_start=cand["head"], head_now=head_now, changed_inputs=changed, stages=results)

    out: dict[str, Any] = {"result": "PASS" if status == "pass" else "FAIL", "iter": it, "stages": results, "readiness": readiness}
    if failed:
        sig = failed.get("signature")
        out["failure"] = failed
        out["repeat_count"] = sum(1 for f in lg2["failures"]["machine"] if sig and f["signature"] == sig)
        out["remaining_iterations"] = max(0, evidence.max_iterations(lg2) - evidence.machine_failures_in_window(lg2))
    return out
