"""Public CLI observation transactions. Role envelopes are synthetic, Git is real.

Design principles 1, 2 and 7: command facts are not verdicts, artifacts have one
home, and follow-up preserves the registered producer and transaction identity.
"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from harness import ROOT, cleanup_repo, commit_all, git, init_repo, run_process, fixture_runtime_env
from tests.test_assurance_v4_contract import blackbox_case_results, contract_for


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


class CliScenario:
    def __init__(self, case, runtime=ROOT, *, prerequisites=False, kind='native', early_review=False):
        self.case, self.runtime, self.kind = case, Path(runtime), kind
        # The frozen commands check application behavior; they do not nest a test runner.
        self.repo = init_repo({'verify.sh': '#!/bin/sh\npython3 -B -c "from src.calc import add; assert add(1,2) == 3"\n'})
        self.environment = {**fixture_runtime_env(self.repo), "PYTHONDONTWRITEBYTECODE": "1"}
        case.addCleanup(cleanup_repo, self.repo)
        self.temp = tempfile.TemporaryDirectory(prefix='observation-wire-')
        case.addCleanup(self.temp.cleanup)
        self.artifacts = Path(self.temp.name)
        self.run = 'observation-fixture'
        self.serial = 0
        contract = contract_for(self.repo)
        contract['execution'].update(driver_enforced=True, agents={})
        if prerequisites:
            contract['authority']['public_prerequisites'] = ['src/public_a.py', 'src/public_b.py']
            original = contract['execution']['work_units'][0]
            a, b = copy.deepcopy(original), copy.deepcopy(original)
            a.update(id='produce-a', depends_on=[])
            a['scope']['paths'] = ['src/public_a.py']
            b.update(id='produce-b', depends_on=['produce-a'])
            b['scope']['paths'] = ['src/public_b.py']
            units = contract['execution']['work_units'][1:]
            for unit in units:
                unit['depends_on'] = ['produce-b' if v == original['id'] else v for v in unit['depends_on']]
            contract['execution']['work_units'] = [a, b, *units]
        self.early_review = early_review
        if early_review:
            contract['assurance']['preflight_before_proof'] = True
            contract['assurance']['reviewer_preflight'] = True
            contract['assurance']['machine_commands'][0]['run_before_full_suite'] = True
        self.contract = contract
        started = self.call('start', '--session-id', 'observation-session',
                            '--contract', self.json_file(contract), '--driver-kind', kind,
                            '--driver-transport', 'codex_app_server' if kind == 'native' else 'native_tools',
                            '--driver-runtime-version', 'fixture',
                            '--driver-protocol-schema-digest', 'a' * 64)
        self.candidate = Path(started['candidate_worktree'])
        self.prepare('builder')

    def json_file(self, value):
        self.serial += 1
        path = self.artifacts / f'{self.serial}.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        return str(path)

    def raw(self, command, *args):
        if command == 'record-evidence' and '--driver-runtime-kind' not in args:
            args = (*args, '--driver-runtime-kind', self.kind)
        result = run_process([sys.executable, '-B', self.runtime / 'scripts/codex-builder-loop.py',
                              'assurance', '--experimental-v4', command,
                              '--repo', self.repo, '--run', self.run, *args],
                             cwd=self.runtime, env=self.environment)
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise AssertionError(f'{command}: rc={result.returncode}, {result.stdout}, {result.stderr}') from exc
        return result.returncode, data

    def call(self, command, *args):
        rc, data = self.raw(command, *args)
        self.case.assertEqual(rc, 0, (command, data))
        return data

    def ledger(self):
        # Read-only public persisted facts, never synthetic ledger mutations.
        return json.loads((self.candidate.parent / 'ledger.json').read_text())

    def prepare(self, role):
        return self.call('prepare-' + role, '--agent-id', role, '--thread-id', role + '-thread',
                         '--driver-runtime-kind', self.kind)

    def next(self):
        return self.call('driver-next')

    def begin(self, role, expected):
        action = self.next()
        self.case.assertEqual(action['action'], expected, action)
        args = ['--action-id', action['action_id'], '--action', action['action'],
                '--role', role, '--thread-id', role + '-thread', '--prompt-digest', 'b' * 64,
                '--output-schema-digest', 'c' * 64, '--driver-runtime-kind', self.kind]
        for key in ('work_unit_id', 'context_projection_digest'):
            if action.get(key):
                args += ['--' + key.replace('_', '-'), action[key]]
        self.call('begin-dispatch', *args)
        return action

    def complete(self, action, result):
        self.call('complete-dispatch', '--action-id', action['action_id'], '--result', self.json_file(result))

    def apply(self, action):
        return self.call('apply-dispatch-result', '--action-id', action['action_id'],
                         '--driver-runtime-kind', self.kind)

    def submit(self, action, result):
        self.complete(action, result)
        return self.apply(action)

    def envelope(self, result, evidence=None, problems=None):
        return dict(result=result, evidence_report=evidence, proof_spec=None, problem_report=problems)

    def evidence(self, kind, details, status='pass'):
        role = 'reviewer' if kind in ('reviewer', 'reviewer_preflight') else 'tester'
        return dict(schema_version=1, kind=kind, status=status,
                    candidate_head=self.ledger()['facets']['execution']['candidate_head'],
                    producer=dict(role=role, agent_id=role, thread_id=role + '-thread'), details=details)

    def build(self, path='src/calc.py', text='def add(a,b): return a+b\n'):
        action = self.begin('builder', 'builder_implement')
        (self.candidate / path).write_text(text)
        commit_all(self.candidate, 'implement fixture')
        self.submit(action, self.envelope('implemented'))
        return action

    def ready_review(self):
        self.build()
        self.call('scan-doc-references', '--driver-runtime-kind', self.kind)
        self.prepare('tester')
        action = self.begin('tester', 'tester_author')
        source = self.ledger()['facets']['execution']['tester_source']
        worktree = Path(source['worktree'])
        path = 'tests/test_extra.py'
        (worktree / path).write_text('from src.calc import add\ndef test_zero():\n    assert add(0,0) == 0\n')
        source_head = commit_all(worktree, 'independent fixture test')
        evidence = self.evidence('tester', dict(result='tests_ready', source_head=source_head,
            files=[dict(path=path, blob=git(worktree, 'rev-parse', source_head + ':' + path))]))
        self.submit(action, self.envelope('tests_ready', evidence))
        self.call('scan-doc-references', '--driver-runtime-kind', self.kind)
        if self.early_review:
            self.call('verify-preflight', '--driver-runtime-kind', self.kind)
        else:
            self.verify_gates()
        self.prepare('reviewer')

    def verify_gates(self):
        self.call('verify-machine', '--driver-runtime-kind', self.kind)
        action = self.begin('tester', 'tester_blackbox')
        head = git(self.candidate, 'rev-parse', 'HEAD')
        command = self.contract['execution']['commands'][0]
        result = run_process(command['argv'], cwd=self.candidate, env={'PYTHONDONTWRITEBYTECODE': '1'})
        self.case.assertEqual(result.returncode, 0, result.stderr)
        self.case.assertEqual(git(self.candidate, 'status', '--porcelain'), '')
        self.case.assertEqual(git(self.candidate, 'rev-parse', 'HEAD'), head)
        evidence = self.evidence('blackbox', dict(result='pass', worktree=str(self.candidate),
            before_head=head, after_head=head, executions=[dict(id=command['id'], argv=command['argv'],
            returncode=result.returncode, timed_out=False)], cases=blackbox_case_results(self.ledger())))
        self.submit(action, self.envelope('pass', evidence))

    def request(self, *, legacy=False):
        action = self.begin('reviewer', 'reviewer_preflight' if self.early_review else 'reviewer_final')
        problem = dict(key='missing-command-observation', owner='tester',
                       summary='Missing local command facts', details='Need the bound zero-input output.')
        if not legacy:
            problem['observation_request'] = dict(acceptance_case_ids=['add-positive'], question='What does the bound local calculator command return?')
        self.submit(action, self.envelope('findings', problems=dict(schema_version=1, problems=[problem])))
        return problem

    def observation_result(self, *, returncode=0, timed_out=False):
        ledger = self.ledger()
        source = ledger['facets']['execution']['tester_source']
        problem = next(p for p in ledger['problems'] if p['key'] == 'missing-command-observation')
        request_digest = digest({k: v for k, v in problem.items() if k not in ('status', 'resolution', 'resolved_at')})
        head = git(self.candidate, 'rev-parse', 'HEAD')
        observation = dict(problem_key=problem['key'], request_digest=request_digest,
            acceptance_case_ids=['add-positive'], input_head=head,
            source_manifest_digest=digest(source['files']), cwd=str(self.candidate),
            before_head=head, after_head=head, argv=[sys.executable, '-B', '-c', 'print(3)'],
            returncode=returncode, timed_out=timed_out, stdout='3\n', stderr='')
        # A successful command really executes; negative result envelopes are explicit
        # synthetic inputs testing intake semantics, not claims of a successful gate.
        if returncode == 0 and not timed_out:
            result = run_process(observation['argv'], cwd=self.candidate)
            self.case.assertEqual(result.returncode, 0)
            observation.update(stdout=result.stdout, stderr=result.stderr)
        return self.envelope('tests_ready', self.evidence('tester', dict(result='tests_ready',
            source_head=source['head'], files=source['files'], observations=[observation])))

    def disposition(self, observation, artifact_digest, accepted=True):
        return dict(problem_key=observation['problem_key'], request_digest=observation['request_digest'],
                    artifact_digest=artifact_digest, accepted=accepted, reason='Reviewed the exact command facts.')


def review_fixture(case, kind='native', *, early_review=False):
    f = CliScenario(case, kind=kind, early_review=early_review)
    f.ready_review()
    return f


def parallel_scenarios(parent, values, exercise):
    """Overlap independent CLI scenarios, without running another test runner.

    Each scenario retains its own repository, ledger, worktrees, runtime directory
    and cleanup stack. Workers raise ordinary assertion exceptions; the original
    unittest method reports every future in a labelled subTest on its own thread.
    No runtime result, Git state or completed setup is shared between scenarios.
    """
    def run(value):
        assertions = unittest.TestCase()
        try:
            exercise(assertions, value)
        finally:
            if not assertions.doCleanups():
                raise AssertionError(f'scenario cleanup failed: {value!r}')

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [(value, pool.submit(run, value)) for value in values]
        for value, future in futures:
            with parent.subTest(scenario=value):
                future.result()


class LocalCommandObservationsTest(unittest.TestCase):
    def fixture(self, kind='native'):
        return review_fixture(self, kind)

    def test_legacy_problem_keeps_source_correction_route(self):
        f = self.fixture()
        f.request(legacy=True)
        self.assertEqual(f.next()['action'], 'tester_fix')

    def test_observation_loop_replay_and_gate_retention(self):
        def exercise(case, value):
            kind = value
            f = review_fixture(case, kind)
            f.request()
            before = f.ledger()
            action = f.begin('tester', 'tester_observe')
            result = f.observation_result()
            f.complete(action, result)
            # Complete and apply are separate processes: resume persisted artifact.
            f.apply(action)
            first = f.ledger()
            f.apply(action)
            case.assertEqual(f.ledger(), first)
            case.assertEqual(set(first), set(before), 'observation intake added a top-level state cache')
            case.assertEqual(first['facets']['execution']['tester_source'], before['facets']['execution']['tester_source'])
            case.assertEqual(first['facets']['execution']['candidate_head'], before['facets']['execution']['candidate_head'])
            for gate in ('machine', 'blackbox'):
                case.assertEqual(first['evidence'][gate], before['evidence'][gate])
            case.assertEqual(next(p for p in first['problems'] if p['key'] == 'missing-command-observation')['status'], 'open')
            followup = f.begin('reviewer', 'reviewer_final')
            case.assertEqual(first['facets']['execution']['agents'], before['facets']['execution']['agents'])
            projection = followup['context_projection']['role_observations']
            case.assertTrue(projection['local_commands'])
            case.assertTrue(projection['observation_requests'])
            observation = result['evidence_report']['details']['observations'][0]
            report = f.evidence('reviewer', dict(result='pass', reviewed_head=first['facets']['execution']['candidate_head'],
                observation_dispositions=[f.disposition(observation, digest(result))]))
            f.submit(followup, f.envelope('pass', report))
            case.assertFalse(any(p['key'] == observation['problem_key'] and p['status'] == 'open' for p in f.ledger()['problems']))
            case.assertNotEqual(f.next()['status'], 'NEEDS_USER')
            case.assertEqual(git(f.candidate, 'status', '--porcelain'), '')
        parallel_scenarios(self, ('native', 'full_driver_skill'), exercise)

    def accepted_observation(self, case, *, correction=None):
        f = review_fixture(case, early_review=correction is None)
        f.request()
        action = f.begin('tester', 'tester_observe')
        result = f.observation_result()
        f.complete(action, result)
        artifact = Path(f.ledger()['dispatch_intent']['result_path'])
        f.apply(action)
        review = f.begin('reviewer', 'reviewer_preflight' if f.early_review else 'reviewer_final')
        observation = result['evidence_report']['details']['observations'][0]
        details = dict(result='findings' if correction else 'pass',
                       reviewed_head=git(f.candidate, 'rev-parse', 'HEAD'),
                       observation_dispositions=[f.disposition(observation, digest(result))])
        problems = None if correction is None else dict(schema_version=1, problems=[dict(
            key='independent-correction', owner=correction, summary='Revise fixture input',
            details='An independent finding requires a source correction.')])
        f.submit(review, f.envelope(details['result'],
            f.evidence('reviewer_preflight' if f.early_review else 'reviewer',
                       details, 'fail' if correction else 'pass'), problems))
        case.assertEqual(next(p for p in f.ledger()['problems']
                              if p['key'] == observation['problem_key'])['status'], 'resolved')
        return f, result, artifact

    def test_accepted_artifact_invalidates_later_review_without_dispositions(self):
        def exercise(case, mutation):
            f, result, artifact = self.accepted_observation(case)
            f.verify_gates()
            review = f.begin('reviewer', 'reviewer_final')
            report = f.evidence('reviewer', dict(result='pass',
                reviewed_head=git(f.candidate, 'rev-parse', 'HEAD')))
            f.submit(review, f.envelope('pass', report))
            recorded = f.call('status')
            case.assertEqual(recorded['readiness']['states']['reviewer'], 'pass')
            original = artifact.read_bytes()
            if mutation == 'missing':
                artifact.unlink()
            else:
                changed = json.loads(original)
                changed['evidence_report']['details']['observations'][0]['stdout'] = 'tampered\n'
                artifact.write_text(json.dumps(changed))
            # The previously recorded gate must become unusable, not just reject
            # a newly submitted report that repeats dispositions.
            state = f.call('status')['readiness']['states']['reviewer']
            case.assertNotEqual(state, 'pass')
            next_action = f.next()
            case.assertNotEqual(next_action['action'], 'finalize')
            case.assertIn(next_action['action'], ('reviewer_preflight', 'reviewer_final'))
            review_kind = 'reviewer_preflight' if next_action['action'] == 'reviewer_preflight' else 'reviewer'
            retry_report = f.evidence(review_kind, report['details'])
            rc, rejected = f.raw('record-evidence', '--kind', review_kind,
                                 '--action-id', next_action['action_id'], '--report', f.json_file(retry_report))
            case.assertNotEqual(rc, 0, rejected)
            case.assertEqual(rejected['code'], 'OBSERVATION_DISPOSITION_INVALID')
            artifact.write_bytes(original)
            restored = f.call('status')
            case.assertEqual(restored['readiness']['states']['reviewer'], 'pass')
        parallel_scenarios(self, ('missing', 'changed'), exercise)

    def test_accepted_request_recovers_after_candidate_or_source_correction(self):
        def exercise(case, role):
            f, old_result, artifact = self.accepted_observation(case, correction=role)
            history = f.ledger()
            original_artifact = artifact.read_bytes()
            old_observation = old_result['evidence_report']['details']['observations'][0]
            action = f.begin(role, role + '_fix')
            if role == 'builder':
                with (f.candidate / 'src/calc.py').open('a') as stream:
                    stream.write('\n# Independently requested correction\n')
                commit_all(f.candidate, 'correct fixture implementation')
                result = f.envelope('implemented')
            else:
                source = history['facets']['execution']['tester_source']
                worktree = Path(source['worktree'])
                path = 'tests/test_extra.py'
                with (worktree / path).open('a') as stream:
                    stream.write('\ndef test_positive():\n    assert add(1,2) == 3\n')
                head = commit_all(worktree, 'extend independent fixture assertions')
                result = f.envelope('tests_ready', f.evidence('tester', dict(
                    result='tests_ready', source_head=head,
                    files=[dict(path=path, blob=git(worktree, 'rev-parse', head + ':' + path))])))
            f.submit(action, result)
            refreshed = f.ledger()
            f.apply(action)
            case.assertEqual(f.ledger(), refreshed, 'replay duplicated the request refresh')
            request = next(p for p in refreshed['problems'] if p['key'] == old_observation['problem_key'])
            case.assertEqual(request['status'], 'open')
            case.assertEqual(artifact.read_bytes(), original_artifact)
            # Historical acceptance receipts remain append-only persisted facts.
            case.assertEqual(refreshed['events'][:len(history['events'])], history['events'])
            case.assertEqual(refreshed['facets']['execution']['agents'], history['facets']['execution']['agents'])
            fresh_result = f.observation_result()
            fresh_observation = fresh_result['evidence_report']['details']['observations'][0]
            case.assertNotEqual(fresh_observation['request_digest'], old_observation['request_digest'])
            case.assertNotEqual(fresh_result['evidence_report']['candidate_head'], old_result['evidence_report']['candidate_head'])
            rc, rejected = f.raw('record-evidence', '--kind', 'tester',
                                 '--report', f.json_file(old_result['evidence_report']))
            case.assertNotEqual(rc, 0, rejected)
            case.assertNotIn(rejected['code'], ('DRIVER_RUNTIME_OWNER_MISMATCH', 'DRIVER_ACTION_STALE'))
            observe = f.begin('tester', 'tester_observe')
            f.submit(observe, fresh_result)
            case.assertEqual(f.next()['action'], 'scan_doc_references')
            f.call('scan-doc-references', '--driver-runtime-kind', f.kind)
            review_action = f.next()
            case.assertIn(review_action['action'], ('reviewer_preflight', 'reviewer_final'))
            case.assertEqual(review_action['agent'], history['facets']['execution']['agents']['reviewer'])
            followup = f.begin('reviewer', review_action['action'])
            review_kind = 'reviewer_preflight' if followup['action'] == 'reviewer_preflight' else 'reviewer'
            stale_review = f.evidence(review_kind, dict(result='findings',
                reviewed_head=git(f.candidate, 'rev-parse', 'HEAD'),
                observation_dispositions=[f.disposition(old_observation, digest(old_result))]), 'fail')
            rc, rejected = f.raw('record-evidence', '--kind', review_kind,
                                 '--report', f.json_file(stale_review))
            case.assertNotEqual(rc, 0, rejected)
            case.assertEqual(rejected['code'], 'OBSERVATION_DISPOSITION_INVALID')
            report = f.evidence(review_kind, dict(result='pass',
                reviewed_head=git(f.candidate, 'rev-parse', 'HEAD'),
                observation_dispositions=[f.disposition(fresh_observation, digest(fresh_result))]))
            f.submit(followup, f.envelope('pass', report))
            case.assertEqual(next(p for p in f.ledger()['problems']
                if p['key'] == old_observation['problem_key'])['status'], 'resolved')
            case.assertEqual(f.next()['action'], 'verify_machine')
            f.verify_gates()
            final_review = f.begin('reviewer', 'reviewer_final')
            later = f.evidence('reviewer', dict(result='pass', reviewed_head=git(f.candidate, 'rev-parse', 'HEAD')))
            f.submit(final_review, f.envelope('pass', later))
            case.assertEqual(f.call('status')['readiness']['states']['reviewer'], 'pass')
            case.assertNotEqual(f.next()['status'], 'NEEDS_USER')
            case.assertEqual(artifact.read_bytes(), original_artifact)
        parallel_scenarios(self, ('builder', 'tester'), exercise)

    def test_nonzero_and_timeout_do_not_close_request_or_produce_proof(self):
        def exercise(case, value):
            code, timeout = value
            f = review_fixture(case)
            f.request()
            action = f.begin('tester', 'tester_observe')
            f.submit(action, f.observation_result(returncode=code, timed_out=timeout))
            ledger = f.ledger()
            case.assertNotIn('proof', ledger['evidence'])
            case.assertTrue(any(p['status'] == 'open' for p in ledger['problems']))
            case.assertEqual(f.next()['action'], 'reviewer_final')
        parallel_scenarios(self, ((7, False), (-9, True)), exercise)

    def test_rejects_unbound_observation_inputs(self):
        mutations = {
            'request_digest': '0' * 64, 'source_manifest_digest': '0' * 64,
            'input_head': '0' * 40, 'before_head': '0' * 40, 'after_head': '0' * 40,
            'cwd': '/tmp', 'acceptance_case_ids': ['unknown-case'],
            'problem_key': 'unknown-request', 'stdout': None, 'returncode': True,
        }
        def exercise(case, value):
            field, value = value
            f = review_fixture(case)
            f.request()
            action = f.begin('tester', 'tester_observe')
            result = f.observation_result()
            result['evidence_report']['details']['observations'][0][field] = value
            # Semantic intake can reject at completion or application, but never accept.
            rc, _ = f.raw('complete-dispatch', '--action-id', action['action_id'], '--result', f.json_file(result))
            if rc == 0:
                rc, _ = f.raw('apply-dispatch-result', '--action-id', action['action_id'], '--driver-runtime-kind', f.kind)
            case.assertNotEqual(rc, 0, field)
            case.assertTrue(any(p['status'] == 'open' for p in f.ledger()['problems']))
            case.assertNotIn('reviewer', f.ledger()['evidence'])
        parallel_scenarios(self, mutations.items(), exercise)

    def test_wrong_producer_and_source_writes_rejected(self):
        def exercise(case, value):
            mutation = value
            f = review_fixture(case)
            f.request()
            action = f.begin('tester', 'tester_observe')
            result = f.observation_result()
            if mutation == 'thread':
                result['evidence_report']['producer']['thread_id'] = 'other-thread'
            elif mutation.endswith('dirty'):
                path = f.candidate if mutation == 'candidate-dirty' else Path(f.ledger()['facets']['execution']['tester_source']['worktree'])
                (path / 'unreviewed.txt').write_text('unreviewed')
            elif mutation.endswith('commit'):
                path = f.candidate if mutation == 'candidate-commit' else Path(f.ledger()['facets']['execution']['tester_source']['worktree'])
                target = 'src/calc.py' if mutation == 'candidate-commit' else 'tests/test_extra.py'
                with (path / target).open('a') as stream:
                    stream.write('\n# Changed input\n')
                commit_all(path, 'advance input after observation')
            elif mutation == 'attachment':
                result['evidence_report']['details']['observations'][0]['attachment_path'] = '/tmp/log'
            else:
                del result['evidence_report']['details']['observations'][0]['stderr']
            rc, _ = f.raw('complete-dispatch', '--action-id', action['action_id'], '--result', f.json_file(result))
            if rc == 0:
                rc, _ = f.raw('apply-dispatch-result', '--action-id', action['action_id'], '--driver-runtime-kind', f.kind)
            case.assertNotEqual(rc, 0, mutation)
        parallel_scenarios(self, ('thread', 'candidate-dirty', 'source-dirty', 'candidate-commit', 'source-commit', 'attachment', 'missing-log'), exercise)

    def test_only_reviewer_can_request_existing_acceptance(self):
        def exercise(case, value):
            owner, role, cases = value
            f = review_fixture(case)
            report = dict(schema_version=1, problems=[dict(key='request-invalid', owner=owner,
                summary='Invalid request', details='Reject invalid request provenance or cases.',
                observation_request=dict(acceptance_case_ids=cases, question='Missing fact?'))])
            rc, _ = f.raw('record-problems', '--report', f.json_file(report), '--role', role,
                          '--agent-id', role, '--thread-id', role + '-thread', '--driver-runtime-kind', f.kind)
            case.assertNotEqual(rc, 0)
        parallel_scenarios(self, (('tester', 'tester', ['add-positive']),
                                    ('builder', 'reviewer', ['add-positive']),
                                    ('tester', 'reviewer', ['not-frozen'])), exercise)

    def test_review_cannot_close_request_without_matching_explicit_acceptance(self):
        def exercise(case, value):
            disposition = value
            f = review_fixture(case)
            f.request()
            action = f.begin('tester', 'tester_observe')
            result = f.observation_result()
            f.submit(action, result)
            review = f.begin('reviewer', 'reviewer_final')
            details = dict(result='pass', reviewed_head=git(f.candidate, 'rev-parse', 'HEAD'))
            if disposition != 'absent':
                item = f.disposition(result['evidence_report']['details']['observations'][0], digest(result),
                                     accepted=disposition != 'rejected')
                if disposition == 'wrong-artifact':
                    item['artifact_digest'] = '0' * 64
                if disposition == 'wrong-request':
                    item['request_digest'] = '0' * 64
                details['observation_dispositions'] = [item]
            envelope = f.envelope('pass', f.evidence('reviewer', details))
            rc, _ = f.raw('complete-dispatch', '--action-id', review['action_id'], '--result', f.json_file(envelope))
            if rc == 0:
                f.raw('apply-dispatch-result', '--action-id', review['action_id'], '--driver-runtime-kind', f.kind)
            case.assertTrue(any(p['key'] == 'missing-command-observation' and p['status'] == 'open'
                                for p in f.ledger()['problems']))
            state = f.call('status')['readiness']['states']['reviewer']
            case.assertNotEqual(state, 'pass')
            case.assertNotEqual(f.next()['action'], 'finalize')
        parallel_scenarios(self, ('absent', 'rejected', 'wrong-artifact', 'wrong-request'), exercise)

    def test_record_evidence_cannot_bypass_dispatch_artifact(self):
        f = self.fixture()
        f.request()
        report = f.observation_result()['evidence_report']
        before = f.ledger()
        rc, _ = f.raw('record-evidence', '--kind', 'tester', '--report', f.json_file(report),
                      '--driver-runtime-kind', f.kind)
        self.assertNotEqual(rc, 0)
        self.assertEqual(f.ledger(), before)

    def test_registered_source_worktree_is_a_valid_observation_input(self):
        f = self.fixture()
        f.request()
        action = f.begin('tester', 'tester_observe')
        result = f.observation_result()
        source = f.ledger()['facets']['execution']['tester_source']
        item = result['evidence_report']['details']['observations'][0]
        item.update(cwd=source['worktree'], input_head=source['head'],
                    before_head=source['head'], after_head=source['head'])
        observed = run_process(item['argv'], cwd=source['worktree'])
        self.assertEqual(observed.returncode, 0)
        item.update(stdout=observed.stdout, stderr=observed.stderr)
        f.submit(action, result)
        self.assertEqual(f.next()['action'], 'reviewer_final')

    def test_identical_rejected_observation_cannot_cycle_without_bound(self):
        f = self.fixture()
        f.request()
        result = f.observation_result()
        observation = result['evidence_report']['details']['observations'][0]
        limit = f.contract['execution']['progress_policy']['attempt_limit']
        stopped = False
        for _ in range(limit + 1):
            next_action = f.next()
            if next_action['status'] == 'NEEDS_USER':
                stopped = True
                break
            action = f.begin('tester', 'tester_observe')
            rc, _ = f.raw('complete-dispatch', '--action-id', action['action_id'], '--result', f.json_file(result))
            if rc == 0:
                rc, _ = f.raw('apply-dispatch-result', '--action-id', action['action_id'], '--driver-runtime-kind', f.kind)
            if rc != 0:
                stopped = True
                break
            review = f.begin('reviewer', 'reviewer_final')
            details = dict(result='findings', reviewed_head=git(f.candidate, 'rev-parse', 'HEAD'),
                           observation_dispositions=[f.disposition(observation, digest(result), False)])
            f.submit(review, f.envelope('findings', f.evidence('reviewer', details, 'fail')))
        self.assertTrue(stopped, 'identical rejected facts created an unbounded observation loop')
        self.assertTrue(any(p['status'] == 'open' for p in f.ledger()['problems']))

    def test_unknown_builder_loop_problem_is_not_observation_permission(self):
        f = self.fixture()
        report = dict(schema_version=1, problems=[dict(key='unknown-fixture-condition',
            owner='builder_loop', summary='Unknown condition',
            details='Missing observations are mentioned but this is not a structured request.')])
        f.call('record-problems', '--report', f.json_file(report), '--role', 'reviewer',
               '--agent-id', 'reviewer', '--thread-id', 'reviewer-thread', '--driver-runtime-kind', f.kind)
        self.assertEqual(f.next()['status'], 'NEEDS_USER')
        self.assertEqual(f.next()['action'], 'builder_loop_problem_decision')

    def test_missing_or_changed_immutable_logs_cannot_be_applied(self):
        def exercise(case, value):
            mutation = value
            f = review_fixture(case)
            f.request()
            action = f.begin('tester', 'tester_observe')
            result = f.observation_result()
            f.complete(action, result)
            artifact = Path(f.ledger()['dispatch_intent']['result_path'])
            case.assertTrue(artifact.is_file())
            if mutation == 'missing':
                artifact.unlink()
            else:
                value = json.loads(artifact.read_text())
                value['evidence_report']['details']['observations'][0]['stdout'] = 'changed after acceptance\n'
                artifact.write_text(json.dumps(value))
            rc, _ = f.raw('apply-dispatch-result', '--action-id', action['action_id'], '--driver-runtime-kind', f.kind)
            case.assertNotEqual(rc, 0)
            case.assertTrue(any(p['status'] == 'open' for p in f.ledger()['problems']))
        parallel_scenarios(self, ('missing', 'changed'), exercise)
