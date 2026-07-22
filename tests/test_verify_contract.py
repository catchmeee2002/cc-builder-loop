from __future__ import annotations

import unittest
from pathlib import Path

from harness import (
    assert_ledger_schema,
    assert_status,
    cleanup_repo,
    commit_all,
    finish_agent_turn,
    git,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    run_cli,
    start_run,
    start_agent_turn,
    worktrees_from,
    write_plan,
)


class VerifyContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def start(self) -> tuple[Path, Path, Path]:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        return repo, builder, run_path

    def test_pass_records_verified_head(self) -> None:
        _repo, builder, run_path = self.start()
        result = run_cli("verify", "--run", run_path)
        assert_status(result, "PASS", rc=0)
        self.assertEqual(result.data.get("head"), head(builder))
        ledger = load_ledger(run_path)
        self.assertEqual(ledger.get("verified_head"), head(builder))

        status = run_cli("status", "--run", run_path)
        assert_status(status, "ACTIVE", rc=0)
        self.assertIn("reviewed_head", status.data.get("missing_gates") or [])
        self.assertFalse(status.data.get("ready_to_finalize"), status.data)

    def test_fail_is_distinct_from_fatal_and_does_not_record_evidence(self) -> None:
        _repo, builder, run_path = self.start()
        (builder / "src" / "calc.py").write_text("def add(a, b):\n    return (\n")
        commit_all(builder, "introduce syntax failure")

        result = run_cli("verify", "--run", run_path)
        assert_status(result, "FAIL", rc=1)
        self.assertTrue(result.data.get("stage") or result.data.get("runner"), result.data)
        ledger = load_ledger(run_path)
        self.assertNotEqual(ledger.get("verified_head"), head(builder))

    def test_missing_runner_is_blocked_and_can_never_be_pass(self) -> None:
        _repo, builder, run_path = self.start()
        (builder / "verify.sh").unlink()
        commit_all(builder, "remove configured runner")

        result = run_cli("verify", "--run", run_path)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "ROLE_BOUNDARY_VIOLATION")
        self.assertNotIn("PASS", result.stdout.splitlines()[-1])

    def test_unknown_run_is_fatal(self) -> None:
        result = run_cli("verify", "--run", "/tmp/does-not-exist-builder-loop-run")
        assert_status(result, "FATAL", rc=2)

    def test_integrated_tester_file_is_executed_by_fixture_runner(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        _builder, tester = worktrees_from(started, run_path)
        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")
        (tester / "tests" / "test_new_failure.py").write_text(
            "def test_new_failure():\n    assert False, 'integrated tester evidence ran'\n"
        )
        commit_all(tester, "add failing independent test")
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_agent_id,
            turn_id=tester_turn_id,
            result="tests_ready",
        )
        assert_status(run_cli("integrate-tests", "--run", run_path), "READY", rc=0)

        result = run_cli("verify", "--run", run_path)
        assert_status(result, "FAIL", rc=1)
        self.assertEqual(result.data.get("code"), "PASS_COMMAND_FAILED")

    def test_integrated_unittest_case_is_executed_by_fixture_runner(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        _builder, tester = worktrees_from(started, run_path)
        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")
        (tester / "tests" / "test_class_failure.py").write_text(
            "import unittest\n\n"
            "class TestFailure(unittest.TestCase):\n"
            "    def test_failure(self):\n"
            "        self.fail('integrated unittest evidence ran')\n"
        )
        commit_all(tester, "add failing unittest test case")
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_agent_id,
            turn_id=tester_turn_id,
            result="tests_ready",
        )
        assert_status(run_cli("integrate-tests", "--run", run_path), "READY", rc=0)

        result = run_cli("verify", "--run", run_path)
        assert_status(result, "FAIL", rc=1)
        self.assertEqual(result.data.get("code"), "PASS_COMMAND_FAILED")

    def test_runner_mutation_is_isolated_from_candidate_and_next_attempt(self) -> None:
        repo = init_repo(
            {
                "mutate.sh": (
                    "#!/usr/bin/env bash\n"
                    "echo 'GENERATED = 1' > src/runner_generated.py\n"
                    "exit 1\n"
                ),
            }
        )
        (repo / "mutate.sh").chmod(0o755)
        commit_all(repo, "add mutating verifier")
        self.repos.append(repo)
        text = plan_markdown(head(repo), runner="bash mutate.sh").replace(
            '  support_paths: ["verify.sh"]', '  support_paths: ["mutate.sh"]'
        )
        plan = write_plan(repo, text)
        started, run_path = start_run(repo, plan)
        builder, _tester = worktrees_from(started, run_path)

        for _ in range(2):
            result = run_cli("verify", "--run", run_path)
            assert_status(result, "FAIL", rc=1)
            self.assertFalse((builder / "src" / "runner_generated.py").exists())
            self.assertFalse((builder.parent / "verify").exists())

    def test_ignored_builder_file_cannot_influence_clean_verification(self) -> None:
        repo = init_repo({".gitignore": ".env\n"})
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo), runner="test -f .env"))
        started, run_path = start_run(repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / ".env").write_text("ALLOW=1\n")

        result = run_cli("verify", "--run", run_path)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "CANDIDATE_DIRTY")
        self.assertTrue((builder / ".env").is_file())

    def test_verification_iteration_limit_stops_unbounded_retry(self) -> None:
        repo = init_repo(
            {
                ".claude/loop.yml": (
                    "pass_cmd:\n"
                    "  - stage: test\n"
                    "    cmd: bash verify.sh\n"
                    "    timeout: 30\n"
                    "max_iterations: 2\n"
                )
            }
        )
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo), runner=None))
        started, run_path = start_run(repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "src" / "calc.py").write_text("def add(a, b):\n    return (\n")
        commit_all(builder, "persistent failure")

        first = run_cli("verify", "--run", run_path)
        assert_status(first, "FAIL", rc=1)
        self.assertFalse(first.data.get("iteration_limit_reached"), first.data)
        second = run_cli("verify", "--run", run_path)
        assert_status(second, "FAIL", rc=1)
        self.assertTrue(second.data.get("iteration_limit_reached"), second.data)

        status = run_cli("status", "--run", run_path)
        assert_status(status, "NEEDS_USER", rc=1)
        self.assertEqual(status.data.get("verification_attempts"), 2)
        self.assertEqual(status.data.get("max_iterations"), 2)
        third = run_cli("verify", "--run", run_path)
        assert_status(third, "NEEDS_USER", rc=1)
        self.assertEqual(third.data.get("code"), "ITERATION_LIMIT_REACHED")
        assert_ledger_schema(run_path)

    def test_static_cd_composite_runner_is_preflighted_and_executed(self) -> None:
        repo = init_repo(
            {
                "checks/verify.sh": (
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "cd ..\n"
                    "bash verify.sh\n"
                )
            }
        )
        self.repos.append(repo)
        text = plan_markdown(
            head(repo), runner="cd checks && bash verify.sh"
        ).replace(
            '  support_paths: ["verify.sh"]',
            '  support_paths: ["checks/verify.sh", "verify.sh"]',
        )
        plan = write_plan(repo, text)
        _started, run_path = start_run(repo, plan)
        result = run_cli("verify", "--run", run_path)
        assert_status(result, "PASS", rc=0)

    def test_repeated_verify_keeps_attempt_logs_immutable(self) -> None:
        _repo, _builder, run_path = self.start()
        first = run_cli("verify", "--run", run_path)
        assert_status(first, "PASS", rc=0)
        first_log = Path(first.data["stages"][0]["log"])
        first_content = first_log.read_text()

        second = run_cli("verify", "--run", run_path)
        assert_status(second, "PASS", rc=0)
        second_log = Path(second.data["stages"][0]["log"])

        self.assertNotEqual(first_log, second_log)
        self.assertIn("attempt-0001", str(first_log))
        self.assertIn("attempt-0002", str(second_log))
        self.assertEqual(first_log.read_text(), first_content)

    def test_internal_builder_checkpoint_ignores_repository_gpg_signing(self) -> None:
        repo, builder, run_path = self.start()
        git(repo, "config", "commit.gpgSign", "true")
        git(repo, "config", "gpg.program", "/bin/false")
        (builder / "src" / "unsigned_checkpoint.py").write_text("VALUE = 1\n")

        result = run_cli("verify", "--run", run_path)
        assert_status(result, "PASS", rc=0)
        self.assertTrue((builder / "src" / "unsigned_checkpoint.py").is_file())


if __name__ == "__main__":
    unittest.main()
