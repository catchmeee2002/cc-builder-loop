"""machine evidence：在候选 worktree（必须 clean、HEAD == ledger 记录）顺序执行冻结的 pass_cmd。

三态：PASS（全部 stage 退出码 0）/ FAIL（某 stage 非 0 或超时）/ FATAL（判据没跑起来：配置问题）。
失败签名对日志做归一化（去 ANSI、时间戳、临时路径、长 hex），用于识别"同一个失败反复出现"。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import contract as contract_mod
from . import evidence, gitx, ledger as ledger_mod, process, worktree
from .config import expand_main_repo
from .errors import Problem, fatal, needs_user, negative
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


def _run_stage(cwd: Path, stage: dict[str, Any], log_path: Path, env: dict[str, str],
               main_repo: Path) -> tuple[int, str, bool]:
    timed_out = False
    with open(log_path, "wb") as log:
        # pass_cmd 与 proof_runner 共用同一个 {main_repo} 展开（#391，原则二）
        cmd = expand_main_repo(stage["cmd"], main_repo)
        # 进程创建 / 超时 / 整组回收都在 process.run 里闭环（#363）：bash 的子孙不会在超时后继续跑
        rc, timed_out = process.run(["bash", "-c", cmd], cwd=cwd, env=env, timeout=stage["timeout"], stdout=log)
        if timed_out:
            rc = 124
            log.write(f"\n[TIMEOUT] stage={stage['stage']} 超过 {stage['timeout']}s\n".encode())
    text = log_path.read_bytes().decode("utf-8", "replace")
    return rc, text, timed_out


def stage_signal(returncode: Any) -> str | None:
    """直接进程被信号终止时返回规范信号名；普通退出码返回 None。

    runtime 只观察到「被信号带走」，观察不到谁发的（外部会话 / 命令自身）——所以这里不给归因，
    只给事实（#297，原则一）。
    """
    if not isinstance(returncode, int) or returncode >= 0:
        return None
    signum = -returncode
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def commands_digest(stages: list[dict[str, Any]]) -> str:
    """preflight 事件与当前 machine_commands 的配对键。ready_cmd 决定基线怎么分类，必须计入（#377）；
    没写 ready_cmd 的 stage 仍按两元素计算，既有事件照常对得上。"""
    items = []
    for s in stages:
        item = [s["stage"], s["cmd"]]
        if s.get("ready_cmd"):
            item.append(s["ready_cmd"])
        items.append(item)
    return sha256_bytes(dumps(items).encode())[:16]


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
    snapshot_error: str | None = None
    base = Path(lg["repo"]["root"]).parent / f".bl-preflight-{lg['run_id']}"
    try:
        with evidence.gate_lock(lg, evidence.GATE_PREFLIGHT), private_pycache(env, ledger_path.parent):
            with worktree.temp_worktree(repo_root, lg["repo"]["target_start_head"], base.parent, base.name) as wt:
                env["BUILDER_LOOP_CANDIDATE"] = str(wt)
                # 快照 worktree 同样是全新检出：依赖按 ledger 的声明从源树链接过来，否则 ready_cmd 恒不满足（#383）
                worktree.prepare_links(lg, wt)
                for stage in stages:
                    probe = ready_probe(wt, stage, log_dir / f"preflight-{stage['stage']}-ready.log", env, repo_root)
                    if probe is not None and not probe["ready"]:
                        results.append(probe)
                        continue
                    log_path = log_dir / f"preflight-{stage['stage']}.log"
                    rc, _text, timed_out = _run_stage(wt, stage, log_path, env, repo_root)
                    results.append({"stage": stage["stage"], "returncode": rc, "timed_out": timed_out, "log": str(log_path)})
    except Problem as exc:
        # 快照 worktree 建不出来（父目录不可写等）也是「没观察到」，不是基线红（#381，原则一）
        if exc.code != "GIT_COMMAND_FAILED":
            raise
        snapshot_error = exc.message
        # git worktree add 失败可能已建了一半目录：不留残骸（下一次 preflight 会复用这个名字）
        shutil.rmtree(base, ignore_errors=True)
        gitx.git(repo_root, "worktree", "prune", check=False)

    red = [r["stage"] for r in results if _is_red(r)]
    timed = [r["stage"] for r in results if r["timed_out"] and not r.get("unready")]
    signaled = [r["stage"] for r in results if _is_signaled(r)]
    unready = [r["stage"] for r in results if r.get("unready")]
    with ledger_mod.mutate(ledger_path) as lg2:
        ledger_mod.log_event(lg2, "preflight", stages=results, commands_digest=commands_digest(stages), baseline_red=red,
                             baseline_unready=unready, snapshot_error=snapshot_error)
    return {"result": "RED" if red else ("INCONCLUSIVE" if timed or signaled or unready or snapshot_error else "GREEN"),
            "baseline_red": red, "baseline_timed_out": timed,
            "baseline_signaled": signaled, "baseline_unready": unready, "snapshot_error": snapshot_error, "stages": results,
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


def ready_probe(cwd: Path, stage: dict[str, Any], log_path: Path, env: dict[str, str],
                main_repo: Path) -> dict[str, Any] | None:
    """环境就绪探针。stage 未声明 ready_cmd → None（视为就绪）。

    探针不看 stage 的输出、不解析日志文本，只看它自己的退出码：环境没准备好时 `cmd` 根本没跑，
    这一次就没有「判据结果」可观察（原则一、四，#377 #383）。
    """
    cmd = stage.get("ready_cmd")
    if not cmd:
        return None
    probe = {"stage": f"{stage['stage']}-ready", "cmd": cmd, "timeout": stage["timeout"]}
    rc, _text, timed_out = _run_stage(cwd, probe, log_path, env, main_repo)
    return {"stage": stage["stage"], "unready": True, "ready_cmd": cmd, "returncode": rc, "timed_out": timed_out,
            "log": str(log_path), "ready": rc == 0 and not timed_out}


def _is_red(stage: dict[str, Any]) -> bool:
    """普通非零退出才是红。超时与信号终止都是「没观察到结果」，不能算观察到失败（原则一）。
    unready 的 returncode 来自探针，`cmd` 没跑过，同样没有判据结果（#377）。"""
    if stage.get("unready"):
        return False
    rc = stage.get("returncode")
    return isinstance(rc, int) and rc > 0 and not stage.get("timed_out")


