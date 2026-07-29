from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from harness import (
    CLI,
    ROOT,
    _ensure_standard_proof_source,
    assert_status,
    assert_status_one_of,
    cleanup_repo,
    commit_all,
    ensure_test_effectiveness,
    fixture_runtime_env,
    head,
    init_repo,
    load_ledger,
    plan_markdown,
    problem_report,
    record_evidence,
    register_agent,
    repo_session_id,
    run_cli,
    run_process,
    start_run,
    worktrees_from,
    write_plan,
)


HOOK = ROOT / "hooks" / "builder-loop.py"
SPEC_HEAD = "492db76a1f3fb4a59532c2dfffce61850c9d66ac"


class LifecycleDeliveryContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = tempfile.TemporaryDirectory(prefix="lifecycle-runtime-")
        self.env = {
            "XDG_RUNTIME_DIR": self.runtime.name,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.repos: list[Path] = []

    def tearDown(self) -> None:
        for repo in self.repos:
            cleanup_repo(repo)
        self.runtime.cleanup()

    def start(self, *, label: str = "lifecycle") -> tuple[Path, Path, str]:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(repo, plan_markdown(head(repo)))
        session_id = repo_session_id(repo, label)
        _started, run_path = start_run(
            repo,
            plan,
            session_id=session_id,
            env=self.env,
        )
        return repo, run_path, session_id

    def direct_event(
        self,
        repo: Path,
        session_id: str,
        *,
        role: str,
        agent_id: str,
        turn_id: str,
        event: str,
        result: str | None = None,
    ):
        argv = [
            "agent-event",
            "--repo",
            repo,
            "--session-id",
            session_id,
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
        return run_cli(
            *argv,
            env={
                **self.env,
                "BUILDER_LOOP_HOOK_EVENT": "1",
                "BUILDER_LOOP_AGENT_EVENT_APPLY": "1",
            },
        )

    def hook_process(
        self,
        event: dict,
        *,
        cwd: Path,
        runtime_env: dict[str, str] | None = None,
    ):
        stdout = tempfile.TemporaryFile(mode="w+")
        stderr = tempfile.TemporaryFile(mode="w+")
        process = subprocess.Popen(
            [sys.executable, HOOK],
            cwd=cwd,
            env={
                **os.environ,
                **(runtime_env or self.env),
                "BUILDER_LOOP_CLI": str(CLI),
            },
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        self.assertIsNotNone(process.stdin)
        if process.stdin is None:
            raise AssertionError("Hook stdin pipe was not created")
        process.stdin.write(json.dumps(event))
        process.stdin.close()
        return process, stdout, stderr

    def call_hook(
        self,
        event: dict,
        *,
        cwd: Path,
        runtime_env: dict[str, str] | None = None,
    ) -> dict:
        process, stdout, stderr = self.hook_process(
            event, cwd=cwd, runtime_env=runtime_env
        )
        process.wait(timeout=20)
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read()
        error = stderr.read()
        stdout.close()
        stderr.close()
        self.assertEqual(process.returncode, 0, error)
        lines = [line for line in output.splitlines() if line.strip()]
        self.assertTrue(lines, output)
        return json.loads(lines[-1])

    def call_hook_with_ledger_and_git_unavailable(
        self, event: dict, *, cwd: Path, run_path: Path
    ) -> dict:
        with tempfile.TemporaryDirectory(prefix="blocked-git-") as tmp:
            fake_git = Path(tmp) / "git"
            fake_git.write_text("#!/bin/sh\nsleep 20\nexit 99\n")
            fake_git.chmod(0o755)
            ledger_path = run_path / "ledger.json"
            original_mode = ledger_path.stat().st_mode & 0o777
            ledger_path.chmod(0)
            stdout = tempfile.TemporaryFile(mode="w+")
            stderr = tempfile.TemporaryFile(mode="w+")
            process = None
            try:
                process = subprocess.Popen(
                    [sys.executable, HOOK],
                    cwd=cwd,
                    env={
                        **os.environ,
                        **self.env,
                        "BUILDER_LOOP_CLI": str(CLI),
                        "PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}",
                    },
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                )
                if process.stdin is None:
                    raise AssertionError("Hook stdin pipe was not created")
                process.stdin.write(json.dumps(event))
                process.stdin.close()
                deadline = time.monotonic() + 4
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertIsNotNone(process.poll(), "Hook attempted ledger or Git I/O")
                stdout.seek(0)
                stderr.seek(0)
                output = stdout.read()
                error = stderr.read()
                self.assertEqual(process.returncode, 0, error)
                lines = [line for line in output.splitlines() if line.strip()]
                self.assertTrue(lines, output)
                return json.loads(lines[-1])
            finally:
                ledger_path.chmod(original_mode)
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                stdout.close()
                stderr.close()

    def runtime_json(
        self, needle: str, runtime_dir: str | os.PathLike[str] | None = None
    ) -> list[Path]:
        root = Path(runtime_dir) if runtime_dir is not None else Path(self.runtime.name)
        return [
            path
            for path in root.rglob("*.json")
            if path.is_file() and needle in path.read_text(errors="replace")
        ]

    def route_path(self, run_path: Path) -> Path:
        matches = []
        for path in self.runtime_json(run_path.name):
            try:
                value = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and "event" not in value
                and "turn_id" not in value
            ):
                matches.append(path)
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def write_private_json(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        path.chmod(0o600)

    def test_missing_and_stale_locator_reject_then_accept_the_same_turn(self) -> None:
        repo, run_path, session_id = self.start(label="route-recovery")
        route = self.route_path(run_path)
        route_bytes = route.read_bytes()
        event = {
            "hook_event_name": "SubagentStart",
            "cwd": str(repo.parent),
            "session_id": session_id,
            "turn_id": "route-recovery-turn",
            "agent_id": "route-recovery-tester",
            "agent_type": "tester",
        }

        route.unlink()
        missing = self.call_hook(event, cwd=repo.parent)
        self.assertIn("systemMessage", missing, missing)
        self.assertEqual(self.runtime_json("route-recovery-turn"), [])

        route.parent.mkdir(parents=True, exist_ok=True)
        route.write_bytes(route_bytes)
        route.chmod(0o600)
        stale_value = json.loads(route.read_text())
        stale_value["run_id"] = "stale-run-binding"
        route.write_text(json.dumps(stale_value, sort_keys=True) + "\n")
        route.chmod(0o600)
        stale = self.call_hook(event, cwd=repo.parent)
        self.assertIn("systemMessage", stale, stale)
        self.assertEqual(self.runtime_json("route-recovery-turn"), [])

        route.write_bytes(route_bytes)
        route.chmod(0o600)
        accepted = self.call_hook(event, cwd=repo.parent)
        self.assertNotIn("systemMessage", accepted, accepted)
        folded = run_cli("status", "--run", run_path, env=self.env)
        self.assertNotEqual(folded.data.get("status"), "FATAL", folded.data)
        ledger = load_ledger(run_path)
        self.assertEqual(
            ledger["agents"]["tester"]["turn_id"], "route-recovery-turn"
        )

    def test_tester_start_attests_clean_baseline_at_capture_time(self) -> None:
        repo, run_path, session_id = self.start(label="baseline-attestation")
        tester = Path(load_ledger(run_path)["worktrees"]["tester"]["path"])
        expected_head = head(tester)
        event = {
            "hook_event_name": "SubagentStart",
            "cwd": str(tester),
            "session_id": session_id,
            "turn_id": "baseline-capture-turn",
            "agent_id": "baseline-capture-tester",
            "agent_type": "tester",
        }
        output = self.call_hook(event, cwd=tester)
        self.assertNotIn("systemMessage", output, output)
        journals = self.runtime_json("baseline-capture-turn")
        self.assertEqual(len(journals), 1, journals)
        envelope = json.loads(journals[0].read_text())
        from jsonschema import Draft202012Validator

        event_schema = json.loads(
            (ROOT / "schema" / "codex-agent-event.schema.json").read_text()
        )
        self.assertIn("tester_baseline", event_schema["required"])
        Draft202012Validator(event_schema).validate(envelope)
        self.assertEqual(
            envelope["tester_baseline"],
            {
                "kind": "initial-author",
                "expected_head": expected_head,
                "tester_head": expected_head,
                "dirty_paths": [],
            },
        )
        try:
            from runtime.codex_builder_loop.lifecycle import event_id
        except ModuleNotFoundError:
            self.fail("public lifecycle.event_id is missing")
        self.assertEqual(envelope["event_id"], event_id(envelope))
        changed_baseline = json.loads(json.dumps(envelope))
        changed_baseline["tester_baseline"]["tester_head"] = "0" * 40
        self.assertNotEqual(envelope["event_id"], event_id(changed_baseline))

        authored = tester / "tests" / "after-capture.py"
        authored.write_text("VALUE = 1\n")
        folded = run_cli("status", "--run", run_path, env=self.env)
        self.assertNotEqual(folded.data.get("status"), "FATAL", folded.data)
        ledger = load_ledger(run_path)
        self.assertEqual(
            ledger["agents"]["tester"]["turn_id"], "baseline-capture-turn"
        )
        authored.unlink()

    def test_tester_start_rejects_ordinary_and_ignored_baseline_residue(self) -> None:
        for kind in ("ordinary", "ignored"):
            with self.subTest(kind=kind):
                repo, run_path, session_id = self.start(
                    label=f"baseline-dirty-{kind}"
                )
                tester = Path(
                    load_ledger(run_path)["worktrees"]["tester"]["path"]
                )
                residue = tester / f"{kind}-baseline.tmp"
                if kind == "ignored":
                    git_common = Path(
                        run_process(
                            ["git", "rev-parse", "--git-common-dir"], cwd=tester
                        ).stdout.strip()
                    )
                    if not git_common.is_absolute():
                        git_common = (tester / git_common).resolve()
                    exclude = git_common / "info" / "exclude"
                    exclude.parent.mkdir(parents=True, exist_ok=True)
                    exclude.write_text(
                        (exclude.read_text() if exclude.is_file() else "")
                        + f"\n/{residue.name}\n"
                    )
                residue.write_text(f"{kind}\n")
                if kind == "ordinary":
                    observed = run_process(
                        ["git", "status", "--porcelain", "--untracked-files=all"],
                        cwd=tester,
                    ).stdout
                else:
                    observed = run_process(
                        [
                            "git",
                            "ls-files",
                            "--others",
                            "--ignored",
                            "--exclude-standard",
                        ],
                        cwd=tester,
                    ).stdout
                self.assertIn(residue.name, observed)

                route = self.route_path(run_path)
                route_value = json.loads(route.read_text())
                attestation = route_value["tester_start_attestation"]
                self.assertEqual(attestation["dirty_paths"], [])
                attestation["dirty_paths"] = [residue.name]
                self.write_private_json(route, route_value)
                turn_id = f"dirty-baseline-{kind}-turn"
                event = {
                    "hook_event_name": "SubagentStart",
                    "cwd": str(tester),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "agent_id": f"dirty-baseline-{kind}-tester",
                    "agent_type": "tester",
                }
                queued = self.call_hook_with_ledger_and_git_unavailable(
                    event, cwd=tester, run_path=run_path
                )
                self.assertNotIn("systemMessage", queued, queued)
                journals = self.runtime_json(turn_id)
                self.assertEqual(len(journals), 1, journals)
                envelope = json.loads(journals[0].read_text())
                self.assertEqual(
                    envelope["tester_baseline"]["dirty_paths"], [residue.name]
                )
                rejected = run_cli("status", "--run", run_path, env=self.env)
                self.assertNotEqual(rejected.returncode, 0, rejected.data)
                after_reject = load_ledger(run_path)
                self.assertEqual(after_reject["phase"], "continuity_failure")

    def test_journal_success_path_fsyncs_file_and_parent_directory(self) -> None:
        repo, _run_path, session_id = self.start(label="journal-fsync")
        self.assertIsNotNone(shutil.which("strace"))
        event = {
            "hook_event_name": "SubagentStart",
            "cwd": str(repo),
            "session_id": session_id,
            "turn_id": "journal-fsync-turn",
            "agent_id": "journal-fsync-reviewer",
            "agent_type": "reviewer",
        }
        traced = run_process(
            [
                "strace",
                "-f",
                "-yy",
                "-e",
                "trace=openat,fsync,rename,renameat,renameat2",
                sys.executable,
                HOOK,
            ],
            cwd=repo,
            env={
                **self.env,
                "BUILDER_LOOP_CLI": str(CLI),
            },
            input_text=json.dumps(event),
        )
        self.assertEqual(traced.returncode, 0, traced.stderr)
        fsync_lines = [
            line
            for line in traced.stderr.splitlines()
            if "fsync(" in line and self.runtime.name in line and "= 0" in line
        ]
        directory_opens = [
            line
            for line in traced.stderr.splitlines()
            if "O_DIRECTORY" in line and self.runtime.name in line
        ]
        directory_fds = {
            match.group(1)
            for line in directory_opens
            if (match := re.search(r"= (\d+)<", line)) is not None
        }
        self.assertGreaterEqual(len(fsync_lines), 2, traced.stderr)
        self.assertTrue(directory_opens, traced.stderr)
        self.assertTrue(
            any(
                any(f"fsync({fd}<" in line for fd in directory_fds)
                for line in fsync_lines
            ),
            traced.stderr,
        )

    def test_hook_enqueue_uses_only_private_route_and_bounded_journal_io(self) -> None:
        repo, run_path, session_id = self.start(label="route-only-hook")
        tester = Path(load_ledger(run_path)["worktrees"]["tester"]["path"])
        route = self.route_path(run_path)
        route_value = json.loads(route.read_text())
        self.assertIs(route_value["accepting_events"], True)
        self.assertEqual(
            route_value["tester_start_attestation"]["kind"], "initial-author"
        )

        with tempfile.TemporaryDirectory(prefix="forbidden-git-") as tmp:
            fake_bin = Path(tmp)
            git_marker = fake_bin / "git-invoked"
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/bin/sh\n"
                f"touch {str(git_marker)!r}\n"
                "exit 99\n"
            )
            fake_git.chmod(0o755)
            ledger_path = run_path / "ledger.json"
            original_mode = ledger_path.stat().st_mode & 0o777
            ledger_path.chmod(0)
            process = None
            stdout = tempfile.TemporaryFile(mode="w+")
            trace = tempfile.TemporaryFile(mode="w+")
            try:
                process = subprocess.Popen(
                    [
                        "strace",
                        "-f",
                        "-yy",
                        "-e",
                        "trace=openat,execve",
                        sys.executable,
                        HOOK,
                    ],
                    cwd=tester,
                    env={
                        **os.environ,
                        **self.env,
                        "BUILDER_LOOP_CLI": str(CLI),
                        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    },
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=trace,
                    text=True,
                )
                if process.stdin is None:
                    raise AssertionError("Hook stdin pipe was not created")
                process.stdin.write(
                    json.dumps(
                        {
                            "hook_event_name": "SubagentStart",
                            "cwd": str(tester),
                            "session_id": session_id,
                            "turn_id": "route-only-turn",
                            "agent_id": "route-only-tester",
                            "agent_type": "tester",
                        }
                    )
                )
                process.stdin.close()
                process.wait(timeout=12)
                trace.seek(0)
                trace_text = trace.read()
                self.assertEqual(process.returncode, 0, trace_text)
                self.assertFalse(git_marker.exists(), trace_text)
                self.assertNotIn(str(ledger_path), trace_text)
                self.assertEqual(len(self.runtime_json("route-only-turn")), 1)
            finally:
                ledger_path.chmod(original_mode)
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                stdout.close()
                trace.close()

    def test_tester_follow_up_attestation_accepts_author_and_blackbox_turns(self) -> None:
        agent_id = "follow-up-tester"

        def prepare_author(label: str):
            repo, run_path, session_id = self.start(label=label)
            tester = Path(load_ledger(run_path)["worktrees"]["tester"]["path"])
            initial_turn = f"{label}-initial"
            base = {
                "cwd": str(tester),
                "session_id": session_id,
                "turn_id": initial_turn,
                "agent_id": agent_id,
                "agent_type": "tester",
            }
            started = self.call_hook(
                {**base, "hook_event_name": "SubagentStart"}, cwd=tester
            )
            self.assertNotIn("systemMessage", started, started)
            start_output = started
            self.assertNotIn("systemMessage", start_output, start_output)
            self.assertEqual(
                run_cli("status", "--run", run_path, env=self.env).returncode, 0
            )
            _ensure_standard_proof_source(run_path)
            (tester / "tests" / "test_follow_up_fixture.py").write_text(
                "from src.calc import add\n\n"
                "def test_follow_up_fixture():\n"
                "    assert add(2, 3) == 5\n"
            )
            commit_all(tester, "add follow-up tester fixture")
            stopped = self.call_hook(
                {
                    **base,
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": "done\nTESTER_RESULT: tests_ready",
                    "stop_hook_active": False,
                },
                cwd=tester,
            )
            self.assertNotIn("systemMessage", stopped, stopped)
            stop_output = stopped
            self.assertNotIn("systemMessage", stop_output, stop_output)
            self.assertEqual(
                run_cli("status", "--run", run_path, env=self.env).returncode, 0
            )
            assert_status_one_of(
                run_cli("integrate-tests", "--run", run_path, env=self.env),
                {"READY", "NOOP"},
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
                env=self.env,
            )
            assert_status(prepared, "READY", rc=0)
            return repo, run_path, session_id, tester, initial_turn, prepared

        mutations = {
            "agent_id": "other-agent",
            "dispatch_id": "0" * 32,
            "role_head": "0" * 40,
            "dirty_paths": ["tests/uncommitted.py"],
        }
        for key, value in mutations.items():
            _repo, run_path, session_id, tester, previous_turn, prepared = (
                prepare_author(f"follow-up-negative-{key.replace('_', '-')}")
            )
            route = self.route_path(run_path)
            original_route = route.read_bytes()
            mutated = json.loads(original_route)
            mutated["tester_start_attestation"][key] = value
            self.write_private_json(route, mutated)
            turn_id = f"negative-{key.replace('_', '-')}-turn"
            captured = self.call_hook(
                {
                    "hook_event_name": "SubagentStart",
                    "cwd": str(tester.parent),
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "agent_id": agent_id,
                    "agent_type": "tester",
                },
                cwd=tester.parent,
            )
            self.assertNotIn("systemMessage", captured, (key, captured))
            self.assertEqual(len(self.runtime_json(turn_id)), 1)
            route.write_bytes(original_route)
            route.chmod(0o600)
            rejected = run_cli("status", "--run", run_path, env=self.env)
            self.assertNotEqual(rejected.returncode, 0, (key, rejected.data))
            after_reject = load_ledger(run_path)
            pending = after_reject["pending_agent_turns"]["tester"]
            self.assertEqual(after_reject["phase"], "continuity_failure")
            self.assertEqual(
                pending["dispatch_id"], prepared.data["dispatch_id"]
            )
            self.assertEqual(
                after_reject["agents"]["tester"]["turn_id"], previous_turn
            )

        _repo, run_path, session_id, tester, previous_turn, prepared = prepare_author(
            "follow-up-valid"
        )
        for purpose in ("author", "blackbox"):
            if purpose == "blackbox":
                prerequisite_ledger = load_ledger(run_path)
                builder = Path(prerequisite_ledger["worktrees"]["builder"]["path"])
                candidate = head(builder)
                verified = run_cli("verify", "--run", run_path, env=self.env)
                assert_status(verified, "PASS", rc=0)
                self.assertEqual(verified.data.get("head"), candidate)
                ensure_test_effectiveness(run_path)
                prerequisite_ledger = load_ledger(run_path)
                self.assertEqual(
                    prerequisite_ledger["evidence"]["machine"]["accepted_head"],
                    candidate,
                )
                test_effectiveness = prerequisite_ledger["evidence"][
                    "test_effectiveness"
                ]
                self.assertIn(
                    candidate,
                    {
                        test_effectiveness.get("head"),
                        test_effectiveness.get("observed_head"),
                        test_effectiveness.get("accepted_head"),
                    },
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
                    "blackbox",
                    env=self.env,
                )
                assert_status(prepared, "READY", rc=0)
            route = self.route_path(run_path)
            attestation = json.loads(route.read_text())["tester_start_attestation"]
            self.assertEqual(attestation["kind"], "follow-up")
            self.assertEqual(attestation["agent_id"], agent_id)
            self.assertEqual(attestation["dispatch_id"], prepared.data["dispatch_id"])
            self.assertEqual(attestation["previous_turn_id"], previous_turn)
            self.assertEqual(attestation["purpose"], purpose)
            self.assertEqual(attestation["role_head"], head(tester))
            self.assertEqual(attestation["dirty_paths"], [])
            turn_id = f"valid-{purpose}-turn"
            event = {
                "cwd": str(tester.parent),
                "session_id": session_id,
                "turn_id": turn_id,
                "agent_id": agent_id,
                "agent_type": "tester",
            }
            started = self.call_hook_with_ledger_and_git_unavailable(
                {**event, "hook_event_name": "SubagentStart"},
                cwd=tester.parent,
                run_path=run_path,
            )
            self.assertNotIn("systemMessage", started, started)
            accepted = started
            self.assertNotIn("systemMessage", accepted, accepted)
            journals = self.runtime_json(turn_id)
            self.assertEqual(len(journals), 1, journals)
            envelope = json.loads(journals[0].read_text())
            self.assertEqual(envelope["tester_baseline"], attestation)
            try:
                from runtime.codex_builder_loop.lifecycle import event_id
            except ModuleNotFoundError:
                self.fail("public lifecycle.event_id is missing")
            self.assertEqual(envelope["event_id"], event_id(envelope))
            start_fold = run_cli("status", "--run", run_path, env=self.env)
            self.assertEqual(start_fold.returncode, 0, start_fold.data)
            after_start = load_ledger(run_path)
            self.assertEqual(
                after_start["agents"]["tester"]["turn_id"], turn_id
            )
            terminal_result = "tests_ready" if purpose == "author" else "pass"
            stopped = self.call_hook(
                {
                    **event,
                    "hook_event_name": "SubagentStop",
                    "last_assistant_message": (
                        f"done\nTESTER_RESULT: {terminal_result}"
                    ),
                    "stop_hook_active": False,
                },
                cwd=tester.parent,
            )
            self.assertNotIn("systemMessage", stopped, stopped)
            terminal_fold = run_cli("status", "--run", run_path, env=self.env)
            self.assertEqual(terminal_fold.returncode, 0, terminal_fold.data)
            after_terminal = load_ledger(run_path)
            self.assertEqual(after_terminal["agents"]["tester"]["turn_id"], turn_id)
            self.assertEqual(
                after_terminal["agents"]["tester"]["result"], terminal_result
            )
            self.assertIsNone(after_terminal["pending_agent_turns"]["tester"])
            if purpose == "author":
                self.assertEqual(
                    after_terminal["agents"]["tester"]["result"], "tests_ready"
                )
                assert_status_one_of(
                    run_cli("integrate-tests", "--run", run_path, env=self.env),
                    {"READY", "NOOP"},
                    rc=0,
                )
            previous_turn = turn_id

    def test_real_hook_receipts_terminal_while_runtime_lock_is_held(self) -> None:
        repo, run_path, session_id = self.start(label="receipt")
        start = self.direct_event(
            repo,
            session_id,
            role="tester",
            agent_id="receipt-tester",
            turn_id="receipt-start",
            event="start",
        )
        assert_status(start, "READY", rc=0)
        initial = self.direct_event(
            repo,
            session_id,
            role="tester",
            agent_id="receipt-tester",
            turn_id="receipt-start",
            event="idle",
            result="tests_ready",
        )
        assert_status(initial, "READY", rc=0)
        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "tester",
            "--agent-id",
            "receipt-tester",
            "--purpose",
            "author",
            env=self.env,
        )
        assert_status(prepared, "READY", rc=0)

        lock_path = run_path / "runtime.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            process, stdout, stderr = self.hook_process(
                {
                    "hook_event_name": "SubagentStop",
                    "cwd": str(repo.parent),
                    "session_id": session_id,
                    "turn_id": "receipt-follow-up",
                    "agent_id": "receipt-tester",
                    "agent_type": "tester",
                    "last_assistant_message": "done\nTESTER_RESULT: tests_ready",
                    "stop_hook_active": False,
                },
                cwd=repo.parent,
            )
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            exited_while_locked = process.poll() is not None
            queued = (
                self.runtime_json("receipt-follow-up")
                if exited_while_locked
                else []
            )
            queued_envelope = (
                json.loads(queued[0].read_text()) if len(queued) == 1 else None
            )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        if process.poll() is None:
            process.wait(timeout=20)
        stdout.seek(0)
        stderr.seek(0)
        hook_output = json.loads([line for line in stdout.read().splitlines() if line][-1])
        hook_error = stderr.read()
        stdout.close()
        stderr.close()
        self.assertTrue(exited_while_locked, hook_error)
        self.assertEqual(process.returncode, 0, hook_error)
        self.assertNotIn("systemMessage", hook_output, hook_output)
        self.assertEqual(len(queued), 1, queued)
        self.assertIsNotNone(queued_envelope)
        if queued_envelope is None:
            raise AssertionError("terminal journal envelope was not captured")
        self.assertIsNone(queued_envelope["tester_baseline"])

        folded = run_cli(
            "status",
            "--repo",
            repo.parent,
            "--session-id",
            session_id,
            env=self.env,
        )
        self.assertIn(folded.data.get("status"), {"ACTIVE", "READY"}, folded.data)
        ledger = load_ledger(run_path)
        self.assertEqual(ledger["agents"]["tester"]["agent_id"], "receipt-tester")
        self.assertEqual(ledger["agents"]["tester"]["turn_id"], "receipt-follow-up")
        self.assertEqual(ledger["agents"]["tester"]["result"], "tests_ready")
        self.assertIsNone(ledger["pending_agent_turns"]["tester"])

    def test_malformed_queued_event_is_preserved_and_diagnosed(self) -> None:
        repo, run_path, session_id = self.start(label="malformed")
        started = self.direct_event(
            repo,
            session_id,
            role="tester",
            agent_id="malformed-tester",
            turn_id="malformed-start",
            event="start",
        )
        assert_status(started, "READY", rc=0)

        lock_path = run_path / "runtime.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            process, stdout, stderr = self.hook_process(
                {
                    "hook_event_name": "SubagentStop",
                    "cwd": str(repo),
                    "session_id": session_id,
                    "turn_id": "malformed-stop",
                    "agent_id": "malformed-tester",
                    "agent_type": "tester",
                    "last_assistant_message": "done\nTESTER_RESULT: tests_ready",
                    "stop_hook_active": False,
                },
                cwd=repo,
            )
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            exited_while_locked = process.poll() is not None
            queued = (
                [
                    path
                    for path in Path(self.runtime.name).rglob("*.json")
                    if path.is_file()
                    and "malformed-stop" in path.read_text(errors="replace")
                ]
                if exited_while_locked
                else []
            )
            malformed_path = queued[0] if len(queued) == 1 else None
            if malformed_path is not None:
                malformed_path.write_text("{broken\n")
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        if process.poll() is None:
            process.wait(timeout=20)
        stdout.close()
        stderr.close()
        self.assertTrue(exited_while_locked, "Hook did not durably enqueue under lock")
        self.assertEqual(len(queued), 1, queued)
        self.assertIsNotNone(malformed_path)
        if malformed_path is None:
            raise AssertionError("malformed journal path was not captured")
        diagnosed = run_cli("doctor", "--run", run_path, env=self.env)
        self.assertNotEqual(diagnosed.returncode, 0, diagnosed.data)
        self.assertIn("LIFECYCLE_JSON_INVALID", json.dumps(diagnosed.data))
        self.assertTrue(malformed_path.is_file())

    def test_follow_up_fold_is_idempotent_and_rejects_conflicting_replay(self) -> None:
        repo, run_path, session_id = self.start(label="fold")
        first = self.direct_event(
            repo,
            session_id,
            role="tester",
            agent_id="fold-tester",
            turn_id="fold-initial",
            event="start",
        )
        assert_status(first, "READY", rc=0)
        initial = self.direct_event(
            repo,
            session_id,
            role="tester",
            agent_id="fold-tester",
            turn_id="fold-initial",
            event="idle",
            result="tests_ready",
        )
        assert_status(initial, "READY", rc=0)
        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "tester",
            "--agent-id",
            "fold-tester",
            "--purpose",
            "author",
            env=self.env,
        )
        assert_status(prepared, "READY", rc=0)

        follow_up = self.direct_event(
            repo,
            session_id,
            role="tester",
            agent_id="fold-tester",
            turn_id="fold-follow-up",
            event="idle",
            result="tests_ready",
        )
        assert_status(follow_up, "READY", rc=0)
        replay = self.direct_event(
            repo,
            session_id,
            role="tester",
            agent_id="fold-tester",
            turn_id="fold-follow-up",
            event="idle",
            result="tests_ready",
        )
        assert_status(replay, "NOOP", rc=0)
        conflict = self.direct_event(
            repo,
            session_id,
            role="tester",
            agent_id="fold-tester",
            turn_id="fold-follow-up",
            event="idle",
            result="blocked",
        )
        self.assertNotEqual(conflict.returncode, 0, conflict.data)
        ledger = load_ledger(run_path)
        self.assertEqual(ledger["agents"]["tester"]["turn_id"], "fold-follow-up")
        self.assertEqual(ledger["agents"]["tester"]["result"], "tests_ready")

    def test_terminal_journal_blocks_new_route_until_old_entry_drains(self) -> None:
        repo, run_path, session_id = self.start(label="terminal-inbox")
        event = {
            "hook_event_name": "SubagentStart",
            "cwd": str(repo),
            "session_id": session_id,
            "turn_id": "terminal-inbox-turn",
            "agent_id": "terminal-inbox-tester",
            "agent_type": "tester",
        }
        accepted = self.call_hook(event, cwd=repo)
        self.assertNotIn("systemMessage", accepted, accepted)
        journal = self.runtime_json("terminal-inbox-turn")
        self.assertEqual(len(journal), 1, journal)
        journal_path = journal[0]
        journal_bytes = journal_path.read_bytes()
        old_route = self.route_path(run_path)
        old_route_bytes = old_route.read_bytes()
        run_cli("status", "--run", run_path, env=self.env)
        self.assertFalse(journal_path.exists())
        abandoned = run_cli(
            "abandon",
            "--run",
            run_path,
            "--reason",
            "terminal inbox fixture",
            env=self.env,
        )
        self.assertEqual(abandoned.returncode, 0, abandoned.data)

        old_route.parent.mkdir(parents=True, exist_ok=True)
        terminal_route = json.loads(old_route_bytes)
        terminal_route["accepting_events"] = False
        self.write_private_json(old_route, terminal_route)
        old_route_bytes = old_route.read_bytes()
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_bytes(journal_bytes)
        journal_path.chmod(0o600)
        new_plan = write_plan(repo, plan_markdown(head(repo)), name="next-plan.md")
        blocked = run_cli(
            "start",
            "--repo",
            repo,
            "--plan",
            new_plan,
            "--run",
            "next-run-after-terminal-inbox",
            "--session-id",
            session_id,
            env=self.env,
        )
        assert_status(blocked, "NEEDS_USER", rc=1)
        self.assertEqual(blocked.data.get("code"), "LIFECYCLE_DELIVERY_PENDING")
        self.assertEqual(journal_path.read_bytes(), journal_bytes)
        self.assertEqual(old_route.read_bytes(), old_route_bytes)

        status = run_cli("status", "--run", run_path, env=self.env)
        doctor = run_cli("doctor", "--run", run_path, env=self.env)
        diagnostics = json.dumps(
            {"status": status.data, "doctor": doctor.data},
            ensure_ascii=False,
        ).lower()
        self.assertIn("queued", diagnostics)
        self.assertIn("terminal", diagnostics)
        self.assertEqual(journal_path.read_bytes(), journal_bytes)
        self.assertEqual(old_route.read_bytes(), old_route_bytes)

        journal_path.unlink()
        started = run_cli(
            "start",
            "--repo",
            repo,
            "--plan",
            new_plan,
            "--run",
            "next-run-after-terminal-inbox",
            "--session-id",
            session_id,
            env=self.env,
        )
        assert_status(started, "READY", rc=0)
        self.assertFalse(journal_path.exists())
        current_route = self.route_path(Path(started.data["run_path"]))
        self.assertNotEqual(current_route.read_bytes(), old_route_bytes)

    def test_blackbox_reviewer_hooks_route_from_ancestor_and_resume(self) -> None:
        repo = init_repo()
        self.repos.append(repo)
        plan = write_plan(
            repo,
            plan_markdown(head(repo), include_e2e=True),
            name="review-plan.md",
        )
        session_id = repo_session_id(repo, "reviewer-ancestor")
        runtime_env = fixture_runtime_env(repo)
        started, run_path = start_run(repo, plan, session_id=session_id)
        builder, tester = worktrees_from(started, run_path)
        tester_agent_id = register_agent(run_path, "tester")
        assert_status_one_of(
            run_cli("integrate-tests", "--run", run_path),
            {"READY", "NOOP"},
            rc=0,
        )
        assert_status(run_cli("verify", "--run", run_path), "PASS", rc=0)
        ensure_test_effectiveness(run_path)
        candidate = head(builder)
        blackbox_follow_up = run_cli(
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
        assert_status(blackbox_follow_up, "READY", rc=0)
        blackbox_turn = "reviewer-prerequisite-blackbox-turn"
        blackbox_event = {
            "cwd": str(tester),
            "session_id": session_id,
            "turn_id": blackbox_turn,
            "agent_id": tester_agent_id,
            "agent_type": "tester",
        }
        blackbox_start = self.call_hook(
            {**blackbox_event, "hook_event_name": "SubagentStart"},
            cwd=tester,
            runtime_env=runtime_env,
        )
        self.assertNotIn("systemMessage", blackbox_start, blackbox_start)
        start_fold = run_cli("status", "--run", run_path)
        self.assertEqual(start_fold.returncode, 0, start_fold.data)
        blackbox_stop = self.call_hook(
            {
                **blackbox_event,
                "hook_event_name": "SubagentStop",
                "last_assistant_message": "passed\nTESTER_RESULT: pass",
                "stop_hook_active": False,
            },
            cwd=tester,
            runtime_env=runtime_env,
        )
        self.assertNotIn("systemMessage", blackbox_stop, blackbox_stop)
        terminal_fold = run_cli("status", "--run", run_path)
        self.assertEqual(terminal_fold.returncode, 0, terminal_fold.data)
        after_blackbox = load_ledger(run_path)
        self.assertEqual(after_blackbox["agents"]["tester"]["turn_id"], blackbox_turn)
        self.assertEqual(after_blackbox["agents"]["tester"]["result"], "pass")
        self.assertIsNone(after_blackbox["pending_agent_turns"]["tester"])
        record_evidence(
            run_path,
            "e2e_verified",
            candidate,
            agent_id=tester_agent_id,
            command_argv=[
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py",
            ],
        )
        base = {
            "cwd": str(repo.parent),
            "session_id": session_id,
            "turn_id": "reviewer-ancestor-turn",
            "agent_id": "reviewer-ancestor-agent",
            "agent_type": "reviewer",
        }
        start_output = self.call_hook(
            {**base, "hook_event_name": "SubagentStart"},
            cwd=repo.parent,
            runtime_env=runtime_env,
        )
        self.assertNotIn("systemMessage", start_output, start_output)
        journals = self.runtime_json(
            "reviewer-ancestor-turn", runtime_env["XDG_RUNTIME_DIR"]
        )
        self.assertEqual(len(journals), 1, journals)
        self.assertIsNone(json.loads(journals[0].read_text())["tester_baseline"])
        run_cli("status", "--run", run_path)
        started_ledger = load_ledger(run_path)
        self.assertTrue(
            started_ledger["agents"]["reviewer"]["review_prerequisites"]["start"][
                "satisfied"
            ]
        )

        stop_output = self.call_hook(
            {
                **base,
                "hook_event_name": "SubagentStop",
                "last_assistant_message": "finding\nREVIEW_RESULT: findings",
                "stop_hook_active": False,
            },
            cwd=repo.parent,
            runtime_env=runtime_env,
        )
        self.assertNotIn("systemMessage", stop_output, stop_output)
        run_cli("status", "--run", run_path)
        ledger = load_ledger(run_path)
        self.assertEqual(
            ledger["agents"]["reviewer"]["turn_id"], "reviewer-ancestor-turn"
        )
        recorded = run_cli(
            "record-problems",
            "--run",
            run_path,
            "--source",
            "reviewer",
            "--source-id",
            "reviewer-ancestor-turn",
            "--manifest",
            "-",
            input_text=json.dumps(
                problem_report(
                {
                    "key": "reviewer-ancestor-finding",
                    "summary": "Reviewer requested follow-up",
                    "details": "The real ancestor-cwd Reviewer Hook reported one finding.",
                    "owner": "builder",
                }
                )
            ),
        )
        assert_status(recorded, "READY", rc=0)
        prepared = run_cli(
            "prepare-follow-up",
            "--run",
            run_path,
            "--role",
            "reviewer",
            "--agent-id",
            "reviewer-ancestor-agent",
            "--purpose",
            "review",
        )
        assert_status(prepared, "READY", rc=0)

    def test_blackbox_lifecycle_diagnostics_matrix(self) -> None:
        result = run_process(
            [
                sys.executable,
                "-m",
                "unittest",
                "-v",
                "tests.test_lifecycle_delivery_contract.LifecycleDeliveryContractTest.test_missing_and_stale_locator_reject_then_accept_the_same_turn",
                "tests.test_lifecycle_delivery_contract.LifecycleDeliveryContractTest.test_malformed_queued_event_is_preserved_and_diagnosed",
                "tests.test_lifecycle_delivery_contract.LifecycleDeliveryContractTest.test_follow_up_fold_is_idempotent_and_rejects_conflicting_replay",
                "tests.test_lifecycle_delivery_contract.LifecycleDeliveryContractTest.test_terminal_journal_blocks_new_route_until_old_entry_drains",
                "tests.test_planning_v3_e2e_contract.PlanningV3E2EContractTest.test_existing_active_v2_run_completes_without_identity_replacement",
            ],
            cwd=ROOT,
            env=self.env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_routing_diagnostics_and_public_schema_fail_closed(self) -> None:
        repo, run_path, session_id = self.start(label="diagnostics")
        ancestor_status = run_cli(
            "status",
            "--repo",
            repo.parent,
            "--session-id",
            session_id,
            env=self.env,
        )
        self.assertEqual(ancestor_status.data.get("run_id"), run_path.name)

        event_schema = ROOT / "schema" / "codex-agent-event.schema.json"
        self.assertTrue(event_schema.is_file(), event_schema)
        schema = json.loads(event_schema.read_text())
        self.assertEqual(schema.get("additionalProperties"), False)
        required = set(schema.get("required", []))
        self.assertTrue(
            {"session_id", "role", "agent_id", "turn_id", "event"}.issubset(required),
            required,
        )

        doctor = run_cli("doctor", "--run", run_path, env=self.env)
        self.assertEqual(doctor.returncode, 0, doctor.data)
        self.assertNotIn("LIFECYCLE_JSON_INVALID", json.dumps(doctor.data))

        added_runtime = run_cli(
            "status", "--run", run_path, env=self.env
        )
        self.assertNotEqual(added_runtime.data.get("status"), "FATAL", added_runtime.data)
        changed = subprocess.run(
            [
                "git",
                "-C",
                ROOT,
                "diff",
                "--diff-filter=A",
                "--name-only",
                SPEC_HEAD,
                "HEAD",
                "--",
                "runtime/codex_builder_loop",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertEqual(
            changed.stdout.splitlines(),
            ["runtime/codex_builder_loop/lifecycle.py"],
        )


if __name__ == "__main__":
    unittest.main()
