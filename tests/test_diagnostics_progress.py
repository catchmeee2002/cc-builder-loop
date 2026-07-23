from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness import (
    assert_ledger_schema,
    assert_status,
    cleanup_repo,
    commit_all,
    git,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    run_cli,
    start_run,
    worktrees_from,
    write_plan,
)


class DiagnosticsAndProgressContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def failing_run(self):
        repo = init_repo(
            {
                "verify.sh": (
                    "#!/usr/bin/env bash\n"
                    "echo 'stable failure signature' >&2\n"
                    "exit 7\n"
                )
            }
        )
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        return repo, builder, run_path

    def test_same_candidate_stops_after_two_failed_attempts_and_can_resume(self) -> None:
        _repo, _builder, run_path = self.failing_run()
        first = run_cli("verify", "--run", run_path)
        assert_status(first, "FAIL", rc=1)
        second = run_cli("verify", "--run", run_path)
        assert_status(second, "FAIL", rc=1)
        self.assertEqual(second.data.get("progress_stop"), "NO_PROGRESS")

        status = run_cli("status", "--run", run_path)
        assert_status(status, "NEEDS_USER", rc=1)
        self.assertEqual(status.data.get("code"), "NO_PROGRESS")
        resumed = run_cli(
            "resume", "--run", run_path, "--reason", "confirmed transient environment"
        )
        assert_status(resumed, "READY", rc=0)
        self.assertEqual(run_cli("status", "--run", run_path).data.get("phase"), "active")

    def test_same_failure_across_three_candidates_requires_architecture_review(self) -> None:
        _repo, builder, run_path = self.failing_run()
        for index in range(3):
            if index:
                (builder / "src" / f"attempt_{index}.py").write_text(f"VALUE = {index}\n")
                commit_all(builder, f"candidate {index}")
            failed = run_cli("verify", "--run", run_path)
            assert_status(failed, "FAIL", rc=1)
        self.assertEqual(
            failed.data.get("progress_stop"), "ARCHITECTURE_REVIEW_REQUIRED"
        )
        ledger = load_ledger(run_path)
        self.assertEqual(ledger["phase"], "architecture_review_required")
        fingerprints = {
            item.get("failure_fingerprint")
            for item in ledger["verification"]["attempts"]
        }
        self.assertEqual(len(fingerprints), 1)

    def test_doctor_is_read_only_and_reports_orphan_loop_worktree(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        orphan = repo.parent / f"{repo.name}-orphan"
        git(repo, "worktree", "add", "-q", "-b", "codex-loop/orphan/builder", str(orphan))
        before = git(repo, "worktree", "list", "--porcelain")

        diagnosed = run_cli("doctor", "--repo", repo)
        assert_status(diagnosed, "NEEDS_USER", rc=1)
        self.assertIn(
            "ORPHAN_LOOP_WORKTREE",
            [item.get("code") for item in diagnosed.data.get("issues", [])],
        )
        self.assertIs(diagnosed.data.get("read_only"), True)
        self.assertEqual(git(repo, "worktree", "list", "--porcelain"), before)
        git(repo, "worktree", "remove", "--force", str(orphan))
        git(repo, "branch", "-D", "codex-loop/orphan/builder")

    def test_abandoned_terminal_run_can_be_safely_cleaned(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        started, run_path = start_run(repo, plan)
        builder, tester = worktrees_from(started, run_path)
        assert_status(
            run_cli("abandon", "--run", run_path, "--reason", "fixture"),
            "COMPLETE",
            rc=0,
        )
        cleaned = run_cli("cleanup", "--run", run_path)
        assert_status(cleaned, "COMPLETE", rc=0)
        self.assertFalse(builder.exists())
        self.assertFalse(tester.exists())

    def test_v1_ledger_is_atomically_migrated_on_next_write(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, run_path = start_run(repo, plan)
        path = run_path / "ledger.json"
        ledger = json.loads(path.read_text())
        ledger["schema_version"] = 1
        ledger["verification_attempts"] = len(ledger.pop("verification")["attempts"])
        evidence = ledger.pop("evidence")
        mapping = {
            "verified_head": "machine",
            "e2e_verified_head": "blackbox",
            "reviewed_head": "review",
            "doc_reviewed_head": "doc_review",
        }
        for old, key in mapping.items():
            record = evidence.get(key)
            ledger[old] = record.get("accepted_head") if isinstance(record, dict) else None
        ledger.pop("workspace_intake")
        ledger["plan"].pop("workspace_intake")
        ledger["plan"].pop("evidence_scopes")
        path.write_text(json.dumps(ledger))

        verified = run_cli("verify", "--run", run_path)
        assert_status(verified, "PASS", rc=0)
        migrated = load_ledger(run_path)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertTrue(
            any(item.get("type") == "ledger_migrated" for item in migrated["events"])
        )
        assert_ledger_schema(run_path)


if __name__ == "__main__":
    unittest.main()
