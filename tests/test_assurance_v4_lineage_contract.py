from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness import CLI, cleanup_repo, init_repo, run_process


NON_SEMANTIC = "tester_correction"
SEMANTIC = "mission_change"


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def base_contract() -> dict[str, Any]:
    return {
        "schema_version": 4,
        "mission": {
            "delivery_kind": "code",
            "revision": 1,
            "supersedes": None,
            "objective": "Deliver a lineage-aware calculator.",
            "behaviors": [
                {"id": "add-values", "description": "Addition returns the sum."}
            ],
            "interfaces": [
                {"id": "calc-api", "description": "src.calc.add(a, b) returns a number."}
            ],
            "acceptance_cases": [
                {"id": "add-positive", "description": "add(1, 2) returns 3."}
            ],
            "trust_boundaries": [
                {
                    "id": "ledger-truth",
                    "description": "Persisted ledgers are the lineage source of truth.",
                }
            ],
        },
        "authority": {
            "target_branch": "main",
            "builder_write": ["src/**"],
            "tester_write": ["tests/**"],
            "dirty_intake": [],
            "public_prerequisites": [],
            "protected_support_paths": [],
            "external_targets": [],
        },
        "assurance": {
            "required": ["machine"],
            "machine_commands": [
                {
                    "id": "fixture-tests",
                    "argv": ["bash", "verify.sh"],
                    "timeout_seconds": 30,
                    "expected_returncodes": [0],
                }
            ],
        },
        "execution": {
            "version": 1,
            "candidate_head": None,
            "builder_files": [],
            "tester_files": [],
            "tester_source": None,
            "driver_enforced": False,
            "continuation": None,
            "carryover": None,
            "deployment": None,
            "dirty_snapshot": [],
            "commands": [],
            "agents": {},
        },
    }


