"""B7：文档随行为一起收敛（docs/architecture.md + CHANGELOG.md）。

- architecture.md 的 PreToolUse 表不再描述 EnterWorktree 白名单（切入候选放行）；
- architecture.md 的 retro 信号不再列 `S-role-background`；
- architecture.md 的 start 校验说明含 linked worktree 硬拒（错误码 START_IN_LINKED_WORKTREE）；
- CHANGELOG.md 含本次条目（以同一个错误码为可检索锚点）。

不绑定发版号、日期或 CHANGELOG 顶部顺序（长期合入的测试不依赖某次 run 的时点）。
其余错误码 / 信号（如 `S-role-wake`）仍应在文档里，作为不变量守卫。
"""

from __future__ import annotations

import re

from conftest import ROOT

ARCH = ROOT / "docs" / "architecture.md"
CHANGELOG = ROOT / "CHANGELOG.md"

NEW_CODE = "START_IN_LINKED_WORKTREE"
RETIRED_SIGNAL = "S-role-background"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _pretooluse_enterworktree_rows() -> list[str]:
    return [line for line in _read(ARCH).splitlines()
            if line.startswith("| PreToolUse") and "EnterWorktree" in line]


# ---------------------------------------------------------------- 正向：新校验进入文档


def test_b7_architecture_documents_linked_worktree_start_reject():
    """start 校验含 linked worktree 硬拒（错误码 START_IN_LINKED_WORKTREE）。"""
    assert NEW_CODE in _read(ARCH), "architecture.md 未记录 linked worktree 开工的硬拒"


def test_b7_changelog_has_this_change_entry():
    """CHANGELOG 含本次条目（以本 run 新增的错误码为锚点，不依赖顶部顺序 / 日期）。"""
    assert NEW_CODE in _read(CHANGELOG), "CHANGELOG.md 未记录 linked worktree 开工硬拒"


# ---------------------------------------------------------------- 反向：退役的描述已移除


def test_b7_architecture_no_retired_role_background_signal():
    """retro 信号不再列 S-role-background。"""
    assert RETIRED_SIGNAL not in _read(ARCH), "architecture.md 仍在 retro 信号里列 S-role-background"


def test_b7_architecture_pretooluse_table_drops_enterworktree_whitelist():
    """PreToolUse 表不再描述 EnterWorktree 白名单（放行候选路径）。"""
    rows = _pretooluse_enterworktree_rows()
    assert rows, "architecture.md 的 PreToolUse 表应仍记录 EnterWorktree 的拦截"
    for row in rows:
        norm = _normalize(row)
        assert "只放行" not in norm, norm
        assert "realpath 等于" not in norm, norm


# ---------------------------------------------------------------- 不变量（守卫）


def test_guard_architecture_still_lists_role_wake_signal():
    """不变量：仍产生并记录的其他 retro 信号（S-role-wake）继续在文档里。"""
    assert "S-role-wake" in _read(ARCH)