from __future__ import annotations

import json
import sys
import unittest

from harness import ROOT, run_process


HOOK = ROOT / "hooks" / "builder-loop.py"
MOCK_RUNTIME = ROOT / "tests" / "helpers" / "mock-runtime.py"


class StopHookGateTest(unittest.TestCase):
    def call_stop(
        self,
        status: str,
        *,
        stop_hook_active: bool = False,
        last_assistant_message: str = "",
    ) -> tuple[int, dict]:
        event = {
            "hook_event_name": "Stop",
            "cwd": str(ROOT),
            "session_id": "hook-fixture-session",
            "stop_hook_active": stop_hook_active,
            "last_assistant_message": last_assistant_message,
        }
        cp = run_process(
            [sys.executable, HOOK],
            cwd=ROOT,
            env={
                "BUILDER_LOOP_CLI": str(MOCK_RUNTIME),
                "MOCK_RUNTIME_STATUS": status,
                "MOCK_RUNTIME_MESSAGE": f"fixture {status}",
            },
            input_text=json.dumps(event),
        )
        lines = [line for line in cp.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"stdout={cp.stdout!r} stderr={cp.stderr!r}")
        return cp.returncode, json.loads(lines[-1])

    def call_subagent_stop(
        self, message: str, *, stop_hook_active: bool = False
    ) -> tuple[int, dict]:
        event = {
            "hook_event_name": "SubagentStop",
            "cwd": str(ROOT),
            "session_id": "hook-fixture-session",
            "turn_id": "hook-tester-turn",
            "agent_id": "hook-tester-agent",
            "agent_type": "tester",
            "stop_hook_active": stop_hook_active,
            "last_assistant_message": message,
        }
        cp = run_process(
            [sys.executable, HOOK],
            cwd=ROOT,
            env={
                "BUILDER_LOOP_CLI": str(MOCK_RUNTIME),
                "MOCK_RUNTIME_STATUS": "READY",
                "MOCK_RUNTIME_MESSAGE": "fixture READY",
            },
            input_text=json.dumps(event),
        )
        lines = [line for line in cp.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"stdout={cp.stdout!r} stderr={cp.stderr!r}")
        return cp.returncode, json.loads(lines[-1])

    def test_active_run_blocks_root_stop(self) -> None:
        rc, result = self.call_stop("ACTIVE")
        self.assertEqual(rc, 0)
        self.assertEqual(result.get("decision"), "block", result)
        self.assertEqual(result.get("reason"), "fixture ACTIVE", result)

    def test_active_recursion_guard_does_not_block_again(self) -> None:
        rc, result = self.call_stop("ACTIVE", stop_hook_active=True)
        self.assertEqual(rc, 0)
        self.assertNotIn("decision", result)
        self.assertIn("仍为 ACTIVE", result.get("systemMessage", ""))

    def test_explicit_user_input_marker_allows_active_run_to_wait(self) -> None:
        rc, result = self.call_stop(
            "ACTIVE",
            last_assistant_message="BUILDER_INPUT_REQUIRED:fixture-run",
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("decision", result)
        self.assertIn("等待用户决定", result.get("systemMessage", ""))

    def test_marker_for_another_run_does_not_bypass_active_gate(self) -> None:
        rc, result = self.call_stop(
            "ACTIVE",
            last_assistant_message="BUILDER_INPUT_REQUIRED:other-run",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result.get("decision"), "block", result)

    def test_every_non_active_status_allows_stop(self) -> None:
        for status in (
            "NOOP",
            "READY",
            "NEEDS_USER",
            "CONFLICT",
            "CONTINUITY_FAILURE",
            "FATAL",
            "FATAL_AMBIGUOUS",
            "COMPLETE",
        ):
            with self.subTest(status=status):
                rc, result = self.call_stop(status)
                self.assertEqual(rc, 0)
                self.assertNotIn("decision", result, result)

    def test_subagent_result_must_be_exactly_the_last_line(self) -> None:
        rc, missing = self.call_subagent_stop("tests finished without marker")
        self.assertEqual(rc, 0)
        self.assertEqual(missing.get("decision"), "block", missing)

        rc, invalid_after_continuation = self.call_subagent_stop(
            "TESTER_RESULT: maybe", stop_hook_active=True
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("decision", invalid_after_continuation)
        self.assertIn(
            "LIFECYCLE_ROUTE_MISSING",
            invalid_after_continuation.get("systemMessage", ""),
        )

        rc, valid = self.call_subagent_stop("details\nTESTER_RESULT: fail")
        self.assertEqual(rc, 0)
        self.assertEqual(valid.get("decision"), "block", valid)
        self.assertIn("LIFECYCLE_ROUTE_MISSING", valid.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
