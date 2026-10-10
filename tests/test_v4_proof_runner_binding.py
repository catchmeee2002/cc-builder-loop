import copy
import sys
import unittest
from unittest.mock import patch
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.models import ContractError, validate_contract, validate_test_proof_spec
from tests.test_proof_schema_contract import valid_input


class ProofRunnerBindingTest(unittest.TestCase):
    def test_schema_accepts_v2_reference_only_and_keeps_legacy(self):
        spec = valid_input()
        self.assertEqual(validate_test_proof_spec(spec), spec)
        spec['schema_version'] = 2
        spec['groups'][0]['runner_id'] = 'unit'
        with self.assertRaises(ContractError):
            validate_test_proof_spec(spec)
        del spec['groups'][0]['argv']
        self.assertEqual(validate_test_proof_spec(spec), spec)
        spec['schema_version'] = 1
        with self.assertRaises(ContractError):
            validate_test_proof_spec(spec)

    def test_binding_uses_explicit_id_and_rejects_unknown_or_duplicate(self):
        runners = [{'id': 'unit', 'argv': [sys.executable, '-m', 'unittest'], 'timeout_seconds': 30},
                   {'id': 'verbose', 'argv': [sys.executable, '-m', 'unittest', '-v'], 'timeout_seconds': 30}]
        group = {'runner_id': 'verbose', 'test_ids': ['tests.test_x.X.test_x']}
        resolved, runner = core._resolve_proof_group(group, runners)
        self.assertEqual(resolved['argv'], runners[1]['argv'] + group['test_ids'])
        self.assertEqual(group['runner_id'], 'verbose')
        for requested in ('missing', 'unit'):
            bad = runners if requested == 'missing' else runners + [runners[0]]
            with self.assertRaises(core.AssuranceError):
                core._resolve_proof_group({**group, 'runner_id': requested}, bad)
        from tests.test_compact_profile_contract import compact_contract
        contract = compact_contract()
        contract['assurance']['proof_runners'] = [runners[0], {**runners[1], 'id': 'unit'}]
        with self.assertRaises(ContractError):
            validate_contract(contract)

    def test_equivalent_input_forms_do_not_count_as_diagnosis_progress(self):
        from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator, NativeDriverError
        from runtime.codex_builder_loop.assurance_v4.models import digest
        spec = valid_input()
        runner = {"id": "unit", "argv": spec["groups"][0]["argv"][:-1], "timeout_seconds": 30}
        reference = copy.deepcopy(spec)
        reference["schema_version"] = 2
        reference["groups"][0].pop("argv")
        reference["groups"][0]["runner_id"] = "unit"
        for prior, updated in ((spec, reference), (reference, spec)):
            with self.assertRaises(NativeDriverError) as error:
                NativeCoordinator._validate_proof_diagnosis(
                    {"result": "tests_ready", "proof_spec": updated, "problem_report": None},
                    proof_failure={"spec": prior, "spec_digest": digest(prior)}, proof_runners=[runner])
            self.assertEqual(error.exception.code, "NATIVE_PROOF_DIAGNOSIS_NO_PROGRESS")

    def test_real_multi_group_proof_preflights_once_and_replays_both_frameworks(self):
        from tests import test_full_driver_v4_contract as fixture
        for framework in ('unittest', 'pytest'):
            with self.subTest(framework=framework):
                helper = fixture.FullDriverV4ContractTest(methodName='test_proof_is_a_distinct_gate_after_tester_and_before_blackbox')
                helper.setUp()
                try:
                    contract = fixture.contract_for(helper.repo)
                    contract['mission']['behaviors'].append({'id': 'second', 'description': 'A second independently proved behavior.'})
                    contract['assurance']['required'].insert(2, 'proof')
                    argv = [sys.executable, '-m', framework]
                    contract['assurance']['proof_runners'] = [{'id': 'runner', 'argv': argv, 'timeout_seconds': 30}]
                    started, run_path = helper.start('runner-binding', contract=contract)
                    rc, checked = helper.invoke('checkpoint-builder', '--repo', helper.repo, '--run', 'runner-binding')
                    self.assertEqual(rc, 0, checked)
                    ledger = helper.prepare_and_record_tester('runner-binding', run_path)
                    tester = ledger['facets']['execution']['agents']['tester']
                    test_id = ('tests.test_full_driver_fixture.FullDriverFixtureTest.test_add' if framework == 'unittest'
                               else 'tests/test_full_driver_fixture.py::FullDriverFixtureTest::test_add')
                    group = {'behavior_ids': ['driver-flow'], 'method': 'mutation', 'runner_id': 'runner',
                             'test_ids': [test_id], 'timeout_seconds': 30,
                             'patch': 'diff --git a/src/calc.py b/src/calc.py\n--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b + 1\n'}
                    spec = {'schema_version': 2, 'groups': [group, {**group, 'behavior_ids': ['second']}]}
                    invalid = copy.deepcopy(spec)
                    invalid['groups'][0]['runner_id'] = 'unknown'
                    with self.assertRaises(core.AssuranceError) as rejected:
                        core.prove_tests(helper.repo, 'runner-binding', invalid, agent_id=tester['agent_id'], thread_id=tester['thread_id'])
                    self.assertEqual(rejected.exception.code, 'TEST_PROOF_RUNNER_UNBOUND')
                    failure = core.current_proof_failure(helper.load_ledger(run_path))
                    self.assertEqual(failure['recovery'], 'tester_diagnosis')
                    self.assertEqual(failure['spec'], invalid)
                    with patch.object(core, '_probe_proof_runner', wraps=core._probe_proof_runner) as probe:
                        core.prove_tests(helper.repo, 'runner-binding', spec, agent_id=tester['agent_id'], thread_id=tester['thread_id'])
                        self.assertEqual(probe.call_count, 1)
                        core.prove_tests(helper.repo, 'runner-binding', spec, agent_id=tester['agent_id'], thread_id=tester['thread_id'])
                        self.assertEqual(probe.call_count, 1)
                    current = helper.load_ledger(run_path)
                    self.assertEqual(core.evidence_state(current, 'proof'), 'pass')
                    self.assertEqual(current['evidence']['proof']['details']['spec'], spec)
                    self.assertEqual(current['evidence']['proof']['details']['results'][0]['argv'], argv + [test_id])
                    # The old command form reuses the same execution and evidence validation path.
                    legacy = copy.deepcopy(spec)
                    legacy['schema_version'] = 1
                    for item in legacy['groups']:
                        del item['runner_id']; item['argv'] = argv + item['test_ids']
                    core.prove_tests(helper.repo, 'runner-binding', legacy, agent_id=tester['agent_id'], thread_id=tester['thread_id'])
                    self.assertEqual(core.evidence_state(helper.load_ledger(run_path), 'proof'), 'pass')
                finally:
                    helper.tearDown()
