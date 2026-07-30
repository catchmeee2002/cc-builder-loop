from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness import (
    assert_status,
    cleanup_repo,
    head,
    load_ledger,
    run_cli,
)
from proof_harness import (
    DEFAULT_CANDIDATE,
    UNITTEST_ID,
    baseline_group,
    create_proof_fixture,
    mutation_group,
    prove,
    unittest_source,
)


class ProofBeforeMachineContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def fixture(self, **kwargs):
        fixture = create_proof_fixture(verify_machine=False, **kwargs)
        self.repos.append(fixture.repo)
        return fixture

    def assert_machine_budget_untouched(self, fixture) -> None:
        ledger = load_ledger(fixture.run_path)
        self.assertEqual(ledger["verification"]["attempts"], [])
        self.assertIsNone(ledger.get("evidence", {}).get("machine"))
        self.assertIsNone(ledger.get("evidence", {}).get("test_effectiveness"))
        status = run_cli("status", "--run", fixture.run_path)
        self.assertEqual(status.data.get("verification_attempts"), 0, status.data)
        self.assertIsNone(status.data.get("verified_head"), status.data)
        self.assertIsNone(status.data.get("test_effectiveness_head"), status.data)

    def test_all_strong_proof_precedes_machine_without_unlocking_blackbox(self) -> None:
        fixture = self.fixture()
        proof = prove(fixture, baseline_group())
        assert_status(proof, "READY", rc=0)
        self.assertEqual(proof.data["test_effectiveness_head"], fixture.integrated_head)

        before_verify = run_cli("status", "--run", fixture.run_path)
        self.assertEqual(before_verify.data.get("verification_attempts"), 0)
        self.assertIsNone(before_verify.data.get("verified_head"))
        self.assertEqual(
            before_verify.data.get("test_effectiveness_head"), fixture.integrated_head
        )

        blackbox = run_cli(
            "prepare-follow-up",
            "--run",
            fixture.run_path,
            "--role",
            "tester",
            "--agent-id",
            fixture.tester_agent_id,
            "--purpose",
            "blackbox",
        )
        assert_status(blackbox, "NEEDS_USER", rc=1)
        self.assertEqual(
            blackbox.data.get("code"), "TESTER_BLACKBOX_PREREQUISITES_MISSING"
        )
        self.assertEqual(
            load_ledger(fixture.run_path)["verification"]["attempts"], []
        )

        verified = run_cli("verify", "--run", fixture.run_path)
        assert_status(verified, "PASS", rc=0)
        final_status = run_cli("status", "--run", fixture.run_path)
        self.assertEqual(final_status.data.get("verified_head"), fixture.integrated_head)
        self.assertEqual(
            final_status.data.get("test_effectiveness_head"), fixture.integrated_head
        )

    def test_pre_machine_proof_failures_preserve_machine_attempt_budget(self) -> None:
        invalid = self.fixture()
        invalid_result = run_cli(
            "prove-tests",
            "--repo",
            invalid.repo,
            "--run",
            invalid.run_path,
            "--spec",
            "-",
            input_text=json.dumps({"schema_version": 1, "groups": []}),
        )
        self.assertEqual(invalid_result.data.get("code"), "TEST_PROOF_SPEC_INVALID")
        self.assert_machine_budget_untouched(invalid)

        candidate_red = self.fixture(
            test_files={
                "tests/test_proof_target.py": unittest_source(
                    "self.assertEqual(add(1, 2), 99)"
                )
            }
        )
        candidate_result = prove(candidate_red, baseline_group())
        self.assertEqual(
            candidate_result.data.get("code"), "TEST_PROOF_CANDIDATE_FAILED"
        )
        self.assert_machine_budget_untouched(candidate_red)

        baseline_not_red = self.fixture(
            baseline_source=DEFAULT_CANDIDATE,
            candidate_source=DEFAULT_CANDIDATE,
        )
        baseline_result = prove(baseline_not_red, baseline_group())
        self.assertNotEqual(baseline_result.returncode, 0)
        self.assertEqual(
            baseline_result.data.get("code"), "TEST_BASELINE_RED_NOT_PROVEN"
        )
        self.assertEqual(
            baseline_result.data["result"]["test_result"]["classification"], "pass"
        )
        self.assert_machine_budget_untouched(baseline_not_red)

        mutation_survives = self.fixture()
        harmless_patch = (
            "diff --git a/src/calc.py b/src/calc.py\n"
            "--- a/src/calc.py\n"
            "+++ b/src/calc.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def add(a, b):\n"
            "+    unused = 1\n"
            "     return a + b\n"
        )
        mutation_result = prove(mutation_survives, mutation_group(harmless_patch))
        self.assertNotEqual(mutation_result.returncode, 0)
        self.assertEqual(mutation_result.data.get("code"), "TEST_MUTATION_SURVIVED")
        self.assertEqual(
            mutation_result.data["result"]["test_result"]["classification"], "pass"
        )
        self.assert_machine_budget_untouched(mutation_survives)

    def test_reviewed_boundaries_remain_machine_first_for_mixed_and_all(self) -> None:
        cases = (
            {"add-positive": "reviewed-boundaries"},
            {
                "add-positive": "strong",
                "add-boundary": "reviewed-boundaries",
            },
        )
        for requirements in cases:
            with self.subTest(requirements=requirements):
                fixture = self.fixture(requirement_minima=requirements)
                group = baseline_group()
                group["behavior_ids"] = list(requirements)
                before = prove(fixture, group)
                assert_status(before, "NEEDS_USER", rc=1)
                self.assertEqual(before.data.get("code"), "TEST_PROOF_MACHINE_MISSING")
                self.assert_machine_budget_untouched(fixture)

                verified = run_cli("verify", "--run", fixture.run_path)
                assert_status(verified, "PASS", rc=0)
                after = prove(fixture, group)
                assert_status(after, "READY", rc=0)
                self.assertEqual(after.data["test_effectiveness_head"], head(fixture.builder))

if __name__ == "__main__":
    unittest.main()
