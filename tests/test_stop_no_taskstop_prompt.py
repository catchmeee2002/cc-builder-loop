"""B6：Stop 阻断文案不再给 TaskStop 清理行动指令，也不再追加 leftover 后台任务清理段。

run 未完成、存在未销 role_background 时，起点代码的 Stop 文案含「角色留下了后台任务……逐个用
TaskStop 停掉 bgx1（tester）」；本 run 把后台账本降级为信息性提示，清理动作指令与该段一并移除。
「文案不含 TaskStop」的断言因此起点上 call 阶段失败（baseline-red）。

保留的行为（边界 / 不变量，守卫不列入 proof_spec）：run 未完成时 Stop 仍 exit 2 并给出
next_action；TaskStop 的 PreToolUse 与终态记账（role_background_stopped 事件）保留。
"""

from __future__ import annotations

from builder_loop import evidence
from builder_loop import ledger as L

from conftest import implement_mul, make_tester_result, role_turn, write_mul_test


def _bg_bash(hook, agent_type: str, agent_id: str, task_id: str = "bgx1", session: str = "S1"):
    return hook("PostToolUse", {"session_id": session, "agent_id": agent_id, "agent_type": agent_type,
                                "tool_name": "Bash", "tool_input": {"command": "sleep 300"},
                                "tool_response": {"backgroundTaskId": task_id, "timedOutAfterMs": 120000}})


def _tester_left_unstopped_task(started, cli, hook) -> None:
    """tester 交卷（缺 patch，run 未完成）、留下一个未停角色后台任务。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    _bg_bash(hook, "tester", "T1")


# ---------------------------------------------------------------- B6（baseline-red）


def test_b6_stop_message_has_no_taskstop_cleanup_instruction(started, cli, hook):
    """given run 未完成、tester 留了一个未停后台任务 / when 主 session 触发 Stop / then 阻断文案
    （stderr）不含 TaskStop 清理指令。"""
    _tester_left_unstopped_task(started, cli, hook)

    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2, r
    assert "TaskStop" not in r["stderr"], r["stderr"]


def test_b6_stop_message_has_no_leftover_background_section(started, cli, hook):
    """不再追加 leftover 后台任务清理段：文案里没有残留段与任务 id。"""
    _tester_left_unstopped_task(started, cli, hook)

    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2, r
    assert "角色留下了后台任务" not in r["stderr"], r["stderr"]
    assert "bgx1" not in r["stderr"], r["stderr"]


# ---------------------------------------------------------------- 边界 / 不变量（守卫，不列入 proof_spec）


def test_guard_stop_still_blocks_with_next_action(started, cli, hook):
    """边界：run 未完成时 Stop 仍正常阻断并给出 next_action（即便有未停后台任务）。"""
    _tester_left_unstopped_task(started, cli, hook)

    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2, r
    assert "next_action=" in r["stderr"], r["stderr"]


def test_guard_stop_without_background_still_blocks(started, cli, hook):
    """边界：没有后台任务时 Stop 的阻断行为不变。"""
    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2, r
    assert "next_action=" in r["stderr"], r["stderr"]


def test_guard_taskstop_pretooluse_still_records_stopped(started, cli, hook):
    """不变量：主会话 PreToolUse(TaskStop) 仍把记录在案的任务记成 role_background_stopped，
    信息字段随之清空（TaskStop 的记账通道保留，供信息字段派生）。"""
    _tester_left_unstopped_task(started, cli, hook)
    assert any(t["task_id"] == "bgx1" for t in evidence.role_background_tasks(L.load(started["ledger"])))

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "bgx1"}})
    assert out["code"] == 0, out
    assert out["json"] is None, out

    stopped = [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_background_stopped"]
    assert len(stopped) == 1 and stopped[0]["task_id"] == "bgx1", stopped
    assert evidence.role_background_tasks(L.load(started["ledger"])) == []