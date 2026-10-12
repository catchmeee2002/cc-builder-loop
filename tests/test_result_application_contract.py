from __future__ import annotations

import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from harness import cleanup_repo, commit_all, git, init_repo
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from tests.test_assurance_v4_contract import contract_for


class Interrupted(BaseException):
    pass


class ResultApplicationContractTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo({"src/calc.py": "def add(a, b):\n    return a + b - 1\n"})
        self.run_id = "result-application"
        contract = contract_for(self.repo)
        contract["execution"]["driver_enforced"] = True
        contract["execution"]["agents"] = {}
        contract["assurance"]["required"].insert(2, "proof")
        started = core.start(self.repo, self.run_id, "result-session", contract,
            driver_runtime={
                "kind": "native", "protocol_version": 1, "transport": "codex_app_server",
                "runtime_version": "fixture", "protocol_schema_digest": "a" * 64,
            })
        self.candidate = Path(started["candidate_worktree"])
        core.prepare_builder(self.repo, self.run_id, "builder", thread_id="builder-thread")
        self.action = self.begin("builder")
        (self.candidate / "src/calc.py").write_text(
            "def add(a, b):\n    return sum((a, b))\n", encoding="utf-8")
        commit_all(self.candidate, "implement calculator")
        self.result = {
            "result": "implemented", "evidence_report": None,
            "problem_report": None, "proof_spec": None,
        }
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action["action_id"],
                               result_value=self.result)

    def tearDown(self):
        cleanup_repo(self.repo)

    def begin(self, role):
        action = driver.next_action(self.repo, self.run_id)
        self.assertTrue(action["action"].startswith(role + "_"), action)
        core.begin_dispatch(
            self.repo, self.run_id, action_id=action["action_id"], action=action["action"],
            role=role, thread_id=role + "-thread", prompt_digest="b" * 64,
            output_schema_digest="c" * 64, driver_runtime_kind="native",
            work_unit_id=action.get("work_unit_id"),
            context_projection_digest=action.get("context_projection_digest"),
        )
        return action

    def apply(self, result=None):
        return core.apply_dispatch_result(
            self.repo, self.run_id, action_id=self.action["action_id"],
            driver_runtime_kind="native", result_value=result)

    def interrupt_after(self, event, operation):
        original = core._store_save_ledger
        def save(repo, ledger):
            original(repo, ledger)
            if any(item["kind"] == event for item in ledger["events"]):
                raise Interrupted()
        with patch.object(core, "_store_save_ledger", side_effect=save):
            with self.assertRaises(Interrupted):
                operation()

    def test_checkpoint_interruption_resumes_without_repeating_or_rebinding_dispatch(self):
        original_projection = read_ledger(self.repo, self.run_id)["dispatch_intent"]["context_projection_digest"]
        self.interrupt_after("builder_checkpointed", self.apply)
        resumed = driver.next_action(self.repo, self.run_id)
        self.assertEqual(resumed["action_id"], self.action["action_id"])
        ledger = read_ledger(self.repo, self.run_id)
        self.assertEqual(ledger["dispatch_intent"]["context_projection_digest"], original_projection)
        self.apply()
        self.apply(self.result)
        ledger = read_ledger(self.repo, self.run_id)
        self.assertIsNone(ledger["dispatch_intent"])
        self.assertEqual(sum(e["kind"] == "builder_checkpointed" for e in ledger["events"]), 1)
        self.assertEqual(sum(e["kind"] == "dispatch_consumed" for e in ledger["events"]), 1)

    def test_consumed_result_cannot_be_replaced(self):
        self.apply()
        with self.assertRaises(core.AssuranceError) as raised:
            self.apply({**self.result, "result": "blocked"})
        self.assertEqual(raised.exception.code, "DISPATCH_APPLICATION_REPLAY_MISMATCH")

    def test_external_worktree_change_is_not_accepted_as_application_progress(self):
        self.interrupt_after("builder_checkpointed", self.apply)
        (self.candidate / "src/calc.py").write_text("unexpected = True\n", encoding="utf-8")
        commit_all(self.candidate, "external change")
        with self.assertRaises(core.AssuranceError) as raised:
            self.apply()
        self.assertEqual(raised.exception.code, "DISPATCH_APPLICATION_WORKTREE_DRIFT")
        self.assertIsNotNone(read_ledger(self.repo, self.run_id)["dispatch_intent"])

    def prepare_tester_result(self):
        self.apply()
        core.scan_doc_references(self.repo, self.run_id)
        core.prepare_tester(self.repo, self.run_id, "tester", "tester-thread")
        self.action = self.begin("tester")
        ledger = read_ledger(self.repo, self.run_id)
        source = ledger["facets"]["execution"]["tester_source"]
        worktree = Path(source["worktree"])
        (worktree / "tests/test_recovery.py").write_text(
            "import unittest\nfrom src.calc import add\n"
            "class RecoveryTest(unittest.TestCase):\n"
            "    def test_negative(self):\n        self.assertEqual(add(-1, -2), -3)\n",
            encoding="utf-8")
        (worktree / "tests/__init__.py").write_text("", encoding="utf-8")
        source_head = commit_all(worktree, "author negative calculator test")
        self.result = {
            "result": "tests_ready", "problem_report": None, "proof_spec": None,
            "evidence_report": {
                "schema_version": 1, "kind": "tester", "status": "pass",
                "candidate_head": ledger["facets"]["execution"]["candidate_head"],
                "producer": {"role": "tester", "agent_id": "tester", "thread_id": "tester-thread"},
                "details": {"result": "tests_ready", "source_head": source_head, "files": [
                    {"path": "tests/test_recovery.py", "blob": git(worktree, "rev-parse", source_head + ":tests/test_recovery.py")}
                ]},
            },
        }
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action["action_id"],
                               result_value=self.result)

    def test_tester_integration_interruption_keeps_candidate_and_records_evidence_once(self):
        self.prepare_tester_result()
        self.interrupt_after("tester_source_integrated", self.apply)
        integrated = read_ledger(self.repo, self.run_id)["facets"]["execution"]["candidate_head"]
        self.assertEqual(driver.next_action(self.repo, self.run_id)["action_id"], self.action["action_id"])
        self.apply()
        ledger = read_ledger(self.repo, self.run_id)
        self.assertEqual(ledger["facets"]["execution"]["candidate_head"], integrated)
        self.assertEqual(ledger["evidence"]["tester"]["candidate_head"], integrated)
        self.assertEqual(sum(e["kind"] == "tester_source_integrated" for e in ledger["events"]), 1)

    def test_git_integration_recovers_after_worktree_switch_before_ledger_commit(self):
        self.prepare_tester_result()
        original = core.git
        def git_with_interruption(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            if args[:2] == ("reset", "--hard"):
                raise Interrupted()
            return result
        with patch.object(core, "git", side_effect=git_with_interruption):
            with self.assertRaises(Interrupted):
                self.apply()
        ledger = read_ledger(self.repo, self.run_id)
        intent = ledger["dispatch_intent"]["application"]["integration"]
        self.assertEqual(ledger["facets"]["execution"]["candidate_head"], intent["before"])
        self.assertEqual(git(self.candidate, "rev-parse", "HEAD"), intent["after"])
        self.assertEqual(driver.next_action(self.repo, self.run_id)["action_id"], self.action["action_id"])
        self.apply()
        self.assertEqual(
            read_ledger(self.repo, self.run_id)["facets"]["execution"]["candidate_head"],
            intent["after"],
        )

    def proof_result(self, *, invalid=False):
        test_id = "tests.test_recovery.RecoveryTest.test_negative"
        return {
            "result": "tests_ready", "evidence_report": None, "problem_report": None,
            "proof_spec": {
                "schema_version": 1,
                "groups": [{
                    "behavior_ids": ["add-values"], "method": "baseline-red",
                    "argv": ["echo", test_id] if invalid else ["python3", "-m", "unittest", test_id],
                    "test_ids": [test_id], "timeout_seconds": 30,
                    "claimed_failure_kind": "assertion-failure",
                }],
            },
        }

    def prepare_proof(self):
        self.prepare_tester_result()
        self.apply()
        core.scan_doc_references(self.repo, self.run_id)
        self.action = self.begin("tester")
        self.assertEqual(self.action["action"], "tester_proof")

    def test_persisted_proof_failure_resumes_to_spec_correction_without_reintegration(self):
        self.prepare_proof()
        self.result = self.proof_result(invalid=True)
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action["action_id"],
                               result_value=self.result)
        self.interrupt_after("proof_failure_recorded", self.apply)
        with patch.object(core, "prove_tests", side_effect=AssertionError("proof rerun")):
            self.apply()
        before = read_ledger(self.repo, self.run_id)
        self.action = self.begin("tester")
        self.assertEqual(self.action["action"], "tester_proof_diagnose")
        self.result = self.proof_result()
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action["action_id"],
                               result_value=self.result)
        self.apply()
        after = read_ledger(self.repo, self.run_id)
        self.assertEqual(after["evidence"]["proof"]["status"], "pass")
        self.assertIsNone(after["proof_failure"])
        for key in ("candidate_head", "tester_source"):
            self.assertEqual(after["facets"]["execution"][key], before["facets"]["execution"][key])
        self.assertEqual(sum(e["kind"] == "tester_source_integrated" for e in after["events"]), 1)

    def test_proof_projection_binds_unchanged_ledger_source_and_public_view(self):
        from runtime.codex_builder_loop.assurance_v4.models import digest
        self.prepare_proof()
        self.apply(self.proof_result())
        before = read_ledger(self.repo, self.run_id)
        context = core.driver_context(self.repo, self.run_id)
        origin = context["evidence_origin"]
        self.assertEqual(origin["source"]["digest"], digest(before["evidence"]))
        self.assertEqual(origin["view"]["digest"], digest(context["evidence"]))
        self.assertEqual(origin["transformation"], "proof_schema_view_v1")
        self.assertNotEqual(origin["source"]["digest"], origin["view"]["digest"])
        self.assertIn("counterexample", before["evidence"]["proof"]["details"]["results"][0])
        self.assertIn("baseline", context["evidence"]["proof"]["details"]["results"][0])
        self.assertEqual(read_ledger(self.repo, self.run_id), before)

    def test_unrecorded_proof_error_preserves_accepted_dispatch(self):
        self.prepare_proof()
        self.result = self.proof_result()
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action["action_id"],
                               result_value=self.result)
        with patch.object(core, "prove_tests", side_effect=core.AssuranceError(
            "unrecorded error", code="UNRECORDED_PROOF_ERROR", status="FAIL"
        )):
            with self.assertRaises(core.AssuranceError):
                self.apply()
        ledger = read_ledger(self.repo, self.run_id)
        self.assertEqual(ledger["phase"], "active")
        self.assertEqual(ledger["dispatch_intent"]["action_id"], self.action["action_id"])
        self.assertNotIn("proof", ledger["evidence"])

    def test_real_proof_commands_do_not_hold_the_repository_lock(self):
        self.prepare_proof()
        self.result = self.proof_result()
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action["action_id"],
                               result_value=self.result)
        original = core._proof_run
        observations = []
        def observe(*args, **kwargs):
            probe = subprocess.run([
                sys.executable, "-c",
                "import fcntl,sys; f=open(sys.argv[1], 'a'); "
                "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                str(self.repo / ".git/builder-loop-assurance-v4/repo.lock"),
            ], capture_output=True, text=True, timeout=5)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            observations.append(probe.returncode)
            return original(*args, **kwargs)
        with patch.object(core, "_proof_run", side_effect=observe):
            self.apply()
        self.assertGreaterEqual(len(observations), 2)
        self.assertEqual(read_ledger(self.repo, self.run_id)["evidence"]["proof"]["status"], "pass")

    def test_missing_action_payload_is_rejected_before_result_acceptance(self):
        self.prepare_proof()
        with self.assertRaises(core.ContractError) as raised:
            core.complete_dispatch(
                self.repo, self.run_id, action_id=self.action["action_id"],
                result_value={"result": "pass", "evidence_report": None,
                              "proof_spec": None, "problem_report": None},
            )
        self.assertEqual(raised.exception.code, "AGENT_RESULT_INVALID")
        ledger = read_ledger(self.repo, self.run_id)
        self.assertEqual(ledger["dispatch_intent"]["state"], "prepared")
        self.assertNotIn("result_path", ledger["dispatch_intent"])
        self.assertEqual(ledger["phase"], "active")


if __name__ == "__main__":
    unittest.main()
