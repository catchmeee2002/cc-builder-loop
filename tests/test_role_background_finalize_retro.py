"""B3：未停的 role_background 不再派生 retro 硬信号 S-role-background；后台任务账本降级为信息性提示。

起点代码在 `retro.derive_signals` 里为未停任务产出 id="S-role-background" 的信号，所以「输出中不存在
该信号」的断言在起点上 call 阶段失败（baseline-red）。

保留的行为（不变量 / 守卫，不列入 proof_spec）：`role_background` 事件照常记录；`bl status` /
`finalize` / `abandon` 的 `role_background_tasks` 信息字段照常派生（`evidence.role_background_tasks`
不删）；`S-role-wake` 仍按既有规则产生。
"""

from __future__ import annotations

from builder_loop import evidence
from builder_loop import ledger as L

from conftest import drive_to_proof_pass, reviewer_pass


def _bg_bash(hook, agent_type: str, agent_id: str, task_id: str = "bgx1", session: str = "S1"):
    return hook("PostToolUse", {"session_id": session, "agent_id": agent_id, "agent_type": agent_type,
                                "tool_name": "Bash", "tool_input": {"command": "sleep 300"},
                                "tool_response": {"backgroundTaskId": task_id, "timedOutAfterMs": 120000}})


def _wake(hook, role: str, agent_id: str, session: str = "S1"):
    return hook("SubagentStart", {"session_id": session, "agent_id": agent_id, "agent_type": role})


def _signal(sig, sid):
    return next((s for s in sig["signals"] if s["id"] == sid), None)


def _signal_ids(cli) -> list[str]:
    return [s["id"] for s in cli("retro", "signals", "--session", "S1")["signals"]]


def _tasks(started) -> list[dict]:
    return evidence.role_background_tasks(L.load(started["ledger"]))


# ---------------------------------------------------------------- B3：retro 不再有 S-role-background（baseline-red）


def test_b3_abandon_retro_omits_role_background_signal(started, cli, hook):
    """given 一个未销的 role_background(bgx1)、run 已 abandon / when `bl retro signals` / then
    输出中不存在 id=S-role-background（即使 role_background_tasks 仍列出 bgx1）。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1")

    out = cli("abandon", "--session", "S1", "--reason", "测试 retro 不再派生后台信号")
    assert any(t.get("task_id") == "bgx1" for t in (out.get("role_background_tasks") or [])), out

    assert "S-role-background" not in _signal_ids(cli), _signal_ids(cli)


def test_b3_finalize_retro_omits_role_background_signal(started, cli, hook):
    """走 finalize 这条终态路径同样不产生 S-role-background。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1")

    out = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul")
    assert out.get("terminal") == "finalized", out
    assert any(t.get("task_id") == "bgx1" for t in (out.get("role_background_tasks") or [])), out

    assert "S-role-background" not in _signal_ids(cli), _signal_ids(cli)


# ---------------------------------------------------------------- 不变量（守卫，不列入 proof_spec）


def test_guard_role_background_event_still_recorded_and_surfaced(started, cli, hook):
    """不变量：role_background 事件与 `bl status` 的 role_background_tasks 信息字段仍正常派生。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    _bg_bash(hook, "reviewer", "R1")

    evs = [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_background"]
    assert len(evs) == 1 and evs[0]["task_id"] == "bgx1", evs
    tasks = cli("status", "--session", "S1")["role_background_tasks"]
    assert any(t.get("task_id") == "bgx1" and t.get("role") == "reviewer" for t in tasks), tasks
    assert _tasks(started) == tasks


def test_guard_retro_keeps_role_wake_signal(started, cli, hook):
    """不变量：S-role-wake（交卷后再唤醒）信号仍按既有规则产生。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1")
    _wake(hook, "reviewer", "R1")

    cli("abandon", "--session", "S1", "--reason", "测试 role_wake 信号保留")
    sig = cli("retro", "signals", "--session", "S1")

    wake = _signal(sig, "S-role-wake")
    assert wake is not None, sig["signals"]
    assert wake["facts"].get("count") == 1, wake


def test_guard_finalize_output_lists_role_background_tasks(started, cli, hook):
    """不变量：finalize 输出的 role_background_tasks 信息字段仍列出未停任务。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1")

    out = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul")
    assert out.get("terminal") == "finalized", out
    tasks = out.get("role_background_tasks") or []
    assert any(t.get("task_id") == "bgx1" for t in tasks), out


def test_guard_abandon_output_lists_role_background_tasks(started, cli, hook):
    """不变量：abandon 的输出同样含 role_background_tasks（未停的 bgx1 依然列出）。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    _bg_bash(hook, "reviewer", "R1")

    out = cli("abandon", "--session", "S1", "--reason", "放弃测试")
    tasks = out.get("role_background_tasks") or []
    assert any(t.get("task_id") == "bgx1" for t in tasks), out


def test_guard_no_background_tasks_empty_list(started, cli, hook):
    """不变量：没有未停任务时 finalize 输出的 role_background_tasks 为空列表。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")

    out = cli("finalize", "--session", "S1", "-m", "feat(foo): [cr_id_skip] Add mul")
    assert out.get("role_background_tasks") == [], out