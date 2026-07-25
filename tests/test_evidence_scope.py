from __future__ import annotations

import sys
import unittest

from harness import (
    assert_status,
    assert_status_one_of,
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


class EvidenceScopeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo(
            {
                "docs/guide.md": "guide\n",
                "tools/blackbox.py": "raise SystemExit(0)\n",
            }
        )
        scopes = {
            "machine": {
                "affects": ["src/**"],
                "exempt": ["docs/**", "tools/**"],
            },
            "blackbox": {
                "affects": ["src/**"],
                "exempt": ["docs/**", "tools/**"],
            },
        }
        self.plan = write_plan(
            self.repo,
            plan_markdown(
                head(self.repo),
                builder_write=["src/**", "docs/**", "tools/**"],
                include_e2e=True,
                evidence_scopes=scopes,
            ),
        )

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def ready_evidence(self):
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)
        tester_id = register_agent(run_path, "tester")
        assert_status_one_of(
            run_cli("integrate-tests", "--run", run_path),
            {"READY", "NOOP"},
            rc=0,
        )
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        candidate = head(builder)
        register_agent(run_path, "tester", agent_id=tester_id, result="pass")
        record_evidence(run_path, "e2e_verified", candidate, agent_id=tester_id)
        reviewer_id = register_agent(run_path, "reviewer")
        record_evidence(run_path, "reviewed", candidate, agent_id=reviewer_id)
        record_evidence(run_path, "doc_reviewed", candidate, agent_id=reviewer_id)
        return run_path, builder

    def test_exempt_change_reuses_machine_and_blackbox_but_not_review(self) -> None:
        run_path, builder = self.ready_evidence()
        observed = head(builder)
        (builder / "docs" / "guide.md").write_text("updated guide\n")

        reused = run_cli("verify", "--run", run_path)
        assert_status(reused, "PASS", rc=0)
        self.assertIs(reused.data.get("reused"), True, reused.data)
        accepted = head(builder)
        self.assertNotEqual(accepted, observed)
        ledger = load_ledger(run_path)
        self.assertEqual(ledger["evidence"]["machine"]["observed_head"], observed)
        self.assertEqual(ledger["evidence"]["machine"]["accepted_head"], accepted)
        self.assertEqual(ledger["evidence"]["blackbox"]["observed_head"], observed)
        self.assertEqual(ledger["evidence"]["blackbox"]["accepted_head"], accepted)
        self.assertIsNone(ledger["evidence"]["review"])
        self.assertIsNone(ledger["evidence"]["doc_review"])

    def test_affecting_change_invalidates_scoped_evidence(self) -> None:
        run_path, builder = self.ready_evidence()
        (builder / "src" / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")

        verified = run_cli("verify", "--run", run_path)
        assert_status(verified, "FAIL", rc=1)
        ledger = load_ledger(run_path)
        self.assertIsNone(ledger["evidence"]["machine"])
        self.assertIsNone(ledger["evidence"]["blackbox"])
        self.assertIsNone(ledger["evidence"]["review"])

    def test_blackbox_command_dependencies_remain_in_scope_during_reuse(self) -> None:
        started, run_path = start_run(self.repo, self.plan)
        builder, _tester = worktrees_from(started, run_path)
        tester_id = register_agent(run_path, "tester")
        assert_status_one_of(
            run_cli("integrate-tests", "--run", run_path),
            {"READY", "NOOP"},
            rc=0,
        )
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        candidate = head(builder)
        register_agent(run_path, "tester", agent_id=tester_id, result="pass")
        recorded = record_evidence(
            run_path,
            "e2e_verified",
            candidate,
            agent_id=tester_id,
            command_argv=[sys.executable, "tools/blackbox.py"],
        )
        assert_status(recorded, "READY", rc=0)
        self.assertIn("tools/blackbox.py", recorded.data["evidence"]["scope"])

        (builder / "docs" / "guide.md").write_text("updated guide\n")
        reused = run_cli("verify", "--run", run_path)
        assert_status(reused, "PASS", rc=0)
        ledger = load_ledger(run_path)
        self.assertEqual(
            ledger["evidence"]["blackbox"]["accepted_head"], head(builder)
        )

        (builder / "tools" / "blackbox.py").write_text("raise SystemExit(1)\n")
        run_cli("verify", "--run", run_path)
        ledger = load_ledger(run_path)
        self.assertIsNone(ledger["evidence"]["blackbox"])


if __name__ == "__main__":
    unittest.main()
