"""Public result entrypoints must preserve unfinished independent author work."""
import copy
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import ROOT, cleanup_repo, commit_all, git
from tests.helpers import prerequisite_recovery as helpers
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.store import read_ledger


class PublicationApplicationRecoveryTest(unittest.TestCase):
    def fixture(self, *, partial=True, root=True):
        f = helpers.PrerequisiteFixture()
        self.addCleanup(f.tearDown)
        f.setUp(root_session=root, initial_files=['src/public_a.py', 'src/public_b.py'])
        core.scan_doc_references(f.repo, f.run_id)
        core.publish_prerequisites(f.repo, f.run_id)
        core.prepare_tester(f.repo, f.run_id, 'tester', 'tester-thread')
        f.action = f.begin('tester')
        source = self.ledger(f)['facets']['execution']['tester_source']
        f.tester = Path(source['worktree'])
        if partial:
            (f.tester / 'tests/partial.py').write_text('def test_partial():\n    assert 2 + 2 == 4\n')
            f.partial = commit_all(f.tester, 'test(fixture): [cr_id_skip] Preserve unfinished author work')
        report = {'result': 'blocked', 'evidence_report': None, 'proof_spec': None,
            'problem_report': {'schema_version': 1, 'problems': [{
                'key': 'public-api-incomplete', 'owner': 'builder',
                'summary': 'Published API requires correction', 'details': 'Correct the published public value.'}]}}
        core.complete_dispatch(f.repo, f.run_id, action_id=f.action['action_id'], result_value=report)
        core.apply_dispatch_result(f.repo, f.run_id, action_id=f.action['action_id'], driver_runtime_kind='native')
        f.action = f.begin('builder')
        (f.candidate / 'src/public_a.py').write_text('PUBLIC_VALUE = 2\n')
        f.incoming = commit_all(f.candidate, 'fix(fixture): [cr_id_skip] Correct public API')
        f.complete()
        return f

    def ledger(self, f):
        return read_ledger(f.repo, f.run_id)

    def recompose(self, f):
        return core.recompose_candidate(f.repo, f.run_id)

    def test_application_and_replay_preserve_draft_without_passing_or_integrating_it(self):
        for root in (True, False):
            with self.subTest(root=root):
                f = self.fixture(root=root)
                before = self.ledger(f)
                artifact = Path(before['dispatch_intent']['result_path']).read_bytes()
                f.apply()
                f.apply()
                pending = self.ledger(f)
                self.assertIsNone(pending['dispatch_intent'])
                self.assertEqual(pending['recomposition_intent']['state'], 'prepared')
                self.assertEqual(driver.next_action(f.repo, f.run_id)['action'], 'recompose_candidate')
                self.recompose(f)
                after = self.ledger(f)
                self.assertIsNone(after['recomposition_intent'])
                self.assertEqual(after['publication']['generation'], 2)
                self.assertEqual(after['facets']['execution']['agents'], before['facets']['execution']['agents'])
                self.assertEqual(Path(before['dispatch_intent']['result_path']).read_bytes(), artifact)
                self.assertTrue((f.tester / 'tests/partial.py').is_file())
                self.assertFalse((f.candidate / 'tests/partial.py').exists())
                self.assertEqual((f.tester / 'src/public_a.py').read_text(), 'PUBLIC_VALUE = 2\n')
                self.assertNotIn('tester', after['evidence'])
                self.assertEqual(after['facets']['execution']['tester_files'], [])
                self.assertEqual(sum(e['kind'] == 'dispatch_consumed' and e['details']['action_id'] == f.action['action_id'] for e in after['events']), 1)
                core.scan_doc_references(f.repo, f.run_id)
                self.assertEqual(driver.next_action(f.repo, f.run_id)['action'], 'tester_author')

    def test_draft_conflict_resolution_keeps_author_work_pending(self):
        f = self.fixture()
        f.apply()
        target_test = 'def test_partial():\n    assert 3 == 3\n'
        (f.repo / 'tests/partial.py').write_text(target_test)
        commit_all(f.repo, 'test(fixture): [cr_id_skip] Advance target test')
        self.recompose(f)
        pending = self.ledger(f)
        self.assertEqual(pending['recomposition_intent']['state'], 'waiting_tester')
        f.action = f.begin('tester')
        self.assertEqual(f.action['action'], 'tester_recompose_fix')
        staging = Path(pending['recomposition_intent']['tester_worktree'])
        resolved = 'def test_partial():\n    assert 2 + 2 == 4\n    assert 3 == 3\n'
        (staging / 'tests/partial.py').write_text(resolved)
        commit_all(staging, 'test(fixture): [cr_id_skip] Resolve draft conflict')
        core.complete_dispatch(f.repo, f.run_id, action_id=f.action['action_id'], result_value={
            'result': 'tests_ready', 'evidence_report': None, 'problem_report': None, 'proof_spec': None})
        core.apply_dispatch_result(f.repo, f.run_id, action_id=f.action['action_id'], driver_runtime_kind='native')
        after = self.ledger(f)
        self.assertIsNone(after['recomposition_intent'])
        self.assertEqual((f.candidate / 'tests/partial.py').read_text(), target_test)
        self.assertEqual((f.tester / 'tests/partial.py').read_text(), resolved)
        self.assertNotIn('tester', after['evidence'])
        self.assertNotIn(f.action['work_unit_id'], core._completed_work_unit_ids(after))
        core.scan_doc_references(f.repo, f.run_id)
        self.assertEqual(driver.next_action(f.repo, f.run_id)['action'], 'tester_author')

    def test_draft_conflict_cannot_publish_nonregular_test_entry(self):
        f = self.fixture()
        f.apply()
        (f.repo / 'tests/partial.py').write_text('target = True\n')
        commit_all(f.repo, 'test(fixture): [cr_id_skip] Conflict with draft')
        self.recompose(f)
        pending = self.ledger(f)
        staging = Path(pending['recomposition_intent']['tester_worktree'])
        path = staging / 'tests/partial.py'
        path.unlink()
        path.symlink_to('../src/public_a.py')
        commit_all(staging, 'test(fixture): [cr_id_skip] Introduce unsupported entry')
        before_candidate = git(f.candidate, 'rev-parse', 'HEAD')
        before_tester = git(f.tester, 'rev-parse', 'HEAD')
        with self.assertRaises(core.AssuranceError) as caught:
            self.recompose(f)
        self.assertEqual(caught.exception.code, 'RECOMPOSITION_TESTER_AUTHORITY_VIOLATION')
        self.assertEqual(git(f.candidate, 'rev-parse', 'HEAD'), before_candidate)
        self.assertEqual(git(f.tester, 'rev-parse', 'HEAD'), before_tester)

    def test_target_restart_preserves_resolved_draft_delta(self):
        f = self.fixture()
        f.apply()
        (f.repo / 'tests/partial.py').write_text('target = True\n')
        commit_all(f.repo, 'test(fixture): [cr_id_skip] Advance target')
        self.recompose(f)
        pending = self.ledger(f)
        staging = Path(pending['recomposition_intent']['tester_worktree'])
        resolved = 'target = True\nresolved_author_work = True\n'
        (staging / 'tests/partial.py').write_text(resolved)
        commit_all(staging, 'test(fixture): [cr_id_skip] Resolve author conflict')
        (f.repo / 'src/another.py').write_text('target_advanced = True\n')
        commit_all(f.repo, 'fix(fixture): [cr_id_skip] Advance target again')
        self.recompose(f)
        after = self.ledger(f)
        self.assertIsNone(after['recomposition_intent'])
        self.assertEqual((f.tester / 'tests/partial.py').read_text(), resolved)
        self.assertEqual((f.candidate / 'tests/partial.py').read_text(), 'target = True\n')
        self.assertNotIn('tester', after['evidence'])

    def test_no_draft_path_also_converges(self):
        f = self.fixture(partial=False)
        f.apply()
        self.recompose(f)
        self.assertEqual(self.ledger(f)['publication']['generation'], 2)

    def test_sources_are_checked_before_candidate_cas(self):
        f = self.fixture()
        f.apply()
        candidate_before = git(f.candidate, 'rev-parse', 'HEAD')
        (f.tester / 'tests/unexpected.py').write_text('unknown = True\n')
        commit_all(f.tester, 'test(fixture): [cr_id_skip] External change')
        with self.assertRaises(core.AssuranceError) as caught:
            self.recompose(f)
        self.assertEqual(caught.exception.code, 'RECOMPOSITION_TESTER_DRIFT')
        self.assertEqual(git(f.candidate, 'rev-parse', 'HEAD'), candidate_before)
        self.assertTrue((f.tester / 'tests/unexpected.py').exists())

    def test_unobserved_draft_is_not_silently_adopted(self):
        f = self.fixture()
        (f.tester / 'tests/unexpected.py').write_text('unknown = True\n')
        commit_all(f.tester, 'test(fixture): [cr_id_skip] External change')
        with self.assertRaises(core.AssuranceError) as caught:
            f.apply()
        self.assertEqual(caught.exception.code, 'RECOMPOSITION_TESTER_DRAFT_UNBOUND')

    def test_replay_rejects_external_candidate_change(self):
        f = self.fixture()
        original = core._store_save_ledger
        class Interrupted(BaseException): pass
        def save(repo, ledger):
            original(repo, ledger)
            if ledger.get('recomposition_intent'):
                raise Interrupted()
        with patch.object(core, '_store_save_ledger', side_effect=save):
            with self.assertRaises(Interrupted): f.apply()
        (f.candidate / 'src/unexpected.py').write_text('outside = True\n')
        commit_all(f.candidate, 'fix(fixture): [cr_id_skip] External candidate drift')
        with self.assertRaises(core.AssuranceError) as caught: f.apply()
        self.assertEqual(caught.exception.code, 'DISPATCH_APPLICATION_WORKTREE_DRIFT')

    def test_recomposition_recovers_at_each_persisted_stage(self):
        for stage in ('prepared', 'builder_staging', 'publication_staging', 'tester_staging', 'candidate_staging', 'committing', 'event'):
            with self.subTest(stage=stage):
                f = self.fixture()
                original = core._store_save_ledger
                class Interrupted(BaseException): pass
                def save(repo, ledger):
                    original(repo, ledger)
                    rec = ledger.get('recomposition_intent') or {}
                    if rec.get('state') == stage or (stage == 'event' and any(e['kind'] == 'prerequisites_republished' for e in ledger['events'])):
                        raise Interrupted()
                with patch.object(core, '_store_save_ledger', side_effect=save):
                    with self.assertRaises(Interrupted):
                        f.apply()
                        self.recompose(f)
                if self.ledger(f)['dispatch_intent']: f.apply()
                self.recompose(f)
                after = self.ledger(f)
                self.assertIsNone(after['recomposition_intent'])
                self.assertTrue((f.tester / 'tests/partial.py').exists())
                self.assertFalse((f.candidate / 'tests/partial.py').exists())
                self.assertEqual(sum(e['kind'] == 'prerequisites_republished' for e in after['events']), 1)


    def test_formal_worktree_switch_interruptions_preserve_draft(self):
        for side in ('candidate', 'tester'):
            with self.subTest(side=side):
                f = self.fixture()
                f.apply()
                original = core.git
                class Interrupted(BaseException): pass
                def call(repo, *args, **kwargs):
                    result = original(repo, *args, **kwargs)
                    target = f.candidate if side == 'candidate' else f.tester
                    if Path(repo) == target and args[:2] == ('reset', '--hard'):
                        raise Interrupted()
                    return result
                with patch.object(core, 'git', side_effect=call):
                    with self.assertRaises(Interrupted): self.recompose(f)
                self.recompose(f)
                self.assertTrue((f.tester / 'tests/partial.py').is_file())
                self.assertFalse((f.candidate / 'tests/partial.py').exists())

    def test_repeated_publication_refresh_keeps_pending_author_work(self):
        f = self.fixture()
        f.apply()
        self.recompose(f)
        # Same original author can report another public-input problem before readiness.
        core.scan_doc_references(f.repo, f.run_id)
        f.action = f.begin('tester')
        result = {'result': 'blocked', 'evidence_report': None, 'proof_spec': None,
            'problem_report': {'schema_version': 1, 'problems': [{
                'key': 'second-api-gap', 'owner': 'builder', 'summary': 'Second public gap', 'details': 'Keep author work.'}]}}
        core.complete_dispatch(f.repo, f.run_id, action_id=f.action['action_id'], result_value=result)
        core.apply_dispatch_result(f.repo, f.run_id, action_id=f.action['action_id'], driver_runtime_kind='native')
        f.action = f.begin('builder')
        (f.candidate / 'src/public_a.py').write_text('PUBLIC_VALUE = 3\n')
        commit_all(f.candidate, 'fix(fixture): [cr_id_skip] Update public input again')
        f.complete()
        f.apply()
        self.recompose(f)
        self.assertEqual(self.ledger(f)['publication']['generation'], 3)
        self.assertTrue((f.tester / 'tests/partial.py').is_file())
        self.assertFalse((f.candidate / 'tests/partial.py').exists())

    def test_old_result_application_requires_frozen_runtime(self):
        self.assert_old_runtime_preserved()

    def test_rejected_old_runtime_migration_preserves_frozen_dirty_intake(self):
        self.assert_old_runtime_preserved(dirty_intake=True)

    def assert_old_runtime_preserved(self, *, dirty_intake=False):
        # Produce the incident with unmodified 0.4.22, not a hand-edited modern ledger.
        old_revision = '863c55ef2e68ee59218b3c92e220a0170d2795a7'
        with tempfile.TemporaryDirectory(prefix='publication-old-runtime-') as raw:
            old = Path(raw) / 'runtime'
            subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(old)], check=True)
            subprocess.run(['git', '-C', str(old), 'checkout', '--quiet', '--detach', old_revision], check=True)
            program = r"""
import json, sys
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[1] + '/tests']
from tests.helpers import prerequisite_recovery as helpers
from harness import commit_all
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
if sys.argv[2] == 'dirty':
    original_start = core.start
    def start_with_intake(repo, run_id, owner, contract, **kwargs):
        path = Path(repo) / 'src/authorized_input.py'
        path.write_text('AUTHORIZED_INPUT = True\n')
        contract['authority']['dirty_intake'] = [{'path': 'src/authorized_input.py', 'sha256': core._file_sha256(path)}]
        return original_start(repo, run_id, owner, contract, **kwargs)
    core.start = start_with_intake
f = helpers.PrerequisiteFixture()
f.setUp(root_session=True, initial_files=['src/public_a.py', 'src/public_b.py'])
core.scan_doc_references(f.repo, f.run_id)
core.publish_prerequisites(f.repo, f.run_id)
core.prepare_tester(f.repo, f.run_id, 'tester', 'tester-thread')
f.action = f.begin('tester')
source = read_ledger(f.repo, f.run_id)['facets']['execution']['tester_source']
tester = Path(source['worktree'])
(tester / 'tests/partial.py').write_text('def test_partial():\n    assert 2 + 2 == 4\n')
partial = commit_all(tester, 'test(fixture): [cr_id_skip] Preserve unfinished old author work')
result = {'result': 'blocked', 'evidence_report': None, 'proof_spec': None,
    'problem_report': {'schema_version': 1, 'problems': [{'key': 'public-api-gap', 'owner': 'builder',
        'summary': 'Public API gap', 'details': 'Correct the public API.'}]}}
core.complete_dispatch(f.repo, f.run_id, action_id=f.action['action_id'], result_value=result)
core.apply_dispatch_result(f.repo, f.run_id, action_id=f.action['action_id'], driver_runtime_kind='native')
f.action = f.begin('builder')
(f.candidate / 'src/public_a.py').write_text('PUBLIC_VALUE = 2\n')
commit_all(f.candidate, 'fix(fixture): [cr_id_skip] Correct old published API')
f.complete()
try:
    f.apply()
except core.AssuranceError as e:
    assert e.code == 'DISPATCH_APPLICATION_WORKTREE_DRIFT', (e.code, str(e))
else:
    raise AssertionError('Old runtime did not reproduce the incident')
ledger = read_ledger(f.repo, f.run_id)
print(json.dumps({'repo': str(f.repo), 'run': f.run_id, 'action': f.action['action_id'],
    'digest': ledger['dispatch_intent']['result_digest'], 'partial': partial}))
"""
            completed = subprocess.run([sys.executable, '-c', program, str(old), 'dirty' if dirty_intake else 'clean'],
                cwd=raw, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, capture_output=True, text=True, timeout=60)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            info = json.loads(completed.stdout.splitlines()[-1])
            repo = Path(info['repo'])
            self.addCleanup(cleanup_repo, repo)
            before = read_ledger(repo, info['run'])
            target_status = git(repo, 'status', '--porcelain=v1')
            target_head = git(repo, 'rev-parse', 'HEAD')
            intake_path = repo / 'src/authorized_input.py'
            intake_bytes = intake_path.read_bytes() if dirty_intake else None
            self.assertEqual(before['runtime_identity']['builder_loop_version'], '0.4.22')
            self.assertEqual(before['dispatch_intent']['application']['effects'], ['checkpoint'])
            self.assertNotIn('tester_draft', before['recomposition_intent'])
            kwargs = dict(action_id=info['action'], driver_runtime_kind='native',
                          owner_session_id='prerequisite-session')
            with self.assertRaises(core.AssuranceError):
                core.apply_dispatch_result(repo, info['run'], **kwargs)
            self.assertEqual(read_ledger(repo, info['run']), before)
            recovery = dict(session_id='prerequisite-session', result_digest=info['digest'],
                reason='Continue original checkpoint and preserve original author work',
                allow_runtime_transition=True, tester_source_head=info['partial'])
            from runtime.codex_builder_loop import core as runtime_core
            current = runtime_core.capture_runtime_identity()
            # Tests may run in a dirty development checkout; only that physical runtime
            # cleanliness observation is replaced. Old ledger and Git operations are real.
            current['adapter_dirty'] = False
            with patch.object(runtime_core, 'capture_runtime_identity', return_value=current):
                for changed in ({'session_id': 'wrong'}, {'result_digest': '0' * 64},
                                {'tester_source_head': '1' * 40}, {'allow_runtime_transition': False}):
                    with self.subTest(changed=changed):
                        with self.assertRaises(core.AssuranceError):
                            core.apply_dispatch_result(repo, info['run'], **kwargs, **{**recovery, **changed})
                        self.assertEqual(read_ledger(repo, info['run']), before)
                with self.assertRaises(core.AssuranceError):
                    core.apply_dispatch_result(repo, info['run'], **kwargs, **{**recovery, 'tester_source_head': None})
                self.assertEqual(read_ledger(repo, info['run']), before)
                if dirty_intake:
                    for mutation in ('changed', 'missing', 'symlink', 'extra'):
                        with self.subTest(target_mutation=mutation):
                            if mutation == 'changed': intake_path.write_text('CHANGED = True\n')
                            elif mutation == 'missing': intake_path.unlink()
                            elif mutation == 'symlink':
                                intake_path.unlink()
                                intake_path.symlink_to('calc.py')
                            else: (repo / 'src/unauthorized.py').write_text('EXTRA = True\n')
                            with self.assertRaises(core.AssuranceError):
                                core.apply_dispatch_result(repo, info['run'], **kwargs, **recovery)
                            self.assertEqual(read_ledger(repo, info['run']), before)
                            if intake_path.is_symlink(): intake_path.unlink()
                            intake_path.write_bytes(intake_bytes)
                            (repo / 'src/unauthorized.py').unlink(missing_ok=True)
                from runtime.codex_builder_loop.assurance_v4 import cli
                argv = ['--experimental-v4', 'apply-dispatch-result', '--repo', str(repo), '--run', info['run']]
                for key, value in {**kwargs, **recovery}.items():
                    argv.append('--' + key.replace('_', '-'))
                    if value is not True:
                        argv.append(str(value))
                for _ in range(2):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        code = cli.main(argv)
                    self.assertNotEqual(code, 0, output.getvalue())
                    self.assertEqual(read_ledger(repo, info['run']), before)
            self.assertEqual(git(repo, 'status', '--porcelain=v1'), target_status)
            self.assertEqual(git(repo, 'rev-parse', 'HEAD'), target_head)
            if dirty_intake:
                self.assertEqual(intake_path.read_bytes(), intake_bytes)


if __name__ == '__main__': unittest.main()
