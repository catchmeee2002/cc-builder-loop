from __future__ import annotations

import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from harness import ROOT, cleanup_repo, commit_all, git, init_repo
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.native_driver.app_server import AppServerError, AppServerTransport
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from tests.test_assurance_v4_contract import contract_for
from tests import test_result_application_contract as fixture


class AuthorUnitsTest(unittest.TestCase):
    begin = fixture.ResultApplicationContractTest.begin
    apply = fixture.ResultApplicationContractTest.apply

    def setUp(self):
        self.repo = init_repo()
        self.run_id = 'author-units'
        contract = contract_for(self.repo)
        contract['execution']['agents'] = {}
        contract['execution']['driver_enforced'] = True
        units = contract['execution']['work_units']
        tester = next(u for u in units if u['role'] == 'tester')
        author_units = []
        for i in range(4):
            unit = copy.deepcopy(tester)
            unit['id'] = f'author-{i}'
            unit['objective'] = f'Author frozen case {i}'
            unit['depends_on'] = [f'author-{i-1}'] if i else tester['depends_on']
            author_units.append(unit)
        for u in units:
            if tester['id'] in u['depends_on']:
                u['depends_on'] = [author_units[-1]['id'] if d == tester['id'] else d for d in u['depends_on']]
        contract['execution']['work_units'] = [u for u in units if u['role'] == 'builder'] + author_units + [u for u in units if u['role'] == 'reviewer']
        started = core.start(self.repo, self.run_id, 'session', contract,
            driver_runtime={'kind': 'native', 'protocol_version': 1, 'transport': 'codex_app_server',
                            'runtime_version': 'fixture', 'protocol_schema_digest': 'a' * 64})
        self.candidate = Path(started['candidate_worktree'])
        core.prepare_builder(self.repo, self.run_id, 'builder', thread_id='builder-thread')
        self.action = self.begin('builder')
        (self.candidate / 'src/calc.py').write_text('def add(a, b):\n    return a + b\n')
        commit_all(self.candidate, 'implement')
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action['action_id'],
                               result_value={'result': 'implemented', 'evidence_report': None, 'proof_spec': None, 'problem_report': None})
        self.apply()
        core.scan_doc_references(self.repo, self.run_id)
        core.prepare_tester(self.repo, self.run_id, 'tester', 'tester-thread')

    def tearDown(self):
        cleanup_repo(self.repo)

    def test_four_units_keep_producer_and_do_not_consume_correction_allowance(self):
        files = []
        for i in range(4):
            core.scan_doc_references(self.repo, self.run_id)
            self.action = self.begin('tester')
            self.assertEqual(self.action['action'], 'tester_author')
            self.assertEqual(self.action['work_unit_id'], f'author-{i}')
            ledger = read_ledger(self.repo, self.run_id)
            source = Path(ledger['facets']['execution']['tester_source']['worktree'])
            name = f'tests/test_case_{i}.py'
            (source / name).write_text(f'def test_case_{i}():\n    from src.calc import add\n    assert add({i}, 1) == {i+1}\n')
            head = commit_all(source, f'author case {i}')
            files.append({'path': name, 'blob': git(source, 'rev-parse', head + ':' + name)})
            result = {'result': 'tests_ready', 'problem_report': None, 'proof_spec': None,
                      'evidence_report': {'schema_version': 1, 'kind': 'tester', 'status': 'pass',
                        'candidate_head': head, 'producer': {'role': 'tester', 'agent_id': 'tester', 'thread_id': 'tester-thread'},
                        'details': {'result': 'tests_ready', 'source_head': head, 'files': copy.deepcopy(files)}}}
            core.complete_dispatch(self.repo, self.run_id, action_id=self.action['action_id'], result_value=result)
            self.apply()
            # Replaying accepted results does not duplicate a completion or integration.
            self.apply(result)
            ledger = read_ledger(self.repo, self.run_id)
            self.assertEqual(core.evidence_state(ledger, 'tester'), 'pass' if i == 3 else 'missing')
            self.assertEqual(driver._tester_correction_progress(ledger)['completed'], 0)
            self.assertEqual(ledger['facets']['execution']['agents']['tester']['thread_id'], 'tester-thread')
            completions = [e for e in ledger['events'] if e['kind'] == 'work_unit_completed' and e['details']['role'] == 'tester']
            self.assertEqual(len(completions), i + 1)
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.run_id, core=Mock(), transport=Mock())
        context = core.driver_context(self.repo, self.run_id)
        self.assertFalse(coordinator._start_context_rotation({'work_unit_id': 'next'}, 'tester', context))
        coordinator.core.call.assert_not_called()

    def test_reusing_previous_source_does_not_complete_next_unit_or_loop(self):
        self.action = self.begin('tester')
        ledger = read_ledger(self.repo, self.run_id)
        source = Path(ledger['facets']['execution']['tester_source']['worktree'])
        name = 'tests/test_first.py'
        (source / name).write_text('def test_first():\n    from src.calc import add\n    assert add(2, 3) == 5\n')
        head = commit_all(source, 'first unit')
        result = {'result': 'tests_ready', 'proof_spec': None, 'problem_report': None,
                  'evidence_report': {'schema_version': 1, 'kind': 'tester', 'status': 'pass',
                    'candidate_head': head,
                    'producer': {'role': 'tester', 'agent_id': 'tester', 'thread_id': 'tester-thread'},
                    'details': {'result': 'tests_ready', 'source_head': head,
                                'files': [{'path': name, 'blob': git(source, 'rev-parse', head + ':' + name)}]}}}
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action['action_id'], result_value=result)
        self.apply()
        core.scan_doc_references(self.repo, self.run_id)
        self.action = self.begin('tester')
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action['action_id'], result_value=result)
        with self.assertRaises(core.AssuranceError) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, 'WORK_UNIT_SOURCE_UNCHANGED')
        ledger = read_ledger(self.repo, self.run_id)
        self.assertNotIn('author-1', core.work_unit_progress(ledger)['completed'])
        self.assertEqual(core.evidence_state(ledger, 'tester'), 'missing')



