from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any

from harness import cleanup_repo, commit_all, git, init_repo
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from tests.test_assurance_v4_contract import contract_for


class SuccessorPublicationContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def next_contract(
        self,
        source: dict[str, Any],
        source_ledger: dict[str, Any],
    ) -> dict[str, Any]:
        contract = contract_for(self.repo)
        contract["mission"]["revision"] = 2
        contract["mission"]["objective"] = (
            "Publish a new public prerequisite from the successor Builder."
        )
        contract["mission"]["supersedes"] = {
            "run_id": source["run_id"],
            "revision": source_ledger["facets"]["mission"]["revision"],
            "mission_digest": source_ledger["digests"]["mission"],
            "candidate_head": source_ledger["facets"]["execution"][
                "candidate_head"
            ],
        }
        contract["authority"]["builder_write"].append("contracts/public.json")
        contract["authority"]["public_prerequisites"] = [
            "contracts/public.json"
        ]
        contract["execution"]["revision_transition"] = {
            "category": "mission_change",
            "predecessor_pressure_digest": core.status(
                self.repo, source["run_id"]
            )["lineage"]["pressure_digest"],
        }
        contract["execution"]["prior_problem_dispositions"] = {
            "source_snapshot_digest": core.status(self.repo, source["run_id"])[
                "lineage"
            ]["open_problem_snapshot_digest"],
            "items": [],
        }
        return contract

    def test_successor_starts_with_carryover_separate_and_new_outputs_empty(self) -> None:
        source = core.start(
            self.repo,
            "publication-source",
            "session-publication-source",
            contract_for(self.repo),
        )
        source_candidate = Path(source["candidate_worktree"])
        (source_candidate / "src/calc.py").write_text(
            "def add(a, b):\n    return a + b\n\nSOURCE_CHANGE = 1\n",
            encoding="utf-8",
        )
        commit_all(source_candidate, "record source carryover")
        core.checkpoint_builder(self.repo, "publication-source")
        source_ledger = read_ledger(self.repo, "publication-source")
        contract = self.next_contract(source, source_ledger)

        validated = core.validate(contract, self.repo)
        self.assertEqual(validated["status"], "READY")
        target = core.start(
            self.repo,
            "publication-target",
            "session-publication-target",
            contract,
        )
        execution = read_ledger(self.repo, "publication-target")["facets"][
            "execution"
        ]
        self.assertEqual(execution["builder_files"], [])
        self.assertEqual(execution["tester_files"], [])
        self.assertIsNone(execution["tester_source"])
        self.assertNotIn(
            "contracts/public.json",
            {item["path"] for item in execution["carryover"]["files"]},
        )
        self.assertEqual(
            read_ledger(self.repo, "publication-target")["publication"]["head"],
            None,
        )

        target_candidate = Path(target["candidate_worktree"])
        public = target_candidate / "contracts/public.json"
        public.parent.mkdir(parents=True, exist_ok=True)
        public.write_text('{"version": 2}\n', encoding="utf-8")
        target_head = commit_all(target_candidate, "create successor prerequisite")
        checkpointed = core.checkpoint_builder(self.repo, "publication-target")
        self.assertIsNone(checkpointed["publication"]["head"])
        self.assertEqual(checkpointed["publication"]["files"], [])
        ledger = read_ledger(self.repo, "publication-target")
        self.assertEqual(
            ledger["facets"]["execution"]["builder_files"],
            ["contracts/public.json"],
        )
        self.assertEqual(
            ledger["facets"]["execution"]["candidate_head"], target_head
        )
        published = core.publish_prerequisites(self.repo, "publication-target")
        self.assertEqual(published["publication"]["candidate_head"], target_head)
        self.assertEqual(
            published["publication"]["files"],
            [
                {
                    "path": "contracts/public.json",
                    "blob": git(
                        self.repo,
                        "rev-parse",
                        f"{target_head}:contracts/public.json",
                    ),
                }
            ],
        )

    def test_successor_contract_validates_before_public_prerequisite_exists(self) -> None:
        source = core.start(
            self.repo,
            "publication-validation-source",
            "session-publication-validation-source",
            contract_for(self.repo),
        )
        source_ledger = read_ledger(self.repo, "publication-validation-source")
        contract = self.next_contract(source, source_ledger)
        self.assertFalse((self.repo / "contracts/public.json").exists())
        self.assertEqual(core.validate(contract, self.repo)["status"], "READY")


if __name__ == "__main__":
    unittest.main()
