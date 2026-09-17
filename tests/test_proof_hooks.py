from pathlib import Path

import pytest

from builder_loop import ledger as L
from builder_loop import proof as P
from conftest import contract_with, drive_to_proof_pass, git, implement_mul, mutation_patch, role_turn, make_tester_result, write_mul_test, write_plan


def _pre(tool: str, tool_input: dict, role: str = "tester", agent: str = "T1") -> dict:
    return {"session_id": "S1", "agent_type": role, "agent_id": agent, "tool_name": tool, "tool_input": tool_input}


def _denied(r) -> bool:
    return bool(r["json"]) and r["json"]["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------- tester 独立性


def test_tester_context_hides_implementation_until_integrated(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    ctx = hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})["json"]["hookSpecificOutput"]["additionalContext"]
    assert str(twt) in ctx and str(wt) not in ctx
    assert "a or b is 0" in ctx and "add() unchanged" in ctx and "不允许 reviewed-boundaries" in ctx
    assert "不需要你给 argv" in ctx and "冻结基线" in ctx

    # 盲写阶段：读 / 搜 / 命令都碰不到候选；`..` 绕不过去；写只能落在自己的 worktree + tester_write
    assert _denied(hook("PreToolUse", _pre("Read", {"file_path": f"{wt}/src/foo.py"})))
    assert _denied(hook("PreToolUse", _pre("Read", {"file_path": f"{twt}/../builder/src/foo.py"})))
    assert not _denied(hook("PreToolUse", _pre("Read", {"file_path": f"{twt}/src/foo.py"})))
    assert _denied(hook("PreToolUse", _pre("Grep", {"pattern": "mul"})))  # 缺省 path 可能落到候选
    assert _denied(hook("PreToolUse", _pre("Glob", {"pattern": "**/*.py", "path": str(wt)})))
    assert not _denied(hook("PreToolUse", _pre("Grep", {"pattern": "mul", "path": str(twt)})))
    assert _denied(hook("PreToolUse", _pre("Bash", {"command": f"cat {wt}/src/foo.py"})))
    assert _denied(hook("PreToolUse", _pre("Bash", {"command": f"git show builder-loop/{started['run_id']}/candidate:src/foo.py"})))
    assert not _denied(hook("PreToolUse", _pre("Bash", {"command": "python3 -m pytest --collect-only -q tests"})))
    assert not _denied(hook("PreToolUse", _pre("Write", {"file_path": f"{twt}/tests/test_x.py"})))
    assert _denied(hook("PreToolUse", _pre("Write", {"file_path": f"{twt}/src/foo.py"})))
    assert _denied(hook("PreToolUse", _pre("Write", {"file_path": f"{wt}/tests/test_x.py"})))
    assert _denied(hook("PreToolUse", _pre("Edit", {"file_path": f"{wt}/src/foo.py"}, role="reviewer", agent="R1")))
    assert hook("PreToolUse", {"session_id": "S1", "tool_name": "Write", "tool_input": {"file_path": f"{wt}/src/foo.py"}})["json"] is None  # 主 session 不受限

    # 集成之后读隔离解除（tester 要读候选才能写 mutation patch），上下文也随之变化
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    assert not _denied(hook("PreToolUse", _pre("Read", {"file_path": f"{wt}/src/foo.py"})))
    ctx2 = hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})["json"]["hookSpecificOutput"]["additionalContext"]
    assert str(wt) in ctx2 and "请补 patch" in ctx2
    assert _denied(hook("PreToolUse", _pre("Write", {"file_path": f"{wt}/src/foo.py"})))  # 能读，仍不能写


