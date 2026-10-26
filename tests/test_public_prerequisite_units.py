"""Independent CLI observations of bounded prerequisite preparation.

Design principle: criteria bind real inputs. Role envelopes only claim completion;
Git blobs, CLI decisions and the persisted event stream establish the outcome.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from harness import cleanup_repo, commit_all, git, init_repo, run_cli
from tests.test_assurance_v4_contract import contract_for


# Top-level fields declared by the planning baseline ledger schema. Optional
# fields may appear during normal lifecycle transitions; new state may not.
BASELINE_LEDGER_FIELDS = frozenset({
    'abandon_intent',
    'builder_checkpointed',
    'candidate_branch',
    'candidate_residue_intent',
    'candidate_worktree',
    'context_rotation_intent',
    'continuation_consume_intent',
    'continuation_consumed_by',
    'created_at',
    'deferred_wait',
    'deployment_transaction',
    'digests',
    'dispatch_intent',
    'dispatch_rehydration_intent',
    'doc_reference_contract_version',
    'doc_reference_scan',
    'driver_failure',
    'driver_runtime',
    'environment_lease',
    'events',
    'evidence',
    'facets',
    'final_head',
    'finalize_intent',
    'hold',
    'machine_failure',
    'owner_session_id',
    'pending_blackbox',
    'phase',
    'problem_dispositions',
    'problems',
    'proof_failure',
    'publication',
    'recomposition_intent',
    'repo_root',
    'retired_reviewer_agents',
    'retired_tester_sources',
    'reviewer_observation',
    'reviewer_replacement_intent',
    'revision_transitions',
    'run_id',
    'runtime_identity',
    'runtime_support',
    'schema_version',
    'supersede_intent',
    'target_branch',
    'target_start_head',
    'tester_replacement_intent',
    'transport_cleanup_intent',
    'updated_at',
})


class PublicPrerequisiteUnitsTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.addCleanup(cleanup_repo, self.repo)
        self.temp = tempfile.TemporaryDirectory(prefix='prerequisite-units-')
        self.addCleanup(self.temp.cleanup)
        self.artifacts = Path(self.temp.name)
        self.run_id = 'serial-prerequisites'
        self.contract = contract_for(self.repo)
        self.contract['authority']['public_prerequisites'] = ['src/public_a.py', 'src/public_b.py']
        self.contract['execution']['agents'] = {}
        self.contract['execution']['driver_enforced'] = True
        self.contract['assurance']['required'].insert(2, 'proof')
        units = self.contract['execution']['work_units']
        a = units[0]
        a['id'] = 'prepare-a'
        a['scope']['paths'] = ['src/public_a.py']
        b = deepcopy(a)
        b.update(id='prepare-b', depends_on=['prepare-a'])
        b['scope']['paths'] = ['src/public_b.py']
        units[1]['depends_on'] = ['prepare-b']
        units[2]['depends_on'] = ['prepare-b']
        units.insert(1, b)
        self.contract['execution']['recovery_policy'] = {
            'schema_version': 1, 'mode': 'automatic_nonsemantic', 'continuation_window': 3}

    def write_json(self, name, value):
        path = self.artifacts / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def cli(self, command, *args, ok=True):
        result = run_cli('assurance', '--experimental-v4', command, '--repo', self.repo, *args)
        if ok:
            self.assertEqual(result.returncode, 0, result.data)
        return result

    def run_command(self, command, *args, ok=True):
        return self.cli(command, '--run', self.run_id, *args, ok=ok)

    def start(self):
        path = self.write_json('contract.json', self.contract)
        validated = self.cli('validate', '--contract', path).data
        self.assertEqual(validated['status'], 'READY', validated)
        started = self.run_command('start', '--session-id', 'units-session', '--contract', path,
            '--driver-kind', 'native', '--driver-transport', 'codex_app_server',
            '--driver-runtime-version', 'fixture', '--driver-protocol-schema-digest', 'a' * 64).data
        self.candidate = Path(started['candidate_worktree'])
        self.ledger_path = self.candidate.parent / 'ledger.json'
        self.run_command('prepare-builder', '--agent-id', 'units-builder', '--thread-id', 'units-thread',
                         '--driver-runtime-kind', 'native')

    def ledger(self):
        return json.loads(self.ledger_path.read_text())

    def next(self):
        action = self.run_command('driver-next').data
        if action['action'] == 'scan_doc_references':
            self.run_command('scan-doc-references', '--action-id', action['action_id'],
                             '--driver-runtime-kind', 'native')
            action = self.run_command('driver-next').data
        return action

    def complete(self, expected_unit, files):
        action = self.next()
        self.assertEqual(action['work_unit_id'], expected_unit, action)
        self.run_command('begin-dispatch', '--action-id', action['action_id'], '--action', action['action'],
            '--role', 'builder', '--thread-id', 'units-thread', '--prompt-digest', 'a' * 64,
            '--output-schema-digest', 'b' * 64, '--work-unit-id', expected_unit,
            '--context-projection-digest', action['context_projection_digest'],
            '--driver-runtime-kind', 'native')
        for path, content in files.items():
            target = self.candidate / path
            if content is None:
                target.unlink()
            else:
                target.write_text(content)
        if files:
            commit_all(self.candidate, 'Prepare public prerequisite')
        result = self.write_json('result.json', dict(result='implemented', evidence_report=None,
                                                    proof_spec=None, problem_report=None))
        self.run_command('complete-dispatch', '--action-id', action['action_id'], '--result', result)
        self.apply(action)
        return action

    def apply(self, action):
        return self.run_command('apply-dispatch-result', '--action-id', action['action_id'],
                                '--driver-runtime-kind', 'native')

    def completed(self):
        return [e['details']['work_unit_id'] for e in self.ledger()['events']
                if e['kind'] == 'work_unit_completed']

    def test_serial_success_and_replay_publish_exact_blobs(self):
        self.start()
        initial_keys = set(self.ledger())
        action = self.complete('prepare-a', {'src/public_a.py': 'A = 1\n'})
        self.assertEqual(self.completed(), ['prepare-a'])
        self.assertFalse(self.ledger()['builder_checkpointed'])
        self.assertFalse([p for p in self.ledger()['problems'] if p['status'] == 'open'])
        self.apply(action)
        self.assertEqual(self.completed(), ['prepare-a'])
        self.assertEqual(self.next()['work_unit_id'], 'prepare-b')
        self.complete('prepare-b', {'src/public_b.py': 'B = 2\n'})
        self.assertTrue(self.ledger()['builder_checkpointed'])
        self.assertEqual(self.completed(), ['prepare-a', 'prepare-b'])
        final_keys = set(self.ledger())
        self.assertTrue(initial_keys.issubset(final_keys), initial_keys - final_keys)
        self.assertEqual(final_keys - BASELINE_LEDGER_FIELDS, set())
        self.next()
        self.run_command('publish-prerequisites', '--driver-runtime-kind', 'native')
        publication = self.ledger()['publication']
        self.assertEqual({f['path'] for f in publication['files']},
                         {'src/public_a.py', 'src/public_b.py'})
        for entry in publication['files']:
            self.assertEqual(entry['blob'], git(self.candidate, 'rev-parse', 'HEAD:' + entry['path']))
        self.run_command('prepare-tester', '--agent-id', 'units-tester', '--thread-id', 'units-tester-thread',
                         '--driver-runtime-kind', 'native')
        source = self.ledger()['facets']['execution']['tester_source']
        self.assertEqual(source['base_head'], publication['head'])
        for entry in publication['files']:
            self.assertEqual(git(Path(source['worktree']), 'rev-parse', 'HEAD:' + entry['path']), entry['blob'])

    def test_single_unit_publication_is_blob_bound_after_candidate_change(self):
        units = self.contract['execution']['work_units']
        units.pop(1)
        units[0]['scope']['paths'].append('src/public_b.py')
        units[1]['depends_on'] = ['prepare-a']
        units[2]['depends_on'] = ['prepare-a']
        self.start()
        self.complete('prepare-a', {'src/public_a.py': 'A = 1\n', 'src/public_b.py': 'B = 2\n'})
        self.assertTrue(self.ledger()['builder_checkpointed'])
        self.assertEqual(self.completed(), ['prepare-a'])
        self.next()
        self.run_command('publish-prerequisites', '--driver-runtime-kind', 'native')
        publication = deepcopy(self.ledger()['publication'])
        self.run_command('prepare-tester', '--agent-id', 'units-tester', '--thread-id', 'units-tester-thread',
                         '--driver-runtime-kind', 'native')
        source = self.ledger()['facets']['execution']['tester_source']
        self.assertEqual(source['base_head'], publication['head'])
        for entry in publication['files']:
            self.assertEqual(entry['blob'], git(self.candidate, 'rev-parse', 'HEAD:' + entry['path']))
            self.assertEqual(entry['blob'], git(Path(source['worktree']), 'rev-parse', 'HEAD:' + entry['path']))
        (self.candidate / 'src/public_a.py').write_text('A = 99\n')
        commit_all(self.candidate, 'Change published blob')
        result = self.run_command('prepare-tester', '--agent-id', 'units-tester', '--thread-id', 'units-tester-thread',
                                  '--driver-runtime-kind', 'native', ok=False)
        self.assertNotEqual(result.returncode, 0, result.data)
        self.assertEqual(self.ledger()['publication'], publication)

    def test_consumers_refuse_partial_publication(self):
        self.start()
        self.complete('prepare-a', {'src/public_a.py': 'A = 1\n'})
        for command, args in [('publish-prerequisites', []), ('prepare-tester',
                ['--agent-id', 'units-tester', '--thread-id', 'units-tester-thread'])]:
            result = self.run_command(command, *args, '--driver-runtime-kind', 'native', ok=False)
            self.assertNotEqual(result.returncode, 0, result.data)
            self.assertIsNone(self.ledger()['publication']['head'])
            self.assertIsNone(self.ledger()['facets']['execution']['tester_source'])

    def test_current_missing_cannot_be_deferred_to_overlapping_future_unit(self):
        self.contract['authority']['public_prerequisites'] = ['src/public_a.py']
        self.contract['execution']['work_units'][1]['scope']['paths'].append('src/public_a.py')
        self.contract['execution']['work_units'][0]['scope']['paths'].append('src/a_impl.py')
        self.start()
        self.complete('prepare-a', {'src/a_impl.py': 'IMPLEMENTED = True\n'})
        self.assertNotIn('prepare-a', self.completed())
        self.assertFalse(self.ledger()['builder_checkpointed'])
        self.assertTrue([p for p in self.ledger()['problems'] if p['status'] == 'open'])
        action = self.next()
        self.assertEqual(action['work_unit_id'], 'prepare-a')
        self.assertEqual(action['reason'], 'authorized_prerequisite_completion')

    def test_loss_of_previously_ready_file_blocks_next_completion(self):
        self.contract['execution']['work_units'][1]['scope']['paths'].append('src/public_a.py')
        self.start()
        self.complete('prepare-a', {'src/public_a.py': 'A = 1\n'})
        self.assertEqual(self.completed(), ['prepare-a'])
        self.complete('prepare-b', {'src/public_a.py': None, 'src/public_b.py': 'B = 2\n'})
        self.assertEqual(self.completed(), ['prepare-a'])
        self.assertFalse(self.ledger()['builder_checkpointed'])
        self.assertTrue([p for p in self.ledger()['problems'] if p['status'] == 'open'])

    def test_unchanged_file_and_completion_claim_do_not_establish_readiness(self):
        (self.repo / 'src/public_a.py').write_text('A = 1\n')
        commit_all(self.repo, 'Existing public file before preparation')
        self.contract['authority']['public_prerequisites'] = ['src/public_a.py']
        self.contract['execution']['work_units'][0]['scope']['paths'].append('src/a_impl.py')
        self.start()
        self.complete('prepare-a', {'src/a_impl.py': 'IMPLEMENTED = True\n'})
        self.assertNotIn('prepare-a', self.completed())
        self.assertFalse(self.ledger()['builder_checkpointed'])
        rejected = self.run_command('publish-prerequisites', '--driver-runtime-kind', 'native', ok=False)
        self.assertNotEqual(rejected.returncode, 0, rejected.data)
        self.assertIsNone(self.ledger()['publication']['head'])

    def assert_admission_rejected(self, expected_units):
        path = self.write_json('invalid.json', self.contract)
        result = self.cli('validate', '--contract', path, ok=False)
        self.assertNotEqual(result.data['status'], 'READY', result.data)
        blockers = result.data['admission']['prerequisite_blockers']
        matching = [b for b in blockers if b['path'] == 'src/public_b.py']
        self.assertTrue(matching, blockers)
        self.assertTrue(matching[0]['reason'])
        self.assertTrue(set(expected_units).issubset(matching[0]['work_unit_ids']), matching)
        started = self.run_command('start', '--session-id', 'units-session', '--contract', path, ok=False)
        self.assertNotEqual(started.returncode, 0, started.data)
        self.assertFalse((self.repo / '.git/builder-loop-assurance-v4/runs' / self.run_id).exists())

    def test_admission_rejects_missing_producer_scope(self):
        self.contract['execution']['work_units'][1]['scope']['paths'] = ['src/other.py']
        self.assert_admission_rejected([])

    def test_admission_rejects_producer_blocked_by_tester(self):
        units = self.contract['execution']['work_units']
        units[1]['depends_on'] = ['tester-work-unit']
        units[2]['depends_on'] = []
        self.assert_admission_rejected(['prepare-b'])

    def test_admission_rejects_producer_blocked_by_reviewer(self):
        units = self.contract['execution']['work_units']
        units[1]['depends_on'] = ['reviewer-work-unit']
        units[3]['depends_on'] = ['prepare-a']
        self.assert_admission_rejected(['prepare-b'])
