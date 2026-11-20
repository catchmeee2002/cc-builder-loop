"""B1：proof 的强证明判据要求每个声明 test_id 匹配到的**全部**用例在反例下 call 阶段 failure；
任一 green / missing / mixed 都 FAIL（组内「至少一条红」不再足够）。B2：一个不带参数实例后缀的
test_id 匹配多个参数实例时，部分实例红记为 mixed 并 FAIL。

覆盖：
- B1 纯函数：单条 id 匹配多个用例、只有部分 failure → 不再算 red
- B1 端到端 mutation 组：一条被破坏（red）、一条无关（green）→ 新判据下必须 FAIL，owner=tester，
  details 列出未全红 id，失败消息含字面锚句
- B1 端到端 baseline-red 组同样适用
- B2 纯函数：参数实例全红 = red、全绿 = green、全无 = missing，部分是 mixed
- B2 端到端：声明不带 `[..]` 的 id、只有部分参数实例翻红 → proof FAIL
- 守卫（不列入 proof_spec，由 machine 全量跑）：全红仍 PASS、候选阶段仍要求全部 passed

这里面的 B1/B2 断言在起点代码上会在 call 阶段失败（旧判据「任一条红即证明成立」），
所以这两组用 baseline-red。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import proof as P

from conftest import (
    contract_with,
    implement_mul,
    make_tester_result,
    mutation_patch,
    role_turn,
    write_plan,
)

ANCHOR = "proof_spec 只列用于证明的测试，以下 test_id 在反例下没有全部变红"


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[*_`]", "", text).split())


def _case(classname, name, outcome="passed", message=""):
    return {"classname": classname, "name": name, "outcome": outcome, "message": message}


MUL_TESTS = (
    "from src.foo import add, mul\n\n\n"
    "def test_mul():\n    assert mul(3, 4) == 12\n\n\n"
    "def test_add_unchanged():\n    assert add(2, 3) == 5\n"
)

PARAM_MUL_TEST = (
    "import pytest\nfrom src.foo import mul\n\n\n"
    "@pytest.mark.parametrize('a,b,expected', [(3, 4, 12), (2, 2, 4)])\n"
    "def test_mul(a, b, expected):\n    assert mul(a, b) == expected\n"
)


def _details_without_logs(failure: dict) -> str:
    return str({k: v for k, v in failure.items() if k not in ("tail", "log")})


def _drive_mutation_group(started, cli, hook, files: dict[str, str], ids: list[str]):
    """盲写 → integrate → machine → 带 patch 交卷 → proof，返回 proof 结果。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    for rel, body in files.items():
        (twt / rel).write_text(body)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=ids), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt), test_ids=ids))
    assert r["code"] == 0, r
    return cli("proof", "--session", "S1", expect=1)


# ---------------------------------------------------------------- B1：纯函数


def test_b1_discrimination_requires_every_matched_case_red():
    """同一个 test_id 匹配到多条用例、只有部分 failure：旧代码 any failure → red；新判据不再算 red。
    具体非 red 值的名字由 B2 冻结（mixed），这里只锁「不再是 red」与 proof FAIL（端到端覆盖）。"""
    cases = [
        _case("tests.test_x", "test_a", "failure", "boom"),
        _case("tests.test_x", "test_b"),
    ]
    verdict = P.discrimination(cases, ["tests/test_x.py"])["tests/test_x.py"]
    assert verdict != "red", verdict


# ---------------------------------------------------------------- B1：端到端


def test_b1_mutation_group_with_a_green_id_fails(started, cli, hook):
    ids = ["tests/test_mul.py::test_mul", "tests/test_mul.py::test_add_unchanged"]
    res = _drive_mutation_group(started, cli, hook, {"tests/test_mul.py": MUL_TESTS}, ids)

    failure = res["failure"]
    # 沿用既有失败码；组内有一条红不够，proof 必须 FAIL
    assert failure["code"] == "TEST_MUTATION_SURVIVED", failure
    assert failure["suggested_owner"] == "tester"
    assert _norm(ANCHOR) in _norm(failure["message"]), failure["message"]
    # details 列出未全红的 id（键名不冻结，只要求该 id 出现在 failure 里）
    assert "tests/test_mul.py::test_add_unchanged" in _details_without_logs(failure), failure


