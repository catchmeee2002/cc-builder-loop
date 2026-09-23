"""B3: agents/reviewer.md 的 `## 审查清单` 一节要包含字面句子，提醒 reviewer 对 brief 里点名的
「反例下从未变红」的 test_id 逐条判断是不是空壳断言。

归一化规则（与 tests/test_deletion_and_text_behavior_rules.py 一致）：去掉 markdown 强调标记 `**`
与反引号 `` ` ``，连续空白归一为一个空格。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEWER_MD = ROOT / "agents" / "reviewer.md"

ANCHOR = ("brief 若列出在反例下从未变红的 test_id，逐条判断它是不是空壳断言："
          "契约里那条边界在被测设计下是否根本不可能被违反。")


def _normalize(text: str) -> str:
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _checklist_section() -> str:
    text = REVIEWER_MD.read_text(encoding="utf-8")
    start_marker = "## 审查清单"
    idx = text.index(start_marker)
    tail = text[idx + len(start_marker):]
    next_idx = tail.find("\n## ")
    section = start_marker + (tail if next_idx == -1 else tail[:next_idx])
    return section


def test_checklist_section_contains_discrimination_guidance():
    section = _normalize(_checklist_section())
    assert _normalize(ANCHOR) in section


def test_discrimination_guidance_lives_inside_checklist_section_not_elsewhere():
    text = REVIEWER_MD.read_text(encoding="utf-8")
    idx = text.index("## 审查清单")
    before = _normalize(text[:idx])
    assert _normalize(ANCHOR) not in before


def test_checklist_keeps_existing_six_items():
    # rebase-integrate-evidence run（B11）往清单里加了第 7 条（目标分支漂移的复审提醒）；这里只断言
    # 原有 6 条都还在，不再断言恰好 6 条——新增条目不是删减，不该被这条测试拦住。
    section = _checklist_section()
    numbered = re.findall(r"^\d+\.", section, flags=re.MULTILINE)
    assert len(numbered) >= 6
    assert "mutation patch 是否真的破坏了对应 behavior" in section


def test_checklist_frontmatter_unchanged():
    text = REVIEWER_MD.read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    assert "name: reviewer" in front
    assert "model: sonnet" in front
    assert "tools: Read, Glob, Grep, Bash" in front
