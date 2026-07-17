from __future__ import annotations

import unittest

from harness import (
    assert_status,
    cleanup_repo,
    commit_all,
    head,
    init_repo,
    plan_markdown,
    record_evidence,
    register_agent,
    run_cli,
    start_run,
    worktrees_from,
    write_plan,
)


class DocAuditGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo({"docs/api.md": "add(a, b) returns a sum.\n"})
        self.spec_head = head(self.repo)
        self.plan = write_plan(self.repo, plan_markdown(self.spec_head))

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_stale_doc_review_head_blocks_finalize_until_re_reviewed(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)
        (builder / "src" / "feature.py").write_text("ENABLED = True\n")
        commit_all(builder, "add behavior requiring doc audit")

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
        tests_integration = run_cli("integrate-tests", "--run", run_path)
        self.assertIn(tests_integration.data.get("status"), {"READY", "NOOP"})
        verified = run_cli("verify", "--run", run_path)
        assert_status(verified, "PASS", rc=0)
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
        stale_doc = run_cli(
            "record-evidence",
            "--run",
            run_path,
            "--kind",
            "doc_reviewed",
            "--head",
            self.spec_head,
            "--agent-id",
            reviewer_agent_id,
        )
        assert_status(stale_doc, "FATAL", rc=2)
        self.assertEqual(stale_doc.data.get("code"), "EVIDENCE_HEAD_MISMATCH")

        status = run_cli("status", "--run", run_path)
        assert_status(status, "ACTIVE", rc=0)
        missing = status.data.get("missing_gates") or status.data.get("stale_gates") or []
        self.assertTrue(any("doc" in str(item).lower() for item in missing), status.data)

        blocked = run_cli("finalize", "--run", run_path)
        assert_status(blocked, "NEEDS_USER")
        self.assertEqual(head(self.repo), self.spec_head)
        self.assertTrue(builder.is_dir())

        record_evidence(
            run_path,
            "doc_reviewed",
            candidate,
            agent_id=reviewer_agent_id,
        )
        completed = run_cli("finalize", "--run", run_path)
        assert_status(completed, "COMPLETE", rc=0)


if __name__ == "__main__":
    unittest.main()
