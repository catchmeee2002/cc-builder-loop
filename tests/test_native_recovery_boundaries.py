"""Recovery must establish eligibility before contacting a role transport."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from runtime.codex_builder_loop.native_driver.cli import _doctor_payload
from runtime.codex_builder_loop.native_driver.app_server import AppServerError
from runtime.codex_builder_loop.native_driver.core_port import CorePortError
from runtime.codex_builder_loop.assurance_v4.driver_contract import (
    dispatch_exhaustion_view, dispatch_exhaustion_blocked,
)


class RecoveryEligibilityTest(unittest.TestCase):
    def test_ineligible_dispatch_never_reads_thread(self):
        base = dict(action_id="a", role="reviewer", action="reviewer_final",
                    state="exhausted", attempt=3, thread_id="thread",
                    failure_code="responseStreamDisconnected")
        cases = [dict(role="tester", action="tester_author"),
                 dict(role="builder", action="builder_fix"),
                 dict(action="tester_author"), dict(state="in_flight"),
                 dict(attempt=2), dict(failure_code="responseStreamTimeout"),
                 dict(action_id="stale"),
                 dict(compaction_recovery={"state": "completed"})]
        for override in cases:
            with self.subTest(override=override):
                coordinator = NativeCoordinator.__new__(NativeCoordinator)
                coordinator._thread_compaction_available = True
                coordinator._context = lambda: {"dispatch_intent": {**base, **override}}
                reader = Mock(side_effect=AssertionError("ineligible dispatch contacted transport"))
                coordinator.transport = SimpleNamespace(read_thread=reader)
                self.assertIsNone(coordinator._start_reviewer_thread_compaction("a"))
                reader.assert_not_called()

    def coordinator(self, *, state="in_flight"):
        coordinator = NativeCoordinator.__new__(NativeCoordinator)
        coordinator.repo, coordinator.run_id = Path("/fixture"), "run"
        coordinator.current_action = dict(action="reviewer_final", action_id="a")
        pending = {**coordinator.current_action, "role": "reviewer", "state": state,
                   "thread_id": "thread", "attempt": 3, "generation": 1,
                   "failure_code": "responseStreamDisconnected"}
        coordinator._context = lambda: {"phase": "active", "dispatch_intent": pending,
            "facets": {"execution": {"agents": {"reviewer": {"thread_id": "thread"}}}}}
        coordinator._thread_compaction_available = True
        coordinator._dispatch_renewal_reason = None
        coordinator.transport = Mock()
        coordinator._assert_builder_retry_safe = Mock()
        coordinator.core = SimpleNamespace(call=Mock(side_effect=CorePortError("exhausted", payload={
            "status": "NEEDS_USER", "code": "NATIVE_DISPATCH_RETRY_EXHAUSTED"}, returncode=1)))
        return coordinator

    def test_closed_connection_exhaustion_defers_even_eligible_reviewer_recovery(self):
        coordinator = self.coordinator()
        result = coordinator.retry_transport_failure(AppServerError(
            "closed output", code="NATIVE_APP_SERVER_DISCONNECTED"))
        self.assertEqual(result["code"], "NATIVE_DISPATCH_RETRY_EXHAUSTED")
        self.assertEqual(result["transport_retry"]["failure_code"], "responseStreamDisconnected")
        self.assertEqual([call[0] for call in coordinator.transport.mock_calls],
                         ["diagnostic_receipt", "wire_snapshot"])
        self.assertEqual(coordinator.core.call.call_args.args[0], "retry-dispatch")

    def test_failed_inspection_of_exhausted_thread_preserves_original_failure(self):
        coordinator = self.coordinator(state="exhausted")
        result = coordinator.retry_transport_failure(AppServerError(
            "read timed out", code="NATIVE_APP_SERVER_TIMEOUT", details={"timeout_seconds": 120}))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "NEEDS_USER")
        self.assertEqual(result["dispatch_exhaustion"]["failure_code"], "responseStreamDisconnected")
        self.assertEqual(result["recovery_error"]["code"], "NATIVE_APP_SERVER_TIMEOUT")
        coordinator.core.call.assert_not_called()

    def test_bootstrap_renewal_after_cleanup_only_persists_intent(self):
        coordinator = self.coordinator(state="prepared")
        coordinator.current_action = {"action": "tester_author", "action_id": "a"}
        agent = {"thread_id": "lost-thread", "agent_id": "lost-agent"}
        pending = {**coordinator.current_action, "role": "tester", "state": "prepared",
                   "activation_state": "pending", "turn_id": None, "thread_id": "lost-thread"}
        context = {"dispatch_intent": pending, "tester_replacement_intent": {
            "action_id": "replacement", "problem_key": "invalid-producer",
            "stage": "awaiting_first_turn", "new_agent": agent},
            "facets": {"execution": {"agents": {"tester": agent}, "tester_source": {
                "agent": agent, "head": "base", "base_head": "base", "files": [],
                "worktree": "/fixture/source"}}}}
        coordinator._context = lambda: context
        calls = []
        def persist(command, *args, **kwargs):
            calls.append(command)
            self.assertEqual(command, "begin-tester-replacement")
            self.assertIn("--renew-bootstrap", args)
            context["tester_replacement_intent"].update(stage="prepared", new_agent=None)
            context["dispatch_intent"] = None
            return context
        coordinator.core = SimpleNamespace(call=persist)
        coordinator._role_config = lambda role: ("fixture instructions", "danger-full-access")
        coordinator.transport.start_thread.side_effect = AssertionError("transport is closed")
        result = coordinator.retry_transport_failure(AppServerError(
            "no rollout found for thread id lost-thread", code="NATIVE_APP_SERVER_REQUEST_FAILED",
            details={"method": "thread/resume", "error": {"message": "no rollout found for thread id lost-thread"}}))
        self.assertEqual(calls, ["begin-tester-replacement"])
        self.assertIsNone(result["tester_replacement_intent"]["new_agent"])
        self.assertEqual(coordinator.transport.mock_calls, [])


class ExhaustionProjectionTest(unittest.TestCase):
    def test_bound_recovery_and_terminal_phase_are_not_exhaustion_blocks(self):
        context = {"phase": "active", "dispatch_intent": dict(
            action_id="a", action="reviewer_final", role="reviewer", thread_id="t",
            state="exhausted", attempt=3, generation=1, failure_code="missingAgentResult")}
        self.assertTrue(dispatch_exhaustion_blocked(context))
        for key in ("dispatch_rehydration_intent", "reviewer_replacement_intent"):
            self.assertFalse(dispatch_exhaustion_blocked({**context, key: {"action_id": "a"}}))
            self.assertTrue(dispatch_exhaustion_blocked({**context, key: {"action_id": "other"}}))
        self.assertFalse(dispatch_exhaustion_blocked({**context, "dispatch_intent": {
            **context["dispatch_intent"], "compaction_recovery": {"state": "prepared"}}}))
        for phase in ("failed", "finalized", "superseded", "abandoned", "finalizing"):
            terminal = {**context, "phase": phase}
            self.assertFalse(dispatch_exhaustion_blocked(terminal))
            self.assertIsNotNone(dispatch_exhaustion_view(terminal))
        self.assertIsNone(dispatch_exhaustion_view({"phase": "active"}))
        doctor = _doctor_payload(context)
        self.assertEqual((doctor["status"], doctor["diagnostic_state"]),
                         ("NEEDS_USER", "dispatch-exhausted"))
        stronger = _doctor_payload({**context, "dispatch_recovery": {"state": "needs_user"}})
        self.assertEqual(stronger["diagnostic_state"], "result-application-blocked")
        self.assertIsNotNone(stronger["dispatch_exhaustion"])


if __name__ == "__main__":
    unittest.main()
