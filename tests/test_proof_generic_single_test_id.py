"""B3：proof_runner.framework=generic 的强证明组必须恰好声明 1 个证据 test_id，
否则交卷 / `bl proof` 校验 proof_spec 时 PROOF_SPEC_INVALID，消息含字面锚句。

起点代码没有这条数量限制（generic 组可以列多个 id 且不加 per_id 记录），所以下面的正向断言在
起点上会在 call 阶段失败（baseline-red）。边界与不变量（reviewed-boundaries 不受限、generic
仍不能做 baseline-red）是既有行为，作为守卫不列入 proof group。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from builder_loop import proof as P
from builder_loop.errors import Problem

from conftest import PROOF_RUNNER_CMD, PYTEST_CMD, contract_with, make_tester_result, role_turn, write_plan

ANCHOR = "generic 强证明组必须恰好声明 1 个证据 test_id"

STRONG = "strong"
REVIEWED = "reviewed-boundaries"


def _lg(behaviors: list[dict], framework: str = "generic") -> dict:
    return {
        "contract": {
            "assurance": {
                "proof_kinds": ["baseline-red", "mutation", "reviewed-boundaries"],
                "proof_runner": {"framework": framework, "cmd": "go test {tests}"},
            },
            "mission": {"behaviors": behaviors},
            "authority": {"builder_write": ["src/**"], "tester_write": ["tests/**"], "protected_paths": []},
        },
        "tester": {"head": None},
    }


def _mutation_group(tids: list[str], behavior: str = "B1") -> dict:
    return {"kind": "mutation", "behavior_ids": [behavior], "test_ids": tids, "timeout": 60}


# ---------------------------------------------------------------- B3：正向


def test_b3_generic_strong_group_with_two_test_ids_is_invalid():
    lg = _lg([{"id": "B1", "proof": STRONG}])
    spec = {"groups": [_mutation_group(["tests/a.go", "tests/b.go"])]}
    with pytest.raises(Problem) as ei:
        P.check_structure(spec, lg, Path("/nonexistent-repo-root"))
    assert ei.value.code == "PROOF_SPEC_INVALID"
    assert ANCHOR in str(ei.value.message) + str(ei.value.details)


def test_b3_boundary_generic_strong_group_with_zero_test_ids_is_still_invalid():
    """边界：零个也不是「恰好一个」——仍然 PROOF_SPEC_INVALID（具体措辞不冻结）。"""
    lg = _lg([{"id": "B1", "proof": STRONG}])
    spec = {"groups": [_mutation_group([])]}
    with pytest.raises(Problem) as ei:
        P.check_structure(spec, lg, Path("/nonexistent-repo-root"))
    assert ei.value.code == "PROOF_SPEC_INVALID"


# ---------------------------------------------------------------- B3：边界 / 守卫


def test_b3_guard_generic_strong_group_with_one_test_id_is_valid():
    lg = _lg([{"id": "B1", "proof": STRONG}])
    spec = {"groups": [_mutation_group(["tests/a.go"])]}
    out = P.check_structure(spec, lg, Path("/nonexistent-repo-root"))
    assert out["groups"][0]["test_ids"] == ["tests/a.go"]


def test_b3_boundary_reviewed_group_not_limited_to_one_test_id():
    """边界：reviewed-boundaries 不受这一条数限制（它不跑反例）。"""
    lg = _lg([{"id": "B1", "proof": REVIEWED}])
    spec = {"groups": [{
        "kind": "reviewed-boundaries", "behavior_ids": ["B1"],
        "test_ids": ["tests/a.go", "tests/b.go"], "timeout": 60,
        "reviewed_boundaries": {"positive": ["tests/a.go"], "negative": ["tests/b.go"], "boundary": [], "invariant": []},
    }]}
    out = P.check_structure(spec, lg, Path("/nonexistent-repo-root"))
    assert out["groups"][0]["test_ids"] == ["tests/a.go", "tests/b.go"]


def test_b3_boundary_generic_still_cannot_do_baseline_red():
    lg = _lg([{"id": "B1", "proof": STRONG}])
    spec = {"groups": [{"kind": "baseline-red", "behavior_ids": ["B1"], "test_ids": ["tests/a.go"], "timeout": 60}]}
    with pytest.raises(Problem) as ei:
        P.check_structure(spec, lg, Path("/nonexistent-repo-root"))
    assert ei.value.code == "PROOF_SPEC_INVALID"


def test_b3_invariant_generic_ignores_junit_and_never_produces_per_id():
    """不变量：generic 只看退出码，候选 rc==0 / 反例非零的判定不变，且不生成 per_id。"""
    assert P.judge_candidate("generic", 0, [], ["tests/a.go"]) == {"ok": True, "per_id": {}, "reason": None}
    failed = P.judge_candidate("generic", 1, [], ["tests/a.go"])
    assert failed["ok"] is False and failed["per_id"] == {}
    assert P.classify_counterexample("generic", 0, [], ["tests/a.go"]) == "pass"
    assert P.classify_counterexample("generic", 1, [], ["tests/a.go"]) == "assertion-failure"


def test_b3_invariant_frozen_runner_means_tester_gives_no_argv(tmp_path):
    """不变量：测试命令来自冻结的 loop.yml，tester 不能给 argv（spec 里出现 argv 仍被拒）。"""
    lg = _lg([{"id": "B1", "proof": STRONG}], framework="pytest")
    spec = {"groups": [{**_mutation_group(["tests/a.py"]), "argv": ["pytest"]}]}
    with pytest.raises(Problem) as ei:
        P.check_structure(spec, lg, Path("/nonexistent-repo-root"))
    assert ei.value.code == "PROOF_SPEC_INVALID" and "argv" in str(ei.value.message)
    # 命令确实来自 runner：build_argv 只拼 test_ids 与 junit 参数
    argv = P.build_argv({"framework": "pytest", "cmd": PROOF_RUNNER_CMD}, ["tests/a.py::t"], tmp_path / "j.xml", Path("/r"))
    assert argv[-1] == "tests/a.py::t"


# ---------------------------------------------------------------- B3：端到端（交卷 gate）


def _generic_run(repo, cli, hook):
    (repo.root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: test\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\nmax_iterations: 3\n"
        "proof_runner:\n  framework: generic\n  cmd: \"bash\"\n"
    )
    write_plan(repo.root, contract_with())
    # loop.yml 现在从目标提交冻结（B1）：先提交再 start
    repo.commit_all("chore(fixture): [cr_id_skip] Generic loop config")
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    return Path(out["worktree"]), Path(out["tester_worktree"])


def test_b3_generic_multi_id_group_rejected_at_handback(repo, cli, hook):
    """given framework=generic 的强证明组 / when tester 交卷 / then 每组多于 1 个 test_id 被
    PROOF_SPEC_INVALID 打回，消息含字面锚句。"""
    wt, twt = _generic_run(repo, cli, hook)
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "check.sh").write_text('python3 -c "from src.foo import mul; assert mul(2, 3) == 6"\n')
    (twt / "tests" / "other.sh").write_text('python3 -c "from src.foo import add; assert add(2, 3) == 5"\n')
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=["tests/check.sh", "tests/other.sh"]), start=False)
    assert r["code"] != 0
    assert "PROOF_SPEC_INVALID" in r["stderr"] and ANCHOR in r["stderr"], r["stderr"]
