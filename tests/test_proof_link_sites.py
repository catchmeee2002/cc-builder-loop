"""B2：link_paths 让 baseline / mutation 临时 worktree 里被忽略的依赖目录与候选同源（符号链接），
runner 在其中能读到候选目录里的文件；链接本身不会被当成「执行期间现场被改动」。

依赖目录用**目录型** ignore（`node_modules/`）声明：git 因此不会把建立在这些路径上的符号链接当成
被忽略文件，链接会以 `?? node_modules` 出现在 status 里。runtime 必须用 _under_link 把它从「现场
被改动」的 extra 里排除，proof 才能 PASS——否则会判 TEST_MUTATION_INVALID。

起点代码完全忽略 link_paths，临时现场里没有这些链接，所以下面每条正向断言在起点上都会在 call
阶段失败（baseline-red）。「未声明时不建任何额外链接」「执行期间改了别的文件仍判
TEST_MUTATION_INVALID」是既有行为，作为守卫不列入 proof_spec。

注：generic 的 mutation 现场在打 patch 前还有一次对照运行（B4），它同样需要这些链接；因此这里对
每个反例现场的记录都断言链接成立，而不假设只有一次执行。
"""

from __future__ import annotations

import os

from proof_link_scenario import (GENERIC_RUNNER, MARKER, PYTEST_RUNNER, add_ignored_dep, commit_scenario,
                                 deliver_patch, install_pytest_wrapper, install_runner, prepare_site,
                                 site_records, snapshot, start_run, write_loop)

TEST_ID = "tests/check.sh"


def _record(parts: str) -> str:
    return 'echo "SITE impl=$IMPL ' + parts + '" >> "$LOG"\n'


def _generic_ready(repo, cli, hook, tmp_path, *, site_body: str, link_paths=None, deps=("node_modules",)):
    log = tmp_path / "site.log"
    write_loop(repo, runner=GENERIC_RUNNER, link_paths=link_paths, ignore=tuple(deps))
    install_runner(repo, log, site_body)
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    for name in deps:
        add_ignored_dep(ctx, name)
    prepare_site(ctx, cli, hook, {TEST_ID: "#!/usr/bin/env bash\nexit 0\n"}, test_id=TEST_ID)
    return ctx, log


# 对照现场（未打 patch）返回 0、mutation 现场（打了 patch）返回 1：proof 才能走到待观察的现场
_PASS_CONTROL_FAIL_MUTATION = 'if [ "$IMPL" = mutated ]; then exit 1; fi\nexit 0\n'


def test_generic_mutation_site_links_each_declared_ignored_dir_to_candidate(repo, cli, hook, tmp_path):
    """多个 link_paths 全部建立，指向候选同名目录、能读到候选文件；链接在 status 里可见（??）却
    不算 extra、proof 仍 PASS；候选现场前后一致。"""
    fields = ("nm=$(link_state node_modules) nm_target=$(readlink -f node_modules) "
              "nm_marker=$(marker_ok node_modules && echo ok || echo missing) "
              "nm_status=$(status_kind node_modules) "
              "vd=$(link_state vendor_deps) vd_target=$(readlink -f vendor_deps) "
              "vd_marker=$(marker_ok vendor_deps && echo ok || echo missing) "
              "vd_status=$(status_kind vendor_deps)")
    ctx, log = _generic_ready(repo, cli, hook, tmp_path,
                              site_body=_record(fields) + _PASS_CONTROL_FAIL_MUTATION,
                              link_paths=["node_modules", "vendor_deps"],
                              deps=("node_modules", "vendor_deps"))
    deliver_patch(ctx, hook, TEST_ID)
    before = snapshot(ctx["worktree"])

    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out

    sites = site_records(ctx, log)
    assert sites, "反例现场没有执行过 runner"
    assert "mutated" in [r["impl"] for r in sites], sites
    mutated = [r for r in sites if r["impl"] == "mutated"]
    assert len(mutated) == 1, sites
    rec = mutated[0]
    for name, short in (("node_modules", "nm"), ("vendor_deps", "vd")):
        assert rec[f"{short}"] == "symlink", rec
        assert rec[f"{short}_target"] == os.path.realpath(ctx["worktree"] / name), rec
        assert rec[f"{short}_marker"] == "ok", rec
        # 链接在 status 里以 ?? 出现（目录型 ignore 不忽略符号链接）——证明 _under_link 的排除真的被触发
        assert rec[f"{short}_status"] == "untracked", rec
    # 链接不改候选：proof 前后候选 worktree 内容一致
    assert snapshot(ctx["worktree"]) == before


