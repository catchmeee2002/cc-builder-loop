"""B1：skills/planner/SKILL.md「方案文件结构」一节收窄——删掉复述 contract 的三段、写明正文不复述
contract 已有的事实。B2：开头（第一个 `## ` 之前）的收敛自检第④条改成「每条 behavior 的 then 可机判」。

起点上两处锚句都不在产物里、旧段名与旧第④条仍在，所以正向断言在 call 阶段失败（baseline-red）。
「不该出现的一侧」与既有原文不变（边界 / 不变量）作为守卫，不列入 proof group——它们在起点与候选上
都通过，仅随交付合入、由 machine 全量运行（例外：锚句全文唯一性守卫要求锚句已存在，起点上必红）。
「contract 是 tester 的唯一输入」「contract 标签」两节的原文不变按整节归一化全文做字面比较（不是抽检锚串）。

断言文本前先对两侧做同样归一化：去掉反引号与 * 强调标记、连续空白归一为一个空格（与 contract 的边界同法）。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import brief
from builder_loop import ledger as L

ROOT = Path(__file__).resolve().parents[1]
PLANNER_SKILL = ROOT / "skills" / "planner" / "SKILL.md"

# B1：「方案文件结构」一节逐字包含的字面锚句
B1_ANCHOR = "正文不复述 contract 已有的事实：行为、边界与验收以 contract 为准。"
# B1：该节不再包含的段名（复述 contract 的三段）
B1_REMOVED_NAMES = ("功能行为规格", "约束与边界", "验收标准")
# B1：该节仍包含的段名
B1_KEPT_NAMES = ("背景与目标", "方案设计", "复用清单", "风险与退路", "文件地图", "执行任务列表", "contract")
# B1 不变量：h2 标题与顺序（完整列表，追加新 h2 也算破坏）
EXPECTED_H2 = ["追问", "contract 是 tester 的唯一输入", "方案文件结构", "contract 标签"]

# B2：开头收敛自检第④条
B2_ANCHOR = "④ 每条 behavior 的 then 可机判"
B2_OLD = "④ 验收标准可机判"

# B2 不变量：①②③ 三条原文
B2_ITEMS = (
    "① 每条用户可见变化都有对应 behavior",
    "② 要动的每个文件在写边界里有归属",
    "③ 该复用的既有代码已点名（复用清单非空，或明示无可复用）",
)
# B2 不变量：validate 与 control_files_protected 两处说明原文
B2_VALIDATE_SENTENCE = (
    "四条全过再跑 `bl contract validate --plan <path> --check-repo`，确认可解析、目标分支与 loop.yml 可用，"
    "并看一眼它列出的 `control_files_protected`——那些文件 builder 改不了，"
    "任务确实要动就在 `builder_write` 里**字面点名**。"
)

# B1 不变量：两节原文不变——冻结归一化全文做字面比较（空白由 _normalize 归一，故可折行书写）。

EXPECTED_CONTRACT_SECTION = (
    '## contract 是 tester 的唯一输入 tester 在 run 起点的冻结基线上盲写测试，看不到实现。它能依据的只有 '
    'contract，所以： - 每条 behavior 都要能直接写成测试：given / when / then '
    '写具体值域与可观察结果，别写「功能正常」。 - boundaries（边界条件）和 '
    'invariants（不应被破坏的既有行为）写全——这是测试厚度的来源。 - 新接口的签名写进 interfaces（路径::函数(参数:类型) -> '
    '返回），tester 据此 import。 - 被测行为依赖的既有接口也写进 interfaces——tester 据此 import，builder '
    '不得另造等价物。 - 外部依赖怎么 mock 写进 mock_strategy。 - contract 会改变既有行为时，先 grep 既有测试与 '
    'conftest 辅助函数，把编码旧行为的用例逐条写进对应 behavior 的 boundaries。 - 写 contract 前先跑 bl contract '
    'fields：只有 readers 含 tester 的字段，tester 才看得到；需要它守住的约束必须写进这些字段。 删除 / 移除类任务：behavior '
    '写成「X 不再存在 / 调用 X 得到 Y 错误」，tester 会据此删掉旧测试并写负向测试。旧测试归 tester，不要为了让 builder '
    '能删它而把测试路径划进 builder_write。「旧测试文件被删除」不写成 behavior：它不是可观察行为，proof '
    '也构造不出反例；tester 删除旧测试、integrate 把删除带进候选、machine 全量通过，已经保证了这一点。 '
    '文本类任务：文本本身就是交付物的 behavior（文档、prompt、提示语要让读者知道某件事），在 then '
    '里冻结要验证的字面锚句，并写明它在哪个文件的哪一节；tester '
    '按这句原文断言，实现逐字写入这句原文。字面锚句一律用反引号包住；反引号之外的引号（「」“”）只是行文里的术语引用，不要求出现在产物里。CLI '
    '输出作为字面锚句时，必须冻结具体观察面：顶层 --help、某个子命令 --help、运行命令的完整输出，或源码文件中的字符串。 test_ids '
    '是证据名单：behavior 里只检查现役行为、在反例上不应变红的守卫测试，不要要求 tester 把它们列入 proof；它们仍随交付合入并由 '
    'machine 全量运行。'
)

EXPECTED_CONTRACT_LABEL_SECTION = (
    '## contract 标签 markdown <!-- builder-loop-contract --> json '
    '{"schema":"builder-loop/contract@1", '
    '"mission":{"revision":1,"slug":"<kebab-case>","objective":"<一句话>", '
    '"behaviors":[{"id":"B1","given":"…","when":"…","then":"…", '
    '"boundaries":["…"],"invariants":["…"]}], "interfaces":["src/x.py::fn(a:int) '
    '-> int"],"mock_strategy":{"db":"sqlite in-memory"}, '
    '"acceptance_cases":[],"trust_boundaries":["…"]}, '
    '"authority":{"builder_write":["src/"],"tester_write":["tests/"], '
    '"protected_paths":[".claude/loop.yml"]}, '
    '"assurance":{"required":["machine","tester","proof","reviewer"],"review_focus":["…"]}} '
    '<!-- /builder-loop-contract --> 规则： - behaviors 非空且 id '
    '唯一。每条默认要求强证明（baseline-red 或 mutation）；只有确实没法构造反例的 behavior 才加 '
    '"proof":"reviewed-boundaries" 放行最弱的那种，并在方案里说明原因。 - 写边界两边 glob 都命中的路径归 '
    'tester（builder_write:[""] 也不会让 builder 碰到 tests/）。落在 tester_write 之内的路径不能写进 '
    'builder_write（字面点名也不行，validate 会拒）——要它跟着实现变，就在对应 behavior 里写明改成什么，由 tester '
    '改。 - '
    '控制面文件（pytest.ini、pyproject.toml、conftest.py、Makefile、package.json、go.mod、Cargo.toml、BUILD、WORKSPACE、loop.yml '
    '等）builder 靠 glob 顺带命中不算数，要改必须字面点名；tester_write 内的 conftest.py 归 tester。 - '
    '文档随交付走：任务会改变对外行为 / 契约 / 导航，或本身就要探查未知的平台行为 / '
    '外部约束时，把会受影响的文档容器（CLAUDE.md、README、docs/ 下对应文件）字面列进 builder_write，具体哪些按 '
    '~/.claude/doc-policy.md 判断（只关乎 AI 协作的坑归 memory，不是列 CLAUDE.md '
    '的理由）。列进写边界只是授权，改不改仍由 builder 按 doc-policy §0 判断，不找补。漏列的代价是 run 内打断用户授权、已有 '
    'evidence 全部重跑。纯内部重构不触及文档面，不列。 - proof 依赖 tester；machine_commands 与 '
    'proof_runner 不写（start 时从 .claude/loop.yml 冻结）；方案新增或依赖 loop.yml 里的 machine '
    'stage 时，把 stage 名写进 assurance.machine_stages（只写名字），start '
    '会校验它们都在；target_branch 省略 = 当前分支。 - 项目没有 .claude/loop.yml → 先跑 builder-loop '
    '接入向导。 - 要锁住「现役行为不变」的文件不列进 protected_paths：mutation 证明必须改它，而 patch 只能改 '
    'builder 拥有的文件；把它字面列进 builder_write，并在 review_focus 里要求候选对它零改动。 同一 run 内改 '
    'contract：mission 修订（revision +1）、写边界扩大、保护集缩小、判据减弱都需要用户确认（bl contract revise '
    '--plan <path> --authorize）。run 内不能增删 tester gate。'
)

def _normalize(text: str) -> str:
    """去掉反引号与 * 强调标记、连续空白归一为一个空格（contract 点名的归一化）。"""
    text = re.sub(r"[`*]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _section(marker: str) -> str:
    """从 `## <marker>` 到下一个 `## ` 标题之间的那一段（与既有章节锚句测试同一取法）。"""
    text = PLANNER_SKILL.read_text(encoding="utf-8")
    start = marker if marker.startswith("## ") else f"## {marker}"
    idx = text.index(start)
    tail = text[idx:]
    end = tail.find("\n## ", len(start))
    return tail if end == -1 else tail[:end]


def _head() -> str:
    """第一个 `## ` 标题之前的全部文本。"""
    text = PLANNER_SKILL.read_text(encoding="utf-8")
    return re.split(r"(?m)^## ", text, maxsplit=1)[0]


def _render(started, role: str) -> str:
    """contract 的 when：render(build(role=...))，首次注入与续接重取同一渲染。"""
    lg = L.load(started["ledger"])
    return brief.render(brief.build(lg, started["repo"].root, role))


def _plan_section() -> str:
    return _section("方案文件结构")


# ---------------------------------------------------------------- B1（proof：baseline-red）


def test_b1_plan_section_contains_no_restate_anchor():
    section = _normalize(_plan_section())
    assert _normalize(B1_ANCHOR) in section, section


def test_b1_plan_section_drops_contract_restating_segment_names():
    section = _normalize(_plan_section())
    for name in B1_REMOVED_NAMES:
        assert name not in section, f"「{name}」仍在「方案文件结构」一节里：{section}"


# ---------------------------------------------------------------- B1（守卫，不列入 proof group）


def test_b1_plan_section_keeps_remaining_segment_names():
    section = _normalize(_plan_section())
    for name in B1_KEPT_NAMES:
        assert name in section, f"「{name}」不在「方案文件结构」一节里：{section}"


def test_b1_anchor_scope_is_plan_section_only():
    """边界：锚句只在「方案文件结构」一节，节外不出现，且全文恰好一次。"""
    section = _plan_section()
    text = PLANNER_SKILL.read_text(encoding="utf-8")
    rest = text[: text.index(section)] + text[text.index(section) + len(section) :]
    assert _normalize(B1_ANCHOR) in _normalize(section)
    assert _normalize(B1_ANCHOR) not in _normalize(rest)
    assert _normalize(text).count(_normalize(B1_ANCHOR)) == 1


def test_b1_anchor_not_in_tester_or_reviewer_brief(started):
    """边界：锚句不出现在 tester 与 reviewer 的 brief 渲染文本中。"""
    assert _normalize(B1_ANCHOR) not in _normalize(_render(started, "tester"))
    assert _normalize(B1_ANCHOR) not in _normalize(_render(started, "reviewer"))


def test_b1_h2_titles_and_order_unchanged():
    headings = re.findall(r"^## (.+)$", PLANNER_SKILL.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert headings == EXPECTED_H2


def test_b1_contract_section_unchanged():
    """不变量：「contract 是 tester 的唯一输入」整节原文不变（含 #294 锚句与两段任务说明）。"""
    assert _normalize(_section("contract 是 tester 的唯一输入")) == _normalize(EXPECTED_CONTRACT_SECTION)


def test_b1_contract_label_section_unchanged():
    """不变量：「contract 标签」整节原文不变。"""
    assert _normalize(_section("contract 标签")) == _normalize(EXPECTED_CONTRACT_LABEL_SECTION)


# ---------------------------------------------------------------- B2（proof：baseline-red）


def test_b2_head_contains_machine_checkable_then_anchor():
    head = _normalize(_head())
    assert _normalize(B2_ANCHOR) in head, head


def test_b2_head_drops_old_acceptance_check_anchor():
    head = _normalize(_head())
    assert _normalize(B2_OLD) not in head, head


# ---------------------------------------------------------------- B2（守卫，不列入 proof group）


def test_b2_head_items_one_two_three_unchanged():
    head = _normalize(_head())
    for item in B2_ITEMS:
        assert _normalize(item) in head, f"{item!r} 不在开头收敛自检里：{head}"


def test_b2_head_validate_and_control_files_notes_unchanged():
    head = _normalize(_head())
    assert _normalize(B2_VALIDATE_SENTENCE) in head, head
