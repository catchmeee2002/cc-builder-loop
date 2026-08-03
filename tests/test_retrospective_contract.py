from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER_SKILL = (
    ROOT / "skills" / "full-driver-v4-experiment" / "SKILL.md"
).read_text()
ENTRY_SKILL = (ROOT / "skills" / "builder" / "SKILL.md").read_text()
RETROSPECTIVE = (
    ROOT / "skills" / "builder" / "references" / "post-delivery-retrospective.md"
).read_text()
VARIANTS = json.loads(
    (ROOT / "experiments" / "agent-behavior" / "variants.json").read_text()
)


class RetrospectiveContractTest(unittest.TestCase):
    def test_builder_delegates_memory_screening_without_copying_old_scoring(self) -> None:
        self.assertIn("post-delivery-retrospective.md", BUILDER_SKILL)
        self.assertIn("$memory-review", BUILDER_SKILL)
        self.assertIn("builder-loop delegated", BUILDER_SKILL)
        self.assertNotIn("①源码不直观", BUILDER_SKILL + RETROSPECTIVE)
        self.assertNotIn("≥4/5", BUILDER_SKILL + RETROSPECTIVE)
        self.assertIn("不得复制旧版五问", BUILDER_SKILL)

    def test_incidents_have_one_owner_and_cross_boundary_chains_split(self) -> None:
        for owner in ("current_project", "builder_loop", "external_platform"):
            self.assertIn(f"`{owner}`", RETROSPECTIVE)
        self.assertIn("`both` 不是合法 owner", RETROSPECTIVE)
        self.assertIn("拆成两个原子事故", RETROSPECTIVE)
        self.assertIn("修复其中一条不能作为", RETROSPECTIVE)

    def test_issue_contract_is_fact_only_versioned_and_user_authorized(self) -> None:
        for field in (
            "归属与版本",
            "触发场景",
            "现场过程",
            "观察到的现象",
            "已确认事实",
            "根因状态",
            "复现条件",
            "runtime_identity.adapter_commit",
        ):
            self.assertIn(field, RETROSPECTIVE)
        self.assertIn("禁止写建议、修复方向、设计方案", RETROSPECTIVE)
        self.assertIn("request_user_input", RETROSPECTIVE)
        self.assertIn("已有同类 issue 时优先追加", RETROSPECTIVE)
        self.assertIn("finalized target 不得被复盘静默改脏", RETROSPECTIVE)

    def test_every_terminal_outcome_enters_one_retrospective_before_result_marker(self) -> None:
        for terminal in (
            "FINALIZED",
            "NEEDS_USER",
            "FATAL",
            "continuity failure",
            "abandon",
        ):
            self.assertIn(terminal, ENTRY_SKILL)
        reference = "post-delivery-retrospective.md"
        self.assertEqual(ENTRY_SKILL.count(reference), 1)
        reference_index = ENTRY_SKILL.index(reference)
        result_index = ENTRY_SKILL.index("FULL_DRIVER_V4_RESULT")
        self.assertLess(reference_index, result_index)
        compact = " ".join(ENTRY_SKILL.split())
        self.assertTrue(
            any(marker in compact for marker in ("保留", "不得改写")), compact
        )
        self.assertTrue(
            any(marker in compact for marker in ("失败事实", "原始事实")), compact
        )

    def test_retrospective_signals_cover_replay_recovery_invalidation_and_revision_pressure(self) -> None:
        semantic_tokens = {
            "action replay": ("action_id", "重复"),
            "manual ledger recovery": ("人工", "ledger", "recovery"),
            "manual evidence invalidation": ("手工", "evidence", "invalidation"),
            "revision pressure": ("revision", "数量", "原因"),
            "no-signal outcome": ("no-op",),
            "live inputs": ("实时", "ledger", "turn", "HEAD", "快照"),
            "stable-document boundary": ("稳定", "Markdown"),
            "deduplicated authorized routing": ("查重", "request_user_input"),
        }
        for signal, tokens in semantic_tokens.items():
            with self.subTest(signal=signal):
                self.assertFalse(
                    [token for token in tokens if token not in RETROSPECTIVE],
                    RETROSPECTIVE,
                )
        self.assertTrue(
            any(
                marker in RETROSPECTIVE
                for marker in ("不写入", "不得写入", "禁止写入")
            ),
            RETROSPECTIVE,
        )

    def test_terminal_retrospective_is_an_explicit_builder_behavior_fixture(self) -> None:
        variants = VARIANTS.get("variants", [])
        builder_variants = [
            item
            for item in variants
            if (item.get("instruction_source") or {}).get("path")
            == "skills/builder/SKILL.md"
        ]
        self.assertEqual(len(builder_variants), 1, variants)
        self.assertEqual(builder_variants[0].get("roles"), ["builder"])
        self.assertEqual(builder_variants[0].get("kind"), "instruction")
        self.assertIn("post-delivery-retrospective.md", ENTRY_SKILL)


if __name__ == "__main__":
    unittest.main()
