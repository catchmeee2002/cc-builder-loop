from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from harness import (
    CLI,
    ROOT,
    assert_status,
    cleanup_repo,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    run_cli,
    run_process,
    start_run,
    worktrees_from,
    write_plan,
)


HOOK = ROOT / "hooks" / "builder-loop.py"


class HookRuntimeIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def call_hook(self, event: dict, *, cwd: Path) -> dict:
        completed = run_process(
            [sys.executable, HOOK],
            cwd=cwd,
            env={"BUILDER_LOOP_CLI": str(CLI)},
            input_text=json.dumps(event),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, completed)
        return json.loads(lines[-1])

    def test_real_hook_finds_run_from_tester_and_builder_worktrees(self) -> None:
        plan = write_plan(self.repo, plan_markdown(head(self.repo)))
        started, run_path = start_run(self.repo, plan)
        builder, tester = worktrees_from(started, run_path)
        base_event = {
            "cwd": str(tester),
            "session_id": "fixture-session",
            "turn_id": "tester-hook-turn",
            "agent_id": "tester-hook-agent",
            "agent_type": "tester",
        }

        start_output = self.call_hook(
            {**base_event, "hook_event_name": "SubagentStart"}, cwd=tester
        )
        self.assertNotIn("systemMessage", start_output, start_output)
        started_ledger = load_ledger(run_path)
        self.assertEqual(started_ledger["agents"]["tester"]["event"], "start")

        stop_output = self.call_hook(
            {
                **base_event,
                "hook_event_name": "SubagentStop",
                "last_assistant_message": "tests committed\nTESTER_RESULT: tests_ready",
                "stop_hook_active": False,
            },
            cwd=tester,
        )
        self.assertNotIn("decision", stop_output, stop_output)
        idle_ledger = load_ledger(run_path)
        self.assertEqual(idle_ledger["agents"]["tester"]["result"], "tests_ready")

        root_stop = self.call_hook(
            {
                "hook_event_name": "Stop",
                "cwd": str(builder),
                "session_id": "fixture-session",
                "stop_hook_active": False,
                "last_assistant_message": "still working",
            },
            cwd=builder,
        )
        self.assertEqual(root_stop.get("decision"), "block", root_stop)

    def test_invalid_ledger_is_fatal_instead_of_no_active_run(self) -> None:
        plan = write_plan(self.repo, plan_markdown(head(self.repo)))
        _started, run_path = start_run(self.repo, plan)
        (run_path / "ledger.json").write_text("{broken\n")

        status = run_cli(
            "status",
            "--repo",
            self.repo,
            "--session-id",
            "fixture-session",
        )
        assert_status(status, "FATAL", rc=2)
        self.assertEqual(status.data.get("code"), "LEDGER_SCAN_INVALID")

        hook_output = self.call_hook(
            {
                "hook_event_name": "Stop",
                "cwd": str(self.repo),
                "session_id": "fixture-session",
                "stop_hook_active": False,
                "last_assistant_message": "done",
            },
            cwd=self.repo,
        )
        self.assertIn("systemMessage", hook_output, hook_output)
        self.assertNotEqual(hook_output, {})


if __name__ == "__main__":
    unittest.main()
