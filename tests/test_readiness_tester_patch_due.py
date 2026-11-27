"""B1/B2：readiness 的关键路径并行语义。

B1：首次 integrate 后 tester 证据为 pass、machine 未过、proof_spec 里有缺 patch 的 mutation 组、
tester 不在跑且最近一次结果不晚于首次 integrate 时，readiness 额外给出 `tester_patch_due=True`
（只是提示，`next_action` 仍是 machine）。起点没有这个字段，取它会 KeyError → call 阶段失败。

B2：tester 证据已经是 pass、它又被续接起来补 patch、machine 还没过时，应允许 builder 先去跑
machine，而不是一律 awaiting_tester。起点在 `elif tester_run` 分支直接给 awaiting_tester，所以
主场景在起点上断言失败（baseline-red）。

边界（tester 证据不是 pass 且在跑 → awaiting_tester；machine 已 pass 且在跑 → awaiting_tester；
tester 结果晚于首次 integrate → tester_patch_due=False；所有 mutation 组都有 patch → False）与
不变量（不新增枚举值；首轮未交卷仍 awaiting_tester）在起点上不成立或本来就成立，作为守卫留在
同一文件，不列入 proof group。
"""

from __future__ import annotations

from builder_loop import evidence
from builder_loop import ledger as L
from builder_loop import proof as P

from conftest import (
    implement_mul,
    make_tester_result,
    mutation_patch,
    role_turn,
    send_message,
    write_mul_test,
)


def _blind_tester_result(hook, kind: str = "mutation"):
    """tester 首轮盲写：mutation 组故意缺 patch。"""
    return role_turn(hook, "tester", "T1", make_tester_result(kind), start=False)


