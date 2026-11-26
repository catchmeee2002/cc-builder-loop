"""B3 / B4：被隔离的会话可以切入候选 worktree。

B3：主会话（无 agent_id、agent_type 为空）在 run 进行中收到 EnterWorktree，且 tool_input.path 的
realpath 等于候选 worktree 的 realpath 时放行（退出码 0、无 permissionDecision、ledger 不追加事件）。
起点代码对所有 EnterWorktree 一律 deny，所以 B3 的正向断言在起点上 call 阶段失败（baseline-red）。

B4：其它任何 EnterWorktree 仍然 deny，permissionDecisionReason 含字面「禁止 EnterWorktree」。
起点已经这样处理，因此 B4 走 mutation：把候选里的 EnterWorktree 拦截面删掉后它们必须变红。
"""

from __future__ import annotations

import os

from builder_loop import ledger as L


def _events_n(started) -> int:
    return len(L.load(started["ledger"])["events"])


def _enter(started, hook, tool_input: dict, **extra):
    ev = {"session_id": "S1", "tool_name": "EnterWorktree", "tool_input": tool_input, **extra}
    return hook("PreToolUse", ev)


def _assert_allowed(r, started, before: int) -> None:
    assert r["code"] == 0, r
    assert r["json"] is None, r
    assert _events_n(started) == before


def _assert_denied(r) -> None:
    assert r["code"] == 0, r
    assert r["json"]["hookSpecificOutput"]["permissionDecision"] == "deny", r
    assert "禁止 EnterWorktree" in r["json"]["hookSpecificOutput"]["permissionDecisionReason"], r


# ---------------------------------------------------------------- B3：切入候选 worktree 放行（baseline-red）


def test_b3_enterworktree_candidate_path_allowed(started, hook):
    wt = started["worktree"]
    before = _events_n(started)
    _assert_allowed(_enter(started, hook, {"path": str(wt)}), started, before)


def test_b3_enterworktree_candidate_path_trailing_slash_allowed(started, hook):
    wt = started["worktree"]
    before = _events_n(started)
    _assert_allowed(_enter(started, hook, {"path": str(wt) + "/"}), started, before)


def test_b3_enterworktree_dotdot_normalized_allowed(started, hook):
    """边界：经 `..` 归一后 realpath 等于候选 worktree 也放行。"""
    wt = started["worktree"]
    path = os.path.join(str(wt), "..", wt.name)
    before = _events_n(started)
    _assert_allowed(_enter(started, hook, {"path": path}), started, before)


def test_b3_enterworktree_symlink_to_candidate_allowed(started, hook, tmp_path):
    """边界：经符号链接归一后 realpath 等于候选 worktree 也放行。"""
    wt = started["worktree"]
    link = tmp_path / "candidate-link"
    os.symlink(str(wt), str(link))
    before = _events_n(started)
    _assert_allowed(_enter(started, hook, {"path": str(link)}), started, before)


# ---------------------------------------------------------------- B4：其它 EnterWorktree 仍 deny（mutation）


def test_b4_name_only_denied(started, hook):
    _assert_denied(_enter(started, hook, {"name": "x"}))


def test_b4_other_path_denied(started, hook):
    _assert_denied(_enter(started, hook, {"path": "/tmp/other"}))


def test_b4_tester_worktree_path_denied(started, hook):
    _assert_denied(_enter(started, hook, {"path": str(started["tester_worktree"])}))


def test_b4_repo_root_path_denied(started, hook):
    _assert_denied(_enter(started, hook, {"path": str(started["repo"].root)}))


def test_b4_empty_tool_input_denied(started, hook):
    _assert_denied(_enter(started, hook, {}))


def test_b4_candidate_path_with_name_still_denied(started, hook):
    """边界：name 新建一律仍拒，即使 path 指向候选 worktree。"""
    _assert_denied(_enter(started, hook, {"path": str(started["worktree"]), "name": "x"}))


def test_b4_parent_of_candidate_denied(started, hook):
    """边界：path 是候选 worktree 的父目录也拒。"""
    _assert_denied(_enter(started, hook, {"path": str(started["worktree"].parent)}))


def test_b4_child_of_candidate_denied(started, hook):
    """边界：path 是候选 worktree 的子目录也拒。"""
    _assert_denied(_enter(started, hook, {"path": str(started["worktree"] / "src")}))


def test_b4_relative_path_denied(started, hook):
    """边界：相对路径按 realpath 判，不等于候选 worktree 就拒。"""
    _assert_denied(_enter(started, hook, {"path": "some/relative/dir"}))


# ---------------------------------------------------------------- 不变量（守卫，不列入 proof group）


def test_invariant_role_enterworktree_unchanged(started, hook):
    """不变量：角色（agent_type=tester/reviewer）发出的 EnterWorktree 与改动前一致——不被新增的
    主会话拦截分支命中（角色分支在它之前返回）。守卫：把 EnterWorktree 判定挪到角色分支之前就会变红。"""
    before = _events_n(started)
    r = _enter(started, hook, {"path": str(started["worktree"])}, agent_type="tester", agent_id="T1")
    assert r["code"] == 0, r
    assert r["json"] is None, r
    assert _events_n(started) == before