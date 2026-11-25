"""B4/B5：删除无消费者的 contract schema 文件、CLAUDE.md 导航指路；planner SKILL 只指路字段表。

B4：schema/contract.schema.json 不再存在；CLAUDE.md 的 schema/ 导航行改为指向 `bl contract fields`，
   agent-result 工作流提示与其余导航行原文保留。起点上旧文件在、旧导航行在 → baseline-red。
B5：「contract 是 tester 的唯一输入」一节新增一条指路 bl contract fields 的列表项；
   「方案文件结构」一节的复用清单改为以 contract.interfaces 为准。起点上两处锚句都不在 → baseline-red。
   tests/test_planner_plan_body_narrowing.py::test_b1_contract_section_unchanged 的冻结常量随本行为更新。

断言文本前先对两侧做同样归一化：去掉反引号与 * 强调标记、连续空白归一为一个空格。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema"
CLAUDE = ROOT / "CLAUDE.md"
PLANNER_SKILL = ROOT / "skills" / "planner" / "SKILL.md"


def _normalize(text: str) -> str:
    """去掉反引号与 * 强调标记、连续空白归一为一个空格（contract 点名的归一化）。"""
    text = re.sub(r"[`*]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _claude_text() -> str:
    return CLAUDE.read_text(encoding="utf-8")


def _skill_text() -> str:
    return PLANNER_SKILL.read_text(encoding="utf-8")


def _section(text: str, marker: str) -> str:
    """从 marker 到下一个 `## ` 标题之间的那一段（与既有章节锚句测试同一取法）。"""
    idx = text.index(marker)
    tail = text[idx:]
    end = tail.find("\n## ", len(marker))
    return tail if end == -1 else tail[:end]


# ==================================================================== B4（proof：baseline-red）

B4_ANCHOR = (
    "proof-spec / agent-result / ledger 的 JSON schema（文档级，runtime 用手写校验）；"
    "contract 字段表在 contract.py，用 bl contract fields 查看"
)


def test_b4_contract_schema_file_removed():
    assert not (SCHEMA / "contract.schema.json").exists()


def test_b4_claude_nav_line_points_to_field_table():
    text = _normalize(_claude_text())
    assert _normalize(B4_ANCHOR) in text, text
    assert "contract / proof-spec" not in text


def test_b4_claude_nav_anchor_appears_exactly_once():
    text = _normalize(_claude_text())
    assert text.count(_normalize(B4_ANCHOR)) == 1


# ---------------------------------------------------------------- B4（守卫，不列入 proof group）


def test_b4_other_schema_files_present_and_valid_json():
    for name in ("proof-spec.schema.json", "agent-result.schema.json", "ledger.schema.json"):
        path = SCHEMA / name
        assert path.is_file(), f"{name} 不应被删除"
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict), name


def test_b4_claude_agent_output_workflow_line_unchanged():
    expected = (
        "- 改 agent 输出契约（`BUILDER_LOOP_RESULT`）→ 同时改 `hooks.py` 的 `*_RESULT_FORMAT`、"
        "`agents/*.md`、`schema/agent-result.schema.json`。"
    )
    assert _normalize(expected) in _normalize(_claude_text())


def test_b4_claude_other_nav_and_pitfall_lines_unchanged():
    text = _normalize(_claude_text())
    for expected in (
        "| `tests/` | pytest；`conftest.py` 提供临时 git 仓（`repo`）、CLI（`cli`）、hook（`hook`）三个基础 fixture，`started` 组合它们 |",
        "| `docs/architecture.md` | 工程推导：模块边界、evidence 投影、hook 接线、失败路径 |",
        "hook stdin 的 `cwd` 跟随 Bash 最后 `cd` 的目录，不能用它定位仓库——一律走 session 绑定。",
    ):
        assert _normalize(expected) in text, expected


# ==================================================================== B5（proof：baseline-red）

B5_FIELDS_ANCHOR = (
    "写 contract 前先跑 bl contract fields：只有 readers 含 tester 的字段，tester 才看得到；"
    "需要它守住的约束必须写进这些字段。"
)
B5_REUSE_ANCHOR = (
    "复用清单（路径::函数 与用途；tester 要 import 的既有接口以 contract 的 interfaces 为准）"
)
B5_OLD_REUSE_TEXT = "方案依赖的既有接口在此声明"
B5_GREP_ITEM = (
    "contract 会改变既有行为时，先 grep 既有测试与 conftest 辅助函数，"
    "把编码旧行为的用例逐条写进对应 behavior 的 boundaries。"
)


def _contract_section() -> str:
    return _section(_skill_text(), "## contract 是 tester 的唯一输入")


def _plan_section() -> str:
    return _section(_skill_text(), "## 方案文件结构")


def test_b5_contract_section_contains_fields_anchor():
    section = _normalize(_contract_section())
    assert _normalize(B5_FIELDS_ANCHOR) in section, section


def test_b5_contract_section_anchor_is_last_list_item_before_deletion_paragraph():
    """边界：锚句作为独立列表项位于该节第一个列表末尾（grep 项之后、删除段之前）。"""
    section = _contract_section()
    lines = section.splitlines()
    hits = [i for i, line in enumerate(lines) if "写 contract 前先跑" in line]
    assert len(hits) == 1, f"锚句应恰好占一行：{hits}"
    idx = hits[0]
    assert lines[idx].lstrip().startswith("- "), f"锚句不是列表项：{lines[idx]!r}"
    # 允许实现把长句折行：锚句完整内容应在列表项到其后若干行之内
    assert _normalize(B5_FIELDS_ANCHOR) in _normalize("\n".join(lines[idx: idx + 3]))
    following = next(line for line in lines[idx + 1:] if line.strip())
    assert following.startswith("**删除 / 移除类任务**"), f"锚句不在第一个列表末尾：{following!r}"

    norm = _normalize(section)
    assert norm.index(_normalize(B5_GREP_ITEM)) < norm.index(_normalize(B5_FIELDS_ANCHOR))
    assert norm.index(_normalize(B5_FIELDS_ANCHOR)) < norm.index("删除 / 移除类任务")


def test_b5_contract_section_anchor_unique_in_whole_skill():
    text = _normalize(_skill_text())
    assert text.count(_normalize(B5_FIELDS_ANCHOR)) == 1
    assert _normalize(B5_FIELDS_ANCHOR) in _normalize(_contract_section())


def test_b5_plan_section_reuse_list_names_interfaces():
    section = _normalize(_plan_section())
    assert _normalize(B5_REUSE_ANCHOR) in section, section


def test_b5_plan_section_drops_old_interfaces_declaration():
    section = _normalize(_plan_section())
    assert _normalize(B5_OLD_REUSE_TEXT) not in section, section


def test_b5_reuse_anchor_unique_in_whole_skill():
    text = _normalize(_skill_text())
    assert text.count(_normalize(B5_REUSE_ANCHOR)) == 1
    assert _normalize(B5_REUSE_ANCHOR) in _normalize(_plan_section())
    assert _normalize(B5_REUSE_ANCHOR) not in _normalize(
        _skill_text()[: _skill_text().index(_plan_section())]
    )


# ---------------------------------------------------------------- B5（守卫，不列入 proof group）


def test_b5_skill_has_no_per_field_reader_list():
    """读者事实只在 bl contract fields 的输出里：SKILL 不写逐字段的路径→读者清单。"""
    assert "assurance.review_focus" not in _normalize(_skill_text())
    assert "mission.behaviors[].boundaries" not in _normalize(_skill_text())