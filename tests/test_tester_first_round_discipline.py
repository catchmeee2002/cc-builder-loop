"""B1/B2：冻结 agents/tester.md「首轮：盲写」一节改写后的两条纪律。

- B1 第 3 条逐字包含「功能被移除的 behavior 只删掉锁住已移除行为的断言，同一文件里仍然成立的
  不变量测试保留或等价迁移；再写负向测试」这段锚句，且旧写法（删掉旧测试）不再出现。
- B2 第 1 条逐字包含「先读测试目录里的 CLAUDE.md 等项目测试规范，再 Glob 现有测试」这段锚句。

起点上两条锚句都不在文件里、旧写法仍在，所以四条正向 / 负向断言的测试在起点 call 阶段失败
（baseline-red）：第 1/3 条各自的锚句、旧写法缺席，以及每条锚句在全文「只出现一次」
（review_focus 的去重要求）。边界（锚句不出现在 tester / reviewer 的 brief 渲染）与不变量
（盲写一节条目数、既有条目原文、硬约束整节、brief 既有规则句）作为守卫，不列入 proof group，
仍由 machine 全量运行。

断言文本前先归一化：去掉反引号与 * 强调标记、连续空白归一为一个空格（与 contract 的边界同法）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from builder_loop import brief
from builder_loop import ledger as L

ROOT = Path(__file__).resolve().parents[1]
TESTER_MD = ROOT / "agents" / "tester.md"

B1_ANCHOR = (
    "功能被移除的 behavior：只删掉锁住已移除行为的断言，同一文件里仍然成立的不变量测试保留或等价迁移；"
    "再写负向测试（断言它不存在 / 调用报错）。"
)
B1_OLD_WORDING = "功能被移除的 behavior：删掉旧测试，写负向测试"
B2_ANCHOR = (
    "读 behaviors / interfaces，先读测试目录里的 CLAUDE.md 等项目测试规范，"
    "再 Glob 现有测试看风格与 fixture，决定文件位置。"
)

# 「首轮：盲写」一节里不应被本次改写触碰的既有条目（第 2、4、5 条）的原文。
BLIND_WRITE_ITEM_2 = (
    "2. 写测试。新接口在基线上 import 不了，所以只需保证语法和收集无误"
    "（`python3 -m pytest --collect-only -q <你的文件>` 之类）；对**已存在**的行为可以直接跑。"
)
BLIND_WRITE_ITEM_4 = (
    "4. 为每个 behavior 选 proof kind：\n"
    "   - `baseline-red`：起点代码 + 你的测试**会在 call 阶段失败**——行为变更、bug 修复、"
    "功能移除的负向测试。断言失败、`KeyError`、解包错都算；收集 / setup 阶段就出错不算"
    "（那说明测试根本没跑起来）。新接口在起点上 import 不了，属于收集失败，不能用。\n"
    "   - `mutation`：新接口 / 新模块用这个。首轮你看不到实现，先不给 `patch_file`，集成后会请你补。\n"
    "   - `reviewed-boundaries`：只有上下文里写明该 behavior 允许时才能用；把 test_ids 分到 "
    "positive / negative / boundary / invariant（并集必须等于 test_ids）。"
)
BLIND_WRITE_ITEM_5 = (
    "5. 有 `SubagentHandback` 工具就调用 `SubagentHandback({message: <完整报告>})` 交卷——"
    "开了它的环境里只有 handback 能送达 Builder，最后写的纯文本不算交卷；没有这个工具就把报告写在"
    "最后一条消息里。两种方式下报告的最后一行都是结果标记（单行 JSON）。proof_spec 每组的 "
    "`behavior_ids` 恰好 1 个。handback 投递前会被校验，不合规就被拒（原因会告诉你），"
    "改好后重新调用即可，被拒的那份不会送到 Builder。"
)

# 「硬约束」整节的既有原文：条目数与各条原文都不应被本次改写触碰（含第 12 条既有锚句）。
HARD_CONSTRAINTS_EXPECTED = """## 硬约束