def _is_signaled(stage: dict[str, Any]) -> bool:
    """直接进程被信号带走（负 returncode）。来源不可辨，因此只作为「这次没观察到」归类（#297）。
    unready 条目的 returncode 属于探针，`cmd` 没跑过，不能记成 stage 被信号终止（#377）。"""
    if stage.get("unready"):
        return False
    return not stage.get("timed_out") and stage_signal(stage.get("returncode")) is not None


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
    """同一次基线预跑里超时的 stage。超时是没观察到结果，不是观察到失败（原则一）：常见于资源争抢。
    unready 条目的超时属于探针（`cmd` 没跑过），只归 `baseline_unready`（#377）。"""
    ev = _latest_preflight(ledger)
    return None if ev is None else [s["stage"] for s in ev.get("stages", []) if s.get("timed_out") and not s.get("unready")]


def baseline_signaled(ledger: dict[str, Any]) -> list[str] | None:
    """同一次基线预跑里被信号终止的 stage。来源不可辨，同样只是没观察到结果（#297）。"""
    ev = _latest_preflight(ledger)
    return None if ev is None else [s["stage"] for s in ev.get("stages", []) if _is_signaled(s)]


def baseline_unready(ledger: dict[str, Any]) -> list[str] | None:
    """同一次基线预跑里环境就绪探针没过的 stage：`cmd` 没跑过，基线红无从谈起（#377 #383）。
    从 stages[] 派生而不读 event 的 baseline_unready 字段（原则二）。"""
    ev = _latest_preflight(ledger)
    return None if ev is None else [s["stage"] for s in ev.get("stages", []) if s.get("unready")]


