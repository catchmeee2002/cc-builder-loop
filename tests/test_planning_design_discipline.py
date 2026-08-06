from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from harness import run_process


ROOT = Path(__file__).resolve().parents[1]
SPEC_HEAD = "1b512b120a673470a4ee154b7c8dd8ac3c3f7e1f"
DELIVERY_HEAD = "e238fe159ce16f225ab7e4dd35a19052f02b2122"
AGENTS_PATH = Path("AGENTS.md")
PLANNER_PATH = Path("skills/builder-loop-planner/SKILL.md")
FULL_DRIVER_PATH = Path("skills/full-driver-v4-experiment/SKILL.md")
BUILDER_PATH = Path("skills/builder/SKILL.md")
BUILDER_VARIANTS_PATH = Path("experiments/agent-behavior/variants.json")
SCENARIOS_PATH = Path("experiments/agent-behavior/scenarios.json")
BEHAVIOR_RUNNER_PATH = Path("experiments/agent-behavior/runner.py")
BUILDER_LINE_LIMIT = 278
BUILDER_MAINTENANCE_LINE_ALLOWANCE = 0
PLANNER_LINE_ALLOWANCE = 36
REFERENCE_PATH = Path("skills/builder-loop-planner/references/design-decisions.md")
REVIEWER_BLOB = "14b67f0abec0be5d1f407b5247d8719b5acd2d0e"
REVIEWER_PATH = Path("agents/reviewer.toml")
TESTER_PATH = Path("agents/tester.toml")
PHILOSOPHY_PATH = Path("docs/design-philosophy.md")
ARCHITECTURE_PATH = Path("docs/architecture.md")
V4_LIFECYCLE_TERMS = (
    "assurancev4",
    "v4",
    "assurance_schema_version=4",
    "schemaversion=4",
)


def read(relative: Path) -> str:
    path = ROOT / relative
    return path.read_text() if path.is_file() else ""


def normalized_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", "", line).lower() for line in text.splitlines()]


def paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", "", item).lower() for item in re.split(r"\n\s*\n", text)]


def has_terms(text: str, *groups: tuple[str, ...]) -> bool:
    compact = re.sub(r"\s+", "", text).lower()
    return all(any(term.lower() in compact for term in group) for group in groups)


def matching_paragraphs(text: str, *groups: tuple[str, ...]) -> list[str]:
    return [item for item in paragraphs(text) if has_terms(item, *groups)]


def heading_scoped_paragraphs(
    text: str,
    heading_terms: tuple[str, ...],
    *groups: tuple[str, ...],
) -> list[str]:
    headings = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, flags=re.MULTILINE))
    matches: list[str] = []
    for index, heading in enumerate(headings):
        if not has_terms(heading.group(2), heading_terms):
            continue
        level = len(heading.group(1))
        end = len(text)
        for later in headings[index + 1 :]:
            if len(later.group(1)) <= level:
                end = later.start()
                break
        matches.extend(matching_paragraphs(text[heading.end() : end], *groups))
    return matches


UNIVERSAL_CUES = tuple(
    quantifier + subject
    for quantifier in ("所有", "每个", "全部")
    for subject in ("任务", "计划", "规划")
) + ("任何任务", "无论什么任务")
MANDATORY_CUES = ("必须", "需要", "应当", "要求", "始终", "一律", "都要", "均需", "不得遗漏")
ALTERNATIVE_CUES = ("方案", "备选", "候选", "方向", "选项", "alternative")
MULTIPLE_CUES = ("两套", "两个", "两种", "多套", "多个", "至少二", "至少2", "至少两个")
LIST_ITEM = re.compile(
    r"^\s*(?:[-*+]|\d+[.)、]|[一二三四五六七八九十]+[.)、）])\s*(.+?)\s*$"
)


def rule_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    list_item = False

    def flush() -> None:
        nonlocal current, list_item
        if current:
            blocks.append(" ".join(current))
        current = []
        list_item = False

    for line in text.splitlines() + [""]:
        item = LIST_ITEM.match(line)
        if item:
            flush()
            current = [item.group(1)]
            list_item = True
        elif not line.strip() or line.lstrip().startswith("#"):
            flush()
        elif list_item and line[:1].isspace():
            current.append(line.strip())
        elif list_item:
            flush()
            current = [line.strip()]
        else:
            current.append(line.strip())
    return blocks


def reference_load_rules(text: str, reference_name: str) -> list[str]:
    return [
        block
        for block in rule_blocks(text)
        if reference_name in block
        and any(cue in block for cue in ("读取", "加载", "查阅", "打开"))
        and not re.search(r"(?:不得|不要|无需|不应|禁止|避免|不能)(?:按需)?(?:读取|加载|查阅|打开)", block)
    ]


