"""B1：EnterWorktree 一律 deny——run 进行中连切入候选 worktree 也不再放行。

起点代码对 `tool_input.path` 的 realpath 等于候选 worktree 的那一次放行（#237 走法 B），所以
「候选路径必须 deny」的断言在起点上都在 call 阶段失败（baseline-red）。

`name` 新建、其它路径、tester / 主仓路径、终态不拦截、角色发出的 EnterWorktree 在起点上本来
就 deny / 不拦截，属于 B1 的边界与不变量；它们作为守卫用例保留在本文件里，但不列入 proof_spec
（由 machine 全量运行）。
"""

from __future__ import annotations

import os

from builder_loop import ledger as L


def _events_n(started) -> int:
    return len(L.load(started["ledger"])["events"])


def _enter(started, hook, tool_input: dict, **extra):
    ev = {"session_id": "S1", "tool_name": "EnterWorktree", "tool_input": tool_input, **extra}
    return hook("PreToolUse", ev)


def _assert_denied(r) -> str:
    assert r["code"] == 0, r
    out = r["json"]
    assert out is not None, r
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", r
    assert "禁止 EnterWorktree" in hso["permissionDecisionReason"], r
    return hso["permissionDecisionReason"]


# ---------------------------------------------------------------- B1：候选路径一律 deny（baseline-red）


def test_b1_candidate_path_denied(started, hook):
    """given run 进行中、候选已建 / when 主会话 EnterWorktree(path=候选) / then deny，且文案仍说明
    worktree 由 runtime 管理；不追加 ledger 事件（deny 不改账）。"""
    before = _events_n(started)
    reason = _assert_denied(_enter(started, hook, {"path": str(started["worktree"])}))
    assert "worktree 由 runtime 管理" in reason, reason
    assert _events_n(started) == before


def test_b1_candidate_path_trailing_slash_denied(started, hook):
    """边界：候选路径带尾斜杠同样 deny（判定不能只靠字符串相等而漏掉归一化）。"""
    _assert_denied(_enter(started, hook, {"path": str(started["worktree"]) + "/"}))


def test_b1_candidate_path_dotdot_normalized_denied(started, hook):
    """边界：经 `..` 归一后就是候选 worktree 的路径同样 deny。"""
    wt = started["worktree"]
    _assert_denied(_enter(started, hook, {"path": os.path.join(str(wt), "..", wt.name)}))


def test_b1_symlink_to_candidate_denied(started, hook, tmp_path):
    """边界：指向候选 worktree 的符号链接同样 deny。"""
    link = tmp_path / "candidate-link"
    os.symlink(str(started["worktree"]), str(link))
    _assert_denied(_enter(started, hook, {"path": str(link)}))


# ---------------------------------------------------------------- 边界 / 不变量（守卫，不列入 proof_spec）


def test_guard_name_only_denied(started, hook):
    """边界：`name` 新建 worktree 仍 deny。"""
    _assert_denied(_enter(started, hook, {"name": "x"}))


def test_guard_candidate_path_with_name_denied(started, hook):
    """边界：path 指向候选但带 name（新建）同样 deny。"""
    _assert_denied(_enter(started, hook, {"path": str(started["worktree"]), "name": "x"}))


def test_guard_other_path_denied(started, hook):
    """边界：非候选路径仍 deny。"""
    _assert_denied(_enter(started, hook, {"path": "/tmp/other"}))


def test_guard_tester_worktree_path_denied(started, hook):
    _assert_denied(_enter(started, hook, {"path": str(started["tester_worktree"])}))


def test_guard_repo_root_path_denied(started, hook):
    _assert_denied(_enter(started, hook, {"path": str(started["repo"].root)}))


def test_guard_empty_tool_input_denied(started, hook):
    _assert_denied(_enter(started, hook, {}))


def test_guard_parent_of_candidate_denied(started, hook):
    _assert_denied(_enter(started, hook, {"path": str(started["worktree"].parent)}))


def test_guard_child_of_candidate_denied(started, hook):
    _assert_denied(_enter(started, hook, {"path": str(started["worktree"] / "src")}))


def test_guard_relative_path_denied(started, hook):
    """边界：相对路径按 realpath 判，不等于候选 worktree 就 deny。"""
    _assert_denied(_enter(started, hook, {"path": "some/relative/dir"}))


def test_guard_terminal_run_enterworktree_not_intercepted(started, cli, hook):
    """边界：run 已终态（复盘尚未记录）时不再拦截 EnterWorktree——终态分支保持静默。"""
    cli("abandon", "--session", "S1", "--reason", "终态不拦截 EnterWorktree")
    r = _enter(started, hook, {"path": str(started["worktree"])})
    assert r["code"] == 0, r
    assert r["json"] is None, r


def test_guard_role_enterworktree_unchanged(started, hook):
    """不变量：角色（agent_type=tester）发出的 EnterWorktree 走角色分支，不被主会话的 deny 命中。"""
    before = _events_n(started)
    r = _enter(started, hook, {"path": str(started["worktree"])}, agent_type="tester", agent_id="T1")
    assert r["code"] == 0, r
    assert r["json"] is None, r
    assert _events_n(started) == before