"""B3：framework=generic 的 mutation 组在打 patch 之前先做一次对照运行（该组自己的 test_ids 与
timeout）。对照现场跑不起来（非 0，例如依赖缺失的 254）时，proof 判 FAIL 且归 contract：
failure.code=TEST_PROOF_RUNNER_FAILED、phase=control、suggested_owner=contract，ledger 里该组不写
assertion-failure 的 counterexample，proof evidence 不是 pass。对照阶段留下的残留先清理，再打 patch。

起点代码没有对照运行：现场未打 patch 就返回非 0，generic 只看退出码会把它记成 assertion-failure，
proof 因此 PASS。所以下面每条正向断言在起点上都会在 call 阶段失败（baseline-red）。
候选阶段失败仍先报 TEST_PROOF_CANDIDATE_FAILED 是不变量，作为守卫不列入 proof_spec。
"""

from __future__ import annotations

import json

from builder_loop import evidence as ev
from builder_loop import ledger as L

from proof_link_scenario import (GENERIC_RUNNER, commit_scenario, deliver_patch, install_runner,
                                 prepare_site, recorded_groups, site_records, start_run, write_loop)

TEST_ID = "tests/check.sh"


def _run(repo, cli, hook, tmp_path, site_body: str) -> dict:
    log = tmp_path / "site.log"
    write_loop(repo, runner=GENERIC_RUNNER)
    install_runner(repo, log, site_body)
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    prepare_site(ctx, cli, hook, {TEST_ID: "#!/usr/bin/env bash\nexit 0\n"}, test_id=TEST_ID)
    deliver_patch(ctx, hook, TEST_ID)
    ctx["log"] = log
    return ctx


def test_b3_control_run_failure_is_runner_failed_owned_by_contract(repo, cli, hook, tmp_path):
    # 现场未打 patch 时依赖缺失：脚本在 IMPL=original 时以 254 退出
    ctx = _run(repo, cli, hook, tmp_path,
               'echo "SITE impl=$IMPL arg=${1:-}" >> "$LOG"\n'
               'if [ "$IMPL" = original ]; then exit 254; fi\nexit 1\n')
    out = cli("proof", "--session", "S1", expect=1)
    assert out["result"] == "FAIL", out
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_RUNNER_FAILED", f
    assert f["suggested_owner"] == "contract", f
    assert f["phase"] == "control", f
    # 对照运行确实发生在现场、拿到的是未打 patch 的实现，且用的是该组自己的 test_id
    sites = site_records(ctx, ctx["log"])
    assert len(sites) == 1, sites
    assert sites[0]["impl"] == "original", sites
    assert sites[0]["arg"] == TEST_ID, sites
    # 该组没有 assertion-failure 的 counterexample 记录
    for g in recorded_groups(ctx["ledger"]):
        assert (g.get("counterexample") or {}).get("classification") != "assertion-failure", g
    # proof evidence 不是 pass
    lg = L.load(ctx["ledger"])
    assert lg["evidence"]["proof"]["status"] != "pass", lg["evidence"]["proof"]
    assert ev.state(lg, "proof", repo.root) != ev.STATE_PASS
    # 失败登记在 ledger 里，且归 contract
    assert lg["failures"]["proof"][-1]["code"] == "TEST_PROOF_RUNNER_FAILED"


def test_b3_control_run_residue_is_cleaned_before_patching(repo, cli, hook, tmp_path):
    """边界：对照阶段留下的残留先清理再打 patch——打 patch 后的现场看不到对照阶段的残留。"""
    ctx = _run(repo, cli, hook, tmp_path,
               'echo "SITE impl=$IMPL residue=$RESIDUE" >> "$LOG"\n'
               'if [ "$IMPL" = original ]; then touch residue.txt; exit 0; fi\n'
               'exit 1\n')
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    sites = site_records(ctx, ctx["log"])
    assert [r["impl"] for r in sites] == ["original", "mutated"], sites
    assert sites[1]["residue"] == "absent", sites[1]


# ---------------------------------------------------------------- 守卫：不变量（不列入 proof_spec）


def test_guard_candidate_stage_failure_still_reported_first(repo, cli, hook, tmp_path):
    """不变量：候选阶段失败仍报 TEST_PROOF_CANDIDATE_FAILED，先于对照实验。"""
    log = tmp_path / "site.log"
    write_loop(repo, runner=GENERIC_RUNNER)
    install_runner(repo, log, "exit 3\n")
    # 去掉候选捷径：让脚本在分支上也失败
    p = repo.root / "scripts" / "check_runner.sh"
    p.write_text(p.read_text().replace(
        "if git symbolic-ref -q HEAD >/dev/null 2>&1; then exit 0; fi\n", ""), encoding="utf-8")
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    prepare_site(ctx, cli, hook, {TEST_ID: "#!/usr/bin/env bash\nexit 0\n"}, test_id=TEST_ID)
    deliver_patch(ctx, hook, TEST_ID)
    out = cli("proof", "--session", "S1", expect=1)
    assert out["failure"]["code"] == "TEST_PROOF_CANDIDATE_FAILED", out["failure"]
    # 候选阶段就失败：没有任何反例现场记录
    assert site_records(ctx, log) == []
    json.dumps(out)  # 结果可序列化（失败 details 不夹带不可序列化对象）
