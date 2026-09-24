"""Claude Code hook 处理。每个 handler 返回 ((stdout, stderr), exit_code)。

约定：
- 所有 hook 首步按 session_id 找绑定 run；无绑定 → 静默 exit 0，零 git 子进程。stdin 的 cwd 不可信，从不使用。
- matcher 不是身份门禁（agent_type 为空的内部 agent 也会被放进来），handler 内一律复核 agent_type 与登记的 agent_id。
- Stop：run 未终态 → exit 2 + 下一步；等待用户 / 等待在跑的 subagent → 放行；终态但未复盘 → 拦住（复盘硬闸门）。
  不跑 pass_cmd、不解析 transcript。
- 角色结果：开了 SubagentHandback 的环境只有它送达调用方，最后一条消息常是收尾句，一轮还会触发多次
  SubagentStop（#257）——本轮有 handback 尝试就只认 PostToolUse(SubagentHandback) 的 tool_input.message。
  handback 只在 auto 权限模式开启（CC 2.1.280）；本轮没有 handback 时，SubagentStop 解析 last_assistant_message 兜底。
  PreToolUse(SubagentHandback) 先做同一套校验，不合规就 deny：被拒的报告不会送达 builder（#303）。
  「本轮」= 该 agent_id 最近一次 role_start 之后的事件（续接会让 Start 再次触发）。
- tester 首次 integrate 之前看不到候选：PreToolUse 拒绝它读写候选 worktree（Bash 只能尽力而为）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from . import brief as brief_mod
from . import contract as contract_mod
from . import evidence, gitx, ledger as ledger_mod
from .errors import Problem
from .jsonutil import canonical_json, dumps, sha256_bytes
from .run import ROLE_TESTER, checkpoint

ROLES = ("tester", "reviewer")
HANDBACK_TOOL = "SubagentHandback"
VIA_HANDBACK, VIA_STOP, VIA_CLI = "handback", "stop", "cli"
RESULT_MARKER = re.compile(r"^BUILDER_LOOP_RESULT:\s*(\{.*\})\s*$", re.MULTILINE)
STALL_LIMIT = 3
MALFORMED_RETRIES = 2
FILE_TOOLS = ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit")
SEARCH_TOOLS = ("Grep", "Glob")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

HookReturn = tuple[tuple[str, str], int]


def _silent() -> HookReturn:
    return ("", ""), 0


def _block(stderr: str) -> HookReturn:
    return ("", stderr.rstrip() + "\n"), 2


def _json_out(obj: dict[str, Any]) -> HookReturn:
    return (dumps(obj) + "\n", ""), 0


def _deny(reason: str) -> HookReturn:
    return _json_out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": f"[builder-loop] {reason}"}})


def _excerpt(s: str, center: int | None = None, width: int = 120) -> str:
    if center is None:
        return s[-width:] if len(s) > width else s
    lo = max(0, center - width // 2)
    return ("…" if lo else "") + s[lo:lo + width] + ("…" if lo + width < len(s) else "")


def parse_result_marker(text: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """(payload, 为什么没拿到)。「没写标记」和「写了但解析不了」必须分开说，并回显原文——
    否则角色以为自己漏了标记，原样再发一次（#244：反斜杠、字面 TAB、全角冒号肉眼都看不出）。"""
    if not text or not text.strip():
        return None, "没有收到任何输出"
    matches = RESULT_MARKER.findall(text)
    if not matches:
        near = [ln for ln in text.splitlines() if "BUILDER_LOOP_RESULT" in ln]
        if near:
            return None, (f"找到了 BUILDER_LOOP_RESULT 行但格式不对（冒号须为半角 `:`，JSON 须完整写在同一行、其后不能再有别的字符）："
                          f"`{_excerpt(near[-1].strip())}`")
        return None, f"没有找到 BUILDER_LOOP_RESULT 行。你最后输出的是：`{_excerpt(text.strip())}`"
    raw = matches[-1]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (f"BUILDER_LOOP_RESULT 行的 JSON 解析失败：{exc.msg}（第 {exc.pos} 个字符附近：`{_excerpt(raw, exc.pos, 60)}`）。"
                      "字符串里的反斜杠要写成 `\\\\`，TAB 和换行要转义成 `\\t` / `\\n`")
    if not isinstance(obj, dict):
        return None, f"BUILDER_LOOP_RESULT 后面必须是 JSON 对象，收到的是 {type(obj).__name__}"
    return obj, None


# ---------------------------------------------------------------- 结果契约（定义在 brief，与注入上下文同源）


TESTER_RESULT_FORMAT = brief_mod.TESTER_RESULT_FORMAT
REVIEWER_RESULT_FORMAT = brief_mod.REVIEWER_RESULT_FORMAT


