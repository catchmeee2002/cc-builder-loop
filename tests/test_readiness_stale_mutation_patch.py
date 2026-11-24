"""B4：machine 为 pass、proof 未过、tester 已存在且不在跑，ledger 里某 mutation 组的 patch 在当前
候选 HEAD 上打不上（builder checkpoint 或 rebase 之后），且 tester 在最近一次候选变化之后没有答复过时，
evidence.readiness 的 next_action 应是 resume_tester（不是 proof）——把「patch 已失效」的拒绝条件
提前到 proof 之前，避免 tester 非要去跑注定 TEST_MUTATION_INVALID 的 proof。

起点代码在同样的输入上直接给 proof，所以主场景在起点上 call 阶段失败（baseline-red）。
patch 仍可打上 / tester 在跑 / tester 已答复 / machine 未过 / patch 为空——都是既有分支，
作为守卫留在同一文件（不列入 proof group）。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import brief as brief_mod
from builder_loop import evidence
from builder_loop import ledger as L
from builder_loop import proof as P

from conftest import implement_mul, make_tester_result, mutation_patch, role_turn, send_message, write_mul_test

CHANGED_MUL = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    total = a * b\n    return total\n"


def _reach_patch_registered(started, cli, hook):
    """tester 盲写（patch 缺省）→ builder 实现 → integrate → machine PASS → tester 补 patch。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "integrate"
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"  # 缺 patch
    patch = mutation_patch(wt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", patch))
    assert r["code"] == 0, r
    return wt, twt


def _make_patch_stale(started, cli):
    """builder 再改一版实现，动了旧 patch 的上下文；在候选新 HEAD 上重跑 machine 让它保持 pass。"""
    wt = started["worktree"]
    (wt / "src" / "foo.py").write_text(CHANGED_MUL)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1")["result"] == "PASS"


def _stale_patch_run(started, cli, hook):
    wt, twt = _reach_patch_registered(started, cli, hook)
    _make_patch_stale(started, cli)
    lg = L.load(started["ledger"])
    root = started["repo"].root
    assert evidence.state(lg, "machine", root) == evidence.STATE_PASS, lg["evidence"].get("machine")
    assert evidence.state(lg, "proof", root) != evidence.STATE_PASS, lg["evidence"].get("proof")
    assert not evidence.role_running(lg, "tester"), "前提：tester 不在跑"
    assert P.stale_patch_groups(lg, root) == ["B1"], lg.get("proof_spec")
    return lg, root


# ---------------------------------------------------------------- B4：正向（起点上 call 阶段失败）


def test_b4_stale_patch_without_reply_resumes_tester(started, cli, hook):
    lg, root = _stale_patch_run(started, cli, hook)
    out = evidence.readiness(lg, root)
    assert out["next_action"] == "resume_tester", out
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"


# ---------------------------------------------------------------- B4：边界 / 不变量（守卫）


def test_b4_boundary_all_patches_apply_keeps_proof(started, cli, hook):
    _reach_patch_registered(started, cli, hook)
    lg = L.load(started["ledger"])
    root = started["repo"].root
    assert P.stale_patch_groups(lg, root) == []
    assert evidence.readiness(lg, root)["next_action"] == "proof", evidence.readiness(lg, root)


def test_b4_boundary_tester_running_stays_awaiting_tester(started, cli, hook):
    lg, root = _stale_patch_run(started, cli, hook)
    # 真续接：主会话先 SendMessage 再触发 SubagentStart，才按生命周期事件判定 tester 仍在跑
    send_message(hook, "T1")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    lg = L.load(started["ledger"])
    assert evidence.role_running(lg, "tester")
    assert evidence.readiness(lg, root)["next_action"] == "awaiting_tester", evidence.readiness(lg, root)


def test_b4_boundary_tester_replied_after_change_falls_back_to_proof(started, cli, hook):
    """tester 在最近一次候选变化之后已答复过（即使 patch 仍打不上）：不再给 resume_tester，避免死循环。"""
    _, root = _stale_patch_run(started, cli, hook)
    r = role_turn(hook, "tester", "T1", {"role": "tester", "status": "insufficient_spec",
                                         "behaviors_covered": ["B1"], "notes": "patch 待重建"}, start=False)
    assert r["code"] == 0, r
    lg = L.load(started["ledger"])
    assert P.stale_patch_groups(lg, root) == ["B1"]
    assert evidence.last_role_result_at(lg, "tester") > lg["candidate"]["checkpoints"][-1]["at"]
    assert evidence.readiness(lg, root)["next_action"] == "proof", evidence.readiness(lg, root)


def test_b4_boundary_machine_not_passed_keeps_existing_action(started, cli, hook):
    """machine 未过时不做这项判断：候选变化让 machine evidence 失效，readiness 仍走 machine 分支。"""
    _reach_patch_registered(started, cli, hook)
    (started["worktree"] / "src" / "foo.py").write_text(CHANGED_MUL)
    cli("checkpoint", "--session", "S1", "--role", "builder")  # 不重跑 machine：evidence 变 stale
    lg = L.load(started["ledger"])
    root = started["repo"].root
    assert P.stale_patch_groups(lg, root) == ["B1"]
    assert evidence.readiness(lg, root)["next_action"] == "machine", evidence.readiness(lg, root)


def test_b4_boundary_empty_patch_group_not_part_of_stale_detection(started, cli, hook):
    """只有 kind=mutation 且 patch 非空的组参与判断；patch 为空的组走既有的 add_mutation_patch 出口。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)  # patch 空
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    lg = L.load(started["ledger"])
    root = started["repo"].root
    assert P.stale_patch_groups(lg, root) == []
    assert evidence.readiness(lg, root)["next_action"] == "resume_tester", evidence.readiness(lg, root)


def test_b4_invariant_brief_stale_mutation_patch_todo_preserved(started, cli, hook):
    lg, root = _stale_patch_run(started, cli, hook)
    b = brief_mod.build(lg, root, "tester")
    stale = [t for t in b["todo"] if t.get("what") == "stale_mutation_patch"]
    assert stale and stale[0]["behaviors"] == ["B1"], b["todo"]
    assert "stale_mutation_patch" in brief_mod.render(b)