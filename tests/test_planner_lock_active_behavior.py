"""B6：skills/planner/SKILL.md 的 `## contract 标签` 规则列表要写明——要锁住「现役行为不变」的文件，
不列进 protected_paths（mutation 证明必须能改它），而是字面列进 builder_write 并在 review_focus 里
要求候选对它零改动。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "planner" / "SKILL.md"

SENTENCE = ("要锁住「现役行为不变」的文件不列进 protected_paths：mutation 证明必须改它，"
            "而 patch 只能改 builder 拥有的文件；把它字面列进 builder_write，"
            "并在 review_focus 里要求候选对它零改动。")


def _normalize(s: str) -> str:
    s = s.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


def _contract_tag_section(text: str) -> str:
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "## contract 标签")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end])


def _rules_list(section: str) -> list[str]:
    lines = section.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "规则：")
    items: list[str] = []
    for ln in lines[start + 1:]:
        if ln.startswith("- "):
            items.append(ln)
        elif ln.strip() == "":
            continue
        else:
            break
    return items


def test_contract_rules_include_lock_active_behavior_sentence():
    text = SKILL.read_text(encoding="utf-8")
    section = _contract_tag_section(text)
    items = _rules_list(section)
    assert items, "规则列表为空——先确认 `## contract 标签` 与 `规则：` 的锚点还在"
    normalized_items = [_normalize(it) for it in items]
    assert any(SENTENCE in it for it in normalized_items), normalized_items


def test_sentence_is_in_rules_list_not_deletion_section():
    text = SKILL.read_text(encoding="utf-8")
    section = _contract_tag_section(text)
    items = _rules_list(section)
    normalized_items = [_normalize(it) for it in items]
    assert any(SENTENCE in it for it in normalized_items)

    # 「删除 / 移除类任务」是 `## contract 标签` 之前的独立段落，句子不该出现在那里
    deletion_start = text.find("**删除 / 移除类任务**")
    contract_tag_start = text.find("## contract 标签")
    assert 0 <= deletion_start < contract_tag_start
    deletion_section = text[deletion_start:contract_tag_start]
    assert _normalize(SENTENCE) not in _normalize(deletion_section)


def test_existing_rule_items_still_present():
    """不变量：新句子是追加，不是替换——原有规则条目还在。"""
    text = SKILL.read_text(encoding="utf-8")
    section = _contract_tag_section(text)
    items = _rules_list(section)
    normalized = _normalize(" ".join(items))
    assert "behaviors 非空且 id 唯一" in normalized
