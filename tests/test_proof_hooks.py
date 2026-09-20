import os
import subprocess
import sys
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
    assert str(twt) in ctx and str(wt) not in ctx and started["run_id"] + "/candidate" not in ctx
    assert "a or b is 0" in ctx and "add() unchanged" in ctx and "baseline-red / mutation" in ctx
    assert "不需要你给 argv" in ctx and "冻结基线" in ctx
    # 注入上下文 = brief 的文本形态，同一来源；写边界只说 tester 自己的（#240）
    assert ctx == cli("brief", "--session", "S1", "--role", "tester")
    assert "src/**" not in ctx and "[write_tests]" in ctx  # 不再复述 builder 的写边界，只说自己的

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
    assert str(wt) in ctx2 and "[add_mutation_patch]" in ctx2
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


def test_resumed_tester_declining_keeps_valid_evidence(started, cli, hook):
    """真实 E2E 里抓到的：续接轮 tester 补不出 patch（没拿到候选路径）交了 insufficient_spec，
    测试没变、原证据依然成立，不能被覆盖成 fail。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    st = cli("status", "--session", "S1")
    # 门铃只指路，不带事实：候选路径要 tester 自己 `bl brief` 取（#243）
    doorbell = st["briefs"]["resume_tester"]
    assert st["readiness"]["next_action"] == "resume_tester"
    assert str(wt) not in doorbell and "brief --run" in doorbell and "--role tester" in doorbell
    b = cli("brief", "--session", "S1", "--role", "tester", "--json")
    assert b["candidate_readable"] and b["candidate_worktree"] == str(wt)
    assert [t["what"] for t in b["todo"]] == ["add_mutation_patch"]
    r = role_turn(hook, "tester", "T1", {"role": "tester", "status": "insufficient_spec", "notes": "没拿到候选路径"})
    assert r["code"] == 0
    lg = L.load(started["ledger"])
    assert lg["evidence"]["tester"]["status"] == "pass" and lg["proof_spec"]
    assert lg["events"][-1]["status"] == "declined" and "候选路径" in lg["events"][-1]["notes"]
    # 它答复过了 → 不再无限续接；硬跑 proof 得到「缺 patch，回 tester」并计入 stall 计数
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "proof"
    assert cli("proof", "--session", "S1", expect=1)["failure"]["code"] == "TEST_MUTATION_PATCH_MISSING"
    # 首轮（还没有通过的证据）交 insufficient_spec 仍然记 fail
    assert "bl brief" in hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})["json"]["hookSpecificOutput"]["additionalContext"]


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
    # call 阶段的 ImportError 也是 <failure>：C1/C3 要求分类不得依赖失败信息文本内容，
    # 唯一的 declared failure、无 error 条目 → assertion-failure（不再按消息前缀判定）
    assert f(["tests/test_x.py::test_imp"]) == "assertion-failure"
    assert f(["tests/test_x.py::test_a"]) == "error"  # rc=1 但声明的用例没失败
    assert f(["tests/test_x.py::TestC::test_m"], rc=2) == "error" and f(["tests/test_x.py::test_a"], rc=0) == "pass"
    assert f(["tests/test_x.py::TestC::test_m"], cs=cases + [_case("tests.test_z", "test_c", "error", "fixture boom")]) == "error"
    assert P.classify_counterexample("generic", 1, [], ["x"]) == "assertion-failure"


def test_build_argv_uses_frozen_runner(tmp_path):
    runner = {"framework": "pytest", "cmd": "{main_repo}/.venv/bin/python -m pytest -p no:html"}
    argv = P.build_argv(runner, ["tests/t.py::a"], tmp_path / "j.xml", Path("/repo"))
    assert argv[:3] == ["/repo/.venv/bin/python", "-m", "pytest"] and argv[-1] == "tests/t.py::a" and f"--junitxml={tmp_path / 'j.xml'}" in argv
    assert P.build_argv({"framework": "generic", "cmd": "go test {tests} -count=1"}, ["./pkg/..."], tmp_path / "j", Path("/r")) == ["go", "test", "./pkg/...", "-count=1"]


def test_brief_is_the_single_source_for_role_facts(started, cli, hook, repo):
    """#240 / #243：归属、可读性、待办一律由 brief 现算；builder 的消息只是门铃。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    b = cli("brief", "--session", "S1", "--role", "tester", "--json")
    assert b["write_paths"] == ["tests/**"] and b["candidate_readable"] is False and b["candidate_worktree"] is None
    assert [t["what"] for t in b["todo"]] == ["write_tests"]

    # tester 在自己的 worktree 里按 --run 自取（PATH 与 cwd 都不靠谱，所以门铃给绝对路径 + run id）
    out = subprocess.run([sys.executable, "-m", "builder_loop", "--repo", str(twt), "brief",
                          "--run", started["run_id"], "--role", "tester"], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "runtime")})
    assert out.returncode == 0 and started["run_id"] in out.stdout
    # 重取命令必须是已安装 runtime 的绝对路径：裸 `bl` 在 PATH 缺失时逼角色去搜，
    # 真实 dogfood 里 reviewer 搜到并用了候选 worktree 里的 bin/bl（正在被审查的那份代码）
    from builder_loop.run import bl_bin
    assert f"`{bl_bin()} brief --run" in out.stdout and bl_bin().startswith("/")

    # 交卷 → 没有待办；contract 一改 evidence 失效 → 待办自己回来，不需要谁去通知它
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("baseline-red"))
    assert cli("brief", "--session", "S1", "--role", "tester", "--json")["todo"] == []
    c2 = contract_with(**{"mission.revision": 2, "mission.objective": "Add mul() and div()"})
    write_plan(repo.root, c2, "plan.md")
    cli("contract", "--session", "S1", "revise", "--plan", str(repo.root / "plan.md"), "--authorize")
    b2 = cli("brief", "--session", "S1", "--role", "tester", "--json")
    assert b2["contract_revision"] == 2 and b2["todo"][0]["what"] == "write_tests" and "revision 2" in b2["todo"][0]["why"]


