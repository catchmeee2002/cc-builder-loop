"""B12: 候选 worktree 里一个归 builder 的已跟踪文件被 git rm 删除，checkpoint 成功，
新 candidate.head 的树里不再有该文件。边界：同一次 checkpoint 里既有删除又有新增/修改。
不变量：越界路径仍被 CHECKPOINT_REJECTED 拒绝。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import gitx
from builder_loop import ledger as L
from conftest import git


def test_b12_git_rm_tracked_builder_file_then_checkpoint_removes_it(started, cli):
    wt = started["worktree"]
    git(wt, "rm", "-q", "src/foo.py")
    out = cli("checkpoint", "--session", "S1", "--role", "builder")
    assert not out["rejected"]
    new_head = out["head"]
    tree_paths = gitx.git(wt, "ls-tree", "-r", "--name-only", new_head).stdout.split()
    assert "src/foo.py" not in tree_paths


def test_b12_boundary_delete_and_add_in_same_checkpoint(started, cli):
    wt = started["worktree"]
    git(wt, "rm", "-q", "src/foo.py")
    (wt / "src" / "bar.py").write_text("def bar():\n    return 1\n")
    out = cli("checkpoint", "--session", "S1", "--role", "builder")
    assert not out["rejected"]
    tree_paths = gitx.git(wt, "ls-tree", "-r", "--name-only", out["head"]).stdout.split()
    assert "src/foo.py" not in tree_paths
    assert "src/bar.py" in tree_paths


def test_b12_invariant_out_of_authority_path_still_rejected(started, cli):
    wt = started["worktree"]
    (wt / "outside.txt").write_text("nope\n")
    out = cli("checkpoint", "--session", "S1", "--role", "builder", expect=1)
    assert out["code"] == "CHECKPOINT_REJECTED"
    assert any(r["path"] == "outside.txt" for r in out["details"]["rejected"])
