from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from harness import init_repo, cleanup_repo
from test_native_driver_v1 import root_native_contract
from codex_builder_loop.assurance_v4 import core, operations, decisions
from codex_builder_loop.assurance_v4.models import digest
from codex_builder_loop.assurance_v4.store import read_ledger, append_event
from codex_builder_loop.native_driver.cli import _root_runtime
from codex_builder_loop.native_driver.core_port import CorePort
from codex_builder_loop.native_agent import _root_turn
from codex_builder_loop.native_driver.role_transport import RoleTransport
from codex_builder_loop.assurance_v4.role_input import role_context
import json


class NativeOperationTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.run = "native-operations"
        runtime = _root_runtime("owner")
        runtime["orchestration"] = "native_agent"
        contract = root_native_contract(self.repo)
        for unit in contract["execution"]["work_units"]:
            if unit["role"] == "tester":
                unit["depends_on"] = []
        core.start(self.repo, self.run, "owner", contract, driver_runtime=runtime)

    def tearDown(self):
        cleanup_repo(self.repo)

    def test_independent_authoring_is_not_single_driver_choice(self):
        # Poison the old scheduler: native operation admission must not call it.
        with patch("codex_builder_loop.assurance_v4.driver.next_action", side_effect=AssertionError("legacy scheduler")):
            builder = operations.operation(self.repo, self.run, "builder_implement")
            tester = operations.operation(self.repo, self.run, "tester_author")
        self.assertNotEqual(builder["action_id"], tester["action_id"])
        self.assertEqual(builder["status"], "CONTINUE")
        self.assertEqual(tester["status"], "CONTINUE")

    def test_repeated_failure_blocks_execution_but_preserves_checkpoint(self):
        ledger = read_ledger(self.repo, self.run)
        for _ in range(3):
            append_event(ledger, "proof_failure_recorded", {"failure_signature": "same-defect"})
        with self.assertRaises(core.AssuranceError) as raised:
            operations.admit(self.repo, ledger, "builder_implement")
        self.assertEqual(raised.exception.code, "OPERATION_EXECUTION_LIMIT")
        self.assertEqual(operations.admit(self.repo, ledger, "checkpoint_builder")["action"], "checkpoint_builder")
        self.assertEqual(raised.exception.details["failures"][0]["count"], 3)

    def test_external_resolution_grants_one_real_verification_attempt(self):
        from codex_builder_loop.assurance_v4.execution_limits import repeated_failures
        ledger = read_ledger(self.repo, self.run)
        ledger["facets"]["execution"]["candidate_head"] = "a" * 40
        ledger["evidence"]["machine"] = None
        for _ in range(3):
            append_event(ledger, "machine_verified", {"failure_signature": "environment", "status": "fail"})
        ledger["problems"].append({"key": "env", "owner": "external_platform", "status": "resolved", "resolution": "fixed"})
        append_event(ledger, "external_problem_resolved", {"key": "env", "reason": "fixed",
            "candidate_head": "a" * 40, "machine_state": "failed", "machine_evidence_digest": None})
        self.assertEqual(repeated_failures(ledger), [])
        append_event(ledger, "machine_verified", {"failure_signature": "environment", "status": "fail"})
        self.assertEqual(repeated_failures(ledger)[0]["count"], 4)

    def test_persisted_host_recovery_cannot_be_bypassed_by_role_dispatch(self):
        ledger = read_ledger(self.repo, self.run)
        ledger["dispatch_rehydration_intent"] = {"action_id": "b" * 64, "state": "prepared",
            "spawn_state": "spawned", "work_unit_id": "independent-tests"}
        ledger["dispatch_intent"] = {"action_id": "b" * 64, "action": "tester_author"}
        with self.assertRaises(core.AssuranceError) as raised:
            operations.admit(self.repo, ledger, "tester_author")
        self.assertEqual(raised.exception.code, "OPERATION_TRANSACTION_PENDING")
        recovery = operations.admit(self.repo, ledger, "rehydrate_dispatch")
        self.assertEqual(recovery["action_id"], "b" * 64)
        selected = operations.find(self.repo, self.run, action_id="b" * 64,
            accepted={"rehydrate_dispatch"}, ledger=ledger)
        self.assertEqual(selected["action"], "rehydrate_dispatch")
        ledger["dispatch_rehydration_intent"]["spawn_state"] = "unknown"
        with self.assertRaises(core.AssuranceError) as raised:
            operations.admit(self.repo, ledger, "rehydrate_dispatch")
        self.assertEqual(raised.exception.code, "OPERATION_RECOVERY_UNRESOLVED")

    def test_native_entry_does_not_import_frozen_coordinator(self):
        import ast
        from harness import ROOT
        for relative in ("runtime/codex_builder_loop/native_agent.py",
                         "runtime/codex_builder_loop/native_driver/host_runtime.py",
                         "runtime/codex_builder_loop/native_driver/role_transport.py",
                         "runtime/codex_builder_loop/assurance_v4/operations.py",
                         "runtime/codex_builder_loop/assurance_v4/role_payload.py"):
            tree = ast.parse((ROOT / relative).read_text())
            modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            self.assertFalse(any(m.endswith(("coordinator", "native_driver.cli", "assurance_v4.driver"))
                                 or m == "driver" for m in modules), relative)

    def test_ready_hold_allows_work_until_ready_and_now_hold_stops_it(self):
        core.hold_run(self.repo, self.run, session_id="owner", reason="Inspect before finalize", when="ready")
        self.assertEqual(operations.operation(self.repo, self.run, "builder_implement")["action"], "builder_implement")
        hold = read_ledger(self.repo, self.run)["hold"]
        core.hold_run(self.repo, self.run, session_id="owner", reason="Continue", release_id=hold["id"])
        core.hold_run(self.repo, self.run, session_id="owner", reason="Inspect now", when="now")
        with self.assertRaises(core.AssuranceError) as raised:
            operations.operation(self.repo, self.run, "builder_implement")
        self.assertEqual(raised.exception.code, "OPERATION_HELD")

    def test_completed_root_result_is_applied_without_new_builder_handoff(self):
        action = operations.operation(self.repo, self.run, "builder_implement")
        _root_turn(CorePort(), self.repo, self.run, "owner", action)
        core.complete_dispatch(self.repo, self.run, action_id=action["action_id"], owner_session_id="owner",
            result_value={"result": "implemented", "evidence_report": None, "proof_spec": None, "problem_report": None})
        pending = operations.operation(self.repo, self.run, "builder_implement")
        result = _root_turn(CorePort(), self.repo, self.run, "owner", pending)
        self.assertNotEqual(result["status"], "BUILDER_HANDOFF")
        self.assertIsNone(read_ledger(self.repo, self.run)["dispatch_intent"])

    def test_independent_tester_feedback_returns_to_original_reviewer_first(self):
        action = operations.operation(self.repo, self.run, "builder_implement")
        _root_turn(CorePort(), self.repo, self.run, "owner", action)
        core.apply_root_builder_result(self.repo, self.run, action_id=action["action_id"], owner_session_id="owner",
            result_value={"result": "implemented", "evidence_report": None, "proof_spec": None, "problem_report": None})
        core.prepare_tester(self.repo, self.run, "tester", "tester-thread")
        core.prepare_reviewer(self.repo, self.run, "reviewer", "reviewer-thread")
        core.record_problems(self.repo, self.run, {"schema_version": 1, "problems": [
            {"key": "test-input", "owner": "tester", "summary": "Clarify public coverage",
             "details": "Original Reviewer must bind its finding to public acceptance inputs."}]},
            role="reviewer", agent_id="reviewer", thread_id="reviewer-thread")
        with self.assertRaises(core.AssuranceError) as raised:
            operations.operation(self.repo, self.run, "tester_fix")
        self.assertEqual(raised.exception.code, "TESTER_FEEDBACK_REVIEW_REQUIRED")
        core.scan_doc_references(self.repo, self.run)
        review = operations.operation(self.repo, self.run, "reviewer_preflight")
        self.assertEqual(review["agent"]["thread_id"], "reviewer-thread")
        self.assertEqual(review["reason"], "tester_recheck_references_required")
        self.assertEqual(review["problem"]["key"], "test-input")

    def test_public_legacy_coordinator_start_is_retired_before_side_effects(self):
        import contextlib, io, os
        from codex_builder_loop.native_driver import cli
        with patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(io.StringIO()) as output:
            os.environ.pop("CODEX_BUILDER_LOOP_ENABLE_LEGACY_START", None)
            rc = cli.main(["start", "--repo", "/nonexistent/native-retirement", "--run", "new",
                           "--contract", "/nonexistent/contract.json"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "LEGACY_COORDINATOR_START_DISABLED")

    def test_transport_bookkeeping_does_not_stale_author_receipt(self):
        ledger = read_ledger(self.repo, self.run)
        before = operations.admit(self.repo, ledger, "builder_implement")
        after = copy.deepcopy(ledger)
        append_event(after, "transport_observed", {"sequence": 27})
        after["updated_at"] = "2099-01-01T00:00:00+00:00"
        receipt = operations.admit(self.repo, after, "builder_implement")
        self.assertEqual(before["action_id"], receipt["action_id"])

    def test_preparing_role_does_not_stale_author_receipt(self):
        before = operations.operation(self.repo, self.run, "builder_implement")
        core.prepare_builder(self.repo, self.run, "codex-root-session:owner", None,
                             owner_mode="root_session", session_id="owner")
        after = operations.operation(self.repo, self.run, "builder_implement")
        self.assertEqual(before["action_id"], after["action_id"])

    def test_missing_evidence_cannot_be_bypassed_by_selecting_finalize(self):
        with self.assertRaises(core.AssuranceError) as raised:
            operations.operation(self.repo, self.run, "finalize")
        self.assertIn(raised.exception.code, {"OPERATION_EVIDENCE_REQUIRED", "OPERATION_WORK_UNIT_DEPENDENCY"})

    def test_missing_evidence_cannot_be_bypassed_by_selecting_review(self):
        with self.assertRaises(core.AssuranceError) as raised:
            operations.operation(self.repo, self.run, "reviewer_final")
        self.assertIn(raised.exception.code, {"OPERATION_EVIDENCE_REQUIRED", "OPERATION_WORK_UNIT_DEPENDENCY"})

    def test_candidate_change_invalidates_receipt(self):
        ledger = read_ledger(self.repo, self.run)
        before = operations.admit(self.repo, ledger, "builder_implement")
        ledger["facets"]["execution"]["candidate_head"] = "a" * 40
        after = operations.admit(self.repo, ledger, "builder_implement")
        self.assertNotEqual(before["action_id"], after["action_id"])

    def test_bounded_prompt_and_role_context_share_complete_input(self):
        action = operations.operation(self.repo, self.run, "tester_author")
        context = core.driver_context(self.repo, self.run)
        adapter = RoleTransport(repo=self.repo, run_id=self.run, core=CorePort(), transport=None)
        prompt = adapter._prompt(action, "tester", context)
        bootstrap = json.loads(prompt.split("\n", 1)[1])
        complete = role_context(self.repo, self.run, action["action_id"])
        self.assertEqual(bootstrap["input_digest"], digest(complete))
        self.assertIn("role_phase_instructions", complete)
        self.assertIn("evidence_report_schema", complete)
        self.assertNotIn("contract", bootstrap)
        self.assertLess(len(prompt), 6000)
        index = role_context(self.repo, self.run, action["action_id"], "/contract/mission/acceptance_cases", index=True)
        self.assertEqual(index["value"], {"type": "list", "length": 1})
        self.assertEqual(index["input_digest"], digest(complete))

    def test_legacy_run_is_not_silently_migrated(self):
        ledger = read_ledger(self.repo, self.run)
        del ledger["driver_runtime"]["orchestration"]
        with self.assertRaises(core.AssuranceError) as raised:
            operations.admit(self.repo, ledger, "builder_implement")
        self.assertEqual(raised.exception.code, "OPERATION_RUNTIME_MISMATCH")

    def test_root_result_application_uses_native_receipt_and_replays(self):
        action = operations.operation(self.repo, self.run, "builder_implement")
        handoff = _root_turn(CorePort(), self.repo, self.run, "owner", action)
        self.assertEqual(handoff["status"], "BUILDER_HANDOFF")
        result = {"result": "implemented", "evidence_report": None,
                  "proof_spec": None, "problem_report": None}
        with patch("codex_builder_loop.assurance_v4.driver.next_action", side_effect=AssertionError("legacy scheduler")):
            applied = core.apply_root_builder_result(self.repo, self.run, action_id=action["action_id"],
                owner_session_id="owner", result_value=result)
            replay = core.apply_root_builder_result(self.repo, self.run, action_id=action["action_id"],
                owner_session_id="owner", result_value=result)
        self.assertIsNone(applied["dispatch_intent"])
        self.assertIsNone(replay["dispatch_intent"])
        self.assertIn("operations", applied)

    def test_product_answer_revises_same_run_without_scheduler(self):
        action = operations.operation(self.repo, self.run, "builder_implement")
        _root_turn(CorePort(), self.repo, self.run, "owner", action)
        request = {"kind": "product_choice", "facet": "mission", "changes": [], "question": "Choose objective"}
        problem = {"key": "choice", "owner": "plan", "summary": "Objective choice",
                   "details": "Explicit product answer required", "decision_request": request}
        core.apply_root_builder_result(self.repo, self.run, action_id=action["action_id"], owner_session_id="owner",
            result_value={"result": "blocked", "evidence_report": None, "proof_spec": None,
                          "problem_report": {"schema_version": 1, "problems": [problem]}})
        before = read_ledger(self.repo, self.run)
        decision = operations.problem_view(self.repo, before, "choice")
        selected = {"kind": "facet_change", "facet": "mission", "question": "User selected objective",
                    "changes": [{"operation": "replace", "pointer": "/objective", "value": "Selected objective"}]}
        with patch("codex_builder_loop.assurance_v4.driver.next_action", side_effect=AssertionError("legacy scheduler")):
            core.correct_decision_request(self.repo, self.run, session_id="owner", problem_key="choice",
                action_id=decision["action_id"], request_digest=digest(request), facet_digest=before["digests"]["mission"],
                request_value=selected, reason="User selected objective", driver_runtime_kind="native", record_product_choice=True)
            current = read_ledger(self.repo, self.run)
            self.assertEqual(before["problems"], current["problems"])
            decision = operations.problem_view(self.repo, current, "choice")
            replacement = copy.deepcopy(current["facets"])
            replacement["mission"].update(revision=2, objective="Selected objective", supersedes={
                "run_id": self.run, "revision": 1, "mission_digest": current["digests"]["mission"],
                "candidate_head": current["facets"]["execution"]["candidate_head"]})
            replacement["execution"]["revision_transition"] = {"category": "mission_change",
                "predecessor_pressure_digest": core.status(self.repo, self.run)["lineage"]["pressure_digest"],
                "architecture_review": None}
            preview = decisions.validate_decision(self.repo, self.run, session_id="owner", problem_key="choice",
                action_id=decision["action_id"], facet="mission", facet_digest=current["digests"]["mission"],
                replacement_contract=replacement)
            self.assertEqual(preview["apply"]["command"], "revise-mission")
            core.revise_mission(self.repo, self.run, replacement["mission"], replacement["execution"]["revision_transition"],
                resolve_plan_problem_key="choice", decision_action_id=decision["action_id"],
                expected_facet_digest=current["digests"]["mission"], owner_session_id="owner")
        final = read_ledger(self.repo, self.run)
        self.assertEqual(final["run_id"], before["run_id"])
        self.assertEqual(final["problems"][0]["status"], "resolved")
        self.assertEqual(final["facets"]["execution"]["agents"], before["facets"]["execution"]["agents"])


if __name__ == "__main__":
    unittest.main()
