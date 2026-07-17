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
        _started, run_path = start_run(repo, plan, task="schema-l2")
        self.validator.validate(load_ledger(run_path))

    def test_l1_ledger_matches_published_schema(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, l1_plan_markdown(head(repo)))
        _started, run_path = start_run(repo, plan, task="schema-l1")
        ledger = load_ledger(run_path)
        self.validator.validate(ledger)
        self.assertEqual(ledger["plan"]["builder_write"], ["README.md"])


if __name__ == "__main__":
    unittest.main()
