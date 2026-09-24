"""B4：finalize / retro signals / Stop 拉回都要能看见「角色留下的未停后台任务」与「角色被唤醒」，
不能因为唤醒被误当续接而挡住 finalize（呼应 B1：唤醒不冒充续接）。

覆盖对象：runtime/builder_loop/finalize.py::finalize / run.py::abandon 的输出新增
role_background_tasks；runtime/builder_loop/retro.py::derive_signals 新增 S-role-background /
S-role-wake；hooks.py::handle_stop 的拉回消息提及未停任务。
"""

from __future__ import annotations

from conftest import drive_to_proof_pass, reviewer_pass


def _bg_bash(hook, agent_type: str, agent_id: str, task_id: str = "bgx1", session: str = "S1"):
    return hook("PostToolUse", {"session_id": session, "agent_id": agent_id, "agent_type": agent_type,
                                "tool_name": "Bash", "tool_input": {"command": "sleep 300"},
                                "tool_response": {"backgroundTaskId": task_id, "timedOutAfterMs": 120000}})


def _wake(hook, role: str, agent_id: str, session: str = "S1"):
    return hook("SubagentStart", {"session_id": session, "agent_id": agent_id, "agent_type": role})


def _signal(sig, sid):
    return next((s for s in sig["signals"] if s["id"] == sid), None)


def test_b4_finalize_succeeds_and_lists_unstopped_background_task(started, cli, hook):
    """given 一个未停的 role_background(bgx1)、另有一次 role_wake（唤醒不冒充续接）/ when
    `bl finalize` / then 成功（不被阻塞），输出含 role_background_tasks 且列出 bgx1。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1")
    _wake(hook, "reviewer", "R1")  # 唤醒：不应把 reviewer 变成"在跑"从而挡住 finalize

    out = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul")
    assert out.get("terminal") == "finalized", out
    tasks = out.get("role_background_tasks") or []
    assert any(t.get("task_id") == "bgx1" for t in tasks), out


def test_b4_retro_signals_include_role_background_and_role_wake(started, cli, hook):
    """given 同上、run 已 abandon（另一条到终态的路） / when `bl retro signals` / then 含
    id=S-role-background（facts 里列出 bgx1）与 id=S-role-wake（count=1）。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1")
    _wake(hook, "reviewer", "R1")

    cli("abandon", "--session", "S1", "--reason", "测试 retro 信号")
    sig = cli("retro", "signals", "--session", "S1")

    bg = _signal(sig, "S-role-background")
    assert bg is not None, sig["signals"]
    assert "bgx1" in (bg["facts"].get("task_ids") or bg["facts"].get("tasks") or []) or "bgx1" in str(bg["facts"]), bg

    wake = _signal(sig, "S-role-wake")
    assert wake is not None, sig["signals"]
    assert wake["facts"].get("count") == 1, wake


def test_b4_boundary_no_background_or_wake_signals_absent_and_empty_list(started, cli, hook):
    """边界：没有未停任务、也没有唤醒 → 不出现这两个信号，finalize 输出的 role_background_tasks
    为空列表。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")

    out = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul")
    assert out.get("role_background_tasks") == [], out


def test_b4_boundary_abandon_output_also_has_role_background_tasks(started, cli, hook):
    """边界：abandon 的输出同样含 role_background_tasks（未停的 bgx1 依然列出）。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1")

    out = cli("abandon", "--session", "S1", "--reason", "放弃测试")
    tasks = out.get("role_background_tasks") or []
    assert any(t.get("task_id") == "bgx1" for t in tasks), out


def test_b4_stop_pullback_mentions_unstopped_task_and_taskstop(started, cli, hook):
    """given tester 交卷、候选未集成之前留了一个未停的后台任务 / when 触发 Stop（run 未完成，
    next_action 需要 builder 动手）/ then 拉回消息（stderr）含 bgx1 与 TaskStop。"""
    from conftest import implement_mul, make_tester_result, role_turn, write_mul_test

    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    _bg_bash(hook, "tester", "T1")

    r = hook("Stop", {"session_id": "S1", "stop_hook_active": False})
    assert r["code"] == 2, r
    assert "bgx1" in r["stderr"] and "TaskStop" in r["stderr"], r["stderr"]
