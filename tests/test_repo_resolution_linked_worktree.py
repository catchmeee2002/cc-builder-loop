"""B1：`--repo` 指向用户创建的 linked worktree 时，选仓根保持该 worktree 的 toplevel。

起点（基线）代码把所有路径都 `main_repo_root(toplevel(...))` 回收到主仓，于是 linked worktree
自己的分支、loop.yml 与 run 命名空间全部被主仓顶替。下面这些用例在基线上都在 call 阶段失败
（baseline-red）。

不变量的守卫（不列入 proof_spec）：主仓解析不变、显式 --target 优先级不变、已有 run 从
candidate/tester worktree 用 --run 定位仍回到该 run 的主仓 ledger。
"""

from __future__ import annotations

import json
from pathlib import Path

from builder_loop import ledger as L
from conftest import CONTRACT, git, write_plan

LINKED_PROOF_CMD = "python3 -m pytest -p no:html -p no:cacheprovider"
LINKED_STAGE_TIMEOUT = 11
LINKED_MAX_ITERATIONS = 7


def _dispatch(repo_arg: Path, *args: str, expect: int | None = 0):
    """与 cli fixture 相同，但 --repo 指向任意路径（linked worktree / candidate / tester worktree）。"""
    from builder_loop.cli import build_parser, dispatch
    from builder_loop.errors import Problem

    ns = build_parser().parse_args(["--repo", str(repo_arg), *args])
    try:
        out, code = dispatch(ns)
    except Problem as exc:
        out, code = exc.to_json(), exc.exit_code
    if expect is not None:
        assert code == expect, f"{args}: exit {code} != {expect}: {out}"
    return out


def _linked_worktree(repo) -> Path:
    """用户自己创建的 linked worktree：分支 release，带一份与主仓不同的 loop.yml。"""
    linked = (repo.root.parent / "linked-release").resolve()
    git(repo.root, "branch", "release")
    git(repo.root, "worktree", "add", str(linked), "release")
    (linked / ".claude" / "loop.yml").write_text(
        "pass_cmd:\n"
        "  - stage: linked\n    cmd: \"python3 -c 'import sys; sys.exit(0)'\"\n"
        f"    timeout: {LINKED_STAGE_TIMEOUT}\n"
        f"max_iterations: {LINKED_MAX_ITERATIONS}\n"
        f'proof_runner:\n  framework: pytest\n  cmd: "{LINKED_PROOF_CMD}"\n',
        encoding="utf-8",
    )
    git(linked, "add", "-A")
    git(linked, "commit", "-q", "-m", "chore(fixture): [cr_id_skip] Linked loop config")
    return linked


# ---------------------------------------------------------------- 选仓根


def test_resolve_repo_root_keeps_linked_worktree_toplevel(repo):
    from builder_loop.run import resolve_repo_root

    linked = _linked_worktree(repo)
    assert resolve_repo_root(str(linked)) == linked


