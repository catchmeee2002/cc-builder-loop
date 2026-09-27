"""B6：主会话（无 agent_id）的 PreToolUse(TaskStop) 也要能把角色留下的后台任务标记为已停——
CC 有时不会为 TaskStop 触发 PostToolUse（"No task found"），只在 PreToolUse 里能拦到这次调用。
hookspec.py 的 PreToolUse 注册要包含 TaskStop（doctor / install 从它派生）。
"""

from __future__ import annotations

from builder_loop import ledger as L
from builder_loop.hookspec import HOOK_SPEC


def _bg_bash(hook, agent_type: str, agent_id: str, task_id: str, session: str = "S1"):
    return hook("PostToolUse", {
        "session_id": session, "agent_id": agent_id, "agent_type": agent_type, "tool_name": "Bash",
        "tool_input": {"command": "sleep 300"},
        "tool_response": {"backgroundTaskId": task_id, "timedOutAfterMs": 120000},
    })


def _events(started, kind: str) -> list[dict]:
    return [e for e in L.load(started["ledger"])["events"] if e["kind"] == kind]


def test_hookspec_pretooluse_registers_taskstop():
    pretools = [m for ev, m, _ in HOOK_SPEC if ev == "PreToolUse" and m]
    matched = [m for m in pretools if "TaskStop" in m.split("|")]
    assert matched, pretools


def test_pretooluse_taskstop_from_main_session_stops_background_task(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = _bg_bash(hook, "tester", "T1", "t1")
    assert r["code"] == 0, r
    assert any(t["task_id"] == "t1" for t in cli("status", "--session", "S1")["role_background_tasks"])

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "t1"}})
    assert out["code"] == 0, out

    assert not any(t["task_id"] == "t1" for t in cli("status", "--session", "S1")["role_background_tasks"])
    stopped = [e for e in _events(started, "role_background_stopped") if e.get("task_id") == "t1"]
    assert len(stopped) == 1, stopped


def test_boundary_pretooluse_taskstop_with_agent_id_not_recorded(started, cli, hook):
    """带 agent_id（角色自己发起）的 TaskStop 不登记：这只是给 builder 用的主会话通道。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    _bg_bash(hook, "tester", "T1", "t2")
    hook("PreToolUse", {"session_id": "S1", "agent_id": "T1", "tool_name": "TaskStop", "tool_input": {"task_id": "t2"}})
    assert any(t["task_id"] == "t2" for t in cli("status", "--session", "S1")["role_background_tasks"])
    assert _events(started, "role_background_stopped") == []


def test_boundary_pretooluse_taskstop_unknown_task_id_not_recorded(started, hook):
    before = len(L.load(started["ledger"])["events"])
    hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "unknown"}})
    assert len(L.load(started["ledger"])["events"]) == before


def test_boundary_pretooluse_then_posttooluse_taskstop_not_double_recorded(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    _bg_bash(hook, "tester", "T1", "t3")
    hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "t3"}})
    hook("PostToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "t3"}})
    stopped = [e for e in _events(started, "role_background_stopped") if e.get("task_id") == "t3"]
    assert len(stopped) == 1, stopped


def test_invariant_posttooluse_taskstop_alone_still_works(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    _bg_bash(hook, "tester", "T1", "t4")
    r = hook("PostToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "t4"}})
    assert r["code"] == 0, r
    assert not any(t["task_id"] == "t4" for t in cli("status", "--session", "S1")["role_background_tasks"])