class MemoryIsolationTest(unittest.TestCase):
    def transport(self, valid=True):
        t = AppServerTransport(isolate_memories=True, executable_identity={'fixture': True})
        values = {k: False if valid else True for k in ('use_memories', 'generate_memories')}
        response = {'config': {'memories': values}, 'origins': {
            'memories.' + k: {'name': {'type': 'sessionFlags'}} for k in values}}
        t._request = Mock(return_value=response)
        return t

    def test_fixed_profile_is_in_real_launch_argv(self):
        t = self.transport()
        self.assertEqual(t._launch_argv()[1:], ['app-server', '--stdio', '-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false'])
        t._verify_memory_isolation('/tester')
        t._request.assert_called_once_with('config/read', {'includeLayers': True, 'cwd': '/tester'})

    def test_unverified_profile_stops_before_role_or_model(self):
        for operation in ('start', 'resume', 'turn'):
            t = self.transport(valid=False)
            with self.subTest(operation=operation), self.assertRaises(AppServerError) as caught:
                if operation == 'start':
                    t.start_thread(cwd='/tester', developer_instructions='role', sandbox='workspace-write')
                elif operation == 'resume':
                    t.resume_thread(thread_id='old', cwd='/tester', developer_instructions='role', sandbox='workspace-write')
                else:
                    t.run_turn(thread_id='old', cwd='/tester', prompt='task', output_schema={}, action_id='a')
            self.assertEqual(caught.exception.code, 'NATIVE_MEMORY_ISOLATION_UNVERIFIED')
            self.assertEqual([c.args[0] for c in t._request.call_args_list], ['config/read'])

    def test_global_false_without_process_origin_is_not_admission(self):
        t = self.transport()
        t._request.return_value['origins'] = {}
        with self.assertRaises(AppServerError):
            t._verify_memory_isolation('/tester')


class PhaseInstructionsTest(unittest.TestCase):
    def test_author_receives_author_discipline_without_other_phase_commands(self):
        import tomllib
        role = tomllib.loads((ROOT / 'agents/tester.toml').read_text())['developer_instructions']
        author = NativeCoordinator._tester_instructions(role, author=True)
        self.assertIn('work_unit', author)
        self.assertIn('当前 turn 继续', author)
        self.assertNotIn('phase=machine_diagnose', author)
        self.assertNotIn('phase=blackbox：', author)
        common = NativeCoordinator._tester_instructions(role, common=True)
        self.assertNotIn('phase=proof', common)
        self.assertIn('role_phase_instructions', common)
        self.assertIn('pytest.main', common)
        self.assertIn('公开注入 seam', common)
        self.assertEqual(NativeCoordinator._tester_instructions(role), role)


class MemoryIsolationProcessTest(unittest.TestCase):
    def test_process_receives_flags_and_checks_real_cwd_before_model(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            log = root / 'calls.jsonl'
            server = root / 'codex'
            server.write_text("""#!/usr/bin/env python3
import json, sys
from pathlib import Path
log = Path(__file__).with_name('calls.jsonl')
log.write_text(json.dumps({'argv':sys.argv[1:]})+'\\n')
for line in sys.stdin:
    msg=json.loads(line)
    with log.open('a') as f: f.write(json.dumps(msg)+'\\n')
    method=msg.get('method')
    if 'id' not in msg: continue
    if method=='initialize': result={'userAgent':'fixture'}
    elif method=='config/read':
        bad=msg['params'].get('cwd')=='/drift'
        keys=('use_memories','generate_memories')
        result={'config':{'memories':{k:bad for k in keys}},
                'origins':{'memories.'+k:{'name':{'type':'sessionFlags'}} for k in keys}}
    elif method=='thread/start': result={'thread':{'id':'test-thread'}}
    else: raise RuntimeError(method)
    print(json.dumps({'id':msg['id'],'result':result}),flush=True)
""")
            server.chmod(0o755)
            t=AppServerTransport(codex_bin=str(server), isolate_memories=True)
            with t:
                self.assertEqual(t.start_thread(cwd='/ok', developer_instructions='role', sandbox='workspace-write'), 'test-thread')
                with self.assertRaises(AppServerError) as caught:
                    t.run_turn(thread_id='test-thread', cwd='/drift', prompt='task', output_schema={}, action_id='action')
                self.assertEqual(caught.exception.code, 'NATIVE_MEMORY_ISOLATION_UNVERIFIED')
            records=[json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(records[0]['argv'], t._launch_argv()[1:])
            self.assertEqual([r['params'].get('cwd') for r in records if r.get('method')=='config/read'], [None, '/ok', '/drift'])
            self.assertFalse(any(r.get('method')=='turn/start' for r in records))
            self.assertTrue(t.cleanup_observation['process_group_gone'])
