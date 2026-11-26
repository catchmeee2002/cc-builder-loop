"""B5：skills/builder/SKILL.md 的「角色的后台残留」段把未停后台任务降级为信息性提示。

该段必须含字面锚句「status 的 `role_background_tasks` 只是信息性提示，无需处理」，不再包含
「逐个用 TaskStop 停掉」；「续接角色一律用 agent_id 作为 SendMessage 的 to」保留。

起点 SKILL 恰好是旧措辞（status 非空时逐个 TaskStop 停掉），所以本文件的断言在起点上 call 阶段
失败（baseline-red）。断言前做与项目一致的归一化：去掉 markdown 强调标记与反引号、连续空白折成
一个空格。
"""

from __future__ import annotations

import re

from conftest import ROOT

SKILL = ROOT / "skills" / "builder" / "SKILL.md"
MARKER = "角色的后台残留"
ANCHOR = "status 的 `role_background_tasks` 只是信息性提示，无需处理"
OLD_CLEANUP = "逐个用 TaskStop 停掉"
KEEP_AGENT_ID = "续接角色一律用 agent_id 作为 SendMessage 的 to"


def _normalize(text: str) -> str:
    text = re.sub(r"[`*_]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def _background_paragraph() -> str:
    """「角色的后台残留」段：从该段标记到下一个空行。"""
    text = _text()
    start = text.index(MARKER)
    end = text.find("\n\n", start)
    return text[start:] if end == -1 else text[start:end]


def _sections() -> dict[str, str]:
    parts = re.split(r"(?m)^(?=## )", _text())
    out = {}
    for p in parts:
        m = re.match(r"## (\d)\.", p)
        if m:
            out[m.group(1)] = p
    return out


# ---------------------------------------------------------------- B5（baseline-red）


def test_b5_background_paragraph_has_informational_hint_anchor():
    para = _normalize(_background_paragraph())
    assert _normalize(ANCHOR) in para, para


def test_b5_background_paragraph_drops_taskstop_cleanup_instruction():
    para = _normalize(_background_paragraph())
    assert _normalize(OLD_CLEANUP) not in para, para


# ---------------------------------------------------------------- 边界 / 不变量（守卫，不列入 proof_spec）


def test_guard_background_paragraph_keeps_agent_id_resume_rule():
    """边界：续接角色用 agent_id 作 SendMessage 的 to 这条规矩保留。"""
    para = _normalize(_background_paragraph())
    assert _normalize(KEEP_AGENT_ID) in para, para


def test_guard_skill_doc_hints_and_retro_text_suite_still_passes():
    """不变量：skill 其余内容保留——headings 顺序、doc-sync 提示位置、复盘步骤措辞都还在。"""
    text = _text()
    pos = [text.index(f"\n## {n}.") for n in "1234"]
    assert pos == sorted(pos)
    sections = _sections()
    assert "doc-policy" in sections["2"]
    assert "finalize 前" not in sections["4"]
    s3 = sections["3"]
    assert "bl retro signals" in s3 and "bl retro record" in s3
    for cat in ("business_issue", "builder_loop_issue", "not_incident"):
        assert cat in s3