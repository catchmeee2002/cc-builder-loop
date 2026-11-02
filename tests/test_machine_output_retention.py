"""Machine diagnostics retain the beginning of long real subprocess output."""
import json
import unittest
import copy
from pathlib import Path
from unittest.mock import patch
from runtime.codex_builder_loop.assurance_v4 import driver
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from tests import test_full_driver_v4_contract as fixture
from runtime.codex_builder_loop.assurance_v4 import core

class MachineOutputRetentionTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FullDriverV4ContractTest()
        self.f.setUp()
        self.repo = self.f.repo
        self.rid = "long-output"
        contract = fixture.contract_for(self.repo)
        contract["execution"]["driver_enforced"] = False
        contract["assurance"]["machine_commands"] = [{"id": "long-failure",
            "argv": ["python3", "-c", "import sys; print('FIRST_ASSERTION实际值'); print('x'*16000); print('FIRST_STDERR',file=sys.stderr); print('y'*16000,file=sys.stderr); sys.exit(9)"],
            "timeout_seconds": 30, "run_before_full_suite": True}]
        original_invoke = self.f.invoke
        def invoke(command, *args, **kwargs):
            if command == "start":
                args = (*args, "--driver-kind", "full_driver_skill")
            elif command in {"checkpoint-builder", "prepare-tester", "integrate-tester", "record-evidence",
                             "verify-preflight", "verify-machine", "record-problems"}:
                args = (*args, "--driver-runtime-kind", "full_driver_skill")
            return original_invoke(command, *args, **kwargs)
        self.f.invoke = invoke
        _, self.run_path = self.f.start(self.rid, contract=contract)
        self.f.invoke("checkpoint-builder", "--repo", self.repo, "--run", self.rid)
        self.f.prepare_and_record_tester(self.rid, self.run_path)

    def tearDown(self):
        self.f.tearDown()

    def test_full_output_survives_verification_worktree_cleanup(self):
        rc, result = self.f.invoke("verify-preflight", "--repo", self.repo, "--run", self.rid)
        self.assertEqual(rc, 0, result)
        ledger = self.f.load_ledger(self.run_path)
        command = ledger["machine_failure"]["commands"][0]
        self.assertLessEqual(len(command["stdout"]), 8000)
        self.assertNotIn("FIRST_ASSERTION", command["stdout"])
        self.assertIn("output_ref", command)
        rc, full = self.f.invoke("read-machine-output", "--repo", self.repo, "--run", self.rid,
            "--digest", command["output_ref"]["digest"])
        self.assertEqual(rc, 0, full)
        self.assertTrue(full["result"]["stdout"].startswith("FIRST_ASSERTION实际值"))
        self.assertTrue(full["result"]["stderr"].startswith("FIRST_STDERR"))
        self.assertEqual(full["candidate_head"], ledger["facets"]["execution"]["candidate_head"])
        self.assertEqual(full["result"]["returncode"], 9)
        self.assertEqual(command, ledger["evidence"]["preflight"]["details"]["commands"][0])

    def legacy_failure(self):
        # Execute a real command through the old bounded-output contract, without a full artifact.
        with patch.object(core, "_save_machine_output", return_value=None):
            core.verify_preflight(self.repo, self.rid)
        ledger = self.f.load_ledger(self.run_path)
        agent = ledger["facets"]["execution"]["agents"]["tester"]
        report = {"schema_version": 1, "problems": [
            {"key": "output-missing", "owner": "builder_loop", "summary": "Missing diagnostics",
             "details": "Failure assertion was lost before persistence."}]}
        core.record_problems(self.repo, self.rid, report, role="tester", **agent)
        return self.f.load_ledger(self.run_path)

    def recovery_args(self, ledger):
        return dict(session_id="full-driver-v4-session", problem_key="output-missing",
            action_id=driver.next_action(self.repo, self.rid)["action_id"],
            failure_digest=ledger["machine_failure"]["failure_digest"],
            reason="Recapture full diagnostics without changing the contract.",
            driver_runtime_kind="full_driver_skill")

    def test_real_cli_recollection_preserves_original_thread_and_failure(self):
        before = self.legacy_failure()
        args = self.recovery_args(before)
        cli_args = [part for key, value in args.items() for part in ("--" + key.replace("_", "-"), value)]
        rc, result = self.f.invoke("recollect-machine-failure", "--repo", self.repo, "--run", self.rid, *cli_args)
        self.assertEqual(rc, 0, result)
        after = self.f.load_ledger(self.run_path)
        self.assertEqual(after["facets"], before["facets"])
        self.assertEqual(after["runtime_identity"], before["runtime_identity"])
        self.assertEqual(after["evidence"]["tester"], before["evidence"]["tester"])
        self.assertEqual(after["evidence"]["preflight"]["status"], "fail")
        event = after["events"][-1]["details"]
        previous = json.loads(Path(event["previous_path"]).read_text())
        self.assertEqual(previous["failure"], before["machine_failure"])
        self.assertEqual(after["problems"][0]["details"], before["problems"][0]["details"])
        self.assertEqual(after["problems"][0]["status"], "resolved")
        action = driver.next_action(self.repo, self.rid)
        self.assertEqual(action["action"], "tester_machine_diagnose")
        self.assertEqual(action["agent"], before["facets"]["execution"]["agents"]["tester"])
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.rid, core=object(), transport=object(), project_root=fixture.ROOT)
        payload = json.loads(coordinator._prompt(action, "tester", core.driver_context(self.repo, self.rid)).split("\n", 1)[1])
        ref = payload["machine_failure"]["commands"][0]["output_ref"]
        self.assertIn("FIRST_ASSERTION", core.read_machine_output(self.repo, self.rid, ref["digest"])["result"]["stdout"])
        self.assertIn("read-machine-output", payload["machine_diagnosis_rule"])
        content = (self.run_path / "ledger.json").read_bytes()
        rc, replay = self.f.invoke("recollect-machine-failure", "--repo", self.repo, "--run", self.rid, *cli_args)
        self.assertEqual(rc, 0, replay)
        self.assertEqual((self.run_path / "ledger.json").read_bytes(), content)

    def test_stale_owner_action_failure_and_hold_cannot_recollect(self):
        before = self.legacy_failure()
        args = self.recovery_args(before)
        for override in ({"session_id": "other"}, {"action_id": "a"*64}, {"failure_digest": "b"*64},
                         {"problem_key": "other"}, {"reason": " "}, {"driver_runtime_kind": "native"}):
            with self.subTest(override=override):
                with patch.object(core, "_run_machine_commands", side_effect=AssertionError("must not execute")):
                    with self.assertRaises(core.AssuranceError):
                        core.recollect_machine_failure(self.repo, self.rid, **{**args, **override})
                self.assertEqual(self.f.load_ledger(self.run_path), before)
        core.hold_run(self.repo, self.rid, session_id="full-driver-v4-session", reason="keep stopped")
        with self.assertRaises(core.AssuranceError) as caught:
            core.recollect_machine_failure(self.repo, self.rid, **args)
        self.assertEqual(caught.exception.code, "MACHINE_RECOLLECTION_PENDING")

    def test_tampered_output_is_rejected_and_signature_binds_non_tail_bytes(self):
        core.verify_machine(self.repo, self.rid)
        ledger = self.f.load_ledger(self.run_path)
        command = ledger["machine_failure"]["commands"][0]
        changed = copy.deepcopy(command)
        changed["output_ref"]["stdout_sha256"] = "a" * 64
        self.assertNotEqual(core._machine_failure_signature("machine", [command]),
                            core._machine_failure_signature("machine", [changed]))
        changed = copy.deepcopy(command)
        changed["output_ref"]["path"] += "-other"
        changed["output_ref"]["digest"] = "b" * 64
        self.assertEqual(core._machine_failure_signature("machine", [command]),
                         core._machine_failure_signature("machine", [changed]))
        ref = command["output_ref"]
        Path(ref["path"]).write_text("{}")
        rc, rejected = self.f.invoke("read-machine-output", "--repo", self.repo, "--run", self.rid, "--digest", ref["digest"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(rejected["code"], "MACHINE_OUTPUT_INVALID")

    def test_candidate_drift_cannot_recollect_or_change_ledger(self):
        before = self.legacy_failure()
        args = self.recovery_args(before)
        dirty = Path(before["candidate_worktree"]) / "src" / "calc.py"
        dirty.write_text(dirty.read_text() + "\n# unrelated edit\n")
        with patch.object(core, "_run_machine_commands", side_effect=AssertionError("must not execute")):
            with self.assertRaises(core.AssuranceError) as caught:
                core.recollect_machine_failure(self.repo, self.rid, **args)
        self.assertEqual(caught.exception.code, "MACHINE_RECOLLECTION_GIT_DRIFT")
        self.assertEqual(self.f.load_ledger(self.run_path), before)
        self.assertIn("unrelated edit", dirty.read_text())

    def test_timeout_retains_partial_output(self):
        ledger = self.f.load_ledger(self.run_path)
        command = {"id": "timeout-output", "argv": ["python3", "-c",
            "import time; print('BEFORE_TIMEOUT',flush=True); time.sleep(10)"],
            "timeout_seconds": 0.2, "expected_returncodes": [0]}
        result = core._run_machine_commands(self.repo, self.rid,
            ledger["facets"]["execution"]["candidate_head"], [command])[0]
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["cleanup"]["process_group_gone"])
        full = core.read_machine_output(self.repo, self.rid, result["output_ref"]["digest"])
        self.assertIn("BEFORE_TIMEOUT", full["result"]["stdout"])
        self.assertTrue(full["result"]["timed_out"])
