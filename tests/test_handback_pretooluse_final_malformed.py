"""B4: 同一角色本轮已被 PreToolUse deny 了 MALFORMED_RETRIES(=2) 次后，再交一份不合规的
SubagentHandback：PreToolUse 不再 deny（静默或仅 stderr 提示，exit 0），ledger 追加一条
role_malformed(final=true) 且该角色 evidence 记为 fail；随后这一轮的 PostToolUse(SubagentHandback)
不再追加 role_result 也不追加 role_malformed。边界：final 之后该角色被续接开新一轮，新一轮的不合规
计数重新从 1 开始并照常 deny。

覆盖对象：hooks.py 新增的 PreToolUse(SubagentHandback) 分支与既有 `_retry_or_fail` 的
MALFORMED_RETRIES 上限共用——冻结基线上 Pre 从不 deny，下面的核心断言在起点代码上会在 call
阶段直接失败（baseline-red）。
"""

from __future__ import annotations

from builder_loop import evidence
from builder_loop import ledger as L
from conftest import (handback, implement_mul, marker, pre_handback,
                      send_message, write_mul_test)


def _tester_ready(started, cli, hook) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(started["tester_worktree"])


def _malformed(started):
    return [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_malformed"]


def _results(started):
    return [e for e in L.load(started["ledger"])["events"] if e["kind"] == "role_result"]


def test_b4_third_malformed_final_silent_and_marks_fail(started, cli, hook):
    _tester_ready(started, cli, hook)
    r1 = pre_handback(hook, "tester", "T1", "不合规1")
    r2 = pre_handback(hook, "tester", "T1", "不合规2")
    for r in (r1, r2):
        j = r["json"] or {}
        assert (j.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny", r
    assert len(_malformed(started)) == 2
    assert all(e["final"] is False for e in _malformed(started))

    r3 = pre_handback(hook, "tester", "T1", "不合规3")
    j3 = r3["json"] or {}
    assert (j3.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny", r3

    lg = L.load(started["ledger"])
    mal = [e for e in lg["events"] if e["kind"] == "role_malformed"]
    assert len(mal) == 3 and mal[-1]["final"] is True, mal
    assert evidence.state(lg, "tester", started["repo"].root) == evidence.STATE_FAIL


def test_b4_post_after_final_adds_nothing(started, cli, hook):
    _tester_ready(started, cli, hook)
    for _ in range(3):
        pre_handback(hook, "tester", "T1", "不合规")
    n_events = len(L.load(started["ledger"])["events"])

    post = handback(hook, "tester", "T1", "还是不合规")
    assert post["code"] == 0, post
    assert len(L.load(started["ledger"])["events"]) == n_events, "final 之后同轮 Post 不应新增任何事件"
    assert not _results(started)


def test_b4_boundary_new_turn_after_final_restarts_count_from_one(started, cli, hook):
    _tester_ready(started, cli, hook)
    for _ in range(3):
        pre_handback(hook, "tester", "T1", "不合规")
    assert [e["final"] for e in _malformed(started)] == [False, False, True]

    send_message(hook, "T1")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = pre_handback(hook, "tester", "T1", "新一轮还是不合规")
    j = r["json"] or {}
    assert (j.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny", r
    mal = _malformed(started)
    assert mal[-1]["final"] is False, mal


def test_b4_invariant_subagent_stop_silent_when_final_already_malformed(started, cli, hook):
    """不变量：SubagentStop 在本轮已有 final malformed 时静默的既有行为不变。"""
    _tester_ready(started, cli, hook)
    for _ in range(3):
        pre_handback(hook, "tester", "T1", "不合规")
    n_events = len(L.load(started["ledger"])["events"])
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester",
                              "last_assistant_message": marker({"role": "tester", "status": "insufficient_spec", "notes": "x"})})
    assert r["code"] == 0 and r["stderr"] == "", r
    assert len(L.load(started["ledger"])["events"]) == n_events
