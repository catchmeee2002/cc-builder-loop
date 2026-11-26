"""B2：finalize 之后 worktree 仍在，`bl cleanup --run <id>` 统一回收。

起点上 finalize 已经删掉两个 worktree，所以「finalize 后目录仍在」「cleanup 的 removed 含
两个 worktree」这些断言在 call 阶段失败（baseline-red）。未复盘跳过、dirty / HEAD 漂移保留
等边界在同组里一并锁定。
"""

from __future__ import annotations

import json

from builder_loop import ledger as L

from conftest import drive_to_proof_pass, git, reviewer_pass

MSG = "feat(foo): [cr_id_skip] Add mul"


def _finalize_retro(started, cli, hook, tmp_path) -> dict:
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
    return out


def _finalize_only(started, cli, hook) -> dict:
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    out = cli("finalize", "--session", "S1", "-m", MSG)
    assert out["terminal"] == "finalized"
    return out


# ---------------------------------------------------------------- given / when / then


def test_cleanup_after_finalize_removes_worktrees_branches_and_is_idempotent(started, cli, hook, tmp_path):
    repo = started["repo"]
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_retro(started, cli, hook, tmp_path)
    assert wt.is_dir() and twt.is_dir()
    run_id = L.load(started["ledger"])["run_id"]

    out = cli("cleanup", "--run", run_id)

    entry = out["cleaned"][0]
    assert entry["run_id"] == run_id
    assert set(entry["removed"]) == {str(wt), str(twt)}
    assert entry["kept"] == []
    assert not wt.exists() and not twt.exists()
    assert not wt.parent.exists()  # 父目录为空时一并移除
    assert git(repo.root, "branch", "--list", "builder-loop/*") == ""

    # 重复执行：目录已不存在也不再报错，不出现在 removed 里
    again = cli("cleanup", "--run", run_id)
    removed = [x for e in again["cleaned"] for x in e.get("removed", [])]
    assert removed == []


def test_cleanup_before_retro_skips_and_keeps_dirs(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_only(started, cli, hook)
    assert wt.is_dir() and twt.is_dir()
    run_id = L.load(started["ledger"])["run_id"]

    out = cli("cleanup", "--run", run_id)

    assert out["cleaned"] == [{"run_id": run_id, "skipped": "尚未复盘"}]
    assert wt.is_dir() and twt.is_dir()


def test_cleanup_keeps_dirty_worktree_and_removes_other(started, cli, hook, tmp_path):
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_retro(started, cli, hook, tmp_path)
    assert wt.is_dir() and twt.is_dir()
    (twt / "scratch.txt").write_text("leftover")
    run_id = L.load(started["ledger"])["run_id"]

    out = cli("cleanup", "--run", run_id)

    entry = out["cleaned"][0]
    assert entry["removed"] == [str(wt)]
    assert entry["kept"] == [{"worktree": str(twt), "reason": "有未提交改动"}]
    assert not wt.exists() and twt.exists()


def test_cleanup_keeps_head_mismatched_worktree(started, cli, hook, tmp_path):
    wt, twt = started["worktree"], started["tester_worktree"]
    _finalize_retro(started, cli, hook, tmp_path)
    assert twt.is_dir()
    # tester worktree 的 HEAD 与 ledger 记录不一致（有人绕过 checkpoint 提交了）
    git(twt, "commit", "--allow-empty", "-m", "chore(fixture): [cr_id_skip] Drift tester head")
    run_id = L.load(started["ledger"])["run_id"]

    out = cli("cleanup", "--run", run_id)

    entry = out["cleaned"][0]
    assert entry["removed"] == [str(wt)]
    assert entry["kept"] == [{"worktree": str(twt), "reason": "HEAD 与 ledger 不一致"}]
    assert not wt.exists() and twt.exists()