def _annotate_baseline(failed: dict[str, Any], ledger: dict[str, Any]) -> None:
    red, timed = baseline_red(ledger), baseline_timed_out(ledger)
    if red is None:
        failed["baseline"] = "未做基线预跑：`bl preflight` 可确认这一段是不是本来就红（后台跑，不占你的时间）"
    elif failed.get("stage") in red:
        # 预跑只在 stage 粒度上观察到「这一段红过」，观察不到红的是不是同一个原因，结论只能停在这一层（原则一，#300）；
        # 不比对两份日志的文本（原则四），把日志交给 builder 自己对照
        failed["baseline_red"] = True
        failed["baseline_log"] = next(s["log"] for s in _latest_preflight(ledger)["stages"] if s["stage"] == failed["stage"])
        failed["baseline"] = "这一段在 run 起点（没有你的改动）上也失败过，但失败原因未必相同：对照 baseline_log 与本次的 log 再判断是否与候选有关"
    elif failed.get("stage") in (timed or []):
        failed["baseline_timed_out"] = True
        failed["baseline"] = "这一段在 run 起点上超时了，可能是资源争抢（有别的全量测试或重负载在跑），不能据此判定失败出在起点；需要时在本机空闲时重跑 `bl preflight`"


def _retry_fact(lg: dict[str, Any], repo_root: Path, reason: str) -> dict[str, Any]:
    """显式重试绑定的当前事实：上一失败签名 + 当前候选 + machine 依赖投影。不判断失败是否与候选无关（#278，原则三）。"""
    rec = lg["evidence"].get("machine") or {}
    mfail = (rec.get("details") or {}).get("failure") or {}
    return {"reason": reason, "previous_signature": mfail.get("signature"), "previous_stage": mfail.get("stage"),
            "candidate_head": lg["candidate"].get("head"), "dependency_digest": evidence.dependency_digest(lg, "machine", repo_root)}


