"""B1/B2/B3: 冻结 skills/planner/SKILL.md 与 agents/tester.md 中关于「删除类任务不把
旧测试被删写成 behavior」以及「文本类 behavior 冻结字面锚句、按归一化后子串匹配」的
字面措辞。

归一化规则（三条 behavior 共用）：去掉 markdown 强调标记 `**` 与反引号 `` ` ``，
连续空白归一为一个空格。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLANNER_SKILL_MD = ROOT / "skills" / "planner" / "SKILL.md"
TESTER_MD = ROOT / "agents" / "tester.md"


def _normalize(text: str) -> str:
    text = text.replace("**", "").replace("`", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _section_to_next_blank_line(text: str, start_marker: str) -> str:
    idx = text.index(start_marker)
    tail = text[idx:]
    blank_idx = tail.find("\n\n")
    if blank_idx == -1:
        return tail
    return tail[:blank_idx]


def _section_to_next_h2(text: str, start_marker: str) -> str:
    idx = text.index(start_marker)
    tail = text[idx + len(start_marker):]
    next_idx = tail.find("\n## ")
    if next_idx == -1:
        return start_marker + tail
    return start_marker + tail[:next_idx]


# --- B1 --------------------------------------------------------------------

B1_ANCHOR = (
    "「旧测试文件被删除」不写成 behavior：它不是可观察行为，proof 也构造不出反例；"
    "tester 删除旧测试、integrate 把删除带进候选、machine 全量通过，已经保证了这一点。"
)


def _b1_section() -> str:
    text = PLANNER_SKILL_MD.read_text(encoding="utf-8")
    return _section_to_next_blank_line(text, "**删除 / 移除类任务**")


def test_b1_deletion_paragraph_contains_frozen_anchor_sentence():
    section = _normalize(_b1_section())
    assert _normalize(B1_ANCHOR) in section


def test_b1_deletion_paragraph_invariant_behavior_wording_kept():
    section = _b1_section()
    assert "behavior 写成「X 不再存在 / 调用 X 得到 Y 错误」" in section


def test_b1_deletion_paragraph_invariant_write_boundary_wording_kept():
    section = _b1_section()
    assert "不要为了让 builder 能删它而把测试路径划进" in section


def test_b1_anchor_sentence_not_present_elsewhere_in_file():
    # 边界：锚句只应出现在删除段落里，而不是文件其他位置。
    text = PLANNER_SKILL_MD.read_text(encoding="utf-8")
    idx = text.index("**删除 / 移除类任务**")
    before = _normalize(text[:idx])
    assert _normalize(B1_ANCHOR) not in before


# --- B2 --------------------------------------------------------------------

B2_ANCHOR = (
    "文本本身就是交付物的 behavior（文档、prompt、提示语要让读者知道某件事），"
    "在 then 里冻结要验证的字面锚句，并写明它在哪个文件的哪一节；"
    "tester 按这句原文断言，实现逐字写入这句原文。"
)


def _b2_section() -> str:
    text = PLANNER_SKILL_MD.read_text(encoding="utf-8")
    return _section_to_next_h2(text, "## contract 是 tester 的唯一输入")


def test_b2_contract_section_contains_frozen_anchor_sentence():
    section = _normalize(_b2_section())
    assert _normalize(B2_ANCHOR) in section


def test_b2_contract_section_invariant_given_when_then_kept():
    section = _b2_section()
    assert "given / when / then" in section


def test_b2_contract_section_invariant_interfaces_mentioned():
    section = _b2_section()
    assert "interfaces" in section


def test_b2_anchor_not_present_in_other_sections():
    # 边界：锚句只应出现在「contract 是 tester 的唯一输入」节内。
    text = PLANNER_SKILL_MD.read_text(encoding="utf-8")
    section = _section_to_next_h2(text, "## contract 是 tester 的唯一输入")
    idx = text.index(section) + len(section)
    rest = text[idx:]
    assert _normalize(B2_ANCHOR) not in _normalize(rest)


def test_b2_h2_heading_order_unchanged():
    text = PLANNER_SKILL_MD.read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    expected_prefix = ["追问", "contract 是 tester 的唯一输入", "方案文件结构", "contract 标签"]
    assert headings[: len(expected_prefix)] == expected_prefix


# --- B3 --------------------------------------------------------------------

B3_ANCHOR = (
    "断言文档或提示文本时，先对原文和锚句做同样的归一化"
    "（去掉 markdown 强调标记与反引号，连续空白归一为一个空格），再做子串匹配；"
    "contract 只描述了意思、没给字面锚句 → status=insufficient_spec，不要自己猜措辞。"
)


def _b3_section() -> str:
    text = TESTER_MD.read_text(encoding="utf-8")
    return _section_to_next_h2(text, "## 硬约束")


def test_b3_hard_constraints_section_contains_frozen_anchor_sentence():
    section = _normalize(_b3_section())
    assert _normalize(B3_ANCHOR) in section


def test_b3_hard_constraints_invariant_write_boundary_overlap_kept():
    section = _b3_section()
    assert "与 builder 写边界重叠的路径也归你" in section


def test_b3_hard_constraints_invariant_no_argv_kept():
    section = _b3_section()
    assert "不要给 argv" in section


def test_b3_anchor_not_present_in_other_sections():
    # 边界：锚句只应出现在「## 硬约束」节内，而不是「首轮：盲写」等其他节。
    text = TESTER_MD.read_text(encoding="utf-8")
    section = _section_to_next_h2(text, "## 硬约束")
    idx = text.index(section) + len(section)
    rest = text[idx:]
    assert _normalize(B3_ANCHOR) not in _normalize(rest)


def test_b3_frontmatter_name_and_description_unchanged():
    text = TESTER_MD.read_text(encoding="utf-8")
    assert "name: tester" in text
    assert "description:" in text
