#!/usr/bin/env python3
"""Codex lifecycle gates for the builder-loop adapter.

This hook records agent lifecycle events through the runtime ledger and enforces the
active-run and terminal retrospective completion gates. It never owns phases or writes
runtime state directly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "runtime"))

from codex_builder_loop import lifecycle as lifecycle_delivery  # noqa: E402


ROLE_MARKERS = {
    "tester": "TESTER_RESULT:",
    "reviewer": "REVIEW_RESULT:",
}
ROLE_RESULTS = {
    "tester": {"tests_ready", "pass", "fail", "target_change_required", "blocked"},
    "reviewer": {"pass", "findings", "blocked"},
}
WARNING_STATUSES = {
    "NEEDS_USER",
    "CONFLICT",
    "CONTINUITY_FAILURE",
    "FATAL",
    "FATAL_AMBIGUOUS",
}


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False))


def runtime_path() -> str | None:
    configured = os.environ.get("BUILDER_LOOP_CLI")
    if configured:
        return configured
    return shutil.which("codex-builder-loop")


def run_runtime(*args: str, cwd: str) -> tuple[dict[str, Any] | None, str | None]:
    cli = runtime_path()
    if not cli:
        return None, "codex-builder-loop 不在 PATH"
    try:
        completed = subprocess.run(
            [cli, *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            env={**os.environ, "BUILDER_LOOP_HOOK_EVENT": "1"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"builder-loop runtime 调用失败: {exc}"

    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        detail = completed.stderr.strip() or f"exit={completed.returncode}"
        return None, f"builder-loop runtime 未返回 JSON: {detail}"
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None, "builder-loop runtime 最后一行不是合法 JSON"
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return None, "builder-loop runtime JSON 缺少 status"
    return payload, None


def extra_context(event: str, text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }


def session_start(event: dict[str, Any]) -> None:
    session_id = str(event.get("session_id") or "")
    context = (
        "Builder-loop Codex adapter 已启用。此 SessionStart context 只提供 adapter 状态，"
        "不构成 Builder-loop 启动授权；是否加载 Builder Skill 和启动 run 必须服从适用 "
        "AGENTS 中的 Codex Builder Loop 授权契约。根线程是协调者和 builder-owned "
        "实现写入者；tester 与 reviewer 必须使用各自 custom agent。"
    )
    if session_id:
        context += f" 当前 session_id={session_id}；$builder 调 start 时必须原样传入。"
    else:
        context += " 当前缺少 session_id；不要启动 builder-loop run。"
    if not runtime_path():
        context += " 当前找不到 codex-builder-loop CLI，禁止声称 loop 已启动。"
    emit(extra_context("SessionStart", context))


def agent_event(
    event: dict[str, Any], lifecycle: str, result_value: str | None = None
) -> tuple[str, str | None]:
    role = str(event.get("agent_type") or "")
    agent_id = str(event.get("agent_id") or "")
    turn_id = str(event.get("turn_id") or "")
    session_id = str(event.get("session_id") or "")
    if role not in ROLE_MARKERS:
        return role, None
    if not agent_id or not turn_id or not session_id:
        return role, "builder-loop agent hook 缺少 agent_id、turn_id 或 session_id"

    try:
        envelope, event_path = lifecycle_delivery.enqueue_event(
            session_id=session_id,
            role=role,
            agent_id=agent_id,
            turn_id=turn_id,
            event=lifecycle,
            result=result_value,
        )
        if envelope is None or event_path is None:
            return role, "LIFECYCLE_RECEIPT_MISSING: lifecycle event was not persisted"
    except lifecycle_delivery.LifecycleDeliveryError as exc:
        return role, f"{exc.code}: {exc}"
    return role, None


def subagent_start(event: dict[str, Any]) -> None:
    role, warning = agent_event(event, "start")
    marker = ROLE_MARKERS.get(role)
    if not marker:
        emit({})
        return
    text = (
        f"你的 builder-loop 身份是 {role}。只执行该 custom agent 契约，"
        f"最后一行必须输出 {marker} 对应结果。"
    )
    result = extra_context("SubagentStart", text)
    if warning:
        result["systemMessage"] = warning
    emit(result)


def subagent_stop(event: dict[str, Any]) -> None:
    role = str(event.get("agent_type") or "")
    marker = ROLE_MARKERS.get(role)
    message = str(event.get("last_assistant_message") or "")
    already_continued = bool(event.get("stop_hook_active"))
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    result_value: str | None = None
    if marker and lines and lines[-1].startswith(marker):
        candidate = lines[-1][len(marker) :].strip()
        if candidate in ROLE_RESULTS[role] and lines[-1] == f"{marker} {candidate}":
            result_value = candidate

    if marker and result_value is None and not already_continued:
        # SubagentStop decision:block continues the same subagent flow. Do not use
        # continue:false here; current Codex semantics make that stop take precedence.
        emit(
            {
                "decision": "block",
                "reason": f"补全 {role} 契约，并在最后一行输出 {marker}。",
            }
        )
        return

    if marker and result_value is None:
        _role, warning = agent_event(event, "closed")
        emit(
            {
                "systemMessage": warning
                or f"{role} 在 continuation 后仍未输出合法完成标记；run 已进入 continuity failure。"
            }
        )
        return

    _role, warning = agent_event(event, "idle", result_value)
    if warning and not already_continued:
        emit(
            {
                "decision": "block",
                "reason": (
                    "builder-loop 尚未可靠接收本轮角色完成事件；"
                    f"请保持同一 turn 并再次输出 {marker} {result_value}。原因：{warning}"
                ),
            }
        )
        return
    result: dict[str, Any] = {}
    if warning:
        result["systemMessage"] = warning
    emit(result)


def stop(event: dict[str, Any]) -> None:
    cwd = str(event.get("cwd") or os.getcwd())
    session_id = str(event.get("session_id") or "")
    already_continued = bool(event.get("stop_hook_active"))
    last_message = str(event.get("last_assistant_message") or "")
    if not session_id:
        emit({"systemMessage": "Builder-loop Stop hook 缺少 session_id，已安全放行。"})
        return

    retrospective, retrospective_error = run_runtime(
        "assurance",
        "--experimental-v4",
        "retrospective-status",
        "--repo",
        cwd,
        "--session-id",
        session_id,
        cwd=cwd,
    )
    if retrospective_error:
        emit({"systemMessage": retrospective_error})
        return
    retrospective_status = str(retrospective.get("status"))
    is_retrospective_payload = isinstance(
        retrospective.get("owner_session_id"), str
    ) or bool(str(retrospective.get("code") or "").strip())
    if is_retrospective_payload and retrospective_status != "NOOP":
        message = str(retrospective.get("message") or retrospective_status)
        if retrospective_status == "ACTIVE":
            run_id = str(retrospective.get("run_id") or "")
            input_marker = f"BUILDER_INPUT_REQUIRED:{run_id}" if run_id else ""
            waiting_for_user = bool(input_marker) and any(
                line.strip() == input_marker for line in last_message.splitlines()
            )
            if waiting_for_user:
                emit(
                    {
                        "systemMessage": (
                            "Builder-loop 正在等待用户决定，已保留 active run 并放行本轮停止。"
                        )
                    }
                )
                return
            if not already_continued:
                emit({"decision": "block", "reason": message})
                return
            emit({"systemMessage": "Builder-loop 仍为 ACTIVE；已避免 Stop hook 自循环。"})
            return
        required_user_block = retrospective.get("required_user_block")
        required_block = (
            str(required_user_block)
            if isinstance(required_user_block, str) and required_user_block
            else str(retrospective.get("required_block") or "")
        )
        surfaced = bool(required_block) and required_block in last_message
        if retrospective_status in {"NEEDS_USER", "READY"} and surfaced:
            active_payload, active_error = run_runtime(
                "status",
                "--repo",
                cwd,
                "--session-id",
                session_id,
                cwd=cwd,
            )
            if active_error:
                emit({"systemMessage": active_error})
                return
            if str(active_payload.get("status")) == "ACTIVE":
                if retrospective_status == "NEEDS_USER":
                    emit(
                        {
                            "systemMessage": (
                                "Builder-loop 复盘与 active run 均在等待用户决定，已保留现场。"
                            )
                        }
                    )
                    return
                emit(
                    {
                        "decision": "block",
                        "reason": str(
                            active_payload.get("message")
                            or "Builder-loop run remains active"
                        ),
                    }
                )
                return
            emit({})
            return
        reason = required_block or message
        emit({"decision": "block", "reason": reason})
        return

    payload, error = run_runtime(
        "status",
        "--repo",
        cwd,
        "--session-id",
        session_id,
        cwd=cwd,
    )
    if error:
        emit({"systemMessage": error})
        return

    status = str(payload.get("status"))
    message = str(payload.get("message") or status)
    run_id = str(payload.get("run_id") or "")
    input_marker = f"BUILDER_INPUT_REQUIRED:{run_id}" if run_id else ""
    waiting_for_user = bool(input_marker) and any(
        line.strip() == input_marker for line in last_message.splitlines()
    )
    if status == "ACTIVE" and waiting_for_user:
        emit({"systemMessage": "Builder-loop 正在等待用户决定，已保留 active run 并放行本轮停止。"})
        return
    if status == "ACTIVE" and not already_continued:
        # Stop decision:block creates a continuation prompt. continue:false would stop
        # the turn and is intentionally not used for this completion gate.
        emit({"decision": "block", "reason": message})
        return

    if status == "ACTIVE":
        emit({"systemMessage": "Builder-loop 仍为 ACTIVE；已避免 Stop hook 自循环。"})
        return
    if status in WARNING_STATUSES:
        emit({"systemMessage": message})
        return
    emit({})


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        emit({"systemMessage": "Builder-loop hook 输入不是合法 JSON，已安全放行。"})
        return 0

    name = str(event.get("hook_event_name") or event.get("hookEventName") or "")
    if name == "SessionStart":
        session_start(event)
    elif name == "SubagentStart":
        subagent_start(event)
    elif name == "SubagentStop":
        subagent_stop(event)
    elif name == "Stop":
        stop(event)
    else:
        emit({})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
