"""Root handoff regressions; platform responses are explicitly synthetic."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import ROOT, CLI, cleanup_repo, commit_all, git, init_repo, run_process
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.store import read_ledger, ledger_path
from runtime.codex_builder_loop.assurance_v4.models import digest
from runtime.codex_builder_loop.native_driver import cli
from runtime.codex_builder_loop.native_driver.app_server import AppServerError, TurnResult
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator, NativeDriverError
from tests.test_assurance_v4_contract import contract_for
from tests.helpers import native_routing_recovery as routing
from tests.helpers.native_delivery_recovery import complete_delivery


CAPABILITY = SimpleNamespace(runtime_version="fixture", protocol_schema_digest="b" * 64,
                             executable_identity=None, thread_compaction=False)
TIMEOUT = "NATIVE_APP_SERVER_TIMEOUT"


class Transport:
    """In-memory platform boundary, including an observable cleanup receipt."""
    def __init__(self, **_kwargs):
        self.exits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.exits += 1

    @property
    def generation(self):
        return self.runtime_snapshot()["generation"]

    @property
    def cleanup_observation(self):
        return {"state": "cleaned", "term_attempt": 1, "kill_attempt": 0,
                "returncode": 0, "process_group_gone": True}

    def runtime_snapshot(self):
        return {"contract_version": 2, "generation": "g" * 64, "state": "ready",
                "executable_identity": {"requested": "/fixture/codex", "resolved": "/fixture/codex",
                    "sha256": "a" * 64, "runtime_version": "fixture",
                    "protocol_schema_digest": "b" * 64, "protocol_canary_digest": None},
                "process_identity": {"pid": 99999999, "pgid": 99999999, "starttime": "1",
                    "parent_pid": 1, "argv_digest": "c" * 64,
                    "executable_identity_digest": "e" * 64, "started_at": "2026-10-21T00:00:00+00:00"}}

    def diagnostic_receipt(self, *, failure_code, turn_error=None):
        receipt = {"schema_version": 1, "failure_code": failure_code,
                   "transport_generation": self.runtime_snapshot()["generation"],
                   "stderr_bytes": 0, "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                   "stderr_summary": "", "stderr_truncated": False, "redaction_count": 0,
                   "turn_error": turn_error, "cleanup_observation": self.cleanup_observation}
        return {**receipt, "receipt_digest": digest(receipt)}


class RootHandoffEntryTest(unittest.TestCase):
    def exercise(self, command, error_code, *, fail_before_handoff=False):
        old_action = {"action": "tester_author", "action_id": "a" * 64,
                      "reason": "tester_source_missing"}
        new_action = {**old_action, "action_id": "b" * 64, "reason": "tester_missing"}
        calls, coordinators = [], []
        intent = {**new_action, "role": "tester", "state": "in_flight",
                  "thread_id": "tester-thread", "turn_id": "tester-turn",
                  "activation_state": "activated", "attempt": 1, "generation": 1}

        class FakeCore:
            def start(self, **_kwargs):
                return {"candidate_worktree": str(ROOT)}

            def call(self, command, *args, input_value=None):
                calls.append((command, args, input_value))
                if command == "driver-context":
                    return {"driver_runtime": cli._root_runtime("root-session"),
                            "facets": {"execution": {"agents": {"tester": {
                                "thread_id": "tester-thread", "agent_id": "tester"}}}},
                            "dispatch_intent": copy.deepcopy(intent) if len(coordinators) == 2 else None}
                if command == "driver-next":
                    return {"action": "scan_doc_references"}
                if command in {"bind-native-transport", "record-transport-cleanup", "record-driver-failure"}:
                    return {"status": "ACTIVE"}
                if command == "retry-dispatch":
                    return {"status": "ACTIVE", "dispatch_intent": {**intent, "attempt": 2}}
                if command == "complete-driver-failure":
                    return {"phase": "failed", "driver_failure": {"state": "terminal"}}
                raise AssertionError(command)

        class Coordinator(NativeCoordinator):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                coordinators.append(self)

            def run(self):
                self.current_action = old_action if self.transport is None else new_action
                if self.transport is None and not fail_before_handoff:
                    return {"status": "TRANSPORT_HANDOFF"}
                raise AppServerError("injected transport failure", code=error_code,
                                     details={"timeout_seconds": 120})

        transport = Transport()
        args = [command, "--repo", str(ROOT), "--run", "handoff-entry"]
        if command == "start":
            args += ["--session-id", "root-session", "--contract", "-"]
        output = StringIO()
        with (patch.object(cli, "CorePort", return_value=FakeCore()),
              patch.object(cli, "NativeCoordinator", Coordinator),
              patch.object(cli, "AppServerTransport", return_value=transport),
              patch.object(cli, "probe_app_server", return_value=CAPABILITY),
              patch("sys.stdin", StringIO(json.dumps({"execution": {
                  "builder_runtime": {"schema_version": 1, "mode": "root_session"}}}))),
              redirect_stdout(output)):
            rc = cli.main(args)
        return rc, json.loads(output.getvalue().splitlines()[-1]), calls, transport, coordinators

    def test_cli_handoff_routes_retry_to_the_executing_dispatch(self):
        for command in ("start", "resume"):
            for code in (TIMEOUT, "NATIVE_APP_SERVER_DISCONNECTED"):
                with self.subTest(command=command, code=code):
                    rc, payload, calls, transport, coordinators = self.exercise(command, code)
                    self.assertEqual(rc, 1, payload)
                    self.assertEqual(payload["status"], "ACTIVE")
                    retries = [args for name, args, _ in calls if name == "retry-dispatch"]
                    self.assertEqual(len(retries), 1)
                    self.assertEqual(retries[0][retries[0].index("--action-id") + 1], "b" * 64)
                    self.assertNotIn("record-driver-failure", [name for name, _, _ in calls])
                    self.assertEqual(transport.exits, 1)
                    self.assertEqual(sum(name == "record-transport-cleanup" for name, _, _ in calls), 1)
                    self.assertEqual(len(coordinators), 2)

    def test_nonretryable_handoff_failure_records_the_new_action(self):
        rc, payload, calls, _, _ = self.exercise("resume", "NATIVE_UNKNOWN_FAILURE")
        self.assertEqual(rc, 2)
        self.assertEqual(payload["phase"], "failed")
        failure = next(value for name, _, value in calls if name == "record-driver-failure")
        self.assertEqual(failure["action"]["action_id"], "b" * 64)
        self.assertNotIn("retry-dispatch", [name for name, _, _ in calls])

    def test_failure_before_handoff_retains_the_original_action(self):
        rc, _, calls, transport, coordinators = self.exercise(
            "resume", "NATIVE_UNKNOWN_FAILURE", fail_before_handoff=True)
        self.assertEqual(rc, 2)
        failure = next(value for name, _, value in calls if name == "record-driver-failure")
        self.assertEqual(failure["action"]["action_id"], "a" * 64)
        self.assertEqual(len(coordinators), 1)
        self.assertEqual(transport.exits, 0)


class RootHandoffLedgerTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo({"src/calc.py": "def add(a, b):\n    return a + b - 1\n"})
        self.addCleanup(cleanup_repo, self.repo)
        self.run_id = "root-handoff-recovery"
        self.f = routing.RootRoutingFixture()
        self.f.repo, self.f.run_id = self.repo, self.run_id
        self.mode = "timeout"
        self.transports = []
        self.turns = []
        self.created_threads = []
        self.resumed_threads = []
        self.compaction_available = False
        self.closed_rpc_calls = []

    def ledger(self):
        return read_ledger(self.repo, self.run_id)

    def platform(self, **kwargs):
        owner = self

        class Platform(Transport):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.identity = str(len(owner.transports) + 1).zfill(64)

            def runtime_snapshot(self):
                return {**super().runtime_snapshot(), "generation": self.identity}

            def start_thread(self, **_kwargs):
                self.assert_open()
                owner.created_threads.append("tester-thread")
                return "tester-thread"

            def resume_thread(self, *, thread_id, **_kwargs):
                self.assert_open()
                owner.resumed_threads.append(thread_id)
                return {"id": thread_id}

            def read_thread(self, thread_id):
                self.assert_open()
                return {"id": thread_id, "turns": []}

            def assert_open(self):
                if self.exits:
                    owner.closed_rpc_calls.append(self.identity)
                    raise AppServerError("App Server is not running", code="NATIVE_APP_SERVER_NOT_RUNNING")

            def run_turn(self, *, thread_id, on_started, **_kwargs):
                self.assert_open()
                pending = owner.ledger()["dispatch_intent"]
                turn_id = f"turn-{len(owner.turns)}"
                on_started(turn_id)
                owner.turns.append(copy.deepcopy(owner.ledger()["dispatch_intent"]))
                if pending["action"] != "tester_author":
                    raise NativeDriverError("synthetic author boundary", code="FIXTURE_AUTHOR_COMPLETE",
                                            status="NEEDS_USER")
                if owner.mode == "timeout":
                    raise AppServerError("Codex App Server read timed out", code=TIMEOUT,
                                         details={"timeout_seconds": 120})
                source = owner.ledger()["facets"]["execution"]["tester_source"]
                worktree = Path(source["worktree"])
                (worktree / "tests/test_recovery.py").write_text(
                    "import unittest\nfrom src.calc import add\n"
                    "class RecoveryTest(unittest.TestCase):\n"
                    "    def test_negative(self):\n        self.assertEqual(add(-1, -2), -3)\n")
                (worktree / "tests/__init__.py").write_text("")
                head = commit_all(worktree, "author recovery test")
                result = {"result": "tests_ready", "problem_report": None, "proof_spec": None,
                          "evidence_report": {"schema_version": 1, "kind": "tester", "status": "pass",
                              "candidate_head": owner.ledger()["facets"]["execution"]["candidate_head"],
                              "producer": {"role": "tester", **source["agent"]},
                              "details": {"result": "tests_ready", "source_head": head, "files": [{
                                  "path": "tests/test_recovery.py", "blob": git(worktree, "rev-parse", head + ":tests/test_recovery.py")}]}}}
                return TurnResult(turn_id=turn_id, status="completed", text=json.dumps(result))

        platform = Platform(**kwargs)
        self.transports.append(platform)
        return platform

    def invoke(self, command="resume", *extra, contract=None):
        from datetime import datetime, timedelta, timezone
        output = StringIO()
        args = [command, "--repo", str(self.repo), "--run", self.run_id, *extra]
        if contract is not None:
            args += ["--session-id", "root-session", "--contract", "-"]
        # Advance only the coordinator clock, without modifying ledger deadlines.
        def coordinator(**kwargs):
            return NativeCoordinator(**kwargs, now_fn=lambda: datetime.now(timezone.utc) + timedelta(days=1))
        capability = SimpleNamespace(**{**vars(CAPABILITY), "thread_compaction": self.compaction_available})
        with (patch.object(cli, "probe_app_server", return_value=capability),
              patch.object(cli, "AppServerTransport", side_effect=self.platform),
              patch.object(cli, "NativeCoordinator", side_effect=coordinator),
              patch("sys.stdin", StringIO(json.dumps(contract))), redirect_stdout(output)):
            rc = cli.main(args)
        return rc, json.loads(output.getvalue().splitlines()[-1])

    def prepare_root(self):
        contract = contract_for(self.repo)
        contract["execution"].update(driver_enforced=True, agents={},
            builder_runtime={"schema_version": 1, "mode": "root_session"})
        contract["assurance"]["required"].insert(2, "proof")
        rc, payload = self.invoke("start", contract=contract)
        self.assertEqual((rc, payload["status"]), (1, "BUILDER_HANDOFF"))
        ledger = self.ledger()
        self.f.candidate = Path(ledger["candidate_worktree"])
        action = driver.next_action(self.repo, self.run_id)
        core.begin_dispatch(self.repo, self.run_id, action_id=action["action_id"],
            action=action["action"], role="builder", thread_id=None,
            owner_session_id="root-session", prompt_digest=payload["prompt_digest"],
            output_schema_digest=payload["output_schema_digest"],
            work_unit_id=action.get("work_unit_id"),
            context_projection_digest=action.get("context_projection_digest"))
        (self.f.candidate / "src/calc.py").write_text("def add(a, b):\n    return sum((a, b))\n")
        commit_all(self.f.candidate, "implement calculator")
        core.complete_dispatch(self.repo, self.run_id, action_id=action["action_id"],
            owner_session_id="root-session", result_value={"result": "implemented", "evidence_report": None,
                                                         "problem_report": None, "proof_spec": None})
        core.apply_dispatch_result(self.repo, self.run_id, action_id=action["action_id"],
                                   driver_runtime_kind="native", owner_session_id="root-session")

    def assert_retry(self, payload):
        ledger = self.ledger()
        self.assertEqual(ledger["phase"], "active", payload)
        self.assertIsNone(ledger["driver_failure"])
        events = [e["kind"] for e in ledger["events"]]
        self.assertIn("dispatch_retry_scheduled", events, payload)
        self.assertNotIn("driver_failure_recorded", events)
        self.assertNotIn("run_failed", events)
        self.assertEqual(ledger["driver_runtime"]["native_transport"]["state"], "cleaned")
        return ledger

    def test_timeout_through_root_handoff_resumes_same_tester_and_completes_delivery(self):
        self.prepare_root()
        target = git(self.repo, "rev-parse", "HEAD")
        rc, payload = self.invoke()
        self.assertEqual(rc, 1, payload)
        after = self.assert_retry(payload)
        pending = after["dispatch_intent"]
        self.assertEqual((pending["attempt"], pending["generation"]), (2, 1))
        self.assertEqual(pending["thread_id"], "tester-thread")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), target)
        self.assertEqual(git(self.f.candidate, "status", "--porcelain"), "")
        self.mode = "success"
        rc, payload = self.invoke()
        self.assertEqual((rc, payload.get("code")), (1, "FIXTURE_AUTHOR_COMPLETE"), payload)
        first, second = self.turns[:2]
        for field in ("action_id", "thread_id", "prompt_digest", "output_schema_digest", "generation"):
            self.assertEqual(first[field], second[field], field)
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(self.created_threads, ["tester-thread"])
        self.assertIn("tester-thread", self.resumed_threads)
        ledger = self.ledger()
        self.f.action = ledger["dispatch_intent"]
        self.assertEqual(self.f.action["action"], "tester_proof")
        core.complete_dispatch(self.repo, self.run_id, action_id=self.f.action["action_id"],
                               result_value=self.f.proof_result())
        self.f.apply()
        complete_delivery(self.f)
        final = self.ledger()
        self.assertEqual(final["phase"], "finalized")
        for event in ("builder_checkpointed", "tester_source_integrated"):
            self.assertEqual(sum(e["kind"] == event for e in final["events"]), 1)
        self.assertTrue(all(t.exits == 1 for t in self.transports))

    def test_exhaustion_preserves_run_and_only_explicit_reason_renews(self):
        self.prepare_root()
        for _ in range(3):
            rc, payload = self.invoke()
            self.assertEqual(rc, 1, payload)
        self.assert_retry(payload)
        exhausted = self.ledger()["dispatch_intent"]
        self.assertEqual((exhausted["state"], exhausted["attempt"], exhausted["generation"]),
                         ("exhausted", 3, 1))
        count = len(self.turns)
        rc, payload = self.invoke()
        self.assertEqual((rc, payload.get("code")), (1, "NATIVE_DISPATCH_RETRY_EXHAUSTED"), payload)
        self.assertEqual(len(self.turns), count)
        self.assertEqual(self.ledger()["dispatch_intent"]["generation"], 1)
        rc, payload = self.invoke("resume", "--reason", "fixture authorizes another generation")
        self.assertEqual((rc, payload["status"]), (1, "ACTIVE"), payload)
        renewed = self.ledger()["dispatch_intent"]
        self.assertEqual(renewed["generation"], 2)
        self.assertEqual(renewed["thread_id"], exhausted["thread_id"])
        self.assertEqual(renewed["action_id"], exhausted["action_id"])
        self.assertEqual(self.turns[-1]["attempt"], 1)
        self.assertEqual(self.turns[-1]["generation"], 2)
        self.assertIsNone(self.ledger()["driver_failure"])
        renewal = [e["details"] for e in self.ledger()["events"] if e["kind"] == "dispatch_renewed"]
        self.assertEqual(len(renewal), 1)
        self.assertEqual((renewal[0]["previous_generation"], renewal[0]["previous_attempt"]), (1, 3))
        self.assertEqual(renewal[0]["thread_id"], exhausted["thread_id"])

    def test_compaction_enabled_timeout_exhaustion_never_uses_closed_transport(self):
        self.compaction_available = True
        self.prepare_root()
        for _ in range(3):
            rc, payload = self.invoke()
            self.assertEqual(rc, 1, payload)
        self.assertEqual(payload.get("code"), "NATIVE_DISPATCH_RETRY_EXHAUSTED", payload)
        self.assertEqual(payload["transport_retry"]["source_code"], TIMEOUT)
        self.assertEqual(self.closed_rpc_calls, [])
        ledger = self.assert_retry(payload)
        kinds = [event["kind"] for event in ledger["events"]]
        self.assertEqual(kinds.count("dispatch_retry_scheduled"), 2)
        self.assertEqual(kinds.count("dispatch_retry_exhausted"), 1)
        self.assertEqual(ledger["dispatch_intent"]["attempt"], 3)

    def test_exhaustion_queries_and_retrospective_share_readonly_facts(self):
        self.prepare_root()
        for _ in range(3):
            self.invoke()
        before = ledger_path(self.repo, self.run_id).read_bytes()
        refs = git(self.repo, "show-ref")
        expected = {"code": "NATIVE_DISPATCH_RETRY_EXHAUSTED", **{
            field: self.ledger()["dispatch_intent"].get(field)
            for field in ("action_id", "action", "role", "thread_id", "attempt", "generation", "failure_code")}}
        for command, expected_rc in (("status", 0), ("doctor", 1)):
            rc, payload = self.invoke(command)
            self.assertEqual((rc, payload["status"]), (expected_rc, "NEEDS_USER"), payload)
            self.assertEqual(payload["dispatch_exhaustion"], expected)
        self.assertEqual(core.status(self.repo, self.run_id)["status"], "NEEDS_USER")
        self.assertEqual(core.driver_context(self.repo, self.run_id)["dispatch_exhaustion"], expected)
        retro = core.retrospective_status(self.repo, "root-session")
        self.assertEqual(retro["status"], "REQUIRED", retro)
        self.assertTrue(retro["snapshot"]["snapshot_digest"])
        self.assertEqual(retro["snapshot"]["runs"][0]["phase"], "active")
        self.assertEqual(ledger_path(self.repo, self.run_id).read_bytes(), before)
        self.assertEqual(git(self.repo, "show-ref"), refs)
        snapshot = retro["snapshot"]
        core.record_retrospective(self.repo, "root-session", {
            "schema_version": 2, "snapshot_digest": snapshot["snapshot_digest"],
            "issue_updates": [], "dispositions": [
                {"signal_id": signal["signal_id"], "disposition": "needs-user",
                 "reason": "Fixture awaits a recovery decision"}
                for signal in snapshot["signals"]],
        })
        pending = core.retrospective_status(self.repo, "root-session")
        self.assertEqual(pending["status"], "NEEDS_USER", pending)
        self.assertIn("dispatch_retry_exhausted", pending["required_user_block"])
        for message, blocked in (("done", True), (pending["required_user_block"], False)):
            result = run_process([sys.executable, ROOT / "hooks/builder-loop.py"], cwd=self.repo,
                env={"BUILDER_LOOP_CLI": str(CLI)}, input_text=json.dumps({
                    "hook_event_name": "Stop", "cwd": str(self.repo), "session_id": "root-session",
                    "stop_hook_active": False, "last_assistant_message": message}))
            self.assertEqual(result.returncode, 0, result.stderr)
            hook = json.loads(result.stdout)
            self.assertEqual(hook.get("decision") == "block", blocked, hook)
        self.assertEqual(ledger_path(self.repo, self.run_id).read_bytes(), before)

        # Mixed-session aggregation must still respect a genuinely active run.
        matching, _ = core._matching_retrospective_ledgers(self.repo, "root-session")
        other = copy.deepcopy(self.ledger())
        other.update(run_id="still-working", dispatch_intent=None)
        original_next = driver.next_action
        def mixed_next(repo, run_id):
            return ({"status": "CONTINUE", "action": "builder_implement"}
                    if run_id == "still-working" else original_next(repo, run_id))
        with (patch.object(core, "_matching_retrospective_ledgers", return_value=(
                  [*matching, (other, digest(other))], [])),
              patch.object(driver, "next_action", side_effect=mixed_next)):
            mixed = core.retrospective_status(self.repo, "root-session")
        self.assertEqual(mixed["status"], "ACTIVE")
        self.assertEqual(mixed["active_runs"], ["still-working"])
        self.assertEqual(mixed["exhausted_dispatches"], {self.run_id: expected})

        self.invoke("resume", "--reason", "fixture explicitly authorizes the next generation")
        self.assertEqual(core.retrospective_status(self.repo, "root-session")["status"], "ACTIVE")
        for _ in range(2):
            self.invoke()
        stale = core.retrospective_status(self.repo, "root-session")
        self.assertEqual(stale["status"], "STALE", stale)
        self.assertNotEqual(stale["snapshot"]["snapshot_digest"], snapshot["snapshot_digest"])


if __name__ == "__main__":
    unittest.main()
