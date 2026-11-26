"""B3：cleanup 从调用者所在目录判断——站在候选 worktree 里时不回收候选。

必须起真实子进程（cwd 设为候选 worktree），因为进程内 dispatch 的 cwd 是 pytest 的，
测不到 cwd 行为。起点上 finalize 已删掉候选 worktree，无法站在里面，断言在 call 阶段失败
（baseline-red）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from builder_loop import ledger as L

from conftest import RUNTIME, drive_to_proof_pass, reviewer_pass

MSG = "feat(foo): [cr_id_skip] Add mul"


def _sub_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [str(RUNTIME)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_cleanup(repo, run_id: str, cwd: Path) -> subprocess.CompletedProcess:
    # --repo 是顶层参数，必须写在子命令之前
    return subprocess.run(
        [sys.executable, "-m", "builder_loop", "--repo", str(repo.root), "cleanup", "--run", run_id],
        cwd=str(cwd), env=_sub_env(), capture_output=True, text=True,
    )


def _finalize_retro(started, cli, hook, tmp_path) -> None:
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    out = cli("finalize", "--session", "S1", "-m", MSG)
    assert out["terminal"] == "finalized"
    sig = cli("retro", "signals", "--session", "S1")
    f = tmp_path / "retro.json"
    f.write_text(json.dumps({
        "dispositions": [{"signal_id": s["id"], "route": "not_incident", "reason": "测试覆盖"} for s in sig["signals"]],
    }))
    cli("retro", "record", "--session", "S1", "--file", str(f))


def _entry(proc: subprocess.CompletedProcess) -> dict:
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    return out["cleaned"][0]


# ---------------------------------------------------------------- given / when / then


def test_cleanup_skips_candidate_when_cwd_inside_candidate(started, cli, hook, tmp_path):
    repo = started["repo"]
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_retro(started, cli, hook, tmp_path)
    assert wt.is_dir() and twt.is_dir()
    run_id = L.load(started["ledger"])["run_id"]

    entry = _entry(_run_cleanup(repo, run_id, wt))

    assert entry["kept"] == [{"worktree": str(wt), "reason": "调用者当前位于该目录内"}]
    assert entry["removed"] == [str(twt)]
    assert wt.is_dir() and not twt.exists()


def test_boundary_cwd_in_subdir_also_skips(started, cli, hook, tmp_path):
    repo = started["repo"]
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_retro(started, cli, hook, tmp_path)
    assert wt.is_dir() and twt.is_dir()
    sub = wt / "src"
    assert sub.is_dir()
    run_id = L.load(started["ledger"])["run_id"]

    entry = _entry(_run_cleanup(repo, run_id, sub))

    assert entry["kept"] == [{"worktree": str(wt), "reason": "调用者当前位于该目录内"}]
    assert entry["removed"] == [str(twt)]
    assert wt.is_dir() and not twt.exists()


def test_boundary_sibling_prefix_dir_is_not_inside(started, cli, hook, tmp_path):
    """路径包含关系按解析后的路径比较，不是字符串前缀：.../builder-extra 不在 .../builder 内。"""
    repo = started["repo"]
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_retro(started, cli, hook, tmp_path)
    assert wt.is_dir() and twt.is_dir()
    extra = wt.parent / "builder-extra"
    extra.mkdir()
    run_id = L.load(started["ledger"])["run_id"]

    entry = _entry(_run_cleanup(repo, run_id, extra))

    assert entry["kept"] == []
    assert set(entry["removed"]) == {str(wt), str(twt)}
    assert not wt.exists() and not twt.exists()
    assert extra.is_dir()


def test_boundary_cwd_main_repo_removes_candidate(started, cli, hook, tmp_path):
    repo = started["repo"]
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_retro(started, cli, hook, tmp_path)
    assert wt.is_dir() and twt.is_dir()
    run_id = L.load(started["ledger"])["run_id"]

    entry = _entry(_run_cleanup(repo, run_id, repo.root))

    assert entry["kept"] == []
    assert set(entry["removed"]) == {str(wt), str(twt)}
    assert not wt.exists() and not twt.exists()


def test_boundary_deleted_cwd_treated_as_not_inside(started, cli, hook, tmp_path):
    """os.getcwd 抛 OSError 时不报错，按不在其中处理。"""
    repo = started["repo"]
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_retro(started, cli, hook, tmp_path)
    assert wt.is_dir() and twt.is_dir()
    run_id = L.load(started["ledger"])["run_id"]
    code = (
        "import os, sys, tempfile\n"
        "d = tempfile.mkdtemp()\n"
        "os.chdir(d)\n"
        "os.rmdir(d)\n"
        "from builder_loop.cli import main\n"
        "sys.exit(main(['--repo', %r, 'cleanup', '--run', %r]))\n" % (str(repo.root), run_id)
    )

    proc = subprocess.run([sys.executable, "-c", code], cwd=str(repo.root), env=_sub_env(),
                          capture_output=True, text=True)

    entry = _entry(proc)
    assert entry["kept"] == []
    assert set(entry["removed"]) == {str(wt), str(twt)}
    assert not wt.exists() and not twt.exists()