"""B13: agents/tester.md 里 baseline-red 与 add_mutation_patch 两段反例失败形态的说明，
不再要求测试必须以断言失败，而是表述为必须在 call 阶段失败（收集 / setup 阶段出错才不算）。
不变量：「不要为了迁就实现去放宽已有断言」这条既有约束保持不变。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTER_MD = ROOT / "agents" / "tester.md"


def _section(text: str, marker: str) -> str:
    idx = text.index(marker)
    return text[idx:idx + 400]


def test_b13_baseline_red_paragraph_requires_call_phase_failure_not_assertion():
    text = TESTER_MD.read_text(encoding="utf-8")
    section = _section(text, "`baseline-red`")
    assert "call" in section.lower() and "阶段失败" in section
    assert "收集" in section or "setup" in section.lower()  # 收集 / setup 阶段出错不算


def test_b13_add_mutation_patch_paragraph_requires_call_phase_failure_not_assertion():
    text = TESTER_MD.read_text(encoding="utf-8")
    section = _section(text, "`add_mutation_patch`")
    assert "call" in section.lower() and "阶段失败" in section


def test_b13_invariant_no_relaxing_assertions_to_fit_implementation_kept():
    text = TESTER_MD.read_text(encoding="utf-8")
    assert "不要为了迁就实现去放宽已有断言" in text
