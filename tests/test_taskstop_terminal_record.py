"""B1 / B2：run 到终态（finalize 或 abandon，复盘尚未记录）后，builder 在主会话里发起的
TaskStop 意图照常记账——把它记成 `role_background_stopped`；非主会话（带 agent_id）、陌生 id、
缺 task_id/shell_id 的 TaskStop 依旧不记账。

B1 的判据：终态 run 上主会话 PreToolUse(TaskStop) 退出码 0、无 permissionDecision，ledger 恰好追加
一条 role_background_stopped，随后 role_background_tasks 为空、retro signals 不再有 S-role-background。
起点代码在终态分支里对非 AskUserQuestion 的 PreToolUse 一律静默返回，TaskStop 根本到不了记账逻辑，
所以 B1 的每条正向断言在起点上都会在 call 阶段失败（baseline-red）。

B2 的判据：不属于「主会话停一个未停任务」的 TaskStop 一律不追加事件。这些在起点上本来就成立
（baseline 会静默返回 / 走既有 _role_background_stopped 守卫），因此 B2 走 mutation：把候选里的
记账守卫去掉后它们必须变红。
"""

from __future__ import annotations

from builder_loop import evidence
from builder_loop import ledger as L

from conftest import drive_to_proof_pass, reviewer_pass


def _bg_bash(hook, agent_type: str, agent_id: str, task_id: str, session: str = "S1"):
    return hook("PostToolUse", {
        "session_id": session, "agent_id": agent_id, "agent_type": agent_type, "tool_name": "Bash",
        "tool_input": {"command": "sleep 300"},
        "tool_response": {"backgroundTaskId": task_id, "timedOutAfterMs": 120000},
    })


def _stopped(started, task_id: str | None = None) -> list[dict]:
    evs = [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_background_stopped"]
    return [e for e in evs if task_id is None or e.get("task_id") == task_id]


def _tasks(started) -> list[dict]:
    return evidence.role_background_tasks(L.load(started["ledger"]))


def _signal_ids(cli) -> list[str]:
    return [s["id"] for s in cli("retro", "signals", "--session", "S1")["signals"]]


def _abandoned_with_task(started, cli, hook, task_id: str = "T1", *extra: str):
    """非终态阶段由 reviewer 留下未停后台任务（终态后 Bash 不再记账），再 abandon 到终态（retro 尚未记录）。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    for tid in (task_id, *extra):
        _bg_bash(hook, "reviewer", "R1", tid)
    expected = {task_id, *extra}
    assert {t["task_id"] for t in _tasks(started)} == expected, _tasks(started)
    cli("abandon", "--session", "S1", "--reason", "测试终态 TaskStop 记账")


# ---------------------------------------------------------------- B1：终态主会话 TaskStop 记账（baseline-red）


def test_b1_abandoned_run_main_session_taskstop_records_stopped(started, cli, hook):
    _abandoned_with_task(started, cli, hook, "T1")

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "T1"}})
    assert out["code"] == 0, out
    assert out["json"] is None, out  # 没有 permissionDecision

    stopped = _stopped(started, "T1")
    assert len(stopped) == 1, stopped
    assert _tasks(started) == [], _tasks(started)
    assert "S-role-background" not in _signal_ids(cli), _signal_ids(cli)


def test_b1_finalized_run_main_session_taskstop_records_stopped(started, cli, hook):
    """终态是 finalized 时同样生效。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1", "T1")
    assert any(t["task_id"] == "T1" for t in _tasks(started)), _tasks(started)
    out_fin = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul")
    assert out_fin.get("terminal") == "finalized", out_fin

    r = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "T1"}})
    assert r["code"] == 0, r
    assert r["json"] is None, r

    assert len(_stopped(started, "T1")) == 1, _stopped(started, "T1")
    assert _tasks(started) == [], _tasks(started)
    assert "S-role-background" not in _signal_ids(cli), _signal_ids(cli)


def test_b1_terminal_taskstop_shell_id_alias_records_stopped(started, cli, hook):
    """边界：tool_input 用 shell_id 代替 task_id 同样记账。"""
    _abandoned_with_task(started, cli, hook, "T1")

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"shell_id": "T1"}})
    assert out["code"] == 0, out
    assert out["json"] is None, out
    assert len(_stopped(started, "T1")) == 1, _stopped(started, "T1")
    assert _tasks(started) == [], _tasks(started)


