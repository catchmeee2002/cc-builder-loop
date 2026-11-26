"""B1：finalize 只做 Git 事务，worktree 回收交给 bl cleanup。

场景：四道 gate 全过的 run，调用者的 shell 站在候选 worktree 内，以真实子进程执行
`python -m builder_loop finalize`（cwd=候选 worktree）。

起点（冻结基线）上 finalize 的 `_complete` 会删掉候选与 tester 两个 worktree 及其分支，
所以「目录仍在 / 分支仍在 / cleanup 键是新形状」这些断言在 call 阶段直接失败（baseline-red）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from builder_loop import ledger as L

from conftest import (
    RUNTIME,
    contract_with,
    drive_to_proof_pass,
    git,
    implement_mul,
    reviewer_pass,
    write_plan,
)

MSG = "feat(foo): [cr_id_skip] Add mul"


# ---------------------------------------------------------------- 子进程辅助


def _sub_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [str(RUNTIME)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_finalize(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "builder_loop", "finalize", "--session", "S1", "-m", MSG],
        cwd=str(cwd), env=_sub_env(), capture_output=True, text=True,
    )


def _ready(started, cli, hook) -> None:
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)


def _expected_cleanup(run_id: str, paths: list[Path]) -> dict:
    return {
        "deferred": True,
        "command": f"bl cleanup --run {run_id}",
        "worktrees": [str(p) for p in paths],
    }


# ---------------------------------------------------------------- given / when / then


def test_finalize_from_candidate_cwd_defers_cleanup(started, cli, hook):
    repo = started["repo"]
    _ready(started, cli, hook)
    wt, twt = started["worktree"], started["tester_worktree"]
    assert wt.is_dir() and twt.is_dir()
    lg = L.load(started["ledger"])
    run_id = lg["run_id"]

    proc = _run_finalize(wt)

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True
    assert out["terminal"] == "finalized" and out["next"] == "retro"
    assert out["cleanup"] == _expected_cleanup(run_id, [wt, twt])
    assert git(repo.root, "rev-parse", "HEAD") == out["final_head"]
    # 目标分支写回了 final_head，tree 与候选一致
    assert git(repo.root, "rev-parse", "HEAD^{tree}") == git(wt, "rev-parse", "HEAD^{tree}")
    # 候选与 tester 的目录、分支都还在
    assert wt.is_dir() and twt.is_dir()
    branches = git(repo.root, "branch", "--list", "builder-loop/*")
    assert lg["candidate"]["branch"] in branches
    assert lg["tester"]["branch"] in branches
    # 既有输出键不变
    for key in ("final_head", "target_branch", "unaddressed_findings", "role_background_tasks"):
        assert key in out


def test_finalize_recovery_defers_cleanup(started, cli, hook):
    """CAS 后中断再次 finalize（沿 finalize_intent 恢复）：同样不删 worktree，输出同形。"""
    from builder_loop import finalize as F

    repo = started["repo"]
    _ready(started, cli, hook)
    wt, twt = started["worktree"], started["tester_worktree"]

    original = F._complete

    def boom(*a, **k):
        raise RuntimeError("crash after CAS")

    F._complete = boom
    try:
        try:
            cli("finalize", "--session", "S1", "-m", MSG)
        except RuntimeError:
            pass
    finally:
        F._complete = original

    lg = L.load(started["ledger"])
    assert lg["finalize_intent"] and lg["terminal"] is None
    assert git(repo.root, "rev-parse", "HEAD") == lg["finalize_intent"]["final_head"]
    run_id = lg["run_id"]

    out = cli("finalize", "--session", "S1")

    assert out["terminal"] == "finalized"
    assert out["cleanup"] == _expected_cleanup(run_id, [wt, twt])
    assert wt.is_dir() and twt.is_dir()


def test_finalize_without_tester_lists_only_candidate(repo, cli, hook):
    """run 没有 tester 时，cleanup.worktrees 只含候选。"""
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    repo.commit_all()
    start = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    wt = Path(start["worktree"])
    assert start["tester_worktree"] is None
    run_id = start["run_id"]

    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    reviewer_pass(hook)

    fin = cli("finalize", "--session", "S1", "-m", MSG)

    assert fin["terminal"] == "finalized"
    assert fin["cleanup"] == _expected_cleanup(run_id, [wt])
    assert wt.is_dir()