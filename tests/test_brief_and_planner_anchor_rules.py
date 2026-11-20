"""B6：skills/planner/SKILL.md 的「文本类任务」段逐字包含 CLI 输出作为字面锚句时冻结具体观察面的
规则。B7：tester 的 brief 文本（build / render 提供，首次注入与续接重取同一渲染）逐字包含「只有
反引号包住的文字是字面锚句……」这条规则。B8：tester / reviewer 的 brief 文本都逐字包含长期合入
测试不得依赖本次 run 的 SHA / 当前日期 / CHANGELOG 顶部顺序的纪律。

起点上这三句都不在对应产物里（planner 段与两个 brief 都缺），所以正向断言在起点 call 阶段失败
（baseline-red）。既有锚句约定与归一化规则作为守卫。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import brief, ledger as L

ROOT = Path(__file__).resolve().parents[1]
PLANNER_SKILL = ROOT / "skills" / "planner" / "SKILL.md"

B6_ANCHOR = ("CLI 输出作为字面锚句时，必须冻结具体观察面：顶层 --help、某个子命令 --help、"
             "运行命令的完整输出，或源码文件中的字符串。")
B7_ANCHOR = "只有反引号包住的文字是字面锚句；「」和“”里的词是术语引用，不按子串断言。"
B8_ANCHOR = "长期合入的测试不得依赖本次 run 的 SHA、当前日期或 CHANGELOG 顶部顺序，除非 contract 明确冻结这些时点事实。"


def _normalize(text: str) -> str:
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _section(text: str, marker: str) -> str:
    """从 marker 到下一个 `## ` 标题之间的那一段（与既有章节锚句测试同一取法）。"""
    idx = text.index(marker)
    tail = text[idx:]
    end = tail.find("\n## ", len(marker))
    return tail if end == -1 else tail[:end]


# ---------------------------------------------------------------- B6


def test_b6_planner_text_task_section_freezes_observation_surface():
    """B6 锚句落在「文本类任务」那一段（从该处到下一个 `## ` 标题为止）。"""
    section = _section(PLANNER_SKILL.read_text(encoding="utf-8"), "文本类任务")
    assert _normalize(B6_ANCHOR) in _normalize(section), section[:2000]


# ---------------------------------------------------------------- B7


def test_b7_tester_brief_contains_backtick_anchor_rule(started, cli):
    text = cli("brief", "--session", "S1", "--role", "tester")
    assert _normalize(B7_ANCHOR) in _normalize(text)


def test_b7_rule_comes_from_brief_build_not_only_agent_file(started):
    """边界：规则由 brief.build() / render() 提供，首次注入与续接重取一致。"""
    lg = L.load(started["ledger"])
    built = brief.build(lg, started["repo"].root, "tester")
    rendered = brief.render(built)
    assert _normalize(B7_ANCHOR) in _normalize(rendered)


# ---------------------------------------------------------------- B8


def test_b8_tester_brief_contains_long_lived_test_discipline(started, cli):
    text = cli("brief", "--session", "S1", "--role", "tester")
    assert _normalize(B8_ANCHOR) in _normalize(text)


def test_b8_reviewer_brief_contains_long_lived_test_discipline(started, cli):
    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert _normalize(B8_ANCHOR) in _normalize(text)


def test_b8_both_briefs_share_the_same_sentence(started, cli):
    t = _normalize(cli("brief", "--session", "S1", "--role", "tester"))
    r = _normalize(cli("brief", "--session", "S1", "--role", "reviewer"))
    assert _normalize(B8_ANCHOR) in t and _normalize(B8_ANCHOR) in r


# ---------------------------------------------------------------- 守卫（不列入 proof group）


def test_b6_invariant_existing_anchor_rule_kept():
    """不变量：文档文件与章节的既有锚句规则保留（反引号包住锚句、章节内定位）。"""
    section = _section(PLANNER_SKILL.read_text(encoding="utf-8"), "文本类任务")
    assert "字面锚句一律用反引号包住" in _normalize(section)


def test_b7_invariant_tester_md_normalization_rule_kept():
    text = (ROOT / "agents" / "tester.md").read_text(encoding="utf-8")
    assert "先对原文和锚句做同样的归一化（去掉 markdown 强调标记与反引号，连续空白归一为一个空格）" in text


def test_b8_invariant_historical_content_verification_not_banned(started, cli, hook):
    """不变量：不禁止正常历史内容验证，不削弱现役行为回归覆盖（brief 照常给出 behaviors）。"""
    text = cli("brief", "--session", "S1", "--role", "tester")
    assert "Behaviors:" in text and "B1" in text
