"""B10: fix_proof 待办的措辞明确要求重新提交时带上覆盖全部 behavior 的完整 proof_spec。
B11: tester 的 brief 文本告知：测试集成进候选之后会被 machine 全量跑一遍，失败会回到 tester；
     盲写阶段关于不得读取候选实现的既有措辞保持不变。
"""

from __future__ import annotations

from builder_loop import brief, ledger as L
from conftest import drive_to_proof_pass, implement_mul, make_tester_result, role_turn, write_mul_test


def test_b10_fix_proof_todo_requires_full_proof_spec_covering_all_behaviors(started, cli, hook):
    """result-channel run（B6）之后：非 final 的不合规 handback 之后 tester 仍算"在跑"（心跳未过期），
    紧接着硬跑 `bl proof` 现在会先被 PROOF_TESTER_RUNNING 挡住——这是新增的、正确的行为，不是这条
    behavior 的负责范围。为了单纯测 fix_proof 待办的措辞，不再额外交一次会被拒的 bogus 补丁：
    首轮盲写留空 patch 本身，集成后硬跑 proof 就已经是 TEST_MUTATION_PATCH_MISSING（owner=tester），
    足以触发 fix_proof 待办。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)  # patch 留空
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    proof_out = cli("proof", "--session", "S1", expect=1)  # 缺 patch → TEST_MUTATION_PATCH_MISSING，owner=tester
    assert proof_out["failure"]["code"] == "TEST_MUTATION_PATCH_MISSING"
    lg = L.load(started["ledger"])
    b = brief.build(lg, started["repo"].root, "tester")
    text = brief.render(b)
    assert "覆盖全部 behavior" in text and "完整 proof_spec" in text


def test_b11_brief_tells_tester_tests_are_rerun_by_machine_after_integration(started, cli):
    lg = L.load(started["ledger"])
    b = brief.build(lg, started["repo"].root, "tester")
    text = brief.render(b)
    assert "machine" in text and "全量" in text and "失败会回到 tester" in text


def test_b11_invariant_blind_write_no_candidate_wording_preserved(started, cli):
    """不变量：盲写阶段关于不得读取候选实现的既有措辞保持不变。"""
    lg = L.load(started["ledger"])
    b = brief.build(lg, started["repo"].root, "tester")
    text = brief.render(b)
    assert "候选 worktree、其他分支、`git log --all` 都不要碰" in text