def _validate_role_payload(role: str, payload: dict[str, Any]) -> str | None:
    if payload.get("role") != role:
        return f"role 字段必须是 {role}"
    if role == "tester":
        if payload.get("status") not in ("pass", "insufficient_spec"):
            return "status 必须是 pass 或 insufficient_spec"
        if payload["status"] == "pass" and not isinstance(payload.get("proof_spec"), dict):
            return "status=pass 时必须提供 proof_spec 对象"
        return None
    if payload.get("verdict") not in ("pass", "changes_requested", "blocked"):
        return "verdict 必须是 pass / changes_requested / blocked"
    findings = payload.get("findings", [])
    if not isinstance(findings, list):
        return "findings 必须是数组"
    for f in findings:
        if not isinstance(f, dict) or f.get("severity") not in ("blocking", "major", "minor"):
            return "每条 finding 需要 severity ∈ blocking/major/minor"
        if f["severity"] in ("blocking", "major") and f.get("owner") not in ("builder", "tester", "contract"):
            return "blocking / major 的 finding 必须带 owner ∈ builder/tester/contract（谁该去修）"
    return None


def payload_sha256(payload: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def record_role_result(ledger_path: Path, repo_root: Path, role: str, agent_id: str, payload: dict[str, Any], *, via: str) -> dict[str, Any]:
    """把角色结果写成 evidence。tester：先提交它 worktree 里的文件，再校验 proof_spec 的结构。
    role_result 事件带来源 via 与 payload_sha256（同轮去重的依据）。"""
    err = _validate_role_payload(role, payload)
    if err:
        raise Problem("RESULT_INVALID", err, details={"role": role}, exit_code=1)
    src = {"via": via, "payload_sha256": payload_sha256(payload)}

    if role == "tester":
        cp = checkpoint(ledger_path, repo_root, ROLE_TESTER, message=None)  # 越界 → CHECKPOINT_REJECTED
        current = ledger_mod.load(ledger_path)
        if payload["status"] == "insufficient_spec" and cp.get("noop") and evidence.state(current, "tester", repo_root) == evidence.STATE_PASS:
            # 续接轮里它只是完成不了这次的请求（比如补不出 patch）：测试没变、原证据依然成立，不要覆盖成 fail
            with ledger_mod.mutate(ledger_path) as lg2:
                ledger_mod.log_event(lg2, "role_result", role=role, agent_id=agent_id, status="declined", notes=str(payload.get("notes", ""))[:500], **src)
                readiness = evidence.readiness(lg2, repo_root)
            return {"recorded": "tester", "status": "declined", "readiness": readiness}
        if payload["status"] == "pass":
            from .proof import validate_spec

            spec = validate_spec(payload["proof_spec"], ledger_mod.load(ledger_path), repo_root)  # → PROOF_SPEC_INVALID
        with ledger_mod.mutate(ledger_path) as lg2:
            files = evidence.tester_files(lg2, repo_root)
            has_files = bool(files["present"] or files["deleted"])
            status = "pass" if (payload["status"] == "pass" and has_files) else "fail"
            details = {
                "result": payload["status"], "files": files,
                "behaviors_covered": payload.get("behaviors_covered", []),
                "notes": payload.get("notes", ""), "tester_head": cp["head"],
            }
            if payload["status"] == "pass" and not has_files:
                details["reason"] = "tester 没有提交任何测试改动"
            if status == "pass":
                lg2["proof_spec"] = spec  # patch_file 已换成 runtime 读到的字节（#281）
            rec = evidence.record(lg2, "tester", status, details, repo_root, agent_id=agent_id)
            ledger_mod.log_event(lg2, "role_result", role=role, agent_id=agent_id, status=status, tester_head=cp["head"], **src)
            readiness = evidence.readiness(lg2, repo_root)
        return {"recorded": "tester", "status": rec["status"], "files": files, "readiness": readiness}

    with ledger_mod.mutate(ledger_path) as lg2:
        starts = [e for e in ledger_mod.events_of(lg2, "role_start") if e.get("role") == "reviewer" and e.get("agent_id") == agent_id]
        started_head = starts[-1].get("candidate_head") if starts else None
        moved = bool(started_head) and started_head != lg2["candidate"]["head"]
        blocking = [f for f in payload.get("findings", []) if f.get("severity") == "blocking"]
        status = "pass" if payload["verdict"] == "pass" and not blocking and not moved else "fail"
        details = {
            "verdict": payload["verdict"], "findings": payload.get("findings", []),
            "behaviors_verified": payload.get("behaviors_verified", []),
            "reviewed_head": started_head or lg2["candidate"]["head"],
        }
        if moved:
            details["reason"] = "候选在审查期间发生了变化，本次结论无效，需要复审"
        rec = evidence.record(lg2, "reviewer", status, details, repo_root, agent_id=agent_id)
        ledger_mod.log_event(lg2, "role_result", role=role, agent_id=agent_id, status=status, verdict=payload["verdict"], candidate_moved=moved, **src)
        readiness = evidence.readiness(lg2, repo_root)
    return {"recorded": "reviewer", "status": rec["status"], "readiness": readiness}


# ---------------------------------------------------------------- 上下文注入


def agent_context(lg: dict[str, Any], role: str, repo_root: Path) -> str:
    """首次 spawn 注入的上下文 = brief 的文本形态。续接时 CC 不送达，角色要自己 `bl brief`。"""
    return brief_mod.render(brief_mod.build(lg, repo_root, role))


# ---------------------------------------------------------------- Stop


ACTION_HINTS = {
    evidence.ACTION_CHECKPOINT: "在候选 worktree 完成实现后运行 `bl checkpoint --role builder --session <session>`",
    evidence.ACTION_INTEGRATE: "运行 `bl integrate --session <session>` 把 tester 的测试并入候选",
    evidence.ACTION_MACHINE: "运行 `bl machine --session <session>`；FAIL 则按日志修复后重新 checkpoint（若是测试本身写错，SendMessage 给 tester 并附失败日志）",
    evidence.ACTION_SPAWN_TESTER: "spawn tester（Agent subagent_type=tester，prompt 只需给 run_id；它在后台跑），然后继续写实现",
    evidence.ACTION_RESUME_TESTER: "用 SendMessage 续接已登记的 tester（status.agents.tester.agent_id），说明要它修什么 / 补 mutation patch",
    evidence.ACTION_PROOF: "运行 `bl proof --session <session>`",
    evidence.ACTION_SPAWN_REVIEWER: "spawn reviewer（Agent subagent_type=reviewer，prompt 只需给 run_id）",
    evidence.ACTION_RESUME_REVIEWER: "修复后重新 checkpoint / machine / proof，再 SendMessage 续接已登记的 reviewer 复审",
    evidence.ACTION_FINALIZE: "运行 `bl finalize --session <session> -m '<commit message>'`；用户决定等外部条件（如发版顺序）再合入 → `bl hold --session <session> --reason '<等什么>'`",
    evidence.ACTION_HELD: "已按用户决定暂缓合入（期间不必重验）；外部条件满足后 `bl hold --session <session> --release`，再按 next_action 走",
    evidence.ACTION_REBASE: "目标分支改过 tester 的测试文件而 tester 分支还在旧基线上：运行 `bl rebase --session <session>` 把它挪过去（冲突会交给 tester 解）",
    evidence.ACTION_NEEDS_USER: "存在 blocker：用 AskUserQuestion 让用户决定；继续则 `bl resume --session <session> --reason '<用户的决定>'`，放弃则 `bl abandon --reason`",
    evidence.ACTION_RETRO: "run 已结束但还没复盘：`bl retro signals --session <session>` 看信号，逐条给去向后 `bl retro record --session <session> --file <json>`",
}


def _stall_tick(lg2: dict[str, Any], ev: dict[str, Any]) -> bool:
    stall = lg2["counters"]["stall"]
    if ev.get("stop_hook_active") and stall.get("seq_seen") == lg2["seq"]:
        stall["count"] = int(stall.get("count", 0)) + 1
    else:
        stall["count"] = 0
    stall["seq_seen"] = lg2["seq"] + 1  # mutate 退出后的 seq
    hit = stall["count"] >= STALL_LIMIT
    if hit:
        ledger_mod.log_event(lg2, "stall_escape", count=stall["count"])
    return hit


def handle_stop(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    lg = bound["ledger"]
    lp, root = bound["ledger_path"], bound["repo_root"]
    sid = ev.get("session_id")
    if ledger_mod.is_terminal(lg) and not ledger_mod.needs_retro(lg):
        ledger_mod.unbind_session(sid)
        return _silent()
    if lg.get("waiting_for_user"):
        return _silent()
    readiness = evidence.readiness(lg, root)
    act = readiness["next_action"]
    if act in evidence.AWAITING_ACTIONS:
        return _silent()  # 后台 subagent 结束时主 session 会被唤醒；这期间拉回只会产生无进展往返（#228）
    if act == evidence.ACTION_HELD:
        return _silent()  # 用户授权的等待：合入时机由外部条件决定，不是没有进展（#291）

    with ledger_mod.mutate(lp) as lg2:
        stalled = _stall_tick(lg2, ev)
    if stalled:
        return ("", f"[builder-loop] run {lg['run_id']} 连续 {STALL_LIMIT} 次 Stop 之间没有任何 runtime 进展，停止续接；请用 AskUserQuestion 让用户决定。"
                    f"若 gate 已全过、是按用户决定等外部条件再合入，用 `bl hold --reason` 记录，Stop 会放行。\n"), 0

    hint = ACTION_HINTS.get(act, "").replace("<session>", str(sid))
    head = "已结束但尚未复盘，不能就此收工" if act == evidence.ACTION_RETRO else "未完成，不能结束"
    msg = f"[builder-loop] run {lg['run_id']} {head}。\nevidence: {readiness['states']}\nnext_action={act}: {hint}\n"
    if readiness["blockers"]:
        msg += f"blockers: {dumps(readiness['blockers'])}\n"
    leftover = evidence.role_background_tasks(lg)
    if leftover:
        msg += ("角色留下了后台任务（交卷后会把它再唤醒）：逐个用 TaskStop 停掉 "
                + ", ".join(f"{t['task_id']}（{t['role']}）" for t in leftover) + "\n")
    return _block(msg)


# ---------------------------------------------------------------- Subagent


def _keep_stall(lg2: dict[str, Any]) -> None:
    """角色 hook 的写入不算主 session 的进展：保持 stall.seq_seen 与 seq 的相等关系。"""
    stall = lg2["counters"]["stall"]
    if stall.get("seq_seen") == lg2["seq"]:
        stall["seq_seen"] = lg2["seq"] + 1


def handle_subagent_start(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    role = ev.get("agent_type")
    if role not in ROLES or ledger_mod.is_terminal(bound["ledger"]):
        return _silent()
    lp, root = bound["ledger_path"], bound["repo_root"]
    agent_id = ev.get("agent_id")
    with ledger_mod.mutate(lp) as lg:
        if role == "tester" and not lg.get("tester"):
            return _silent()
        prev = lg["agents"].get(role) or {}
        if prev.get("agent_id") == agent_id and not _resume_requested(lg, role, agent_id):
            # 已登记的角色、builder 没发过续接：是它残留的后台任务结束把它唤醒了（#306）。这一轮不是续接，
            # 不记 role_start、不注入上下文，它这一轮里说的结论一律不登记（原则七：续接只认持久化的 intent）
            ledger_mod.log_event(lg, "role_wake", role=role, agent_id=agent_id, candidate_head=lg["candidate"]["head"])
            _keep_stall(lg)
            return _silent()
        if prev and prev.get("agent_id") != agent_id:
            ledger_mod.log_event(lg, "role_replaced", role=role, old_agent_id=prev.get("agent_id"), new_agent_id=agent_id)
            prev = {}
        turn = int(prev.get("turn", 0)) + 1
        lg["agents"][role] = {"agent_id": agent_id, "started_at": prev.get("started_at") or ledger_mod.now_iso(), "turn": turn, "stops": 0}
        ledger_mod.log_event(lg, "role_start", role=role, agent_id=agent_id, turn=turn, candidate_head=lg["candidate"]["head"])
        _keep_stall(lg)
        ctx = agent_context(lg, role, root)
    evidence.touch_heartbeat(bound["ledger"], role)
    return _json_out({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ctx}})


def _registered_role(ev: dict[str, Any], bound: dict[str, Any]) -> str | None:
    """事件来自本 run 登记在册的 tester / reviewer 才返回角色名。"""
    role = ev.get("agent_type")
    if role not in ROLES:
        return None
    lg = bound["ledger"]
    reg = lg["agents"].get(role) or {}
    if ledger_mod.is_terminal(lg) or not reg or reg.get("agent_id") != ev.get("agent_id"):
        return None
    return role


def _resume_requested(lg: dict[str, Any], role: str, agent_id: str | None) -> bool:
    """最近一次 resume_request（builder 的 SendMessage）晚于该 agent 最近一次开轮 / 唤醒 / 登记：这次 SubagentStart 是续接。
    一条请求只对应一次开轮；请求之后角色又交了结论（消息夹在它的运行中到达，没有开新一轮）也就作废。"""
    life = [e for e in ledger_mod.events_of(lg, "resume_request", "role_start", "role_wake", "role_result")
            if e.get("role") == role and e.get("agent_id") == agent_id]
    return bool(life) and life[-1]["kind"] == "resume_request"


def _in_wake_turn(lg: dict[str, Any], role: str, agent_id: str | None) -> bool:
    """该 agent 当前这一轮是被残留后台任务唤醒的（最近一次开轮事件是 role_wake）。"""
    opens = [e for e in ledger_mod.events_of(lg, "role_start", "role_wake")
             if e.get("role") == role and e.get("agent_id") == agent_id]
    return bool(opens) and opens[-1]["kind"] == "role_wake"


def _turn_events(lg: dict[str, Any], role: str, agent_id: str | None) -> list[dict[str, Any]]:
    """本轮 = 该 agent 最近一次 role_start 之后。只从 events 派生，不另记状态。"""
    life = [e for e in ledger_mod.events_of(lg, "role_start", "role_result", "role_malformed")
            if e.get("role") == role and e.get("agent_id") == agent_id]
    starts = [i for i, e in enumerate(life) if e["kind"] == "role_start"]
    return life[starts[-1] + 1:] if starts else life


def _handback_hint(role: str) -> str:
    fmt = TESTER_RESULT_FORMAT if role == "tester" else REVIEWER_RESULT_FORMAT
    return f"重新调用 {HANDBACK_TOOL}({{message: <完整报告>}})，message 最后一行必须是单行：{fmt}"


def _stop_hint(role: str) -> str:
    fmt = TESTER_RESULT_FORMAT if role == "tester" else REVIEWER_RESULT_FORMAT
    return (f"有 {HANDBACK_TOOL} 工具就调用它交卷（message 最后一行是结果行）；没有就让最后一条消息的最后一行是单行：{fmt}")


def _accept(bound: dict[str, Any], role: str, agent_id: str | None, text: Any, via: str, hint: str) -> HookReturn:
    """解析 → 校验 → 同轮去重 → 登记 → 清零不合规计数。handback 与 Stop 兜底共用，只有来源与提示不同。"""
    lp, root = bound["ledger_path"], bound["repo_root"]
    payload, err = parse_result_marker(text if isinstance(text, str) else None)
    if payload:
        err = _validate_role_payload(role, payload)
    if err:
        return _retry_or_fail(lp, root, role, agent_id, f"{err}。{hint}", via=via)
    if _already_recorded(bound["ledger"], role, agent_id, payload_sha256(payload)):
        return _silent()
    try:
        record_role_result(lp, root, role, agent_id, payload, via=via)
    except Problem as exc:
        return _retry_or_fail(lp, root, role, agent_id,
                              f"{exc.code}: {exc.message} {dumps(exc.details) if exc.details else ''}。修正后{hint}", via=via)
    with ledger_mod.mutate(lp) as lg2:
        lg2["agents"][role]["stops"] = 0  # 交上合规结论，本轮之前的不合规次数不再累计
        _keep_stall(lg2)
    return _silent()


def _turn_failed(lg: dict[str, Any], role: str, agent_id: str | None) -> bool:
    return any(e["kind"] == "role_malformed" and e.get("final") for e in _turn_events(lg, role, agent_id))


def _precheck_result(bound: dict[str, Any], role: str, text: Any) -> str | None:
    """登记前会做的校验，不提交、不写 ledger。test_ids 查 tester worktree 的工作树（登记时才提交）。"""
    payload, err = parse_result_marker(text if isinstance(text, str) else None)
    if payload:
        err = _validate_role_payload(role, payload)
    if err or role != "tester" or payload["status"] != "pass":
        return err
    from .proof import validate_spec

    try:
        validate_spec(payload["proof_spec"], bound["ledger"], bound["repo_root"], worktree_files=True)
    except Problem as exc:
        return f"{exc.code}: {exc.message} {dumps(exc.details) if exc.details else ''}"
    return None


def handle_handback_pre(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    """PreToolUse(SubagentHandback)：投递前校验，不合规就 deny——被拒的报告到不了 builder（#303，原则二）。
    只校验不登记：deny 之后工具不执行，登记仍在 PostToolUse。超过重试上限记 fail 并放行，不把角色困死（原则三）。"""
    role = _registered_role(ev, bound)
    agent_id = ev.get("agent_id")
    if not role or _in_wake_turn(bound["ledger"], role, agent_id) or _turn_failed(bound["ledger"], role, agent_id):
        return _silent()
    err = _precheck_result(bound, role, (ev.get("tool_input") or {}).get("message"))
    if not err:
        return _silent()
    return _retry_or_fail(bound["ledger_path"], bound["repo_root"], role, agent_id, f"{err}。{_handback_hint(role)}",
                          via=VIA_HANDBACK, deny=True)


def handle_handback(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    """PostToolUse(SubagentHandback)：送达调用方的那份报告就是角色结论，当场登记（早于调用方收到它，#250）。
    本轮已记 final fail（PreToolUse 到上限后放行的那份）就不再登记。"""
    role = _registered_role(ev, bound)
    if not role or _in_wake_turn(bound["ledger"], role, ev.get("agent_id")) or _turn_failed(bound["ledger"], role, ev.get("agent_id")):
        return _silent()
    return _accept(bound, role, ev.get("agent_id"), (ev.get("tool_input") or {}).get("message"), VIA_HANDBACK, _handback_hint(role))


def _already_recorded(lg: dict[str, Any], role: str, agent_id: str | None, sha: str) -> bool:
    """同一轮重复交同一份结论才算已登记。只跟本轮最后一条比：A→B→A 的最后一次 A 是新结论。
    tester 的 worktree 还有未提交改动时不算重复——那些测试要随这次登记提交。"""
    results = [e for e in _turn_events(lg, role, agent_id) if e["kind"] == "role_result"]
    if not results or results[-1].get("payload_sha256") != sha:
        return False
    return role != "tester" or gitx.is_clean(lg["tester"]["worktree"])


def handle_subagent_stop(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    """本轮有 handback 尝试：只认 handback，最后一条消息调用方收不到——已有结论放行，只有不合规的就打回重交。
    本轮没有 handback（该环境没开 handback，按环境开关、不由版本号决定）：最后一条消息就是送达的报告，兜底解析登记。"""
    role = _registered_role(ev, bound)
    if not role:
        return _silent()
    agent_id = ev.get("agent_id")
    if _in_wake_turn(bound["ledger"], role, agent_id):
        return _silent()  # 唤醒轮次：它重发的旧结论不登记，也不算不合规（#306）
    turn = _turn_events(bound["ledger"], role, agent_id)
    if any(e["kind"] == "role_result" for e in turn):
        return _silent()
    if _turn_failed(bound["ledger"], role, agent_id):
        return _silent()  # 已记 fail，不再打回
    if any(e.get("via") == VIA_HANDBACK for e in turn):
        return _retry_or_fail(bound["ledger_path"], bound["repo_root"], role, agent_id,
                              f"本轮 {HANDBACK_TOOL} 的结果不合规，还没有登记。{_handback_hint(role)}", via=VIA_STOP)
    return _accept(bound, role, agent_id, ev.get("last_assistant_message"), VIA_STOP, _stop_hint(role))


def _retry_or_fail(lp: Path, root: Path, role: str, agent_id: str | None, reason: str, *, via: str, deny: bool = False) -> HookReturn:
    """不合规计一次；未到上限就打回（Stop / PostToolUse 用 exit 2，PreToolUse 用 deny），到上限记 fail 放行。"""
    with ledger_mod.mutate(lp) as lg:
        reg = lg["agents"][role]
        reg["stops"] = int(reg.get("stops", 0)) + 1
        stops = reg["stops"]
        final = stops > MALFORMED_RETRIES
        ledger_mod.log_event(lg, "role_malformed", role=role, agent_id=agent_id, final=final, via=via, reason=reason[:500])
        if final:
            evidence.record(lg, role, "fail", {"result": "malformed", "reason": reason}, root, agent_id=agent_id)
        else:
            _keep_stall(lg)
    if final:
        return ("", f"[builder-loop] {role} 结果连续 {stops} 次不合规，已记为 fail：{reason}\n"), 0
    if deny:
        return _deny(f"{role} 结果不合规（第 {stops} 次），这份报告没有送出：{reason}")
    return _block(f"[builder-loop] {role} 结果不合规（第 {stops} 次）：{reason}")


# ---------------------------------------------------------------- PreToolUse


def _under(path: str, root: str) -> bool:
    """realpath 之后再做前缀判断：`…/tester/../builder/x` 这类路径骗不过去。"""
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip("/") + "/")


def _mentions_path(cmd: str, root: str) -> bool:
    """命令串里出现 root：原样、realpath，或某个路径样的词经 `..` 归一后落在 root 下。只做路径子串判断，不解析命令语义。"""
    if root in cmd or os.path.realpath(root) in cmd:
        return True
    for word in cmd.replace("=", " ").split():
        word = word.strip("'\"")
        if "/" in word and word.startswith(("/", "~")) and _under(os.path.expanduser(word), root):
            return True
    return False


def _guard_tester(ev: dict[str, Any], lg: dict[str, Any]) -> str | None:
    tool, ti = ev.get("tool_name"), (ev.get("tool_input") or {})
    t, cand = lg.get("tester") or {}, lg["candidate"]
    blind = not evidence.implementation_readable_by_tester(lg)
    auth = lg["contract"]["authority"]

    if tool in WRITE_TOOLS:
        path = str(ti.get("file_path") or ti.get("notebook_path") or "")
        if not t or not _under(path, t["worktree"]):
            return f"tester 只能写自己的 worktree {t.get('worktree')}"
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(t["worktree"]))
        reason = contract_mod.write_rejection(auth, contract_mod.OWNER_TESTER, rel)
        return f"tester 不能写 {rel}（{reason}）；写边界 tester_write={auth['tester_write']}" if reason else None
    if tool == "Bash":
        cmd = str(ti.get("command") or "")
        if _mentions_path(cmd, cand["worktree"]):
            # 集成后读候选走 Read / Grep / Glob 或 `git show <分支>:<路径>`；在候选里改文件或跑命令会污染 builder 的现场（#234 #275）
            return "tester 的命令不能触及候选 worktree：读候选用 Read / Grep / Glob；生成 patch 按 brief 的办法用 git show 取出到临时目录"
        if blind and cand["branch"] in cmd:
            return "集成之前 tester 的命令不能触及候选 worktree 或候选分支"
        return None
    if not blind:
        return None
    if tool == "Read":
        path = str(ti.get("file_path") or "")
        return "集成之前 tester 不能读候选实现；测试只依据 contract 的 behaviors / interfaces 写" if path and _under(path, cand["worktree"]) else None
    if tool in SEARCH_TOOLS:
        path = ti.get("path")
        if not path:
            return f"请显式给出 path（你的 worktree：{t.get('worktree')}）；缺省目录可能落到候选实现上"
        return "集成之前 tester 不能搜索候选实现" if _under(str(path), cand["worktree"]) else None
    return None


def handle_pre_tool_use(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    tool = ev.get("tool_name")
    lg = bound["ledger"]
    if ledger_mod.is_terminal(lg):
        # 复盘阶段主 session 仍会用 AskUserQuestion（问哪些要立项），等待用户时 Stop 必须放行
        if tool == "AskUserQuestion" and ledger_mod.needs_retro(lg) and ev.get("agent_type") not in ROLES:
            with ledger_mod.mutate(bound["ledger_path"]) as lg2:
                lg2["waiting_for_user"] = {"since": ledger_mod.now_iso(), "reason": "AskUserQuestion", "tool_use_id": ev.get("tool_use_id")}
        return _silent()
    if tool == HANDBACK_TOOL:
        return handle_handback_pre(ev, bound)
    role = ev.get("agent_type") if ev.get("agent_type") in ROLES else None
    if role:
        reg = lg["agents"].get(role) or {}
        if reg.get("agent_id") == ev.get("agent_id"):
            evidence.touch_heartbeat(lg, role)  # 续租只碰心跳文件，不写 ledger
        if tool == "Bash" and (ev.get("tool_input") or {}).get("run_in_background"):
            # 角色的后台进程活过交卷，结束时的任务通知会把它反复唤醒（#283）；只看结构化字段，不解析命令
            return _deny("角色不能用 run_in_background 起后台任务：交卷后它会把你反复唤醒。需要跑久的命令就前台执行并给足 timeout，只跑与你的结论有关的测试文件")
        if role == "reviewer":
            return _deny("reviewer 只读，不允许写文件") if tool in WRITE_TOOLS else _silent()
        reason = _guard_tester(ev, lg)
        return _deny(reason) if reason else _silent()
    if tool == "AskUserQuestion":
        with ledger_mod.mutate(bound["ledger_path"]) as lg2:
            lg2["waiting_for_user"] = {"since": ledger_mod.now_iso(), "reason": "AskUserQuestion", "tool_use_id": ev.get("tool_use_id")}
        return _silent()
    if tool == "SendMessage" and not ev.get("agent_id"):
        # builder 续接角色的持久化 intent（#306）：SubagentStart 本身分不出续接与唤醒，只有这里看得到 to
        to = (ev.get("tool_input") or {}).get("to")
        for r in ROLES:
            if to and (lg["agents"].get(r) or {}).get("agent_id") == to:
                with ledger_mod.mutate(bound["ledger_path"]) as lg2:
                    ledger_mod.log_event(lg2, "resume_request", role=r, agent_id=to)
        return _silent()
    if tool == "EnterWorktree":
        return _deny(f"run {lg['run_id']} 进行中：worktree 由 runtime 管理（{lg['candidate']['worktree']}），禁止 EnterWorktree。")
    return _silent()


def _user_input(bound: dict[str, Any], source: str) -> HookReturn:
    lg = bound["ledger"]
    if ledger_mod.is_terminal(lg) and not ledger_mod.needs_retro(lg):
        return _silent()
    # 只有 AskUserQuestion 的回答算「用户输入」事件（`bl resume` 据此确认授权来自用户）。
    # UserPromptSubmit 不算：后台 subagent 结束的任务通知也会触发它（CC 2.1.272 实测），那不是真人。
    if source != "AskUserQuestion" and not lg.get("waiting_for_user"):
        return _silent()
    with ledger_mod.mutate(bound["ledger_path"]) as lg2:
        lg2["waiting_for_user"] = None
        if source == "AskUserQuestion":
            ledger_mod.log_event(lg2, "user_input", source=source)
    return _silent()


# 唤醒轮次里交的结论不登记（#306），所以不能让角色停下来等它（#302）
ROLE_BACKGROUND_HINT = ("这条命令超时后被转到了后台。别等它：这一轮就把结论交上来，等它结束时唤醒你的那一轮里交的结论不会被登记。"
                        "需要它的结果就给足 timeout 在前台重跑，或缩小命令范围；这个后台任务会由 builder 停掉。")


def _tool_response(ev: dict[str, Any]) -> dict[str, Any]:
    r = ev.get("tool_response")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except ValueError:
            return {}
    return r if isinstance(r, dict) else {}


def _role_background(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    """角色的前台 Bash 超时后被 CC 转到后台（PreToolUse 的 run_in_background 拦截看不到，#283）：
    PostToolUse 的 tool_response 带 backgroundTaskId。记下来交给 builder 停，并当场告诉角色。"""
    role = _registered_role(ev, bound)
    task_id = _tool_response(ev).get("backgroundTaskId")
    if not role or not task_id:
        return _silent()
    with ledger_mod.mutate(bound["ledger_path"]) as lg2:
        ledger_mod.log_event(lg2, "role_background", role=role, agent_id=ev.get("agent_id"), task_id=str(task_id),
                             command=str((ev.get("tool_input") or {}).get("command", ""))[:200])
        _keep_stall(lg2)
    return _json_out({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                             "additionalContext": f"[builder-loop] {ROLE_BACKGROUND_HINT}（任务 {task_id}）"}})


def _role_background_stopped(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    ti = ev.get("tool_input") or {}
    task_id = ti.get("task_id") or ti.get("shell_id")
    if ev.get("agent_id") or not task_id:
        return _silent()
    if any(t["task_id"] == task_id for t in evidence.role_background_tasks(bound["ledger"])):
        with ledger_mod.mutate(bound["ledger_path"]) as lg2:
            ledger_mod.log_event(lg2, "role_background_stopped", task_id=str(task_id))
    return _silent()


def handle_post_tool_use(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    if ev.get("tool_name") == HANDBACK_TOOL:
        return handle_handback(ev, bound)
    if ev.get("tool_name") == "Bash" and ev.get("agent_id"):
        return _role_background(ev, bound)
    if ev.get("tool_name") == "TaskStop":
        return _role_background_stopped(ev, bound)
    if ev.get("tool_name") == "AskUserQuestion" and ev.get("agent_type") not in ROLES:
        return _user_input(bound, "AskUserQuestion")
    return _silent()


def handle_user_prompt_submit(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    return _user_input(bound, "UserPromptSubmit")


HANDLERS = {
    "Stop": handle_stop,
    "SubagentStart": handle_subagent_start,
    "SubagentStop": handle_subagent_stop,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
    "UserPromptSubmit": handle_user_prompt_submit,
}
TRACED_EVENTS = ("Stop", "SubagentStart", "SubagentStop")


def handle_hook(event: str, stdin_text: str) -> HookReturn:
    handler = HANDLERS.get(event)
    if not handler:
        return _silent()
    try:
        ev = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        return _silent()
    bound = ledger_mod.lookup_session(ev.get("session_id"))
    if not bound:
        return _silent()
    result = handler(ev, bound)
    if event in TRACED_EVENTS or ev.get("tool_name") == HANDBACK_TOOL or result[1] != 0 or result[0][0]:
        _trace(event, ev, bound, result)
    return result


def _trace(event: str, ev: dict[str, Any], bound: dict[str, Any], result: HookReturn) -> None:
    """生命周期事件与被拦截的工具调用各记一行诊断；普通放行的 PreToolUse 不记（否则每次 Read 一行）。"""
    try:
        path = ledger_mod.home_dir() / "hook-trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "at": ledger_mod.now_iso(), "event": event, "run_id": bound["ledger"]["run_id"],
            "tool_name": ev.get("tool_name"), "agent_type": ev.get("agent_type"), "agent_id": ev.get("agent_id"),
            "stop_hook_active": ev.get("stop_hook_active"), "exit": result[1], "denied": bool(result[0][0]) and event == "PreToolUse",
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(dumps(line) + "\n")
    except OSError:
        pass
