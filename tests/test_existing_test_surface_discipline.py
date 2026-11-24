"""B1/B2：把「既有测试失效面的点名（planner）与补漏（tester）」两条纪律写进 planner SKILL、
tester brief 与 agents/tester.md。

起点上两处锚句都不存在，正向断言在 call 阶段失败（baseline-red）。
「句子不该出现的一侧」（边界）与既有原文不变（不变量）作为守卫，不列入 proof group——
它们在起点与候选上都应通过，仅由 machine 全量运行。

断言文本前先归一化：去掉反引号与 * 强调标记、连续空白归一为一个空格（与 contract 的边界同法）。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import brief
from builder_loop import ledger as L

ROOT = Path(__file__).resolve().parents[1]
TESTER_MD = ROOT / "agents" / "tester.md"
PLANNER_SKILL = ROOT / "skills" / "planner" / "SKILL.md"

# B1：tester 的 brief 与 agents/tester.md「硬约束」一节都逐字包含
B1_ANCHOR = ("contract 改变了既有行为时，先核对 boundaries 点名的既有测试与 conftest 辅助函数，"
             "再自己 grep 补漏；把排查范围与改动写进交卷 notes。")
# B2：skills/planner/SKILL.md「contract 是 tester 的唯一输入」一节逐字包含
B2_ANCHOR = ("contract 会改变既有行为时，先 grep 既有测试与 conftest 辅助函数，"
             "把编码旧行为的用例逐条写进对应 behavior 的 boundaries。")

# B1 不变量：tester brief 中这五条既有规则的原文
EVIDENCE_LIST_RULE_TEXT = ("proof_spec.test_ids 只列用于证明本 behavior 的测试：每条在反例下都必须变红；"
                           "只检查现役行为的守卫测试不要列入，它们仍由 machine 全量运行。")
ANCHOR_RULE_TEXT = "只有反引号包住的文字是字面锚句；「」和“”里的词是术语引用，不按子串断言。"
LONG_LIVED_RULE_TEXT = "长期合入的测试不得依赖本次 run 的 SHA、当前日期或 CHANGELOG 顶部顺序，除非 contract 明确冻结这些时点事实。"
GUARD_SELFCHECK_RULE_TEXT = ("不列入 proof_spec 的守卫测试，交卷前必须自检：把它声称锁住的那行改回去，它应当变红；"
                             "同一组守卫不得共用会让它们走进同一条分支的素材。")
SNAPSHOT_RULE_TEXT = ("reviewed-boundaries 的 positive / boundary / invariant 测试不得拿某次 run 的历史快照"
                      "（git show <sha>）做比对；原样搬迁类 behavior 只写合入后仍然成立的检查。")
INTEGRATED_SENTENCE = "你交卷后测试会被集成进候选，由 machine 全量跑一遍"

# B2 不变量：SKILL.md 两段既有原文（原文不变）
TEXT_TASK_PARAGRAPH = ("**文本类任务**：文本本身就是交付物的 behavior（文档、prompt、提示语要让读者知道某件事），"
                       "在 then 里冻结要验证的字面锚句，并写明它在哪个文件的哪一节；tester 按这句原文断言，"
                       "实现逐字写入这句原文。字面锚句一律用反引号包住；反引号之外的引号（「」“”）只是行文里的术语引用，"
                       "不要求出现在产物里。CLI 输出作为字面锚句时，必须冻结具体观察面：顶层 --help、某个子命令 --help、"
                       "运行命令的完整输出，或源码文件中的字符串。")
DELETE_TASK_PARAGRAPH = ("**删除 / 移除类任务**：behavior 写成「X 不再存在 / 调用 X 得到 Y 错误」，"
                         "tester 会据此删掉旧测试并写负向测试。旧测试归 tester，不要为了让 builder 能删它而把测试路径划进 "
                         "`builder_write`。「旧测试文件被删除」不写成 behavior：它不是可观察行为，proof 也构造不出反例；"
                         "tester 删除旧测试、integrate 把删除带进候选、machine 全量通过，已经保证了这一点。")


def _normalize(text: str) -> str:
    """去掉反引号与 * 强调标记、连续空白归一为一个空格（contract 点名的归一化）。"""
    text = re.sub(r"[`*]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _section(text: str, marker: str) -> str:
    """从 marker 到下一个 `## ` 标题之间的那一段（与既有章节锚句测试同一取法）。"""
    idx = text.index(marker)
    tail = text[idx:]
    end = tail.find("\n## ", len(marker))
    return tail if end == -1 else tail[:end]


def _paragraph(text: str, marker: str) -> str:
    """从 marker 到下一个空行（段落边界）之间的那一段。"""
    idx = text.index(marker)
    tail = text[idx:]
    end = tail.find("\n\n")
    return tail if end == -1 else tail[:end]


def _render(started, role: str) -> str:
    """contract 的 when：render(build(role=...))，首次注入与续接重取同一渲染。"""
    lg = L.load(started["ledger"])
    return brief.render(brief.build(lg, started["repo"].root, role))


# ---------------------------------------------------------------- B1


def test_b1_tester_brief_names_existing_test_surface(started):
    text = _render(started, "tester")
    assert _normalize(B1_ANCHOR) in _normalize(text), text


def test_b1_tester_md_hard_constraints_name_existing_test_surface():
    section = _section(TESTER_MD.read_text(encoding="utf-8"), "## 硬约束")
    assert _normalize(B1_ANCHOR) in _normalize(section), section[:2000]


# ---------------------------------------------------------------- B2


def test_b2_planner_skill_contract_section_names_existing_test_surface():
    section = _section(PLANNER_SKILL.read_text(encoding="utf-8"), "## contract 是 tester 的唯一输入")
    assert _normalize(B2_ANCHOR) in _normalize(section), section[:2000]


# ---------------------------------------------------------------- 守卫（不列入 proof group）


def test_b1_boundary_reviewer_brief_does_not_carry_rule(started):
    """边界：reviewer 的 brief 不含这句。"""
    assert _normalize(B1_ANCHOR) not in _normalize(_render(started, "reviewer"))


def test_b1_invariant_evidence_list_rule_unchanged(started):
    assert _normalize(EVIDENCE_LIST_RULE_TEXT) in _normalize(_render(started, "tester"))


def test_b1_invariant_anchor_rule_unchanged(started):
    assert _normalize(ANCHOR_RULE_TEXT) in _normalize(_render(started, "tester"))


def test_b1_invariant_long_lived_rule_unchanged(started):
    assert _normalize(LONG_LIVED_RULE_TEXT) in _normalize(_render(started, "tester"))


def test_b1_invariant_guard_selfcheck_rule_unchanged(started):
    assert _normalize(GUARD_SELFCHECK_RULE_TEXT) in _normalize(_render(started, "tester"))


def test_b1_invariant_snapshot_rule_unchanged(started):
    assert _normalize(SNAPSHOT_RULE_TEXT) in _normalize(_render(started, "tester"))


def test_b1_invariant_integration_sentence_unchanged(started):
    assert _normalize(INTEGRATED_SENTENCE) in _normalize(_render(started, "tester"))


def test_b2_boundary_rule_not_in_tester_or_reviewer_brief(started):
    """边界：该句只出现在 planner SKILL.md，不出现在 tester 与 reviewer 的 brief 中。"""
    assert _normalize(B2_ANCHOR) not in _normalize(_render(started, "tester"))
    assert _normalize(B2_ANCHOR) not in _normalize(_render(started, "reviewer"))


def test_b2_invariant_text_task_paragraph_unchanged():
    paragraph = _paragraph(PLANNER_SKILL.read_text(encoding="utf-8"), "**文本类任务**")
    assert _normalize(paragraph) == _normalize(TEXT_TASK_PARAGRAPH)


def test_b2_invariant_delete_task_paragraph_unchanged():
    paragraph = _paragraph(PLANNER_SKILL.read_text(encoding="utf-8"), "**删除 / 移除类任务**")
    assert _normalize(paragraph) == _normalize(DELETE_TASK_PARAGRAPH)