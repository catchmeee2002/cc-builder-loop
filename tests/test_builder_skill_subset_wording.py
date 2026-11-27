"""B9：skills/builder/SKILL.md 让 builder 知道「提交前只跑相关测试子集」这条加速纪律。

起点 SKILL.md 只有「门禁期间不要另起全量测试」的约束，没有这条放宽/边界说明，所以锚句断言在
起点上 call 阶段失败（baseline-red）。既有纪律句保留作为守卫，不列入 proof group。

断言前做项目一致的归一化：去掉 markdown 强调标记与反引号，连续空白折成一个空格。
"""

from __future__ import annotations

import re

from conftest import ROOT

SKILL = ROOT / "skills" / "builder" / "SKILL.md"
ANCHOR_SUBSET = "提交前只跑与改动相关的测试子集"
ANCHOR_NOT_MACHINE = "子集通过不等于 machine 通过"
KEEP_DISCIPLINE = "不要在本机另起全量测试或长时间占用 CPU / IO 的后台任务"


def _normalize(text: str) -> str:
    text = re.sub(r"[`*_]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _skill_text() -> str:
    return _normalize(SKILL.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 正向（baseline-red）


def test_b9_skill_contains_subset_test_guidance():
    text = _skill_text()
    assert _normalize(ANCHOR_SUBSET) in text
    assert _normalize(ANCHOR_NOT_MACHINE) in text


# ---------------------------------------------------------------- 边界（守卫）


def test_b9_boundary_gate_time_full_test_discipline_kept():
    assert _normalize(KEEP_DISCIPLINE) in _skill_text()