"""B5: PreToolUse(SubagentHandback) 只在"已登记角色、非唤醒轮次、run 未终态"时校验并可能 deny；
来自未登记 agent_id / 唤醒轮次 / run 已 finalize-abandon 时一律静默放行，不产生任何新事件。
tester 交卷时 test_ids 引用的文件已写在 worktree 里但尚未提交：不因"文件在提交里不存在"被
deny（PostToolUse 登记时照常提交并记 role_result）；但引用的文件在 worktree 里也不存在时，
deny 且 reason 指出该文件。

覆盖对象：hooks.py 新增的 PreToolUse(SubagentHandback) 分支要在工作树（而非已提交的 tester_head）
上检查 test_ids 引用的文件是否存在——冻结基线上没有这项检查，下面标记为 baseline-red 的断言在
起点代码上会在 call 阶段直接失败。
"""

from __future__ import annotations

from builder_loop import ledger as L
from conftest import (handback, implement_mul, make_tester_result, marker,
                      pre_handback, send_message, write_mul_test)


def _events(started):
    return L.load(started["ledger"])["events"]


def _tester_ready(started, cli, hook) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(started["tester_worktree"])


def test_b5_unregistered_agent_id_silent(started, cli, hook):
    n = len(_events(started))
    r = pre_handback(hook, "tester", "GHOST", marker(make_tester_result("mutation")))
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r
    assert len(_events(started)) == n


def test_b5_wake_turn_silent(started, cli, hook):
    """已登记角色但当前是唤醒轮次（最近开轮事件是 role_wake）→ PreToolUse(SubagentHandback) 静默。"""
    _tester_ready(started, cli, hook)
    assert handback(hook, "tester", "T1", marker(make_tester_result("mutation")))["code"] == 0
    # 残留后台任务把它唤醒：没有 SendMessage 续接就再来一次 SubagentStart → role_wake
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    wake = [e for e in _events(started) if e["kind"] == "role_wake"]
    assert wake, _events(started)
    n = len(_events(started))
    r = pre_handback(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r
    assert len(_events(started)) == n, "唤醒轮次里的结论不应产生任何新事件"


def test_b5_terminal_run_silent(started, cli, hook):
    cli("abandon", "--session", "S1", "--reason", "test cleanup")
    n = len(_events(started))
    r = pre_handback(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r["code"] == 0 and r["stdout"] == "" and r["stderr"] == "", r
    assert len(_events(started)) == n


def test_b5_uncommitted_test_files_in_worktree_not_denied(started, cli, hook):
    """第四种：test_ids 引用的测试文件已写在 tester worktree 里但尚未提交 → 不因「文件在提交里
    不存在」被 deny；PostToolUse 登记时照常提交这些文件并记 role_result。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(started["tester_worktree"])  # 写了但还没 checkpoint/commit
    payload = make_tester_result("mutation")
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    j = pre["json"] or {}
    assert (j.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny", pre

    post = handback(hook, "tester", "T1", marker(payload))
    assert post["code"] == 0, post
    res = [e for e in _events(started) if e["kind"] == "role_result"]
    assert res and res[-1]["status"] == "pass", res


def test_b5_boundary_test_id_file_missing_in_worktree_denied(started, cli, hook):
    """边界：test_ids 引用的测试文件在 tester worktree 工作树里也不存在 → deny，reason 指出该文件。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    payload = make_tester_result("mutation", test_ids=["tests/test_never_written.py::test_x"])
    pre = pre_handback(hook, "tester", "T1", marker(payload))
    j = pre["json"] or {}
    hso = j.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "deny", pre
    reason = hso.get("permissionDecisionReason") or ""
    assert "tests/test_never_written.py" in reason, reason
