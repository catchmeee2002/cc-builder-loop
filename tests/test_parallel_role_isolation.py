from __future__ import annotations

import unittest

from harness import (
    assert_status,
    cleanup_repo,
    commit_all,
    finish_agent_turn,
    git,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    repo_session_id,
    run_cli,
    start_run,
    start_agent_turn,
    worktrees_from,
    write_plan,
)


class ParallelRoleIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.spec_head = head(self.repo)
        self.plan = write_plan(self.repo, plan_markdown(self.spec_head, parallel_ready=True))

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_builder_and_tester_start_from_same_spec_head_and_integrate_explicitly(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, tester = worktrees_from(started, run_path)

        self.assertNotEqual(builder, tester)
        self.assertTrue(builder.is_dir())
        self.assertTrue(tester.is_dir())
        self.assertEqual(head(builder), self.spec_head)
        self.assertEqual(head(tester), self.spec_head)
        self.assertEqual(head(self.repo), self.spec_head)

        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")

        (builder / "src" / "builder_only.py").write_text("VALUE = 1\n")
        builder_head = commit_all(builder, "builder implementation")
        (tester / "tests" / "test_tester_only.py").write_text(
            "from src.builder_only import VALUE\n\n"
            "def test_owned():\n"
            "    assert VALUE == 1\n"
        )
        tester_head = commit_all(tester, "tester independent test")

        self.assertFalse((builder / "tests" / "test_tester_only.py").exists())
        self.assertFalse((tester / "src" / "builder_only.py").exists())
        self.assertEqual(head(self.repo), self.spec_head)

        builder_check = run_cli("role-check", "--run", run_path, "--role", "builder")
        tester_check = run_cli("role-check", "--run", run_path, "--role", "tester")
        assert_status(builder_check, "READY", rc=0)
        assert_status(tester_check, "READY", rc=0)
        self.assertEqual(builder_check.data.get("checked_head"), builder_head)
        self.assertEqual(tester_check.data.get("checked_head"), tester_head)

        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_agent_id,
            turn_id=tester_turn_id,
            result="tests_ready",
        )
        integrated = run_cli("integrate-tests", "--run", run_path)
        assert_status(integrated, "READY", rc=0)
        self.assertTrue((builder / "tests" / "test_tester_only.py").is_file())
        self.assertTrue((builder / "src" / "builder_only.py").is_file())
        self.assertFalse((tester / "src" / "builder_only.py").exists())
        self.assertEqual(head(self.repo), self.spec_head)

    def test_start_stops_when_plan_spec_head_is_stale(self) -> None:
        (self.repo / "README.md").write_text("main moved\n")
        commit_all(self.repo, "advance main after planning")
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            self.plan,
            "--task",
            "stale plan",
            "--session-id",
            repo_session_id(self.repo),
        )
        assert_status(result, "NEEDS_USER")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_non_l1_integration_requires_tester_author_turn(self) -> None:
        _started, run_path = start_run(self.repo, self.plan)
        result = run_cli("integrate-tests", "--run", run_path)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "TESTER_AUTHOR_RESULT_MISSING")

    def test_empty_tester_commit_advances_author_integration_without_conflict(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        _builder, tester = worktrees_from(started, run_path)
        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")
        tester_head = commit_all(tester, "tester author attestation without tree delta")
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_agent_id,
            turn_id=tester_turn_id,
            result="tests_ready",
        )

        integrated = run_cli("integrate-tests", "--run", run_path)
        assert_status(integrated, "NOOP", rc=0)
        self.assertEqual(integrated.data.get("code"), "NO_TEST_TREE_CHANGES")
        ledger = load_ledger(run_path)
        self.assertEqual(ledger["tester_integration"]["source_head"], tester_head)
        self.assertIs(ledger["tester_integration"]["completed"], True)

    def test_internal_test_integration_ignores_repository_gpg_signing(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        _builder, tester = worktrees_from(started, run_path)
        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")
        (tester / "tests" / "test_unsigned_integration.py").write_text(
            "from src.calc import add\n\n"
            "def test_unsigned_integration():\n"
            "    assert add(2, 3) == 5\n"
        )
        commit_all(tester, "tester evidence before signing policy")
        git(self.repo, "config", "commit.gpgSign", "true")
        git(self.repo, "config", "gpg.program", "/bin/false")
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_agent_id,
            turn_id=tester_turn_id,
            result="tests_ready",
        )

        integrated = run_cli("integrate-tests", "--run", run_path)
        assert_status(integrated, "READY", rc=0)


if __name__ == "__main__":
    unittest.main()
