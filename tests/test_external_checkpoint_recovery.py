"""External recovery after a real blocked result, checkpoint and publication refresh."""
import json
import copy
import contextlib
import io
import os
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import ROOT, commit_all, git, cleanup_repo
from tests.helpers import prerequisite_recovery as helpers
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.store import read_ledger, run_dir


class ExternalCheckpointRecoveryTest(unittest.TestCase):
    def fixture(self):
        f = helpers.PrerequisiteFixture()
        f.setUp(root_session=True, initial_files=['src/public_a.py', 'src/public_b.py'])
        self.addCleanup(f.tearDown)
        core.scan_doc_references(f.repo, f.run_id)
        core.publish_prerequisites(f.repo, f.run_id)
        core.prepare_tester(f.repo, f.run_id, 'tester', 'tester-thread')
        action = f.begin('tester')
        tester = Path(read_ledger(f.repo, f.run_id)['facets']['execution']['tester_source']['worktree'])
        (tester / 'tests/partial.py').write_text('def test_partial():\n    assert True\n')
        commit_all(tester, 'test(fixture): [cr_id_skip] Preserve author draft')
        def blocked(owner, key):
            return {'result': 'blocked', 'evidence_report': None, 'proof_spec': None,
                    'problem_report': {'schema_version': 1, 'problems': [{
                        'key': key, 'owner': owner, 'summary': 'Prerequisite unavailable',
                        'details': 'Actual environment probe failed; verification remains pending.'}]}}
        core.complete_dispatch(f.repo, f.run_id, action_id=action['action_id'], result_value=blocked('builder', 'public-input'))
        core.apply_dispatch_result(f.repo, f.run_id, action_id=action['action_id'], driver_runtime_kind='native')
        f.action = f.begin('builder')
        (f.candidate / 'src/public_a.py').write_text('PUBLIC_VALUE = 2\n')
        commit_all(f.candidate, 'fix(fixture): [cr_id_skip] Repair published prerequisite')
        f.result = blocked('external_platform', 'environment')
        f.complete()
        f.apply()
        original = read_ledger(f.repo, f.run_id)
        self.assertEqual(driver.next_action(f.repo, f.run_id)['action'], 'checkpoint_builder')
        core.checkpoint_builder(f.repo, f.run_id, owner_session_id="prerequisite-session")
        self.assertEqual(driver.next_action(f.repo, f.run_id)['action'], 'recompose_candidate')
        core.recompose_candidate(f.repo, f.run_id)
        current = read_ledger(f.repo, f.run_id)
        self.assertNotEqual(original['facets']['execution']['candidate_head'], current['facets']['execution']['candidate_head'])
        self.assertEqual(driver.next_action(f.repo, f.run_id)['reason'], 'open_external_platform_problem')
        f.head = current['facets']['execution']['candidate_head']
        f.tester = tester
        return f

    def invoke(self, f, *extra):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/codex-builder-loop.py'),
            'assurance', '--experimental-v4', 'resolve-external-problem', '--repo', str(f.repo),
            '--run', f.run_id, '--problem-key', 'environment', '--reason', 'Fresh environment probe succeeded',
            *extra], capture_output=True, text=True)
        payload = json.loads(result.stdout)
        return result.returncode, payload

    def args(self, f):
        return ['--expected-candidate-head', f.head, '--owner-session-id', 'prerequisite-session',
                '--driver-runtime-kind', 'native']

    def test_cli_recovery_preserves_report_draft_and_requires_new_verification(self):
        f = self.fixture()
        before = read_ledger(f.repo, f.run_id)
        rc, value = self.invoke(f)
        self.assertNotEqual(rc, 0)
        self.assertEqual(value['code'], 'EXTERNAL_PROBLEM_STALE')
        rc, value = self.invoke(f, *self.args(f))
        self.assertEqual(rc, 0, value)
        after = read_ledger(f.repo, f.run_id)
        old = next(p for p in before['problems'] if p['key'] == 'environment')
        new = next(p for p in after['problems'] if p['key'] == 'environment')
        self.assertEqual(new['candidate_head'], old['candidate_head'])
        self.assertEqual(new['producer'], old['producer'])
        self.assertEqual(new['status'], 'resolved')
        self.assertEqual(before['evidence'], after['evidence'])
        self.assertEqual(before['facets'], after['facets'])
        self.assertTrue((f.tester / 'tests/partial.py').exists())
        self.assertFalse((f.candidate / 'tests/partial.py').exists())
        self.assertEqual(len([e for e in after['events'] if e['kind'] == 'external_problem_resolved']), 1)
        self.assertEqual(self.invoke(f, *self.args(f))[0], 0)
        self.assertEqual(read_ledger(f.repo, f.run_id), after)
        core.scan_doc_references(f.repo, f.run_id)
        self.assertEqual(driver.next_action(f.repo, f.run_id)['action'], 'tester_author')

    def test_cli_rejects_stale_binding_wrong_owner_and_dirty_candidate_atomically(self):
        f = self.fixture()
        ledger_path = run_dir(f.repo, f.run_id) / 'ledger.json'
        for args in (['--expected-candidate-head', '0' * 40, '--owner-session-id', 'prerequisite-session', '--driver-runtime-kind', 'native'],
                     ['--expected-candidate-head', f.head, '--owner-session-id', 'stranger', '--driver-runtime-kind', 'native'],
                     ['--expected-candidate-head', f.head, '--owner-session-id', 'prerequisite-session', '--driver-runtime-kind', 'full_driver_skill'],
                     ['--allow-runtime-transition']):
            before = ledger_path.read_bytes()
            self.assertNotEqual(self.invoke(f, *args)[0], 0)
            self.assertEqual(ledger_path.read_bytes(), before)
        path = f.candidate / 'src/public_a.py'
        path.write_text('uncommitted\n')
        before = ledger_path.read_bytes()
        self.assertNotEqual(self.invoke(f, *self.args(f))[0], 0)
        self.assertEqual(ledger_path.read_bytes(), before)

    def test_pending_transactions_and_live_transport_reject_without_writes(self):
        f = self.fixture()
        original = read_ledger(f.repo, f.run_id)
        kwargs = dict(problem_key='environment', reason='Fresh probe succeeded',
            expected_candidate_head=f.head, owner_session_id='prerequisite-session', driver_runtime_kind='native')
        for field in ('dispatch_intent', 'recomposition_intent', 'supersede_intent', 'finalize_intent', 'native_transport'):
            ledger = copy.deepcopy(original)
            if field == 'native_transport':
                ledger['driver_runtime']['native_transport'] = {'state': 'running', 'process_identity': {'process_group_gone': False}}
            else:
                ledger[field] = {'state': 'prepared'}
            with patch.object(core, 'read_ledger', return_value=ledger):
                with self.assertRaises(core.AssuranceError) as caught:
                    core.resolve_external_problem(f.repo, f.run_id, **kwargs)
            self.assertEqual(caught.exception.code, 'EXTERNAL_PROBLEM_TRANSITION_PENDING')
            self.assertEqual(read_ledger(f.repo, f.run_id), original)

    def test_runtime_upgrade_is_explicit_and_preserves_original_binding(self):
        f = self.fixture()
        ledger = read_ledger(f.repo, f.run_id)
        runtime = dict(ledger['runtime_identity'], adapter_commit='f' * 40, adapter_dirty=False)
        kwargs = dict(problem_key='environment', reason='Fresh environment probe succeeded',
                      expected_candidate_head=f.head, owner_session_id='prerequisite-session', driver_runtime_kind='native')
        with patch('runtime.codex_builder_loop.core.capture_runtime_identity', return_value=runtime):
            with self.assertRaises(core.AssuranceError) as caught:
                core.resolve_external_problem(f.repo, f.run_id, **kwargs)
            self.assertEqual(caught.exception.code, 'EXTERNAL_PROBLEM_RUNTIME_IDENTITY_MISMATCH')
            self.assertEqual(read_ledger(f.repo, f.run_id), ledger)
            core.resolve_external_problem(f.repo, f.run_id, **kwargs, allow_runtime_transition=True)
        after = read_ledger(f.repo, f.run_id)
        self.assertEqual(after['runtime_identity'], runtime)
        self.assertEqual(after['facets'], ledger['facets'])
        self.assertEqual(after['evidence'], ledger['evidence'])

    def test_unmodified_0424_run_requires_its_frozen_runtime(self):
        with tempfile.TemporaryDirectory(prefix='external-old-runtime-') as raw:
            old = Path(raw) / 'runtime'
            subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(old)], check=True)
            subprocess.run(['git', '-C', str(old), 'checkout', '--quiet', '--detach',
                            '0352215700ec0be371c133df1e7db59921751ad7'], check=True)
            program = """
import importlib.util, json, sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + '/tests']
spec = importlib.util.spec_from_file_location('recovery_fixture', sys.argv[2])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
t = m.ExternalCheckpointRecoveryTest()
f = t.fixture()
rc, value = t.invoke(f)
assert value['code'] == 'EXTERNAL_PROBLEM_STALE', value
print(json.dumps({'repo': str(f.repo), 'run': f.run_id, 'head': f.head}))
"""
            result = subprocess.run([sys.executable, '-c', program, str(old), str(Path(__file__).resolve())],
                cwd=raw, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stderr)
            info = json.loads(result.stdout.splitlines()[-1])
            repo = Path(info['repo'])
            self.addCleanup(cleanup_repo, repo)
            before = read_ledger(repo, info['run'])
            self.assertEqual(before['runtime_identity']['builder_loop_version'], '0.4.24')
            from runtime.codex_builder_loop import core as runtime_core
            from runtime.codex_builder_loop.assurance_v4 import cli
            identity = {**runtime_core.capture_runtime_identity(), 'adapter_dirty': False}
            argv = ['--experimental-v4', 'resolve-external-problem', '--repo', str(repo), '--run', info['run'],
                '--problem-key', 'environment', '--reason', 'Fresh isolated environment probe succeeded',
                '--expected-candidate-head', info['head'], '--owner-session-id', 'prerequisite-session',
                '--driver-runtime-kind', 'native', '--allow-runtime-transition']
            with patch.object(runtime_core, 'capture_runtime_identity', return_value=identity):
                for _ in range(2):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        rc = cli.main(argv)
                    self.assertNotEqual(rc, 0, output.getvalue())
                    self.assertEqual(read_ledger(repo, info['run']), before)



if __name__ == '__main__':
    unittest.main()
