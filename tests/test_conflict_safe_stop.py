from __future__ import annotations

import unittest
from pathlib import Path

from harness import (
    assert_ledger_schema,
    assert_status,
    assert_status_one_of,
    cleanup_repo,
    commit_all,
    git,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    record_evidence,
    register_agent,
    repo_session_id,
    run_cli,
    start_run,
    worktrees_from,
    write_plan,
)


class ConflictSafeStopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo({"shared.txt": "base\n"})
        self.spec_head = head(self.repo)
        self.plan = write_plan(
            self.repo,
            plan_markdown(
                self.spec_head,
                builder_write=["src/**", "docs/**", "shared.txt"],
            ),
        )

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_conflict_preserves_main_branches_worktrees_and_ledger(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, tester = worktrees_from(started, run_path)
        (builder / "shared.txt").write_text("builder change\n")
        commit_all(builder, "builder changes shared line")

        assert_status(
            run_cli("role-check", "--run", run_path, "--role", "builder"),
            "READY",
            rc=0,
        )
        assert_status(
            run_cli("role-check", "--run", run_path, "--role", "tester"),
            "READY",
            rc=0,
        )
        tester_agent_id = register_agent(run_path, "tester")
        integrated = run_cli("integrate-tests", "--run", run_path)
        assert_status_one_of(integrated, {"READY", "NOOP"}, rc=0)
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        candidate = head(builder)
        register_agent(run_path, "tester", agent_id=tester_agent_id, result="pass")
        record_evidence(
            run_path,
            "e2e_verified",
            candidate,
            agent_id=tester_agent_id,
        )
        reviewer_agent_id = register_agent(run_path, "reviewer")
        record_evidence(
            run_path,
            "reviewed",
            candidate,
            agent_id=reviewer_agent_id,
        )
        record_evidence(
            run_path,
            "doc_reviewed",
            candidate,
            agent_id=reviewer_agent_id,
        )

        (self.repo / "shared.txt").write_text("concurrent main change\n")
        main_before_finalize = commit_all(self.repo, "main changes shared line")
        branches_before = set(git(self.repo, "branch", "--format=%(refname:short)").splitlines())

        result = run_cli("finalize", "--run", run_path)
        assert_status(result, "CONFLICT")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(head(self.repo), main_before_finalize)
        self.assertTrue(builder.is_dir())
        self.assertTrue(tester.is_dir())
        self.assertTrue((run_path / "ledger.json").is_file())
        self.assertEqual(
            set(git(self.repo, "branch", "--format=%(refname:short)").splitlines()),
            branches_before,
        )
        self.assertEqual(git(builder, "diff", "--name-only", "--diff-filter=U"), "")

        git_dir = git(builder, "rev-parse", "--git-dir")
        git_dir_path = (builder / git_dir).resolve() if not git_dir.startswith("/") else Path(git_dir)
        self.assertFalse((git_dir_path / "rebase-merge").exists())
        self.assertFalse((git_dir_path / "rebase-apply").exists())
        self.assertFalse((git_dir_path / "MERGE_HEAD").exists())

        conflicts = result.data.get("conflict_files") or []
        self.assertIn("shared.txt", conflicts)
        status = run_cli("status", "--run", run_path)
        assert_status(status, "CONFLICT")
        assert_ledger_schema(run_path)

    def test_non_conflicting_target_move_persists_continuity_failure(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, tester = worktrees_from(started, run_path)
        (builder / "src" / "feature.py").write_text("VALUE = 1\n")
        commit_all(builder, "builder changes source")
        tester_agent_id = register_agent(run_path, "tester")
        assert_status_one_of(
            run_cli("integrate-tests", "--run", run_path),
            {"READY", "NOOP"},
            rc=0,
        )
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        candidate = head(builder)
        register_agent(run_path, "tester", agent_id=tester_agent_id, result="pass")
        record_evidence(
            run_path,
            "e2e_verified",
            candidate,
            agent_id=tester_agent_id,
        )
        reviewer_agent_id = register_agent(run_path, "reviewer")
        for kind in ("reviewed", "doc_reviewed"):
            record_evidence(run_path, kind, candidate, agent_id=reviewer_agent_id)

        (self.repo / "README.md").write_text("concurrent non-conflicting change\n")
        moved_head = commit_all(self.repo, "advance target without overlap")
        status_before = run_cli("status", "--run", run_path)
        assert_status(status_before, "CONTINUITY_FAILURE", rc=1)
        self.assertFalse(status_before.data.get("target_continuous"), status_before.data)
        self.assertFalse(status_before.data.get("ready_to_finalize"), status_before.data)

        result = run_cli("finalize", "--run", run_path)
        assert_status(result, "CONTINUITY_FAILURE", rc=1)
        self.assertEqual(head(self.repo), moved_head)
        self.assertTrue(builder.is_dir())
        self.assertTrue(tester.is_dir())

        status_after = run_cli("status", "--run", run_path)
        assert_status(status_after, "CONTINUITY_FAILURE", rc=1)
        self.assertEqual(status_after.data.get("phase"), "continuity_failure")
        assert_ledger_schema(run_path)

    def test_status_continuity_failure_does_not_recover_when_ref_moves_back(self) -> None:
        _started, run_path = start_run(self.repo, self.plan)
        (self.repo / "README.md").write_text("temporary target movement\n")
        moved_head = commit_all(self.repo, "move target temporarily")

        first = run_cli("status", "--run", run_path)
        assert_status(first, "CONTINUITY_FAILURE", rc=1)
        self.assertEqual(first.data.get("phase"), "continuity_failure")

        git(
            self.repo,
            "update-ref",
            "refs/heads/main",
            self.spec_head,
            moved_head,
        )
        second = run_cli("status", "--run", run_path)
        assert_status(second, "CONTINUITY_FAILURE", rc=1)
        self.assertEqual(second.data.get("phase"), "continuity_failure")
        assert_ledger_schema(run_path)

    def test_deleted_target_branch_persists_continuity_failure_after_recreation(self) -> None:
        git(self.repo, "branch", "release")
        started = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            self.plan,
            "--run",
            "deleted-release-run",
            "--session-id",
            repo_session_id(self.repo, "deleted-release"),
            "--target-branch",
            "release",
        )
        assert_status(started, "READY", rc=0)
        run_path = Path(started.data["run_path"])
        git(self.repo, "branch", "-D", "release")

        first = run_cli("status", "--run", run_path)
        assert_status(first, "CONTINUITY_FAILURE", rc=1)
        self.assertIsNone(first.data.get("target_head"))
        git(self.repo, "branch", "release", self.spec_head)

        second = run_cli("status", "--run", run_path)
        assert_status(second, "CONTINUITY_FAILURE", rc=1)
        self.assertEqual(second.data.get("phase"), "continuity_failure")


if __name__ == "__main__":
    unittest.main()
