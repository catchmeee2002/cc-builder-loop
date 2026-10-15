from __future__ import annotations
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
import json
import os
import subprocess
import sys
from unittest.mock import patch

from harness import ROOT, commit_all, git
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.native_driver.app_server import AppServerError
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from runtime.codex_builder_loop.native_driver.core_port import CorePort, CorePortError
from runtime.codex_builder_loop.native_driver.transport_failures import classify_turn_failure
from tests.helpers import native_routing_recovery as fixture_helpers
from tests.helpers.native_delivery_recovery import complete_delivery


class RoutingClassifierTest(unittest.TestCase):
    def test_only_explicit_platform_routing_timeout_is_recoverable(self):
        self.assertEqual(classify_turn_failure(fixture_helpers.ERROR), "workspaceRoutingTimeout")
        for message in ("workspace routing discovery failed", "timeout", "MCP HTTP403",
                        "permission denied: workspace routing discovery timed out",
                        "tool output: workspace routing discovery timed out"):
            self.assertEqual(classify_turn_failure({"codexErrorInfo": "other", "message": message}), "other")
        self.assertEqual(classify_turn_failure({**fixture_helpers.ERROR, "codexErrorInfo": "usageLimitExceeded"}), "usageLimitExceeded")


class RoutingRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture_helpers.RootRoutingFixture()
        self.addCleanup(self.f.tearDown)
        self.f.setUp()
        self.turn = self.f.fail_turn()
        self.transport = fixture_helpers.FailedTurnTransport(self.turn)
        self.coordinator = NativeCoordinator(repo=self.f.repo, run_id=self.f.run_id,
            core=CorePort(), transport=self.transport, project_root=ROOT,
            builder_mode="root_session", root_session_id="root-session")
        self.coordinator.current_action = self.f.action
        self.before = read_ledger(self.f.repo, self.f.run_id)

    def retry(self):
        return self.coordinator._retry_turn_failure(self.turn, self.f.action["action_id"])

    def ledger(self):
        return read_ledger(self.f.repo, self.f.run_id)

    def assert_rejected(self):
        before = copy.deepcopy(self.ledger()["dispatch_intent"])
        with self.assertRaises(CorePortError) as raised:
            self.retry()
        self.assertEqual(raised.exception.code, "NATIVE_ROUTING_RETRY_UNVERIFIED")
        self.assertEqual(raised.exception.status, "NEEDS_USER")
        after = self.ledger()
        self.assertEqual(after["phase"], "active")
        self.assertEqual(after["dispatch_intent"], before)
        self.assertEqual(after["events"][-1]["kind"], "routing_retry_rejected")

    def test_root_checkpoint_is_preserved_and_retry_completes_delivery(self):
        self.assertTrue(self.retry())
        after = self.ledger()
        pending = after["dispatch_intent"]
        self.assertEqual(pending["attempt"], 2)
        self.assertEqual(pending["thread_id"], "tester-thread")
        self.assertEqual(pending["generation"], 1)
        self.assertEqual((datetime.fromisoformat(pending["retry_not_before"]) -
                          datetime.fromisoformat(pending["retry_scheduled_at"])).total_seconds(), 30)
        self.assertEqual(after["facets"]["execution"]["candidate_head"], self.before["facets"]["execution"]["candidate_head"])
        self.f.finish_author()
        self.f.action = self.f.begin("tester")
        self.f.result = self.f.proof_result()
        core.complete_dispatch(self.f.repo, self.f.run_id, action_id=self.f.action["action_id"], result_value=self.f.result)
        self.f.apply()
        complete_delivery(self.f)
        final = self.ledger()
        self.assertEqual(final["phase"], "finalized")
        self.assertEqual(sum(e["kind"] == "builder_checkpointed" for e in final["events"]), 1)
        self.assertEqual(sum(e["kind"] == "tester_source_integrated" for e in final["events"]), 1)

    def test_exhaustion_does_not_create_role_or_rehydration_and_explicit_resume_renews(self):
        for attempt in (1, 2):
            self.assertTrue(self.retry())
            self.turn = self.f.fail_turn()
            self.transport = fixture_helpers.FailedTurnTransport(self.turn)
            self.coordinator.transport = self.transport
        with self.assertRaises(CorePortError) as raised:
            self.retry()
        self.assertEqual(raised.exception.code, "NATIVE_DISPATCH_RETRY_EXHAUSTED")
        exhausted = self.ledger()
        self.assertEqual(exhausted["phase"], "active")
        self.assertEqual(exhausted["dispatch_intent"]["state"], "exhausted")
        self.assertEqual(exhausted["dispatch_intent"]["attempt"], 3)
        self.assertIsNone(exhausted["dispatch_rehydration_intent"])
        self.coordinator._dispatch_renewal_reason = "platform recovered; fixture authorization"
        self.assertTrue(self.retry())
        renewed = self.ledger()["dispatch_intent"]
        self.assertEqual(renewed["generation"], 2)
        self.assertEqual(renewed["attempt"], 1)
        self.assertEqual(renewed["thread_id"], "tester-thread")
        events = self.ledger()["events"]
        self.assertTrue(any(e["kind"] == "dispatch_retry_exhausted" for e in events))

    def test_missing_or_nonempty_thread_tail_preserves_dispatch(self):
        pristine = copy.deepcopy(self.transport.thread)
        for items in (None, 5, [{"type": "commandExecution"}], [{"type": "agentMessage", "text": "done"}], [{"type": "mcpToolCall"}], [{"type": "unknown"}]):
            with self.subTest(items=items):
                self.transport.thread = copy.deepcopy(pristine)
                if items is None:
                    del self.transport.thread["turns"][-1]["items"]
                else:
                    self.transport.thread["turns"][-1]["items"] = items
                self.assert_rejected()

    def test_bound_user_input_is_not_an_execution_side_effect(self):
        self.transport.thread["turns"][-1]["items"] = [{
            "type": "userMessage", "clientId": self.coordinator._dispatch_client_id(
                self.ledger()["dispatch_intent"]),
            "content": [{"type": "text", "text": "frozen prompt"}],
        }, {"type": "reasoning"}]
        self.assertTrue(self.retry())
        self.assertEqual(self.ledger()["dispatch_intent"]["attempt"], 2)

    def test_unbound_or_duplicate_user_input_preserves_dispatch(self):
        client_id = self.coordinator._dispatch_client_id(self.ledger()["dispatch_intent"])
        for items in ([{"type": "userMessage"}],
                      [{"type": "userMessage", "clientId": client_id + "-other"}],
                      [{"type": "userMessage", "clientId": client_id}] * 2):
            with self.subTest(items=items):
                self.transport.thread["turns"][-1]["items"] = items
                self.assert_rejected()

    def test_partial_thread_item_view_cannot_prove_no_activity(self):
        self.transport.thread["turns"][-1]["itemsView"] = "summary"
        self.assert_rejected()

    def test_thread_read_failure_preserves_dispatch(self):
        with patch.object(self.transport, "read_thread", side_effect=AppServerError("unavailable", code="NATIVE_APP_SERVER_TIMEOUT")):
            self.assert_rejected()

    def test_thread_or_tail_identity_drift_preserves_dispatch(self):
        self.transport.thread["id"] = "different-thread"
        self.assert_rejected()
        self.transport.thread["id"] = "tester-thread"
        self.transport.thread["turns"][-1]["id"] = "different-turn"
        self.assert_rejected()

    def test_tester_worktree_write_blocks_retry(self):
        source = self.ledger()["facets"]["execution"]["tester_source"]
        (Path(source["worktree"]) / "tests/partial.py").write_text("partial = True\n")
        self.assert_rejected()

    def test_candidate_write_blocks_retry(self):
        (self.f.candidate / "src/calc.py").write_text("changed = True\n")
        self.assert_rejected()

    def test_target_advance_blocks_retry(self):
        (self.f.repo / "advance.txt").write_text("advance\n")
        commit_all(self.f.repo, "advance target")
        self.assert_rejected()

    def test_public_core_rejects_missing_or_stale_receipt(self):
        for details in (None, {"routing_retry": {}}, {"routing_retry": {
            **self.coordinator._routing_retry_observation(self.turn, self.f.action["action_id"]),
            "attempt": 2}}):
            with self.assertRaises(core.AssuranceError) as raised:
                core.retry_dispatch(self.f.repo, self.f.run_id, action_id=self.f.action["action_id"],
                                    failure_code="workspaceRoutingTimeout", failure_details=details)
            self.assertEqual(raised.exception.code, "NATIVE_ROUTING_RETRY_UNVERIFIED")
        self.assertEqual(self.ledger()["dispatch_intent"]["attempt"], 1)

    def test_duplicate_retry_receipt_cannot_consume_another_attempt(self):
        self.retry()
        self.assert_rejected()

    def test_wait_restart_uses_remaining_deadline(self):
        self.retry()
        pending = self.ledger()["dispatch_intent"]
        clock = datetime.fromisoformat(pending["retry_not_before"]) - timedelta(seconds=7)
        sleeps = []
        def sleep(seconds):
            nonlocal clock
            sleeps.append(seconds)
            clock += timedelta(seconds=seconds)
        restarted = NativeCoordinator(repo=self.f.repo, run_id=self.f.run_id, core=CorePort(),
            transport=self.transport, project_root=ROOT, now_fn=lambda: clock, sleep_fn=sleep)
        restarted._wait_for_retry(pending)
        self.assertEqual(sum(sleeps), 7)
        self.assertEqual(self.ledger()["dispatch_intent"]["attempt"], 2)

    def test_reactivation_rechecks_files_changed_during_backoff(self):
        self.retry()
        pending = self.ledger()["dispatch_intent"]
        (self.f.candidate / "src/calc.py").write_text("drift = True\n")
        with self.assertRaises(core.AssuranceError) as raised:
            core.record_dispatch_activation(self.f.repo, self.f.run_id,
                action_id=pending["action_id"], activation_state="activated",
                failure_details=pending["failure_details"])
        self.assertEqual(raised.exception.code, "NATIVE_ROUTING_RETRY_UNVERIFIED")
        self.assertEqual(self.ledger()["dispatch_intent"]["attempt"], 2)

    def test_reactivation_requires_the_same_thread_observation(self):
        self.retry()
        pending = self.ledger()["dispatch_intent"]
        details = copy.deepcopy(pending["failure_details"])
        details["routing_retry"]["thread_digest"] = "0" * 64
        with self.assertRaises(core.AssuranceError):
            core.record_dispatch_activation(self.f.repo, self.f.run_id,
                action_id=pending["action_id"], activation_state="activated", failure_details=details)
        core.record_dispatch_activation(self.f.repo, self.f.run_id,
            action_id=pending["action_id"], activation_state="activated",
            failure_details=pending["failure_details"])
        self.assertEqual(self.ledger()["dispatch_intent"]["activation_state"], "activated")

    def test_restart_reads_failed_turn_through_the_same_classifier(self):
        context = self.coordinator._context()
        pending = context["dispatch_intent"]
        result = self.coordinator._recover_dispatch(pending, "tester", context, self.f.action)
        self.assertIsNone(result)
        self.assertEqual(self.ledger()["dispatch_intent"]["attempt"], 2)

    def test_sigkill_after_retry_persistence_does_not_repeat_attempt(self):
        details = {"routing_retry": self.coordinator._routing_retry_observation(self.turn, self.f.action["action_id"])}
        program = """
import json,os,signal,sys
from runtime.codex_builder_loop.assurance_v4 import core
original=core._store_save_ledger
def save(repo,ledger):
 original(repo,ledger)
 if ledger['dispatch_intent']['attempt']==2: os.kill(os.getpid(),signal.SIGKILL)
core._store_save_ledger=save
core.retry_dispatch(sys.argv[1],sys.argv[2],action_id=sys.argv[3],failure_code='workspaceRoutingTimeout',failure_details=json.loads(sys.argv[4]))
"""
        child = subprocess.run([sys.executable, "-c", program, str(self.f.repo), self.f.run_id,
            self.f.action["action_id"], json.dumps(details)], cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
        self.assertEqual(child.returncode, -9, child.stderr)
        pending = self.ledger()["dispatch_intent"]
        self.assertEqual(pending["attempt"], 2)
        with self.assertRaises(core.AssuranceError):
            core.retry_dispatch(self.f.repo,self.f.run_id,action_id=pending["action_id"],
                failure_code="workspaceRoutingTimeout", failure_details=details)
        self.assertEqual(self.ledger()["dispatch_intent"]["attempt"], 2)

    def test_restart_after_activation_does_not_start_turn_with_drift(self):
        self.retry()
        pending = self.ledger()["dispatch_intent"]
        core.record_dispatch_activation(self.f.repo, self.f.run_id,
            action_id=pending["action_id"], activation_state="activated",
            failure_details=pending["failure_details"])
        (self.f.candidate / "src/calc.py").write_text("late_drift = True\n")
        self.coordinator._now_fn = lambda: datetime.now(timezone.utc) + timedelta(minutes=5)
        context = self.coordinator._context()
        with self.assertRaises(CorePortError) as raised:
            self.coordinator._recover_dispatch(context["dispatch_intent"], "tester", context, self.f.action)
        self.assertEqual(raised.exception.code, "NATIVE_ROUTING_RETRY_UNVERIFIED")
        self.assertEqual(self.ledger()["dispatch_intent"]["attempt"], 2)

    def recover_to_next_failed_turn(self):
        pending = self.ledger()["dispatch_intent"]
        self.coordinator._now_fn = lambda: datetime.now(timezone.utc) + timedelta(minutes=5)
        calls = []
        self.transport.resume_thread = lambda **kwargs: None
        def run_turn(**kwargs):
            calls.append(kwargs["action_id"])
            turn_id = f"real-submit-g{pending['generation']}-a{pending['attempt']}"
            kwargs["on_started"](turn_id)
            self.transport.thread["turns"].append({"id": turn_id, "status": "failed", "items": [],
                                                   "error": dict(fixture_helpers.ERROR)})
            from runtime.codex_builder_loop.native_driver.app_server import TurnResult
            return TurnResult(turn_id=turn_id, status="failed", text="", error=dict(fixture_helpers.ERROR))
        self.transport.run_turn = run_turn
        context = self.coordinator._context()
        self.coordinator._recover_dispatch(context["dispatch_intent"], "tester", context, self.f.action)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.ledger()["dispatch_intent"]["attempt"], pending["attempt"] + 1)
        return calls[0]

    def test_recovered_prepared_attempt_submits_and_binds_next_turn(self):
        self.retry()
        self.assertTrue(self.recover_to_next_failed_turn().endswith(":2"))

    def test_explicit_generation_two_submits_on_original_thread(self):
        for _ in (1, 2):
            self.retry()
            self.turn = self.f.fail_turn()
            self.transport = fixture_helpers.FailedTurnTransport(self.turn)
            self.coordinator.transport = self.transport
        with self.assertRaises(CorePortError):
            self.retry()
        self.coordinator._dispatch_renewal_reason = "fixture explicit resume"
        self.retry()
        self.assertTrue(self.recover_to_next_failed_turn().endswith(":g2:1"))
        self.assertEqual(self.ledger()["dispatch_intent"]["thread_id"], "tester-thread")
