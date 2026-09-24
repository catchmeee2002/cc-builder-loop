"""B1：角色被残留后台任务唤醒（无主会话 SendMessage 续接）时，SubagentStart/Stop/handback 只记
`role_wake`，不记 `role_start` / `role_result`，evidence 与 turn 都保持唤醒前原样，additionalContext 不再注入。

B2：主会话确有 SendMessage 续接某个已登记 agent_id 时，先记 `resume_request`，随后的
SubagentStart 才是真正的新 turn（`role_start`，turn +1），角色结果照常登记进 evidence。

覆盖对象：runtime/builder_loop/hooks.py 的 handle_subagent_start / handle_pre_tool_use（新增
SendMessage 分支）与 handle_subagent_stop / handle_handback（唤醒轮次不登记结果）。
"""

from __future__ import annotations

import builder_loop.ledger as L
from conftest import handback, implement_mul, make_tester_result, marker, send_message, write_mul_test

REVIEW_PASS = {"role": "reviewer", "verdict": "pass", "findings": [], "behaviors_verified": ["B1"]}


def _start(hook, role: str, agent_id: str) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": role})


def _events(started, kind: str, role: str | None = None) -> list[dict]:
    lg = L.load(started["ledger"])
    return [e for e in lg["events"] if e["kind"] == kind and (role is None or e.get("role") == role)]


def _evidence(started, role: str) -> dict | None:
    return L.load(started["ledger"])["evidence"].get(role)


def _reviewer_ready_and_passed(started, cli, hook, agent_id: str = "R1") -> None:
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    _start(hook, "reviewer", agent_id)
    r = handback(hook, "reviewer", agent_id, marker(REVIEW_PASS))
    assert r["code"] == 0, r
    assert _evidence(started, "reviewer")["status"] == "pass"


def _tester_ready_and_passed(started, cli, hook, agent_id: str = "T1") -> None:
    _start(hook, "tester", agent_id)
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(started["tester_worktree"])
    r = handback(hook, "tester", agent_id, marker(make_tester_result("mutation")))
    assert r["code"] == 0, r
    assert _evidence(started, "tester")["status"] == "pass"


# ================================================================== B1


def test_b1_wake_via_subagent_start_no_new_role_start_or_result(started, cli, hook):
    """given reviewer R1 已登记结论、无 SendMessage / when 再收到 SubagentStart(R1) / then 只新增
    role_wake，不新增 role_start / role_result；evidence.reviewer 与 agents.reviewer.turn 不变；
    additionalContext 不再注入。"""
    _reviewer_ready_and_passed(started, cli, hook)
    before_lg = L.load(started["ledger"])
    before_ev = before_lg["evidence"]["reviewer"]
    before_turn = before_lg["agents"]["reviewer"]["turn"]
    n_start_before = len(_events(started, "role_start", "reviewer"))
    n_result_before = len(_events(started, "role_result", "reviewer"))

    r = _start_and_capture(hook, "reviewer", "R1")

    wakes = _events(started, "role_wake", "reviewer")
    assert len(wakes) == 1 and wakes[-1].get("agent_id") == "R1", wakes
    assert len(_events(started, "role_start", "reviewer")) == n_start_before, "唤醒不应新增 role_start"
    assert len(_events(started, "role_result", "reviewer")) == n_result_before, "唤醒不应新增 role_result"

    after_lg = L.load(started["ledger"])
    assert after_lg["evidence"]["reviewer"] == before_ev, "evidence.reviewer 不应因唤醒改变"
    assert after_lg["agents"]["reviewer"]["turn"] == before_turn, "agents.reviewer.turn 不应因唤醒改变"
    out = r["json"] or {}
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext")
    assert not ctx, "唤醒轮次的 SubagentStart 不应注入 additionalContext"


def _start_and_capture(hook, role, agent_id):
    return hook("SubagentStart", {"session_id": "S1", "agent_id": agent_id, "agent_type": role})


def test_b1_boundary_wake_via_handback_same_result_not_recorded(started, cli, hook):
    """边界：唤醒轮次里用 PostToolUse(SubagentHandback) 重发同样的结果行 → 同样不登记，不新增
    role_result。"""
    _reviewer_ready_and_passed(started, cli, hook)
    n_result_before = len(_events(started, "role_result", "reviewer"))

    _start_and_capture(hook, "reviewer", "R1")  # 唤醒
    r2 = handback(hook, "reviewer", "R1", marker(REVIEW_PASS))
    assert r2["code"] == 0, r2
    assert len(_events(started, "role_result", "reviewer")) == n_result_before, "唤醒轮次重发同样结果不应新增 role_result"


