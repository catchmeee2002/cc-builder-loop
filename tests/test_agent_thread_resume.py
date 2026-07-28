from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from harness import (
    ROOT,
    assert_ledger_schema,
    assert_status,
    cleanup_repo,
    head,
    init_repo,
    plan_markdown,
    problem_report,
    record_problems,
    run_cli,
    run_process,
    start_run,
    write_plan,
)


ADAPTER = ROOT / "tests" / "helpers" / "mock-agent-adapter.py"


def run_agent_event(*args: str):
    return run_cli(*args, env={"BUILDER_LOOP_HOOK_EVENT": "1"})


class AgentThreadResumeContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-adapter-state-")
        self.state = Path(self.tmp.name) / "state.json"
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)
        self.tmp.cleanup()

    def call(self, role: str, op: str, thread_id: str = "") -> tuple[int, dict]:
        argv = [
            sys.executable,
            ADAPTER,
            "--state",
            self.state,
            "--role",
            role,
            "--op",
            op,
        ]
        if thread_id:
            argv.extend(["--thread-id", thread_id])
        cp = run_process(argv)
        return cp.returncode, json.loads(cp.stdout.splitlines()[-1])

    def test_tester_and_reviewer_each_create_once_then_follow_up_same_thread(self) -> None:
        rc_t, tester = self.call("tester", "create")
        self.assertEqual(rc_t, 0)
        tester_id = tester["thread_id"]
        for _ in range(2):
            rc, result = self.call("tester", "follow-up", tester_id)
            self.assertEqual(rc, 0)
            self.assertEqual(result["thread_id"], tester_id)
        _, tester_final = self.call("tester", "inspect")
        self.assertEqual(tester_final["create_count"], 1)
        self.assertEqual(tester_final["follow_up_count"], 2)

        rc_r, reviewer = self.call("reviewer", "create")
        self.assertEqual(rc_r, 0)
        reviewer_id = reviewer["thread_id"]
        rc, reviewer_follow = self.call("reviewer", "follow-up", reviewer_id)
        self.assertEqual(rc, 0)
        self.assertNotEqual(reviewer_id, tester_id)
        self.assertEqual(reviewer_follow["create_count"], 1)
        self.assertEqual(reviewer_follow["follow_up_count"], 1)

    def test_duplicate_create_and_cross_role_thread_are_rejected(self) -> None:
        _, tester = self.call("tester", "create")
        duplicate_rc, duplicate = self.call("tester", "create")
        self.assertEqual(duplicate_rc, 2)
        self.assertEqual(duplicate["reason"], "duplicate_create")

        self.call("reviewer", "create")
        wrong_rc, wrong = self.call("reviewer", "follow-up", tester["thread_id"])
        self.assertEqual(wrong_rc, 2)
        self.assertEqual(wrong["reason"], "thread_mismatch")

    def test_agent_event_keeps_native_orchestration_on_one_thread(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, run_path = start_run(repo, plan)

        _, tester = self.call("tester", "create")
        tester_id = tester["thread_id"]
        first = run_agent_event(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            tester_id,
            "--turn-id",
            "tester-turn-1",
            "--event",
            "start",
        )
        assert_status(first, "READY", rc=0)

        idle = run_agent_event(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            tester_id,
            "--turn-id",
            "tester-turn-1",
            "--event",
            "idle",
            "--result",
            "tests_ready",
        )
        assert_status(idle, "READY", rc=0)

        self.call("tester", "follow-up", tester_id)
        resumed = run_agent_event(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            tester_id,
            "--turn-id",
            "tester-turn-2",
            "--event",
            "start",
        )
        assert_status(resumed, "READY", rc=0)
        self.assertEqual(resumed.data.get("agent_id"), tester_id)

        _, final = self.call("tester", "inspect")
        self.assertEqual(final["create_count"], 1)
        self.assertEqual(final["follow_up_count"], 1)

        replacement = run_agent_event(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            "tester-thread-2",
            "--turn-id",
            "tester-replacement-turn",
            "--event",
            "start",
        )
        assert_status(replacement, "NEEDS_USER")
        self.assertNotEqual(replacement.returncode, 0)

    def test_closed_role_thread_cannot_be_replaced(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, run_path = start_run(repo, plan)

        for event, expected in (("start", "READY"), ("closed", "CONTINUITY_FAILURE")):
            result = run_agent_event(
                "agent-event",
                "--repo",
                repo,
                "--session-id",
                "fixture-session",
                "--role",
                "tester",
                "--agent-id",
                "tester-thread-1",
                "--turn-id",
                "tester-closed-turn",
                "--event",
                event,
            )
            assert_status(result, expected)

        replacement = run_agent_event(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            "tester-thread-2",
            "--turn-id",
            "tester-replacement-turn",
            "--event",
            "start",
        )
        assert_status(replacement, "CONTINUITY_FAILURE", rc=1)
        status = run_cli("status", "--run", run_path)
        assert_status(status, "CONTINUITY_FAILURE", rc=1)
        assert_ledger_schema(run_path)

    def test_agent_event_rejects_non_hook_callers(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        start_run(repo, plan)
        result = run_cli(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            "fake-agent",
            "--turn-id",
            "fake-turn",
            "--event",
            "idle",
            "--result",
            "pass",
        )
        assert_status(result, "FATAL", rc=2)
        self.assertEqual(result.data.get("code"), "AGENT_EVENT_HOOK_REQUIRED")

    def test_stale_or_completed_turn_events_cannot_replace_current_result(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        _started, run_path = start_run(repo, plan)
        agent_id = "stable-tester-thread"

        for event, result_value in (("start", None), ("idle", "pass")):
            argv = [
                "agent-event",
                "--repo",
                repo,
                "--session-id",
                "fixture-session",
                "--role",
                "tester",
                "--agent-id",
                agent_id,
                "--turn-id",
                "turn-1",
                "--event",
                event,
            ]
            if result_value:
                argv.extend(["--result", result_value])
            assert_status(run_agent_event(*argv), "READY", rc=0)

        assert_status(
            run_agent_event(
                "agent-event",
                "--repo",
                repo,
                "--session-id",
                "fixture-session",
                "--role",
                "tester",
                "--agent-id",
                agent_id,
                "--turn-id",
                "turn-2",
                "--event",
                "start",
            ),
            "READY",
            rc=0,
        )
        stale_idle = run_agent_event(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            agent_id,
            "--turn-id",
            "turn-1",
            "--event",
            "idle",
            "--result",
            "pass",
        )
        assert_status(stale_idle, "NEEDS_USER", rc=1)
        self.assertEqual(stale_idle.data.get("code"), "AGENT_TURN_MISMATCH")

        assert_status(
            run_agent_event(
                "agent-event",
                "--repo",
                repo,
                "--session-id",
                "fixture-session",
                "--role",
                "tester",
                "--agent-id",
                agent_id,
                "--turn-id",
                "turn-2",
                "--event",
                "idle",
                "--result",
                "fail",
            ),
            "READY",
            rc=0,
        )
        replay = run_agent_event(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            agent_id,
            "--turn-id",
            "turn-1",
            "--event",
            "start",
        )
        assert_status(replay, "NOOP", rc=0)
        self.assertEqual(replay.data.get("code"), "STALE_AGENT_TURN")
        assert_status(
            record_problems(
                run_path,
                source="tester",
                source_id="turn-2",
                manifest=problem_report(
                    {
                        "key": "tester-turn-failed",
                        "summary": "Tester turn failed",
                        "details": "The completed Tester turn requires author follow-up.",
                        "owner": "tester",
                    }
                ),
            ),
            "READY",
            rc=0,
        )
        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "tester",
            "--agent-id",
            agent_id,
            "--purpose",
            "author",
        )
        assert_status(prepared, "READY", rc=0)
        stale_prepared_terminal = run_agent_event(
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            "fixture-session",
            "--role",
            "tester",
            "--agent-id",
            agent_id,
            "--turn-id",
            "turn-1",
            "--event",
            "idle",
            "--result",
            "pass",
        )
        assert_status(stale_prepared_terminal, "NEEDS_USER", rc=1)
        self.assertEqual(
            stale_prepared_terminal.data.get("code"), "AGENT_TURN_MISMATCH"
        )
        ledger = json.loads((run_path / "ledger.json").read_text())
        self.assertEqual(ledger["agents"]["tester"]["turn_id"], "turn-2")
        self.assertEqual(ledger["agents"]["tester"]["result"], "fail")
        self.assertEqual(
            ledger["pending_agent_turns"]["tester"]["dispatch_id"],
            prepared.data["dispatch_id"],
        )


if __name__ == "__main__":
    unittest.main()
