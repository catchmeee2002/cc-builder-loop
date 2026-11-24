"""B1-B4：把四条纪律（reviewer 观察缺失、review_focus 覆盖面、守卫回退自检、历史快照禁令）
写进角色 brief 与 agents 定义。

起点上这四句都不在对应产物里，正向断言在 call 阶段失败（baseline-red）。
「句子不该出现的一侧」（边界）与既有原文不变（不变量）作为守卫，不列入 proof group——
它们在起点与候选上都应通过，仅由 machine 全量运行。

断言文本前先归一化：去掉反引号与 * 强调标记、连续空白归一为一个空格；**保留下划线**——锚句里的
behaviors_verified / proof_spec 是标识符，抹掉下划线会让写错的标识符也蒙混过关。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import brief
from builder_loop import ledger as L

ROOT = Path(__file__).resolve().parents[1]
TESTER_MD = ROOT / "agents" / "tester.md"
REVIEWER_MD = ROOT / "agents" / "reviewer.md"

B1_ANCHOR = ("工具调用没有拿到输出时，先重试或换命令；仍拿不到就缩小 behaviors_verified 或判 blocked，"
             "不得以其他 gate 的结论或自述代替自己的观察。")
B2_ANCHOR = "review_focus 点名的约束覆盖整个候选 diff，包括 tester 集成进来的测试与夹具，不只是实现文件。"
B3_TESTER_ANCHOR = ("不列入 proof_spec 的守卫测试，交卷前必须自检：把它声称锁住的那行改回去，它应当变红；"
                    "同一组守卫不得共用会让它们走进同一条分支的素材。")
B3_REVIEWER_ANCHOR = "对守卫类测试的 finding 复审时，要求附回退实验结果：把目标修复回退后该测试是否变红。"
B4_ANCHOR = ("reviewed-boundaries 的 positive / boundary / invariant 测试不得拿某次 run 的历史快照"
             "（git show <sha>）做比对；原样搬迁类 behavior 只写合入后仍然成立的检查。")

LONG_LIVED_RULE_TEXT = "长期合入的测试不得依赖本次 run 的 SHA、当前日期或 CHANGELOG 顶部顺序，除非 contract 明确冻结这些时点事实。"
TARGET_DRIFT_HINT_TEXT = ("目标分支在你上次审查后前进了。patch 未变不构成沿用上次结论的依据：本轮复审漂入的提交与候选的交互——"
                          "候选改过契约的函数是否有了新的调用方、漂入的文档是否引用了候选删改的东西、是否出现了重复实现。")
DOC_HINTS_HEAD = "文档引用线索（启发式 grep，可能误报；逐条判断，确实失效的按审查清单第 6 条处理）:"


def _normalize(text: str) -> str:
    """去掉反引号与 * 强调标记、连续空白归一；保留下划线——锚句里的 behaviors_verified /
    proof_spec 是标识符，抹掉下划线会让写错的标识符也匹配上。"""
    text = re.sub(r"[`*]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _section(text: str, marker: str) -> str:
    """从 marker 到下一个 `## ` 标题之间的那一段（与既有章节锚句测试同一取法）。"""
    idx = text.index(marker)
    tail = text[idx:]
    end = tail.find("\n## ", len(marker))
    return tail if end == -1 else tail[:end]


def _render(started, role: str) -> str:
    """contract 的 when：render(build(role=...))，首次注入与续接重取同一渲染。"""
    lg = L.load(started["ledger"])
    return brief.render(brief.build(lg, started["repo"].root, role))


# ---------------------------------------------------------------- B1


def test_b1_reviewer_brief_states_observation_requirement(started):
    text = _render(started, "reviewer")
    assert _normalize(B1_ANCHOR) in _normalize(text), text


def test_b1_reviewer_md_checklist_states_observation_requirement():
    section = _section(REVIEWER_MD.read_text(encoding="utf-8"), "## 审查清单")
    assert _normalize(B1_ANCHOR) in _normalize(section), section[:2000]


def test_b1_boundary_tester_brief_does_not_carry_reviewer_observation_rule(started):
    text = _render(started, "tester")
    assert _normalize(B1_ANCHOR) not in _normalize(text)


def test_b1_invariant_reviewer_brief_existing_hints_unchanged(started):
    text = _render(started, "reviewer")
    assert _normalize(LONG_LIVED_RULE_TEXT) in _normalize(text)
    # 文档引用线索整行原文不变：无命中时是「表头 + 空格 + 无」，有命中时是表头 + 逐条缩进行
    assert brief._render_doc_hints({"hits": [], "error": None}) == [DOC_HINTS_HEAD + " 无"]
    assert brief._render_doc_hints({"hits": ["src/x.py:1"], "error": None}) == [DOC_HINTS_HEAD, "  - src/x.py:1"]
    assert DOC_HINTS_HEAD in text or "文档引用线索不可用" in text
    assert brief.LONG_LIVED_RULE == LONG_LIVED_RULE_TEXT
    assert brief.TARGET_DRIFT_HINT == TARGET_DRIFT_HINT_TEXT


# ---------------------------------------------------------------- B2


def test_b2_reviewer_brief_covers_review_focus_scope(started):
    text = _render(started, "reviewer")
    assert _normalize(B2_ANCHOR) in _normalize(text), text
    # 边界：review_focus 为空列表时 brief 仍渲染该句，且 review_focus 渲染行不变
    assert "review_focus: []" in text


def test_b2_reviewer_md_states_review_focus_scope():
    full = REVIEWER_MD.read_text(encoding="utf-8")
    assert _normalize(B2_ANCHOR) in _normalize(full)


def test_b2_invariant_review_focus_line_rendering_unchanged(started):
    """不变量：review_focus 本身的渲染行不变（空列表与非空列表两种形态；不含新锚句断言）。"""
    lg = L.load(started["ledger"])
    assert "review_focus: []" in brief.render(brief.build(lg, started["repo"].root, "reviewer"))
    lg["contract"]["assurance"]["review_focus"] = ["候选改过 contract.py 的写边界"]
    text = brief.render(brief.build(lg, started["repo"].root, "reviewer"))
    assert "review_focus: ['候选改过 contract.py 的写边界']" in text


# ---------------------------------------------------------------- B3


def test_b3_tester_brief_states_guard_self_check(started):
    text = _render(started, "tester")
    assert _normalize(B3_TESTER_ANCHOR) in _normalize(text), text


def test_b3_tester_md_hard_constraints_state_guard_self_check():
    section = _section(TESTER_MD.read_text(encoding="utf-8"), "## 硬约束")
    assert _normalize(B3_TESTER_ANCHOR) in _normalize(section), section[:2000]


def test_b3_reviewer_brief_requires_guard_rollback_evidence(started):
    text = _render(started, "reviewer")
    assert _normalize(B3_REVIEWER_ANCHOR) in _normalize(text), text


def test_b3_reviewer_md_checklist_requires_guard_rollback_evidence():
    section = _section(REVIEWER_MD.read_text(encoding="utf-8"), "## 审查清单")
    assert _normalize(B3_REVIEWER_ANCHOR) in _normalize(section), section[:2000]


def test_b3_boundary_sentences_do_not_cross_briefs(started):
    tester = _normalize(_render(started, "tester"))
    reviewer = _normalize(_render(started, "reviewer"))
    assert _normalize(B3_TESTER_ANCHOR) not in reviewer
    assert _normalize(B3_REVIEWER_ANCHOR) not in tester


def test_b3_invariant_evidence_list_rule_unchanged(started):
    text = _render(started, "tester")
    assert _normalize(brief.EVIDENCE_LIST_RULE) in _normalize(text)
    assert "只检查现役行为的守卫测试不要列入" in _normalize(text)


# ---------------------------------------------------------------- B4


def test_b4_tester_brief_bans_historical_snapshot_comparison(started):
    text = _render(started, "tester")
    assert _normalize(B4_ANCHOR) in _normalize(text), text


def test_b4_tester_md_hard_constraints_ban_historical_snapshots():
    section = _section(TESTER_MD.read_text(encoding="utf-8"), "## 硬约束")
    assert _normalize(B4_ANCHOR) in _normalize(section), section[:2000]


def test_b4_boundary_reviewer_brief_does_not_carry_history_ban(started):
    text = _render(started, "reviewer")
    assert _normalize(B4_ANCHOR) not in _normalize(text)


def test_b4_invariant_long_lived_rule_unchanged_in_both_briefs(started):
    tester = _render(started, "tester")
    reviewer = _render(started, "reviewer")
    assert _normalize(LONG_LIVED_RULE_TEXT) in _normalize(tester)
    assert _normalize(LONG_LIVED_RULE_TEXT) in _normalize(reviewer)
    assert brief.LONG_LIVED_RULE == LONG_LIVED_RULE_TEXT
