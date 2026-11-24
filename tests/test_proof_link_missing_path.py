"""B5：link_paths 声明的路径在候选 worktree 里不存在时，需要反例现场的组建立现场就失败：
TEST_PROOF_RUNNER_FAILED、suggested_owner=contract、details 里列出缺失的路径。

起点代码没有链接机制，也就没有这项检查：缺路径时链接根本不会被建立，脚本照常按退出码给出结论
（generic 会把它当 assertion-failure 而 PASS），所以下面每条正向断言在起点上都会在 call 阶段失败
（baseline-red）。reviewed-boundaries 不建现场、不受影响是既有行为，作为守卫不列入 proof_spec。
"""

from __future__ import annotations

import copy

from conftest import CONTRACT, implement_mul, make_tester_result, role_turn

from proof_link_scenario import (GENERIC_RUNNER, PYTEST_RUNNER, add_ignored_dep, commit_scenario,
                                 deliver_patch, install_pytest_wrapper, install_runner, prepare_site,
                                 start_run, write_loop)

TEST_ID = "tests/check.sh"


def _ready(repo, cli, hook, tmp_path, *, link_paths: list[str], present: tuple[str, ...]) -> dict:
    log = tmp_path / "site.log"
    write_loop(repo, runner=GENERIC_RUNNER, link_paths=link_paths, ignore=tuple(present))
    install_runner(repo, log, 'exit 1\n')
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    for name in present:
        add_ignored_dep(ctx, name)
    prepare_site(ctx, cli, hook, {TEST_ID: "#!/usr/bin/env bash\nexit 0\n"}, test_id=TEST_ID)
    return ctx


def _assert_missing(out, expected: set[str]) -> None:
    assert out["result"] == "FAIL", out
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_RUNNER_FAILED", f
    assert f["suggested_owner"] == "contract", f
    # details 合并进了 failure 顶层：缺失集合必须**恰好**等于期望，不多不少。
    # （顶层还有声明的 link_paths，所以在整个 blob 里找子串约束不到缺失集合——missing=[] 也能过。）
    assert set(f["missing"]) == expected, f


def test_b5_missing_declared_link_path_fails_mutation_site(repo, cli, hook, tmp_path):
    ctx = _ready(repo, cli, hook, tmp_path, link_paths=["node_modules", "vendor_deps"],
                 present=("node_modules",))
    deliver_patch(ctx, hook, TEST_ID)
    out = cli("proof", "--session", "S1", expect=1)
    _assert_missing(out, {"vendor_deps"})


def test_b5_multiple_missing_paths_are_all_listed(repo, cli, hook, tmp_path):
    """边界：多个声明里缺两个也失败，且列出全部缺失项。"""
    ctx = _ready(repo, cli, hook, tmp_path, link_paths=["node_modules", "vendor_deps", "extra_deps"],
                 present=("node_modules",))
    deliver_patch(ctx, hook, TEST_ID)
    out = cli("proof", "--session", "S1", expect=1)
    _assert_missing(out, {"vendor_deps", "extra_deps"})


def test_b5_missing_link_path_fails_pytest_baseline_site(repo, cli, hook, tmp_path):
    """baseline 现场同样需要 link_paths：缺路径也判 TEST_PROOF_RUNNER_FAILED（pytest 才有 baseline 现场）。"""
    log = tmp_path / "pytest.log"
    write_loop(repo, runner=PYTEST_RUNNER, link_paths=["vendor_deps"])
    install_pytest_wrapper(repo, log)
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    body = "import src.foo\n\n\ndef test_mul():\n    assert src.foo.mul(3, 4) == 12\n"
    prepare_site(ctx, cli, hook, {"tests/test_mul.py": body}, test_id="tests/test_mul.py::test_mul", kind="baseline-red")
    out = cli("proof", "--session", "S1", expect=1)
    _assert_missing(out, {"vendor_deps"})


# ---------------------------------------------------------------- 守卫：不变量（不列入 proof_spec）


def test_guard_reviewed_boundaries_spec_needs_no_link_check(repo, cli, hook, tmp_path):
    """不变量：该检查只在需要反例现场的组上发生；纯 reviewed-boundaries 的 spec 不受影响。"""
    contract = copy.deepcopy(CONTRACT)
    contract["mission"]["behaviors"][0]["proof"] = "reviewed-boundaries"
    log = tmp_path / "site.log"
    write_loop(repo, runner=GENERIC_RUNNER, link_paths=["vendor_deps"])
    install_runner(repo, log, "exit 1\n")
    commit_scenario(repo)
    ctx = start_run(repo, cli, contract=contract, plan_name="plan-reviewed.md")
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(ctx["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (ctx["tester_worktree"] / TEST_ID).write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    res = make_tester_result("reviewed-boundaries", test_ids=[TEST_ID])
    r = role_turn(hook, "tester", "T1", res, start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
