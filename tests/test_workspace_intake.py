from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

from harness import (
    assert_status,
    cleanup_repo,
    git,
    head,
    init_repo,
    plan_markdown,
    record_evidence,
    register_agent,
    run_process,
    run_cli,
    start_run,
    worktrees_from,
    write_plan,
)


class WorkspaceIntakeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def intake_plan(self, paths: list[str], *, include_e2e: bool = False) -> Path:
        scanned = run_cli(
            "workspace-scan",
            "--repo",
            self.repo,
            *[value for path in paths for value in ("--path", path)],
        )
        assert_status(scanned, "READY", rc=0)
        marker = [
            {"path": item["path"], "state_sha256": item["state_sha256"]}
            for item in scanned.data["entries"]
        ]
        return write_plan(
            self.repo,
            plan_markdown(
                head(self.repo),
                builder_write=["src/**", "docs/**"],
                include_e2e=include_e2e,
                workspace_intake=marker,
            ),
        )

    def bind_delivery_evidence(self, run_path: Path, builder: Path) -> str:
        tester_id = register_agent(run_path, "tester")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        candidate = head(builder)
        register_agent(run_path, "tester", agent_id=tester_id, result="pass")
        record_evidence(run_path, "e2e_verified", candidate, agent_id=tester_id)
        reviewer_id = register_agent(run_path, "reviewer")
        record_evidence(run_path, "reviewed", candidate, agent_id=reviewer_id)
        record_evidence(run_path, "doc_reviewed", candidate, agent_id=reviewer_id)
        return candidate

    def test_snapshot_injects_exact_dirty_paths_without_changing_target(self) -> None:
        calc = self.repo / "src" / "calc.py"
        calc.write_text("def add(a, b):\n    return a + b + 1\n")
        added = self.repo / "src" / "local.py"
        added.write_text("VALUE = 7\n")
        before_status = git(self.repo, "status", "--porcelain=v1")
        plan = self.intake_plan(["src/calc.py", "src/local.py"])

        started, run_path = start_run(self.repo, plan)
        builder, tester = worktrees_from(started, run_path)

        self.assertNotEqual(head(builder), head(self.repo))
        self.assertEqual(head(tester), head(self.repo))
        self.assertEqual((builder / "src" / "calc.py").read_text(), calc.read_text())
        self.assertEqual((builder / "src" / "local.py").read_text(), added.read_text())
        self.assertEqual(git(self.repo, "status", "--porcelain=v1"), before_status)
        intake = started.data["workspace_intake"]
        self.assertIs(intake["required"], True)
        self.assertEqual(set(intake["paths"]), {"src/calc.py", "src/local.py"})

        abandoned = run_cli("abandon", "--run", run_path, "--reason", "fixture")
        assert_status(abandoned, "COMPLETE", rc=0)
        self.assertEqual(git(self.repo, "status", "--porcelain=v1"), before_status)

    def test_planning_digest_drift_and_unsafe_entries_are_rejected(self) -> None:
        calc = self.repo / "src" / "calc.py"
        calc.write_text("first dirty version\n")
        plan = self.intake_plan(["src/calc.py"])
        calc.write_text("second dirty version\n")

        drift = run_cli("plan-validate", "--repo", self.repo, "--plan", plan)
        assert_status(drift, "NEEDS_USER", rc=1)
        self.assertEqual(drift.data.get("code"), "WORKSPACE_INTAKE_DRIFT")

        ignored = self.repo / "secret.cache"
        (self.repo / ".gitignore").write_text("secret.cache\n")
        ignored.write_text("secret\n")
        rejected = run_cli(
            "workspace-scan", "--repo", self.repo, "--path", "secret.cache"
        )
        assert_status(rejected, "NEEDS_USER", rc=1)
        self.assertEqual(rejected.data.get("code"), "WORKSPACE_IGNORED_PATH")

        link = self.repo / "src" / "link.py"
        link.symlink_to(calc)
        symlink = run_cli(
            "workspace-scan", "--repo", self.repo, "--path", "src/link.py"
        )
        assert_status(symlink, "NEEDS_USER", rc=1)
        self.assertEqual(symlink.data.get("code"), "WORKSPACE_ENTRY_NOT_REGULAR")

    def test_finalize_consumes_unchanged_intake_and_preserves_unrelated_residue(self) -> None:
        calc = self.repo / "src" / "calc.py"
        calc.write_text("# local intake\ndef add(a, b):\n    return a + b\n")
        plan = self.intake_plan(["src/calc.py"], include_e2e=True)
        started, run_path = start_run(self.repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "src" / "feature.py").write_text("VALUE = 1\n")

        self.bind_delivery_evidence(run_path, builder)
        unrelated = self.repo / "local.log"
        unrelated.write_text("keep me\n")

        finalized = run_cli("finalize", "--run", run_path, "--message", "feat: intake")
        assert_status(finalized, "COMPLETE", rc=0)
        self.assertTrue((self.repo / "src" / "feature.py").is_file())
        self.assertEqual(unrelated.read_text(), "keep me\n")

    def test_finalize_blocks_when_captured_path_changes_during_run(self) -> None:
        calc = self.repo / "src" / "calc.py"
        calc.write_text("# local intake\ndef add(a, b):\n    return a + b\n")
        plan = self.intake_plan(["src/calc.py"], include_e2e=True)
        started, run_path = start_run(self.repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        self.bind_delivery_evidence(run_path, builder)
        calc.write_text("changed after start\n")

        blocked = run_cli("finalize", "--run", run_path, "--message", "feat: intake")
        assert_status(blocked, "NEEDS_USER", rc=1)
        self.assertEqual(blocked.data.get("code"), "FINAL_COMMIT_STAGED_TARGET_BLOCKED")
        self.assertIn(
            "TARGET_INTAKE_DRIFT",
            [item.get("code") for item in blocked.data.get("finalize_blockers", [])],
        )

    def test_finalize_removes_authorized_untracked_file_deleted_by_builder(self) -> None:
        local = self.repo / "src" / "temporary.py"
        local.write_text("VALUE = 1\n")
        plan = self.intake_plan(["src/temporary.py"], include_e2e=True)
        started, run_path = start_run(self.repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "src" / "temporary.py").unlink()
        self.bind_delivery_evidence(run_path, builder)

        finalized = run_cli(
            "finalize", "--run", run_path, "--message", "fix: delete intake"
        )
        assert_status(finalized, "COMPLETE", rc=0)
        self.assertFalse(local.exists())

    def test_intake_finalize_recovers_after_crash_between_cas_and_sync(self) -> None:
        calc = self.repo / "src" / "calc.py"
        calc.write_text("# local intake\ndef add(a, b):\n    return a + b\n")
        plan = self.intake_plan(["src/calc.py"], include_e2e=True)
        started, run_path = start_run(self.repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "src" / "feature.py").write_text("VALUE = 1\n")
        self.bind_delivery_evidence(run_path, builder)

        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        wrapper_dir = self.repo / ".git" / "intake-intent-crash-wrapper"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        marker = wrapper_dir / "fired"
        wrapper = wrapper_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ ! -e \"$CRASH_MARKER\" ] && [ \"$3\" = reset ] "
            "&& [ \"$4\" = --hard ]; then\n"
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
                str(Path(__file__).parents[1] / "scripts" / "codex-builder-loop.py"),
                "finalize",
                "--run",
                run_path,
                "--message",
                "feat: recover intake intent fixture",
            ],
            env={
                "PATH": f"{wrapper_dir}:{os.environ.get('PATH', '')}",
                "REAL_GIT": str(real_git),
                "CRASH_MARKER": str(marker),
            },
        )
        self.assertNotEqual(crashed.returncode, 0)
        self.assertTrue(marker.is_file())

        recovered = run_cli(
            "finalize",
            "--run",
            run_path,
            "--message",
            "feat: recover intake intent fixture",
        )
        assert_status(recovered, "COMPLETE", rc=0)
        self.assertEqual(calc.read_text(), "# local intake\ndef add(a, b):\n    return a + b\n")
        self.assertTrue((self.repo / "src" / "feature.py").is_file())


if __name__ == "__main__":
    unittest.main()
