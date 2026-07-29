from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
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
    repo_session_id,
    run_cli,
    run_process,
    start_run,
    write_plan,
)


HOOK = ROOT / "hooks" / "builder-loop.py"


class LifecycleJournalReplayContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory(prefix="journal-replay-")
        self.env = {
            "XDG_RUNTIME_DIR": self.runtime.name,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.repo = init_repo()
        self.session_id = repo_session_id(self.repo, "journal-replay")
        plan = write_plan(self.repo, plan_markdown(head(self.repo)))
        _started, self.run_path = start_run(
            self.repo, plan, session_id=self.session_id, env=self.env
        )
        self.tester = Path(load_ledger(self.run_path)["worktrees"]["tester"]["path"])

    def tearDown(self) -> None:
        cleanup_repo(self.repo)
        self.runtime.cleanup()

    def _journals(self, turn_id: str) -> list[Path]:
        matches = []
        for path in Path(self.runtime.name).rglob("*.json"):
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("turn_id") == turn_id:
                matches.append(path)
        return matches

    def _queue_while_locked(self, event: dict) -> None:
        completed = run_process(
            [sys.executable, HOOK],
            cwd=self.tester,
            env={**self.env, "BUILDER_LOOP_CLI": str(CLI)},
            input_text=json.dumps(event),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def _direct_event(self, turn_id: str, event: str, result: str | None = None):
        args = [
            "agent-event",
            "--repo",
            self.repo,
            "--session-id",
            self.session_id,
            "--role",
            "tester",
            "--agent-id",
            "journal-tester",
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
                **self.env,
                "BUILDER_LOOP_HOOK_EVENT": "1",
                "BUILDER_LOOP_AGENT_EVENT_APPLY": "1",
            },
        )

    def test_two_cli_consumers_fold_one_start_and_terminal_once(self) -> None:
        turn_id = "concurrent-journal-turn"
        base = {
            "cwd": str(self.tester),
            "session_id": self.session_id,
            "turn_id": turn_id,
            "agent_id": "journal-tester",
            "agent_type": "tester",
        }
        lock_path = self.run_path / "runtime.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            for event in (
                {**base, "hook_event_name": "SubagentStart"},
                {
                    **base,
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "done\nTESTER_RESULT: tests_ready",
                    "stop_hook_active": False,
                },
            ):
                process = subprocess.Popen(
                    [sys.executable, HOOK],
                    cwd=self.tester,
                    env={
                        **os.environ,
                        **self.env,
                        "BUILDER_LOOP_CLI": str(CLI),
                    },
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout, stderr = process.communicate(json.dumps(event), timeout=12)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            queued = self._journals(turn_id)
            self.assertEqual(len(queued), 2, queued)
            saved = {path: path.read_bytes() for path in queued}
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        argv = [sys.executable, str(CLI), "status", "--run", str(self.run_path)]
        consumers = [
            subprocess.Popen(
                argv,
                cwd=ROOT,
                env={**os.environ, **self.env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = [process.communicate(timeout=20) for process in consumers]
        for process, (stdout, stderr) in zip(consumers, outputs):
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertNotIn("TESTER_FOLLOW_UP_ATTESTATION_MISMATCH", stdout + stderr)

        ledger = load_ledger(self.run_path)
        tester = ledger["agents"]["tester"]
        self.assertEqual(tester["turn_id"], turn_id)
        self.assertEqual(tester["event"], "idle")
        self.assertEqual(tester["result"], "tests_ready")
        matching = [
            event
            for event in ledger["events"]
            if event.get("type") == "agent_event"
            and event.get("facts", {}).get("turn_id") == turn_id
        ]
        self.assertEqual(
            [(item["facts"]["event"], item["facts"]["result"]) for item in matching],
            [("start", None), ("idle", "tests_ready")],
        )
        self.assertFalse(self._journals(turn_id))
        self.assertEqual(ledger["phase"], "active")
        encoded = json.dumps(ledger)
        self.assertNotIn("agent_event_rejected", encoded)
        self.assertNotIn("continuity_failure", encoded)

        for path, content in saved.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o600)
        replayed = run_cli("status", "--run", self.run_path, env=self.env)
        self.assertEqual(replayed.returncode, 0, replayed.data)
        self.assertFalse(self._journals(turn_id))
        replay_ledger = load_ledger(self.run_path)
        replay_matching = [
            event
            for event in replay_ledger["events"]
            if event.get("type") == "agent_event"
            and event.get("facts", {}).get("turn_id") == turn_id
        ]
        self.assertEqual(replay_matching, matching)

    def test_direct_replay_is_noop_but_real_conflicts_fail_closed(self) -> None:
        turn_id = "direct-replay-turn"
        assert_status(self._direct_event(turn_id, "start"), "READY", rc=0)
        assert_status(self._direct_event(turn_id, "start"), "NOOP", rc=0)
        assert_status(
            self._direct_event(turn_id, "idle", "tests_ready"), "READY", rc=0
        )
        assert_status(
            self._direct_event(turn_id, "idle", "tests_ready"), "NOOP", rc=0
        )

        conflict = self._direct_event(turn_id, "idle", "blocked")
        self.assertNotEqual(conflict.returncode, 0, conflict.data)
        self.assertEqual(conflict.data.get("code"), "AGENT_TURN_RESULT_CONFLICT")

        new_turn = self._direct_event("unprepared-new-turn", "start")
        self.assertNotEqual(new_turn.returncode, 0, new_turn.data)
        other_agent = run_cli(
            "agent-event",
            "--repo",
            self.repo,
            "--session-id",
            self.session_id,
            "--role",
            "tester",
            "--agent-id",
            "other-tester",
            "--turn-id",
            turn_id,
            "--event",
            "start",
            env={
                **self.env,
                "BUILDER_LOOP_HOOK_EVENT": "1",
                "BUILDER_LOOP_AGENT_EVENT_APPLY": "1",
            },
        )
        self.assertNotEqual(other_agent.returncode, 0, other_agent.data)


if __name__ == "__main__":
    unittest.main()