def test_tester_result_lifecycle(started, cli, hook):
    twt = started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(twt)
    (twt / "src" / "foo.py").write_text("# tester 越界\n")
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert r["code"] == 2 and "builder_owned" in r["stderr"]
    git(twt, "checkout", "--", "src/foo.py")
    assert role_turn(hook, "tester", "T1", None, start=False, message="forgot")["code"] == 2
    r = role_turn(hook, "tester", "T1", None, start=False, message="forgot again")
    assert r["code"] == 0 and "已记为 fail" in r["stderr"]
    lg = L.load(started["ledger"])
    assert lg["evidence"]["tester"]["status"] == "fail" and [e["final"] for e in lg["events"] if e["kind"] == "role_malformed"] == [False, False, True]
    # 续接后合规交卷
    assert role_turn(hook, "tester", "T1", make_tester_result("mutation"))["code"] == 0
    lg = L.load(started["ledger"])
    assert lg["evidence"]["tester"]["status"] == "pass" and lg["agents"]["tester"]["turn"] == 2
    assert lg["evidence"]["tester"]["details"]["files"] == {"present": ["tests/test_mul.py"], "deleted": []}
    # 冒充：未登记的 agent_id 不记账；matcher 放进来的空 agent_type 也不记账
    before = L.load(started["ledger"])["seq"]
    assert role_turn(hook, "tester", "FAKE", {"role": "tester", "status": "insufficient_spec"}, start=False)["code"] == 0
    assert hook("SubagentStop", {"session_id": "S1", "agent_id": "X", "agent_type": "", "last_assistant_message": "hi"})["code"] == 0
    assert L.load(started["ledger"])["seq"] == before
    # 重新 spawn 一个新 tester 顶替 → 记事件
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T2", "agent_type": "tester"})
    assert L.load(started["ledger"])["events"][-2]["kind"] == "role_replaced"


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["proof_spec"]["groups"][0].update(argv=["pytest"]), "不再接受"),
    (lambda p: p["proof_spec"]["groups"][0].update(test_ids=["--co"]), "不能以 '-' 开头"),
    (lambda p: p["proof_spec"]["groups"][0].update(test_ids=["src/foo.py::test_x"]), "tester_write"),
    (lambda p: p["proof_spec"]["groups"][0].update(test_ids=["tests/test_missing.py::t"]), "不存在"),
    (lambda p: p["proof_spec"]["groups"].append(dict(p["proof_spec"]["groups"][0])), "一一对应"),
    (lambda p: p["proof_spec"]["groups"][0].update(kind="reviewed-boundaries", reviewed_boundaries={"positive": ["tests/test_mul.py::test_mul"]}), "没有放行"),
    (lambda p: p["proof_spec"]["groups"][0].update(patch="not a diff"), "无法识别"),
])
def test_proof_spec_validation(started, hook, mutate, needle):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(started["tester_worktree"])
    payload = make_tester_result("mutation")
    mutate(payload)
    r = role_turn(hook, "tester", "T1", payload, start=False)
    assert r["code"] == 2 and needle in r["stderr"], r["stderr"]


def test_reviewed_boundaries_needs_contract_permission(repo, cli, hook):
    c = contract_with()
    c["mission"]["behaviors"][0]["proof"] = "reviewed-boundaries"
    write_plan(repo.root, c, "weak.md")
    out = cli("start", "--plan", str(repo.root / "weak.md"), "--session", "S1")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    write_mul_test(Path(out["tester_worktree"]))
    assert role_turn(hook, "tester", "T1", make_tester_result("reviewed-boundaries"), start=False)["code"] == 0


# ---------------------------------------------------------------- proof 执行


