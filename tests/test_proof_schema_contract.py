from __future__ import annotations

import copy
import json
import math
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schema" / "codex-test-proof.schema.json").read_text())


def definition_validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {
            "$schema": SCHEMA["$schema"],
            "$defs": SCHEMA["$defs"],
            "$ref": f"#/$defs/{name}",
        }
    )


def valid_input() -> dict:
    return {
        "schema_version": 1,
        "groups": [
            {
                "behavior_ids": ["proof-contract-parity"],
                "method": "baseline-red",
                "argv": [
                    "python3",
                    "-m",
                    "unittest",
                    "tests.test_contract.ContractTest.test_value",
                ],
                "test_ids": ["tests.test_contract.ContractTest.test_value"],
                "timeout_seconds": 30.0,
                "claimed_failure_kind": "assertion-failure",
            }
        ],
    }


def reviewed_group() -> dict:
    return {
        "behavior_ids": ["proof-contract-parity"],
        "method": "reviewed-boundaries",
        "argv": [
            "python3",
            "-m",
            "unittest",
            "tests.test_contract.ContractTest.test_value",
        ],
        "execution_argv": [
            "/usr/bin/python3",
            "-m",
            "unittest",
            "tests.test_contract.ContractTest.test_value",
        ],
        "framework": "unittest",
        "executable_identity": {
            "kind": "trusted-python",
            "requested": "python3",
            "path": "/usr/bin/python3",
            "sha256": "a" * 64,
            "size": 123,
        },
        "test_ids": ["tests.test_contract.ContractTest.test_value"],
        "timeout_seconds": 30,
        "reason": "The frozen behavior already has direct boundary coverage.",
        "reviewed_boundaries": {
            "positive_test_ids": ["tests.test_contract.ContractTest.test_positive"],
            "negative_test_ids": ["tests.test_contract.ContractTest.test_negative"],
            "boundary_test_ids": ["tests.test_contract.ContractTest.test_boundary"],
            "invariant_test_ids": ["tests.test_contract.ContractTest.test_invariant"],
        },
        "machine_evidence_head": "b" * 40,
    }


def valid_evidence() -> dict:
    group = reviewed_group()
    return {
        "status": "READY",
        "message": "proof recorded",
        "run_id": "A.b_C-1",
        "head": "b" * 40,
        "test_effectiveness_head": "b" * 40,
        "tester_source_head": "c" * 40,
        "tester_manifest_sha256": "d" * 64,
        "spec_sha256": "e" * 64,
        "groups": [group],
        "evidence": {
            "kind": "test_effectiveness",
            "observed_head": "b" * 40,
            "accepted_head": "b" * 40,
            "input_digest": "f" * 64,
            "scope": ["tests/**"],
            "provenance": {"spec_sha256": "e" * 64, "groups": [group]},
        },
    }


def classified_failure(test_id: str) -> dict:
    return {
        "argv": ["python3", "-m", "unittest", test_id],
        "returncode": 1,
        "timed_out": False,
        "test_result": {
            "framework": "unittest",
            "classification": "assertion-failure",
            "counts": {"tests": 1, "failures": 1},
            "matched_test_ids": [test_id],
        },
    }


class ProofSchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Draft202012Validator.check_schema(SCHEMA)
        cls.input_validator = Draft202012Validator(SCHEMA)
        cls.evidence_validator = definition_validator("proofEvidence")
        cls.failure_validator = definition_validator("proofFailure")

    def test_published_schema_uses_draft_2020_12_and_examples_validate(self) -> None:
        self.assertEqual(
            SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        for example in SCHEMA["examples"]:
            with self.subTest(example=example):
                self.input_validator.validate(example)

    def test_draft_integer_semantics_accept_integral_float_only(self) -> None:
        self.input_validator.validate(valid_input())
        for invalid in (True, False, math.nan, math.inf, -math.inf, 1.5, 0, 601):
            data = valid_input()
            data["groups"][0]["timeout_seconds"] = invalid
            with self.subTest(value=invalid):
                self.assertTrue(
                    list(self.input_validator.iter_errors(data)),
                    f"invalid timeout accepted: {invalid!r}",
                )

    def test_success_evidence_freezes_run_and_tester_source_identity(self) -> None:
        evidence = valid_evidence()
        self.evidence_validator.validate(evidence)
        for missing in (
            "run_id",
            "tester_source_head",
            "tester_manifest_sha256",
            "test_effectiveness_head",
        ):
            mutated = copy.deepcopy(evidence)
            mutated.pop(missing)
            with self.subTest(missing=missing):
                self.assertTrue(list(self.evidence_validator.iter_errors(mutated)))

    def test_run_id_accepts_case_dot_and_underscore_but_rejects_path_forms(self) -> None:
        evidence = valid_evidence()
        self.evidence_validator.validate(evidence)
        for invalid in (".leading", "contains/slash", "contains space", "x" * 65):
            mutated = copy.deepcopy(evidence)
            mutated["run_id"] = invalid
            with self.subTest(run_id=invalid):
                self.assertTrue(list(self.evidence_validator.iter_errors(mutated)))

    def test_evidence_integer_fields_reject_booleans_and_non_integral_values(self) -> None:
        for field, invalid in (("size", True), ("timeout_seconds", 30.5)):
            evidence = valid_evidence()
            if field == "size":
                evidence["groups"][0]["executable_identity"][field] = invalid
                evidence["evidence"]["provenance"]["groups"][0][
                    "executable_identity"
                ][field] = invalid
            else:
                evidence["groups"][0][field] = invalid
                evidence["evidence"]["provenance"]["groups"][0][field] = invalid
            with self.subTest(field=field, value=invalid):
                self.assertTrue(list(self.evidence_validator.iter_errors(evidence)))

    def test_candidate_failure_schema_keeps_single_shape_and_constrains_aggregate(self) -> None:
        first_id = "tests.test_contract.FirstTest.test_value"
        second_id = "tests.test_contract.SecondTest.test_value"
        first = classified_failure(first_id)
        second = classified_failure(second_id)
        single = {
            "status": "FAIL",
            "code": "TEST_PROOF_CANDIDATE_FAILED",
            "message": "candidate proof tests failed",
            "group": 0,
            "result": first,
        }
        self.failure_validator.validate(single)

        aggregate = copy.deepcopy(single)
        aggregate["failures"] = [
            {"group": 0, "result": first},
            {"group": 1, "result": second},
        ]
        self.failure_validator.validate(aggregate)

        invalid_values = (
            [],
            [{"group": 0}],
            [{"group": -1, "result": first}],
            [{"group": 0, "result": {"returncode": True}}],
        )
        for failures in invalid_values:
            invalid = copy.deepcopy(single)
            invalid["failures"] = failures
            with self.subTest(failures=failures):
                self.assertTrue(
                    list(self.failure_validator.iter_errors(invalid)),
                    f"invalid aggregate candidate failures accepted: {failures!r}",
                )


if __name__ == "__main__":
    unittest.main()
