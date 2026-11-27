"""B1：`bl doctor` / `bl runs` 接受写在子命令之后的 `--repo`。

起点上这两个子命令没有挂 `--repo`，argparse 直接 `unrecognized arguments` / exit 2：
`test_b1_doctor_accepts_repo_after_subcommand`、`test_b1_runs_accepts_repo_after_subcommand` 与两条
「子命令后的 --repo 覆盖顶层」用例都在 call 阶段断言失败（baseline-red）。

只检查现役行为、不证明本次改动的用例（顶层 --repo 仍可用、仍拒绝 --run / --session、
contract fields / hook 不挂 --repo、无 --repo 时按 cwd、其余子命令 --repo 既有行为）是守卫，
不进 proof_spec；它们仍由 machine 全量运行。

子进程辅助一律复用 tests/test_cleanup_repo_locator.py::_run_bl（不另造等价物）。
"""

from __future__ import annotations

import json

from test_cleanup_repo_locator import _run_bl


def _elsewhere(tmp_path):
    """仓外的一个普通目录：不写 --repo 时它既不是 git 仓库，也不会命中当前仓。"""
    d = tmp_path / "elsewhere"
    d.mkdir()
    return d


# ---------------------------------------------------------------- proof：子命令后 --repo


def test_b1_doctor_accepts_repo_after_subcommand(repo, tmp_path):
    elsewhere = _elsewhere(tmp_path)

    proc = _run_bl(["doctor", "--repo", str(repo.root)], elsewhere)

    assert "unrecognized arguments" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    out = json.loads(proc.stdout)
    assert "healthy" in out, out

    # 与顶层写法逐字节相同
    top = _run_bl(["--repo", str(repo.root), "doctor"], elsewhere)
    assert top.returncode == 0, (top.returncode, top.stderr)
    assert proc.stdout.encode() == top.stdout.encode(), (proc.stdout[-2000:], top.stdout[-2000:])


def test_b1_runs_accepts_repo_after_subcommand(repo, tmp_path):
    elsewhere = _elsewhere(tmp_path)

    proc = _run_bl(["runs", "--repo", str(repo.root)], elsewhere)

    assert "unrecognized arguments" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    out = json.loads(proc.stdout)
    assert out.get("ok") is True, out
    assert "runs" in out and isinstance(out["runs"], list), out

    top = _run_bl(["--repo", str(repo.root), "runs"], elsewhere)
    assert top.returncode == 0, (top.returncode, top.stderr)
    assert proc.stdout.encode() == top.stdout.encode(), (proc.stdout[-2000:], top.stdout[-2000:])


def test_b1_boundary_subcommand_repo_overrides_top_level_doctor(repo, tmp_path):
    """边界：两处都给时以子命令后的为准（顶层指向不存在的路径也不影响定位）。"""
    elsewhere = _elsewhere(tmp_path)
    missing = tmp_path / "no-such-repo"

    proc = _run_bl(["--repo", str(missing), "doctor", "--repo", str(repo.root)], elsewhere)

    assert "unrecognized arguments" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    out = json.loads(proc.stdout)
    assert (out.get("repo") or {}).get("root") == str(repo.root.resolve()), out


def test_b1_boundary_subcommand_repo_overrides_top_level_runs(repo, tmp_path):
    elsewhere = _elsewhere(tmp_path)
    missing = tmp_path / "no-such-repo"

    proc = _run_bl(["--repo", str(missing), "runs", "--repo", str(repo.root)], elsewhere)

    assert "unrecognized arguments" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    out = json.loads(proc.stdout)
    assert out.get("ok") is True, out
    assert isinstance(out.get("runs"), list), out


# ---------------------------------------------------------------- 守卫（不进 proof_spec）


def test_b1_guard_doctor_rejects_run_and_session(repo, tmp_path):
    """doctor 不消费 --run / --session：仍要在解析期被拒绝。"""
    elsewhere = _elsewhere(tmp_path)
    for locator in (["--run", "x"], ["--session", "x"]):
        proc = _run_bl(["--repo", str(repo.root), "doctor", *locator], elsewhere)
        assert proc.returncode == 2, (locator, proc.returncode, proc.stdout, proc.stderr)
        assert "unrecognized arguments" in proc.stderr, (locator, proc.stderr)


def test_b1_guard_runs_rejects_run_and_session(repo, tmp_path):
    """runs 不消费 --run / --session：仍要在解析期被拒绝。"""
    elsewhere = _elsewhere(tmp_path)
    for locator in (["--run", "x"], ["--session", "x"]):
        proc = _run_bl(["--repo", str(repo.root), "runs", *locator], elsewhere)
        assert proc.returncode == 2, (locator, proc.returncode, proc.stdout, proc.stderr)
        assert "unrecognized arguments" in proc.stderr, (locator, proc.stderr)


