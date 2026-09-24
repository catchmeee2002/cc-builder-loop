"""B7: 已集成的 run，ledger.proof_spec 中 B_x 的 mutation 组 patch 非空，builder 之后 checkpoint
改动了该 patch 上下文所在的行使它打不到新的候选 HEAD 时，`bl brief`（或 brief.build 取 tester
brief）的 todo 中要有一项 what=stale_mutation_patch，behaviors 恰为打不上的那些组的 behavior id，
candidate_head 等于当前候选 HEAD；文本输出里也要出现 "stale_mutation_patch"。边界：所有 patch
都能打上时没有这一项；patch 为空的组只出现在 add_mutation_patch，不出现在 stale_mutation_patch；
候选对 tester 尚不可读时（首次集成前）没有这一项。

覆盖对象：brief.py::_tester_todo 新增分支——冻结基线上不存在，下面对 stale_mutation_patch 的
断言在起点代码上会在 call 阶段直接失败（baseline-red）。
"""

from __future__ import annotations

from builder_loop import brief as brief_mod
from builder_loop import ledger as L
from conftest import implement_mul, make_tester_result, mutation_patch, role_turn, write_mul_test


def _ready_with_patch(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    patch = mutation_patch(wt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", patch))
    assert r["code"] == 0, r
    return wt, twt


def _tester_brief_todo(started):
    lg = L.load(started["ledger"])
    b = brief_mod.build(lg, started["repo"].root, "tester")
    return b, lg


def test_b7_stale_patch_surfaced_after_candidate_changes_context(started, cli, hook):
    wt, twt = _ready_with_patch(started, cli, hook)
    # builder 再改一版实现，动了 patch 上下文所在的行，使旧 patch 打不上新 HEAD
    (wt / "src" / "foo.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    total = a * b\n    return total\n"
    )
    cli("checkpoint", "--session", "S1", "--role", "builder")

    b, lg = _tester_brief_todo(started)
    stale = [t for t in b["todo"] if t.get("what") == "stale_mutation_patch"]
    assert stale, b["todo"]
    assert stale[0]["behaviors"] == ["B1"], stale[0]
    assert stale[0]["candidate_head"] == lg["candidate"]["head"], stale[0]

    text = brief_mod.render(b)
    assert "stale_mutation_patch" in text, text


def test_b7_boundary_all_patches_apply_no_stale_entry(started, cli, hook):
    _ready_with_patch(started, cli, hook)
    b, _ = _tester_brief_todo(started)
    assert not [t for t in b["todo"] if t.get("what") == "stale_mutation_patch"], b["todo"]


def test_b7_boundary_empty_patch_group_only_in_add_mutation_patch(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)  # 首轮盲写，patch 缺省
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"

    b, _ = _tester_brief_todo(started)
    stale = [t for t in b["todo"] if t.get("what") == "stale_mutation_patch"]
    assert not stale, b["todo"]
    add_patch = [t for t in b["todo"] if t.get("what") == "add_mutation_patch"]
    assert add_patch and add_patch[0]["behaviors"] == ["B1"], b["todo"]


def test_b7_boundary_not_readable_before_integrate_no_stale_entry(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(started["tester_worktree"])
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    b, _ = _tester_brief_todo(started)
    assert not [t for t in b["todo"] if t.get("what") == "stale_mutation_patch"], b["todo"]
