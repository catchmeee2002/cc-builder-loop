"""B2：`bl start` 在 linked worktree 里硬拒（START_IN_LINKED_WORKTREE）。

判据：解析后的 repo_root 是 linked worktree（`gitx.main_repo_root(repo_root) != repo_root`）时，
`run.start` / `bl start` 以错误码 `START_IN_LINKED_WORKTREE` 拒绝，details 带主仓根路径；不建
candidate / tester worktree、不写 ledger、不绑定 session。拒绝发生在建 worktree 与写 ledger 之前，
detached HEAD 的 linked worktree 同样拒绝。

起点代码允许从 linked worktree 开工（并把该 worktree 当作 run 的根），因此本文件里「必须拒绝」
的用例在起点上都在 call 阶段失败（baseline-red）。

「主仓根开工不受影响」「--repo 显式指向主仓根时即使 cwd 在 worktree 中也放行」是 B2 的边界的
守卫，起点上本来就成立，不列入 proof_spec。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import gitx
from builder_loop import ledger as L

from conftest import git

PLAN = "plan.md"


def _dispatch(repo_arg: Path, *args: str, expect: int | None = None):
    from builder_loop.cli import build_parser, dispatch
    from builder_loop.errors import Problem

    ns = build_parser().parse_args(["--repo", str(repo_arg), *args])
    try:
        out, code = dispatch(ns)
    except Problem as exc:
        out, code = exc.to_json(), exc.exit_code
    if expect is not None:
        assert code == expect, f"{args}: exit {code} != {expect}: {out}"
    return out, code


def _linked(repo, name: str = "linked-release", branch: str = "release") -> Path:
    """用户自己创建的 linked worktree（与主仓共用 .git，但 toplevel 是它自己）。"""
    linked = (repo.root.parent / name).resolve()
    git(repo.root, "branch", branch)
    git(repo.root, "worktree", "add", str(linked), branch)
    return linked


def _run_start(linked: Path, plan: Path, session: str):
    """直接走接口，便于断言 Problem 的 code / exit_code / details。"""
    from builder_loop.errors import Problem
    from builder_loop.run import start

    try:
        return start(linked, plan, session), None
    except Problem as exc:
        return None, exc


def _assert_rejected(linked: Path, out, exc, main_root: Path) -> None:
    assert out is None, out
    assert exc is not None
    assert exc.code == "START_IN_LINKED_WORKTREE", exc.to_json()
    # contract B2（revision 2）冻结的是 fatal 拒绝：CLI exit code 2。
    assert exc.exit_code == 2, exc.to_json()
    dump = str(exc.to_json())
    assert str(main_root) in dump, dump
    assert gitx.main_repo_root(linked) == main_root


def _no_side_effects(repo, linked: Path, session: str) -> None:
    assert L.lookup_session(session) is None
    assert L.list_runs(linked) == []
    assert L.list_runs(repo.root) == []
    # linked worktree 开 run 时 worktree 根落在 linked.parent/builder-loop-worktrees/<linked.name>
    assert not (linked.parent / "builder-loop-worktrees" / linked.name).exists()


# ---------------------------------------------------------------- B2：linked worktree 硬拒（baseline-red）


def test_b2_start_from_linked_worktree_rejected(repo):
    linked = _linked(repo)
    main_root = repo.root.resolve()

    out, exc = _run_start(linked, repo.root / PLAN, "S1")

    _assert_rejected(linked, out, exc, main_root)


def test_b2_cli_start_from_linked_worktree_rejected(repo):
    linked = _linked(repo)

    out, code = _dispatch(linked, "start", "--plan", str(repo.root / PLAN), "--session", "S1")

    assert code == 2, (code, out)
    assert out["code"] == "START_IN_LINKED_WORKTREE", out
    assert str(repo.root.resolve()) in str(out.get("details") or {}), out


def test_b2_rejection_happens_before_worktrees_and_ledger(repo):
    """拒绝发生在创建 worktree 与写 ledger 之前：没有 candidate/tester worktree、没有 run 目录、
    session 也没绑定。"""
    linked = _linked(repo)
    before = sorted(p.name for p in linked.parent.iterdir())

    out, exc = _run_start(linked, repo.root / PLAN, "S2")

    _assert_rejected(linked, out, exc, repo.root.resolve())
    _no_side_effects(repo, linked, "S2")
    assert sorted(p.name for p in linked.parent.iterdir()) == before


def test_b2_detached_head_linked_worktree_rejected(repo):
    """边界：linked worktree 处于 detached HEAD 时同样拒绝（检测早于目标分支解析）。"""
    linked = (repo.root.parent / "linked-detached").resolve()
    head = git(repo.root, "rev-parse", "HEAD")
    git(repo.root, "worktree", "add", "--detach", str(linked), head)

    out, exc = _run_start(linked, repo.root / PLAN, "S3")

    _assert_rejected(linked, out, exc, repo.root.resolve())
    _no_side_effects(repo, linked, "S3")


# ---------------------------------------------------------------- 边界 / 不变量（守卫，不列入 proof_spec）


def test_guard_main_repo_root_start_not_rejected(repo):
    """边界：repo_root 就是主仓根（main_repo_root == repo_root）时不因这项检查被拒。"""
    out, code = _dispatch(repo.root, "start", "--plan", str(repo.root / PLAN), "--session", "S4")

    assert code == 0, out
    assert "run_id" in out, out
    assert "START_IN_LINKED_WORKTREE" not in str(out), out
    assert L.lookup_session("S4") is not None


def test_guard_explicit_repo_main_root_wins_over_worktree_cwd(repo, monkeypatch):
    """边界：cwd 在 linked worktree 但 --repo 显式指向主仓根 → 放行（检测基于解析后的 repo_root）。"""
    linked = _linked(repo)
    monkeypatch.chdir(linked)

    out, code = _dispatch(repo.root, "start", "--plan", str(repo.root / PLAN), "--session", "S5")

    assert code == 0, out
    assert "run_id" in out, out
    bound = L.lookup_session("S5")
    assert bound is not None, out  # 绑定到主仓的 run
    assert Path(bound["ledger_path"]).resolve().is_relative_to(repo.root.resolve()), bound