def run_machine(ledger_path: Path, repo_root: Path, retry_reason: str | None = None) -> dict[str, Any]:
    lg = ledger_mod.load(ledger_path)
    if ledger_mod.is_terminal(lg):
        raise fatal("RUN_TERMINAL", "run 已到终态", terminal=lg["terminal"])
    cand = lg["candidate"]
    if not cand.get("head") or not cand.get("checkpoints"):
        raise fatal("CANDIDATE_NOT_CHECKPOINTED", "候选还没有 checkpoint，先 `checkpoint --role builder`")
    if retry_reason is not None and not retry_reason.strip():
        raise fatal("MACHINE_RETRY_REASON_MISSING", "`bl machine --retry` 必须带非空的 --reason：重试是一次被记录的判断，不是重跑碰运气")
    if retry_reason is not None and evidence.state(lg, "machine", repo_root) != evidence.STATE_FAIL:
        # 只给「当前输入上确实有失败」的重试留出口：否则它会变成绕过证据有效性的通用重跑键
        raise fatal("MACHINE_RETRY_NO_FAILURE", "当前输入上没有 fresh machine 失败，--retry 没有作用；直接跑 `bl machine`",
                    state=evidence.state(lg, "machine", repo_root))
    blocked = evidence.machine_blocked(lg, repo_root)
    if blocked:
        # 上限是真的停止点：越过它需要一次被记录的用户决定（bl resume），不是再跑一次
        raise needs_user("MACHINE_BLOCKED", "已触发迭代上限或无进展；用 AskUserQuestion 让用户决定，继续则 `bl resume --reason`", blockers=blocked)
    wt = Path(cand["worktree"])
    worktree.assert_candidate_identity(wt, cand["branch"], cand["head"])
    worktree.assert_candidate_clean(wt, worktree.link_ignores(lg))

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
    signaled: dict[str, Any] | None = None
    unready: dict[str, Any] | None = None
    with evidence.gate_lock(lg, evidence.GATE_MACHINE), private_pycache(env, run_dir):
        if retry_reason is not None:
            # 拿到锁之后再记：GATE_BUSY 时命令根本没执行，审计不能包含一次没发生的重试（原则一）
            with ledger_mod.mutate(ledger_path) as lg_retry:
                ledger_mod.log_event(lg_retry, "machine_retry", **_retry_fact(lg, repo_root, retry_reason.strip()))
        for stage in stages:
            probe = ready_probe(wt, stage, log_dir / f"iter-{it}-{stage['stage']}-ready.log", env, repo_root)
            if probe is not None and not probe["ready"]:
                # 环境没就绪：cmd 没跑，这次没有判据结果可归因给候选（#377 #383，原则一）
                unready = probe
                break
            log_path = log_dir / f"iter-{it}-{stage['stage']}.log"
            rc, text, timed_out = _run_stage(wt, stage, log_path, env, repo_root)
            entry = {"stage": stage["stage"], "returncode": rc, "log": str(log_path), "timed_out": timed_out}
            results.append(entry)
            if rc != 0:
                sig_name = stage_signal(rc)
                if sig_name:
                    # 直接进程被信号带走：runtime 分不清是外部会话还是命令自身，不能冒充候选失败（#297，原则一）
                    signaled = dict(entry, signal=sig_name)
                    break
                mentioned = _tester_paths_in(lg, repo_root, cand["head"], text)
                # 完整日志已经落盘（entry.log）；失败签名读同一份文本，不再对外复制一份截断视图（#365）
                failed = dict(entry, signature=failure_signature(text, stage["stage"], rc), tester_files_mentioned=mentioned)
                break

    wt_changed: dict[str, Any] | None = None
    if signaled is None and unready is None:
        mutated = worktree.residue(wt, worktree.link_ignores(lg))
        head_after = gitx.head(wt)
        if mutated or head_after != cand["head"]:
            # 收尾只观察到「工作树变了」，观察不到谁写的（builder、pass_cmd 或其他）：来源不可辨的观察不归因给候选（#378，原则一）
            wt_changed = {"paths": mutated[:50], "head_at_start": cand["head"], "head_after": head_after}

    status = "fail" if failed else "pass"
    head_now: str | None = None
    changed: list[str] = []
    signaled_now: dict[str, Any] | None = None
    unready_now: dict[str, Any] | None = None
    worktree_changed_now: dict[str, Any] | None = None
    with ledger_mod.mutate(ledger_path) as lg2:
        changed = evidence.input_changes(lg, lg2, "machine", repo_root)
        if changed:
            # 观察期间 bl 自己登记的输入变了（checkpoint / integrate / contract revise）：这次 stage 结果对应不到任何确定输入，
            # 既不是 pass 也不是 fail（原则一）。判据是输入投影而非 git HEAD；未登记进 ledger 的工作树改动走 machine_worktree_changed。
            # 只作废这一次：不写 evidence / failures / machine_iter，只留一条 event
            head_now = lg2["candidate"]["head"]
            ledger_mod.log_event(lg2, "machine_input_changed", head_at_start=cand["head"], head_now=head_now, iter=it, changed_inputs=changed)
        elif unready is not None:
            # 环境没就绪同样不是可归因的观察：只留审计事实，不写 evidence / failures / machine_iter（#377 #383，原则一）
            unready_now = unready
            ledger_mod.log_event(lg2, "machine_stage_unready", stage=unready["stage"], ready_cmd=unready["ready_cmd"],
                                 returncode=unready["returncode"], timed_out=unready["timed_out"], log=unready["log"],
                                 candidate_head=cand["head"], iter=it,
                                 dependency_digest=evidence.dependency_digest(lg, "machine", repo_root))
        elif signaled is not None:
            # 信号终止同样不是可归因的观察：只留审计事实，不写 evidence / failures / machine_iter（#297，原则一）
            signaled_now = signaled
            ledger_mod.log_event(lg2, "machine_stage_signaled", stage=signaled["stage"], returncode=signaled["returncode"],
                                 signal=signaled["signal"], candidate_head=cand["head"], iter=it,
                                 dependency_digest=evidence.dependency_digest(lg, "machine", repo_root))
        elif wt_changed is not None:
            # 收尾看到候选工作树被改动，写入者不可辨（#378）：与信号同构，只作废这次观察，不记候选失败、不占迭代
            worktree_changed_now = wt_changed
            ledger_mod.log_event(lg2, "machine_worktree_changed", paths=wt_changed["paths"], head_at_start=wt_changed["head_at_start"],
                                 head_after=wt_changed["head_after"], candidate_head=cand["head"], iter=it,
                                 dependency_digest=evidence.dependency_digest(lg, "machine", repo_root))
        else:
            if failed and failed.get("stage"):
                _annotate_baseline(failed, lg2)  # 收尾时的 ledger：排队 / 执行期间 preflight 写下的新结论要算数（#270）
            lg2["counters"]["machine_iter"] = it
            details: dict[str, Any] = {"iter": it, "stages": results}
            if failed:
                details["failure"] = dict(failed)
                lg2["failures"]["machine"].append({"iter": it, "stage": failed.get("stage"), "signature": failed.get("signature"), "log": failed.get("log"), "at": ledger_mod.now_iso()})
            evidence.record(lg2, "machine", status, details, repo_root)
            readiness = evidence.readiness(lg2, repo_root)
    if head_now is not None:
        raise negative("MACHINE_INPUT_CHANGED", "machine 执行期间输入变化（checkpoint / integrate / contract revise），本次观察作废：不记 evidence、不计失败、不占迭代；在新输入上直接重跑 `bl machine`", head_at_start=cand["head"], head_now=head_now, changed_inputs=changed, stages=results)
    if signaled_now is not None:
        raise negative("MACHINE_STAGE_SIGNALED",
                       f"stage {signaled_now['stage']} 的直接进程被 {signaled_now['signal']} 终止（returncode {signaled_now['returncode']}）："
                       "runtime 无法判断信号来自外部会话还是命令自身，这不构成候选失败。日志保留；输入不变时直接重跑 `bl machine`",
                       stages=results, **{k: signaled_now[k] for k in ("stage", "returncode", "signal", "log")})
    if unready_now is not None:
        raise negative("MACHINE_STAGE_UNREADY",
                       f"stage {unready_now['stage']} 的环境就绪探针 `{unready_now['ready_cmd']}` 没通过"
                       f"（returncode {unready_now['returncode']}{'，超时' if unready_now['timed_out'] else ''}）："
                       f"环境没准备好，判据命令没跑，这不构成候选失败。日志保留在 {unready_now['log']}；装好环境后直接重跑 `bl machine`，"
                       "或删掉该 stage 的 ready_cmd 恢复旧行为",
                       stages=results, **{k: unready_now[k] for k in ("stage", "ready_cmd", "returncode", "timed_out", "log")})
    if worktree_changed_now is not None:
        raise negative("MACHINE_WORKTREE_CHANGED",
                       "machine 执行期间候选工作树被改动（未提交改动或 HEAD 变化）：runtime 无法判断写入来自 builder、pass_cmd 还是其他，"
                       "这不构成候选失败。现场与日志保留；处理掉改动或 checkpoint 吸收后在确定的输入上直接重跑 `bl machine`",
                       stages=results, **{k: worktree_changed_now[k] for k in ("paths", "head_at_start", "head_after")})

    out: dict[str, Any] = {"result": "PASS" if status == "pass" else "FAIL", "iter": it, "stages": results, "readiness": readiness}
    if failed:
        sig = failed.get("signature")
        out["failure"] = failed
        out["repeat_count"] = sum(1 for f in lg2["failures"]["machine"] if sig and f["signature"] == sig)
        out["remaining_iterations"] = max(0, evidence.max_iterations(lg2) - evidence.machine_failures_in_window(lg2))
    return out
