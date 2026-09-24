"""B5：skills/builder/SKILL.md 要提醒 builder 用 TaskStop 停掉角色留下的后台任务，并统一用
agent_id 作为 SendMessage 的续接目标。断言前做与 contract 一致的归一化：去掉 markdown 强调标记
与反引号、连续空白折成一个空格。
"""

from __future__ import annotations

import re

from conftest import ROOT

SKILL = ROOT / "skills" / "builder" / "SKILL.md"
SENTENCE = "status 的 role_background_tasks 非空时，逐个用 TaskStop 停掉这些角色后台任务；续接角色一律用 agent_id 作为 SendMessage 的 to。"


def _normalize(text: str) -> str:
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _sections() -> dict[str, str]:
    text = SKILL.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=## )", text)
    out = {}
    for p in parts:
        m = re.match(r"## (\d)\.", p)
        if m:
            out[m.group(1)] = p
    return out


def test_b5_skill_contains_normalized_sentence():
    full = _normalize(SKILL.read_text(encoding="utf-8"))
    assert _normalize(SENTENCE) in full, full


def test_b5_boundary_sentence_in_advance_or_retro_section():
    sections = _sections()
    combined_advance_retro = _normalize(sections.get("2", "") + sections.get("3", ""))
    assert _normalize(SENTENCE) in combined_advance_retro, combined_advance_retro
    # 不应该出现在启动 / 汇报节里（1 / 4）
    combined_other = _normalize(sections.get("1", "") + sections.get("4", ""))
    assert _normalize(SENTENCE) not in combined_other, combined_other


def test_b5_invariant_skill_doc_hints_and_skill_text_suite_still_passes():
    """不变量：skill 其余内容保留——headings 顺序、doc-sync 提示位置、复盘步骤措辞都还在。"""
    text = SKILL.read_text(encoding="utf-8")
    pos = [text.index(f"\n## {n}.") for n in "1234"]
    assert pos == sorted(pos)
    sections = _sections()
    assert "doc-policy" in sections["2"]
    assert "finalize 前" not in sections["4"]
    s3 = sections["3"]
    assert "bl retro signals" in s3 and "bl retro record" in s3
    for cat in ("business_issue", "builder_loop_issue", "not_incident"):
        assert cat in s3