def test_reviewer_brief_carries_previous_findings(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("baseline-red"))
    b = cli("brief", "--session", "S1", "--role", "reviewer", "--json")
    assert b["todo"][0]["what"] == "review" and b["diff_range"].startswith(started["target_start_head"])
    finding = {"severity": "blocking", "owner": "builder", "file": "src/foo.py", "line": 1, "summary": "边界没处理"}
    role_turn(hook, "reviewer", "R1", {"role": "reviewer", "verdict": "changes_requested", "findings": [finding], "behaviors_verified": []})
    b2 = cli("brief", "--session", "S1", "--role", "reviewer", "--json")
    assert b2["todo"][0]["previous_findings"] == [finding]


def test_proof_runner_failure_is_not_the_builders_fault(started, cli, hook):
    """runner 起不来两个角色都改不了：归 contract，不是让 builder 去改实现（#241）。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    cli("machine", "--session", "S1")
    role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt)))
    # 冻结的 runner 在这台机器上起不来（典型：项目走 uv，loop.yml 没配 proof_runner）
    with L.mutate(started["ledger"]) as x:
        x["contract"]["assurance"]["proof_runner"] = {"framework": "pytest", "cmd": "python3 -c 'import sys; sys.exit(4)' --"}
    out = cli("proof", "--session", "S1", expect=1)
    assert out["failure"]["code"] == "TEST_PROOF_RUNNER_FAILED" and out["failure"]["suggested_owner"] == "contract"


@pytest.mark.parametrize("line,needle", [
    # #244：肉眼看不出毛病、却曾一律被报成「缺少结果标记」的三种写法
    ('BUILDER_LOOP_RESULT: {"role":"tester","status":"pass","notes":"匹配 \\d+"}', "JSON 解析失败"),
    ('BUILDER_LOOP_RESULT: {"role":"tester","status":"pass","notes":"a\tb"}', "JSON 解析失败"),
    ('BUILDER_LOOP_RESULT： {"role":"tester","status":"pass"}', "格式不对"),
    ("交付完成，测试都写好了。", "没有找到 BUILDER_LOOP_RESULT 行"),
])
def test_malformed_marker_says_why(started, hook, line, needle):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = role_turn(hook, "tester", "T1", None, start=False, message="## 交付\n说明段落\n" + line)
    assert r["code"] == 2 and needle in r["stderr"], r["stderr"]
    # 原因要落进 ledger：builder 看不到 hook 解析的原文，只能从这里对照
    ev = L.load(started["ledger"])["events"][-1]
    assert ev["kind"] == "role_malformed" and needle in ev["reason"]


def test_parse_result_marker_points_at_the_bad_spot():
    from builder_loop.hooks import parse_result_marker
    payload, err = parse_result_marker('BUILDER_LOOP_RESULT: {"role":"tester","notes":"C:\\Users\\x"}')
    assert payload is None and "C:" in err and "\\\\" in err  # 回显出错位置，并告诉它怎么写
    payload, err = parse_result_marker('前文\nBUILDER_LOOP_RESULT: {"role":"tester","status":"pass"}\n\n')
    assert payload == {"role": "tester", "status": "pass"} and err is None  # 标记后的空行不影响
