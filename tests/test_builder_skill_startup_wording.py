"""B4：skills/builder/SKILL.md 的「## 1. 启动」节写明主仓根开工的主路径，开头纪律段不再提
「只放行切入候选那一处」。

锚句（contract）：若当前已在某个 worktree 中，先 `ExitWorktree --action keep` 回到主仓根目录，
再执行 `bl start`。断言前按项目纪律做归一化：去掉 markdown 强调标记与反引号、连续空白折成一个空格，
原文与锚句同一处理后再做子串匹配。

起点 SKILL 没有这句锚句、没有 START_IN_LINKED_WORKTREE，纪律段还写着「只放行切入候选那一处」，
所以这些断言在起点上 call 阶段失败（baseline-red）。
"""

from __future__ import annotations

import re

from conftest import ROOT

SKILL = ROOT / "skills" / "builder" / "SKILL.md"

ANCHOR = "若当前已在某个 worktree 中，先 `ExitWorktree --action keep` 回到主仓根目录，再执行 `bl start`"
OLD_DISCIPLINE = "只放行切入候选那一处"


def _normalize(text: str) -> str:
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _section_one() -> str:
    text = _text()
    parts = re.split(r"(?m)^(?=## )", text)
    for p in parts:
        m = re.match(r"## 1\.", p)
        if m:
            return p
    raise AssertionError("找不到 SKILL.md 的 ## 1. 启动 一节")


def _discipline() -> str:
    """开头纪律段：`# Builder` 之后、`## 1.` 之前的部分。"""
    text = _text()
    idx = text.index("\n## 1.")
    return text[:idx]


# ---------------------------------------------------------------- B4（baseline-red）


def test_b4_start_section_has_worktree_exit_anchor():
    """启动节含字面锚句：先 ExitWorktree --action keep 回主仓根，再 bl start。"""
    section = _normalize(_section_one())
    assert _normalize(ANCHOR) in section, section


def test_b4_start_section_mentions_start_in_linked_worktree():
    """启动节提及错误码 START_IN_LINKED_WORKTREE（从 worktree 里开工会被硬拒）。"""
    section = _section_one()
    assert "START_IN_LINKED_WORKTREE" in section, section


def test_b4_discipline_paragraph_drops_candidate_whitelist_phrase():
    """开头纪律段不再包含「只放行切入候选那一处」——EnterWorktree 已一律 deny。"""
    assert _normalize(OLD_DISCIPLINE) not in _normalize(_discipline()), _discipline()


# ---------------------------------------------------------------- 不变量（守卫，不列入 proof_spec）


def test_guard_start_section_keeps_existing_error_codes():
    """不变量：启动节其余既有错误码说明保留。"""
    section = _section_one()
    for code in ("TARGET_NOT_IN_SYNC", "HOOKS_OUTDATED", "MACHINE_STAGE_MISSING", "WORKTREE_LINK_SOURCE_MISSING"):
        assert code in section, (code, section)


def test_guard_skill_heading_order_unchanged():
    """不变量：四个编号节仍在且顺序不变。"""
    text = _text()
    pos = [text.index(f"\n## {n}.") for n in "1234"]
    assert pos == sorted(pos)