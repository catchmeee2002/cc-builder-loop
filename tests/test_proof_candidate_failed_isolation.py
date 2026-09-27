"""B4：候选执行阶段测试失败（TEST_PROOF_CANDIDATE_FAILED）且 machine evidence 此刻是 fresh pass 时，
suggested_owner 判给 tester 并附非空 isolation_hint；machine 不是 fresh pass 时仍归 builder，没有
isolation_hint。随后 tester 的 brief 出现 fix_proof 待办，readiness.next_action 是 resume_tester。

用两个互相依赖导入顺序的测试文件制造「全量顺序下靠别的测试先导入模块、单独跑就报错」的场景：
tests/test_warm.py 在模块导入时给 src.foo.add 打上标记，tests/test_mul.py 依赖这个标记。整套
tests 目录跑（machine）时两个文件都会被收集，标记总会在执行前就绪；只指定
tests/test_mul.py::test_mul 单独跑（proof 的候选阶段）时标记缺失，断言失败。
"""

from __future__ import annotations

import copy
from pathlib import Path

from builder_loop import brief as brief_mod
from builder_loop import evidence
from builder_loop import ledger as L
from conftest import CONTRACT, implement_mul, make_tester_result, mutation_patch, role_turn, write_plan

TEST_IDS = ["tests/test_mul.py::test_mul"]


def _start(repo, cli, session: str = "S1") -> dict:
    plan = write_plan(repo.root, copy.deepcopy(CONTRACT), f"plan-{session}.md")
    out = cli("start", "--plan", str(plan), "--session", session)
    return {"worktree": Path(out["worktree"]), "tester_worktree": Path(out["tester_worktree"]), "ledger": Path(out["ledger"])}


def _write_isolation_sensitive_tests(twt: Path) -> None:
    (twt / "tests" / "test_warm.py").write_text(
        "from src.foo import add\n\nadd._warmed = True\n\n\ndef test_warm():\n    assert True\n"
    )
    (twt / "tests" / "test_mul.py").write_text(
        "from src.foo import add, mul\n\n\ndef test_mul():\n"
        "    assert getattr(add, '_warmed', False) is True\n"
        "    assert mul(3, 4) == 12\n"
    )


def _blind_then_integrate(ctx, cli, hook) -> None:
    wt, twt = ctx["worktree"], ctx["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    _write_isolation_sensitive_tests(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=TEST_IDS), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")


def test_candidate_failed_owner_tester_with_isolation_hint_when_machine_fresh(repo, cli, hook):
    ctx = _start(repo, cli, "S1")
    wt = ctx["worktree"]
    _blind_then_integrate(ctx, cli, hook)
    assert cli("machine", "--session", "S1")["result"] == "PASS"

    patch = mutation_patch(wt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", patch, test_ids=TEST_IDS))
    assert r["code"] == 0, r

    out = cli("proof", "--session", "S1", expect=1)
    assert out["result"] == "FAIL", out
    failure = out["failure"]
    assert failure["code"] == "TEST_PROOF_CANDIDATE_FAILED"
    assert failure["suggested_owner"] == "tester"
    hint = failure.get("isolation_hint")
    assert isinstance(hint, str) and hint.strip()

    lg = L.load(ctx["ledger"])
    b = brief_mod.build(lg, repo.root, "tester")
    assert any(t["what"] == "fix_proof" for t in b["todo"])
    assert evidence.readiness(lg, repo.root)["next_action"] == "resume_tester"


def test_boundary_candidate_failed_owner_builder_without_isolation_hint_when_machine_not_fresh(repo, cli, hook):
    ctx = _start(repo, cli, "S1")
    wt = ctx["worktree"]
    _blind_then_integrate(ctx, cli, hook)
    # 不跑 machine：machine evidence 缺失（不是 fresh pass）

    patch = mutation_patch(wt)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", patch, test_ids=TEST_IDS))
    assert r["code"] == 0, r

    out = cli("proof", "--session", "S1", expect=1)
    assert out["result"] == "FAIL", out
    failure = out["failure"]
    assert failure["code"] == "TEST_PROOF_CANDIDATE_FAILED"
    assert failure["suggested_owner"] == "builder"
    assert "isolation_hint" not in failure


def test_invariant_tester_owned_failures_and_runner_owner_unchanged(repo, cli, hook):
    from builder_loop.proof import OWNER_BY_FAILURE, TESTER_OWNED_FAILURES

    assert set(TESTER_OWNED_FAILURES) == {
        "TEST_BASELINE_RED_NOT_PROVEN", "TEST_MUTATION_SURVIVED", "TEST_MUTATION_INVALID",
        "TEST_MUTATION_PATCH_MISSING", "TEST_PROOF_NOT_EXECUTED",
    }
    assert OWNER_BY_FAILURE["TEST_PROOF_RUNNER_FAILED"] == "contract"
