"""B3：角色的 Bash 命令超时被 CC 转成后台任务（PostToolUse 携带 backgroundTaskId）时，runtime 记
一条 role_background 事件、`bl status` 的 role_background_tasks 里能看到它，并提醒角色以后给足
timeout；主会话用 PostToolUse(TaskStop) 显式停掉后，任务从列表里消失。

result-channel run 的 B8：提醒措辞改成冻结锚句——不再说「交卷后它会把你再唤醒」（这句话在
handback 环境下不成立：等它结束时唤醒你的那一轮里交的结论根本不会被登记），改为明确告诉角色
别等它、这一轮就把结论交上来。role_background 事件的记录与 TaskStop 标记停止的行为不变。

覆盖对象：runtime/builder_loop/hooks.py 的 handle_post_tool_use 新增 Bash / TaskStop 分支，
runtime/builder_loop/evidence.py::role_background_tasks，`bl status` 的 role_background_tasks 字段，
以及 ROLE_BACKGROUND_HINT 的措辞（B8）。
"""

from __future__ import annotations

import builder_loop.ledger as L
from conftest import implement_mul

BG_SENTENCE = ("这条命令超时后被转到了后台。别等它：这一轮就把结论交上来，等它结束时唤醒你的那一轮里交的结论不会被登记。"
              "需要它的结果就给足 timeout 在前台重跑，或缩小命令范围；这个后台任务会由 builder 停掉。")
OLD_BG_PHRASE = "交卷后它会把你再唤醒"


def _reviewer_ready(started, cli, hook, agent_id: str = "R1") -> None:
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": "reviewer"})


def _bg_bash(hook, agent_type: str | None, agent_id: str | None, task_id: str = "bgx1",
            timeout_ms: int = 120000, session: str = "S1"):
    ev = {"session_id": session, "tool_name": "Bash", "tool_input": {"command": "sleep 300"},
          "tool_response": {"backgroundTaskId": task_id, "timedOutAfterMs": timeout_ms}}
    if agent_id is not None:
        ev["agent_id"] = agent_id
    if agent_type is not None:
        ev["agent_type"] = agent_type
    return hook("PostToolUse", ev)


def _task_stop(hook, task_id: str, session: str = "S1"):
    return hook("PostToolUse", {"session_id": session, "tool_name": "TaskStop", "tool_input": {"task_id": task_id}})


def _events(started, kind: str) -> list[dict]:
    return [e for e in L.load(started["ledger"])["events"] if e["kind"] == kind]


def test_b3_reviewer_background_task_recorded_and_surfaced(started, cli, hook):
    """given reviewer R1 已登记 / when 收到 PostToolUse(Bash) 带 backgroundTaskId+timedOutAfterMs /
    then 新增 role_background 事件；bl status 的 role_background_tasks 含它；additionalContext 含
    提醒句子。"""
    _reviewer_ready(started, cli, hook)
    r = _bg_bash(hook, "reviewer", "R1")

    evs = _events(started, "role_background")
    assert len(evs) == 1, evs
    assert evs[0]["role"] == "reviewer" and evs[0]["agent_id"] == "R1" and evs[0]["task_id"] == "bgx1", evs[0]

    tasks = cli("status", "--session", "S1")["role_background_tasks"]
    assert any(t.get("task_id") == "bgx1" and t.get("role") == "reviewer" for t in tasks), tasks

    out = r["json"] or {}
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert BG_SENTENCE in ctx, ctx


def test_b8_boundary_old_you_will_be_rewoken_phrase_gone(started, cli, hook):
    """B8 边界：不再含「交卷后它会把你再唤醒」——handback 环境下这句话不成立。"""
    _reviewer_ready(started, cli, hook)
    r = _bg_bash(hook, "reviewer", "R1")
    out = r["json"] or {}
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert OLD_BG_PHRASE not in ctx, ctx


def test_b3_boundary_no_background_task_id_is_silent(started, cli, hook):
    """边界：tool_response 没有 backgroundTaskId → 不记事件，hook 输出为空。"""
    _reviewer_ready(started, cli, hook)
    r = hook("PostToolUse", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer",
                             "tool_name": "Bash", "tool_input": {"command": "echo hi"},
                             "tool_response": {"success": True}})
    assert _events(started, "role_background") == []
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r


def test_b3_boundary_main_session_background_is_silent(started, cli, hook):
    """边界：主会话（无 agent_id）的 Bash 带 backgroundTaskId → 不记事件。"""
    _reviewer_ready(started, cli, hook)
    r = _bg_bash(hook, None, None)
    assert _events(started, "role_background") == []
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r


def test_b3_boundary_task_stop_removes_from_list(started, cli, hook):
    """边界：此后主会话 PostToolUse(TaskStop, task_id=bgx1) → 新增 role_background_stopped 事件，
    role_background_tasks 不再含 bgx1。"""
    _reviewer_ready(started, cli, hook)
    _bg_bash(hook, "reviewer", "R1")
    assert any(t.get("task_id") == "bgx1" for t in cli("status", "--session", "S1")["role_background_tasks"])

    r = _task_stop(hook, "bgx1")
    assert r["code"] == 0, r
    stopped = _events(started, "role_background_stopped")
    assert len(stopped) == 1 and stopped[0]["task_id"] == "bgx1", stopped
    assert not any(t.get("task_id") == "bgx1" for t in cli("status", "--session", "S1")["role_background_tasks"])


def test_b3_boundary_task_stop_unknown_task_id_is_silent(started, cli, hook):
    """边界：TaskStop 的 task_id 不是任何已记录的角色任务 → 不记事件。"""
    _reviewer_ready(started, cli, hook)
    _bg_bash(hook, "reviewer", "R1")
    n = len(L.load(started["ledger"])["events"])
    r = _task_stop(hook, "not-a-real-task")
    assert r["code"] == 0, r
    assert len(L.load(started["ledger"])["events"]) == n, "未知 task_id 不应新增事件"


def test_b3_invariant_role_run_in_background_still_denied(started, cli, hook):
    """不变量：角色显式 run_in_background 的 PreToolUse 拒绝保持不变。"""
    _reviewer_ready(started, cli, hook)
    out = hook("PreToolUse", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer",
                              "tool_name": "Bash", "tool_input": {"command": "sleep 300", "run_in_background": True}})
    assert out["json"]["hookSpecificOutput"]["permissionDecision"] == "deny"
