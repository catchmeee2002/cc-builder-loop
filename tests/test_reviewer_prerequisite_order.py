from __future__ import annotations

import unittest
from pathlib import Path

from harness import (
    assert_ledger_schema,
    assert_status,
    cleanup_repo,
    commit_all,
    head,
    init_repo,
    l1_plan_markdown,
    load_ledger,
    plan_markdown,
    record_evidence,
    register_agent,
    run_cli,
    start_run,
    worktrees_from,
    write_plan,
)


def agent_event(
    run_path: Path,
    *,
    role: str,
    agent_id: str,
    turn_id: str,
    event: str,
    result: str | None = None,
):
    ledger = load_ledger(run_path)
    argv: list[str | Path] = [
        "agent-event",
        "--repo",
        Path(str(ledger["repo_root"])),
        "--session-id",
        str(ledger["owner_session_id"]),
        "--role",
        role,
        "--agent-id",
        agent_id,
        "--turn-id",
        turn_id,
        "--event",
        event,
    ]
    if result is not None:
        argv.extend(["--result", result])
    return run_cli(*argv, env={"BUILDER_LOOP_HOOK_EVENT": "1"})


class ReviewerPrerequisiteOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.repos = [self.repo]

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)

    def start_l2(self) -> tuple[Path, Path]:
        plan = write_plan(self.repo, plan_markdown(head(self.repo)))
        started, run_path = start_run(self.repo, plan)
        builder, _tester = worktrees_from(started, run_path)
        return run_path, builder

    def prepare_non_l1_gates(self, run_path: Path, builder: Path) -> tuple[str, str]:
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
        return tester_agent_id, candidate

    def test_review_started_before_verification_cannot_be_blessed_afterward(self) -> None:
        run_path, builder = self.start_l2()
        reviewer_agent_id = "fixture-reviewer-agent"

        started = agent_event(
            run_path,
            role="reviewer",
            agent_id=reviewer_agent_id,
            turn_id="review-turn-before-gates",
            event="start",
        )
        assert_status(started, "READY", rc=0)
        self.assertFalse(started.data["review_prerequisites"]["start"]["satisfied"])
        assert_ledger_schema(run_path)

        _tester_agent_id, candidate = self.prepare_non_l1_gates(run_path, builder)
        completed = agent_event(
            run_path,
            role="reviewer",
            agent_id=reviewer_agent_id,
            turn_id="review-turn-before-gates",
            event="idle",
            result="pass",
        )
        assert_status(completed, "NEEDS_USER", rc=1)
        self.assertEqual(
            completed.data.get("code"), "REVIEW_PREREQUISITES_NOT_BOUND"
        )
        binding = completed.data["review_prerequisites"]
        self.assertFalse(binding["bound"], binding)
        self.assertFalse(binding["start"]["satisfied"], binding)
        self.assertTrue(binding["completion"]["satisfied"], binding)
        assert_ledger_schema(run_path)

        stale_pass = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "reviewed",
            "--head",
            candidate,
            "--agent-id",
            reviewer_agent_id,
        )
        assert_status(stale_pass, "FATAL", rc=2)
        self.assertEqual(
            stale_pass.data.get("code"), "REVIEW_PREREQUISITES_NOT_BOUND"
        )

        for event, result in (("start", None), ("idle", "pass")):
            rereviewed = agent_event(
                run_path,
                role="reviewer",
                agent_id=reviewer_agent_id,
                turn_id="review-turn-after-gates",
                event=event,
                result=result,
            )
            assert_status(rereviewed, "READY", rc=0)
        self.assertTrue(rereviewed.data["review_prerequisites"]["bound"])
        record_evidence(
            run_path, "reviewed", candidate, agent_id=reviewer_agent_id
        )
        record_evidence(
            run_path, "doc_reviewed", candidate, agent_id=reviewer_agent_id
        )
        assert_ledger_schema(run_path)

    def test_review_completion_must_still_see_verified_candidate(self) -> None:
        run_path, builder = self.start_l2()
        tester_agent_id, candidate = self.prepare_non_l1_gates(run_path, builder)
        reviewer_agent_id = "fixture-reviewer-agent"

        started = agent_event(
            run_path,
            role="reviewer",
            agent_id=reviewer_agent_id,
            turn_id="review-turn-loses-gate",
            event="start",
        )
        assert_status(started, "READY", rc=0)
        self.assertTrue(started.data["review_prerequisites"]["start"]["satisfied"])

        register_agent(run_path, "tester", agent_id=tester_agent_id, result="fail")
        completed = agent_event(
            run_path,
            role="reviewer",
            agent_id=reviewer_agent_id,
            turn_id="review-turn-loses-gate",
            event="idle",
            result="pass",
        )
        assert_status(completed, "NEEDS_USER", rc=1)
        self.assertEqual(
            completed.data.get("code"), "REVIEW_PREREQUISITES_NOT_BOUND"
        )
        binding = completed.data["review_prerequisites"]
        self.assertTrue(binding["start"]["satisfied"], binding)
        self.assertFalse(binding["completion"]["satisfied"], binding)
        self.assertFalse(binding["bound"], binding)
        assert_ledger_schema(run_path)

        register_agent(run_path, "tester", agent_id=tester_agent_id, result="pass")
        record_evidence(
            run_path,
            "e2e_verified",
            candidate,
            agent_id=tester_agent_id,
        )
        supplemented = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "doc_reviewed",
            "--head",
            candidate,
            "--agent-id",
            reviewer_agent_id,
        )
        assert_status(supplemented, "FATAL", rc=2)
        self.assertEqual(
            supplemented.data.get("code"), "REVIEW_PREREQUISITES_NOT_BOUND"
        )

        register_agent(run_path, "reviewer", agent_id=reviewer_agent_id)
        record_evidence(
            run_path, "reviewed", candidate, agent_id=reviewer_agent_id
        )
        assert_ledger_schema(run_path)

    def test_l1_review_binds_candidate_and_document_paths_without_tester(self) -> None:
        plan = write_plan(self.repo, l1_plan_markdown(head(self.repo)))
        started, run_path = start_run(self.repo, plan, task="L1 review ordering")
        builder, _tester = worktrees_from(started, run_path)
        (builder / "README.md").write_text("fixture documentation update\n")
        commit_all(builder, "update documentation")
        candidate = head(builder)

        reviewer_agent_id = register_agent(run_path, "reviewer")
        ledger = load_ledger(run_path)
        reviewer = ledger["agents"]["reviewer"]
        binding = reviewer["review_prerequisites"]
        self.assertTrue(binding["bound"], binding)
        for snapshot in (binding["start"], binding["completion"]):
            self.assertEqual(snapshot["plan_level"], "L1")
            self.assertEqual(snapshot["candidate_head"], candidate)
            self.assertEqual(
                snapshot["documentation_paths"], ledger["plan"]["builder_write"]
            )
            self.assertFalse(snapshot["tester_integration_completed"])
            self.assertIsNone(snapshot["verified_head"])
            self.assertIsNone(snapshot["e2e_verified_head"])
            self.assertTrue(snapshot["satisfied"])

        record_evidence(
            run_path, "reviewed", candidate, agent_id=reviewer_agent_id
        )
        record_evidence(
            run_path, "doc_reviewed", candidate, agent_id=reviewer_agent_id
        )
        status = run_cli("status", "--run", run_path)
        assert_status(status, "ACTIVE", rc=0)
        self.assertTrue(status.data.get("ready_to_finalize"), status.data)
        assert_ledger_schema(run_path)


if __name__ == "__main__":
    unittest.main()
