import copy
import unittest
from harness import init_repo, cleanup_repo, head, commit_all
from tests.test_compact_profile_contract import compact_contract
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.models import candidate_input_identity, evidence_dependency, facet_digests, validate_contract, ContractError, assurance_downgrades


class EvidenceReuseTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.addCleanup(cleanup_repo, self.repo)
        contract = validate_contract(compact_contract())
        contract['assurance']['evidence_neutral_paths'] = {'machine': ['README.md']}
        contract['execution']['candidate_head'] = head(self.repo)
        self.ledger = {'repo_root': str(self.repo), 'facets': contract, 'digests': facet_digests(contract), 'evidence': {}, 'events': [], 'target_start_head': head(self.repo)}
        record = {'kind': 'machine', 'status': 'pass', 'candidate_head': head(self.repo), 'details': {'commands': []}}
        record['dependency_digest'] = evidence_dependency(self.ledger, 'machine', evidence=record)
        self.ledger['evidence']['machine'] = record

    def test_reuse_requires_receipt_and_preserves_execution_head(self):
        observed = copy.deepcopy(self.ledger['evidence']['machine'])
        (self.repo / 'README.md').write_text('new documentation\n')
        self.ledger['facets']['execution']['candidate_head'] = commit_all(self.repo, 'docs')
        self.assertEqual(core.evidence_state(self.ledger, 'machine'), 'stale')
        core._record_evidence_reuse(self.ledger)
        self.assertEqual(core.evidence_state(self.ledger, 'machine'), 'pass')
        self.assertEqual(self.ledger['evidence']['machine'], observed)
        core._record_evidence_reuse(self.ledger)
        self.assertEqual(len(self.ledger['events']), 1)
        (self.repo / 'src/calc.py').write_text('def add(a,b): return 0\n')
        self.ledger['facets']['execution']['candidate_head'] = commit_all(self.repo, 'code')
        self.assertEqual(core.evidence_state(self.ledger, 'machine'), 'stale')

    def test_test_and_runner_inputs_cannot_be_exempted(self):
        for path in ['tests/test_calc.py', 'src/calc.py', 'verify.sh']:
            self.ledger['facets']['assurance']['evidence_neutral_paths']['machine'] = [path]
            with self.assertRaises(ContractError):
                candidate_input_identity(self.ledger, 'machine')

    def test_scope_changes_require_explicit_downgrade_authorization(self):
        old = self.ledger['facets']['assurance']
        changed = copy.deepcopy(old)
        changed['evidence_neutral_paths']['machine'].append('another.md')
        self.assertTrue(assurance_downgrades(old, changed))

    def test_runner_argument_aliases_cannot_be_neutral(self):
        (self.repo / 'tools').mkdir()
        path = self.repo / 'tools/check.js'
        path.write_text('console.log("checked")')
        self.ledger['facets']['execution']['candidate_head'] = commit_all(self.repo, 'runner')
        self.ledger['facets']['assurance']['evidence_neutral_paths']['machine'] = ['tools/check.js']
        for argument in ['./tools/check.js', str(path), '--script=./tools/check.js']:
            self.ledger['facets']['assurance']['machine_commands'][0]['argv'] = ['node', argument]
            with self.assertRaises(ContractError):
                candidate_input_identity(self.ledger, 'machine')

    def test_real_proof_reuses_unchanged_inputs_without_rewriting_observation(self):
        import sys
        from pathlib import Path
        from tests import test_full_driver_v4_contract as fixture
        helper = fixture.FullDriverV4ContractTest(methodName='test_proof_is_a_distinct_gate_after_tester_and_before_blackbox')
        helper.setUp()
        self.addCleanup(helper.tearDown)
        contract = fixture.contract_for(helper.repo)
        contract['assurance']['required'].insert(2, 'proof')
        contract['authority']['builder_write'].append('README.md')
        contract['assurance']['evidence_neutral_paths'] = {'proof': ['README.md']}
        started, run_path = helper.start('reuse-real-proof', contract=contract)
        rc, value = helper.invoke('checkpoint-builder', '--repo', helper.repo, '--run', 'reuse-real-proof')
        self.assertEqual(rc, 0, value)
        ledger = helper.prepare_and_record_tester('reuse-real-proof', run_path)
        tester = ledger['facets']['execution']['agents']['tester']
        test_id = 'tests.test_full_driver_fixture.FullDriverFixtureTest.test_add'
        spec = {'schema_version': 1, 'groups': [{'behavior_ids': ['driver-flow'], 'method': 'mutation', 'argv': [sys.executable, '-m', 'unittest', test_id], 'test_ids': [test_id], 'timeout_seconds': 30, 'patch': 'diff --git a/src/calc.py b/src/calc.py\n--- a/src/calc.py\n+++ b/src/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b + 1\n'}]}
        core.prove_tests(helper.repo, 'reuse-real-proof', spec, agent_id=tester['agent_id'], thread_id=tester['thread_id'])
        original = helper.load_ledger(run_path)['evidence']['proof']
        candidate = Path(started['candidate_worktree'])
        (candidate / 'README.md').write_text('Changed documentation only\n')
        commit_all(candidate, 'docs only')
        core.checkpoint_builder(helper.repo, 'reuse-real-proof')
        updated = helper.load_ledger(run_path)
        self.assertEqual(core.evidence_state(updated, 'proof'), 'pass')
        self.assertEqual(updated['evidence']['proof'], original)
        self.assertNotEqual(original['candidate_head'], updated['facets']['execution']['candidate_head'])
        self.assertEqual(core.evidence_state(updated, 'reviewer'), 'missing')

    def test_tree_cache_is_commit_bound_and_does_not_cache_authority(self):
        from unittest.mock import patch
        from runtime.codex_builder_loop.assurance_v4 import models
        models._candidate_tree_entries.cache_clear()
        original_run = models.subprocess.run
        with patch.object(models.subprocess, 'run', wraps=original_run) as run:
            first = candidate_input_identity(self.ledger, 'machine')
            self.assertEqual(candidate_input_identity(self.ledger, 'machine'), first)
            scans = lambda: sum('ls-tree' in call.args[0] for call in run.call_args_list)
            self.assertEqual(scans(), 1)
            self.ledger['facets']['authority']['protected_support_paths'].append('README.md')
            with self.assertRaises(ContractError):
                candidate_input_identity(self.ledger, 'machine')
            self.assertEqual(scans(), 1)
            self.ledger['facets']['authority']['protected_support_paths'].remove('README.md')
            (self.repo / 'src/calc.py').write_text('def add(a,b): return a-b\n')
            self.ledger['facets']['execution']['candidate_head'] = commit_all(self.repo, 'changed')
            self.assertNotEqual(candidate_input_identity(self.ledger, 'machine'), first)
            self.assertEqual(scans(), 2)
            other = init_repo()
            self.addCleanup(cleanup_repo, other)
            other_ledger = copy.deepcopy(self.ledger)
            other_ledger['repo_root'] = str(other)
            other_ledger['facets']['execution']['candidate_head'] = head(other)
            candidate_input_identity(other_ledger, 'machine')
            self.assertEqual(scans(), 3)

    def test_only_added_exemptions_are_downgrades(self):
        old = copy.deepcopy(self.ledger['facets']['assurance'])
        old['evidence_neutral_paths']['machine'] = ['README.md', 'CHANGELOG.md']
        for neutral in ({}, {'machine': []}, {'machine': ['README.md']}, {'machine': ['CHANGELOG.md', 'README.md']}):
            changed = {**old, 'evidence_neutral_paths': neutral}
            self.assertFalse(assurance_downgrades(old, changed))
        changed = {**old, 'evidence_neutral_paths': {'proof': ['README.md']}}
        self.assertTrue(assurance_downgrades(old, changed))
