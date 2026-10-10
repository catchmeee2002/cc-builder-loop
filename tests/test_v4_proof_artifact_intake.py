import copy
from pathlib import Path
import unittest
from harness import init_repo, cleanup_repo, head
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.models import validate_test_proof_spec, ContractError
from runtime.codex_builder_loop.assurance_v4.store import run_dir


class ProofArtifactTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.addCleanup(cleanup_repo, self.repo)
        self.ledger = {"run_id": "patch-test", "candidate_worktree": str(self.repo), "facets": {"authority": {"builder_write": ["src/**"], "tester_write": ["tests/**"]}, "execution": {"candidate_head": head(self.repo)}}}
        self.intent = {"role": "tester", "action": "tester_proof", "action_id": "action-1"}
        self.directory = run_dir(self.repo, "patch-test") / "artifacts" / "patches-action-1"
        self.directory.mkdir(parents=True)
        self.path = self.directory / "counterexample.patch"
        self.patch = "diff --git a/src/proof_fixture.py b/src/proof_fixture.py\n--- a/src/proof_fixture.py\n+++ b/src/proof_fixture.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 0\n"
        self.path.write_text(self.patch)
        self.result = {"result": "pass", "evidence_report": None, "problem_report": None, "proof_spec": {"schema_version": 2, "groups": [{"method": "mutation", "behavior_ids": ["b"], "argv": ["python3", "-m", "unittest", "tests.test_value"], "test_ids": ["tests.test_value.T.test_value"], "timeout_seconds": 30, "patch_file": str(self.path)}]}}

    def test_intake_freezes_bytes_independently_of_original_file(self):
        canonical = core._intake_proof_files(self.repo, self.ledger, self.intent, self.result)
        self.path.unlink()
        replay = core._intake_proof_files(self.repo, self.ledger, self.intent, canonical)
        group = replay["proof_spec"]["groups"][0]
        self.assertEqual(group["patch"], self.patch)
        self.assertEqual(group["patch_source"]["bytes"], len(self.patch.encode()))
        self.assertEqual(canonical, replay)

    def test_symlink_escape_and_invalid_patch_are_rejected(self):
        self.path.unlink()
        self.path.symlink_to(self.repo / "README.md")
        with self.assertRaises(core.AssuranceError):
            core._intake_proof_files(self.repo, self.ledger, self.intent, self.result)
        self.path.unlink()
        self.path.write_text("not a patch")
        with self.assertRaises(core.AssuranceError):
            core._intake_proof_files(self.repo, self.ledger, self.intent, self.result)

    def test_v1_and_ambiguous_input_are_rejected_for_file_handoff(self):
        spec = copy.deepcopy(self.result["proof_spec"])
        spec["schema_version"] = 1
        with self.assertRaises(ContractError):
            validate_test_proof_spec(spec)
        spec["schema_version"] = 2
        spec["groups"][0]["patch"] = self.patch
        with self.assertRaises(ContractError):
            validate_test_proof_spec(spec)

    def test_changing_only_patch_provenance_is_not_progress(self):
        from runtime.codex_builder_loop.assurance_v4.models import digest
        from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator, NativeDriverError
        canonical = core._intake_proof_files(self.repo, self.ledger, self.intent, self.result)["proof_spec"]
        revised = copy.deepcopy(canonical)
        revised["groups"][0]["patch_source"]["path"] = "/different-dispatch/same.patch"
        with self.assertRaises(NativeDriverError) as raised:
            NativeCoordinator._validate_proof_diagnosis({"result": "tests_ready", "proof_spec": revised, "problem_report": None}, proof_failure={"spec": canonical, "spec_digest": digest(canonical)})
        self.assertEqual(raised.exception.code, "NATIVE_PROOF_DIAGNOSIS_NO_PROGRESS")

    def test_completed_dispatch_replays_canonical_bytes_after_source_removal(self):
        import json
        from runtime.codex_builder_loop.assurance_v4 import driver
        from runtime.codex_builder_loop.assurance_v4.models import digest
        from tests import test_full_driver_v4_contract as fixture
        helper = fixture.FullDriverV4ContractTest(methodName='test_proof_is_a_distinct_gate_after_tester_and_before_blackbox')
        helper.setUp()
        self.addCleanup(helper.tearDown)
        contract = fixture.contract_for(helper.repo)
        contract['assurance']['required'].insert(2, 'proof')
        started, run_path = helper.start('patch-dispatch-replay', contract=contract)
        rc, checked = helper.invoke('checkpoint-builder', '--repo', helper.repo, '--run', 'patch-dispatch-replay')
        self.assertEqual(rc, 0, checked)
        ledger = helper.prepare_and_record_tester('patch-dispatch-replay', run_path)
        ledger['driver_runtime'] = {'kind': 'native', 'protocol_version': 1, 'transport': 'codex_app_server', 'runtime_version': 'patch-test', 'protocol_schema_digest': 'f' * 64}
        core.save_ledger(helper.repo, ledger)
        action = driver.next_action(helper.repo, 'patch-dispatch-replay')
        self.assertEqual(action['action'], 'tester_proof')
        core.begin_dispatch(helper.repo, 'patch-dispatch-replay', action_id=action['action_id'], action='tester_proof', role='tester', thread_id=ledger['facets']['execution']['agents']['tester']['thread_id'], prompt_digest='a'*64, output_schema_digest='b'*64)
        path = run_path / 'artifacts' / ('patches-' + action['action_id']) / 'proof.patch'
        path.write_text(self.patch)
        result = copy.deepcopy(self.result)
        result['proof_spec']['groups'][0]['patch_file'] = str(path)
        completed = core.complete_dispatch(helper.repo, 'patch-dispatch-replay', action_id=action['action_id'], result_value=result)
        intent = completed['dispatch_intent']
        canonical = json.loads(Path(intent['result_path']).read_text())
        self.assertEqual(digest(canonical), intent['result_digest'])
        self.assertEqual(canonical['proof_spec']['groups'][0]['patch'], self.patch)
        path.unlink()
        for replay in (result, canonical):
            again = core.complete_dispatch(helper.repo, 'patch-dispatch-replay', action_id=action['action_id'], result_value=replay)
            self.assertEqual(again['dispatch_intent']['result_digest'], intent['result_digest'])
        changed = copy.deepcopy(canonical)
        changed['proof_spec']['groups'][0]['patch'] += '\n'
        with self.assertRaises(core.AssuranceError):
            core.complete_dispatch(helper.repo, 'patch-dispatch-replay', action_id=action['action_id'], result_value=changed)
