"""agents/reviewer.md 与本次退役无关的两条守卫（从被整文件删除的
tests/test_reviewer_checklist_discrimination.py 里取回，只留与 undiscriminated 无关的部分）：

- frontmatter 的 name / model / tools 不变（agent 发现与权限面）
- 审查清单仍保留原有编号条目，且「mutation patch 是否真的破坏了对应 behavior」这条还在

这两项守卫在起点与候选上都应通过，不列入 proof group；退役的 undiscriminated 补偿提醒的负向断言
在 test_brief_test_ids_and_undiscriminated_retired.py 里，这里不恢复。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEWER_MD = ROOT / "agents" / "reviewer.md"


def _checklist_section() -> str:
    text = REVIEWER_MD.read_text(encoding="utf-8")
    start_marker = "## 审查清单"
    idx = text.index(start_marker)
    tail = text[idx + len(start_marker):]
    next_idx = tail.find("\n## ")
    return start_marker + (tail if next_idx == -1 else tail[:next_idx])


def test_reviewer_frontmatter_unchanged():
    front = REVIEWER_MD.read_text(encoding="utf-8").split("---", 2)[1]
    assert "name: reviewer" in front
    assert "model: sonnet" in front
    assert "tools: Read, Glob, Grep, Bash" in front


def test_reviewer_checklist_keeps_existing_items():
    """只断言原有条目都还在（>= 6），不断言恰好几条——新增条目不是删减。"""
    section = _checklist_section()
    numbered = re.findall(r"^\d+\.", section, flags=re.MULTILINE)
    assert len(numbered) >= 6, section
    assert "mutation patch 是否真的破坏了对应 behavior" in section