1. 首轮只工作在 run 起点的基线上：不碰候选 worktree、候选分支、`git log --all`（hook 会拦，别绕）；首次集成之后才可读候选。
2. 只在自己的 worktree 内、brief 的 `tester_write` 范围内读写；Grep / Glob 显式给 `path`。归属有疑问就看 brief，不要按别人的说法推断——**与 builder 写边界重叠的路径也归你**。
3. 断言锚定 behaviors 的 given / when / then、边界与不变量。规格不足以写出可证伪的测试 → `status=insufficient_spec`，在 `notes` 说清缺什么，不要猜。
4. 不改业务源码、构建配置、`builder_write` 内的任何东西；不用 skip / xfail / 吞异常 / 恒真断言让测试变绿。
5. 不自己 `git commit`——你交卷时 hook 会提交你的文件。
6. 每个 behavior 恰好一个 proof group。测试命令由项目冻结，**不要给 argv**，只给 `test_ids`（pytest node id，如 `tests/test_x.py::test_a`）。`test_ids` 是证据名单：只列用于证明本 behavior 的测试，每条在反例下都必须变红；只检查现役行为的守卫测试不要列入，它们仍由 machine 全量运行。
7. 断言文档或提示文本时，先对原文和锚句做同样的归一化（去掉 markdown 强调标记与反引号，连续空白归一为一个空格），再做子串匹配；contract 只描述了意思、没给字面锚句 → status=insufficient_spec，不要自己猜措辞。then 里只有反引号包住的文字是字面锚句；「」和“”里的词是术语引用，不按子串断言。
8. 长期合入的测试不得依赖本次 run 的 SHA、当前日期或 CHANGELOG 顶部顺序，除非 contract 明确冻结这些时点事实。
9. 不要用 run_in_background 起后台任务：交卷后它会把你反复唤醒；需要跑久的命令就前台执行并给足 timeout，只跑与你的结论有关的测试文件。前台命令超过 timeout 也会被转到后台，同样会唤醒你：不要跑扫描整个文件系统这类收不住的命令，ledger 的路径 brief 里已经给了。
10. 不列入 proof_spec 的守卫测试，交卷前必须自检：把它声称锁住的那行改回去，它应当变红；同一组守卫不得共用会让它们走进同一条分支的素材。
11. reviewed-boundaries 的 positive / boundary / invariant 测试不得拿某次 run 的历史快照（git show <sha>）做比对；原样搬迁类 behavior 只写合入后仍然成立的检查。
12. contract 改变了既有行为时，先核对 boundaries 点名的既有测试与 conftest 辅助函数，再自己 grep 补漏；把排查范围与改动写进交卷 notes。
"""

# B2 不变量：tester brief 渲染中既有规则句（EVIDENCE_LIST_RULE / ANCHOR_RULE / LONG_LIVED_RULE /
# GUARD_SELFCHECK_RULE / SNAPSHOT_RULE / EXISTING_TEST_IMPACT_RULE）原文。
BRIEF_RULES = {
    "EVIDENCE_LIST_RULE": (
        "proof_spec.test_ids 只列用于证明本 behavior 的测试：每条在反例下都必须变红；"
        "只检查现役行为的守卫测试不要列入，它们仍由 machine 全量运行。"
    ),
    "ANCHOR_RULE": "只有反引号包住的文字是字面锚句；「」和“”里的词是术语引用，不按子串断言。",
    "LONG_LIVED_RULE": (
        "长期合入的测试不得依赖本次 run 的 SHA、当前日期或 CHANGELOG 顶部顺序，"
        "除非 contract 明确冻结这些时点事实。"
    ),
    "GUARD_SELFCHECK_RULE": (
        "不列入 proof_spec 的守卫测试，交卷前必须自检：把它声称锁住的那行改回去，它应当变红；"
        "同一组守卫不得共用会让它们走进同一条分支的素材。"
    ),
    "SNAPSHOT_RULE": (
        "reviewed-boundaries 的 positive / boundary / invariant 测试不得拿某次 run 的历史快照"
        "（git show <sha>）做比对；原样搬迁类 behavior 只写合入后仍然成立的检查。"
    ),
    "EXISTING_TEST_IMPACT_RULE": (
        "contract 改变了既有行为时，先核对 boundaries 点名的既有测试与 conftest 辅助函数，"
        "再自己 grep 补漏；把排查范围与改动写进交卷 notes。"
    ),
}


def _normalize(text: str) -> str:
    """去掉反引号与 * 强调标记、连续空白归一为一个空格（contract 点名的归一化）。"""
    text = re.sub(r"[*`]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _section(text: str, marker: str) -> str:
    """从 marker 到下一个 `## ` 标题之间的那一段（与既有章节锚句测试同一取法）。"""
    idx = text.index(marker)
    tail = text[idx:]
    end = tail.find("\n## ", len(marker))
    return tail if end == -1 else tail[:end]


def _numbered_items(section: str) -> list[str]:
    """按行首 `N. ` 切分编号条目；子项目（缩进的 `- `）留在所属条目内。"""
    matches = list(re.finditer(r"^\d+\. ", section, flags=re.MULTILINE))
    items: list[str] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(section)
        items.append(section[match.start():end])
    return items


def _blind_write_items() -> list[str]:
    return _numbered_items(_section(TESTER_MD.read_text(encoding="utf-8"), "## 首轮：盲写"))


def _render(started, role: str) -> str:
    """contract 的 when：render(build(role=...))，首次注入与续接重取同一渲染。"""
    lg = L.load(started["ledger"])
    return brief.render(brief.build(lg, started["repo"].root, role))


# ---------------------------------------------------------------- B1


def test_b1_blind_write_item3_contains_removed_feature_discipline():
    """第 3 条逐字包含「只删锁住已移除行为的断言、保留/等价迁移不变量测试」的锚句。"""
    items = _blind_write_items()
    assert len(items) >= 3, items
    assert _normalize(B1_ANCHOR) in _normalize(items[2]), items[2]


def test_b1_old_removed_feature_wording_is_gone():
    """旧写法（删掉旧测试，写负向测试）不再出现在 agents/tester.md。"""
    text = TESTER_MD.read_text(encoding="utf-8")
    assert _normalize(B1_OLD_WORDING) not in _normalize(text)


def test_b1_anchor_appears_exactly_once_in_tester_md():
    """review_focus 的「只出现一次」：B1 锚句在全文恰好一次，复制到其他节即变红。"""
    text = _normalize(TESTER_MD.read_text(encoding="utf-8"))
    assert text.count(_normalize(B1_ANCHOR)) == 1


# ---------------------------------------------------------------- B2


def test_b2_blind_write_item1_contains_test_convention_discipline():
    """第 1 条逐字包含「先读测试目录里的 CLAUDE.md 等项目测试规范」的锚句。"""
    items = _blind_write_items()
    assert items, "「首轮：盲写」一节没有任何编号条目"
    assert _normalize(B2_ANCHOR) in _normalize(items[0]), items[0]


def test_b2_anchor_appears_exactly_once_in_tester_md():
    """review_focus 的「只出现一次」：B2 锚句在全文恰好一次，复制到其他节即变红。"""
    text = _normalize(TESTER_MD.read_text(encoding="utf-8"))
    assert text.count(_normalize(B2_ANCHOR)) == 1


# ---------------------------------------------------------------- 守卫（不列入 proof group）


def test_b1_boundary_anchor_not_in_tester_or_reviewer_brief(started):
    """边界：B1 锚句只出现在 agents/tester.md，不出现在两种 brief 渲染文本中。"""
    anchor = _normalize(B1_ANCHOR)
    assert anchor not in _normalize(_render(started, "tester"))
    assert anchor not in _normalize(_render(started, "reviewer"))


def test_b2_boundary_anchor_not_in_tester_or_reviewer_brief(started):
    """边界：B2 锚句只出现在 agents/tester.md，不出现在两种 brief 渲染文本中。"""
    anchor = _normalize(B2_ANCHOR)
    assert anchor not in _normalize(_render(started, "tester"))
    assert anchor not in _normalize(_render(started, "reviewer"))


def test_b1_invariant_blind_write_section_item_count_unchanged():
    assert len(_blind_write_items()) == 5


def test_b1_invariant_hard_constraints_section_unchanged():
    """「硬约束」一节的条目数与既有各条原文不变（含第 12 条既有锚句）。"""
    section = _section(TESTER_MD.read_text(encoding="utf-8"), "## 硬约束")
    assert len(_numbered_items(section)) == 12
    assert _normalize(section) == _normalize(HARD_CONSTRAINTS_EXPECTED)


def test_b2_invariant_blind_write_items_2_4_5_unchanged():
    """「首轮：盲写」第 2、4、5 条原文不变。"""
    items = _blind_write_items()
    assert len(items) == 5, items
    assert _normalize(items[1]) == _normalize(BLIND_WRITE_ITEM_2)
    assert _normalize(items[3]) == _normalize(BLIND_WRITE_ITEM_4)
    assert _normalize(items[4]) == _normalize(BLIND_WRITE_ITEM_5)


@pytest.mark.parametrize("rule_name", sorted(BRIEF_RULES))
def test_b2_invariant_tester_brief_rule_sentence_unchanged(started, rule_name):
    """tester brief 渲染中既有规则句原文不变。"""
    rendered = _normalize(_render(started, "tester"))
    assert _normalize(BRIEF_RULES[rule_name]) in rendered, rule_name