def test_pytest_mutation_site_reads_ignored_dependency_through_link(repo, cli, hook, tmp_path):
    """pytest 框架同样建立链接：测试经链接读到候选 marker，patch 后的实现被抓住（assertion-failure）；
    链接可见于 status 却不被算作现场被改动。"""
    log = tmp_path / "pytest.log"
    write_loop(repo, runner=PYTEST_RUNNER, link_paths=["node_modules"], ignore=("node_modules",))
    install_pytest_wrapper(repo, log, node_modules=True)
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    add_ignored_dep(ctx, "node_modules")
    test_body = (
        "import src.foo\n\n\n"
        "def test_mul_needs_dependency():\n"
        f"    assert {MARKER!r} in open('node_modules/marker').read()\n"
        "    assert src.foo.mul(3, 4) == 12\n"
    )
    prepare_site(ctx, cli, hook, {"tests/test_mul.py": test_body}, test_id="tests/test_mul.py::test_mul_needs_dependency")
    deliver_patch(ctx, hook, "tests/test_mul.py::test_mul_needs_dependency")
    before = snapshot(ctx["worktree"])

    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    assert out["groups"][0]["counterexample"]["classification"] == "assertion-failure", out

    sites = site_records(ctx, log)
    assert len(sites) == 1, sites  # pytest 不做对照运行，只有 mutation 现场
    assert sites[0]["impl"] == "mutated"
    assert sites[0]["link"] == "symlink" and sites[0]["marker"] == "ok", sites
    assert sites[0]["link_status"] == "untracked", sites  # 链接可见于 status
    assert snapshot(ctx["worktree"]) == before


def test_tracked_path_in_temp_site_is_not_overwritten_by_a_link(repo, cli, hook, tmp_path):
    """边界：临时 worktree 里该路径已被 git 跟踪时不覆盖，保持 checkout 内容；其余声明的忽略目录照常建链接
    （链接可见于 status，但不算现场被改动）。"""
    fields = ("nm=$(link_state node_modules) nm_marker=$(marker_ok node_modules && echo ok || echo missing) "
              "nm_status=$(status_kind node_modules) "
              "vendor=$(link_state vendor) vendor_marker=$(marker_ok vendor && echo ok || echo missing)")
    log = tmp_path / "site.log"
    write_loop(repo, runner=GENERIC_RUNNER, link_paths=["vendor", "node_modules"], ignore=("node_modules",))
    install_runner(repo, log, _record(fields) + _PASS_CONTROL_FAIL_MUTATION)
    # vendor 是**被跟踪**的目录：临时现场里必须保持 checkout 出来的一份真实目录
    (repo.root / "vendor").mkdir(parents=True, exist_ok=True)
    (repo.root / "vendor" / "marker").write_text(MARKER + "\n", encoding="utf-8")
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    add_ignored_dep(ctx, "node_modules")
    prepare_site(ctx, cli, hook, {TEST_ID: "#!/usr/bin/env bash\nexit 0\n"}, test_id=TEST_ID)
    deliver_patch(ctx, hook, TEST_ID)

    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    sites = site_records(ctx, log)
    assert sites, "反例现场没有执行过 runner"
    for rec in sites:
        assert rec["nm"] == "symlink" and rec["nm_marker"] == "ok", rec
        assert rec["nm_status"] == "untracked", rec  # 链接可见于 status
        assert rec["vendor"] == "other", rec      # 真实目录，不是符号链接
        assert rec["vendor_marker"] == "ok", rec  # 内容来自 checkout


