from __future__ import annotations

import copy
import unittest

from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.models import ContractError, validate_problem_report
from tests.test_compact_profile_contract import compact_contract


class AutomaticRecoveryContractTest(unittest.TestCase):
    def test_automatic_policy_has_exact_three_transition_window(self) -> None:
        contract = compact_contract()
        policy = contract["execution"]["recovery_policy"]
        self.assertEqual(policy, {
            "schema_version": 1,
            "mode": "automatic_nonsemantic",
            "continuation_window": 3,
        })
        validated = core.validate_contract(contract)
        self.assertEqual(validated["execution"]["recovery_policy"], policy)

    def test_engineering_correction_is_assurance_facet_and_plan_owned(self) -> None:
        report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "machine-command-correction",
                    "summary": "A frozen machine timeout needs a mechanical correction.",
                    "details": "The timeout value is invalid for the current trusted command.",
                    "owner": "plan",
                    "decision_request": {
                        "kind": "engineering_correction",
                        "facet": "assurance",
                        "changes": [
                            {
                                "operation": "replace",
                                "pointer": "/assurance/machine_commands/0/timeout_seconds",
                                "value": 60,
                            }
                        ],
                        "question": "Apply this exact non-semantic correction once?",
                    },
                }
            ],
        }
        self.assertEqual(validate_problem_report(report), report)

    def test_engineering_correction_cannot_change_mission_or_authority(self) -> None:
        report = {
            "schema_version": 1,
            "problems": [
                {
                    "key": "invalid-correction-facet",
                    "summary": "The correction attempts to change the mission.",
                    "details": "The requested delta is not an Assurance-only correction.",
                    "owner": "plan",
                    "decision_request": {
                        "kind": "engineering_correction",
                        "facet": "mission",
                        "changes": [{"operation": "replace", "pointer": "/mission/objective", "value": "new"}],
                        "question": "Change the mission?",
                    },
                }
            ],
        }
        with self.assertRaises(ContractError):
            validate_problem_report(report)

    def test_legacy_manual_policy_remains_distinct(self) -> None:
        contract = compact_contract()
        contract["execution"]["recovery_policy"] = {
            "schema_version": 1,
            "mode": "manual",
        }
        validated = core.validate_contract(contract)
        self.assertEqual(validated["execution"]["recovery_policy"]["mode"], "manual")
        automatic = copy.deepcopy(contract)
        automatic["execution"]["recovery_policy"] = {
            "schema_version": 1,
            "mode": "automatic_nonsemantic",
            "continuation_window": 3,
        }
        self.assertNotEqual(
            validated["execution"]["recovery_policy"],
            core.validate_contract(automatic)["execution"]["recovery_policy"],
        )


if __name__ == "__main__":
    unittest.main()
