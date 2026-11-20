"""B1：machine 失败的 check_machine_failure 待办只认当前输入上的 fresh FAIL。

given machine 在某候选输入上 FAIL 且日志命中 tester-owned 文件，随后 checkpoint / integrate /
contract revise 使该 evidence stale → tester brief 不再给 check_machine_failure；旧失败仍留在
ledger.failures 里。fresh FAIL 的待办携带 candidate_head、files 与完整的 log（不是 tail）。

起点代码只读 evidence.status == "fail"，不区分 stale——下面三条 stale 断言在起点上会在 call 阶段
失败（baseline-red）。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import brief as brief_mod
from builder_loop import evidence
from builder_loop import ledger as L
from conftest import (
    PROOF_RUNNER_CMD,
    contract_with,
    implement_mul,
    make_tester_result,
    role_turn,
    write_plan,
)

# 首尾标记夹住超过旧 tail（4000 字符）的输出：整份日志从 HEADMARK 开始、到 ENDMARK 结束
STAGE_CMD = "echo HEADMARK; seq 1 20000; echo 'see tests/test_foo.py'; echo ENDMARK; exit 1"


def _start(repo, cli, *, max_iterations: int = 5):
    (repo.root / ".claude" / "loop.yml").write_text(
        f'pass_cmd:\n  - stage: unit\n    cmd: "{STAGE_CMD}"\n    timeout: 30\n'
        f"max_iterations: {max_iterations}\n"
        f'proof_runner:\n  framework: pytest\n  cmd: "{PROOF_RUNNER_CMD}"\n',
        encoding="utf-8",
    )
    repo.commit_all("chore(fixture): [cr_id_skip] Failing machine stage")
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    return {"repo": repo.root, "worktree": Path(out["worktree"]),
            "tester_worktree": Path(out["tester_worktree"]), "ledger": Path(out["ledger"])}


def _todo(lg, repo_root):
    return [t for t in brief_mod.build(lg, repo_root, "tester")["todo"] if t.get("what") == "check_machine_failure"]


def _fresh_source_of_truth(ctx, cli):
    """让 machine 在候选上 FAIL，返回 (tester brief 待办, 当时的 ledger)。"""
    implement_mul(ctx["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["result"] == "FAIL" and res["failure"]["tester_files_mentioned"] == ["tests/test_foo.py"]
    lg = L.load(ctx["ledger"])
    assert evidence.state(lg, "machine", ctx["repo"]) == "fail"
    return lg


def _assert_fresh_todo(todo, lg):
    assert len(todo) == 1, todo
    t = todo[0]
    # files / log 是本待办既有键名；候选 HEAD 允许 candidate_head（brief 里 stale_mutation_patch 的既有命名）或 head
    head = t.get("candidate_head", t.get("head"))
    assert head == lg["candidate"]["head"], t
    assert t["files"] == ["tests/test_foo.py"], t
    assert t["log"] == lg["evidence"]["machine"]["details"]["failure"]["log"], t
    log = Path(t["log"])
    assert log.is_file()
    text = log.read_text(encoding="utf-8", errors="replace")
    assert text.strip().startswith("HEADMARK") and text.strip().endswith("ENDMARK"), text[:200]


def _assert_stale_hides_todo(ctx):
    lg = L.load(ctx["ledger"])
    assert evidence.state(lg, "machine", ctx["repo"]) == "stale"
    assert _todo(lg, ctx["repo"]) == []
    # 旧失败仍留在 ledger 里：隐藏的是待办，不是证据历史
    assert len(lg["failures"]["machine"]) == 1
    assert lg["evidence"]["machine"]["status"] == "fail"


def test_b1_checkpoint_staleness_hides_old_machine_failure(repo, cli):
    ctx = _start(repo, cli)
    _assert_fresh_todo(_todo(_fresh_source_of_truth(ctx, cli), repo.root), L.load(ctx["ledger"]))

    (ctx["worktree"] / "src" / "foo.py").write_text(
        (ctx["worktree"] / "src" / "foo.py").read_text() + "\n# candidate moves\n"
    )
    cli("checkpoint", "--session", "S1", "--role", "builder")
    _assert_stale_hides_todo(ctx)


def test_b1_integrate_staleness_hides_old_machine_failure(repo, cli, hook):
    ctx = _start(repo, cli)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    (ctx["tester_worktree"] / "tests" / "test_mul.py").write_text(
        "from src.foo import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n"
    )
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 0, r
    _assert_fresh_todo(_todo(_fresh_source_of_truth(ctx, cli), repo.root), L.load(ctx["ledger"]))

    cli("integrate", "--session", "S1")  # 候选 HEAD 前进：machine 结论对应不到当前输入
    _assert_stale_hides_todo(ctx)


def test_b1_contract_revise_staleness_hides_old_machine_failure(repo, cli):
    ctx = _start(repo, cli)
    _assert_fresh_todo(_todo(_fresh_source_of_truth(ctx, cli), repo.root), L.load(ctx["ledger"]))

    c = contract_with(**{"assurance.review_focus": ["look here"]})
    write_plan(repo.root, c, "plan-neutral.md")
    out = cli("contract", "revise", "--session", "S1", "--plan", str(repo.root / "plan-neutral.md"))
    assert out["changes"] == ["NEUTRAL"], out
    assert L.load(ctx["ledger"])["contract"]["assurance"]["review_focus"] == ["look here"]
    _assert_stale_hides_todo(ctx)


def test_guard_other_tester_todos_are_not_affected_by_machine_staleness(repo, cli):
    """不变量（守卫，不列入 proof_spec）：stale 只隐藏 check_machine_failure，
    其它 tester 待办（这里是没有 tester evidence 时的 write_tests）照旧。"""
    ctx = _start(repo, cli)
    _fresh_source_of_truth(ctx, cli)
    (ctx["worktree"] / "src" / "foo.py").write_text(
        (ctx["worktree"] / "src" / "foo.py").read_text() + "\n# candidate moves\n"
    )
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg = L.load(ctx["ledger"])
    todo = brief_mod.build(lg, repo.root, "tester")["todo"]
    assert [t["what"] for t in todo if t["what"] != "check_machine_failure"] == ["write_tests"]