def test_b1_terminal_second_taskstop_not_double_recorded(started, cli, hook):
    """边界：同一个 T1 之后再来一次 PreToolUse 或 PostToolUse(TaskStop) 不再追加事件。"""
    _abandoned_with_task(started, cli, hook, "T1")

    hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "T1"}})
    assert len(_stopped(started, "T1")) == 1, _stopped(started, "T1")  # 首次主会话 PreToolUse 就该记账
    hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "T1"}})
    hook("PostToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "T1"}})

    assert len(_stopped(started, "T1")) == 1, _stopped(started, "T1")


def test_b1_terminal_only_named_task_stopped_leaves_other_and_signal(started, cli, hook):
    """边界：ledger 里有 T1、T2 两个未停任务时只停 T1：T2 仍在列表，S-role-background 仍存在。"""
    _abandoned_with_task(started, cli, hook, "T1", "T2")
    assert {t["task_id"] for t in _tasks(started)} == {"T1", "T2"}, _tasks(started)

    hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "T1"}})

    assert {t["task_id"] for t in _tasks(started)} == {"T2"}, _tasks(started)
    assert len(_stopped(started, "T1")) == 1, _stopped(started, "T1")
    assert "S-role-background" in _signal_ids(cli), _signal_ids(cli)


# ---------------------------------------------------------------- B2：不属于主会话停未停任务的 TaskStop（mutation）


def test_b2_nonterminal_taskstop_with_agent_id_not_recorded(started, cli, hook):
    """非终态：带 agent_id（角色自己停）不记账，也不拦。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    _bg_bash(hook, "reviewer", "R1", "T1")
    before = len(L.load(started["ledger"])["events"])

    out = hook("PreToolUse", {"session_id": "S1", "agent_id": "R1", "tool_name": "TaskStop", "tool_input": {"task_id": "T1"}})
    assert out["code"] == 0, out
    assert out["json"] is None, out
    assert len(L.load(started["ledger"])["events"]) == before
    assert any(t["task_id"] == "T1" for t in _tasks(started)), _tasks(started)


def test_b2_nonterminal_taskstop_unknown_id_not_recorded(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    _bg_bash(hook, "reviewer", "R1", "T1")
    before = len(L.load(started["ledger"])["events"])

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "nope"}})
    assert out["code"] == 0, out
    assert out["json"] is None, out
    assert len(L.load(started["ledger"])["events"]) == before


def test_b2_nonterminal_taskstop_without_task_id_not_recorded(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    _bg_bash(hook, "reviewer", "R1", "T1")
    before = len(L.load(started["ledger"])["events"])

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {}})
    assert out["code"] == 0, out
    assert out["json"] is None, out
    assert len(L.load(started["ledger"])["events"]) == before


def test_b2_terminal_taskstop_with_agent_id_not_recorded(started, cli, hook):
    """终态：带 agent_id 依然不记账。"""
    _abandoned_with_task(started, cli, hook, "T1")
    before = len(L.load(started["ledger"])["events"])

    out = hook("PreToolUse", {"session_id": "S1", "agent_id": "R1", "tool_name": "TaskStop", "tool_input": {"task_id": "T1"}})
    assert out["code"] == 0, out
    assert out["json"] is None, out
    assert len(L.load(started["ledger"])["events"]) == before
    assert any(t["task_id"] == "T1" for t in _tasks(started)), _tasks(started)


def test_b2_terminal_taskstop_unknown_id_not_recorded(started, cli, hook):
    _abandoned_with_task(started, cli, hook, "T1")
    before = len(L.load(started["ledger"])["events"])

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "nope"}})
    assert out["code"] == 0, out
    assert out["json"] is None, out
    assert len(L.load(started["ledger"])["events"]) == before


def test_b2_terminal_taskstop_without_task_id_not_recorded(started, cli, hook):
    _abandoned_with_task(started, cli, hook, "T1")
    before = len(L.load(started["ledger"])["events"])

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": ""}})
    assert out["code"] == 0, out
    assert out["json"] is None, out
    assert len(L.load(started["ledger"])["events"]) == before


def test_b2_boundary_terminal_taskstop_without_any_background_not_recorded(started, cli, hook):
    """边界：终态 run 且 ledger 里没有任何 role_background 时，TaskStop 同样不追加事件。"""
    cli("abandon", "--session", "S1", "--reason", "无后台任务")
    before = len(L.load(started["ledger"])["events"])

    out = hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "T9"}})
    assert out["code"] == 0, out
    assert out["json"] is None, out
    assert len(L.load(started["ledger"])["events"]) == before


def test_b2_boundary_unknown_id_does_not_touch_other_unstopped_tasks(started, cli, hook):
    """边界：T1 之外另有未停任务时，陌生 id 不影响它们的列表。"""
    _abandoned_with_task(started, cli, hook, "T1", "T2")
    before = len(L.load(started["ledger"])["events"])

    hook("PreToolUse", {"session_id": "S1", "tool_name": "TaskStop", "tool_input": {"task_id": "stranger"}})

    assert {t["task_id"] for t in _tasks(started)} == {"T1", "T2"}, _tasks(started)
    assert len(L.load(started["ledger"])["events"]) == before