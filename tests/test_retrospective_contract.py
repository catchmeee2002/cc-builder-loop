from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


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
ARCHITECTURE = (ROOT / "docs" / "architecture.md").read_text()
RETROSPECTIVE_SCHEMA = json.loads(
    (ROOT / "schema" / "assurance-v4-retrospective.schema.json").read_text()
)


class RetrospectiveContractTest(unittest.TestCase):
    def test_retrospective_schema_validates_public_snapshot_report_and_status_shapes(
        self,
    ) -> None:
        Draft202012Validator.check_schema(RETROSPECTIVE_SCHEMA)
        validator = Draft202012Validator(RETROSPECTIVE_SCHEMA)
        signal_id = "recorded-problem-0123456789abcdef"
        snapshot = {
            "schema_version": 1,
            "repo_root": "/tmp/retrospective-schema-fixture",
            "owner_session_id": "retrospective-schema-session",
            "runs": [
                {
                    "run_id": "retrospective-schema-run",
                    "phase": "abandoned",
                    "terminal_status": "abandoned",
                    "root_run_id": "retrospective-schema-run",
                    "mission_revision": 1,
                    "ledger_digest": "a" * 64,
                    "runtime_identity": {
                        "adapter": "codex",
                        "adapter_commit": "b" * 40,
                        "adapter_dirty": False,
                        "capture_status": "captured",
                    },
                    "problem_count": 1,
                    "event_count": 2,
                }
            ],
            "signals": [
                {
                    "signal_id": signal_id,
                    "kind": "recorded-problem",
                    "severity": "mandatory",
                    "run_ids": ["retrospective-schema-run"],
                    "summary": "One recorded problem requires routing.",
                    "facts": {"problem_key": "schema-fixture-problem"},
                }
            ],
            "snapshot_digest": "c" * 64,
        }
        report_input = {
            "schema_version": 1,
            "snapshot_digest": snapshot["snapshot_digest"],
            "dispositions": [
                {
                    "signal_id": signal_id,
                    "disposition": "issue",
                    "owner": "builder_loop",
                    "reference": "https://example.invalid/issues/1",
                }
            ],
        }
        stored_report = {
            **report_input,
            "repo_root": snapshot["repo_root"],
            "owner_session_id": snapshot["owner_session_id"],
            "report_digest": "d" * 64,
            "recorded_at": "2026-08-04T00:00:00+00:00",
        }
        status = {
            "status": "READY",
            "owner_session_id": snapshot["owner_session_id"],
            "snapshot": snapshot,
            "report": stored_report,
            "required_block": (
                "Canonical summary\n"
                f"BUILDER_RETROSPECTIVE_READY:{snapshot['snapshot_digest']}:"
                f"{stored_report['report_digest']}"
            ),
            "required_user_block": (
                "Builder-loop retrospective complete.\n"
                "Runs: 1; Signals: 1; Issue routes: 1.\n"
                f"Report: {stored_report['report_digest']}\n"
                f"BUILDER_RETROSPECTIVE_READY:{snapshot['snapshot_digest']}:"
                f"{stored_report['report_digest']}"
            ),
        }
        for value in (snapshot, report_input, stored_report, status):
            with self.subTest(value=value):
                validator.validate(value)
        self.assertIn(
            "required_user_block",
            RETROSPECTIVE_SCHEMA["$defs"]["status"]["properties"],
        )

        invalid_advisory = {
            "schema_version": 1,
            "snapshot_digest": "e" * 64,
            "dispositions": [
                {
                    "signal_id": "revision-pressure-fedcba9876543210",
                    "disposition": "not-incident",
                    "reason": "   ",
                }
            ],
        }
        self.assertFalse(validator.is_valid(invalid_advisory))

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

    def test_terminal_gate_and_external_recovery_are_shipped_as_public_contracts(
        self,
    ) -> None:
        shipped = "\n".join(
            (ENTRY_SKILL, BUILDER_SKILL, RETROSPECTIVE, ARCHITECTURE)
        )
        for token in (
            "retrospective-status",
            "record-retrospective",
            "required_user_block",
            "BUILDER_INPUT_REQUIRED",
            "BUILDER_RETROSPECTIVE_READY",
            "resolve-external-problem",
            "--consumer-source",
            "consumer_source",
            "full_driver_skill",
            "operator_recovery",
        ):
            with self.subTest(token=token):
                self.assertIn(token, shipped)
        self.assertIn("request_user_input", shipped)
        self.assertIn("external_platform", shipped)

        ordered_sources = [
            source
            for source in (ENTRY_SKILL, BUILDER_SKILL, RETROSPECTIVE, ARCHITECTURE)
            if "FULL_DRIVER_V4_RESULT" in source
            and "BUILDER_RETROSPECTIVE_READY" in source
        ]
        self.assertTrue(ordered_sources, shipped)
        self.assertTrue(
            any(
                source.index("BUILDER_RETROSPECTIVE_READY")
                < source.index("FULL_DRIVER_V4_RESULT")
                for source in ordered_sources
            ),
            ordered_sources,
        )


if __name__ == "__main__":
    unittest.main()
