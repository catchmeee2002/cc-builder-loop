from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from harness import (
    cleanup_repo,
    head,
    init_repo,
    l1_plan_markdown,
    load_ledger,
    plan_markdown,
    start_run,
    write_plan,
)
from runtime.codex_builder_loop.assurance_v4.models import validate_lineage, validate_telemetry


ROOT = Path(__file__).resolve().parents[1]
LEDGER_SCHEMA = json.loads((ROOT / "schema" / "codex-loop-ledger.schema.json").read_text())


class LedgerSchemaContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []
        Draft202012Validator.check_schema(LEDGER_SCHEMA)
        self.validator = Draft202012Validator(LEDGER_SCHEMA, format_checker=FormatChecker())

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def validate_started_plan(self, plan_text: str, *, task: str) -> dict:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_text)
        _started, run_path = start_run(repo, plan, task=task)
        ledger = load_ledger(run_path)
        self.validator.validate(ledger)
        return ledger

    def test_l2_ledger_matches_published_schema(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan, task="schema-l2")
        ledger = load_ledger(run_path)
        self.validator.validate(ledger)
        self.assertEqual(ledger["schema_version"], 2)
        self.assertEqual(ledger["runtime_identity"]["adapter"], "codex")
        self.assertRegex(
            ledger["runtime_identity"]["adapter_commit"], r"^[0-9a-f]{40}$"
        )
        self.assertIn(
            ledger["runtime_identity"]["capture_status"], {"captured", "partial"}
        )
        self.assertEqual(started.data["runtime_identity"], ledger["runtime_identity"])
        self.assertIn("evidence", ledger)
        self.assertIn("workspace_intake", ledger)
        self.assertNotIn("verified_head", ledger)
        self.assertNotIn("verification_attempts", ledger)

    def test_l1_ledger_matches_published_schema(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, l1_plan_markdown(head(repo)))
        _started, run_path = start_run(repo, plan, task="schema-l1")
        ledger = load_ledger(run_path)
        self.validator.validate(ledger)
        self.assertEqual(ledger["plan"]["builder_write"], ["README.md"])

    def test_telemetry_profile_and_duration_breakdown_are_schema_bound(self) -> None:
        value = {
            "schema_version": 1,
            "elapsed_ms": 12,
            "active_stage": "machine",
            "stages": [
                {
                    "name": "machine",
                    "attempts": 1,
                    "completed_attempts": 1,
                    "failed_attempts": 0,
                    "retry_count": 0,
                    "total_duration_ms": 12,
                    "last_failure_code": None,
                }
            ],
            "candidate_changes": 0,
            "evidence_attempts": {"machine": 1},
            "evidence_replays": 0,
            "retries": {"total": 0, "by_failure_code": {}},
            "profile": {
                "requested": "compact",
                "effective": "compact",
                "escalation_reason": None,
            },
            "duration_breakdown": {
                "implementation_ms": 0,
                "verification_ms": 12,
                "orchestration_ms": 0,
                "waiting_ms": 0,
            },
        }
        self.assertEqual(validate_telemetry(value), value)

    def test_lineage_schema_keeps_task_pressure_and_cost_ancestry_derived(self) -> None:
        stage = {
            "name": "machine",
            "attempts": 0,
            "completed_attempts": 0,
            "failed_attempts": 0,
            "retry_count": 0,
            "total_duration_ms": 0,
            "last_failure_code": None,
        }
        cumulative = {
            "elapsed_ms": 0,
            "stages": [stage],
            "candidate_changes": 0,
            "evidence_attempts": {},
            "evidence_replays": 0,
            "retries": {"total": 0, "by_failure_code": {}},
        }
        value = {
            "schema_version": 1,
            "root_run_id": "root-run",
            "current_run_id": "root-run",
            "complete": True,
            "health": "healthy",
            "revision_count": 1,
            "transition_count": 0,
            "transitions": [],
            "non_semantic_transition_count": 0,
            "transition_category_counts": {},
            "cumulative_telemetry": cumulative,
            "task_root_run_id": "root-run",
            "cost_ancestry": [],
            "task_revision_count": 1,
            "task_transition_count": 0,
            "task_non_semantic_transition_count": 0,
            "task_transition_category_counts": {},
            "task_cumulative_telemetry": cumulative,
            "task_pressure_digest": "a" * 64,
            "continuation_grant": None,
            "problem_disposition_counts": {
                "included": 0,
                "handled_elsewhere": 0,
                "discarded": 0,
            },
            "open_problem_snapshot_digest": "b" * 64,
            "open_problem_keys": [],
            "lineage_digest": "c" * 64,
            "pressure_digest": "d" * 64,
        }
        self.assertEqual(validate_lineage(value), value)


if __name__ == "__main__":
    unittest.main()
