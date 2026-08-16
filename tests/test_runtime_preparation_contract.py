from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from harness import cleanup_repo, commit_all, init_repo
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.store import read_ledger, state_root
from tests.test_assurance_v4_contract import contract_for


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(
    "runtime/codex_builder_loop/assurance_v4/runtime-support.json"
)
PROOF_CORE = "runtime/codex_builder_loop/assurance_v4/core.py"
NEW_PROOF_CORE = "runtime/codex_builder_loop/assurance_v4/new_writer.py"


class RuntimePreparationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        files = {
            MANIFEST_PATH.as_posix(): (ROOT / MANIFEST_PATH).read_text(encoding="utf-8"),
            "scripts/codex-builder-loop.py": "raise SystemExit(0)\n",
            "runtime/codex_builder_loop/core.py": "VALUE = 1\n",
            PROOF_CORE: "VALUE = 1\n",
            "runtime/codex_builder_loop/native_driver/coordinator.py": "VALUE = 1\n",
            "schema/assurance-v4-admission.schema.json": "{}\n",
            "schema/assurance-v4-contract.schema.json": "{}\n",
            "schema/assurance-v4-evidence.schema.json": "{}\n",
            "schema/assurance-v4-ledger.schema.json": "{}\n",
            "schema/assurance-v4-runtime-support.schema.json": "{}\n",
            "schema/codex-test-proof.schema.json": "{}\n",
        }
        for relative, content in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        commit_all(self.repo, "seed self-hosted runtime support")
        self.source_patch = patch.object(
            core,
            "_runtime_source_root",
            return_value=self.repo,
        )
        self.source_patch.start()

    def tearDown(self) -> None:
        self.source_patch.stop()
        cleanup_repo(self.repo)

    def runtime_contract(self, *paths: str) -> dict:
        contract = contract_for(self.repo)
        contract["authority"]["builder_write"] = list(paths)
        contract["authority"]["tester_write"] = []
        return contract

    def preparation_contract(self, *paths: str) -> dict:
        contract = self.runtime_contract(*paths)
        contract["mission"]["delivery_kind"] = "preparation"
        contract["authority"]["protected_support_paths"] = list(paths)
        contract["assurance"]["required"] = ["machine", "blackbox", "reviewer"]
        return contract

    def test_normal_self_hosted_contract_is_rejected_before_run_state_exists(self) -> None:
        contract = self.runtime_contract(PROOF_CORE)
        with self.assertRaises(core.AssuranceError) as raised:
            core.start(
                self.repo,
                "self-hosted-cycle",
                "self-hosted-session",
                contract,
            )
        self.assertEqual(raised.exception.code, "RUNTIME_PREPARATION_REQUIRED")
        self.assertEqual(raised.exception.status, "NEEDS_USER")
        self.assertEqual(
            raised.exception.details["runtime_support"]["affected_paths"],
            [PROOF_CORE],
        )
        self.assertFalse(state_root(self.repo).exists())

    def test_preparation_requires_exact_paths_and_independent_gates(self) -> None:
        contract = self.preparation_contract(PROOF_CORE)
        validated = core.validate(contract, self.repo)
        self.assertEqual(validated["status"], "READY")
        self.assertEqual(validated["runtime_support"]["mode"], "self_hosted")
        self.assertEqual(validated["runtime_support"]["affected_gates"], ["proof"])

        cyclic = deepcopy(contract)
        cyclic["assurance"]["required"].append("proof")
        with self.assertRaises(core.AssuranceError) as raised:
            core.validate(cyclic, self.repo)
        self.assertEqual(raised.exception.code, "RUNTIME_PREPARATION_GATE_CYCLE")

        incomplete = deepcopy(contract)
        incomplete["assurance"]["required"].remove("blackbox")
        with self.assertRaises(core.AssuranceError) as raised:
            core.validate(incomplete, self.repo)
        self.assertEqual(
            raised.exception.code,
            "RUNTIME_PREPARATION_ASSURANCE_INCOMPLETE",
        )

        mismatched = deepcopy(contract)
        mismatched["authority"]["protected_support_paths"] = []
        with self.assertRaises(core.AssuranceError) as raised:
            core.validate(mismatched, self.repo)
        self.assertEqual(raised.exception.code, "RUNTIME_PREPARATION_PATH_MISMATCH")

    def test_start_persists_runtime_support_snapshot(self) -> None:
        contract = self.preparation_contract(PROOF_CORE)
        started = core.start(
            self.repo,
            "runtime-preparation",
            "runtime-preparation-session",
            contract,
        )
        ledger = read_ledger(self.repo, "runtime-preparation")
        self.assertEqual(started["runtime_support"], ledger["runtime_support"])
        self.assertRegex(ledger["runtime_support"]["runtime_head"], r"^[0-9a-f]{40}$")
        self.assertRegex(ledger["runtime_support"]["manifest_blob"], r"^[0-9a-f]{40}$")
        self.assertRegex(
            ledger["runtime_support"]["manifest_digest"],
            r"^[0-9a-f]{64}$",
        )

    def test_checkpoint_uses_frozen_manifest_and_records_late_overlap(self) -> None:
        contract = self.preparation_contract(MANIFEST_PATH.as_posix(), NEW_PROOF_CORE)
        contract["authority"]["protected_support_paths"] = [MANIFEST_PATH.as_posix()]
        started = core.start(
            self.repo,
            "runtime-preparation-late-path",
            "runtime-preparation-session",
            contract,
        )
        candidate = Path(started["candidate_worktree"])
        manifest = json.loads((candidate / MANIFEST_PATH).read_text(encoding="utf-8"))
        manifest["support_sets"][0]["path_patterns"] = [MANIFEST_PATH.as_posix()]
        (candidate / MANIFEST_PATH).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        new_writer = candidate / NEW_PROOF_CORE
        new_writer.parent.mkdir(parents=True, exist_ok=True)
        new_writer.write_text("VALUE = 2\n", encoding="utf-8")
        candidate_head = commit_all(candidate, "change runtime support and add writer")

        with self.assertRaises(core.AssuranceError) as raised:
            core.checkpoint_builder(self.repo, "runtime-preparation-late-path")
        self.assertEqual(raised.exception.code, "RUNTIME_PREPARATION_PATH_MISMATCH")
        self.assertIn(NEW_PROOF_CORE, raised.exception.details["expected_paths"])
        ledger = read_ledger(self.repo, "runtime-preparation-late-path")
        problems = [
            item
            for item in ledger["problems"]
            if item["key"] == "runtime-preparation-required"
            and item["status"] == "open"
        ]
        self.assertEqual(len(problems), 1, problems)
        problem = problems[0]
        self.assertEqual(problem["owner"], "builder_loop")
        self.assertEqual(problem["candidate_head"], candidate_head)
        self.assertNotEqual(
            ledger["facets"]["execution"]["candidate_head"],
            candidate_head,
        )
        self.assertFalse(ledger["builder_checkpointed"])
        details = json.loads(problem["details"])
        self.assertEqual(details["code"], "RUNTIME_PREPARATION_PATH_MISMATCH")
        self.assertEqual(
            details["runtime_support"]["affected_paths"],
            sorted([MANIFEST_PATH.as_posix(), NEW_PROOF_CORE]),
        )

    def test_external_repository_remains_unaffected(self) -> None:
        self.source_patch.stop()
        try:
            contract = contract_for(self.repo)
            validated = core.validate(contract, self.repo)
        finally:
            self.source_patch.start()
        self.assertEqual(validated["status"], "READY")
        self.assertEqual(validated["runtime_support"]["mode"], "external")
        self.assertEqual(validated["runtime_support"]["affected_paths"], [])


if __name__ == "__main__":
    unittest.main()
