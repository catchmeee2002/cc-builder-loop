"""B6/B7：cleanup 的 `--repo` 定位参数。

B6：`bl cleanup --run <id> --repo <主仓根>`（--repo 在子命令之后）能被正常解析并执行回收，
    退出码不为 2，stderr 不含 `unrecognized arguments`；`--repo` 在顶层仍可执行；`--run` 仍只回收
    指定 run；不存在的 run id 报 RUN_NOT_FOUND 而不是参数错误。
B7：在仓 A 的目录下用 `--repo` 指向仓 B，回收的是仓 B 的 run，仓 A 不受影响；`--repo` 指向不存在
    的路径时是真实报错，而不是静默作用于当前仓。

起点上 cleanup 子命令没有 `--repo`，argparse 直接 `unrecognized arguments` / exit 2 —— 下列 proof
用例在 call 阶段失败（baseline-red）。顶层 `--repo` 仍可用是既有行为，只作守卫。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from builder_loop import ledger as L

from conftest import RUNTIME, git


def _sub_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [str(RUNTIME)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_bl(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "builder_loop", *args],
        cwd=str(cwd), env=_sub_env(), capture_output=True, text=True,
    )


def _init_repo(path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "config", "user.email", "t@t")
    git(path, "config", "user.name", "t")
    (path / "src").mkdir()
    (path / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n")
    (path / ".gitignore").write_text(".claude/builder-loop/\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "chore(fixture): [cr_id_skip] Init")


def _make_reclaimable(repo_root, run_id: str, base) -> Path:
    """造一个「终态、已复盘、候选目录仍在」的 run，返回候选 worktree 路径。"""
    head = git(repo_root, "rev-parse", "HEAD")
    branch = f"builder-loop/{run_id}/candidate"
    wt = base / run_id / "builder"
    wt.parent.mkdir(parents=True, exist_ok=True)
    git(repo_root, "worktree", "add", "-b", branch, str(wt), head)
    lg = L.new_ledger(run_id=run_id)
    lg["session"] = {"owner_session_id": None}
    lg["repo"] = {"root": str(repo_root), "target_branch": "main", "target_start_head": head}
    lg["candidate"] = {"branch": branch, "worktree": str(wt), "head": head, "checkpoints": []}
    lg["tester"] = None
    lg["terminal"] = {"status": "finalized", "final_head": None, "reason": None, "at": L.now_iso()}
    lg["retrospective"] = {"at": L.now_iso(), "dispositions": [], "observations": []}
    L.create(L.ledger_path(repo_root, run_id), lg)
    return wt


# ---------------------------------------------------------------- B6


def test_b6_repo_after_subcommand_cleanup(repo, tmp_path):
    wt = _make_reclaimable(repo.root, "run-b6", repo.root.parent / "fake-wts")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = _run_bl(["cleanup", "--run", "run-b6", "--repo", str(repo.root)], elsewhere)

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert "unrecognized arguments" not in proc.stderr, proc.stderr
    out = json.loads(proc.stdout)
    entry = out["cleaned"][0]
    assert entry["run_id"] == "run-b6", entry
    assert str(wt) in entry["removed"], entry
    assert not wt.exists()


def test_b6_boundary_only_named_run_reclaimed(repo, tmp_path):
    wt_one = _make_reclaimable(repo.root, "run-one", repo.root.parent / "fake-wts")
    wt_two = _make_reclaimable(repo.root, "run-two", repo.root.parent / "fake-wts")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = _run_bl(["cleanup", "--run", "run-one", "--repo", str(repo.root)], elsewhere)

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert not wt_one.exists()
    assert wt_two.exists()
    assert L.ledger_path(repo.root, "run-two").is_file()


def test_b6_boundary_missing_run_reports_run_not_found(repo, tmp_path):
    _make_reclaimable(repo.root, "run-present", repo.root.parent / "fake-wts")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = _run_bl(["cleanup", "--run", "no-such-run", "--repo", str(repo.root)], elsewhere)

    assert "unrecognized arguments" not in proc.stderr, proc.stderr
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout)
    assert payload["code"] == "RUN_NOT_FOUND", payload


def test_guard_top_level_repo_still_works(repo, tmp_path):
    """守卫（不列入 proof_spec）：`bl --repo <根> cleanup --run <id>`（--repo 在顶层）仍可执行。"""
    wt = _make_reclaimable(repo.root, "run-top", repo.root.parent / "fake-wts")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = _run_bl(["--repo", str(repo.root), "cleanup", "--run", "run-top"], elsewhere)

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert not wt.exists()


def test_guard_cleanup_rejects_session_not_reclaim_all(repo, tmp_path):
    """守卫（不列入 proof_spec）：cleanup 只认 --run。

    `--session` 必须在解析期被拒绝：接受却不消费的定位参数会让 dispatch 读到空的 --run，
    退化成「回收本仓全部可回收 run」（reviewer 指出过的破坏性缺口）。
    """
    wt = _make_reclaimable(repo.root, "run-session", repo.root.parent / "fake-wts")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    proc = _run_bl(["--repo", str(repo.root), "cleanup", "--session", "S-anything"], elsewhere)

    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "unrecognized arguments" in proc.stderr, proc.stderr
    assert wt.exists()
    assert L.ledger_path(repo.root, "run-session").is_file()


# ---------------------------------------------------------------- B7


def test_b7_repo_selects_other_repo(repo, tmp_path):
    repo_b = tmp_path / "repo-b"
    _init_repo(repo_b)
    wt_a = _make_reclaimable(repo.root, "run-a", repo.root.parent / "fake-wts")
    wt_b = _make_reclaimable(repo_b, "run-b", repo_b.parent / "fake-wts")

    # 在仓 A 的目录下，用 --repo 指向仓 B，回收仓 B 的 run
    proc = _run_bl(["cleanup", "--run", "run-b", "--repo", str(repo_b)], repo.root)

    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    out = json.loads(proc.stdout)
    assert out["cleaned"][0]["run_id"] == "run-b", out
    assert not wt_b.exists()
    assert wt_a.exists()
    assert L.ledger_path(repo.root, "run-a").is_file()


def test_b7_boundary_nonexistent_repo_errors_not_current(repo, tmp_path):
    wt_a = _make_reclaimable(repo.root, "run-a", repo.root.parent / "fake-wts")
    missing = tmp_path / "no-such-repo"

    # 在仓 A 的目录下，--repo 指向不存在的路径：必须报错，而不是静默回收当前仓的 run
    proc = _run_bl(["cleanup", "--run", "run-a", "--repo", str(missing)], repo.root)

    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "unrecognized arguments" not in proc.stderr, proc.stderr
    assert wt_a.exists()
    assert L.ledger_path(repo.root, "run-a").is_file()