# ---------------------------------------------------------------- 守卫：不变量（不列入 proof_spec）


def test_guard_tracked_dir_declared_as_link_path_still_flags_internal_writes(repo, cli, hook, tmp_path):
    """不变量：声明为 link_paths 的路径若其实是**被 git 跟踪**的真实目录，runner 运行期往它内部写
    新文件仍算现场改动——_under_link 只精确放行链接本身（`?? vendor`），不放过它内部的新文件
    （`?? vendor/x`）。把 _under_link 改回前缀匹配后本条会变绿，所以它守护该精确匹配修复。"""
    log = tmp_path / "site.log"
    write_loop(repo, runner=GENERIC_RUNNER, link_paths=["vendor"])
    # 对照（未打 patch）跑通、mutation 跑失败，证明 extra 检查不依赖退出码
    install_runner(repo, log,
                   "echo generated > vendor/during-run.txt\n" + _PASS_CONTROL_FAIL_MUTATION)
    # vendor 是**被跟踪**的目录（不是被忽略的安装产物）：临时现场里 checkout 出真实目录，不建链接
    (repo.root / "vendor").mkdir(parents=True, exist_ok=True)
    (repo.root / "vendor" / "marker").write_text(MARKER + "\n", encoding="utf-8")
    commit_scenario(repo)
    ctx = start_run(repo, cli)
    prepare_site(ctx, cli, hook, {TEST_ID: "#!/usr/bin/env bash\nexit 0\n"}, test_id=TEST_ID)
    deliver_patch(ctx, hook, TEST_ID)

    out = cli("proof", "--session", "S1", expect=1)
    assert out["failure"]["code"] == "TEST_MUTATION_INVALID", out
    # extra_paths 必须点名被跟踪目录内部新写的文件（不是链接本身，那条已由 _under_link 放行）
    assert "vendor/during-run.txt" in out["failure"].get("extra_paths", []), out["failure"]


def test_guard_no_links_when_link_paths_undeclared(repo, cli, hook, tmp_path):
    """不变量：未声明 link_paths 时临时现场不出现任何额外链接（依赖目录不存在）。"""
    ctx, log = _generic_ready(repo, cli, hook, tmp_path,
                              site_body=_record("nm=$(link_state node_modules)") + _PASS_CONTROL_FAIL_MUTATION,
                              deps=("node_modules",))
    deliver_patch(ctx, hook, TEST_ID)
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    assert all(r["nm"] == "absent" for r in site_records(ctx, log))


def test_guard_link_visible_but_other_stray_file_still_mutation_invalid(repo, cli, hook, tmp_path):
    """不变量：链接在 status 里可见却被排除，但同期别的 stray 文件仍判 TEST_MUTATION_INVALID——
    _under_link 只放行声明的链接路径，不是把所有现场改动都放过。"""
    ctx, log = _generic_ready(repo, cli, hook, tmp_path,
                              site_body=_record("nm=$(link_state node_modules) nm_status=$(status_kind node_modules)")
                                        + 'echo x > stray.txt\n',
                              link_paths=["node_modules"])
    deliver_patch(ctx, hook, TEST_ID)
    out = cli("proof", "--session", "S1", expect=1)
    assert out["failure"]["code"] == "TEST_MUTATION_INVALID", out
    # 链接确实可见（?? 出现在 status），证明这条失败来自 stray，而不是链接
    rec = [r for r in site_records(ctx, log) if r["impl"] == "mutated"][0]
    assert rec["nm"] == "symlink" and rec["nm_status"] == "untracked", rec
    assert "stray.txt" in out["failure"].get("extra_paths", []), out["failure"]
