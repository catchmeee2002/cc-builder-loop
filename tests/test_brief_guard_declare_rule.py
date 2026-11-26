"""B12：guard 声明的引导句进 brief 与 agents/tester.md。

tester 的 brief 渲染（brief.render / bl brief --role tester）与 agents/tester.md 都要含字面锚句：
`守卫测试想让 runtime 核对它的鉴别力：写进所在 group 的 guards（test_ids 加 patch_file，patch 只改 builder 文件），
proof 会要求打上 patch 后它们全部变红。`

起点代码两处都没有这句（brief 的常量与 agents/tester.md 的条目都是本 run 新增），所以正向断言在起点上
call 阶段失败（baseline-red）。reviewer brief 不含这句是守卫（不列入 group）。

注意：锚句按约定归一化后做子串匹配（去 markdown 强调标记与反引号、连续空白归一）。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import brief as brief_mod
from builder_loop import ledger as L

ROOT = Path(__file__).resolve().parents[1]
TESTER_MD = ROOT / "agents" / "tester.md"

ANCHOR = ("守卫测试想让 runtime 核对它的鉴别力：写进所在 group 的 guards（test_ids 加 patch_file，"
          "patch 只改 builder 文件），proof 会要求打上 patch 后它们全部变红。")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", text)).strip()


def _render(started, role: str) -> str:
    lg = L.load(started["ledger"])
    return brief_mod.render(brief_mod.build(lg, started["repo"].root, role))


# ---------------------------------------------------------------- 两处都含锚句（baseline-red）


def test_b12_tester_brief_states_guard_declaration_rule(started):
    assert _norm(ANCHOR) in _norm(_render(started, "tester"))


def test_b12_tester_agent_md_states_guard_declaration_rule():
    assert _norm(ANCHOR) in _norm(TESTER_MD.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 守卫（不列入 proof group）


def test_guard_b12_reviewer_brief_has_no_guard_declaration_rule(started):
    """reviewer 的 brief 不含这句（引导句只给 tester）。"""
    assert _norm(ANCHOR) not in _norm(_render(started, "reviewer"))