def test_two_phase_proof_flow(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    lg = L.load(started["ledger"])
    group = lg["evidence"]["proof"]["details"]["groups"][0]
    assert group["candidate"]["per_id"] == {"tests/test_mul.py::test_mul": "passed"}
    assert group["counterexample"]["classification"] == "assertion-failure" and group["counterexample"]["patch_paths"] == ["src/foo.py"]
    assert lg["evidence"]["proof"]["details"]["runner"]["framework"] == "pytest"
    assert lg["failures"]["proof"] == []  # 缺 patch 由 readiness 引导续接，不制造一次 proof 失败
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "spawn_reviewer"


def test_proof_prerequisites_and_missing_patch(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("proof", "--session", "S1", expect=1)["code"] == "PROOF_PREREQ_TESTER"
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    assert cli("proof", "--session", "S1", expect=1)["code"] == "PROOF_PREREQ_INTEGRATE"
    cli("integrate", "--session", "S1")
    assert cli("integrate", "--session", "S1")["noop"] is True  # 幂等
    res = cli("proof", "--session", "S1", expect=1)  # 硬跑也只会得到「缺 patch，回 tester」
    assert res["failure"]["code"] == "TEST_MUTATION_PATCH_MISSING" and res["failure"]["suggested_owner"] == "tester"


def test_baseline_red_rejects_new_interface_and_weak_mutation_survives(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt, "assert mul(2, 2) == 4  # 弱：a+b 也等于 4")
    role_turn(hook, "tester", "T1", make_tester_result("baseline-red"), start=False)
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    res = cli("proof", "--session", "S1", expect=1)
    # 新接口在起点上是 ImportError（收集阶段出错），不是断言失败
    assert res["failure"]["code"] == "TEST_BASELINE_RED_NOT_PROVEN" and res["failure"]["classification"] == "error"
    assert res["readiness"]["next_action"] == "resume_tester"
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))
    res = cli("proof", "--session", "S1", expect=1)
    assert res["failure"]["code"] == "TEST_MUTATION_SURVIVED" and res["failure"]["classification"] == "pass"
    # tester 答复过了但同样的失败再次出现 → 不再无限续接，回到 proof 计数
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "proof"
    cli("proof", "--session", "S1", expect=1)
    res = cli("proof", "--session", "S1", expect=3)
    assert any(b["code"] == "PROOF_STALL" for b in res["readiness"]["blockers"])
    assert cli("proof", "--session", "S1", expect=3)["code"] == "PROOF_BLOCKED"


