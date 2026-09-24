"""B6: tester evidence 为 pass、proof 可以跑的 run，tester 被 builder 续接（resume_request 之后
SubagentStart，尚未交卷），evidence.role_running(lg,'tester') 为真时执行 `bl proof`：exit 1，
JSON 输出 ok=false、code=PROOF_TESTER_RUNNING；ledger 的 evidence.proof 与调用前相同，没有新增
proof 相关事件。边界：tester 交卷登记后（role_running 为假）`bl proof` 照常执行；reviewer 在跑
不影响 `bl proof`。不变量：tester 不在跑时 PROOF_PREREQ_TESTER / PROOF_PREREQ_INTEGRATE /
PROOF_BLOCKED 的既有判定与顺序不变。

覆盖对象：`bl proof` 入口（runtime/builder_loop/proof.py::run_proof）新增的
evidence.role_running(lg, "tester") 前置检查——冻结基线上不存在，下面对 code=PROOF_TESTER_RUNNING
的断言在起点代码上会在 call 阶段直接失败（baseline-red：起点上 `bl proof` 会真的往下跑）。
"""

from __future__ import annotations

from builder_loop import ledger as L
from conftest import drive_to_proof_pass, reviewer_pass, send_message


def test_b6_proof_blocked_while_tester_resumed_and_running(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    failures_before = len(L.load(started["ledger"])["failures"]["proof"])
    proof_evidence_before = L.load(started["ledger"])["evidence"]["proof"]
    proof_events_before = len([e for e in L.load(started["ledger"])["events"] if e["kind"].startswith("proof")])

    # 真续接：主会话先 SendMessage 再 SubagentStart，尚未交卷
    send_message(hook, "T1")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})

    out = cli("proof", "--session", "S1", expect=1)
    assert out.get("ok") is False, out
    assert out["code"] == "PROOF_TESTER_RUNNING", out

    lg = L.load(started["ledger"])
    assert lg["evidence"]["proof"] == proof_evidence_before
    assert len(lg["failures"]["proof"]) == failures_before
    assert len([e for e in lg["events"] if e["kind"].startswith("proof")]) == proof_events_before


def test_b6_boundary_proof_runs_normally_once_tester_registered_not_running(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out


def test_b6_boundary_reviewer_running_does_not_block_proof(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out


def test_b6_invariant_prereq_tester_check_unaffected_when_tester_not_running(started, cli, hook):
    """tester 必须真的『不在跑』：不能先 SubagentStart 注册一个从未交卷的 tester——那样它自己就是
    running（life 的最后一条生命周期事件是 role_start），会先触发 PROOF_TESTER_RUNNING，测不出
    这条不变量。tester 从未注册过（life 为空）才是『不在跑』且 evidence 仍为 missing 的干净场景。"""
    from conftest import implement_mul

    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    out = cli("proof", "--session", "S1", expect=1)
    assert out["code"] == "PROOF_PREREQ_TESTER", out
