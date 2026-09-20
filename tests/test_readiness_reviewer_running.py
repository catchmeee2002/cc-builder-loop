"""B14: reviewer 在 required 内且 evidence 为 fresh pass，但按生命周期事件判定它仍在跑
（最近一条角色事件是 role_start 且租约未过期）时，readiness.next_action 应为 awaiting_reviewer，
不是 finalize——避免在 reviewer 可能还有后续输出时就把 run 收尾。
"""

from __future__ import annotations

from builder_loop import evidence
from builder_loop import ledger as L
from conftest import drive_to_proof_pass, reviewer_pass


def test_b14_reviewer_pass_but_running_blocks_finalize(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook, "R1")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "finalize"  # 边界：pass 且不在跑 → finalize

    # 续接/再次 SubagentStart：按生命周期事件判定 reviewer 仍在跑，evidence 仍是 fresh pass
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    lg = L.load(started["ledger"])
    assert evidence.state(lg, "reviewer", started["repo"].root) == evidence.STATE_PASS
    assert evidence.role_running(lg, "reviewer")
    assert evidence.readiness(lg, started["repo"].root)["next_action"] == "awaiting_reviewer"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "awaiting_reviewer"


def test_b14_boundary_reviewer_fail_and_running_stays_awaiting(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    r = hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    finding = {"severity": "blocking", "owner": "builder", "file": "src/foo.py", "line": 1, "summary": "x"}
    from conftest import role_turn

    role_turn(hook, "reviewer", "R1", {"role": "reviewer", "verdict": "changes_requested", "findings": [finding], "behaviors_verified": []}, start=False)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    lg = L.load(started["ledger"])
    assert evidence.state(lg, "reviewer", started["repo"].root) == evidence.STATE_FAIL
    assert evidence.readiness(lg, started["repo"].root)["next_action"] == "awaiting_reviewer"


def test_b14_invariant_tester_running_check_unaffected(started, cli, hook):
    """不变量：tester 在跑时的既有判定不变——builder 干完后等 tester 交卷，不是被 B14 的 reviewer 逻辑打断。"""
    from conftest import implement_mul

    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "awaiting_tester"
