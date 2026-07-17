from __future__ import annotations

import os
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
    record_evidence,
    register_agent,
    run_cli,
    start_agent_turn,
    start_run,
    worktrees_from,
    write_plan,
)


class SerialPrerequisiteContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.spec_head = head(self.repo)
        self.plan = write_plan(
            self.repo,
            plan_markdown(self.spec_head, parallel_ready=False),
        )

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def start(self):
        started, run_path = start_run(self.repo, self.plan, task="serial-prerequisite")
        builder, tester = worktrees_from(started, run_path)
        return started, run_path, builder, tester

    def publish(self, run_path: Path, builder: Path, content: str = "API_VERSION = 1\n"):
        (builder / "src" / "public_api.py").write_text(content)
        result = run_cli("publish-prerequisites", "--run", run_path)
        assert_status(result, "READY", rc=0)
        return result

    def test_serial_tester_cannot_start_before_publication(self) -> None:
        _started, run_path, _builder, _tester = self.start()
        ledger = load_ledger(run_path)
        result = run_cli(
            "agent-event",
            "--repo",
            ledger["repo_root"],
            "--session-id",
            ledger["owner_session_id"],
            "--role",
            "tester",
            "--agent-id",
            "serial-tester",
            "--turn-id",
            "serial-turn-1",
            "--event",
            "start",
            env={"BUILDER_LOOP_HOOK_EVENT": "1"},
        )
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "PREREQUISITES_NOT_PUBLISHED")

    def test_publication_uses_isolated_head_and_binds_tester_author(self) -> None:
        _started, run_path, builder, tester = self.start()
        published = self.publish(run_path, builder)
        publication_head = str(published.data["head"])
        builder_head = str(published.data["builder_head"])

        self.assertEqual(head(tester), publication_head)
        self.assertEqual(git(tester, "rev-parse", f"{publication_head}^"), self.spec_head)
        self.assertEqual(
            git(tester, "diff", "--name-only", self.spec_head, publication_head),
            "src/public_api.py",
        )
        self.assertNotEqual(publication_head, builder_head)

        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")
        (tester / "tests" / "test_public_api.py").write_text(
            "from src.public_api import API_VERSION\n\n"
            "def test_public_api_version():\n"
            "    assert API_VERSION == 1\n"
        )
        commit_all(tester, "test published public API")
        finish_agent_turn(
            run_path,
            "tester",
            agent_id=tester_agent_id,
            turn_id=tester_turn_id,
            result="tests_ready",
        )
        integrated = run_cli("integrate-tests", "--run", run_path)
        assert_status(integrated, "READY", rc=0)
        ledger = load_ledger(run_path)
        self.assertEqual(
            ledger["tester_integration"]["author_prerequisite_manifest_sha256"],
            ledger["prerequisite_publication"]["manifest_sha256"],
        )
        assert_ledger_schema(run_path)

    def test_builder_history_is_not_in_tester_publication_ancestry(self) -> None:
        _started, run_path, builder, tester = self.start()
        (builder / "src" / "secret_implementation.py").write_text("SECRET = 1\n")
        commit_all(builder, "temporary private implementation")
        (builder / "src" / "secret_implementation.py").unlink()
        commit_all(builder, "remove temporary private implementation")

        published = self.publish(run_path, builder)
        publication_head = str(published.data["head"])
        ancestry = git(tester, "rev-list", "--parents", "-n", "1", publication_head).split()
        self.assertEqual(ancestry, [publication_head, self.spec_head])
        self.assertNotIn(
            "secret_implementation.py",
            git(tester, "log", "--name-only", "--format=", publication_head),
        )

    def test_published_files_are_immutable_for_the_builder(self) -> None:
        _started, run_path, builder, _tester = self.start()
        self.publish(run_path, builder)
        (builder / "src" / "public_api.py").write_text("API_VERSION = 2\n")
        commit_all(builder, "attempt to change published API")

        result = run_cli("role-check", "--run", run_path, "--role", "builder")
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertTrue(
            any(
                item.get("kind") == "published_prerequisite_changed"
                for item in result.data.get("violations", [])
            ),
            result.data,
        )

    def test_existing_tester_commit_is_preserved_and_rejected(self) -> None:
        _started, run_path, builder, tester = self.start()
        (tester / "tests" / "early.py").write_text("VALUE = 1\n")
        tester_head = commit_all(tester, "early tester commit")
        (builder / "src" / "public_api.py").write_text("API_VERSION = 1\n")

        result = run_cli("publish-prerequisites", "--run", run_path)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "TESTER_AUTHOR_BASELINE_MISMATCH")
        self.assertEqual(head(tester), tester_head)
        self.assertTrue((tester / "tests" / "early.py").is_file())

    def test_symlink_public_prerequisite_is_rejected(self) -> None:
        _started, run_path, builder, tester = self.start()
        link = builder / "src" / "public_api.py"
        os.symlink("calc.py", link)

        result = run_cli("publish-prerequisites", "--run", run_path)
        assert_status(result, "NEEDS_USER", rc=1)
        self.assertEqual(result.data.get("code"), "PREREQUISITE_FILE_INVALID")
        self.assertEqual(head(tester), self.spec_head)

    def test_reviewer_snapshot_binds_serial_publication(self) -> None:
        _started, run_path, builder, tester = self.start()
        published = self.publish(run_path, builder)
        tester_agent_id, tester_turn_id = start_agent_turn(run_path, "tester")
        (tester / "tests" / "test_public_api.py").write_text(
            "from src.public_api import API_VERSION\n\n"
            "def test_public_api_version():\n"
            "    assert API_VERSION == 1\n"
        )
        commit_all(tester, "test serial publication")
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
        register_agent(run_path, "tester", agent_id=tester_agent_id, result="pass")
        record_evidence(
            run_path, "e2e_verified", candidate, agent_id=tester_agent_id
        )
        reviewer_id = register_agent(run_path, "reviewer")
        reviewer = load_ledger(run_path)["agents"]["reviewer"]
        for snapshot in (
            reviewer["review_prerequisites"]["start"],
            reviewer["review_prerequisites"]["completion"],
        ):
            self.assertTrue(snapshot["prerequisite_required"])
            self.assertTrue(snapshot["prerequisite_bound"])
            self.assertEqual(snapshot["prerequisite_head"], published.data["head"])
            self.assertEqual(
                snapshot["prerequisite_manifest_sha256"],
                published.data["manifest_sha256"],
            )
        record_evidence(run_path, "reviewed", candidate, agent_id=reviewer_id)
        assert_ledger_schema(run_path)


if __name__ == "__main__":
    unittest.main()