def test_b1_boundary_stale_evidence_stays_stale_after_wake_and_resend(started, cli, hook):
    """边界：唤醒前候选 HEAD 已经 checkpoint 前进（reviewer evidence 变 stale）→ 唤醒并重发 pass
    之后，evidence.reviewer 仍为 stale，next_action 不变成 finalize。"""
    _reviewer_ready_and_passed(started, cli, hook)
    wt = started["worktree"]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# moved\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    from builder_loop import evidence as EV

    lg = L.load(started["ledger"])
    assert EV.state(lg, "reviewer", started["repo"].root) == EV.STATE_STALE

    _start_and_capture(hook, "reviewer", "R1")  # 唤醒（不是续接）
    r2 = handback(hook, "reviewer", "R1", marker(REVIEW_PASS))
    assert r2["code"] == 0, r2

    lg2 = L.load(started["ledger"])
    assert EV.state(lg2, "reviewer", started["repo"].root) == EV.STATE_STALE, "唤醒重发不能把 stale 结论刷新成 fresh pass"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] != "finalize"


def test_b1_boundary_wake_turn_malformed_last_message_no_new_malformed(started, cli, hook):
    """边界：唤醒轮次的最后一条消息不合规（没有结果行）→ 不新增 role_malformed，
    agents.reviewer.stops 不变。"""
    _reviewer_ready_and_passed(started, cli, hook)
    stops_before = L.load(started["ledger"])["agents"]["reviewer"]["stops"]
    n_mal_before = len(_events(started, "role_malformed", "reviewer"))

    _start_and_capture(hook, "reviewer", "R1")  # 唤醒
    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer",
                              "last_assistant_message": "已交付，没有结果行。"})
    assert r["code"] == 0, r

    assert len(_events(started, "role_malformed", "reviewer")) == n_mal_before
    assert L.load(started["ledger"])["agents"]["reviewer"]["stops"] == stops_before


def test_b1_boundary_tester_wake_same_semantics_no_worktree_commit(started, cli, hook):
    """边界：tester 被唤醒同理——SubagentStart 本身不新增 role_result；唤醒轮次里即便真的收到一条
    带结果行的 handback（内容与已登记的那次相同），也同样不登记：不新增 role_result、
    evidence.tester 不变，且不提交 tester worktree（worktree 里放一份未提交的改动，唤醒 +
    重发结果之后仍未被 checkpoint/提交——若被当成真续接处理，登记 tester 结果时会先 checkpoint
    整个 worktree，这份改动就会被提交掉）。"""
    _tester_ready_and_passed(started, cli, hook)
    before_ev = L.load(started["ledger"])["evidence"]["tester"]
    n_result_before = len(_events(started, "role_result", "tester"))

    (started["tester_worktree"] / "tests" / "test_dangling.py").write_text(
        "def test_dangling():\n    assert True\n", encoding="utf-8"
    )
    from conftest import git

    dirty_before = git(started["tester_worktree"], "status", "--porcelain")
    assert dirty_before

    _start_and_capture(hook, "tester", "T1")  # 唤醒：本身不应新增 role_start / role_result
    assert len(_events(started, "role_result", "tester")) == n_result_before
    assert L.load(started["ledger"])["evidence"]["tester"] == before_ev
    assert git(started["tester_worktree"], "status", "--porcelain") == dirty_before, "唤醒不应提交 tester worktree"

    # 唤醒轮次里真的收到一条带结果行的 handback（与已登记的那次结果相同）→ 同样不登记
    r2 = handback(hook, "tester", "T1", marker(make_tester_result("mutation")))
    assert r2["code"] == 0, r2
    assert len(_events(started, "role_result", "tester")) == n_result_before, "唤醒轮次重发 handback 不应新增 role_result"
    assert L.load(started["ledger"])["evidence"]["tester"] == before_ev
    assert git(started["tester_worktree"], "status", "--porcelain") == dirty_before, "唤醒轮次的 handback 不应提交 tester worktree"


