"""Claude Code hook 处理。每个 handler 返回 ((stdout, stderr), exit_code)。

约定：
- 所有 hook 首步按 session_id 找绑定 run；无绑定 → 静默 exit 0，零 git 子进程。
- Stop：run 未终态 → exit 2 + stderr 说明下一步；不跑 pass_cmd、不解析 transcript。
- SubagentStart/Stop：只认 CC 提供的 agent_type / agent_id；tester/reviewer 的 evidence 只由这里写入。
- PreToolUse(AskUserQuestion) 写 waiting_for_user，PostToolUse / UserPromptSubmit 清除。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import contract as contract_mod
from . import evidence, ledger as ledger_mod
from .errors import Problem
from .jsonutil import dumps
from .run import ROLE_TESTER, checkpoint

ROLES = ("tester", "reviewer")
RESULT_MARKER = re.compile(r"^BUILDER_LOOP_RESULT:\s*(\{.*\})\s*$", re.MULTILINE)
STALL_LIMIT = 3
MALFORMED_RETRIES = 2

HookReturn = tuple[tuple[str, str], int]


def _silent() -> HookReturn:
    return ("", ""), 0


def _block(stderr: str) -> HookReturn:
    return ("", stderr.rstrip() + "\n"), 2


def _json_out(obj: dict[str, Any]) -> HookReturn:
    return (dumps(obj) + "\n", ""), 0


def parse_result_marker(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    matches = RESULT_MARKER.findall(text)
    if not matches:
        return None
    try:
        obj = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# ---------------------------------------------------------------- 结果契约


TESTER_RESULT_FORMAT = (
    'BUILDER_LOOP_RESULT: {"role":"tester","status":"pass|insufficient_spec","files":["tests/..."],'
    '"behaviors_covered":["B1"],"proof_spec":{"groups":[{"kind":"baseline-red|mutation|reviewed-boundaries",'
    '"behavior_ids":["B1"],"argv":["python3","-m","pytest","-q","tests/test_x.py"],"test_ids":["tests/test_x.py::test_a"],'
    '"timeout":120,"patch":"<mutation 时的 unified diff>","reviewed_boundaries":{"positive":[],"negative":[],"boundary":[],"invariant":[]}}]},'
    '"notes":""}'
)
REVIEWER_RESULT_FORMAT = (
    'BUILDER_LOOP_RESULT: {"role":"reviewer","verdict":"pass|changes_requested|blocked",'
    '"findings":[{"severity":"blocking|major|minor","file":"src/x.py","line":10,"summary":"..."}],'
    '"behaviors_verified":["B1"]}'
)


def _validate_role_payload(role: str, payload: dict[str, Any]) -> str | None:
    if payload.get("role") != role:
        return f"role 字段必须是 {role}"
    if role == "tester":
        if payload.get("status") not in ("pass", "insufficient_spec"):
            return "status 必须是 pass 或 insufficient_spec"
        if not isinstance(payload.get("files", []), list):
            return "files 必须是数组"
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
    return None


def _tester_files_from_checkpoints(lg: dict[str, Any], repo_root: Path) -> list[str]:
    from . import gitx

    paths: set[str] = set()
    for cp in lg["candidate"]["checkpoints"]:
        if cp["role"] == ROLE_TESTER:
            paths.update(cp["paths"])
    head = lg["candidate"]["head"]
    if not paths or not head:
        return []
    existing = gitx.ls_tree_blobs(repo_root, head, sorted(paths))
    return sorted(p for p in paths if p in existing)


def record_role_result(ledger_path: Path, repo_root: Path, role: str, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """把角色结果写成 evidence。tester 先 checkpoint 自己的文件、校验 proof_spec。"""
    err = _validate_role_payload(role, payload)
    if err:
        raise Problem("RESULT_INVALID", err, details={"role": role}, exit_code=1)

    if role == "tester":
        cp = checkpoint(ledger_path, repo_root, ROLE_TESTER, message=None)  # 越界 → CHECKPOINT_REJECTED(exit 1)
        if payload["status"] == "pass":
            from .proof import validate_spec

            lg = ledger_mod.load(ledger_path)
            validate_spec(payload["proof_spec"], lg)  # 无效 → PROOF_SPEC_INVALID
        with ledger_mod.mutate(ledger_path) as lg2:
            files = _tester_files_from_checkpoints(lg2, repo_root)
            status = "pass" if (payload["status"] == "pass" and files) else "fail"
            details = {
                "result": payload["status"],
                "files": files,
                "declared_files": payload.get("files", []),
                "behaviors_covered": payload.get("behaviors_covered", []),
                "notes": payload.get("notes", ""),
                "checkpoint_head": cp["head"],
            }
            if payload["status"] == "pass" and not files:
                details["reason"] = "tester 未提交任何测试文件"
            lg2["proof_spec"] = payload.get("proof_spec") if status == "pass" else None
            rec = evidence.record(lg2, "tester", status, details, repo_root, agent_id=agent_id)
            readiness = evidence.readiness(lg2, repo_root)
        return {"recorded": "tester", "status": rec["status"], "files": files, "dependency_digest": rec["dependency_digest"], "readiness": readiness}

    with ledger_mod.mutate(ledger_path) as lg2:
        blocking = [f for f in payload.get("findings", []) if f.get("severity") == "blocking"]
        status = "pass" if payload["verdict"] == "pass" and not blocking else "fail"
        details = {
            "verdict": payload["verdict"],
            "findings": payload.get("findings", []),
            "behaviors_verified": payload.get("behaviors_verified", []),
            "reviewed_head": lg2["candidate"]["head"],
        }
        rec = evidence.record(lg2, "reviewer", status, details, repo_root, agent_id=agent_id)
        readiness = evidence.readiness(lg2, repo_root)
    return {"recorded": "reviewer", "status": rec["status"], "dependency_digest": rec["dependency_digest"], "readiness": readiness}


# ---------------------------------------------------------------- 上下文注入


def agent_context(lg: dict[str, Any], role: str, repo_root: Path) -> str:
    c = lg["contract"]
    m, a, s = c["mission"], c["authority"], c["assurance"]
    cand = lg["candidate"]
    lines = [
        f"[builder-loop] 你是本 run 的 {role}。run_id={lg['run_id']}",
        f"候选 worktree（唯一允许写入的位置）: {cand['worktree']}",
        f"主仓（只读）: {repo_root}；目标分支 {lg['repo']['target_branch']}；起点 {lg['repo']['target_start_head'][:12]}；候选 HEAD {(cand['head'] or '')[:12]}",
        f"Mission: {m['objective']}",
        "Behaviors:",
    ]
    for b in m["behaviors"]:
        lines.append(f"  - {b['id']}: given {b['given']} / when {b['when']} / then {b['then']}")
    if m.get("interfaces"):
        lines.append("Interfaces: " + "; ".join(m["interfaces"]))
    if m.get("trust_boundaries"):
        lines.append("Trust boundaries: " + "; ".join(m["trust_boundaries"]))
    if role == "tester":
        lines += [
            f"写边界 tester_write: {a['tester_write']}；禁止触碰 builder_write {a['builder_write']} 与 protected {a.get('protected_paths', [])}",
            f"machine 命令（供参考）: {[x['cmd'] for x in s.get('machine_commands', [])]}",
            f"允许的 proof kinds: {s.get('proof_kinds')}",
            "每个 behavior 恰好对应一个 proof group；baseline-red 只适用于起点上会 assertion 失败（而非 ImportError）的用例，新接口请用 mutation 并附 unified diff（只改 builder_write 内已有文件）。",
            "完成后 assistant 消息最后一行必须是单行：",
            TESTER_RESULT_FORMAT,
        ]
    else:
        ev = lg["evidence"]
        summary = {k: (ev[k]["status"] if ev.get(k) else None) for k in ("machine", "tester", "proof")}
        lines += [
            f"审查范围: 在候选 worktree 内 `git diff {lg['repo']['target_start_head'][:12]}..{(cand['head'] or 'HEAD')[:12]}`",
            f"前置 evidence: {summary}",
            f"review_focus: {s.get('review_focus', [])}",
            "只读审查，不修改任何文件。完成后 assistant 消息最后一行必须是单行：",
            REVIEWER_RESULT_FORMAT,
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------- Stop


ACTION_HINTS = {
    evidence.ACTION_CHECKPOINT: "在候选 worktree 完成实现后运行 `bl checkpoint --role builder --session <session>`",
    evidence.ACTION_MACHINE: "运行 `bl machine --session <session>`；FAIL 则按日志修复后重新 checkpoint",
    evidence.ACTION_SPAWN_TESTER: "spawn tester subagent（Agent subagent_type=tester，prompt 只需给 run_id）",
    evidence.ACTION_RESUME_TESTER: "用 SendMessage 续接已登记的 tester agent，让其修正测试/proof_spec",
    evidence.ACTION_PROOF: "运行 `bl proof --session <session>`",
    evidence.ACTION_SPAWN_REVIEWER: "spawn reviewer subagent（Agent subagent_type=reviewer，prompt 只需给 run_id）",
    evidence.ACTION_RESUME_REVIEWER: "修复后重新 checkpoint/machine/proof，再 SendMessage 续接已登记的 reviewer 复审",
    evidence.ACTION_FINALIZE: "运行 `bl finalize --session <session> -m '<commit message>'`",
    evidence.ACTION_NEEDS_USER: "存在 blocker，用 AskUserQuestion 让用户决定（继续 / `bl abandon --reason`）",
}


def handle_stop(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    lg = bound["ledger"]
    lp, root = bound["ledger_path"], bound["repo_root"]
    sid = ev.get("session_id")
    if ledger_mod.is_terminal(lg):
        ledger_mod.unbind_session(sid)
        return _silent()
    if lg.get("waiting_for_user"):
        return _silent()

    stall_hit = False
    with ledger_mod.mutate(lp) as lg2:
        stall = lg2["counters"]["stall"]
        if ev.get("stop_hook_active") and stall.get("seq_seen") == lg2["seq"]:
            stall["count"] = int(stall.get("count", 0)) + 1
        else:
            stall["count"] = 0
        stall["seq_seen"] = lg2["seq"] + 1  # mutate 退出后的 seq
        stall_hit = stall["count"] >= STALL_LIMIT
        readiness = evidence.readiness(lg2, root)
    if stall_hit:
        return ("", f"[builder-loop] run {lg['run_id']} 连续 {STALL_LIMIT} 次 Stop 之间没有任何 runtime 进展，停止续接；请用 AskUserQuestion 让用户决定。\n"), 0

    act = readiness["next_action"]
    hint = ACTION_HINTS.get(act, "").replace("<session>", str(sid))
    msg = (
        f"[builder-loop] run {lg['run_id']} 未完成，不能结束。\n"
        f"evidence: {readiness['states']}\n"
        f"next_action={act}: {hint}\n"
    )
    if readiness["blockers"]:
        msg += f"blockers: {dumps(readiness['blockers'])}\n"
    return _block(msg)


# ---------------------------------------------------------------- Subagent


def handle_subagent_start(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    role = ev.get("agent_type")
    if role not in ROLES:
        return _silent()
    lp, root = bound["ledger_path"], bound["repo_root"]
    with ledger_mod.mutate(lp) as lg:
        if ledger_mod.is_terminal(lg):
            return _silent()
        lg["agents"][role] = {"agent_id": ev.get("agent_id"), "started_at": ledger_mod.now_iso(), "stops": 0}
        ctx = agent_context(lg, role, root)
    return _json_out({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ctx}})


def handle_subagent_stop(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    role = ev.get("agent_type")
    if role not in ROLES:
        return _silent()
    lg = bound["ledger"]
    lp, root = bound["ledger_path"], bound["repo_root"]
    reg = lg["agents"].get(role) or {}
    if not reg or reg.get("agent_id") != ev.get("agent_id"):
        return _silent()
    fmt = TESTER_RESULT_FORMAT if role == "tester" else REVIEWER_RESULT_FORMAT

    payload = parse_result_marker(ev.get("last_assistant_message"))
    err = None if payload else "缺少结果标记"
    if payload:
        err = _validate_role_payload(role, payload)
    if err:
        return _retry_or_fail(lp, root, role, ev.get("agent_id"), f"{err}。最后一行必须是单行：{fmt}")
    try:
        record_role_result(lp, root, role, ev.get("agent_id"), payload)
    except Problem as exc:
        return _retry_or_fail(lp, root, role, ev.get("agent_id"), f"{exc.code}: {exc.message} {dumps(exc.details) if exc.details else ''}")
    return _silent()


def _retry_or_fail(lp: Path, root: Path, role: str, agent_id: str | None, reason: str) -> HookReturn:
    with ledger_mod.mutate(lp) as lg:
        reg = lg["agents"][role]
        reg["stops"] = int(reg.get("stops", 0)) + 1
        stops = reg["stops"]
        if stops > MALFORMED_RETRIES:
            evidence.record(lg, role, "fail", {"result": "malformed", "reason": reason}, root, agent_id=agent_id)
    if stops > MALFORMED_RETRIES:
        return ("", f"[builder-loop] {role} 结果连续 {stops} 次不合规，已记为 fail：{reason}\n"), 0
    return _block(f"[builder-loop] {role} 结果不合规（第 {stops} 次）：{reason}\n请修正后重新输出结果标记行。")


# ---------------------------------------------------------------- 工具类 hook


def handle_pre_tool_use(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    tool = ev.get("tool_name")
    lg = bound["ledger"]
    if ledger_mod.is_terminal(lg):
        return _silent()
    if tool == "AskUserQuestion":
        with ledger_mod.mutate(bound["ledger_path"]) as lg2:
            lg2["waiting_for_user"] = {"since": ledger_mod.now_iso(), "reason": "AskUserQuestion", "tool_use_id": ev.get("tool_use_id")}
        return _silent()
    if tool == "EnterWorktree":
        return _json_out({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"builder-loop run {lg['run_id']} 进行中：候选 worktree 已由 runtime 管理（{lg['candidate']['worktree']}），禁止 EnterWorktree。",
        }})
    if tool in ("Write", "Edit", "MultiEdit") and ev.get("agent_type") in ROLES:
        path = str((ev.get("tool_input") or {}).get("file_path", ""))
        wt = lg["candidate"]["worktree"]
        role = ev["agent_type"]
        reason = None
        if role == "reviewer":
            reason = "reviewer 只读，不允许写文件"
        elif not path.startswith(wt.rstrip("/") + "/"):
            reason = f"tester 只能写候选 worktree {wt} 内的路径"
        else:
            rel = path[len(wt.rstrip("/")) + 1:]
            auth = lg["contract"]["authority"]
            if contract_mod.path_in(auth.get("protected_paths", []), rel) or not contract_mod.path_in(auth["tester_write"], rel):
                reason = f"tester 只能写 tester_write={auth['tester_write']} 内的路径（{rel} 越界）"
        if reason:
            return _json_out({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": f"[builder-loop] {reason}"}})
    return _silent()


def _clear_waiting(bound: dict[str, Any]) -> HookReturn:
    lg = bound["ledger"]
    if lg.get("waiting_for_user") and not ledger_mod.is_terminal(lg):
        with ledger_mod.mutate(bound["ledger_path"]) as lg2:
            lg2["waiting_for_user"] = None
    return _silent()


def handle_post_tool_use(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    if ev.get("tool_name") == "AskUserQuestion":
        return _clear_waiting(bound)
    return _silent()


def handle_user_prompt_submit(ev: dict[str, Any], bound: dict[str, Any]) -> HookReturn:
    return _clear_waiting(bound)


HANDLERS = {
    "Stop": handle_stop,
    "SubagentStart": handle_subagent_start,
    "SubagentStop": handle_subagent_stop,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
    "UserPromptSubmit": handle_user_prompt_submit,
}


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
    _trace(event, ev, bound, result)
    return result


def _trace(event: str, ev: dict[str, Any], bound: dict[str, Any], result: HookReturn) -> None:
    """run 绑定时记一行诊断（agent_type / agent_id / 退出码），供 dogfood 时核对 hook 输入。"""
    try:
        path = ledger_mod.home_dir() / "hook-trace.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "at": ledger_mod.now_iso(), "event": event, "run_id": bound["ledger"]["run_id"],
            "tool_name": ev.get("tool_name"), "agent_type": ev.get("agent_type"), "agent_id": ev.get("agent_id"),
            "stop_hook_active": ev.get("stop_hook_active"), "exit": result[1],
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(dumps(line) + "\n")
    except OSError:
        pass
