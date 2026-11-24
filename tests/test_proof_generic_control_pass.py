"""B4：framework=generic 的 mutation 组先做对照运行（未打 patch 的现场）——返回 0 说明现场本身能跑，
之后打 patch 再返回非 0 才算 assertion-failure、proof 可 PASS；对照运行超时判
TEST_PROOF_GROUP_TIMEOUT 且 phase=control；framework=pytest 的 mutation 组不执行对照运行，行为与现状一致。

起点代码没有对照运行：generic 的现场能跑与否从不被观察，对照阶段的这几次执行根本不存在。下面涉及
对照运行的正向断言（PASS + 两次现场记录、超时的 phase）在起点上都会在 call 阶段失败（baseline-red）。
pytest 不做对照运行、对照 0 而 patch 后仍 0 判 TEST_MUTATION_SURVIVED 是同样成立的旧行为，作为守卫
不列入 proof_spec。
"""

from __future__ import annotations

from proof_link_scenario import (GENERIC_RUNNER, PYTEST_RUNNER, add_ignored_dep, commit_scenario,
                                 deliver_patch, install_pytest_wrapper, install_runner, prepare_site,
                                 site_records, start_run, write_loop)

TEST_ID = "tests/check.sh"
GROUP_TIMEOUT = 15
SLOW = "sleep 40\n"


def _generic(repo, cli, hook, tmp_path, site_body: str, *, timeout: int | None = None) -> dict:
    log = tmp_path / "site.log"
    write_loop(repo, runner=GENERIC_RUNNER)
    install_runner(repo, log, site_body)
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    prepare_site(ctx, cli, hook, {TEST_ID: "#!/usr/bin/env bash\nexit 0\n"}, test_id=TEST_ID)
    deliver_patch(ctx, hook, TEST_ID, timeout=timeout)
    ctx["log"] = log
    return ctx


def test_b4_control_passes_then_mutated_fails_means_assertion_failure(repo, cli, hook, tmp_path):
    # 现场能跑（未打 patch 时 0），打上 patch 后 1：两次执行都要留下现场记录，顺序是 original → mutated
    ctx = _generic(repo, cli, hook, tmp_path,
                   'echo "SITE impl=$IMPL" >> "$LOG"\n'
                   'if [ "$IMPL" = mutated ]; then exit 1; fi\nexit 0\n')
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    order = [r["impl"] for r in site_records(ctx, ctx["log"])]
    assert order == ["original", "mutated"], order
    assert out["groups"][0]["counterexample"]["classification"] == "assertion-failure", out


def test_b4_control_timeout_is_group_timeout_phase_control(repo, cli, hook, tmp_path):
    ctx = _generic(repo, cli, hook, tmp_path,
                   'echo "SITE impl=$IMPL" >> "$LOG"\n' + SLOW + "exit 0\n",
                   timeout=GROUP_TIMEOUT)
    out = cli("proof", "--session", "S1", expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_GROUP_TIMEOUT", f
    assert f["phase"] == "control", f
    assert f["suggested_owner"] == "tester", f
    # 对照运行确实发生在未打 patch 的现场
    sites = site_records(ctx, ctx["log"])
    assert len(sites) == 1 and sites[0]["impl"] == "original", sites


def test_b4_pytest_mutation_group_runs_no_control_execution(repo, cli, hook, tmp_path):
    log = tmp_path / "pytest.log"
    write_loop(repo, runner=PYTEST_RUNNER, ignore=("node_modules",))
    install_pytest_wrapper(repo, log)
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    add_ignored_dep(ctx, "node_modules")
    body = "import src.foo\n\n\ndef test_mul():\n    assert src.foo.mul(3, 4) == 12\n"
    prepare_site(ctx, cli, hook, {"tests/test_mul.py": body}, test_id="tests/test_mul.py::test_mul")
    deliver_patch(ctx, hook, "tests/test_mul.py::test_mul")

    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    # 只有 mutation 现场（mutated）一次执行；没有对照的 original 那次
    sites = site_records(ctx, log)
    assert [r["impl"] for r in sites] == ["mutated"], sites


# ---------------------------------------------------------------- 守卫：不变量（不列入 proof_spec）


def test_guard_control_passes_and_mutated_also_passes_is_mutation_survived(repo, cli, hook, tmp_path):
    """边界：对照返回 0 而 patch 后仍返回 0 → 仍判 TEST_MUTATION_SURVIVED。"""
    ctx = _generic(repo, cli, hook, tmp_path, "exit 0\n")
    out = cli("proof", "--session", "S1", expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_MUTATION_SURVIVED", f
    assert f["suggested_owner"] == "tester", f