def reference_load_violations(text: str, reference_name: str) -> list[str]:
    violations: list[str] = []
    contradiction_cues = ("无论是否", "不论是否", "无论有无", "即使不存在", "即便不存在")
    for rule in reference_load_rules(text, reference_name):
        if any(cue in rule for cue in contradiction_cues):
            violations.append(rule)
            continue
        if not has_terms(
            rule,
            ("只有", "仅当", "若", "如果", "才"),
            ("重大", "高影响", "难逆", "难以逆转", "范式"),
            ("真实",),
            ("分叉",),
        ):
            violations.append(rule)
    return violations


def alternative_target_negated(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    target = "(?:方案|备选|候选|方向|选项|alternative)"
    negative = (
        "(?:不要求|不强制|无需|不得|不应|禁止|避免|不能|不提供|不列出|不比较|"
        "不要要求|不要强制).{0,20}" + target
    )
    return re.search(negative, compact, flags=re.IGNORECASE) is not None


def local_clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for line in text.splitlines()
        for clause in re.split(r"[；;。.!！？?]+", line)
        if clause.strip()
    ]


def affirmative_abandon_before_start(text: str) -> list[str]:
    violations: list[str] = []
    patterns = (
        r"先abandon.{0,16}(?:再|后)?.{0,8}start",
        r"abandon.{0,16}(?:后再|再|before|then).{0,8}start",
    )
    negations = ("不得", "不要", "禁止", "不能", "不可", "不应", "无需", "never", "mustnot")
    for clause in local_clauses(text):
        compact = re.sub(r"\s+", "", clause).lower()
        for pattern in patterns:
            for match in re.finditer(pattern, compact):
                prefix = compact[max(0, match.start() - 12) : match.start()]
                if any(negation in prefix for negation in negations):
                    continue
                violations.append(clause)
    return violations


def unconditional_alternative_requirements(text: str) -> list[str]:
    violations: list[str] = []
    for clause in local_clauses(text):
        compact = re.sub(r"\s+", "", clause).lower()
        if alternative_target_negated(clause):
            continue
        if (
            any(cue in compact for cue in UNIVERSAL_CUES)
            and any(cue in compact for cue in MANDATORY_CUES)
            and any(cue in compact for cue in ALTERNATIVE_CUES)
            and any(cue in compact for cue in MULTIPLE_CUES)
        ):
            violations.append(clause)
    for items, intro in _list_blocks(text):
        compact_intro = re.sub(r"\s+", "", intro)
        if alternative_target_negated(compact_intro):
            continue
        mandatory_for_all = any(cue in compact_intro for cue in UNIVERSAL_CUES) and any(
            cue in compact_intro for cue in MANDATORY_CUES
        )
        if not mandatory_for_all:
            continue
        for item in items:
            compact_item = re.sub(r"\s+", "", item)
            if alternative_target_negated(compact_item):
                continue
            if any(cue in compact_item for cue in ALTERNATIVE_CUES) and any(
                cue in compact_item for cue in MULTIPLE_CUES
            ):
                violations.append(f"{intro} -> {item}")
    return violations


def _list_blocks(text: str) -> list[tuple[list[str], str]]:
    lines = text.splitlines()
    blocks: list[tuple[list[str], str]] = []
    current: list[str] = []
    intro = ""
    for index, line in enumerate(lines + [""]):
        match = LIST_ITEM.match(line)
        if match:
            if not current:
                previous = lines[index - 1].strip() if 0 < index <= len(lines) else ""
                local_intro = re.split(r"[；;。.!！？?，,]+", previous)[-1].strip()
                if local_intro.endswith(("：", ":")) or re.search(
                    r"(?:如下|以下|遵守|包括|包含)$", local_intro
                ):
                    intro = local_intro
            current.append(match.group(1))
            continue
        if current:
            blocks.append((current, intro))
            current = []
            intro = ""
    return blocks


