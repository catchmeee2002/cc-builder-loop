from __future__ import annotations

import unittest

from harness import (
    assert_ledger_schema,
    assert_status,
    cleanup_repo,
    head,
    init_repo,
    plan_markdown,
    problem_snapshot,
    run_cli,
    start_run,
    worktrees_from,
    write_plan,
)


class AbandonContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_abandon_preserves_role_worktrees_and_is_idempotent(self) -> None:
        plan = write_plan(self.repo, plan_markdown(head(self.repo)))
        started, run_path = start_run(self.repo, plan)
        builder, tester = worktrees_from(started, run_path)

        abandoned = run_cli(
            "abandon",
            "--run",
            run_path,
            "--reason",
            "fixture user decision",
        )
        assert_status(abandoned, "COMPLETE", rc=0)
        self.assertTrue(builder.is_dir())
        self.assertTrue(tester.is_dir())
        self.assertTrue(abandoned.data.get("worktrees_preserved"), abandoned.data)
        first_snapshot = problem_snapshot(run_path, abandoned.data)
        self.assertEqual(first_snapshot["problem_ids"], [])
        assert_ledger_schema(run_path)

        again = run_cli("abandon", "--run", run_path)
        assert_status(again, "COMPLETE", rc=0)
        second_snapshot = problem_snapshot(run_path, again.data)
        self.assertEqual(
            second_snapshot["snapshot_sha256"], first_snapshot["snapshot_sha256"]
        )
        self.assertEqual(second_snapshot["problem_ids"], first_snapshot["problem_ids"])
        status = run_cli("status", "--run", run_path)
        assert_status(status, "COMPLETE", rc=0)
        self.assertEqual(status.data.get("phase"), "abandoned")


if __name__ == "__main__":
    unittest.main()
