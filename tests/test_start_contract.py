from __future__ import annotations

import concurrent.futures
import os
import unittest
from pathlib import Path

from harness import (
    assert_status,
    cleanup_repo,
    commit_all,
    git,
    head,
    init_repo,
    l1_plan_markdown,
    load_ledger,
    plan_markdown,
    run_cli,
    write_plan,
)


class StartContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.plan = write_plan(self.repo, plan_markdown(head(self.repo)))

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def assert_public_paths(self, result) -> tuple[Path, Path, Path]:
        run_path_raw = result.data.get("run_path")
        self.assertIsInstance(run_path_raw, str, result.data)
        run_path = Path(run_path_raw)
        self.assertTrue(run_path.is_dir(), result.data)

        worktrees = result.data.get("worktrees")
        self.assertIsInstance(worktrees, dict, result.data)
        builder_raw = worktrees.get("builder")
        tester_raw = worktrees.get("tester")
        self.assertIsInstance(builder_raw, str, result.data)
        self.assertIsInstance(tester_raw, str, result.data)
        builder = Path(builder_raw)
        tester = Path(tester_raw)
        self.assertTrue(builder.is_dir(), result.data)
        self.assertTrue(tester.is_dir(), result.data)
        self.assertNotEqual(builder, tester)
        return run_path, builder, tester

    def test_task_start_generates_run_id_and_public_string_worktrees(self) -> None:
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            self.plan,
            "--task",
            "public contract fixture",
            "--session-id",
            "task-session",
        )
        assert_status(result, "READY", rc=0)
        run_id = result.data.get("run_id")
        self.assertIsInstance(run_id, str, result.data)
        self.assertTrue(run_id, result.data)
        run_path, builder, tester = self.assert_public_paths(result)
        self.assertEqual(result.data.get("spec_head"), head(self.repo))
        self.assertEqual(head(builder), head(self.repo))
        self.assertEqual(head(tester), head(self.repo))

        ledger = load_ledger(run_path)
        self.assertEqual(ledger.get("run_id"), run_id)
        self.assertIsInstance(ledger.get("worktrees", {}).get("builder"), dict)
        self.assertIsInstance(ledger.get("worktrees", {}).get("tester"), dict)

    def test_explicit_run_selector_preserves_requested_run_id(self) -> None:
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            self.plan,
            "--run",
            "explicit-fixture-run",
            "--session-id",
            "explicit-session",
        )
        assert_status(result, "READY", rc=0)
        self.assertEqual(result.data.get("run_id"), "explicit-fixture-run")
        self.assert_public_paths(result)

    def test_start_requires_task_or_run(self) -> None:
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            self.plan,
            "--session-id",
            "missing-selector-session",
        )
        assert_status(result, "FATAL", rc=2)
        self.assertEqual(result.data.get("code"), "RUN_OR_TASK_REQUIRED")

    def test_start_requires_real_session_id(self) -> None:
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            self.plan,
            "--task",
            "missing session fixture",
        )
        assert_status(result, "FATAL", rc=2)
        self.assertEqual(result.data.get("code"), "CLI_USAGE_ERROR")

    def test_l1_start_without_unit_test_spec_requires_only_review_gates(self) -> None:
        l1_plan = write_plan(
            self.repo,
            l1_plan_markdown(head(self.repo)),
            name="l1-plan.md",
        )
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            l1_plan,
            "--task",
            "l1 docs fixture",
            "--session-id",
            "l1-session",
        )
        assert_status(result, "READY", rc=0)
        self.assertIs(result.data.get("parallel_ready"), False)
        run_path, _builder, _tester = self.assert_public_paths(result)

        ledger = load_ledger(run_path)
        self.assertEqual(ledger.get("plan", {}).get("level"), "L1")
        self.assertIsNone(ledger.get("plan", {}).get("runner"))

        status = run_cli("status", "--run", run_path)
        assert_status(status, "ACTIVE", rc=0)
        self.assertEqual(
            set(status.data.get("required_gates") or []),
            {"reviewed_head", "doc_reviewed_head"},
            status.data,
        )
        self.assertNotIn("verified_head", status.data.get("required_gates") or [])

        builder = Path(result.data["worktrees"]["builder"])
        (builder / "src" / "l1_escape.py").write_text("VALUE = 1\n")
        commit_all(builder, "attempt source change in L1")
        role = run_cli("role-check", "--run", run_path, "--role", "builder")
        assert_status(role, "NEEDS_USER", rc=1)
        self.assertTrue(
            any(item.get("path") == "src/l1_escape.py" for item in role.data.get("violations", [])),
            role.data,
        )

    def test_invalid_cli_arguments_still_return_json_contract(self) -> None:
        result = run_cli("verify", "--kind", "review", "--run", "missing")
        assert_status(result, "FATAL", rc=2)
        self.assertEqual(result.data.get("code"), "CLI_USAGE_ERROR")
        self.assertIsInstance(result.data.get("message"), str)

    def test_empty_loop_stage_is_fatal_before_worktree_creation(self) -> None:
        loop_config = self.repo / ".claude" / "loop.yml"
        loop_config.parent.mkdir(parents=True, exist_ok=True)
        loop_config.write_text(
            "pass_cmd:\n"
            "  - stage: \"\"\n"
            "    cmd: bash verify.sh\n"
            "    timeout: 30\n"
        )
        commit_all(self.repo, "add invalid empty stage")
        plan = write_plan(self.repo, plan_markdown(head(self.repo)), name="empty-stage-plan.md")
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            plan,
            "--task",
            "empty stage",
            "--session-id",
            "empty-stage-session",
        )
        assert_status(result, "FATAL", rc=2)
        self.assertEqual(result.data.get("code"), "LOOP_CONFIG_INVALID")
        self.assertEqual(
            git(self.repo, "worktree", "list", "--porcelain").count("worktree "),
            1,
        )

    def test_concurrent_start_allows_only_one_active_run_per_session(self) -> None:
        def invoke(run_id: str):
            return run_cli(
                "start",
                "--repo",
                self.repo,
                "--plan",
                self.plan,
                "--run",
                run_id,
                "--session-id",
                "shared-concurrent-session",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(invoke, ("concurrent-a", "concurrent-b")))
        ready = [result for result in results if result.data.get("status") == "READY"]
        rejected = [
            result
            for result in results
            if result.data.get("code") == "SESSION_ALREADY_ACTIVE"
        ]
        self.assertEqual(len(ready), 1, [result.data for result in results])
        self.assertEqual(len(rejected), 1, [result.data for result in results])

    def test_l1_plan_freezes_planning_time_head(self) -> None:
        plan = write_plan(
            self.repo,
            l1_plan_markdown(head(self.repo)),
            name="stale-l1-plan.md",
        )
        (self.repo / "README.md").write_text("target moved\n")
        commit_all(self.repo, "move target after L1 planning")

        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            plan,
            "--run",
            "stale-l1-run",
            "--session-id",
            "stale-l1-session",
        )
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "TARGET_SPEC_MISMATCH")

    def test_l1_start_ignores_invalid_legacy_runner_config(self) -> None:
        loop_config = self.repo / ".claude" / "loop.yml"
        loop_config.parent.mkdir(parents=True, exist_ok=True)
        loop_config.write_text("pass_cmd: []\n")
        commit_all(self.repo, "add invalid legacy loop config")
        plan = write_plan(
            self.repo,
            l1_plan_markdown(head(self.repo)),
            name="l1-invalid-runner.md",
        )

        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            plan,
            "--run",
            "l1-invalid-runner",
            "--session-id",
            "l1-invalid-runner-session",
        )
        assert_status(result, "READY", rc=0)
        ledger = load_ledger(Path(result.data["run_path"]))
        self.assertEqual(ledger["loop_config"]["path"], "none")
        self.assertEqual(ledger["loop_config"]["stages"], [])

    def test_repository_runner_must_exist_at_spec_head(self) -> None:
        text = plan_markdown(head(self.repo), runner="bash missing.sh").replace(
            '  support_paths: ["verify.sh"]',
            '  support_paths: ["missing.sh"]',
        )
        plan = write_plan(self.repo, text, name="missing-runner.md")
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            plan,
            "--run",
            "missing-runner",
            "--session-id",
            "missing-runner-session",
        )
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "RUNNER_NOT_FROZEN")
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_repository_runner_symlink_is_rejected(self) -> None:
        verify = self.repo / "verify.sh"
        verify.unlink()
        os.symlink("/bin/true", verify)
        commit_all(self.repo, "replace runner with external symlink")
        plan = write_plan(
            self.repo,
            plan_markdown(head(self.repo)),
            name="symlink-runner.md",
        )
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            plan,
            "--run",
            "symlink-runner",
            "--session-id",
            "symlink-runner-session",
        )
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "RUNNER_ENTRY_NOT_REGULAR")

    def test_revised_plan_must_supersede_an_abandoned_recorded_contract(self) -> None:
        first = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            self.plan,
            "--run",
            "superseded-run",
            "--session-id",
            "superseded-session",
        )
        assert_status(first, "READY", rc=0)
        run_path = Path(first.data["run_path"])
        old_sha = load_ledger(run_path)["plan"]["sha256"]
        assert_status(run_cli("abandon", "--run", run_path), "COMPLETE", rc=0)

        revised_text = plan_markdown(head(self.repo)).replace(
            "plan_revision: 1",
            "plan_revision: 2\n"
            "supersedes:\n"
            '  run_id: "superseded-run"\n'
            f'  plan_sha256: "{old_sha}"',
        )
        revised = write_plan(self.repo, revised_text, name="revised-plan.md")
        result = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            revised,
            "--run",
            "replacement-run",
            "--session-id",
            "replacement-session",
        )
        assert_status(result, "READY", rc=0)


if __name__ == "__main__":
    unittest.main()
