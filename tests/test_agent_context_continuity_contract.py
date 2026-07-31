from __future__ import annotations

import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

from harness import ROOT, run_process


BUILDER_SKILL = ROOT / "skills" / "full-driver-v4-experiment" / "SKILL.md"
REVIEWER_AGENT = ROOT / "agents" / "reviewer.toml"
SCENARIOS = ROOT / "experiments" / "agent-behavior" / "scenarios.json"
VARIANTS = ROOT / "experiments" / "agent-behavior" / "variants.json"
BEHAVIOR_RUNNER = ROOT / "experiments" / "agent-behavior" / "runner.py"
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"
SHARED_PARENT_CONTEXT_MARKERS = (
    "父线程讨论",
    "用户倾向",
    "builder 辩护",
)
TESTER_PARENT_CONTEXT_MARKERS = (
    *SHARED_PARENT_CONTEXT_MARKERS,
    "候选信息",
)
REVIEWER_PARENT_CONTEXT_MARKERS = SHARED_PARENT_CONTEXT_MARKERS


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def compact(text: str) -> str:
    value = re.sub(r"\s*[/／]\s*", "/", text.lower())
    return re.sub(r"\s+", " ", value).strip()


def bounded(text: str, start: str, end: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise AssertionError(f"missing contract boundary: {start!r}")
    end_index = text.find(end, start_index + len(start))
    if end_index < 0:
        raise AssertionError(f"missing contract boundary: {end!r}")
    return text[start_index:end_index]


def window(text: str, marker: str, radius: int = 500) -> str:
    normalized = compact(text)
    marker_value = compact(marker)
    index = normalized.find(marker_value)
    if index < 0:
        raise AssertionError(f"missing contract marker: {marker!r}")
    return normalized[max(0, index - radius) : index + len(marker_value) + radius]


def last_window(text: str, marker: str, radius: int = 500) -> str:
    normalized = compact(text)
    marker_value = compact(marker)
    index = normalized.rfind(marker_value)
    if index < 0:
        raise AssertionError(f"missing terminal contract marker: {marker!r}")
    return normalized[max(0, index - radius) : index + len(marker_value) + radius]


def has_negated_clause(text: str, marker: str) -> bool:
    for clause in re.split(r"[。；;\n]", compact(text)):
        if marker in clause and any(
            negation in clause for negation in ("不得", "禁止", "永不", "不能", "不建议")
        ):
            return True
    return False


def forbids_context_reset(text: str) -> bool:
    for clause in re.split(r"[。；;\n]", compact(text)):
        negation = r"(?:不得|禁止|永不|不能|不应|不可|不)"
        reset = r"(?:清空|重置)"
        protected = r"(?:上下文|角色历史)"
        if re.search(
            rf"(?:{negation}[^。；]{{0,40}}{reset}[^。；]{{0,30}}{protected}|"
            rf"{protected}[^。；]{{0,30}}{negation}[^。；]{{0,20}}{reset})",
            clause,
        ):
            return True
    return False


def replace_all(text: str, pattern: str, replacement: str) -> str:
    value, count = re.subn(pattern, replacement, text, flags=re.IGNORECASE)
    if count < 1:
        raise AssertionError(f"mutation target is missing: {pattern!r}")
    return value


def weaken_context_reset_clause(text: str) -> str:
    normalized = compact(text)
    for clause in re.split(r"[。；;\n]", normalized):
        if not forbids_context_reset(clause):
            continue
        weakened = clause.replace("上下文", "未声明对象").replace(
            "角色历史", "未声明对象"
        )
        return normalized.replace(clause, weakened, 1)
    raise AssertionError("missing context-preservation clause for mutation")


def weaken_negated_marker_clause(text: str, marker: str) -> str:
    normalized = compact(text)
    marker_value = compact(marker)
    changed = False
    for clause in re.split(r"[。；;\n]", normalized):
        if marker_value not in clause:
            continue
        if not any(
            negation in clause
            for negation in ("不得", "禁止", "永不", "不能", "不建议")
        ):
            continue
        weakened = clause.replace(marker_value, "已删除约束")
        normalized = normalized.replace(clause, weakened, 1)
        changed = True
    if not changed:
        raise AssertionError(f"missing negated clause for mutation: {marker!r}")
    return normalized


def initial_spawn_violations(
    text: str,
    *,
    role: str,
    frozen_inputs: tuple[str, ...],
    spawn_once_pattern: str,
    forbidden_context_markers: tuple[str, ...],
) -> list[str]:
    normalized = compact(text)
    violations: list[str] = []
    if re.search(rf"agent_type\s*[:=]\s*[\"']{role}[\"']", normalized) is None:
        violations.append("custom-agent-type")
    if re.search(r"fork_turns\s*[:=]\s*[\"']none[\"']", normalized) is None:
        violations.append("fork-turns-none")
    if "最小" not in normalized or "brief" not in normalized:
        violations.append("minimal-brief")
    violations.extend(
        f"missing-input:{item}" for item in frozen_inputs if item not in normalized
    )
    if re.search(spawn_once_pattern, normalized) is None:
        violations.append("single-initial-spawn")
    violations.extend(
        f"parent-context:{marker}"
        for marker in forbidden_context_markers
        if not has_negated_clause(normalized, marker)
    )
    if role == "reviewer" and has_negated_clause(normalized, "候选信息"):
        violations.append("required-candidate-prohibited")
    return violations


def follow_up_violations(text: str, *, role: str) -> list[str]:
    normalized = compact(text)
    violations: list[str] = []
    if "followup_task" not in normalized:
        violations.append("followup-task")
    same_thread = (
        "follow-up 同一个 tester thread"
        if role == "tester"
        else "follow-up 同一 reviewer thread"
    )
    if same_thread not in normalized:
        violations.append("same-thread")
    no_respawn = "禁止 spawn 新 tester" if role == "tester" else "不新建 reviewer"
    if no_respawn not in normalized:
        violations.append("no-respawn")
    if not forbids_context_reset(normalized):
        violations.append("no-context-reset")
    return violations


class BuilderConversationIsolationContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = read_text(BUILDER_SKILL)
        author = bounded(
            cls.skill,
            "## 角色隔离与连续性",
            "## 原生持续循环",
        )
        cls.tester_initial = bounded(author, "### Tester 初次 spawn", "### Reviewer 初次 spawn")
        cls.reviewer_initial = bounded(author, "### Reviewer 初次 spawn", "### 后续 turn")
        cls.tester_follow_up = bounded(author, "Tester 后续阶段", "Reviewer finding")
        cls.reviewer_follow_up = bounded(author, "Reviewer finding", "只有 Core")

    def test_initial_tester_spawn_is_isolated_with_a_minimal_role_brief(self) -> None:
        normalized = compact(self.tester_initial)
        frozen_inputs = (
            "run_id",
            "contract",
            "tester worktree",
            "spec head",
            "publication manifest",
        )
        self.assertRegex(
            normalized,
            r"agent_type\s*[:=]\s*[\"']tester[\"']",
            "initial Tester must select the tester custom-agent type explicitly",
        )
        self.assertRegex(
            normalized,
            r"fork_turns\s*[:=]\s*[\"']none[\"']",
            "initial Tester must not inherit the parent conversation",
        )
        self.assertIn("最小", normalized)
        self.assertIn("brief", normalized)
        self.assertEqual(
            [item for item in frozen_inputs if item not in normalized],
            [],
            "initial Tester brief is missing frozen role inputs",
        )
        self.assertRegex(normalized, r"一个 run 只 spawn 一次 tester")
        for marker in TESTER_PARENT_CONTEXT_MARKERS:
            self.assertTrue(
                has_negated_clause(normalized, marker),
                f"initial Tester brief may carry parent context: {marker}",
            )
        self.assertEqual(
            initial_spawn_violations(
                self.tester_initial,
                role="tester",
                frozen_inputs=frozen_inputs,
                spawn_once_pattern=r"一个 run 只 spawn 一次 tester",
                forbidden_context_markers=TESTER_PARENT_CONTEXT_MARKERS,
            ),
            [],
            "initial Tester spawn contract is incomplete",
        )

    def test_initial_reviewer_spawn_is_isolated_with_a_minimal_role_brief(self) -> None:
        normalized = compact(self.reviewer_initial)
        frozen_inputs = (
            "contract",
            "candidate",
            "完整 diff",
            "验证证据",
            "文档政策路径",
        )
        self.assertRegex(
            normalized,
            r"agent_type\s*[:=]\s*[\"']reviewer[\"']",
            "initial Reviewer must select the reviewer custom-agent type explicitly",
        )
        self.assertRegex(
            normalized,
            r"fork_turns\s*[:=]\s*[\"']none[\"']",
            "initial Reviewer must not inherit the parent conversation",
        )
        self.assertIn("最小", normalized)
        self.assertIn("brief", normalized)
        self.assertEqual(
            [item for item in frozen_inputs if item not in normalized],
            [],
            "initial Reviewer brief is missing frozen role inputs",
        )
        self.assertRegex(normalized, r"spawn 一次 reviewer")
        for marker in REVIEWER_PARENT_CONTEXT_MARKERS:
            self.assertTrue(
                has_negated_clause(normalized, marker),
                f"initial Reviewer brief may carry parent context: {marker}",
            )
        self.assertIn("candidate", normalized)
        self.assertIn("完整 diff", normalized)
        self.assertFalse(
            has_negated_clause(normalized, "候选信息"),
            "Reviewer candidate and full diff are required review inputs",
        )
        self.assertEqual(
            initial_spawn_violations(
                self.reviewer_initial,
                role="reviewer",
                frozen_inputs=frozen_inputs,
                spawn_once_pattern=r"spawn 一次 reviewer",
                forbidden_context_markers=REVIEWER_PARENT_CONTEXT_MARKERS,
            ),
            [],
            "initial Reviewer spawn contract is incomplete",
        )

    def test_later_role_turns_follow_up_the_bound_original_threads(self) -> None:
        tester_follow_up = compact(self.tester_follow_up)
        self.assertIn("followup_task", tester_follow_up)
        self.assertIn("follow-up 同一个 tester thread", tester_follow_up)
        self.assertIn("禁止 spawn 新 tester", tester_follow_up)
        self.assertTrue(
            forbids_context_reset(tester_follow_up),
            "Tester follow-up must not clear its role context or history",
        )
        self.assertEqual(
            follow_up_violations(self.tester_follow_up, role="tester"),
            [],
            "Tester author to blackbox must preserve its original context",
        )

        reviewer_follow_up = compact(self.reviewer_follow_up)
        self.assertIn("followup_task", reviewer_follow_up)
        self.assertIn("follow-up 同一 reviewer thread", reviewer_follow_up)
        self.assertIn("不新建 reviewer", reviewer_follow_up)
        self.assertTrue(
            forbids_context_reset(reviewer_follow_up),
            "Reviewer rereview must not clear its role context or history",
        )
        self.assertEqual(
            follow_up_violations(self.reviewer_follow_up, role="reviewer"),
            [],
            "Reviewer rereview must preserve its original context",
        )

    def test_initial_role_detector_rejects_each_weakened_spawn_contract(self) -> None:
        role_contracts = (
            (
                "tester",
                self.tester_initial,
                (
                    "run_id",
                    "contract",
                    "tester worktree",
                    "spec head",
                    "publication manifest",
                ),
                r"一个 run 只 spawn 一次 tester",
                TESTER_PARENT_CONTEXT_MARKERS,
            ),
            (
                "reviewer",
                self.reviewer_initial,
                ("contract", "candidate", "完整 diff", "验证证据", "文档政策路径"),
                r"spawn 一次 reviewer",
                REVIEWER_PARENT_CONTEXT_MARKERS,
            ),
        )
        mismatches: list[str] = []
        for (
            role,
            contract,
            frozen_inputs,
            spawn_once_pattern,
            forbidden_context_markers,
        ) in role_contracts:
            detector = lambda value: initial_spawn_violations(
                value,
                role=role,
                frozen_inputs=frozen_inputs,
                spawn_once_pattern=spawn_once_pattern,
                forbidden_context_markers=forbidden_context_markers,
            )
            self.assertEqual(detector(contract), [], f"{role} positive contract")
            mutations = [
                (
                    f"{role}:agent-type",
                    "custom-agent-type",
                    replace_all(
                        contract,
                        rf"agent_type\s*[:=]\s*[\"']{role}[\"']",
                        'agent_type="default"',
                    ),
                ),
                (
                    f"{role}:fork-turns",
                    "fork-turns-none",
                    replace_all(
                        contract,
                        r"fork_turns\s*[:=]\s*[\"']none[\"']",
                        'fork_turns="all"',
                    ),
                ),
                (
                    f"{role}:minimal-brief",
                    "minimal-brief",
                    replace_all(contract, r"最小", "宽泛"),
                ),
                *[
                    (
                        f"{role}:frozen-input:{item}",
                        f"missing-input:{item}",
                        replace_all(contract, re.escape(item), "已删除输入"),
                    )
                    for item in frozen_inputs
                ],
                (
                    f"{role}:single-initial-spawn",
                    "single-initial-spawn",
                    replace_all(contract, spawn_once_pattern, "允许多次 spawn"),
                ),
                *[
                    (
                        f"{role}:parent-context:{marker}",
                        f"parent-context:{marker}",
                        weaken_negated_marker_clause(contract, marker),
                    )
                    for marker in forbidden_context_markers
                ],
            ]
            if role == "reviewer":
                mutations.append(
                    (
                        "reviewer:required-candidate-prohibited",
                        "required-candidate-prohibited",
                        contract + "。不得夹带候选信息。",
                    )
                )
            for name, expected, weakened in mutations:
                actual = detector(weakened)
                if expected not in actual:
                    mismatches.append(f"{name}: expected={expected} actual={actual}")
        self.assertEqual(
            mismatches,
            [],
            "each weakened initial-role constraint must report its exact violation",
        )

    def test_follow_up_detector_rejects_respawn_or_context_reset(self) -> None:
        role_contracts = (
            (
                "tester",
                self.tester_follow_up,
                r"禁止 spawn 新 tester",
                "允许 spawn 新 tester",
            ),
            (
                "reviewer",
                self.reviewer_follow_up,
                r"不新建 reviewer",
                "允许新建 reviewer",
            ),
        )
        mismatches: list[str] = []
        for role, contract, respawn_pattern, replacement in role_contracts:
            detector = lambda value: follow_up_violations(value, role=role)
            self.assertEqual(detector(contract), [], f"{role} positive follow-up")
            same_thread = (
                "follow-up 同一个 tester thread"
                if role == "tester"
                else "follow-up 同一 reviewer thread"
            )
            mutations = (
                (
                    f"{role}:followup-task",
                    "followup-task",
                    replace_all(contract, r"followup_task", "省略续接工具"),
                ),
                (
                    f"{role}:same-thread",
                    "same-thread",
                    replace_all(contract, re.escape(same_thread), "follow-up 新 thread"),
                ),
                (
                    f"{role}:respawn",
                    "no-respawn",
                    replace_all(contract, respawn_pattern, replacement),
                ),
                (
                    f"{role}:context-reset",
                    "no-context-reset",
                    weaken_context_reset_clause(contract),
                ),
            )
            for name, expected, weakened in mutations:
                actual = detector(weakened)
                if expected not in actual:
                    mismatches.append(f"{name}: expected={expected} actual={actual}")
        self.assertEqual(
            mismatches,
            [],
            "each weakened follow-up constraint must report its exact violation",
        )


class ReviewerBlockedContinuityContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.instructions = read_text(REVIEWER_AGENT)

    def test_repairable_blocker_returns_same_thread_rereview_conditions(self) -> None:
        continuity = window(self.instructions, "同一 Reviewer thread", radius=650)
        markers = (
            "冻结目标",
            "ownership",
            "验收标准",
            "continuity",
            "未变化",
            "缺口",
            "复审条件",
        )
        self.assertEqual(
            [marker for marker in markers if marker.lower() not in continuity],
            [],
            "repairable blocked output is missing same-thread rereview conditions",
        )
        self.assertTrue(
            "可补齐" in continuity or "可修复" in continuity,
            "same-thread rereview is only for a repairable prerequisite or evidence gap",
        )

    def test_fresh_reviewer_is_forbidden_and_invalid_continuity_stops_safely(self) -> None:
        normalized = compact(self.instructions)
        self.assertTrue(
            has_negated_clause(normalized, "fresh reviewer"),
            "Reviewer instructions must explicitly reject a fresh Reviewer",
        )
        self.assertTrue(
            has_negated_clause(normalized, "replacement reviewer"),
            "Reviewer instructions must explicitly reject a replacement Reviewer",
        )

        blocked_terminal = last_window(
            normalized,
            "REVIEW_RESULT: blocked",
            radius=1100,
        )
        self.assertIn("abandon", blocked_terminal)
        self.assertIn("new plan", blocked_terminal)
        self.assertIn("new run", blocked_terminal)
        self.assertIn("continuity failure", blocked_terminal)
        self.assertTrue(
            "停止" in blocked_terminal or "blocked" in blocked_terminal,
            blocked_terminal,
        )
        self.assertIn("保留现场", blocked_terminal)
        self.assertIn("只读 reviewer", normalized)
        self.assertIn("不要修改文件", normalized)


class ReviewerBehaviorLabContractTest(unittest.TestCase):
    def test_reviewer_blocked_scenario_uses_current_instruction_digest(self) -> None:
        scenarios = json.loads(read_text(SCENARIOS))["scenarios"]
        candidates = [
            scenario
            for scenario in scenarios
            if scenario.get("role") == "reviewer"
            and "fresh reviewer" in compact(str(scenario.get("prompt", "")))
        ]
        self.assertEqual(
            len(candidates),
            1,
            "behavior lab must contain one Reviewer blocked-continuity scenario",
        )
        scenario = candidates[0]
        checks = scenario.get("mechanical_checks", {})
        contains = checks.get("contains", [])
        excludes = checks.get("not_contains", [])
        compliant_markers = (
            "blocked",
            "同一 Reviewer thread",
            "复审条件",
            "不得建议 fresh Reviewer",
            "不得新建 reviewer",
        )
        self.assertEqual(
            [marker for marker in compliant_markers if marker not in contains],
            [],
            "Reviewer scenario must recognize the complete compliant blocked answer",
        )
        forbidden_recommendations = (
            "请启动 fresh Reviewer",
            "启动 replacement Reviewer",
        )
        self.assertEqual(
            [marker for marker in forbidden_recommendations if marker not in excludes],
            [],
            "Reviewer scenario must reject explicit replacement recommendations",
        )
        semantics = compact(" ".join(scenario.get("semantic_criteria", [])))
        semantic_markers = ("冻结目标", "ownership", "验收标准", "continuity")
        self.assertEqual(
            [marker for marker in semantic_markers if marker.lower() not in semantics],
            [],
            "Reviewer scenario is missing frozen-continuity semantics",
        )

        variants = json.loads(read_text(VARIANTS))["variants"]
        matching = [item for item in variants if item.get("id") == "reviewer-current"]
        self.assertEqual(len(matching), 1)
        variant = matching[0]
        self.assertEqual(variant.get("kind"), "instruction")
        self.assertEqual(variant.get("roles"), ["reviewer"])
        source = variant.get("instruction_source", {})
        self.assertEqual(source.get("path"), "agents/reviewer.toml")
        self.assertEqual(source.get("revision"), "WORKTREE")
        expected_digest = hashlib.sha256(REVIEWER_AGENT.read_bytes()).hexdigest()
        self.assertEqual(source.get("sha256"), expected_digest)

        prepared = run_process(
            [
                sys.executable,
                BEHAVIOR_RUNNER,
                "prepare",
                "--scenario-id",
                str(scenario["id"]),
                "--variant-id",
                "reviewer-current",
            ],
            cwd=ROOT,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        payload = json.loads(prepared.stdout)
        self.assertEqual(payload["scenario_id"], scenario["id"])
        self.assertEqual(payload["variant_id"], "reviewer-current")
        self.assertEqual(
            payload["request"]["instruction_source"]["sha256"], expected_digest
        )

        def score(response: str) -> dict:
            completed = run_process(
                [
                    sys.executable,
                    BEHAVIOR_RUNNER,
                    "score",
                    "--scenario-id",
                    str(scenario["id"]),
                    "--variant-id",
                    "reviewer-current",
                    "--response-file",
                    "-",
                ],
                cwd=ROOT,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
                input_text=response,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)

        compliant_response = "；".join(compliant_markers)
        compliant_score = score(compliant_response)
        self.assertTrue(compliant_score["mechanical_pass"], compliant_score)
        self.assertEqual(compliant_score["false_triggers"], [])

        for recommendation in forbidden_recommendations:
            violating_score = score(f"{compliant_response}；{recommendation}")
            self.assertFalse(violating_score["mechanical_pass"], violating_score)
            self.assertIn(recommendation, violating_score["false_triggers"])


class IsolationBoundaryDocumentationContractTest(unittest.TestCase):
    def assert_four_layers(self, path: Path) -> None:
        text = compact(read_text(path))
        layers = (
            "conversation",
            "git/artifact",
            "filesystem acl",
            "platform attestation",
        )
        self.assertEqual(
            [layer for layer in layers if layer not in text],
            [],
            f"{path}: isolation boundary layers are incomplete",
        )

        conversation = window(text, "conversation", radius=420)
        self.assertRegex(
            conversation,
            r"fork_turns\s*[:=]\s*[\"']none[\"']",
        )
        self.assertIn("最小", conversation)
        self.assertIn("brief", conversation)
        self.assertIn("同一", conversation)
        self.assertIn("thread", conversation)

        git_artifact = window(text, "git/artifact", radius=420)
        self.assertIn("runtime", git_artifact)
        self.assertTrue(
            any(
                marker in git_artifact
                for marker in ("worktree", "manifest", "ledger", "evidence")
            ),
            git_artifact,
        )

        for unsupported in ("filesystem acl", "platform attestation"):
            boundary = window(text, unsupported, radius=260)
            self.assertTrue(
                any(
                    marker in boundary
                    for marker in ("不提供", "不承诺", "未提供", "不能证明", "不是")
                ),
                f"{path}: {unsupported} must be described as outside the guarantee",
            )

    def test_readme_states_the_user_visible_isolation_boundary(self) -> None:
        self.assert_four_layers(README)

    def test_architecture_separates_conversation_git_filesystem_and_platform(self) -> None:
        self.assert_four_layers(ARCHITECTURE)
        attestation = window(read_text(ARCHITECTURE), "platform attestation", radius=420)
        self.assertTrue(
            "context manifest" in attestation or "上下文清单" in attestation,
            attestation,
        )


if __name__ == "__main__":
    unittest.main()
