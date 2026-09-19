"""contract assurance.machine_stages：声明所需 machine stage，冻结时对照 loop.yml 校验。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from builder_loop import ledger as L
from conftest import contract_with, git, write_plan

# 改动前 CONTRACT（无 machine_stages）经 `bl contract validate` 得到的 digests
GOLDEN_DIGESTS = {
    "mission": "70bce81f222f00761fa8ce86955801b3d7f83b4395fbaa7f7d9d65a9c70d61c5",
    "authority": "bbf369e2f0fb74a5d262292bcff814db875b052440ea5844f2e12166e984a8a3",
    "assurance": "3f2336c4f221e149b791dc39b4ed4db025be44c6d8aa9154e4fc2dfe7103642d",
}


def _plan(repo, stages, name="stages.md"):
    return write_plan(repo.root, contract_with(**{"assurance.machine_stages": stages}), name)


def _snapshot(repo):
    runs = repo.root / ".claude" / "builder-loop" / "runs"
    ledgers = sorted(str(p) for p in runs.rglob("*") if p.is_file()) if runs.exists() else []
    return {
        "ledgers": ledgers,
        "worktrees": git(repo.root, "worktree", "list", "--porcelain"),
        "branches": git(repo.root, "branch", "--list", "--format=%(refname:short)"),
    }


def test_b3_start_missing_stage_fatal_and_creates_nothing(repo, cli):
    plan = _plan(repo, ["test", "lint"])
    before = _snapshot(repo)
    res = cli("start", "--plan", str(plan), "--session", "S1", expect=2)
    assert res["code"] == "MACHINE_STAGE_MISSING", res
    assert res["details"]["missing"] == ["lint"]
    assert res["details"]["available"] == ["test"]
    assert _snapshot(repo) == before
    # session 未绑定：同一 session 对合法 plan 能 start
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    assert out["run_id"]


def test_b3_all_stages_present_start_ok_and_recorded(repo, cli):
    plan = _plan(repo, ["test"])
    out = cli("start", "--plan", str(plan), "--session", "S1")
    lg = L.load(Path(out["ledger"]))
    assert lg["contract"]["assurance"]["machine_stages"] == ["test"]


def test_b3_no_machine_stages_start_unchanged(repo, cli):
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    lg = L.load(Path(out["ledger"]))
    assert lg["contract"]["assurance"].get("machine_stages") in (None, [])


def test_b3_validate_check_repo_reports_missing_stage(repo, cli):
    plan = _plan(repo, ["test", "lint"])
    res = cli("contract", "validate", "--plan", str(plan), "--check-repo", expect=1)
    assert res["code"] == "CONTRACT_REPO_CHECK_FAILED", res
    assert any("lint" in p for p in res["details"]["problems"])
    ok = cli("contract", "validate", "--plan", str(plan))
    assert ok["valid"] is True


def test_b3_revise_authorize_with_unknown_stage_rejected(repo, cli):
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    lp = Path(out["ledger"])
    before = L.load(lp)["contract"]
    plan2 = _plan(repo, ["test", "ghost"], "plan2.md")
    res = cli("contract", "revise", "--session", "S1", "--plan", str(plan2), "--authorize", expect=2)
    assert res["code"] == "MACHINE_STAGE_MISSING", res
    assert L.load(lp)["contract"] == before


@pytest.mark.parametrize("bad", ["test", ["test", ""], ["test", "test"], [""], [1]])
def test_b3_invalid_machine_stages_rejected(repo, cli, bad):
    plan = _plan(repo, bad)
    res = cli("contract", "validate", "--plan", str(plan), expect=2)
    assert res["code"] == "CONTRACT_INVALID", res
    res = cli("start", "--plan", str(plan), "--session", "S1", expect=2)
    assert res["code"] == "CONTRACT_INVALID", res


def test_b3_digests_unchanged_without_machine_stages(repo, cli):
    out = cli("contract", "validate", "--plan", str(repo.root / "plan.md"))
    assert out["digests"] == GOLDEN_DIGESTS