def _integrated_blind_state(started, cli, hook):
    """盲写 → builder 实现 → checkpoint → integrate；不跑 machine（machine 未过）。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = _blind_tester_result(hook)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    lg = L.load(started["ledger"])
    root = started["repo"].root
    # 前提自检
    assert evidence.state(lg, "tester", root) == evidence.STATE_PASS, lg["evidence"].get("tester")
    assert evidence.state(lg, "machine", root) != evidence.STATE_PASS, lg["evidence"].get("machine")
    assert evidence.missing_patch(lg) is True, lg.get("proof_spec")
    assert not evidence.role_running(lg, "tester")
    first_integrate = [cp for cp in lg["candidate"]["checkpoints"] if cp.get("role") == "integrate"][0]["at"]
    assert evidence.last_role_result_at(lg, "tester") <= first_integrate
    return lg, root


def _builder_checkpoint_without_tester_result(started, cli) -> None:
    """builder 已 checkpoint（has_builder_cp），tester 尚未交卷：此时 tester_run 分支才可达。"""
    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")


# ---------------------------------------------------------------- B1：tester_patch_due（baseline-red）


def test_b1_tester_patch_due_after_first_integrate(started, cli, hook):
    lg, root = _integrated_blind_state(started, cli, hook)
    out = evidence.readiness(lg, root)
    assert isinstance(out, dict), out
    assert out["tester_patch_due"] is True, out
    assert out["next_action"] == "machine", out


# ---------------------------------------------------------------- B2：tester 在跑但证据已 pass（baseline-red）


def test_b2_tester_pass_but_running_still_moves_to_machine(started, cli, hook):
    lg, root = _integrated_blind_state(started, cli, hook)
    # 真续接：主会话先 SendMessage 再 SubagentStart，role_running 才为真
    send_message(hook, "T1")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    lg = L.load(started["ledger"])
    assert evidence.role_running(lg, "tester") is True
    assert evidence.state(lg, "tester", root) == evidence.STATE_PASS
    assert evidence.state(lg, "machine", root) != evidence.STATE_PASS
    out = evidence.readiness(lg, root)
    assert out["next_action"] != "awaiting_tester", out
    assert out["next_action"] == "machine", out


# ---------------------------------------------------------------- B1：边界（守卫，不列入 proof group）


def test_b1_boundary_tester_not_pass_and_running_is_awaiting_tester(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    _builder_checkpoint_without_tester_result(started, cli)
    lg = L.load(started["ledger"])
    root = started["repo"].root
    assert evidence.role_running(lg, "tester") is True
    assert evidence.state(lg, "tester", root) != evidence.STATE_PASS
    out = evidence.readiness(lg, root)
    assert out["next_action"] == "awaiting_tester", out
    assert out["tester_patch_due"] is False, out


def test_b1_boundary_all_mutation_groups_have_patch(started, cli, hook):
    """machine 非 pass（还没跑）+ 全部 mutation 组 patch 齐备且打得到候选 + tester 未在 integrate 后答复。

    首轮盲写阶段也可以交一段针对 builder 文件的 diff（此时不做 patch 可应用性预检）；
    patch 用 `mutation_patch()` 生成的原始 git diff，保证 integrate 之后仍能 apply，
    不会触发 stale_patch_groups。这样 patch 齐备与「tester 最近一次结果不晚于首次 integrate」
    同时成立，且 machine 仍是 missing，不会把 machine 分支短路掉。
    """
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")

    lg = L.load(started["ledger"])
    root = started["repo"].root
    first_integrate = [cp for cp in lg["candidate"]["checkpoints"] if cp.get("role") == "integrate"][0]["at"]
    # 前提自检：machine 非 pass、patch 齐备且可应用、tester 未再答复
    assert evidence.state(lg, "machine", root) != evidence.STATE_PASS
    assert evidence.state(lg, "tester", root) == evidence.STATE_PASS
    assert evidence.missing_patch(lg) is False, lg.get("proof_spec")
    assert P.stale_patch_groups(lg, root) == [], lg.get("proof_spec")
    assert not evidence.role_running(lg, "tester")
    assert evidence.last_role_result_at(lg, "tester") <= first_integrate

    out = evidence.readiness(lg, root)
    assert out["tester_patch_due"] is False, out


def test_b1_boundary_tester_replied_after_first_integrate(started, cli, hook):
    lg, root = _integrated_blind_state(started, cli, hook)
    first_integrate = [cp for cp in lg["candidate"]["checkpoints"] if cp.get("role") == "integrate"][0]["at"]
    # 直接追加一条更晚的 tester 结果事件（tester 已就当前候选答复过）
    with L.mutate(started["ledger"]) as lg2:
        ev = L.log_event(lg2, "role_result", role="tester", agent_id="T1", status="declined", notes="patch 待补")
    lg = L.load(started["ledger"])
    assert ev["at"] > first_integrate
    assert evidence.last_role_result_at(lg, "tester") > first_integrate
    out = evidence.readiness(lg, root)
    assert out["tester_patch_due"] is False, out


def test_b1_boundary_machine_gate_running_keeps_patch_due(started, cli, hook, tmp_path):
    """machine 门禁正在跑时 next_action 为 awaiting_gate；tester_patch_due 不受影响。"""
    from keypath_helpers import hold_gate, release_hold

    lg, root = _integrated_blind_state(started, cli, hook)
    proc = hold_gate(lg, evidence.GATE_MACHINE, 1.5, tmp_path)
    try:
        out = evidence.readiness(L.load(started["ledger"]), root)
    finally:
        release_hold(proc)
    assert out["next_action"] == "awaiting_gate", out
    assert out["tester_patch_due"] is True, out


def test_b1_invariant_next_action_is_machine_when_machine_not_passed(started, cli, hook):
    """不变量：machine 未过且 tester 不在跑时 next_action 仍是 machine（不是新枚举值）。"""
    lg, root = _integrated_blind_state(started, cli, hook)
    out = evidence.readiness(lg, root)
    assert out["next_action"] == "machine", out


# ---------------------------------------------------------------- B2：边界 / 不变量（守卫）


def test_b2_boundary_tester_not_pass_and_running_is_awaiting_tester(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    _builder_checkpoint_without_tester_result(started, cli)
    lg = L.load(started["ledger"])
    out = evidence.readiness(lg, started["repo"].root)
    assert out["next_action"] == "awaiting_tester", out


def test_b2_boundary_machine_passed_and_tester_running_is_awaiting_tester(started, cli, hook):
    lg, root = _integrated_blind_state(started, cli, hook)
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    send_message(hook, "T1")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    lg = L.load(started["ledger"])
    assert evidence.state(lg, "machine", root) == evidence.STATE_PASS
    assert evidence.role_running(lg, "tester") is True
    out = evidence.readiness(lg, root)
    assert out["next_action"] == "awaiting_tester", out


def test_b2_invariant_first_round_tester_not_submitted_is_awaiting_tester(started, cli, hook):
    """不变量：首轮 tester 还没交卷（证据缺失、在跑，builder 已 checkpoint）时行为不变。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    _builder_checkpoint_without_tester_result(started, cli)
    lg = L.load(started["ledger"])
    assert evidence.state(lg, "tester", started["repo"].root) == evidence.STATE_MISSING
    out = evidence.readiness(lg, started["repo"].root)
    assert out["next_action"] == "awaiting_tester", out