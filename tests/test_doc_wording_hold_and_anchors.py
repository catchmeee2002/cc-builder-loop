"""B7：skills/planner/SKILL.md「同一 run 内改 contract」段落含字面锚句
「mission 修订（revision +1）、写边界扩大、保护集缩小、判据减弱都需要用户确认」，且仍保留
`bl contract revise --plan <path> --authorize` 这个命令写法。

B8：skills/planner/SKILL.md「文本类任务」段含字面锚句「字面锚句一律用反引号包住；反引号之外的
引号（「」“”）只是行文里的术语引用，不要求出现在产物里」；agents/tester.md 的断言规则含字面锚句
「then 里只有反引号包住的文字是字面锚句；「」和“”里的词是术语引用，不按子串断言」；tester.md 原
有的归一化规则仍在。

B9：skills/builder/SKILL.md 的 next_action 表 finalize 行含字面锚句
「正常等待集成放行不必问用户」，held 行含「已暂缓合入，Stop 放行，直接结束这一轮」；全文不再含
「已按用户决定暂缓合入」与「release 不会作废用户已经给过的回答」；held 行仍给出 `bl hold --release`。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize(text: str) -> str:
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------- B7


B7_ANCHOR = "mission 修订（revision +1）、写边界扩大、保护集缩小、判据减弱都需要用户确认"


def test_planner_skill_mission_revision_anchor_present():
    text = (ROOT / "skills" / "planner" / "SKILL.md").read_text(encoding="utf-8")
    idx = text.index("同一 run 内改 contract")
    section = text[idx:idx + 400]
    assert normalize(B7_ANCHOR) in normalize(section), section


def test_invariant_contract_revise_command_syntax_kept():
    text = (ROOT / "skills" / "planner" / "SKILL.md").read_text(encoding="utf-8")
    assert "bl contract revise --plan <path> --authorize" in text


# ---------------------------------------------------------------- B8


B8_PLANNER_ANCHOR = "字面锚句一律用反引号包住；反引号之外的引号（「」“”）只是行文里的术语引用，不要求出现在产物里"
B8_TESTER_ANCHOR = "then 里只有反引号包住的文字是字面锚句；「」和“”里的词是术语引用，不按子串断言"


def test_planner_skill_text_task_section_has_literal_anchor_rule():
    text = (ROOT / "skills" / "planner" / "SKILL.md").read_text(encoding="utf-8")
    idx = text.index("文本类任务")
    section = text[idx:idx + 600]
    assert normalize(B8_PLANNER_ANCHOR) in normalize(section), section


def test_tester_md_has_literal_anchor_rule_for_quotes():
    text = (ROOT / "agents" / "tester.md").read_text(encoding="utf-8")
    start = text.index("## 硬约束")
    end = text.index("## ", start + len("## 硬约束"))
    section = text[start:end]
    assert normalize(B8_TESTER_ANCHOR) in normalize(section), section


def test_invariant_tester_md_normalization_rule_kept():
    text = (ROOT / "agents" / "tester.md").read_text(encoding="utf-8")
    anchor = "先对原文和锚句做同样的归一化（去掉 markdown 强调标记与反引号，连续空白归一为一个空格）"
    assert anchor in text


# ---------------------------------------------------------------- B9


B9_NEW_ANCHOR = "正常等待集成放行不必问用户"
B9_HELD_ANCHOR = "已暂缓合入，Stop 放行，直接结束这一轮"
B9_REMOVED_OLD_HELD = "已按用户决定暂缓合入"
B9_REMOVED_RELEASE_PHRASE = "release 不会作废用户已经给过的回答"


def test_builder_skill_finalize_row_has_new_hold_wait_anchor():
    text = (ROOT / "skills" / "builder" / "SKILL.md").read_text(encoding="utf-8")
    idx = text.index("| `finalize` |")
    end = text.index("\n", idx)
    row = text[idx:end]
    assert B9_NEW_ANCHOR in row, row


def test_boundary_builder_skill_drops_old_hold_wording():
    text = (ROOT / "skills" / "builder" / "SKILL.md").read_text(encoding="utf-8")
    assert B9_REMOVED_OLD_HELD not in text
    assert B9_REMOVED_RELEASE_PHRASE not in text


def test_invariant_builder_skill_held_row_has_new_anchor_and_release_command():
    text = (ROOT / "skills" / "builder" / "SKILL.md").read_text(encoding="utf-8")
    idx = text.index("| `held` |")
    end = text.index("\n", idx)
    row = text[idx:end]
    assert B9_HELD_ANCHOR in row, row
    assert "`bl hold --release`" in row
