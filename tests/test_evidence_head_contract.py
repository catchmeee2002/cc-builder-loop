from __future__ import annotations

import json
import unittest

from harness import (
    assert_ledger_schema,
    assert_status,
    cleanup_repo,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    record_evidence,
    register_agent,
    run_cli,
    start_run,
    worktrees_from,
    write_plan,
)


class EvidenceHeadContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.plan = write_plan(
            self.repo,
            plan_markdown(head(self.repo), include_e2e=True),
        )

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_candidate_change_invalidates_all_non_machine_evidence(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)

        tester_agent_id = register_agent(run_path, "tester")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
        verified = run_cli("verify", "--run", run_path)
        assert_status(verified, "PASS", rc=0)
        first_candidate = head(builder)
        self.assertEqual(verified.data.get("head"), first_candidate)
        register_agent(run_path, "tester", agent_id=tester_agent_id, result="pass")
        record_evidence(
            run_path,
            "e2e_verified",
            first_candidate,
            agent_id=tester_agent_id,
        )
        reviewer_agent_id = register_agent(run_path, "reviewer")

        for kind in ("reviewed", "doc_reviewed"):
            recorded = record_evidence(
                run_path,
                kind,
                first_candidate,
                agent_id=reviewer_agent_id,
            )
            self.assertEqual(recorded.data.get("head"), first_candidate)
            self.assertEqual(recorded.data.get("candidate_head"), first_candidate)

        ready = run_cli("status", "--run", run_path)
        assert_status(ready, "ACTIVE", rc=0)
        self.assertTrue(ready.data.get("ready_to_finalize"), ready.data)
        self.assertEqual(ready.data.get("candidate_head"), first_candidate)

        (builder / "src" / "after_review.py").write_text("VALUE = 2\n")
        reverified = run_cli("verify", "--run", run_path)
        assert_status(reverified, "PASS", rc=0)
        second_candidate = head(builder)
        self.assertNotEqual(second_candidate, first_candidate)
        self.assertEqual(reverified.data.get("head"), second_candidate)

        old_turn = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "reviewed",
            "--head",
            second_candidate,
            "--agent-id",
            reviewer_agent_id,
        )
        assert_status(old_turn, "FATAL", rc=2)
        self.assertEqual(old_turn.data.get("code"), "EVIDENCE_AGENT_HEAD_MISMATCH")

        stale_record = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "reviewed",
            "--head",
            first_candidate,
            "--agent-id",
            reviewer_agent_id,
        )
        assert_status(stale_record, "FATAL", rc=2)
        self.assertEqual(stale_record.data.get("code"), "EVIDENCE_HEAD_MISMATCH")

        ledger = load_ledger(run_path)
        self.assertEqual(
            ledger["evidence"]["machine"]["accepted_head"], second_candidate
        )
        self.assertIsNone(ledger["evidence"]["blackbox"], ledger)
        self.assertIsNone(ledger["evidence"]["review"], ledger)
        self.assertIsNone(ledger["evidence"]["doc_review"], ledger)

        stale = run_cli("status", "--run", run_path)
        assert_status(stale, "ACTIVE", rc=0)
        self.assertEqual(stale.data.get("candidate_head"), second_candidate)
        missing = set(stale.data.get("missing_gates") or [])
        self.assertTrue(
            {"e2e_verified_head", "reviewed_head", "doc_reviewed_head"}.issubset(missing),
            stale.data,
        )
        self.assertFalse(stale.data.get("ready_to_finalize"), stale.data)
        assert_ledger_schema(run_path)

    def test_non_passing_agent_result_cannot_be_recorded_as_evidence(self) -> None:
        _started, run_path = start_run(self.repo, self.plan)
        candidate = head(self.repo)
        tester_agent_id = register_agent(run_path, "tester", result="fail")
        tester = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "e2e_verified",
            "--head",
            candidate,
            "--agent-id",
            tester_agent_id,
        )
        assert_status(tester, "FATAL", rc=2)
        self.assertEqual(tester.data.get("code"), "EVIDENCE_ROLE_RESULT_NOT_PASS")

        reviewer_agent_id = register_agent(run_path, "reviewer", result="findings")
        reviewer = run_cli(
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
        assert_status(reviewer, "FATAL", rc=2)
        self.assertEqual(reviewer.data.get("code"), "EVIDENCE_ROLE_RESULT_NOT_PASS")

    def test_new_agent_turn_invalidates_role_evidence(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)
        tester_agent_id = register_agent(run_path, "tester")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        candidate = head(builder)
        register_agent(run_path, "tester", agent_id=tester_agent_id, result="pass")
        record_evidence(
            run_path, "e2e_verified", candidate, agent_id=tester_agent_id
        )
        reviewer_agent_id = register_agent(run_path, "reviewer")
        record_evidence(run_path, "reviewed", candidate, agent_id=reviewer_agent_id)
        record_evidence(run_path, "doc_reviewed", candidate, agent_id=reviewer_agent_id)

        register_agent(
            run_path,
            "reviewer",
            agent_id=reviewer_agent_id,
            result="findings",
        )
        after_findings = load_ledger(run_path)
        self.assertIsNone(after_findings["evidence"]["review"])
        self.assertIsNone(after_findings["evidence"]["doc_review"])
        self.assertEqual(
            after_findings["evidence"]["blackbox"]["accepted_head"], candidate
        )

        register_agent(run_path, "tester", agent_id=tester_agent_id, result="fail")
        after_failure = load_ledger(run_path)
        self.assertIsNone(after_failure["evidence"]["blackbox"])
        self.assertFalse(run_cli("status", "--run", run_path).data.get("ready_to_finalize"))

    def test_blackbox_evidence_requires_author_integration_and_replay_details(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)
        candidate = head(builder)
        tester_agent_id = register_agent(run_path, "tester", result="pass")
        no_author = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "e2e_verified",
            "--head",
            candidate,
            "--agent-id",
            tester_agent_id,
            "--details",
            json.dumps(
                {
                    "candidate_worktree": str(builder),
                    "head_before": candidate,
                    "head_after": candidate,
                    "command": "fixture blackbox",
                    "returncode": 0,
                }
            ),
        )
        assert_status(no_author, "FATAL", rc=2)
        self.assertEqual(no_author.data.get("code"), "E2E_AUTHOR_INTEGRATION_MISSING")

        register_agent(run_path, "tester", agent_id=tester_agent_id, result="tests_ready")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
        register_agent(run_path, "tester", agent_id=tester_agent_id, result="pass")
        missing_details = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "e2e_verified",
            "--head",
            candidate,
            "--agent-id",
            tester_agent_id,
        )
        assert_status(missing_details, "FATAL", rc=2)
        self.assertEqual(missing_details.data.get("code"), "E2E_DETAILS_REQUIRED")

        boolean_returncode = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "e2e_verified",
            "--head",
            candidate,
            "--agent-id",
            tester_agent_id,
            "--details",
            json.dumps(
                {
                    "candidate_worktree": str(builder),
                    "head_before": candidate,
                    "head_after": candidate,
                    "command": "fixture blackbox",
                    "returncode": False,
                }
            ),
        )
        assert_status(boolean_returncode, "FATAL", rc=2)
        self.assertEqual(boolean_returncode.data.get("code"), "E2E_DETAILS_INVALID")

    def test_prepared_tester_follow_up_records_blackbox_pass_without_start_hook(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)
        tester_agent_id = register_agent(run_path, "tester", result="tests_ready")
        assert_status(run_cli("integrate-tests", "--run", run_path), "NOOP", rc=0)
        verified = run_cli("verify", "--run", run_path)
        assert_status(verified, "PASS", rc=0)
        candidate = head(builder)

        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "tester",
            "--agent-id",
            tester_agent_id,
            "--purpose",
            "blackbox",
        )
        assert_status(prepared, "READY", rc=0)
        repeated = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "tester",
            "--agent-id",
            tester_agent_id,
            "--purpose",
            "blackbox",
        )
        assert_status(repeated, "NOOP", rc=0)
        self.assertEqual(repeated.data["dispatch_id"], prepared.data["dispatch_id"])
        pending = load_ledger(run_path)["pending_agent_turns"]["tester"]
        self.assertEqual(pending["purpose"], "blackbox")

        completed = run_cli(
            "agent-event",
            "--repo",
            self.repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            tester_agent_id,
            "--turn-id",
            "tester-blackbox-follow-up",
            "--event",
            "idle",
            "--result",
            "pass",
            env={"BUILDER_LOOP_HOOK_EVENT": "1"},
        )
        assert_status(completed, "READY", rc=0)
        ledger = load_ledger(run_path)
        self.assertEqual(ledger["agents"]["tester"]["result"], "pass")
        self.assertEqual(
            ledger["agents"]["tester"]["turn_id"], "tester-blackbox-follow-up"
        )
        self.assertEqual(
            ledger["agents"]["tester"]["follow_up_dispatch_id"],
            prepared.data["dispatch_id"],
        )
        self.assertIsNone(ledger["pending_agent_turns"]["tester"])

        recorded = record_evidence(
            run_path,
            "e2e_verified",
            candidate,
            agent_id=tester_agent_id,
        )
        assert_status(recorded, "READY", rc=0)
        assert_ledger_schema(run_path)


if __name__ == "__main__":
    unittest.main()
