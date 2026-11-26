"""finalize 的 on-target worktree 枚举：同步与 dirty 保护覆盖主仓 + 所有 linked worktree（#372）。

场景共同起点：run 推进到 ACTION_FINALIZE（drive_to_proof_pass + reviewer_pass）。目标分支是 fixture
主仓的 `main`；linked worktree 用 `git worktree add` 建在仓库同级目录。
"""

import json

from builder_loop import ledger as L
from conftest import drive_to_proof_pass, git, reviewer_pass

MSG = "feat(foo): [cr_id_skip] Add mul"


# ---------------------------------------------------------------- 场景积木


def _target(started: dict) -> str:
    return L.load(started["ledger"])["repo"]["target_branch"]


def _off_target(repo, layout: str) -> None:
    """把主仓挪离目标分支：detached，或切到一个新分支（两种布局都要覆盖）。"""
    if layout == "detached":
        git(repo.root, "checkout", "--detach")
    else:
        git(repo.root, "checkout", "-b", "elsewhere")


def _add_linked(repo, target: str, name: str = "linked-target"):
    linked = (repo.root.parent / name).resolve()
    git(repo.root, "worktree", "add", str(linked), target)
    return linked


def _ready(started, cli, hook) -> tuple:
    repo = started["repo"]
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    return repo, _target(started)


def _assert_synced(linked, final_head: str, cand_tree: str) -> None:
    assert git(linked, "rev-parse", "HEAD") == final_head
    assert git(linked, "diff", "--cached") == ""
    assert git(linked, "diff") == ""
    assert git(linked, "status", "--porcelain") == ""
    assert git(linked, "rev-parse", "HEAD^{tree}") == cand_tree


# ---------------------------------------------------------------- B1


def test_linked_worktree_synced_when_main_detached(started, cli, hook):
    repo, target = _ready(started, cli, hook)
    cand_tree = git(started["worktree"], "rev-parse", "HEAD^{tree}")
    _off_target(repo, "detached")
    linked = _add_linked(repo, target)

    out = cli("finalize", "--session", "S1", "-m", MSG)

    assert out["terminal"] == "finalized"
    _assert_synced(linked, out["final_head"], cand_tree)
    assert git(repo.root, "rev-parse", target) == out["final_head"]
    # finalize 只做 Git 事务：候选 + tester worktree 与分支留给 bl cleanup
    assert started["worktree"].is_dir() and started["tester_worktree"].is_dir()


def test_linked_worktree_synced_when_main_on_other_branch(started, cli, hook):
    repo, target = _ready(started, cli, hook)
    cand_tree = git(started["worktree"], "rev-parse", "HEAD^{tree}")
    _off_target(repo, "other")
    linked = _add_linked(repo, target)

    out = cli("finalize", "--session", "S1", "-m", MSG)

    assert out["terminal"] == "finalized"
    _assert_synced(linked, out["final_head"], cand_tree)
    assert git(repo.root, "rev-parse", target) == out["final_head"]
    assert started["worktree"].is_dir() and started["tester_worktree"].is_dir()


# ---------------------------------------------------------------- B2


def test_main_checkout_on_target_synced(started, cli, hook):
    repo, target = _ready(started, cli, hook)
    before = git(repo.root, "rev-parse", "HEAD")
    cand_tree = git(started["worktree"], "rev-parse", "HEAD^{tree}")

    out = cli("finalize", "--session", "S1", "-m", MSG)

    # 返回结构不变
    assert {"final_head", "cleanup", "terminal"} <= set(out)
    assert out["terminal"] == "finalized"
    assert git(repo.root, "rev-parse", "HEAD") == out["final_head"] != before
    assert git(repo.root, "rev-parse", "HEAD^{tree}") == cand_tree
    assert git(repo.root, "status", "--porcelain") == ""


# ---------------------------------------------------------------- B3


def test_target_unchecked_anywhere_no_sync(started, cli, hook):
    repo, target = _ready(started, cli, hook)
    cand_tree = git(started["worktree"], "rev-parse", "HEAD^{tree}")
    _off_target(repo, "detached")
    other = (repo.root.parent / "linked-other").resolve()
    git(repo.root, "worktree", "add", "-b", "linked-other", str(other), "HEAD")
    main_before = (repo.root / "src" / "foo.py").read_text()
    other_before = (other / "src" / "foo.py").read_text()

    out = cli("finalize", "--session", "S1", "-m", MSG)

    assert {"final_head", "cleanup", "terminal"} <= set(out)
    assert out["terminal"] == "finalized"
    assert git(repo.root, "rev-parse", target) == out["final_head"]
    assert git(repo.root, "rev-parse", f"{target}^{{tree}}") == cand_tree
    # 目标分支没被任何 worktree checkout：两边都不做同步动作，也不报 read-tree 错误
    assert git(repo.root, "status", "--porcelain") == ""
    assert git(other, "status", "--porcelain") == ""
    assert (repo.root / "src" / "foo.py").read_text() == main_before
    assert (other / "src" / "foo.py").read_text() == other_before
    assert "def mul" not in (repo.root / "src" / "foo.py").read_text()