class AssuranceV4LineageContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.tempdir = tempfile.TemporaryDirectory(prefix="assurance-v4-lineage-")
        self.artifacts = Path(self.tempdir.name)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.tempdir.cleanup()

    def invoke(self, command: str, *args: str | Path) -> tuple[int, dict[str, Any]]:
        completed = run_process(
            [
                "python3",
                CLI,
                "assurance",
                "--experimental-v4",
                command,
                *args,
            ]
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, (completed.returncode, completed.stdout, completed.stderr))
        value = json.loads(lines[-1])
        self.assertIsInstance(value, dict)
        return completed.returncode, value

    def write_json(self, name: str, value: Any) -> Path:
        path = self.artifacts / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def start(self, run_id: str, contract: dict[str, Any]) -> dict[str, Any]:
        path = self.write_json(f"{run_id}.json", contract)
        rc, value = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            run_id,
            "--session-id",
            f"session-{run_id}",
            "--contract",
            path,
        )
        self.assertEqual(rc, 0, value)
        return value

    def status(self, run_id: str) -> dict[str, Any]:
        rc, value = self.invoke("status", "--repo", self.repo, "--run", run_id)
        self.assertEqual(rc, 0, value)
        return value

    def next_contract(
        self,
        source: dict[str, Any],
        *,
        category: str,
        decision_digest: str | None = None,
        prior_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        lineage = source["lineage"]
        candidate = run_process(
            ["git", "-C", source["candidate_worktree"], "rev-parse", "HEAD"]
        )
        self.assertEqual(candidate.returncode, 0, candidate.stderr)
        candidate_head = candidate.stdout.strip()
        contract = base_contract()
        contract["mission"]["revision"] = source["mission_revision"] + 1
        contract["mission"]["supersedes"] = {
            "run_id": source["run_id"],
            "revision": source["mission_revision"],
            "mission_digest": source["digests"]["mission"],
            "candidate_head": candidate_head,
        }
        transition: dict[str, Any] = {
            "category": category,
            "predecessor_pressure_digest": lineage["pressure_digest"],
        }
        if decision_digest is not None:
            transition["architecture_review"] = {
                "decision": "continue",
                "pressure_digest": decision_digest,
            }
        contract["execution"]["revision_transition"] = transition
        contract["execution"]["prior_problem_dispositions"] = {
            "source_snapshot_digest": lineage["open_problem_snapshot_digest"],
            "items": prior_items or [],
        }
        return contract

    def test_revision_one_without_extension_fields_reports_complete_lineage(self) -> None:
        contract = base_contract()
        rc, validated = self.invoke(
            "validate", "--contract", self.write_json("revision-one.json", contract)
        )
        self.assertEqual(rc, 0, validated)

        started = self.start("lineage-r1", contract)
        lineage = started["lineage"]
        self.assertEqual(lineage["schema_version"], 1)
        self.assertTrue(lineage["complete"])
        self.assertEqual(lineage["health"], "healthy")
        self.assertEqual(lineage["root_run_id"], "lineage-r1")
        self.assertEqual(lineage["current_run_id"], "lineage-r1")
        self.assertEqual(lineage["revision_count"], 1)
        self.assertEqual(lineage["transitions"], [])
        self.assertEqual(lineage["non_semantic_transition_count"], 0)

        context_rc, context = self.invoke(
            "driver-context", "--repo", self.repo, "--run", "lineage-r1"
        )
        self.assertEqual(context_rc, 0, context)
        self.assertEqual(context["lineage"], lineage)

    def test_superseding_runs_aggregate_source_ledger_telemetry(self) -> None:
        first = self.start("aggregate-r1", base_contract())
        self.assertEqual(self.status("aggregate-r1")["phase"], "active")
        source_candidate = run_process(
            ["git", "-C", first["candidate_worktree"], "rev-parse", "HEAD"]
        ).stdout.strip()
        second = self.start(
            "aggregate-r2", self.next_contract(first, category="resource_parameter")
        )

        source_after = self.status("aggregate-r1")
        self.assertEqual(source_after["phase"], "superseded")
        self.assertEqual(
            source_after["supersede_intent"],
            {
                "source_run_id": "aggregate-r1",
                "target_run_id": "aggregate-r2",
                "state": "received",
            },
        )
        context_rc, context = self.invoke(
            "driver-context", "--repo", self.repo, "--run", "aggregate-r2"
        )
        self.assertEqual(context_rc, 0, context)
        execution = context["facets"]["execution"]
        self.assertEqual(
            execution["carryover"]["source_run_id"], "aggregate-r1"
        )
        self.assertEqual(
            execution["carryover"]["source_candidate_head"],
            source_candidate,
        )
        self.assertEqual(execution["candidate_head"], source_candidate)
        self.assertEqual(execution["agents"], {})
        self.assertIsNone(execution["tester_source"])
        self.assertEqual(context["evidence"], {})

        lineage = second["lineage"]
        self.assertEqual(lineage["root_run_id"], "aggregate-r1")
        self.assertEqual(lineage["current_run_id"], "aggregate-r2")
        self.assertEqual(lineage["revision_count"], 2)
        self.assertEqual([item["category"] for item in lineage["transitions"]], ["resource_parameter"])
        self.assertEqual(lineage["non_semantic_transition_count"], 1)
        self.assertEqual(lineage["transition_category_counts"], {"resource_parameter": 1})
        for field in (
            "elapsed_ms",
            "candidate_changes",
            "evidence_attempts",
            "evidence_replays",
            "retries",
            "stages",
        ):
            self.assertIn(field, lineage["cumulative_telemetry"])
        self.assertGreaterEqual(lineage["cumulative_telemetry"]["elapsed_ms"], 0)
        self.assertIsInstance(lineage["cumulative_telemetry"]["stages"], list)

    def test_third_nonsemantic_transition_stops_before_any_mutation(self) -> None:
        first = self.start("pressure-r1", base_contract())
        second = self.start(
            "pressure-r2", self.next_contract(first, category="resource_parameter")
        )
        third = self.start(
            "pressure-r3", self.next_contract(second, category="target_drift")
        )
        proposed = self.next_contract(third, category=NON_SEMANTIC)
        source_head = run_process(
            ["git", "-C", third["candidate_worktree"], "rev-parse", "HEAD"]
        ).stdout.strip()

        rc, rejected = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "pressure-r4",
            "--session-id",
            "session-pressure-r4",
            "--contract",
            self.write_json("pressure-r4.json", proposed),
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected["status"], "NEEDS_USER")
        self.assertEqual(rejected["code"], "LINEAGE_ARCHITECTURE_REVIEW_REQUIRED")
        branch = run_process(
            [
                "git",
                "-C",
                self.repo,
                "show-ref",
                "--verify",
                "refs/heads/assurance-v4/pressure-r4/candidate",
            ]
        )
        self.assertNotEqual(branch.returncode, 0)
        current_head = run_process(
            ["git", "-C", third["candidate_worktree"], "rev-parse", "HEAD"]
        ).stdout.strip()
        self.assertEqual(current_head, source_head)
        self.assertIsNone(self.status("pressure-r3")["supersede_intent"])

    def test_review_decision_is_valid_only_for_exact_pressure_digest(self) -> None:
        first = self.start("review-r1", base_contract())
        second = self.start(
            "review-r2", self.next_contract(first, category="resource_parameter")
        )
        third = self.start(
            "review-r3", self.next_contract(second, category="target_drift")
        )
        digest = third["lineage"]["pressure_digest"]

        stale = self.next_contract(third, category=NON_SEMANTIC, decision_digest="0" * 64)
        rc, rejected = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "review-stale",
            "--session-id",
            "session-review-stale",
            "--contract",
            self.write_json("review-stale.json", stale),
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected["code"], "LINEAGE_ARCHITECTURE_REVIEW_REQUIRED")

        fourth = self.start(
            "review-r4",
            self.next_contract(third, category=NON_SEMANTIC, decision_digest=digest),
        )
        self.assertEqual(fourth["lineage"]["non_semantic_transition_count"], 3)

        later = self.next_contract(fourth, category="role_continuity", decision_digest=digest)
        rc, rejected_later = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "review-r5",
            "--session-id",
            "session-review-r5",
            "--contract",
            self.write_json("review-r5.json", later),
        )
        self.assertEqual(rc, 1, rejected_later)
        self.assertEqual(
            rejected_later["code"], "LINEAGE_ARCHITECTURE_REVIEW_REQUIRED"
        )

    def test_mission_change_is_visible_but_excluded_from_pressure(self) -> None:
        first = self.start("semantic-r1", base_contract())
        contract = self.next_contract(first, category=SEMANTIC)
        contract["mission"]["objective"] = "Deliver subtraction as an explicit user-requested change."
        second = self.start("semantic-r2", contract)

        lineage = second["lineage"]
        self.assertEqual(lineage["revision_count"], 2)
        self.assertEqual(lineage["transitions"][0]["category"], SEMANTIC)
        self.assertEqual(lineage["transition_category_counts"][SEMANTIC], 1)
        self.assertEqual(lineage["non_semantic_transition_count"], 0)

    def test_prior_problem_dispositions_are_complete_and_survive_by_intent(self) -> None:
        first = self.start("problems-r1", base_contract())
        report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "builder-fix-needed",
                    "summary": "Builder fix remains required",
                    "details": "The candidate still lacks an accepted behavior.",
                    "owner": "builder",
                },
                {
                    "key": "handled-externally",
                    "summary": "External tracking exists",
                    "details": "The issue has an authoritative external record.",
                    "owner": "current_project",
                },
                {
                    "key": "discarded-finding",
                    "summary": "Finding was invalid",
                    "details": "The observation does not apply to the frozen target.",
                    "owner": "tester",
                },
            ],
        }
        rc, recorded = self.invoke(
            "record-problems",
            "--repo",
            self.repo,
            "--run",
            "problems-r1",
            "--report",
            self.write_json("problems.json", report),
            "--role",
            "tester",
            "--agent-id",
            "tester-agent",
            "--thread-id",
            "tester-thread",
        )
        self.assertEqual(rc, 0, recorded)
        open_by_key = {
            item["key"]: item
            for item in recorded["problems"]
            if item["status"] == "open"
        }

        omitted = self.next_contract(recorded, category="execution_contract")
        rc, omission = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "problems-omitted",
            "--session-id",
            "session-problems-omitted",
            "--contract",
            self.write_json("problems-omitted.json", omitted),
        )
        self.assertEqual(rc, 1, omission)
        self.assertEqual(omission["code"], "PRIOR_PROBLEM_DISPOSITIONS_INCOMPLETE")

        items = [
            {
                "key": open_by_key["builder-fix-needed"]["key"],
                "disposition": "included",
            },
            {
                "key": open_by_key["handled-externally"]["key"],
                "disposition": "handled_elsewhere",
            },
            {
                "key": open_by_key["discarded-finding"]["key"],
                "disposition": "discarded",
            },
        ]
        second = self.start(
            "problems-r2",
            self.next_contract(recorded, category="execution_contract", prior_items=items),
        )
        self.assertEqual(second["lineage"]["problem_disposition_counts"], {
            "included": 1,
            "handled_elsewhere": 1,
            "discarded": 1,
        })
        self.assertEqual(second["lineage"]["open_problem_keys"], ["builder-fix-needed"])

        included = open_by_key["builder-fix-needed"]
        third = self.start(
            "problems-r3",
            self.next_contract(
                second,
                category="role_continuity",
                prior_items=[
                    {
                        "key": included["key"],
                        "disposition": "included",
                    }
                ],
            ),
        )
        self.assertEqual(third["lineage"]["open_problem_keys"], ["builder-fix-needed"])

    def test_supersession_never_inherits_role_or_evidence_state(self) -> None:
        first = self.start("isolation-r1", base_contract())
        second = self.start(
            "isolation-r2", self.next_contract(first, category="role_continuity")
        )
        context_rc, context = self.invoke(
            "driver-context", "--repo", self.repo, "--run", "isolation-r2"
        )
        self.assertEqual(context_rc, 0, context)
        execution = context["facets"]["execution"]
        self.assertEqual(execution["agents"], {})
        self.assertIsNone(execution["tester_source"])
        self.assertEqual(execution["tester_files"], [])
        self.assertEqual(context["evidence"], {})
        self.assertEqual(second["lineage"]["revision_count"], 2)

    def test_snapshot_drift_and_duplicate_dispositions_fail_before_mutation(self) -> None:
        first = self.start("invalid-r1", base_contract())
        stale = self.next_contract(first, category="execution_contract")
        stale["execution"]["prior_problem_dispositions"]["source_snapshot_digest"] = "f" * 64
        rc, rejected_stale = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "invalid-stale-r2",
            "--session-id",
            "session-invalid-stale-r2",
            "--contract",
            self.write_json("invalid-stale-r2.json", stale),
        )
        self.assertNotEqual(rc, 0, rejected_stale)
        self.assertEqual(rejected_stale["code"], "PRIOR_PROBLEM_SNAPSHOT_MISMATCH")

        duplicate = self.next_contract(first, category="execution_contract")
        duplicate["execution"]["prior_problem_dispositions"]["items"] = [
            {
                "key": "duplicate-problem",
                "disposition": "discarded",
            },
            {
                "key": "duplicate-problem",
                "disposition": "discarded",
            },
        ]
        rc, rejected_duplicate = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "invalid-duplicate-r2",
            "--session-id",
            "session-invalid-duplicate-r2",
            "--contract",
            self.write_json("invalid-duplicate-r2.json", duplicate),
        )
        self.assertNotEqual(rc, 0, rejected_duplicate)
        self.assertEqual(rejected_duplicate["code"], "ASSURANCE_CONTRACT_INVALID")
        self.assertIsNone(self.status("invalid-r1")["supersede_intent"])

    def test_retained_ledger_without_transition_metadata_is_honestly_incomplete(self) -> None:
        first = self.start("legacy-r1", base_contract())
        second = self.start(
            "legacy-r2", self.next_contract(first, category="execution_contract")
        )
        ledger_path = Path(second["candidate_worktree"]).parent / "ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["facets"]["execution"].pop("revision_transition")
        ledger["facets"]["execution"].pop("prior_problem_dispositions")
        ledger["digests"]["execution"] = canonical_digest(ledger["facets"]["execution"])
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")

        retained = self.status("legacy-r2")
        self.assertFalse(retained["lineage"]["complete"])
        self.assertEqual(retained["lineage"]["health"], "incomplete")
        self.assertEqual(retained["lineage"]["revision_count"], 2)
        self.assertEqual(retained["phase"], "active")

        proposed = self.next_contract(retained, category="execution_contract")
        rc, rejected = self.invoke(
            "start",
            "--repo",
            self.repo,
            "--run",
            "legacy-r3",
            "--session-id",
            "session-legacy-r3",
            "--contract",
            self.write_json("legacy-r3.json", proposed),
        )
        self.assertEqual(rc, 1, rejected)
        self.assertEqual(rejected["code"], "LINEAGE_ARCHITECTURE_REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