def test_mutation_patch_must_stay_in_builder_territory(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    bad = "diff --git a/tests/test_mul.py b/tests/test_mul.py\n--- a/tests/test_mul.py\n+++ b/tests/test_mul.py\n@@ -4 +4 @@\n-    assert mul(3, 4) == 12\n+    assert False\n"
    role_turn(hook, "tester", "T1", make_tester_result("mutation", bad), start=False)
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    res = cli("proof", "--session", "S1", expect=1)
    assert res["failure"]["code"] == "TEST_MUTATION_INVALID" and res["failure"]["reason"] == "tester_owned"


def test_deletion_task_flows_without_contract_revision(repo, cli, hook):
    """#227：删除功能时旧断言与被删代码同生共死。旧测试归 tester，两个角色各在自己的 worktree 里干，
    不再互相整单拒绝，也不需要 contract revise。"""
    (repo.root / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    (repo.root / "tests" / "test_sub.py").write_text("from src.foo import sub\n\n\ndef test_sub():\n    assert sub(3, 1) == 2\n")
    repo.commit_all()
    c = contract_with()
    c["mission"].update(slug="remove-sub", objective="Remove sub()", interfaces=[])
    c["mission"]["behaviors"] = [{"id": "B1", "given": "module src.foo", "when": "looking up sub", "then": "sub no longer exists; add() unchanged"}]
    write_plan(repo.root, c, "rm.md")
    out = cli("start", "--plan", str(repo.root / "rm.md"), "--session", "S1")
    wt, twt = Path(out["worktree"]), Path(out["tester_worktree"])

    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    (wt / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    # tester 在基线上：删掉旧测试，写一个负向测试（起点上 sub 还在 → 天然 baseline-red）
    (twt / "tests" / "test_sub.py").unlink()
    (twt / "tests" / "test_sub_removed.py").write_text("import src.foo\n\n\ndef test_sub_removed():\n    assert not hasattr(src.foo, 'sub')\n")
    r = role_turn(hook, "tester", "T1", make_tester_result("baseline-red", test_ids=["tests/test_sub_removed.py::test_sub_removed"]), start=False)
    assert r["code"] == 0, r
    files = L.load(Path(out["ledger"]))["evidence"]["tester"]["details"]["files"]
    assert files == {"present": ["tests/test_sub_removed.py"], "deleted": ["tests/test_sub.py"]}

    integ = cli("integrate", "--session", "S1")
    assert integ["integrated_paths"] == ["tests/test_sub.py", "tests/test_sub_removed.py"] and not (wt / "tests" / "test_sub.py").exists()
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    res = cli("proof", "--session", "S1")
    assert res["result"] == "PASS" and res["groups"][0]["counterexample"]["classification"] == "assertion-failure"
    assert L.load(Path(out["ledger"]))["contract"]["history"] == []


# ---------------------------------------------------------------- junit 判定（纯函数）


def _case(classname, name, outcome="passed", message=""):
    return {"classname": classname, "name": name, "outcome": outcome, "message": message}


def test_junit_matching_and_classification():
    cases = [
        _case("tests.test_x", "test_a"),
        _case("tests.test_x.TestC", "test_m", "failure", "AssertionError: assert 1 == 2"),
        _case("tests.test_x", "test_p[1]"), _case("tests.test_x", "test_p[2]", "failure", "assert 3 == 4"),
        _case("test_y", "test_b", "skipped"),  # monorepo 子目录自带 ini：rootdir 变了，classname 变短
        _case("tests.test_x", "test_imp", "failure", "ModuleNotFoundError: No module named 'zzz'"),
        _case("tests.test_x", "test_raises", "failure", "Failed: DID NOT RAISE <class 'ValueError'>"),
    ]
    assert [c["name"] for c in P.match_cases("tests/test_x.py::test_a", cases)] == ["test_a"]
    assert [c["name"] for c in P.match_cases("tests/test_x.py::TestC::test_m", cases)] == ["test_m"]
    assert [c["name"] for c in P.match_cases("tests/test_x.py::test_p", cases)] == ["test_p[1]", "test_p[2]"]
    assert [c["name"] for c in P.match_cases("tests/test_x.py::test_p[2]", cases)] == ["test_p[2]"]
    assert [c["name"] for c in P.match_cases("pkg/sub/test_y.py::test_b", cases)] == ["test_b"]
    assert len(P.match_cases("tests/test_x.py", cases)) == 6

    ok = P.judge_candidate("pytest", 0, cases, ["tests/test_x.py::test_a"])
    assert ok["ok"] and ok["per_id"] == {"tests/test_x.py::test_a": "passed"}
    # skipped / 未执行 / 参数实例有一个失败，都不算候选通过
    assert P.judge_candidate("pytest", 0, cases, ["pkg/sub/test_y.py::test_b"])["per_id"] == {"pkg/sub/test_y.py::test_b": "skipped"}
    assert P.judge_candidate("pytest", 0, cases, ["tests/test_x.py::test_nope"])["per_id"] == {"tests/test_x.py::test_nope": "missing"}
    assert not P.judge_candidate("pytest", 1, cases, ["tests/test_x.py::test_p"])["ok"]

    f = lambda ids, rc=1, cs=cases: P.classify_counterexample("pytest", rc, cs, ids)  # noqa: E731
    assert f(["tests/test_x.py::TestC::test_m"]) == "assertion-failure"
    assert f(["tests/test_x.py::test_raises"]) == "assertion-failure"  # pytest.raises 未触发：删除类任务的负向断言
    assert f(["tests/test_x.py::test_imp"]) == "error"  # call 阶段的 ImportError 也是 <failure>，但不是断言失败
    assert f(["tests/test_x.py::test_a"]) == "error"  # rc=1 但声明的用例没失败
    assert f(["tests/test_x.py::TestC::test_m"], rc=2) == "error" and f(["tests/test_x.py::test_a"], rc=0) == "pass"
    assert f(["tests/test_x.py::TestC::test_m"], cs=cases + [_case("tests.test_z", "test_c", "error", "fixture boom")]) == "error"
    assert P.classify_counterexample("generic", 1, [], ["x"]) == "assertion-failure"


def test_build_argv_uses_frozen_runner(tmp_path):
    runner = {"framework": "pytest", "cmd": "{main_repo}/.venv/bin/python -m pytest -p no:html"}
    argv = P.build_argv(runner, ["tests/t.py::a"], tmp_path / "j.xml", Path("/repo"))
    assert argv[:3] == ["/repo/.venv/bin/python", "-m", "pytest"] and argv[-1] == "tests/t.py::a" and f"--junitxml={tmp_path / 'j.xml'}" in argv
    assert P.build_argv({"framework": "generic", "cmd": "go test {tests} -count=1"}, ["./pkg/..."], tmp_path / "j", Path("/r")) == ["go", "test", "./pkg/...", "-count=1"]