def fixed_seven_target_negated(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    target = r"(?:固定)?(?:七|7)(?:问|项标签|个问题|项问题|个标签|问问卷)"
    negative = (
        r"(?:不采用|不使用|不要求|不强制|不要采用|不要使用|不要要求|不要强制|"
        r"无需采用|无需使用|不得采用|不得使用|禁止采用|禁止使用).{0,12}" + target
    )
    return re.search(negative, compact) is not None


def fixed_seven_questionnaires(text: str) -> list[str]:
    violations: list[str] = []
    for clause in local_clauses(text):
        compact = re.sub(r"\s+", "", clause)
        if fixed_seven_target_negated(compact):
            continue
        fixed_count = re.search(
            r"固定(?:七|7)(?:问|项标签|个问题|项问题|个标签|问问卷)", compact
        )
        mandatory_count = re.search(
            r"(?:七|7)(?:问|项标签|个问题|项问题|个标签|问问卷)", compact
        ) and any(cue in compact for cue in MANDATORY_CUES)
        if fixed_count or mandatory_count:
            violations.append(clause)

    for items, intro in _list_blocks(text):
        if len(items) != 7:
            continue
        compact_intro = re.sub(r"\s+", "", intro)
        if fixed_seven_target_negated(compact_intro):
            continue
        mandatory_for_all = any(cue in compact_intro for cue in UNIVERSAL_CUES) and any(
            cue in compact_intro for cue in MANDATORY_CUES
        )
        question_like = all("?" in item or "？" in item for item in items)
        label_like = all(re.match(r"^[^：:]{1,16}[：:]", item) for item in items)
        if mandatory_for_all and (question_like or label_like):
            violations.append(intro)

    heading_numbers: list[tuple[int, int]] = []
    chinese_number = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^\s*#{1,6}\s*第\s*([一二三四五六七]|[1-7])\s*问", line)
        if match:
            raw = match.group(1)
            heading_numbers.append((index, chinese_number.get(raw, int(raw) if raw.isdigit() else 0)))
    for offset in range(max(0, len(heading_numbers) - 6)):
        window = heading_numbers[offset : offset + 7]
        if [number for _index, number in window] != list(range(1, 8)):
            continue
        first_index = window[0][0]
        previous = lines[first_index - 1].strip() if first_index > 0 else ""
        intro = re.split(r"[；;。.!！？?，,]+", previous)[-1].strip()
        compact_intro = re.sub(r"\s+", "", intro)
        if fixed_seven_target_negated(compact_intro):
            continue
        if any(cue in compact_intro for cue in UNIVERSAL_CUES) and any(
            cue in compact_intro for cue in MANDATORY_CUES
        ):
            violations.append(intro)
    return violations


def git_text(revision: str, path: Path) -> str:
    result = run_process(
        ["git", "show", f"{revision}:{path.as_posix()}"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


def git_blob(revision: str, path: Path) -> str:
    result = run_process(
        ["git", "rev-parse", f"{revision}:{path.as_posix()}"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def worktree_blob(path: Path) -> str:
    result = run_process(
        ["git", "hash-object", path.as_posix()],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def changed_paths() -> list[str]:
    result = run_process(
        ["git", "diff", "--name-only", SPEC_HEAD, DELIVERY_HEAD],
        cwd=ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return [line for line in result.stdout.splitlines() if line]


class PlanningDesignDisciplineTest(unittest.TestCase):
    def test_reverse_detector_rejects_unconditional_alternative_quotas(self) -> None:
        rejected = (
            "所有任务都必须提供两套同层方案。\n"
            "每个任务始终列出至少两个备选。\n"
            "所有任务必须遵守：\n"
            "- 至少提供两个备选方案\n"
        )
        accepted = (
            "不要要求所有任务提供两套方案。\n"
            "局部可逆任务无需备选；只有真实重大分叉才比较同层方案。\n"
            "只有存在真实重大分叉且有可信同层方案时：\n"
            "- 比较两个备选方案\n"
        )
        synonym_rejected = """所有任务必须遵守：
- 列出两个候选方向
"""
        stale_negation_does_not_apply = """局部任务不要比较方案。
所有任务必须遵守：
- 列出两个候选方向
"""
        conditional_candidates = """只有存在真实重大分叉且有可信同层候选时：
- 比较两个候选方向
"""
        common_scope_rejected = (
            "每个任务都要列出两个候选方向。\n"
            "每个计划均需提供两个备选方案。\n"
            "全部规划都要列出两个选项。\n"
            "任何任务都要列出两个候选方向。\n"
            "无论什么任务，一律提供两个备选方案。\n"
        )
        conditional_plan = (
            "每个计划只有存在真实重大分叉且有可信同层候选时，才比较两个候选方向。\n"
            "全部规划只有存在真实重大分叉时，才比较两个选项。\n"
            "任何任务只有存在真实重大分叉时，才比较两个候选方向。"
        )

        self.assertEqual(len(unconditional_alternative_requirements(rejected)), 3)
        self.assertTrue(unconditional_alternative_requirements(synonym_rejected))
        self.assertTrue(
            unconditional_alternative_requirements(stale_negation_does_not_apply)
        )
        self.assertEqual(
            len(unconditional_alternative_requirements(common_scope_rejected)), 5
        )
        self.assertEqual(unconditional_alternative_requirements(accepted), [])
        self.assertEqual(unconditional_alternative_requirements(conditional_candidates), [])
        self.assertEqual(unconditional_alternative_requirements(conditional_plan), [])

    def test_reverse_detector_rejects_fixed_seven_question_or_label_forms(self) -> None:
        questions = """所有任务必须依次回答：
1. 目标是什么？
2. 边界是什么？
3. 风险是什么？
4. 依赖是什么？
5. 接口是什么？
6. 验证是什么？
7. 回滚是什么？
"""
        labels = """每个任务必须填写：
- 目标：当前行为
- 边界：输入范围
- 风险：失败影响
- 依赖：外部系统
- 接口：公开入口
- 验证：执行命令
- 回滚：恢复方式
"""
        chinese_questions = """所有任务必须依次回答：
一、目标是什么？
二、边界是什么？
三、风险是什么？
四、依赖是什么？
五、接口是什么？
六、验证是什么？
七、回滚是什么？
"""
        heading_questions = """每个任务必须填写以下问卷：
### 第一问 目标是什么？
### 第二问 边界是什么？
### 第三问 风险是什么？
### 第四问 依赖是什么？
### 第五问 接口是什么？
### 第六问 验证是什么？
### 第七问 回滚是什么？
"""
        arabic_count = "所有计划采用固定 7 个问题。"
        non_target_negation = """所有任务必须遵守且不要遗漏：
1. 目标是什么？
2. 边界是什么？
3. 风险是什么？
4. 依赖是什么？
5. 接口是什么？
6. 验证是什么？
7. 回滚是什么？
"""
        legal_negations = """不采用固定 7 个问题。
不使用固定七问。
不要求固定七问问卷。
不强制固定七项标签。
"""
        mandatory_plan_questions = """每个计划都要逐项回答：
1. 目标是什么？
2. 边界是什么？
3. 风险是什么？
4. 依赖是什么？
5. 接口是什么？
6. 验证是什么？
7. 回滚是什么？
"""
        mandatory_no_omission = """所有任务不得遗漏以下问题：
1. 目标是什么？
2. 边界是什么？
3. 风险是什么？
4. 依赖是什么？
5. 接口是什么？
6. 验证是什么？
7. 回滚是什么？
"""
        any_task_questions = """任何任务都要逐项回答：
1. 目标是什么？
2. 边界是什么？
3. 风险是什么？
4. 依赖是什么？
5. 接口是什么？
6. 验证是什么？
7. 回滚是什么？
"""
        conditional_plan_questions = """每个计划只有存在范式级真实分叉时，才按需追问：
- 哪些同层候选可信？
- 哪项取舍需要用户选择？
"""
        conditional = """只有存在真实且重大的范式分叉时，才按需追问：
- 哪些同层方案可信？
- 哪项取舍需要用户选择？
不得采用固定 7 个问题。
"""

        self.assertTrue(fixed_seven_questionnaires(questions))
        self.assertTrue(fixed_seven_questionnaires(labels))
        self.assertTrue(fixed_seven_questionnaires(chinese_questions))
        self.assertTrue(fixed_seven_questionnaires(heading_questions))
        self.assertTrue(fixed_seven_questionnaires(arabic_count))
        self.assertTrue(fixed_seven_questionnaires(non_target_negation))
        self.assertTrue(fixed_seven_questionnaires(mandatory_plan_questions))
        self.assertTrue(fixed_seven_questionnaires(mandatory_no_omission))
        self.assertTrue(fixed_seven_questionnaires(any_task_questions))
        self.assertEqual(fixed_seven_questionnaires(legal_negations), [])
        self.assertEqual(fixed_seven_questionnaires(conditional_plan_questions), [])
        self.assertEqual(fixed_seven_questionnaires(conditional), [])

    def test_planner_conditionally_routes_only_real_major_choices(self) -> None:
        planner = read(PLANNER_PATH)
        reference = read(REFERENCE_PATH)
        reasonable_rules = """- 只有面对真实且重大、难逆或范式级设计分叉时，才按需读取 design-decisions.md。
- 局部可逆任务无需比较方案。
"""
        contradictory_mutation = reasonable_rules + (
            "- 高影响选择无论是否存在真实分叉都必须读取 design-decisions.md。\n"
        )

        self.assertEqual(
            reference_load_violations(reasonable_rules, REFERENCE_PATH.name), []
        )
        self.assertTrue(
            reference_load_violations(contradictory_mutation, REFERENCE_PATH.name)
        )

        self.assertTrue((ROOT / REFERENCE_PATH).is_file(), REFERENCE_PATH)
        self.assertEqual(planner.count(REFERENCE_PATH.name), 1)
        self.assertTrue(
            reference_load_rules(planner, REFERENCE_PATH.name),
            "Planner 必须包含读取设计取舍 reference 的局部规则",
        )
        self.assertEqual(
            reference_load_violations(planner, REFERENCE_PATH.name),
            [],
            "每条 reference 加载动作都必须在同一局部规则中绑定条件化的真实重大设计分叉",
        )
        self.assertTrue(
            matching_paragraphs(
                reference,
                ("同层",),
                ("真实", "可信", "可行"),
                ("比较", "对比"),
            ),
            "只有真实可行的同层方案才进入比较",
        )
        self.assertTrue(
            matching_paragraphs(
                reference,
                ("局部",),
                ("可逆",),
                ("无需", "不比较", "不提供", "直接"),
            ),
            "局部可逆工作不得被强制比较方案",
        )
        self.assertTrue(
            matching_paragraphs(
                reference,
                ("范式",),
                ("用户",),
                ("选择", "决定", "确认"),
            ),
            "范式级分叉必须交给用户选择",
        )
        self.assertEqual(unconditional_alternative_requirements(planner + reference), [])
        self.assertEqual(fixed_seven_questionnaires(planner + reference), [])

    def test_reference_preserves_only_one_known_verifiable_migration_seam(self) -> None:
        reference = read(REFERENCE_PATH)

        self.assertTrue(
            matching_paragraphs(
                reference,
                ("已知", "明确"),
                ("演进", "下一阶段", "下一步"),
                ("一个", "一条", "单一", "至多一"),
                ("最小",),
                ("可验证",),
                ("迁移", "兼容"),
            ),
            "只有已知演进压力可保留一个最小、可验证的迁移通道",
        )
        self.assertTrue(
            matching_paragraphs(
                reference,
                ("未知未来", "未知需求", "假想未来", "可能有一天"),
                ("不得", "不要", "不应", "不新增", "不预留"),
                ("抽象", "模块", "接口", "扩展点"),
            ),
            "未知未来不得制造抽象或扩展点",
        )
        self.assertTrue(
            matching_paragraphs(
                reference,
                ("模块",),
                ("接口",),
                ("依赖",),
                ("扩展点",),
                ("行为",),
                ("演进压力",),
                ("映射", "对应", "追溯"),
            ),
            "每个新结构都必须映射到冻结行为或已知演进压力",
        )

    def test_reviewer_phase_zero_audits_minimal_sufficient_design(self) -> None:
        reviewer = read(REVIEWER_PATH)
        match = re.search(r"Phase 0.*?(?=Phase C)", reviewer, flags=re.DOTALL)
        self.assertIsNotNone(match, "Reviewer 必须保留 Phase 0")
        phase_zero = match.group(0) if match else ""

        self.assertTrue(has_terms(phase_zero, ("最小充分",)))
        for concept in ("模块", "接口", "依赖", "扩展点"):
            self.assertIn(concept, phase_zero)
        self.assertTrue(has_terms(phase_zero, ("具体",), ("成本", "偏离")))
        self.assertTrue(has_terms(phase_zero, ("风格偏好",), ("不得", "不要", "不是")))
        self.assertNotIn(REFERENCE_PATH.name, reviewer)

    def test_role_prompts_stay_within_frozen_line_budgets(self) -> None:
        planner_baseline = git_text(SPEC_HEAD, PLANNER_PATH).splitlines()
        reviewer_baseline = git_text(SPEC_HEAD, REVIEWER_PATH).splitlines()
        tester_baseline = git_text(SPEC_HEAD, TESTER_PATH).splitlines()
        variants = json.loads(read(BUILDER_VARIANTS_PATH))
        builder_current = [
            item for item in variants["variants"] if item["id"] == "builder-current"
        ]
        planner_current = [
            item for item in variants["variants"] if item["id"] == "planner-current"
        ]
        reviewer_current = [
            item for item in variants["variants"] if item["id"] == "reviewer-current"
        ]

        self.assertEqual(git_blob("HEAD", PLANNER_PATH), worktree_blob(PLANNER_PATH))
        self.assertLessEqual(
            len(read(PLANNER_PATH).splitlines()),
            len(planner_baseline) + PLANNER_LINE_ALLOWANCE,
        )
        self.assertEqual(len(builder_current), 1, builder_current)
        self.assertEqual(
            builder_current[0]["instruction_source"]["path"], str(BUILDER_PATH)
        )
        self.assertEqual(
            builder_current[0]["instruction_source"]["sha256"],
            hashlib.sha256((ROOT / BUILDER_PATH).read_bytes()).hexdigest(),
        )
        self.assertEqual(git_blob("HEAD", BUILDER_PATH), worktree_blob(BUILDER_PATH))
        self.assertEqual(len(planner_current), 1, planner_current)
        self.assertEqual(
            planner_current[0]["instruction_source"]["path"], str(PLANNER_PATH)
        )
        self.assertEqual(
            planner_current[0]["instruction_source"]["sha256"],
            hashlib.sha256((ROOT / PLANNER_PATH).read_bytes()).hexdigest(),
        )
        self.assertEqual(len(reviewer_current), 1, reviewer_current)
        self.assertEqual(
            reviewer_current[0]["instruction_source"]["path"], str(REVIEWER_PATH)
        )
        self.assertEqual(
            reviewer_current[0]["instruction_source"]["sha256"],
            hashlib.sha256((ROOT / REVIEWER_PATH).read_bytes()).hexdigest(),
        )
        self.assertEqual(git_blob("HEAD", REVIEWER_PATH), REVIEWER_BLOB)
        self.assertEqual(worktree_blob(REVIEWER_PATH), REVIEWER_BLOB)
        self.assertLessEqual(
            len(read(BUILDER_PATH).splitlines()),
            BUILDER_LINE_LIMIT + BUILDER_MAINTENANCE_LINE_ALLOWANCE,
        )
        self.assertLessEqual(len(read(REVIEWER_PATH).splitlines()), len(reviewer_baseline) + 28)
        self.assertLessEqual(len(read(TESTER_PATH).splitlines()), len(tester_baseline) + 24)

    def test_planner_revision_guidance_uses_one_ledger_derived_lineage_view(self) -> None:
        planner = read(PLANNER_PATH)
        retrospective = read(Path("skills/builder/references/post-delivery-retrospective.md"))

        self.assertTrue(
            has_terms(
                planner,
                ("lineage", "沿革", "修订链"),
                ("status", "driver-context"),
                ("摘要", "health", "健康"),
            ),
            "Planner must consume the runtime lineage view instead of reconstructing revisions",
        )
        self.assertTrue(
            has_terms(
                planner,
                ("变化", "changed", "delta"),
                ("语义", "semantic"),
                ("prior", "问题"),
                ("pressure", "压力", "架构审查"),
            ),
            "higher revisions ask only for changed semantics, problem dispositions, and pressure decisions",
        )
        self.assertTrue(
            has_terms(
                retrospective,
                ("lineage", "沿革", "修订链"),
                ("status", "ledger"),
                ("pressure", "压力", "重复原因"),
            ),
            "retrospective must reuse the same structured lineage pressure",
        )
        self.assertFalse(
            has_terms(retrospective, ("transcript", "对话"), ("重建", "推断")),
            "retrospective must not reconstruct lineage from transcripts",
        )

    def test_planner_closes_role_manifest_authority_and_uses_plain_language(self) -> None:
        planner = read(PLANNER_PATH)

        self.assertTrue(
            has_terms(
                planner,
                ("instruction source", "角色"),
                ("manifest",),
                ("直接引用",),
                ("Builder写边界",),
            ),
            "Planner must freeze versioned verification manifests before implementation",
        )
        self.assertTrue(
            has_terms(
                planner,
                ("re-export", "聚合"),
                ("checker", "校验"),
                ("精确",),
                ("Builder写边界",),
                ("只读",),
            ),
            "Planner must close repository-declared re-export authority before implementation",
        )
        self.assertTrue(
            has_terms(
                planner,
                ("目录", "glob", "通配"),
                ("不得", "不"),
                ("扩大", "授权"),
            ),
            "Planner must not replace exact authority closure with broad path grants",
        )
        self.assertTrue(
            has_terms(
                planner,
                ("request_user_input",),
                ("用户能观察",),
                ("行为",),
                ("成本",),
                ("退路",),
            ),
            "Planner choices must lead with user-observable behavior and cost",
        )
        for internal_term in ("carryover", "turn", "supersession", "actionable finding"):
            self.assertIn(internal_term, planner)
        self.assertTrue(has_terms(planner, ("内部词",), ("不能单独",)))

    def test_v4_supersession_guidance_is_active_first_and_version_scoped(self) -> None:
        self.assertTrue(
            affirmative_abandon_before_start(
                "Assurance v4 有 successor 时先 abandon，再 start。"
            )
        )
        self.assertEqual(
            affirmative_abandon_before_start(
                "Assurance v4 有 successor 时不得先 abandon，再 start。"
            ),
            [],
        )
        scoped_example = (
            "## v4 lifecycle\n\n"
            "Use update-facet in the same active run.\n\n"
            "## legacy lifecycle\n\n"
            "Use update-facet in the same active run.\n"
        )
        self.assertTrue(
            heading_scoped_paragraphs(
                scoped_example,
                V4_LIFECYCLE_TERMS,
                ("update-facet", "revise-mission"),
                ("同一", "same"),
                ("active",),
            )
        )
        self.assertEqual(
            heading_scoped_paragraphs(
                scoped_example,
                ("v5",),
                ("update-facet", "revise-mission"),
                ("同一", "same"),
                ("active",),
            ),
            [],
        )
        sources = {
            "project rules": read(AGENTS_PATH),
            "Planner": read(PLANNER_PATH),
            "Full Driver": read(FULL_DRIVER_PATH),
            "Reviewer": read(REVIEWER_PATH),
            "architecture": read(ARCHITECTURE_PATH),
        }

        for label, text in sources.items():
            lifecycle = matching_paragraphs(
                text,
                V4_LIFECYCLE_TERMS,
                ("successor", "后继", "新run", "更高revision"),
                ("active",),
                ("start",),
                ("superseded",),
                ("abandon",),
            )
            self.assertTrue(
                lifecycle,
                f"{label} must describe the v4 active-to-superseded transaction",
            )
            self.assertTrue(
                any(
                    has_terms(
                        block,
                        ("用户取消", "取消"),
                        ("没有successor", "无successor", "没有后继", "没有承接"),
                    )
                    for block in lifecycle
                ),
                f"{label} must reserve abandon for cancellation without a successor",
            )
            same_run_guidance = matching_paragraphs(
                text,
                V4_LIFECYCLE_TERMS,
                ("update-facet", "revise-mission"),
                ("同一", "same"),
                ("active",),
            )
            same_run_guidance.extend(
                heading_scoped_paragraphs(
                    text,
                    V4_LIFECYCLE_TERMS,
                    ("update-facet", "revise-mission"),
                    ("同一", "same"),
                    ("active",),
                )
            )
            self.assertTrue(
                same_run_guidance,
                f"{label} must converge expressible decisions in the same active v4 run",
            )
            self.assertEqual(
                [
                    clause
                    for block in lifecycle
                    for clause in affirmative_abandon_before_start(block)
                ],
                [],
                f"{label} still recommends abandon before v4 start",
            )

        for label in ("project rules", "Planner", "Full Driver", "architecture"):
            self.assertTrue(
                matching_paragraphs(
                    sources[label],
                    ("执行信息", "execution"),
                    ("普通", "常规", "非语义"),
                    ("不得", "不应", "不能", "无需", "不以", "不触发", "不升级"),
                    ("abandon", "新run", "revision"),
                ),
                f"{label} must not escalate ordinary execution information to abandon/new run",
            )

        for label in ("Planner", "Full Driver"):
            self.assertTrue(
                matching_paragraphs(
                    sources[label],
                    ("terminal", "abandoned", "superseded", "finalized"),
                    ("continuity", "连续性"),
                    ("不可恢复", "不能恢复", "不恢复"),
                ),
                f"{label} must state that terminal source continuity cannot be restored",
            )

    def test_v4_guidance_preserves_legacy_abandoned_source_contract(self) -> None:
        sources = {
            "project rules": read(AGENTS_PATH),
            "Planner": read(PLANNER_PATH),
            "Full Driver": read(FULL_DRIVER_PATH),
            "Reviewer": read(REVIEWER_PATH),
            "architecture": read(ARCHITECTURE_PATH),
        }

        for label, text in sources.items():
            self.assertTrue(
                matching_paragraphs(
                    text,
                    V4_LIFECYCLE_TERMS,
                    ("legacy", "v2", "v3"),
                    ("abandon",),
                    ("successor", "revision", "supersed"),
                    ("保持", "既有", "不由", "继续", "仍按"),
                ),
                f"{label} must preserve the legacy abandoned-source revision contract",
            )

    def test_reviewer_v4_scenario_mechanically_rejects_abandon_first(self) -> None:
        scenarios = json.loads(read(SCENARIOS_PATH))
        matches = [
            item
            for item in scenarios["scenarios"]
            if item["id"] == "reviewer-v4-active-supersession"
        ]
        self.assertEqual(len(matches), 1, matches)
        scenario = matches[0]
        self.assertEqual(scenario["role"], "reviewer")
        self.assertEqual(scenario["trigger_type"], "trigger")
        combined = scenario["prompt"] + "\n" + "\n".join(
            scenario["semantic_criteria"]
        )
        self.assertTrue(
            has_terms(
                combined,
                ("assurancev4", "v4"),
                ("successor", "后继"),
                ("active",),
                ("start",),
                ("superseded",),
                ("abandon",),
                ("continuity", "连续性"),
            )
        )
        checks = scenario["mechanical_checks"]
        self.assertIn("不得先 abandon", checks["contains"])
        self.assertIn("先 abandon 再 start", checks["not_contains"])

        accepted_response = "；".join(checks["contains"])
        accepted = run_process(
            [
                "python3",
                BEHAVIOR_RUNNER_PATH,
                "score",
                "--scenario-id",
                scenario["id"],
                "--variant-id",
                "reviewer-current",
            ],
            cwd=ROOT,
            input_text=accepted_response,
        )
        self.assertEqual(
            accepted.returncode, 0, (accepted.stdout, accepted.stderr)
        )
        accepted_score = json.loads(accepted.stdout.splitlines()[-1])
        self.assertTrue(accepted_score["mechanical_pass"], accepted_score)

        rejected = run_process(
            [
                "python3",
                BEHAVIOR_RUNNER_PATH,
                "score",
                "--scenario-id",
                scenario["id"],
                "--variant-id",
                "reviewer-current",
            ],
            cwd=ROOT,
            input_text=accepted_response + "；先 abandon 再 start",
        )
        self.assertEqual(
            rejected.returncode, 0, (rejected.stdout, rejected.stderr)
        )
        rejected_score = json.loads(rejected.stdout.splitlines()[-1])
        self.assertFalse(rejected_score["mechanical_pass"], rejected_score)
        self.assertEqual(rejected_score["false_triggers"], ["先 abandon 再 start"])

    def test_reference_is_not_duplicated_into_always_loaded_roles_or_docs(self) -> None:
        reference = read(REFERENCE_PATH)
        planner = read(PLANNER_PATH)
        reviewer = read(REVIEWER_PATH)
        philosophy = read(PHILOSOPHY_PATH)
        architecture = read(ARCHITECTURE_PATH)

        self.assertEqual(planner.count(REFERENCE_PATH.name), 1)
        self.assertNotIn(REFERENCE_PATH.name, reviewer + philosophy + architecture)

        operational_lines = {
            line
            for line in normalized_lines(reference)
            if len(line) >= 36 and re.match(r"^(?:[-*+]|\d+[.)、]|\|)", line)
        }
        copied = operational_lines.intersection(normalized_lines(philosophy + architecture))
        self.assertEqual(copied, set(), f"docs copied operational checklist lines: {copied}")

    def test_entrypoint_and_delivery_layer_invariants_remain_explicit(self) -> None:
        planner = read(PLANNER_PATH)
        docs = read(PHILOSOPHY_PATH) + read(ARCHITECTURE_PATH)

        self.assertIn("/plan", planner)
        self.assertIn("$builder", planner)
        self.assertLess(planner.index("/plan"), planner.rindex("$builder"))
        self.assertTrue(has_terms(docs, ("契约层", "契约"), ("方法论", "开发方法")))
        self.assertTrue(has_terms(docs, ("最小充分",), ("planner",), ("reviewer",)))
        self.assertTrue(has_terms(docs, ("已知",), ("演进",), ("迁移", "兼容")))

    def test_candidate_changes_stay_inside_frozen_delivery_surface(self) -> None:
        allowed_exact = {
            "AGENTS.md",
            "CHANGELOG.md",
            "README.md",
            "agents/AGENTS.md.block",
            REVIEWER_PATH.as_posix(),
            TESTER_PATH.as_posix(),
            PHILOSOPHY_PATH.as_posix(),
            ARCHITECTURE_PATH.as_posix(),
            "schema/codex-loop-ledger.schema.json",
            "schema/codex-test-proof.schema.json",
        }
        allowed_prefixes = (
            "experiments/agent-behavior/",
            "runtime/codex_builder_loop/",
            "skills/builder-loop-planner/",
            "skills/builder/",
            "tests/",
        )
        unexpected = [
            path
            for path in changed_paths()
            if path not in allowed_exact
            and not any(path.startswith(prefix) for prefix in allowed_prefixes)
        ]

        self.assertEqual(unexpected, [])


if __name__ == "__main__":
    unittest.main()