def test_start_from_linked_worktree_uses_its_root_branch_and_config(repo):
    linked = _linked_worktree(repo)
    linked_head = git(linked, "rev-parse", "HEAD")

    out = _dispatch(linked, "start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    lg = L.load(Path(out["ledger"]))
    assert lg["repo"]["root"] == str(linked)
    assert lg["repo"]["target_branch"] == "release"
    assert lg["repo"]["target_start_head"] == linked_head
    assert lg["loop_config"]["proof_runner"]["cmd"] == LINKED_PROOF_CMD
    assert lg["loop_config"]["pass_cmd"][0]["timeout"] == LINKED_STAGE_TIMEOUT
    assert lg["loop_config"]["max_iterations"] == LINKED_MAX_ITERATIONS
    assert out["target_branch"] == "release"


def test_run_started_from_linked_worktree_is_located_from_its_own_worktrees(repo):
    """组合用例：从 linked worktree start 出的 run，再从它自己的 candidate / tester worktree
    用 --repo + --run 取 status / brief，仍必须回到该 run 的 ledger（reviewer finding，B1）。"""
    linked = _linked_worktree(repo)
    out = _dispatch(linked, "start", "--plan", str(repo.root / "plan.md"), "--session", "S1")

    lg = L.load(Path(out["ledger"]))
    assert lg["repo"]["root"] == str(linked)  # 起点上就红：run 属于 linked，不属于主仓
    tester_wt = Path(out["tester_worktree"])
    assert tester_wt is not None and tester_wt.is_dir()
    for wt in (Path(out["worktree"]), tester_wt):
        status = _dispatch(wt, "status", "--run", out["run_id"])
        assert status["run_id"] == out["run_id"], status
        assert status["target"]["root"] == str(linked), status["target"]
        brief = _dispatch(wt, "brief", "--run", out["run_id"], "--role", "tester", "--json")
        assert brief["run_id"] == out["run_id"], brief
        assert brief["worktree"] == str(tester_wt.resolve()), brief


def test_runs_lists_runs_under_the_pointed_linked_worktree(repo):
    linked = _linked_worktree(repo)
    L.create(L.ledger_path(linked, "manual-run-1"), L.new_ledger(run_id="manual-run-1"))

    out = _dispatch(linked, "runs")

    assert "manual-run-1" in [r["run_id"] for r in out["runs"]], out


def test_doctor_reports_linked_worktree_root_and_its_loop_config(repo):
    linked = _linked_worktree(repo)

    out = _dispatch(linked, "doctor")

    assert out["repo"]["root"] == str(linked), out.get("repo")
    assert out["repo"]["proof_runner"]["cmd"] == LINKED_PROOF_CMD, out.get("repo")


def test_cleanup_operates_on_the_pointed_linked_worktree(repo):
    linked = _linked_worktree(repo)
    lg = L.new_ledger(run_id="abandoned-linked")
    lg["terminal"] = {"status": "abandoned", "reason": "fixture", "at": L.now_iso(), "final_head": None}
    lg["retrospective"] = {"at": L.now_iso(), "dispositions": [], "observations": []}
    L.create(L.ledger_path(linked, "abandoned-linked"), lg)

    out = _dispatch(linked, "cleanup", "--run", "abandoned-linked")

    assert [c["run_id"] for c in out["cleaned"]] == ["abandoned-linked"], out


def test_contract_validate_check_repo_uses_linked_worktree(repo):
    linked = _linked_worktree(repo)
    plan = write_plan(repo.root, CONTRACT, "plan-linked.md")

    out = _dispatch(linked, "contract", "validate", "--plan", str(plan), "--check-repo")

    assert out["repo"]["target_branch"] == "release", out.get("repo")
    assert out["repo"]["proof_runner"]["cmd"] == LINKED_PROOF_CMD, out.get("repo")


# ---------------------------------------------------------------- 守卫：不变量 / 边界（不列入 proof_spec）


def test_guard_main_repo_resolution_unchanged(repo):
    from builder_loop.run import resolve_repo_root

    assert resolve_repo_root(str(repo.root)) == repo.root.resolve()


def test_guard_explicit_target_still_wins_over_linked_branch(repo):
    linked = _linked_worktree(repo)

    out = _dispatch(linked, "start", "--plan", str(repo.root / "plan.md"), "--session", "S1", "--target", "main")

    lg = L.load(Path(out["ledger"]))
    assert lg["repo"]["target_branch"] == "main"


def test_guard_existing_run_located_from_tester_worktree(started):
    """已有 run 从 tester worktree 用 --repo + --run 定位时，仍回到该 run 的主仓 ledger。"""
    out = _dispatch(started["tester_worktree"], "status", "--run", started["run_id"])
    assert out["run_id"] == started["run_id"]
    brief = _dispatch(started["tester_worktree"], "brief", "--run", started["run_id"], "--role", "tester", "--json")
    assert brief["run_id"] == started["run_id"]
    assert brief["worktree"] == str(started["tester_worktree"].resolve())


def test_guard_existing_run_located_from_candidate_worktree(started):
    out = _dispatch(started["worktree"], "status", "--run", started["run_id"])
    assert out["run_id"] == started["run_id"]


def test_guard_session_location_unchanged(started, cli):
    out = cli("status", "--session", "S1")
    assert out["run_id"] == started["run_id"]
    # 附带确认 ledger 仍可按主仓 ledger 路径加载（不迁移旧 ledger）
    assert json.loads(started["ledger"].read_text(encoding="utf-8"))["run_id"] == started["run_id"]