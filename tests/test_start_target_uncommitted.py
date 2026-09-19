"""bl start 报告主仓里不进基线的未提交 tracked 改动（只报告，不拦截，不落盘，不动主仓）。"""

from __future__ import annotations

import json
from pathlib import Path

from builder_loop import ledger as L
from conftest import contract_with, git, write_plan

LEGACY_KEYS = {"run_id", "ledger", "worktree", "tester_worktree", "candidate_branch", "target_branch", "target_start_head", "contract_digests", "readiness"}


def _dirty(repo):
    r = repo.root
    (r / "src" / "foo.py").write_text("def add(a, b):\n    return a + b  # dirty\n")
    (r / "tests" / "test_foo.py").write_text("# staged change\n")
    git(r, "add", "tests/test_foo.py")
    (r / "notes.txt").write_text("untracked\n")


def test_b4_reports_tracked_changes_sorted_not_untracked(repo, cli):
    clean = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S0")
    base_blockers = clean["readiness"]["blockers"]
    write_plan(repo.root, contract_with(**{"mission.slug": "other"}), "plan_other.md")
    _dirty(repo)
    contents = {p: (repo.root / p).read_text() for p in ("src/foo.py", "tests/test_foo.py", "notes.txt")}
    out = cli("start", "--plan", str(repo.root / "plan_other.md"), "--session", "S1")
    assert out["target_uncommitted"] == ["src/foo.py", "tests/test_foo.py"]
    assert out["readiness"]["blockers"] == base_blockers
    wt = Path(out["worktree"])
    for p in ("src/foo.py", "tests/test_foo.py"):
        assert (wt / p).read_text().rstrip("\n") == git(repo.root, "show", f"HEAD:{p}").rstrip("\n")
    for p, c in contents.items():
        assert (repo.root / p).read_text() == c
    assert LEGACY_KEYS <= set(out)
    lg = L.load(Path(out["ledger"]))
    assert "target_uncommitted" not in json.dumps(lg)


def test_b4_clean_repo_empty(repo, cli):
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    assert out["target_uncommitted"] == []
    assert LEGACY_KEYS <= set(out)


def test_b4_only_untracked_empty(repo, cli):
    (repo.root / "scratch.txt").write_text("x\n")
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    assert out["target_uncommitted"] == []


def test_b4_current_branch_not_target_empty(repo, cli):
    git(repo.root, "branch", "dev")
    write_plan(repo.root, contract_with(**{"authority.target_branch": "dev"}), "plan_dev.md")
    (repo.root / "src" / "foo.py").write_text("def add(a, b):\n    return a + b  # dirty\n")
    assert git(repo.root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    out = cli("start", "--plan", str(repo.root / "plan_dev.md"), "--session", "S1")
    assert out["target_branch"] == "dev"
    assert out["target_uncommitted"] == []
