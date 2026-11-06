"""Current acceptance and historical rejection across real CLI/Git transitions."""
import copy
import unittest
from pathlib import Path

from harness import commit_all, git
from tests.test_local_command_observations import digest, review_fixture


class ObservationDispositionTest(unittest.TestCase):
    def revised(self):
        f = review_fixture(self, early_review=True)
        f.request()
        observe = f.begin('tester', 'tester_observe')
        old = f.observation_result()
        f.complete(observe, old)
        old_path = Path(f.ledger()['dispatch_intent']['result_path'])
        f.apply(observe)
        old_observation = old['evidence_report']['details']['observations'][0]
        review = f.begin('reviewer', 'reviewer_preflight')
        report = f.evidence('reviewer_preflight', dict(result='findings',
            reviewed_head=git(f.candidate, 'rev-parse', 'HEAD'),
            observation_dispositions=[f.disposition(old_observation, digest(old))]), 'fail')
        problems = dict(schema_version=1, problems=[dict(key='correct-implementation', owner='builder',
            summary='Implementation correction', details='Correct the frozen implementation behavior.')])
        f.submit(review, f.envelope('findings', report, problems))
        action = f.begin('builder', 'builder_fix')
        path = f.candidate / 'src/calc.py'
        path.write_text(path.read_text() + '\n# corrected implementation\n')
        commit_all(f.candidate, 'correct implementation')
        f.submit(action, f.envelope('implemented'))
        observe = f.begin('tester', 'tester_observe')
        current = f.observation_result()
        f.submit(observe, current)
        self.assertEqual(f.next()['action'], 'scan_doc_references')
        f.call('scan-doc-references', '--driver-runtime-kind', f.kind)
        self.assertEqual(f.next()['action'], 'verify_preflight')
        self.assertEqual(f.next()['reason'], 'review_prerequisite_preflight_refresh')
        f.call('verify-preflight', '--driver-runtime-kind', f.kind)
        review = f.begin('reviewer', 'reviewer_preflight')
        report = f.evidence('reviewer_preflight', dict(result='pass',
            reviewed_head=git(f.candidate, 'rev-parse', 'HEAD'), observation_dispositions=[
                f.disposition(current['evidence_report']['details']['observations'][0], digest(current)),
                f.disposition(old_observation, digest(old), accepted=False)]))
        return f, review, report, old_path

    def test_accept_current_and_reject_history_remains_current_through_final_review(self):
        f, review, report, old_path = self.revised()
        original = old_path.read_bytes()
        agents = f.ledger()['facets']['execution']['agents']
        f.submit(review, f.envelope('pass', report))
        after = f.ledger()
        f.apply(review)
        self.assertEqual(after, f.ledger())
        self.assertEqual(old_path.read_bytes(), original)
        self.assertEqual(after['facets']['execution']['agents'], agents)
        self.assertEqual(f.next()['action'], 'verify_machine')
        self.assertEqual(next(p for p in after['problems'] if p['key']=='missing-command-observation')['status'], 'resolved')
        receipts = [e['details'] for e in after['events'] if e['kind']=='local_observation_reviewed']
        self.assertEqual(receipts[-1]['accepted'], False)
        f.verify_gates()
        final = f.begin('reviewer', 'reviewer_final')
        changed_receipt = copy.deepcopy(report)
        changed_receipt['kind'] = 'reviewer'
        changed_receipt['details']['observation_dispositions'][0]['reason'] = 'Different replay of closed request'
        before_invalid = f.ledger()
        rc, error = f.raw('complete-dispatch', '--action-id', final['action_id'],
                         '--result', f.json_file(f.envelope('pass', changed_receipt)))
        self.assertNotEqual(rc, 0, error)
        self.assertEqual(error['code'], 'EVIDENCE_REPORT_INVALID')
        self.assertEqual(before_invalid, f.ledger())
        report = f.evidence('reviewer', dict(result='pass', reviewed_head=git(f.candidate, 'rev-parse', 'HEAD')))
        f.submit(final, f.envelope('pass', report))
        self.assertEqual(f.call('status')['readiness']['states']['reviewer'], 'pass')

    def test_invalid_dispositions_rejected_before_acceptance_with_and_without_problem_report(self):
        f, review, valid, old_path = self.revised()
        before = f.ledger()
        for mutation in ('accept_history', 'unknown_artifact', 'unknown_request', 'corrupt_artifact'):
            for with_problem in (False, True):
                with self.subTest(mutation=mutation, with_problem=with_problem):
                    report = copy.deepcopy(valid)
                    disposition = report['details']['observation_dispositions'][1]
                    original = old_path.read_bytes()
                    if mutation == 'accept_history': disposition['accepted'] = True
                    if mutation == 'unknown_artifact': disposition['artifact_digest'] = 'a'*64
                    if mutation == 'unknown_request': disposition['request_digest'] = 'b'*64
                    if mutation == 'corrupt_artifact': old_path.write_text('{}')
                    problems = None
                    outcome = 'pass'
                    if with_problem:
                        outcome = 'findings'
                        report['status'] = 'fail'
                        report['details']['result'] = 'findings'
                        problems = dict(schema_version=1, problems=[dict(key='other-finding', owner='builder_loop',
                            summary='Other finding', details='The invalid disposition still needs correction.')])
                    try:
                        rc, rejected = f.raw('complete-dispatch', '--action-id', review['action_id'],
                            '--result', f.json_file(f.envelope(outcome, report, problems)))
                        self.assertNotEqual(rc, 0, rejected)
                        self.assertEqual(rejected['code'], 'EVIDENCE_REPORT_INVALID')
                        self.assertEqual(rejected['source_code'], 'OBSERVATION_DISPOSITION_INVALID')
                        self.assertEqual(before, f.ledger())
                    finally:
                        old_path.write_bytes(original)
        f.submit(review, f.envelope('pass', valid))
        self.assertEqual(f.ledger()['phase'], 'active')


if __name__ == '__main__':
    unittest.main()
