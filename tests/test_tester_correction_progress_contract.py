from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from harness import (
    assert_status,
    cleanup_repo,
    commit_all,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    repo_session_id,
    run_cli,
    start_run,
    worktrees_from,
    write_plan,
)


def nested_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = nested_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_value(child, key)
            if found is not None:
                return found
    return None


class TesterCorrectionProgressContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()
        self.session_id = repo_session_id(self.repo, "tester-correction")
        plan = write_plan(self.repo, plan_markdown(head(self.repo)))
        _started, self.run_path = start_run(
            self.repo, plan, session_id=self.session_id
        )
        self.builder, self.tester = worktrees_from(_started, self.run_path)
        self.agent_id = "correction-tester"
        self.turn_index = 0
        self._complete_turn(initial=True)
        assert_status(run_cli("integrate-tests", "--run", self.run_path), "READY", rc=0)

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def _event(self, turn_id: str, event: str, result: str | None = None):
        args = [
            "agent-event",
            "--repo",
            self.repo,
            "--session-id",
            self.session_id,
            "--role",
            "tester",
            "--agent-id",
            self.agent_id,
            "--turn-id",
            turn_id,
            "--event",
            event,
        ]
        if result is not None:
            args.extend(["--result", result])
        return run_cli(
            *args,
            env={
                "BUILDER_LOOP_HOOK_EVENT": "1",
                "BUILDER_LOOP_AGENT_EVENT_APPLY": "1",
            },
        )

    def _complete_turn(self, *, initial: bool = False) -> str:
        self.turn_index += 1
        turn_id = f"correction-turn-{self.turn_index}"
        if not initial:
            prepared = run_cli(
                "prepare-follow-up",
                "--run",
                self.run_path,
                "--role",
                "tester",
                "--agent-id",
                self.agent_id,
                "--purpose",
                "author",
            )
            assert_status(prepared, "READY", rc=0)
            progress = prepared.data.get("tester_correction_progress")
            self.assertIsInstance(progress, dict, prepared.data)
        assert_status(self._event(turn_id, "start"), "READY", rc=0)
        test_path = self.tester / "tests" / "test_correction_fixture.py"
        if initial:
            test_path.write_text(
                "import unittest\n"
                "from src.calc import add\n\n"
                "class CorrectionFixtureTest(unittest.TestCase):\n"
                "    def test_initial(self):\n"
                "        self.assertEqual(add(1, 2), 3)\n"
            )
        else:
            with test_path.open("a") as stream:
                stream.write(
                    f"\n    def test_correction_{self.turn_index}(self):\n"
                    f"        self.assertEqual(add({self.turn_index}, 1), "
                    f"{self.turn_index + 1})\n"
                )
        commit_all(self.tester, f"tester correction {self.turn_index}")
        assert_status(
            self._event(turn_id, "idle", "tests_ready"), "READY", rc=0
        )
        return turn_id

    def _complete_correction(self) -> str:
        turn_id = self._complete_turn()
        integrated = run_cli("integrate-tests", "--run", self.run_path)
        self.assertIn(integrated.data.get("status"), {"READY", "NOOP"}, integrated.data)
        self.assertEqual(integrated.returncode, 0, integrated.data)
        return turn_id

    def _progress(self, result) -> dict:
        value = nested_value(result.data, "tester_correction_progress")
        self.assertIsInstance(value, dict, result.data)
        return value

    def _assert_progress_facts(
        self, progress: dict, *, window_count: int, lifetime_count: int, turns: list[str]
    ) -> None:
        self.assertEqual(
            nested_value(progress, "current_window_count"), window_count, progress
        )
        self.assertEqual(
            nested_value(progress, "lifetime_count"), lifetime_count, progress
        )
        encoded = json.dumps(progress, ensure_ascii=False)
        for turn_id in turns:
            self.assertIn(turn_id, encoded)
        if turns:
            self.assertIn("tests_ready", encoded)
            self.assertIn(head(self.tester), encoded)
        self.assertIsNotNone(nested_value(progress, "window_start"), progress)

    def test_three_corrections_stop_fourth_and_diagnostics_share_progress(self) -> None:
        turns = [self._complete_correction() for _ in range(3)]

        stopped = run_cli(
            "prepare-follow-up",
            "--run",
            self.run_path,
            "--role",
            "tester",
            "--agent-id",
            self.agent_id,
            "--purpose",
            "author",
        )
        assert_status(stopped, "NEEDS_USER", rc=1)
        self.assertEqual(stopped.data.get("code"), "ARCHITECTURE_REVIEW_REQUIRED")
        self.assertEqual(stopped.data.get("phase"), "architecture_review_required")
        stop_progress = self._progress(stopped)
        self._assert_progress_facts(
            stop_progress, window_count=3, lifetime_count=3, turns=turns
        )
        self.assertIsNone(load_ledger(self.run_path)["pending_agent_turns"]["tester"])

        status = run_cli("status", "--run", self.run_path)
        doctor = run_cli("doctor", "--run", self.run_path)
        self.assertEqual(self._progress(status), stop_progress)
        self.assertEqual(self._progress(doctor), stop_progress)
        diagnostic = json.dumps(stopped.data, ensure_ascii=False).lower()
        self.assertIn("architecture", diagnostic)
        self.assertIn("tester", diagnostic)
        self.assertIn("correction", diagnostic)

    def test_resume_and_machine_pass_open_new_windows_without_erasing_history(self) -> None:
        first_window = [self._complete_correction() for _ in range(3)]
        stopped = run_cli(
            "prepare-follow-up",
            "--run",
            self.run_path,
            "--role",
            "tester",
            "--agent-id",
            self.agent_id,
            "--purpose",
            "author",
        )
        assert_status(stopped, "NEEDS_USER", rc=1)
        attempts_before = len(load_ledger(self.run_path)["verification"]["attempts"])
        max_iterations = load_ledger(self.run_path)["loop_config"]["max_iterations"]
        resume_candidate = head(self.builder)

        resumed = run_cli(
            "resume",
            "--run",
            self.run_path,
            "--reason",
            "explicit architecture review completed",
        )
        assert_status(resumed, "READY", rc=0)
        self.assertEqual(resumed.data.get("progress_source"), "tester_correction")
        resumed_progress = self._progress(resumed)
        window_start = resumed_progress.get("window_start")
        self.assertIsInstance(window_start, dict, resumed_progress)
        self.assertEqual(window_start.get("candidate_head"), resume_candidate)
        self._assert_progress_facts(
            resumed_progress,
            window_count=0,
            lifetime_count=3,
            turns=first_window,
        )
        ledger = load_ledger(self.run_path)
        self.assertEqual(len(ledger["verification"]["attempts"]), attempts_before)
        self.assertEqual(ledger["loop_config"]["max_iterations"], max_iterations)

        after_resume = self._complete_correction()
        verified = run_cli("verify", "--run", self.run_path)
        assert_status(verified, "PASS", rc=0)
        pass_progress = self._progress(verified)
        self._assert_progress_facts(
            pass_progress,
            window_count=0,
            lifetime_count=4,
            turns=first_window + [after_resume],
        )

        second_window = [self._complete_correction() for _ in range(3)]
        stopped_again = run_cli(
            "prepare-follow-up",
            "--run",
            self.run_path,
            "--role",
            "tester",
            "--agent-id",
            self.agent_id,
            "--purpose",
            "author",
        )
        assert_status(stopped_again, "NEEDS_USER", rc=1)
        self._assert_progress_facts(
            self._progress(stopped_again),
            window_count=3,
            lifetime_count=7,
            turns=first_window + [after_resume] + second_window,
        )


if __name__ == "__main__":
    unittest.main()
