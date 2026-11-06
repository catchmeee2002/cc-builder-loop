"""Real CLI/Git closure for Reviewer feedback; no live model claims."""
import copy
import json
import unittest
from pathlib import Path
from harness import commit_all, git
from tests.test_local_command_observations import review_fixture
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.role_input import build_role_input, project_role_input


class TesterFeedbackTest(unittest.TestCase):
    def test_integrated_correction_receives_review_without_reference_roundtrip(self):
        f = review_fixture(self, early_review=True, collaborative=True)
        review = f.begin('reviewer', 'reviewer_preflight')
        finding = dict(key='missing-tool', owner='tester', summary='Missing tool',
                       details='The test tool whitelist omits cmp required by the declared command.')
        f.submit(review, f.envelope('findings', problems=dict(schema_version=1, problems=[
            finding, dict(key='implementation-fix', owner='builder',
                          summary='Fix implementation', details='Update implementation')])) )
        repair = f.begin('builder', 'builder_fix')
        path = f.candidate / 'src/calc.py'
        path.write_text(path.read_text() + '\n# implementation corrected\n')
        commit_all(f.candidate, 'repair implementation')
        f.submit(repair, f.envelope('implemented'))
        action = f.next()
        self.assertEqual(action['action'], 'tester_fix')
        self.assertEqual(action['agent']['thread_id'], 'tester-thread')
        payload = build_role_input(core.driver_context(f.repo, f.run), action, 'tester')
        self.assertEqual(payload['candidate_worktree'], str(f.candidate))
        received = next(p for p in payload['problems'] if p['key'] == finding['key'])
        self.assertEqual(received['details'], finding['details'])
        self.assertEqual(received['producer']['role'], 'reviewer')
        self.assertEqual(payload['evidence_gates']['preflight']['state'], 'stale')
        source = f.ledger()['facets']['execution']['tester_source']
        action = f.begin('tester', 'tester_fix')
        test_path = source['files'][0]['path']
        (Path(source['worktree']) / test_path).write_text(
            'from src.calc import add\ndef test_positive():\n    assert add(1,2) == 3\n')
        head = commit_all(Path(source['worktree']), 'repair independent test')
        report = f.evidence('tester', dict(result='tests_ready', source_head=head,
            files=[dict(path=test_path, blob=git(Path(source['worktree']), 'rev-parse', head+':'+test_path))]))
        f.submit(action, f.envelope('tests_ready', report))
        observed = []
        for _ in range(4):
            next_action = f.next()
            if next_action['action'] not in {'scan_doc_references', 'verify_preflight'}:
                break
            observed.append(next_action['action'])
            f.call(next_action['action'].replace('_', '-'), '--driver-runtime-kind', f.kind)
        self.assertEqual(observed, ['scan_doc_references', 'verify_preflight'])
        review = f.begin('reviewer', 'reviewer_preflight')
        head = f.ledger()['facets']['execution']['candidate_head']
        f.submit(review, f.envelope('pass', f.evidence('reviewer_preflight', dict(result='pass', reviewed_head=head))))
        self.assertEqual(f.next()['action'], 'verify_machine')
        f.verify_gates()
        review = f.begin('reviewer', 'reviewer_final')
        f.submit(review, f.envelope('pass', f.evidence('reviewer', dict(result='pass', reviewed_head=head))))
        self.assertEqual(f.next()['action'], 'finalize')

    def test_original_reviewer_supplements_then_original_tester_repairs(self):
        self.repair_loop('native')

    def test_skill_adapter_uses_same_feedback_and_completion_path(self):
        self.repair_loop('full_driver_skill')

    def repair_loop(self, kind):
        f = review_fixture(self, kind, early_review=True)
        if kind == 'native':
            original = f.request(legacy=True)
        else:
            original=dict(key='missing-command-observation',owner='tester',summary='Check own test',details='Original finding')
            review=f.begin('reviewer','reviewer_preflight')
            f.submit(review,f.envelope('findings',problems=dict(schema_version=1,problems=[original,
                dict(key='builder-comment',owner='builder',summary='Fix comment',details='Implementation documentation')])) )
            repair=f.begin('builder','builder_fix')
            path=f.candidate/'src/calc.py';path.write_text(path.read_text()+'\n# documented implementation\n')
            commit_all(f.candidate,'repair implementation comment')
            f.submit(repair,f.envelope('implemented'))
        followup = f.next()
        refresh_actions = []
        while followup['action'] in {'scan_doc_references', 'verify_preflight'}:
            self.assertLess(len(refresh_actions), 2)
            refresh_actions.append(followup['action'])
            f.call(followup['action'].replace('_', '-'), '--driver-runtime-kind', f.kind)
            followup = f.next()
        if kind == 'full_driver_skill':
            self.assertEqual(refresh_actions, ['scan_doc_references', 'verify_preflight'])
        self.assertEqual(followup['action'], 'reviewer_preflight')
        self.assertEqual(followup['reason'], 'tester_recheck_references_required')
        source = f.ledger()['facets']['execution']['tester_source']
        request = dict(source_head=source['head'], files=[{k:v for k,v in item.items() if k in ('path','blob')} for item in source['files']],
                       acceptance_case_ids=['add-positive'])
        supplemented = {**original, 'details': 'SECRET CANDIDATE IMPLEMENTATION', 'tester_recheck': request}
        action = f.begin('reviewer', 'reviewer_preflight')
        f.submit(action, f.envelope('findings', problems=dict(schema_version=1, problems=[supplemented])))
        next_action = f.next()
        self.assertEqual(next_action['action'], 'tester_fix')
        context = core.driver_context(f.repo, f.run)
        payload = build_role_input(context, next_action, 'tester')
        self.assertNotIn('SECRET', json.dumps(payload))
        self.assertEqual(payload['tester_rechecks'], [request])
        self.assertEqual(project_role_input(payload)['tester_rechecks'], [request])
        # Persisted dispatch replay must not duplicate the supplementation.
        f.apply(action)
        self.assertEqual(f.ledger()['problems'][0]['details'], original['details'])
        self.assertEqual(len([e for e in f.ledger()['events'] if e['kind']=='problem_report_updated']), 1)
        self.assertEqual(sum(p['key']==original['key'] for p in f.ledger()['problems']),1)
        action = f.begin('tester', 'tester_fix')
        worktree = Path(source['worktree']); path = source['files'][0]['path']
        (worktree/path).write_text('from src.calc import add\ndef test_positive():\n    assert add(1,2) == 3\n')
        head = commit_all(worktree, 'correct independent test')
        report = f.evidence('tester', dict(result='tests_ready', source_head=head,
                            files=[dict(path=path, blob=git(worktree,'rev-parse',head+':'+path))]))
        f.submit(action, f.envelope('tests_ready', report))
        self.assertEqual(f.ledger()['problems'][0]['status'], 'resolved')
        self.assertEqual(f.ledger()['facets']['execution']['agents']['tester']['thread_id'], 'tester-thread')
        f.call('scan-doc-references', '--driver-runtime-kind', f.kind)
        f.call('verify-preflight', '--driver-runtime-kind', f.kind)
        self.assertEqual(f.next()['action'], 'reviewer_preflight')
        review=f.begin('reviewer','reviewer_preflight')
        head=f.ledger()['facets']['execution']['candidate_head']
        f.submit(review,f.envelope('pass',f.evidence('reviewer_preflight',dict(result='pass',reviewed_head=head))))
        f.verify_gates()
        review=f.begin('reviewer','reviewer_final')
        f.submit(review,f.envelope('pass',f.evidence('reviewer',dict(result='pass',reviewed_head=head))))
        self.assertEqual(f.next()['action'],'finalize')

    def test_stale_or_candidate_reference_is_rejected_at_intake(self):
        f = review_fixture(self, early_review=True)
        source = f.ledger()['facets']['execution']['tester_source']
        action = f.begin('reviewer', 'reviewer_preflight')
        problem = dict(key='bad-reference', owner='tester', summary='Check test', details='Private prose',
                       tester_recheck=dict(source_head=source['head'], files=[dict(path='src/calc.py',blob='a'*40)],
                                           acceptance_case_ids=['add-positive']))
        result=f.envelope('findings',problems=dict(schema_version=1,problems=[problem]))
        rc, data = f.raw('complete-dispatch','--action-id',action['action_id'],'--result',f.json_file(result))
        self.assertNotEqual(rc,0,data)
        self.assertEqual(data['code'], 'TESTER_RECHECK_INVALID')
        self.assertEqual(f.ledger()['problems'], [])

    def test_original_reviewer_withdraws_mistake_without_tester_attempt(self):
        f = review_fixture(self, early_review=True)
        f.request(legacy=True)
        action = f.next()
        context = core.driver_context(f.repo, f.run)
        payload = build_role_input(context, action, 'reviewer')
        request = payload['tester_recheck_request']
        source = f.ledger()['facets']['execution']['tester_source']
        action = f.begin('reviewer', 'reviewer_preflight')
        candidate = f.ledger()['facets']['execution']['candidate_head']
        report = f.evidence('reviewer_preflight',dict(result='pass',reviewed_head=candidate,
            tester_finding_withdrawals=[dict(problem_identity=request['problem_identity'],source_head=source['head'],
                                             reason='Frozen contract permits the existing assertion.')]))
        f.submit(action,f.envelope('pass',report))
        f.apply(action)  # Completed artifact replay must be idempotent.
        ledger=f.ledger()
        self.assertEqual(ledger['problems'][0]['status'],'resolved')
        self.assertEqual(sum(e['kind']=='tester_finding_withdrawn' for e in ledger['events']),1)
        self.assertFalse(any(e['kind']=='dispatch_prepared' and e['details']['action']=='tester_fix' for e in ledger['events']))

    def test_recovery_preserves_facts_and_rejects_stale_binding(self):
        f = review_fixture(self, early_review=True)
        problem=f.request(legacy=True)
        before=f.ledger();action=f.next()
        args=['--session-id','observation-session','--action-id',action['action_id'],
              '--problem-key',problem['key'],'--reason','Continue source-bound feedback',
              '--driver-runtime-kind',f.kind]
        f.call('recover-tester-feedback',*args)
        f.call('recover-tester-feedback',*args)
        after=f.ledger()
        for key in ('facets','problems','evidence'):
            self.assertEqual(before[key],after[key])
        self.assertEqual(sum(e['kind']=='tester_feedback_recovery_requested' for e in after['events']),1)
        bad=list(args);bad[3]='0'*64
        rc,data=f.raw('recover-tester-feedback',*bad)
        self.assertNotEqual(rc,0)
        self.assertEqual(data['code'],'TESTER_FEEDBACK_STALE')
        source=after['facets']['execution']['tester_source']
        (Path(source['worktree'])/'unrelated.txt').write_text('external drift')
        bad=list(args);bad[3]=f.next()['action_id']
        before_drift=f.ledger()
        rc,data=f.raw('recover-tester-feedback',*bad)
        self.assertNotEqual(rc,0)
        self.assertEqual(data['code'],'TESTER_FEEDBACK_INPUT_DRIFT')
        self.assertEqual(before_drift,f.ledger())

    def test_projection_rejects_each_unbound_reference_and_foreign_producer(self):
        from tests.test_role_input_boundary import RoleInputBoundaryTest
        from runtime.codex_builder_loop.assurance_v4.role_input import RoleInputError
        f=RoleInputBoundaryTest();f.setUp()
        facets=f.context['facets'];source=facets['execution']['tester_source']
        facets['execution']['agents']['reviewer']={'agent_id':'reviewer','thread_id':'reviewer-thread'}
        source['files']=[{'path':'tests/public.py','blob':'4'*40}]
        case=facets['mission']['acceptance_cases'][0]['id']
        request=dict(source_head=source['head'],files=copy.deepcopy(source['files']),acceptance_case_ids=[case])
        problem=dict(key='test-defect',owner='tester',status='open',summary='SECRET',details='SECRET',
                     producer={'role':'reviewer',**facets['execution']['agents']['reviewer']},tester_recheck=request)
        f.context['problems']=[problem]
        action={**f.action,'action':'tester_fix'}
        good=build_role_input(f.context,action,'tester')
        self.assertNotIn('SECRET',json.dumps(good))
        for name in ('tester-requires-host-module-cache', 'acceptance-platform-assertion-shape', 'missing-node-cmp-dependency'):
            context=copy.deepcopy(f.context)
            context['problems'][0].update(key=name, details='SECRET ' + name)
            self.assertEqual(build_role_input(context,action,'tester'),good)
        for mutate in (
            lambda p:p['tester_recheck'].update(source_head='9'*40),
            lambda p:p['tester_recheck']['files'][0].update(blob='9'*40),
            lambda p:p['tester_recheck']['files'][0].update(path='src/private.py'),
            lambda p:p['tester_recheck'].update(acceptance_case_ids=['private-case']),
            lambda p:p['tester_recheck'].update(answer='SECRET'),
            lambda p:p['producer'].update(thread_id='another-thread'),
        ):
            context=copy.deepcopy(f.context);mutate(context['problems'][0])
            with self.assertRaises(RoleInputError):
                build_role_input(context,action,'tester')
        self.assertEqual(project_role_input(good)['tester_rechecks'],[request])

    def test_tester_disagreement_preserves_tests_and_returns_original_reviewer(self):
        self.disagreement_loop(False)

    def test_collaborative_disagreement_preserves_tests_and_returns_original_reviewer(self):
        self.disagreement_loop(True)

    def disagreement_loop(self, collaborative):
        f=review_fixture(self,early_review=True,collaborative=collaborative)
        source=f.ledger()['facets']['execution']['tester_source']
        request=dict(source_head=source['head'],files=[{k:v for k,v in i.items() if k in ('path','blob')} for i in source['files']],
                     acceptance_case_ids=['add-positive'])
        problem=dict(key='incorrect-review',owner='tester',summary='Recheck boundary',details='Original reviewer opinion',tester_recheck=request)
        if collaborative:
            problem.pop('tester_recheck')
        review=f.begin('reviewer','reviewer_preflight')
        f.submit(review,f.envelope('findings',problems=dict(schema_version=1,problems=[problem])))
        action=f.next();payload=build_role_input(core.driver_context(f.repo,f.run),action,'tester')
        digest=payload['author_result_contract']['recheck_requests'][0]['request_digest']
        action=f.begin('tester','tester_fix')
        report=f.evidence('tester',dict(result='tests_ready',source_head=source['head'],files=source['files'],
                          recheck_results=[dict(request_digest=digest,decision='disputed',reason='Existing assertions match frozen contract.')]))
        f.submit(action,f.envelope('tests_ready',report))
        self.assertEqual(source,f.ledger()['facets']['execution']['tester_source'])
        self.assertEqual(f.ledger()['problems'][0]['status'],'open')
        next_action=f.next()
        self.assertEqual(next_action['action'],'reviewer_preflight')
        self.assertEqual(next_action['reason'],'tester_recheck_disputed')
        self.assertEqual(next_action['agent']['thread_id'],'reviewer-thread')

    def test_repeated_unusable_feedback_stops_without_spending_tester_budget(self):
        f=review_fixture(self,early_review=True)
        problem=f.request(legacy=True)
        for _ in range(3):
            action=f.begin('reviewer','reviewer_preflight')
            f.submit(action,f.envelope('findings',problems=dict(schema_version=1,problems=[problem])))
        action=f.next()
        self.assertEqual(action['status'],'NEEDS_USER')
        self.assertEqual(action['reason'],'tester_recheck_no_progress')
        self.assertFalse(any(e['kind']=='dispatch_prepared' and e['details']['action']=='tester_fix' for e in f.ledger()['events']))
