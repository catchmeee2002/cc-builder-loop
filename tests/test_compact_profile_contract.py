from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from harness import cleanup_repo, commit_all, init_repo
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.store import read_ledger


def compact_contract() -> dict:
    return {
        "schema_version": 4,
        "mission": {
            "delivery_kind": "code",
            "revision": 1,
            "supersedes": None,
            "objective": "Deliver a compact calculator correction.",
            "behaviors": [
                {"id": "add-values", "description": "Addition returns the sum."}
            ],
            "interfaces": [
                {"id": "calculator-api", "description": "The public calculator API."}
            ],
            "acceptance_cases": [
                {
                    "id": "add-positive",
                    "description": "The public calculator returns 3 for 1 plus 2.",
                    "observation": {
                        "surface_id": "compact-cli",
                        "surface_description": "The isolated public calculator surface.",
                        "execution_ids": ["compact-blackbox"],
                        "required_dimensions": ["mechanical", "verify", "quality"],
                    },
                }
            ],
            "trust_boundaries": [
                {
                    "id": "independent-gates",
                    "description": "Compact does not remove independent assurance gates.",
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
            "profile": "compact",
            "required": ["tester", "proof", "machine", "blackbox", "reviewer"],
            "machine_commands": [
                {
                    "id": "compact-machine",
                    "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                    "expected_returncodes": [0],
                    "timeout_seconds": 30,
                }
            ],
            "preflight_before_proof": False,
            "reviewer_preflight": False,
        },
        "execution": {
            "version": 1,
            "driver_enforced": True,
            "candidate_head": None,
            "builder_files": [],
            "tester_files": [],
            "tester_source": None,
            "dirty_snapshot": [],
            "recovery_policy": {
                "schema_version": 1,
                "mode": "automatic_nonsemantic",
                "continuation_window": 3,
            },
            "cost_ancestry": None,
            "continuation": None,
            "carryover": None,
            "deployment": None,
            "commands": [
                {
                    "id": "compact-blackbox",
                    "argv": [sys.executable, "-c", "raise SystemExit(0)"],
                    "expected_returncodes": [0],
                    "timeout_seconds": 30,
                }
            ],
            "agents": {},
        },
    }


class CompactProfileContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_eligible_compact_contract_keeps_all_five_gates(self) -> None:
        contract = compact_contract()
        validated = core.validate(contract, self.repo)

        self.assertEqual(validated["status"], "READY")
        self.assertEqual(validated["admission"]["status"], "READY")
        self.assertEqual(contract["assurance"]["profile"], "compact")
        self.assertEqual(
            set(contract["assurance"]["required"]),
            {"tester", "proof", "machine", "blackbox", "reviewer"},
        )

    def test_compact_profile_is_rejected_when_behavior_count_exceeds_three(self) -> None:
        contract = compact_contract()
        contract["mission"]["behaviors"].extend(
            [
                {"id": "subtract-values", "description": "Subtraction works."},
                {"id": "multiply-values", "description": "Multiplication works."},
                {"id": "divide-values", "description": "Division works."},
            ]
        )

        with self.assertRaises(core.AssuranceError) as raised:
            core.validate(contract, self.repo)

        self.assertIn(raised.exception.status, {"FAIL", "NEEDS_USER"})
        self.assertFalse((self.repo / ".git" / "builder-loop-assurance-v4").exists())

    def test_compact_start_projects_requested_and_effective_profile(self) -> None:
        started = core.start(
            self.repo,
            "compact-profile-run",
            "compact-profile-session",
            compact_contract(),
        )
        ledger = read_ledger(self.repo, "compact-profile-run")
        telemetry = started["telemetry"]

        self.assertEqual(telemetry["profile"], {
            "requested": "compact",
            "effective": "compact",
            "escalation_reason": None,
        })
        self.assertEqual(ledger["facets"]["assurance"]["profile"], "compact")
        self.assertEqual(telemetry["duration_breakdown"].keys(), {
            "implementation_ms",
            "verification_ms",
            "orchestration_ms",
            "waiting_ms",
        })

    def test_compact_contract_does_not_accept_reviewer_preflight(self) -> None:
        contract = compact_contract()
        contract["assurance"]["reviewer_preflight"] = True

        with self.assertRaises(core.AssuranceError) as raised:
            core.validate(contract, self.repo)

        self.assertIn(raised.exception.status, {"FAIL", "NEEDS_USER"})

    def test_profile_fields_are_not_a_second_runtime_state_store(self) -> None:
        contract = compact_contract()
        contract["assurance"]["profile"] = "compact"
        first = core.validate(contract, self.repo)
        second = core.validate(copy.deepcopy(contract), self.repo)

        self.assertEqual(first["digests"], second["digests"])
        self.assertNotIn("profile_state", first)
        self.assertNotIn("profile_state", second)


if __name__ == "__main__":
    unittest.main()
