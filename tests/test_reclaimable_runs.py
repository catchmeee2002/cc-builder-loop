"""B4：`retro.reclaimable_runs(repo_root)` 列出「终态、已复盘、候选或 tester 目录至少一个仍在」
的 run；`bl doctor --repo` 的 repo.reclaimable_runs 与它一致。

reclaimable_runs 是本 run 新增的接口，冻结基线上 import 不到，故用 mutation 证明鉴别力。
ledger 直接构造（不经完整四道 gate），只验证「哪些 run 算待回收」这一投影。
"""

from __future__ import annotations

import json
import shutil

from builder_loop import ledger as L
from builder_loop import retro
from builder_loop.doctor import doctor

from conftest import git


# ---------------------------------------------------------------- 构造 run


def _make_run(repo, run_id: str, *, terminal_status="finalized", retro_done: bool = True,
              with_candidate: bool = False, with_tester: bool = False,
              legacy: bool = False, unreadable: bool = False) -> str:
    lp = L.ledger_path(repo.root, run_id)
    lp.parent.mkdir(parents=True, exist_ok=True)
    if unreadable:
        lp.write_text("{ this is not json", encoding="utf-8")
        return run_id
    if legacy:
        lp.write_text(json.dumps({"schema": "builder-loop/ledger@1", "run_id": run_id}), encoding="utf-8")
        return run_id

    head = git(repo.root, "rev-parse", "HEAD")
    lg = L.new_ledger(run_id=run_id)
    lg["session"] = {"owner_session_id": None}
    lg["repo"] = {"root": str(repo.root), "target_branch": "main", "target_start_head": head}
    cand = {"branch": f"builder-loop/{run_id}/candidate", "worktree": None, "head": None, "checkpoints": []}
    ter = None
    base = repo.root.parent / "fake-wts" / run_id
    if with_candidate:
        p = base / "builder"
        git(repo.root, "worktree", "add", "-b", cand["branch"], str(p), head)
        cand["worktree"], cand["head"] = str(p), head
    if with_tester:
        branch = f"builder-loop/{run_id}/tester"
        p = base / "tester"
        git(repo.root, "worktree", "add", "-b", branch, str(p), head)
        ter = {"branch": branch, "worktree": str(p), "head": head}
    lg["candidate"] = cand
    lg["tester"] = ter
    lg["terminal"] = ({"status": terminal_status, "final_head": None, "reason": None, "at": L.now_iso()}
                      if terminal_status else None)
    lg["retrospective"] = ({"at": L.now_iso(), "dispositions": [], "observations": []} if retro_done else None)
    L.create(lp, lg)
    return run_id


# ---------------------------------------------------------------- given / when / then


def test_reclaimable_lists_terminal_retro_runs_with_dirs_sorted(repo):
    _make_run(repo, "run-b", with_candidate=True)
    _make_run(repo, "run-a", with_candidate=True, with_tester=True)
    _make_run(repo, "run-none", with_candidate=False)  # 已复盘但目录已被回收

    assert retro.reclaimable_runs(repo.root) == ["run-a", "run-b"]


def test_reclaimable_excludes_unretro_terminal(repo):
    _make_run(repo, "run-a", with_candidate=True)
    _make_run(repo, "run-c", retro_done=False, with_candidate=True)

    assert retro.reclaimable_runs(repo.root) == ["run-a"]


def test_reclaimable_excludes_active_run(repo):
    _make_run(repo, "run-a", with_candidate=True)
    _make_run(repo, "run-d", terminal_status=None, retro_done=False, with_candidate=True)

    assert retro.reclaimable_runs(repo.root) == ["run-a"]


def test_reclaimable_skips_legacy_and_unreadable(repo):
    _make_run(repo, "run-a", with_candidate=True)
    _make_run(repo, "run-legacy", legacy=True)
    _make_run(repo, "run-bad", unreadable=True)

    assert retro.reclaimable_runs(repo.root) == ["run-a"]


def test_reclaimable_run_disappears_after_cleanup(repo, cli):
    _make_run(repo, "run-a", with_candidate=True, with_tester=True)
    assert retro.reclaimable_runs(repo.root) == ["run-a"]

    out = cli("cleanup", "--run", "run-a")
    assert out["cleaned"][0]["kept"] == []

    assert retro.reclaimable_runs(repo.root) == []


def test_doctor_reports_same_reclaimable_runs(repo, cli):
    _make_run(repo, "run-a", with_candidate=True)
    _make_run(repo, "run-none", with_candidate=False)

    rep = cli("doctor")
    assert rep["repo"]["reclaimable_runs"] == ["run-a"]
    assert rep["repo"]["reclaimable_runs"] == retro.reclaimable_runs(repo.root)


# ---------------------------------------------------------------- 不变量（守卫）


def test_boundary_empty_when_no_runs_dir(repo):
    """边界：没有 runs 目录时返回空列表。

    这是守卫而非 proof id：空输入下无论 `alive` 过滤怎么写结果都是 []，反相 mutation 改的是
    「目录是否存在」这一分支，对本用例恒为绿；它锁的是 `ledger.list_runs` 的「runs 目录不存在
    直接返回 []」早退，由 machine 全量兜住。"""
    assert not (repo.root / ".claude" / "builder-loop" / "runs").exists()
    assert retro.reclaimable_runs(repo.root) == []


def test_invariant_doctor_without_repo_has_no_repo_key(repo):
    assert "repo" not in doctor(None)


def test_invariant_doctor_problems_unaffected_by_reclaimable_runs(repo):
    _make_run(repo, "run-a", with_candidate=True, with_tester=True)
    first = doctor(str(repo.root))
    assert first["repo"]["reclaimable_runs"] == ["run-a"]
    shutil.rmtree(repo.root.parent / "fake-wts" / "run-a", ignore_errors=True)
    second = doctor(str(repo.root))
    assert second["repo"]["reclaimable_runs"] == []
    assert second["problems"] == first["problems"]
    assert second["healthy"] == first["healthy"]