def test_b1_guard_top_level_repo_still_works(repo, tmp_path):
    """顶层 --repo 仍可用，且只给顶层时不会被子命令默认值抹成 None。"""
    elsewhere = _elsewhere(tmp_path)

    doc = _run_bl(["--repo", str(repo.root), "doctor"], elsewhere)
    assert doc.returncode == 0, (doc.returncode, doc.stderr)
    d = json.loads(doc.stdout)
    assert "repo" in d, d
    assert d["repo"].get("root") == str(repo.root.resolve()), d["repo"]

    run = _run_bl(["--repo", str(repo.root), "runs"], elsewhere)
    assert run.returncode == 0, (run.returncode, run.stderr)
    assert json.loads(run.stdout).get("ok") is True, run.stdout


def test_b1_guard_no_repo_locates_cwd_unchanged(repo, tmp_path):
    """两处都不写 --repo：runs 按当前目录定位，doctor 行为不变。"""
    from_cwd = _run_bl(["runs"], repo.root)
    assert from_cwd.returncode == 0, (from_cwd.returncode, from_cwd.stderr)
    assert json.loads(from_cwd.stdout).get("ok") is True, from_cwd.stdout
    assert json.loads(from_cwd.stdout)["runs"] == [], from_cwd.stdout

    # 仓外且没有 --repo：不虚构当前仓（按 cwd 定位失败是真实报错）
    elsewhere = _elsewhere(tmp_path)
    from_elsewhere = _run_bl(["runs"], elsewhere)
    assert from_elsewhere.returncode != 0, (from_elsewhere.returncode, from_elsewhere.stdout, from_elsewhere.stderr)

    doc = _run_bl(["doctor"], repo.root)
    assert doc.returncode == 0, (doc.returncode, doc.stderr)
    d = json.loads(doc.stdout)
    assert "healthy" in d, d
    # 改动前 doctor(None) 不做仓库解析：不加 --repo 时输出里没有 repo 块
    assert "repo" not in d, d


def test_b1_guard_contract_fields_and_hook_reject_repo(repo, tmp_path):
    """不消费 args.repo 的子命令不挂 --repo：contract fields 与 hook 仍拒绝子命令后的 --repo。"""
    elsewhere = _elsewhere(tmp_path)

    fields = _run_bl(["--repo", str(repo.root), "contract", "fields", "--repo", str(repo.root)], elsewhere)
    assert fields.returncode == 2, (fields.returncode, fields.stdout, fields.stderr)
    assert "unrecognized arguments" in fields.stderr, fields.stderr

    hook = _run_bl(["--repo", str(repo.root), "hook", "PreToolUse", "--repo", str(repo.root)], elsewhere)
    assert hook.returncode == 2, (hook.returncode, hook.stdout, hook.stderr)
    assert "unrecognized arguments" in hook.stderr, hook.stderr


def test_b1_guard_runs_missing_repo_errors_not_current(repo, tmp_path):
    """--repo 指向不存在的路径：真实报错，不静默作用在当前仓。"""
    missing = tmp_path / "no-such-repo"

    proc = _run_bl(["runs", "--repo", str(missing)], repo.root)
    data = json.loads(proc.stdout) if proc.stdout.strip() else None
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert not (isinstance(data, dict) and data.get("ok") is True), data


def test_b1_guard_doctor_missing_repo_not_current(repo, tmp_path):
    """doctor 遇到不存在的 --repo 时也要暴露错误，不能悄悄落在当前仓上。"""
    missing = tmp_path / "no-such-repo"

    proc = _run_bl(["doctor", "--repo", str(missing)], repo.root)
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    repo_block = out.get("repo") or {}
    assert proc.returncode != 0 or "error" in repo_block, (proc.returncode, out)
    assert repo_block.get("root") != str(repo.root.resolve()), repo_block


def test_b1_guard_other_subcommand_repo_unchanged(repo, tmp_path):
    """其余子命令（以 status 为代表）接受子命令后 --repo 的既有行为不变。"""
    elsewhere = _elsewhere(tmp_path)

    proc = _run_bl(["status", "--repo", str(repo.root)], elsewhere)

    assert "unrecognized arguments" not in proc.stderr, proc.stderr
    assert json.loads(proc.stdout)["code"] == "RUN_AMBIGUOUS", proc.stdout