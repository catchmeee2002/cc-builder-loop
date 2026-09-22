"""B1: proof 记录每条声明 test_id 在反例下是否变红（per_id: red/green/missing），
供 reviewer 识别从未变红的搭便车断言（B2/B3 见 test_reviewer_checklist_discrimination.py
与 test_reviewer_brief_discrimination.py）。

单条 kind=mutation 的 group 覆盖 B1 全部场景：
- 纯函数 discrimination(cases, test_ids) 的 red/green/missing 值域（对照 test_proof_hooks.py::_case 的构造风格）
- 端到端：同一 mutation 组两条 test_id，一条被 patch 破坏（red）、一条无关（green）——这正是 B1 given 描述的场景
- 端到端：baseline-red 组同样记录 per_id
- 端到端：reviewed-boundaries 组的 counterexample 不含 per_id 键
- 端到端：generic proof_runner（无 junit）时 per_id 为空字典 {}

patch 首轮留空：新接口在起点上无法 import discrimination，这里全部走 mutation。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import proof as P

from conftest import (
    CONTRACT,
    PYTEST_CMD,
    contract_with,
    implement_mul,
    make_tester_result,
    mutation_patch,
    role_turn,
    write_mul_test,
    write_plan,
)


def _case(classname, name, outcome="passed", message=""):
    return {"classname": classname, "name": name, "outcome": outcome, "message": message}


# ---------------------------------------------------------------- 纯函数：值域


def test_discrimination_maps_outcomes_to_red_green_missing():
    cases = [
        _case("tests.test_x", "test_a", "failure", "boom"),
        _case("tests.test_x", "test_b", "passed"),
    ]
    assert P.discrimination(cases, ["tests/test_x.py::test_a"]) == {"tests/test_x.py::test_a": "red"}
    assert P.discrimination(cases, ["tests/test_x.py::test_b"]) == {"tests/test_x.py::test_b": "green"}
    assert P.discrimination(cases, ["tests/test_x.py::test_nope"]) == {"tests/test_x.py::test_nope": "missing"}
    assert P.discrimination(cases, ["tests/test_x.py::test_a", "tests/test_x.py::test_b", "tests/test_x.py::test_nope"]) == {
        "tests/test_x.py::test_a": "red",
        "tests/test_x.py::test_b": "green",
        "tests/test_x.py::test_nope": "missing",
    }


# ---------------------------------------------------------------- 端到端：mutation 组内一红一绿（B1 given 场景）


def test_mutation_group_records_red_and_green_per_id(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_mul.py").write_text(
        "from src.foo import add, mul\n\n\n"
        "def test_mul():\n    assert mul(3, 4) == 12\n\n\n"
        "def test_add_unchanged():\n    assert add(2, 3) == 5\n"
    )
    ids = ["tests/test_mul.py::test_mul", "tests/test_mul.py::test_add_unchanged"]
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=ids), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    # mutation_patch 只把 mul 的实现改坏，add() 不受影响 —— 这正是"一条被破坏、一条无关"
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt), test_ids=ids))
    assert r["code"] == 0, r
    res = cli("proof", "--session", "S1")
    assert res["result"] == "PASS", res
    per_id = res["groups"][0]["counterexample"]["per_id"]
    assert per_id == {"tests/test_mul.py::test_mul": "red", "tests/test_mul.py::test_add_unchanged": "green"}


# ---------------------------------------------------------------- 端到端：baseline-red 组同样记录 per_id


def test_baseline_red_group_records_per_id(repo, cli, hook):
    (repo.root / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    (repo.root / "tests" / "test_sub.py").write_text("from src.foo import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")
    repo.commit_all()
    c = contract_with()
    c["mission"].update(slug="remove-sub-per-id", objective="Remove sub()", interfaces=[])
    c["mission"]["behaviors"] = [{"id": "B1", "given": "module src.foo", "when": "looking up sub", "then": "sub no longer exists; add() unchanged"}]
    write_plan(repo.root, c, "rm-per-id.md")
    out = cli("start", "--plan", str(repo.root / "rm-per-id.md"), "--session", "S1")
    wt, twt = Path(out["worktree"]), Path(out["tester_worktree"])

    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_sub.py").unlink()
    (twt / "tests" / "test_sub_removed.py").write_text("import src.foo\n\n\ndef test_sub_removed():\n    assert not hasattr(src.foo, 'sub')\n")
    ids = ["tests/test_sub_removed.py::test_sub_removed"]
    r = role_turn(hook, "tester", "T1", make_tester_result("baseline-red", test_ids=ids), start=False)
    assert r["code"] == 0, r

    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    res = cli("proof", "--session", "S1")
    assert res["result"] == "PASS", res
    assert res["groups"][0]["counterexample"]["per_id"] == {"tests/test_sub_removed.py::test_sub_removed": "red"}


# ---------------------------------------------------------------- 端到端：reviewed-boundaries 组不带 per_id 键


def test_reviewed_boundaries_counterexample_has_no_per_id_key(repo, cli, hook):
    c = contract_with()
    c["mission"]["behaviors"][0]["proof"] = "reviewed-boundaries"
    write_plan(repo.root, c, "weak-per-id.md")
    out = cli("start", "--plan", str(repo.root / "weak-per-id.md"), "--session", "S1")
    wt, twt = Path(out["worktree"]), Path(out["tester_worktree"])
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    r = role_turn(hook, "tester", "T1", make_tester_result("reviewed-boundaries"), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    res = cli("proof", "--session", "S1")
    assert res["result"] == "PASS", res
    assert "per_id" not in res["groups"][0]["counterexample"]


# ---------------------------------------------------------------- 端到端：generic proof_runner 没有 junit → per_id 为 {}


def test_generic_framework_counterexample_per_id_is_empty_dict(repo, cli, hook):
    (repo.root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: test\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\nmax_iterations: 3\n"
        "proof_runner:\n  framework: generic\n  cmd: \"bash\"\n"
    )
    write_plan(repo.root, CONTRACT)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    wt, twt = Path(out["worktree"]), Path(out["tester_worktree"])
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "check.sh").write_text('python3 -c "from src.foo import mul; assert mul(2, 3) == 6"\n')
    ids = ["tests/check.sh"]
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=ids), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt), test_ids=ids))
    assert r["code"] == 0, r
    res = cli("proof", "--session", "S1")
    assert res["result"] == "PASS", res
    assert res["groups"][0]["counterexample"]["per_id"] == {}
