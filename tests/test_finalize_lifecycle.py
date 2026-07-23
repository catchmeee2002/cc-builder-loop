from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

from harness import (
    CLI,
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
    record_evidence,
    register_agent,
    run_cli,
    run_process,
    start_run,
    start_agent_turn,
    tree,
    worktrees_from,
    write_plan,
)


class FinalizeLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.spec_head = head(self.repo)
        self.plan = write_plan(self.repo, plan_markdown(self.spec_head))

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def ready_run(
        self, files: dict[str, str] | None = None
    ) -> tuple[Path, Path, str]:
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)
        candidate_files = files or {"src/ready_feature.py": "READY = True\n"}
        for relative, content in candidate_files.items():
            path = builder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        commit_all(builder, "ready candidate")
        tester_agent_id = register_agent(run_path, "tester")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        candidate = head(builder)
        register_agent(run_path, "tester", agent_id=tester_agent_id, result="pass")
        record_evidence(run_path, "e2e_verified", candidate, agent_id=tester_agent_id)
        reviewer_agent_id = register_agent(run_path, "reviewer")
        for kind in ("reviewed", "doc_reviewed"):
            record_evidence(run_path, kind, candidate, agent_id=reviewer_agent_id)
        return run_path, builder, candidate

    def test_finalization_squashes_all_role_commits_and_cleans_worktrees(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, tester = worktrees_from(started, run_path)
        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")

        (builder / "src" / "feature_a.py").write_text("A = 1\n")
        commit_all(builder, "builder commit one")
        (builder / "src" / "feature_b.py").write_text("B = 2\n")
        commit_all(builder, "builder commit two")

        (tester / "tests" / "test_feature_a.py").write_text(
            "from src.feature_a import A\n\ndef test_a():\n    assert A == 1\n"
        )
        commit_all(tester, "tester commit one")
        (tester / "tests" / "test_feature_b.py").write_text(
            "from src.feature_b import B\n\ndef test_b():\n    assert B == 2\n"
        )
        commit_all(tester, "tester commit two")

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
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_agent_id,
            turn_id=tester_turn_id,
            result="tests_ready",
        )
        assert_status(run_cli("integrate-tests", "--run", run_path), "READY", rc=0)
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)

        candidate = head(builder)
        candidate_tree = tree(builder)
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
        ledger_before = load_ledger(run_path)
        branches = ledger_before.get("branches", {})
        git_common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        if not git_common.is_absolute():
            git_common = (self.repo / git_common).resolve()
        commit_msg_hook = git_common / "hooks" / "commit-msg"
        commit_msg_hook.parent.mkdir(parents=True, exist_ok=True)
        commit_msg_hook.write_text("#!/bin/sh\ntouch \"$0.ran\"\n")
        commit_msg_hook.chmod(0o755)

        ready = run_cli("status", "--run", run_path)
        assert_status(ready, "ACTIVE", rc=0)
        self.assertIs(ready.data.get("delivery_gates_ready"), True, ready.data)
        self.assertIs(ready.data.get("ready_to_finalize"), True, ready.data)
        result = run_cli("finalize", "--run", run_path)
        assert_status(result, "COMPLETE", rc=0)
        self.assertEqual(result.data.get("final_head"), head(self.repo))
        self.assertEqual(tree(self.repo), candidate_tree)
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{self.spec_head}..HEAD"), "1")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), self.spec_head)
        self.assertEqual(git(self.repo, "show", "-s", "--format=%an", "HEAD"), "builder-loop fixture")
        self.assertTrue(Path(str(commit_msg_hook) + ".ran").is_file())
        self.assertFalse(builder.exists())
        self.assertFalse(tester.exists())

        if isinstance(branches, dict):
            remaining = set(git(self.repo, "branch", "--format=%(refname:short)").splitlines())
            for branch in branches.values():
                if isinstance(branch, str) and branch:
                    self.assertNotIn(branch, remaining)

        self.assertTrue((run_path / "ledger.json").is_file())
        assert_ledger_schema(run_path)
        again = run_cli("finalize", "--run", run_path)
        assert_status(again, "NOOP", rc=0)
        self.assertEqual(head(self.repo), result.data.get("final_head"))
        terminal = run_cli("abandon", "--run", run_path)
        assert_status(terminal, "NEEDS_USER", rc=1)
        self.assertEqual(terminal.data.get("code"), "RUN_TERMINAL")

    def test_dirty_tester_worktree_blocks_finalize_without_losing_tests(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, tester = worktrees_from(started, run_path)
        (builder / "src" / "feature.py").write_text("VALUE = 1\n")
        commit_all(builder, "builder candidate")
        tester_agent_id = register_agent(run_path, "tester")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
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

        pending_test = tester / "tests" / "test_pending.py"
        pending_test.write_text(
            "from src.calc import add\n\ndef test_pending():\n    assert add(2, 2) == 4\n"
        )
        status = run_cli("status", "--run", run_path)
        assert_status(status, "ACTIVE", rc=0)
        self.assertIn("tests/test_pending.py", status.data.get("tester_dirty_paths") or [])
        self.assertFalse(status.data.get("tester_fully_integrated"), status.data)
        self.assertFalse(status.data.get("ready_to_finalize"), status.data)

        blocked = run_cli("finalize", "--run", run_path)
        assert_status(blocked, "NEEDS_USER", rc=1)
        self.assertEqual(blocked.data.get("code"), "TESTER_DIRTY")
        self.assertTrue(pending_test.is_file())
        self.assertTrue(tester.is_dir())
        self.assertEqual(head(self.repo), self.spec_head)

    def test_unrelated_ignored_target_residue_survives_finalize(self) -> None:
        run_path, _builder, _candidate = self.ready_run()
        git_common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        if not git_common.is_absolute():
            git_common = (self.repo / git_common).resolve()
        exclude = git_common / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write("/target-local.cache\n")
        residue = self.repo / "target-local.cache"
        residue.write_text("local residue\n")
        ordinary = self.repo / "local-notes.txt"
        ordinary.write_text("ordinary residue\n")

        result = run_cli("finalize", "--run", run_path)

        assert_status(result, "COMPLETE", rc=0)
        self.assertEqual(residue.read_text(), "local residue\n")
        self.assertEqual(ordinary.read_text(), "ordinary residue\n")
        self.assertEqual(git(self.repo, "ls-files", "target-local.cache"), "")
        self.assertEqual(git(self.repo, "ls-files", "local-notes.txt"), "")
        self.assertNotEqual(head(self.repo), self.spec_head)

    def test_tracked_target_residue_stages_once_and_resumes_after_cleanup(self) -> None:
        run_path, _builder, candidate = self.ready_run()
        initially_ready = run_cli("status", "--run", run_path)
        assert_status(initially_ready, "ACTIVE", rc=0)
        self.assertIs(initially_ready.data.get("ready_to_finalize"), True)
        readme = self.repo / "README.md"
        original = readme.read_text()
        readme.write_text(original + "local target edit\n")

        git_common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        if not git_common.is_absolute():
            git_common = (self.repo / git_common).resolve()
        hook = git_common / "hooks" / "commit-msg"
        marker = git_common / "finalize-hook-ran"
        hook.write_text(
            "#!/bin/sh\n"
            f'if [ -e "{marker}" ]; then exit 23; fi\n'
            f': > "{marker}"\n'
        )
        hook.chmod(0o755)

        status = run_cli("status", "--run", run_path)
        assert_status(status, "ACTIVE", rc=0)
        self.assertIs(status.data.get("delivery_gates_ready"), True, status.data)
        self.assertIs(status.data.get("ready_to_stage_final"), True, status.data)
        self.assertIs(status.data.get("ready_to_finalize"), False, status.data)
        self.assertEqual(
            [item.get("code") for item in status.data.get("finalize_blockers", [])],
            ["TARGET_TRACKED_DIRTY"],
            status.data,
        )

        blocked = run_cli("finalize", "--run", run_path)
        assert_status(blocked, "NEEDS_USER", rc=1)
        self.assertEqual(
            blocked.data.get("code"), "FINAL_COMMIT_STAGED_TARGET_BLOCKED"
        )
        staged = blocked.data.get("staged_final_head")
        self.assertIsInstance(staged, str, blocked.data)
        self.assertEqual(head(self.repo), self.spec_head)
        self.assertTrue(marker.is_file())
        self.assertEqual(load_ledger(run_path)["finalize_intent"]["final_head"], staged)

        unchanged = run_cli("finalize", "--run", run_path)
        assert_status(unchanged, "NEEDS_USER", rc=1)
        self.assertEqual(unchanged.data.get("staged_final_head"), staged)

        staged_status = run_cli("status", "--run", run_path)
        assert_status(staged_status, "ACTIVE", rc=0)
        self.assertEqual(staged_status.data.get("staged_final_head"), staged)
        self.assertIs(staged_status.data.get("ready_to_stage_final"), False)
        self.assertIs(staged_status.data.get("ready_to_finalize"), False)

        readme.write_text(original)
        ready = run_cli("status", "--run", run_path)
        assert_status(ready, "ACTIVE", rc=0)
        self.assertIs(ready.data.get("ready_to_finalize"), True, ready.data)
        completed = run_cli("finalize", "--run", run_path)
        assert_status(completed, "COMPLETE", rc=0)
        self.assertEqual(completed.data.get("final_head"), staged)
        self.assertEqual(tree(self.repo), tree(self.repo, candidate))

    def test_untracked_file_directory_collisions_stage_without_overwrite(self) -> None:
        run_path, _builder, _candidate = self.ready_run(
            {
                "src/generated/value.txt": "generated\n",
                "src/standalone": "tracked final file\n",
            }
        )
        parent_collision = self.repo / "src" / "generated"
        parent_collision.write_text("local parent file\n")
        child_collision = self.repo / "src" / "standalone" / "local.log"
        child_collision.parent.mkdir(parents=True)
        child_collision.write_text("local child file\n")

        blocked = run_cli("finalize", "--run", run_path)

        assert_status(blocked, "NEEDS_USER", rc=1)
        self.assertEqual(
            blocked.data.get("code"), "FINAL_COMMIT_STAGED_TARGET_BLOCKED"
        )
        collision = next(
            item
            for item in blocked.data.get("finalize_blockers", [])
            if item.get("code") == "TARGET_PATH_COLLISION"
        )
        self.assertEqual(
            set(collision.get("paths") or []),
            {"src/generated", "src/standalone/local.log"},
        )
        self.assertEqual(parent_collision.read_text(), "local parent file\n")
        self.assertEqual(child_collision.read_text(), "local child file\n")
        self.assertEqual(head(self.repo), self.spec_head)

    def test_temporary_target_worktree_is_removed_after_success(self) -> None:
        git(self.repo, "branch", "release")
        started = run_cli(
            "start",
            "--repo",
            self.repo,
            "--plan",
            self.plan,
            "--task",
            "release target fixture",
            "--session-id",
            "release-session",
            "--target-branch",
            "release",
        )
        assert_status(started, "READY", rc=0)
        run_path = Path(started.data["run_path"])
        builder, _tester = worktrees_from(started, run_path)
        finalize_path = builder.parent / "finalize"
        (builder / "src" / "release_feature.py").write_text("VALUE = 1\n")
        commit_all(builder, "release candidate")
        tester_agent_id = register_agent(run_path, "tester")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
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

        completed = run_cli("finalize", "--run", run_path)
        assert_status(completed, "COMPLETE", rc=0)
        self.assertEqual(git(self.repo, "rev-parse", "release"), completed.data.get("final_head"))
        self.assertEqual(head(self.repo), self.spec_head)
        self.assertFalse(finalize_path.exists())
        self.assertNotIn(str(finalize_path), git(self.repo, "worktree", "list", "--porcelain"))

    def test_mutating_commit_hook_never_moves_target_branch(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "src" / "feature.py").write_text("VALUE = 1\n")
        commit_all(builder, "builder candidate")
        tester_agent_id = register_agent(run_path, "tester")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
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

        git_common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        if not git_common.is_absolute():
            git_common = (self.repo / git_common).resolve()
        hook = git_common / "hooks" / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "echo 'INJECTED = 1' > src/hook_injected.py\n"
            "git add src/hook_injected.py\n"
        )
        hook.chmod(0o755)
        target_before = head(self.repo)

        result = run_cli("finalize", "--run", run_path)
        assert_status(result, "CONFLICT", rc=1)
        self.assertEqual(result.data.get("code"), "FINAL_COMMIT_TREE_MISMATCH")
        self.assertEqual(head(self.repo), target_before)
        self.assertFalse((self.repo / "src" / "hook_injected.py").exists())
        self.assertTrue((builder.parent / "finalize" / "src" / "hook_injected.py").is_file())
        self.assertEqual(load_ledger(run_path).get("phase"), "finalize_conflict")
        assert_ledger_schema(run_path)

    def test_target_fast_forward_does_not_run_post_merge_hook(self) -> None:
        run_path, _builder, _candidate = self.ready_run()
        git_common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        if not git_common.is_absolute():
            git_common = (self.repo / git_common).resolve()
        hook = git_common / "hooks" / "post-merge"
        hook.write_text(
            "#!/bin/sh\n"
            "echo 'post-merge mutation' > README.md\n"
            "touch post-merge-ran\n"
        )
        hook.chmod(0o755)

        result = run_cli("finalize", "--run", run_path)
        assert_status(result, "COMPLETE", rc=0)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse((self.repo / "post-merge-ran").exists())

    def test_target_ref_rewind_before_update_fails_compare_and_swap(self) -> None:
        (self.repo / "README.md").write_text("fixture with parent\n")
        commit_all(self.repo, "add target parent for rewind")
        self.spec_head = head(self.repo)
        self.plan = write_plan(self.repo, plan_markdown(self.spec_head), name="cas-plan.md")
        rewind_head = git(self.repo, "rev-parse", f"{self.spec_head}^")
        run_path, builder, _candidate = self.ready_run()

        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_dir = self.repo / ".git" / "cas-wrapper"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        marker = wrapper_dir / "fired"
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ ! -e \"$CAS_MARKER\" ] && [ \"$3\" = update-ref ] "
            "&& [ \"$4\" = \"$CAS_REF\" ] && [ \"$6\" = \"$CAS_EXPECTED\" ]; then\n"
            "  \"$REAL_GIT\" -C \"$CAS_REPO\" reset --hard \"$CAS_REWIND\" >/dev/null\n"
            "  : > \"$CAS_MARKER\"\n"
            "fi\n"
            "exec \"$REAL_GIT\" \"$@\"\n"
        )
        wrapper.chmod(0o755)
        result = run_cli(
            "finalize",
            "--run",
            run_path,
            "--message",
            "feat: compare-and-swap fixture",
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "CAS_MARKER": str(marker),
                "CAS_REF": "refs/heads/main",
                "CAS_EXPECTED": self.spec_head,
                "CAS_REPO": str(self.repo),
                "CAS_REWIND": rewind_head,
            },
        )
        assert_status(result, "CONTINUITY_FAILURE", rc=1)
        self.assertEqual(result.data.get("code"), "FINALIZE_FAST_FORWARD_CAS_FAILED")
        self.assertTrue(marker.is_file())
        self.assertEqual(head(self.repo), rewind_head)
        self.assertTrue(builder.is_dir())

    def test_finalize_recovers_after_crash_between_cas_and_worktree_sync(self) -> None:
        run_path, builder, _candidate = self.ready_run()
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_dir = self.repo / ".git" / "intent-crash-wrapper"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        marker = wrapper_dir / "fired"
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ ! -e \"$CRASH_MARKER\" ] && [ \"$3\" = read-tree ] "
            "&& [ \"$4\" != -n ]; then\n"
            "  : > \"$CRASH_MARKER\"\n"
            "  kill -9 \"$PPID\"\n"
            "  exit 137\n"
            "fi\n"
            "exec \"$REAL_GIT\" \"$@\"\n"
        )
        wrapper.chmod(0o755)
        crashed = run_process(
            [
                sys.executable,
                CLI,
                "finalize",
                "--run",
                run_path,
                "--message",
                "feat: recover finalize intent fixture",
            ],
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "CRASH_MARKER": str(marker),
            },
        )
        self.assertNotEqual(crashed.returncode, 0)
        self.assertTrue(marker.is_file())
        interrupted = load_ledger(run_path)
        self.assertEqual(interrupted["phase"], "active")
        self.assertIsNotNone(interrupted["finalize_intent"])
        self.assertEqual(head(self.repo), interrupted["finalize_intent"]["final_head"])
        assert_status(run_cli("status", "--run", run_path), "ACTIVE", rc=0)

        recovered = run_cli(
            "finalize",
            "--run",
            run_path,
            "--message",
            "feat: recover finalize intent fixture",
        )
        assert_status(recovered, "COMPLETE", rc=0)
        self.assertEqual(head(self.repo), interrupted["finalize_intent"]["final_head"])
        self.assertFalse(builder.exists())

    def test_finalize_intent_freezes_new_agent_turns_before_cas(self) -> None:
        run_path, _builder, candidate = self.ready_run()
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_dir = self.repo / ".git" / "intent-freeze-wrapper"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        marker = wrapper_dir / "fired"
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ ! -e \"$CRASH_MARKER\" ] && [ \"$3\" = update-ref ]; then\n"
            "  : > \"$CRASH_MARKER\"\n"
            "  kill -9 \"$PPID\"\n"
            "  exit 137\n"
            "fi\n"
            "exec \"$REAL_GIT\" \"$@\"\n"
        )
        wrapper.chmod(0o755)
        crashed = run_process(
            [
                sys.executable,
                CLI,
                "finalize",
                "--run",
                run_path,
                "--message",
                "feat: freeze finalize intent fixture",
            ],
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "CRASH_MARKER": str(marker),
            },
        )
        self.assertNotEqual(crashed.returncode, 0)
        interrupted = load_ledger(run_path)
        self.assertEqual(head(self.repo), interrupted["target_start_head"])
        reviewer_id = interrupted["agents"]["reviewer"]["agent_id"]
        blocked = run_cli(
            "agent-event",
            "--repo",
            interrupted["repo_root"],
            "--session-id",
            interrupted["owner_session_id"],
            "--role",
            "reviewer",
            "--agent-id",
            reviewer_id,
            "--turn-id",
            "review-after-finalize-intent",
            "--event",
            "start",
            env={"BUILDER_LOOP_HOOK_EVENT": "1"},
        )
        assert_status(blocked, "NEEDS_USER", rc=1)
        self.assertEqual(blocked.data.get("code"), "FINALIZE_INTENT_ACTIVE")
        frozen = load_ledger(run_path)
        self.assertEqual(frozen["reviewed_head"], candidate)
        self.assertEqual(frozen["doc_reviewed_head"], candidate)

        recovered = run_cli(
            "finalize",
            "--run",
            run_path,
            "--message",
            "feat: freeze finalize intent fixture",
        )
        assert_status(recovered, "COMPLETE", rc=0)

    def test_abandon_refuses_finalize_conflict_after_target_ref_moved(self) -> None:
        run_path, _builder, _candidate = self.ready_run()
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_dir = self.repo / ".git" / "postcondition-wrapper"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$2\" = \"$TARGET_WORKTREE\" ] && [ \"$3\" = write-tree ]; then\n"
            "  echo \"$WRONG_TREE\"\n"
            "  exit 0\n"
            "fi\n"
            "exec \"$REAL_GIT\" \"$@\"\n"
        )
        wrapper.chmod(0o755)
        result = run_cli(
            "finalize",
            "--run",
            run_path,
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "TARGET_WORKTREE": str(self.repo),
                "WRONG_TREE": tree(self.repo, self.spec_head),
            },
        )
        assert_status(result, "CONFLICT", rc=1)
        self.assertEqual(
            result.data.get("code"), "FINALIZE_FAST_FORWARD_POSTCONDITION"
        )
        conflicted = load_ledger(run_path)
        intent = conflicted.get("finalize_intent")
        self.assertEqual(conflicted.get("phase"), "finalize_conflict")
        self.assertIsInstance(intent, dict)
        self.assertEqual(head(self.repo), intent.get("final_head"))

        abandoned = run_cli("abandon", "--run", run_path)
        assert_status(abandoned, "NEEDS_USER", rc=1)
        self.assertEqual(abandoned.data.get("code"), "FINALIZE_INTENT_ACTIVE")
        self.assertEqual(load_ledger(run_path).get("phase"), "finalize_conflict")
        self.assertEqual(head(self.repo), intent.get("final_head"))

    def test_hook_created_extra_commit_enters_preserved_finalize_conflict(self) -> None:
        run_path, builder, _candidate = self.ready_run()
        git_common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        if not git_common.is_absolute():
            git_common = (self.repo / git_common).resolve()
        hook = git_common / "hooks" / "post-commit"
        hook.write_text(
            "#!/bin/sh\n"
            "marker=\"$(git rev-parse --git-dir)/builder-loop-extra-once\"\n"
            "if [ ! -e \"$marker\" ]; then\n"
            "  touch \"$marker\"\n"
            "  git commit --allow-empty --no-verify -m 'extra hook commit' >/dev/null\n"
            "fi\n"
        )
        hook.chmod(0o755)

        result = run_cli("finalize", "--run", run_path)
        assert_status(result, "CONFLICT", rc=1)
        self.assertEqual(result.data.get("code"), "FINALIZE_NOT_SINGLE_COMMIT")
        self.assertEqual(load_ledger(run_path)["phase"], "finalize_conflict")
        self.assertTrue((builder.parent / "finalize").is_dir())

    def test_cleanup_retry_refuses_target_ref_rollback(self) -> None:
        run_path, builder, _candidate = self.ready_run()
        git_common = Path(git(self.repo, "rev-parse", "--git-common-dir"))
        if not git_common.is_absolute():
            git_common = (self.repo / git_common).resolve()
        hook = git_common / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\ntouch finalize-cleanup-blocker\n")
        hook.chmod(0o755)

        first = run_cli("finalize", "--run", run_path)
        assert_status(first, "NEEDS_USER", rc=1)
        self.assertEqual(first.data.get("code"), "FINALIZE_CLEANUP_INCOMPLETE")
        final_head = first.data["final_head"]
        self.assertEqual(head(self.repo), final_head)
        terminal = run_cli("abandon", "--run", run_path)
        assert_status(terminal, "NEEDS_USER", rc=1)
        self.assertEqual(terminal.data.get("code"), "RUN_TERMINAL")
        git(self.repo, "update-ref", "refs/heads/main", self.spec_head, final_head)

        retry = run_cli("finalize", "--run", run_path)
        assert_status(retry, "CONTINUITY_FAILURE", rc=1)
        self.assertEqual(retry.data.get("code"), "FINALIZED_TARGET_CONTINUITY_FAILURE")
        self.assertTrue((builder.parent / "finalize").is_dir())


if __name__ == "__main__":
    unittest.main()