# ---------------------------------------------------------------- B4


def _linked_ready(started, cli, hook) -> tuple:
    repo, target = _ready(started, cli, hook)
    _off_target(repo, "detached")
    linked = _add_linked(repo, target)
    return repo, target, linked


def _assert_rejected_over_linked(started, repo, target, linked, err) -> None:
    assert err["code"] == "DIRTY_OVERLAP"
    blob = json.dumps(err, ensure_ascii=False)
    assert str(linked) in blob, blob
    assert "src/foo.py" in blob, blob
    # update-ref CAS 之前就拒绝：ref 不动、无 intent 残留
    assert git(repo.root, "rev-parse", target) == started["target_start_head"]
    assert L.load(started["ledger"]).get("finalize_intent") is None


def test_linked_worktree_dirty_overlap_unstaged(started, cli, hook):
    repo, target, linked = _linked_ready(started, cli, hook)
    dirty = linked / "src" / "foo.py"
    dirty.write_text("# local edit\n")

    err = cli("finalize", "--session", "S1", "-m", MSG, expect=1)

    _assert_rejected_over_linked(started, repo, target, linked, err)
    assert dirty.read_text() == "# local edit\n"  # 用户 dirty 内容不被修改


def test_linked_worktree_dirty_overlap_staged(started, cli, hook):
    repo, target, linked = _linked_ready(started, cli, hook)
    dirty = linked / "src" / "foo.py"
    dirty.write_text("# staged edit\n")
    git(linked, "add", "src/foo.py")  # 已暂存未提交

    err = cli("finalize", "--session", "S1", "-m", MSG, expect=1)

    _assert_rejected_over_linked(started, repo, target, linked, err)
    assert dirty.read_text() == "# staged edit\n"


# ---------------------------------------------------------------- B5


def test_dirty_overlap_reports_main_worktree_path(started, cli, hook):
    repo, target = _ready(started, cli, hook)
    dirty = repo.root / "src" / "foo.py"
    dirty.write_text("# local edit\n")

    err = cli("finalize", "--session", "S1", "-m", MSG, expect=1)

    assert err["code"] == "DIRTY_OVERLAP"
    blob = json.dumps(err, ensure_ascii=False)
    assert str(repo.root) in blob or str(repo.root.resolve()) in blob, blob
    assert "src/foo.py" in blob, blob
    # 拒绝时机不变：CAS 之前
    assert git(repo.root, "rev-parse", target) == started["target_start_head"]
    assert L.load(started["ledger"]).get("finalize_intent") is None
    assert dirty.read_text() == "# local edit\n"


# ---------------------------------------------------------------- B6


def test_recover_syncs_linked_worktree(started, cli, hook):
    from builder_loop import finalize as F
    from builder_loop import gitx

    repo, target = _ready(started, cli, hook)
    cand_tree = git(started["worktree"], "rev-parse", "HEAD^{tree}")
    _off_target(repo, "detached")
    linked = _add_linked(repo, target)

    # 模拟 CAS 前进程中断：finalize_intent 已落盘，ref 仍停在 expected_target_head
    original_cas = gitx.update_ref_cas

    def boom(*a, **k):
        raise RuntimeError("crash before CAS")

    gitx.update_ref_cas = boom
    try:
        try:
            cli("finalize", "--session", "S1", "-m", MSG)
        except RuntimeError:
            pass
    finally:
        gitx.update_ref_cas = original_cas

    lg = L.load(started["ledger"])
    intent = lg["finalize_intent"]
    assert intent and lg["terminal"] is None
    assert git(repo.root, "rev-parse", target) == intent["expected_target_head"]

    # recover 必须复用 _sync_checkout（同一套同步逻辑）
    calls = []
    original_sync = F._sync_checkout

    def spy(*a, **k):
        calls.append((a, k))
        return original_sync(*a, **k)

    F._sync_checkout = spy
    try:
        out = cli("finalize", "--session", "S1")
    finally:
        F._sync_checkout = original_sync

    assert calls, "recover 应复用 _sync_checkout 完成同步"
    assert out["terminal"] == "finalized"
    assert git(repo.root, "rev-parse", target) == out["final_head"]
    _assert_synced(linked, out["final_head"], cand_tree)