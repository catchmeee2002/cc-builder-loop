"""Same public CLI scenario against an immutable baseline and tested checkout."""
from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from harness import ROOT, git, run_process
from tests.test_local_command_observations import CliScenario, digest

BASELINE = 'f536b63ceb027614072342341858878aae8b9a20'


def advance_a(case, runtime):
    f = CliScenario(case, runtime, prerequisites=True)
    action = f.build('src/public_a.py', 'VALUE = "A"\n')
    ledger = f.ledger()
    progress = f.next()['work_unit_progress']
    facts = dict(input_runtime=str(runtime), contract_digest=digest(f.contract),
                 fixture_start_head=git(f.repo, 'rev-parse', 'HEAD'),
                 candidate_head=git(f.candidate, 'rev-parse', 'HEAD'),
                 completed=progress['completed'], pending=progress['pending'],
                 publication=ledger['publication'],
                 problems=ledger['problems'], action_id=action['action_id'])
    return f, facts


class PrerequisiteBaselineComparisonTest(unittest.TestCase):
    def test_immutable_baseline_fails_a_completion_and_candidate_publishes_after_b(self):
        self.assertEqual(git(ROOT, 'rev-parse', BASELINE + '^{commit}'), BASELINE)
        with tempfile.TemporaryDirectory(prefix='prerequisite-baseline-') as temporary:
            destination = Path(temporary)
            archive = destination / 'baseline.tar'
            result = run_process(['git', '-C', ROOT, 'archive', '--format=tar',
                                  '--output', archive, BASELINE])
            self.assertEqual(result.returncode, 0, result.stderr)
            runtime = destination / 'runtime'
            runtime.mkdir()
            with tarfile.open(archive) as bundle:
                for member in bundle.getmembers():
                    target = runtime / member.name
                    self.assertTrue(target.resolve().is_relative_to(runtime.resolve()))
                    self.assertTrue(member.isfile() or member.isdir(), member.name)
                bundle.extractall(runtime)
            _, baseline = advance_a(self, runtime)
            # The expected discriminator is precisely A's completion with B absent.
            # Parsing/startup/application errors above fail, rather than qualifying red.
            self.assertNotIn('produce-a', baseline['completed'], baseline)
            self.assertEqual(len(baseline['problems']), 1, baseline)
            problem = baseline['problems'][0]
            self.assertEqual(problem['key'], 'public-prerequisite-classification-invalid')
            detail = json.loads(problem['details'])
            self.assertEqual(detail['code'], 'PUBLIC_PREREQUISITE_CLASSIFICATION_INVALID')
            self.assertEqual(detail['paths'], ['src/public_b.py'])
            self.assertEqual(detail['classification'], [
                dict(path='src/public_a.py', source='builder', status='ready'),
                dict(path='src/public_b.py', source='builder', status='deferred')])
            self.assertIsNone(baseline['publication']['head'])
            print(json.dumps(dict(comparison='bounded-prerequisites', side='baseline',
                runtime_head=BASELINE, assertion='produce-a completed before produce-b',
                assertion_passed=False, facts=baseline), sort_keys=True), flush=True)

        f, candidate = advance_a(self, ROOT)
        self.assertEqual(candidate['contract_digest'], baseline['contract_digest'])
        print(json.dumps(dict(comparison='bounded-prerequisites', side='candidate',
            runtime_head=git(ROOT, 'rev-parse', 'HEAD'),
            assertion='produce-a completed before produce-b',
            assertion_passed='produce-a' in candidate['completed'], facts=candidate), sort_keys=True), flush=True)
        self.assertIn('produce-a', candidate['completed'], candidate)
        self.assertNotIn('produce-b', candidate['completed'])
        self.assertIsNone(candidate['publication']['head'])
        self.assertEqual(candidate['publication']['files'], [])
        self.assertFalse(any(p['status'] == 'open' for p in candidate['problems']), candidate)
        f.build('src/public_b.py', 'VALUE = "B"\n')
        f.call('scan-doc-references', '--driver-runtime-kind', f.kind)
        self.assertEqual(f.next()['action'], 'publish_prerequisites')
        f.call('publish-prerequisites', '--driver-runtime-kind', f.kind)
        publication = f.ledger()['publication']
        expected = [dict(path=p, blob=git(f.candidate, 'rev-parse', 'HEAD:' + p))
                    for p in ['src/public_a.py', 'src/public_b.py']]
        self.assertEqual(publication['files'], expected)
        for item in expected:
            self.assertEqual(git(f.repo, 'rev-parse', publication['head'] + ':' + item['path']), item['blob'])
        print(json.dumps(dict(comparison='bounded-prerequisites', side='candidate',
            runtime_head=git(ROOT, 'rev-parse', 'HEAD'), assertion='A then B publishes exact blobs',
            assertion_passed=True, publication=publication), sort_keys=True), flush=True)