def test_b1_boundary_running_false_after_wake(started, cli, hook):
    """边界：唤醒之后 `bl status` 的 running.reviewer 为 false。"""
    _reviewer_ready_and_passed(started, cli, hook)
    _start_and_capture(hook, "reviewer", "R1")  # 唤醒
    assert cli("status", "--session", "S1")["running"]["reviewer"] is False


def test_b1_invariant_unregistered_agent_id_still_records_role_start(started, cli, hook):
    """不变量：未登记的 agent_id 首次 SubagentStart 照旧记 role_start 并注入上下文。"""
    r = hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    starts = _events(started, "role_start", "tester")
    assert len(starts) == 1 and starts[0]["agent_id"] == "T1"
    out = r["json"] or {}
    assert (out.get("hookSpecificOutput") or {}).get("additionalContext"), "首次 spawn 必须注入上下文"


def test_b1_invariant_different_agent_id_still_records_role_replaced(started, cli, hook):
    """不变量：不同 agent_id 顶替照旧记 role_replaced。"""
    _reviewer_ready_and_passed(started, cli, hook)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R2", "agent_type": "reviewer"})
    replaced = _events(started, "role_replaced", "reviewer")
    assert len(replaced) == 1 and replaced[0]["old_agent_id"] == "R1" and replaced[0]["new_agent_id"] == "R2", replaced


# ================================================================== B2


def test_b2_resume_request_then_role_start_new_turn_records_result(started, cli, hook):
    """given R1 已登记 / when 主会话先 PreToolUse(SendMessage, to=R1)，再 SubagentStart(R1) 与带
    结果行的 SubagentStop / then ledger 新增 resume_request(agent_id=R1)，随后 role_start（turn+1）
    与 role_result，结论照旧登记进 evidence.reviewer。"""
    _reviewer_ready_and_passed(started, cli, hook)
    turn_before = L.load(started["ledger"])["agents"]["reviewer"]["turn"]

    sm = send_message(hook, "R1")
    assert sm["code"] == 0, sm
    reqs = _events(started, "resume_request")
    assert len(reqs) == 1 and reqs[-1].get("agent_id") == "R1", reqs

    _start_and_capture(hook, "reviewer", "R1")
    starts = _events(started, "role_start", "reviewer")
    assert starts[-1]["turn"] == turn_before + 1, starts

    r = hook("SubagentStop", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer",
                              "last_assistant_message": marker(REVIEW_PASS)})
    assert r["code"] == 0, r
    assert L.load(started["ledger"])["evidence"]["reviewer"]["status"] == "pass"


def test_b2_boundary_second_start_without_new_request_is_wake(started, cli, hook):
    """边界：一条 resume_request 只对应一次 SubagentStart：同一请求之后第二次无请求的
    SubagentStart → role_wake。"""
    _reviewer_ready_and_passed(started, cli, hook)
    send_message(hook, "R1")
    _start_and_capture(hook, "reviewer", "R1")
    n_start = len(_events(started, "role_start", "reviewer"))

    _start_and_capture(hook, "reviewer", "R1")  # 第二次，没有新的 SendMessage
    assert len(_events(started, "role_start", "reviewer")) == n_start, "同一条 resume_request 不能撑起第二次 role_start"
    wakes = _events(started, "role_wake", "reviewer")
    assert len(wakes) == 1


def test_b2_boundary_to_not_a_registered_agent_id_no_resume_request(started, cli, hook):
    """边界：SendMessage 的 to 不是任何已登记角色的 agent_id → 不记 resume_request。"""
    _reviewer_ready_and_passed(started, cli, hook)
    send_message(hook, "NOBODY")
    assert _events(started, "resume_request") == []


def test_b2_boundary_send_message_from_role_itself_no_resume_request(started, cli, hook):
    """边界：PreToolUse(SendMessage) 来自带 agent_id 的角色自身 → 不记 resume_request。"""
    _reviewer_ready_and_passed(started, cli, hook)
    hook("PreToolUse", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer",
                        "tool_name": "SendMessage", "tool_input": {"to": "R1"}})
    assert _events(started, "resume_request") == []


def test_b2_invariant_send_message_pretooluse_is_not_denied(started, cli, hook):
    """不变量：PreToolUse(SendMessage) 的 hook 放行（不 deny）。"""
    _reviewer_ready_and_passed(started, cli, hook)
    r = send_message(hook, "R1")
    assert r["code"] == 0
    out = r["json"] or {}
    assert (out.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny"