def test_b1_baseline_red_group_with_a_green_id_fails(repo, cli, hook):
    """baseline-red 组同样要求声明 id 全部变红：起点上本来就通过的守卫断言不能搭便车。"""
    (repo.root / "src" / "foo.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    )
    (repo.root / "tests" / "test_sub.py").write_text(
        "from src.foo import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n"
    )
    repo.commit_all()
    c = contract_with()
    c["mission"].update(slug="remove-sub-mixed", objective="Remove sub()", interfaces=[])
    c["mission"]["behaviors"] = [
        {"id": "B1", "given": "module src.foo", "when": "looking up sub", "then": "sub no longer exists; add() unchanged"}
    ]
    write_plan(repo.root, c, "rm-mixed.md")
    out = cli("start", "--plan", str(repo.root / "rm-mixed.md"), "--session", "S1")
    wt, twt = Path(out["worktree"]), Path(out["tester_worktree"])

    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_sub.py").unlink()
    (twt / "tests" / "test_sub_removed.py").write_text(
        "import src.foo\n\n\ndef test_sub_removed():\n    assert not hasattr(src.foo, 'sub')\n"
    )
    # 起点上就通过的守卫断言（不约束被移除的行为）
    (twt / "tests" / "test_add_light.py").write_text(
        "from src.foo import add\n\n\ndef test_add_light():\n    assert add(1, 2) == 3\n"
    )
    ids = ["tests/test_sub_removed.py::test_sub_removed", "tests/test_add_light.py::test_add_light"]
    r = role_turn(hook, "tester", "T1", make_tester_result("baseline-red", test_ids=ids), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"

    res = cli("proof", "--session", "S1", expect=1)
    failure = res["failure"]
    assert failure["code"] == "TEST_BASELINE_RED_NOT_PROVEN", failure
    assert failure["suggested_owner"] == "tester"
    assert _norm(ANCHOR) in _norm(failure["message"]), failure["message"]
    assert "tests/test_add_light.py::test_add_light" in _details_without_logs(failure), failure


# ---------------------------------------------------------------- B1：守卫


def test_b1_guard_single_red_id_still_passes(started, cli, hook):
    """不变量：每个声明 id 都全红时 proof 照常 PASS（全红才过，不是一律不过）。"""
    ids = ["tests/test_mul.py::test_mul"]
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_mul.py").write_text(MUL_TESTS)
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=ids), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt), test_ids=ids))
    assert r["code"] == 0, r
    res = cli("proof", "--session", "S1")
    assert res["result"] == "PASS", res


def test_b1_guard_candidate_stage_still_requires_all_passed():
    """不变量：候选阶段仍要求 rc==0 且声明用例全部 passed（skipped / 缺一都不算）。"""
    cases = [
        _case("tests.test_x", "test_a"),
        _case("tests.test_x", "test_b", "skipped"),
    ]
    assert P.judge_candidate("pytest", 0, cases, ["tests/test_x.py::test_a"])["ok"] is True
    assert P.judge_candidate("pytest", 0, cases, ["tests/test_x.py::test_b"])["ok"] is False
    assert P.judge_candidate("pytest", 0, cases, ["tests/test_x.py::test_a", "tests/test_x.py::test_b"])["ok"] is False


# ---------------------------------------------------------------- B2：纯函数


def test_b2_partial_param_instances_is_mixed():
    cases = [
        _case("tests.test_p", "test_mul[3-4-12]", "failure", "assert 7 == 12"),
        _case("tests.test_p", "test_mul[2-2-4]"),
    ]
    assert P.discrimination(cases, ["tests/test_p.py::test_mul"]) == {"tests/test_p.py::test_mul": "mixed"}


# ---------------------------------------------------------------- B2：端到端


def test_b2_param_id_partial_instances_fails_proof(started, cli, hook):
    """声明不带 `[..]` 的 id、只有一个参数实例翻红：该 id 是 mixed，proof 必须 FAIL。"""
    ids = ["tests/test_mul.py::test_mul"]
    res = _drive_mutation_group(started, cli, hook, {"tests/test_mul.py": PARAM_MUL_TEST}, ids)
    failure = res["failure"]
    assert failure["code"] == "TEST_MUTATION_SURVIVED", failure
    assert failure["suggested_owner"] == "tester"
    assert _norm(ANCHOR) in _norm(failure["message"]), failure["message"]
    assert ids[0] in str(failure), failure


# ---------------------------------------------------------------- B2：守卫

def test_b2_guard_all_param_instances_failure_is_red():
    cases = [
        _case("tests.test_p", "test_mul[3-4-12]", "failure", "assert 7 == 12"),
        _case("tests.test_p", "test_mul[2-2-4]", "failure", "assert 4 == 4"),
    ]
    assert P.discrimination(cases, ["tests/test_p.py::test_mul"]) == {"tests/test_p.py::test_mul": "red"}


def test_b2_guard_all_param_instances_passed_is_green():
    cases = [
        _case("tests.test_p", "test_mul[3-4-12]"),
        _case("tests.test_p", "test_mul[2-2-4]"),
    ]
    assert P.discrimination(cases, ["tests/test_p.py::test_mul"]) == {"tests/test_p.py::test_mul": "green"}


def test_b2_guard_no_matching_instance_is_missing():
    cases = [_case("tests.test_p", "test_other[9-9-81]")]
    assert P.discrimination(cases, ["tests/test_p.py::test_mul"]) == {"tests/test_p.py::test_mul": "missing"}


def test_b2_boundary_declaring_the_failing_instance_is_red():
    """边界：可只声明具体会翻红的参数实例。"""
    cases = [
        _case("tests.test_p", "test_mul[3-4-12]", "failure", "assert 7 == 12"),
        _case("tests.test_p", "test_mul[2-2-4]"),
    ]
    assert P.discrimination(cases, ["tests/test_p.py::test_mul[3-4-12]"]) == {
        "tests/test_p.py::test_mul[3-4-12]": "red",
    }


def test_b2_invariant_colon_param_ids_still_red():
    """不变量：参数 id 内含 `::` 的既有匹配支持不变。"""
    cases = [
        _case("tests.unit.test_a", "test_survives[tests/unit/test_x.py::test_y]", "failure", "x"),
        _case("tests.test_b.TestK", "test_m[a::b]", "failure", "y"),
    ]
    ids = [
        "tests/unit/test_a.py::test_survives[tests/unit/test_x.py::test_y]",
        "tests/test_b.py::TestK::test_m[a::b]",
    ]
    assert P.discrimination(cases, ids) == {ids[0]: "red", ids[1]: "red"}
