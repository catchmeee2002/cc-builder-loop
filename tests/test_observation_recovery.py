"""Original Tester observation retry via public CLI and real fixture execution."""
import sys
import unittest
from unittest.mock import patch

from harness import ROOT, git, run_process
from tests.test_local_command_observations import review_fixture
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator


class ObservationRecoveryTest(unittest.TestCase):
    def blocked(self):
        f = review_fixture(self, early_review=True)
        f.request()
        action = f.begin('tester', 'tester_observe')
        request = action['request_digest']
        f.submit(action, f.envelope('blocked', problems=dict(schema_version=1, problems=[dict(
            key='fixture-unavailable', owner='builder_loop', summary='Cannot execute fixture',
            details='Execution sandbox prohibits temporary files and local server.')])) )
        self.assertEqual(f.next()['action'], 'builder_loop_problem_decision')
        return f, request

    def retry(self, f, request, **overrides):
        args = dict(session_id='observation-session', action_id=f.next()['action_id'],
                    problem_key='fixture-unavailable', request_digest=request,
                    reason='Fixture execution environment repaired', driver_runtime_kind=f.kind)
        args.update(overrides)
        argv = [v for k, value in args.items() for v in ('--'+k.replace('_', '-'), value)]
        return f.raw('retry-tester-observation', *argv)

    def test_retry_real_fixture_preserves_gates_and_original_review(self):
        f, request = self.blocked()
        before = f.ledger()
        decision = f.next()['action_id']
        self.assertEqual(self.retry(f, request)[0], 0)
        ready = f.ledger()
        self.assertEqual(self.retry(f, request, action_id=decision)[0], 0)
        self.assertEqual(ready, f.ledger())
        action = f.begin('tester', 'tester_observe')
        self.assertEqual(action['agent']['thread_id'], 'tester-thread')
        self.assertEqual(action['request_digest'], request)
        result = f.observation_result()
        observation = result['evidence_report']['details']['observations'][0]
        # Real temp writes, subprocess build artifact, loopback HTTP and a nonzero
        # test result. Executable observations are not automatically passing gates.
        code = '''import tempfile, pathlib, subprocess, sys, threading, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
with tempfile.TemporaryDirectory() as root:
 p = pathlib.Path(root)/'artifact'
 subprocess.run([sys.executable, '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).write_text("built")', str(p)], check=True)
 class Handler(BaseHTTPRequestHandler):
  def do_GET(self):
   self.send_response(200); self.end_headers(); self.wfile.write(p.read_bytes())
  def log_message(self,*args): pass
 with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
  t=threading.Thread(target=server.serve_forever);t.start()
  try:
   assert urllib.request.urlopen('http://127.0.0.1:'+str(server.server_port)).read()==b'built'
   print('fixture executed; business assertion failed')
  finally: server.shutdown();t.join()
sys.exit(2)
'''
        observation['argv'] = [sys.executable, '-B', '-c', code]
        ran = run_process(observation['argv'], cwd=f.candidate)
        self.assertEqual(ran.returncode, 2, ran.stderr)
        observation.update(returncode=ran.returncode, stdout=ran.stdout, stderr=ran.stderr)
        path = f.candidate / 'src/calc.py'
        original = path.read_bytes()
        path.write_text('unauthorized source edit')
        try:
            rc, error = f.raw('complete-dispatch', '--action-id', action['action_id'], '--result', f.json_file(result))
            self.assertNotEqual(rc, 0, error)
            self.assertEqual(next(p for p in f.ledger()['problems'] if p['key']=='fixture-unavailable')['status'], 'open')
        finally:
            path.write_bytes(original)
        f.complete(action, result)
        f.apply(action)
        after = f.ledger()
        f.apply(action)
        self.assertEqual(after, f.ledger())
        self.assertEqual(after['facets'], before['facets'])
        self.assertEqual(after['evidence'], before['evidence'])
        problems = {p['key']:p for p in after['problems']}
        self.assertEqual(problems['fixture-unavailable']['status'], 'resolved')
        self.assertEqual(problems['missing-command-observation']['status'], 'open')
        next_action = f.next()
        self.assertEqual(next_action['action'], 'reviewer_preflight')
        self.assertEqual(next_action['agent']['thread_id'], 'reviewer-thread')
        self.assertEqual(git(f.candidate, 'status', '--porcelain'), '')

    def test_rejected_owner_request_and_dirty_source_do_not_mutate(self):
        f, request = self.blocked()
        before = f.ledger()
        for overrides, expected in [({'session_id':'other'}, 'OBSERVATION_RETRY_OWNER_MISMATCH'),
                                    ({'request_digest':'e'*64}, 'OBSERVATION_RETRY_REQUEST_INVALID'),
                                    ({'action_id':'a'*64}, 'OBSERVATION_RETRY_STALE')]:
            with self.subTest(expected=expected):
                rc, value = self.retry(f, request, **overrides)
                self.assertNotEqual(rc, 0)
                self.assertEqual(value['code'], expected)
                self.assertEqual(before, f.ledger())
        path = f.candidate / 'src/calc.py'
        original = path.read_bytes()
        path.write_text('changed source')
        try:
            rc, value = self.retry(f, request)
            self.assertNotEqual(rc, 0, value)
            self.assertEqual(before, f.ledger())
        finally:
            path.write_bytes(original)

    def test_failed_retry_consumes_grant_without_resolving(self):
        f, request = self.blocked()
        self.assertEqual(self.retry(f, request)[0], 0)
        action = f.begin('tester', 'tester_observe')
        f.submit(action, f.envelope('blocked', problems=dict(schema_version=1, problems=[dict(
            key='fixture-unavailable', owner='builder_loop', summary='Still unavailable', details='Server still cannot bind.')])) )
        self.assertEqual(f.next()['action'], 'builder_loop_problem_decision')
        blocker = next(p for p in f.ledger()['problems'] if p['key']=='fixture-unavailable')
        self.assertEqual(blocker['status'], 'open')
        receipt = [e['details'] for e in f.ledger()['events'] if e['kind']=='observation_retry_completed']
        self.assertEqual(len(receipt), 1)
        self.assertFalse(receipt[0]['observed'])

    def test_observe_uses_configured_tester_permissions(self):
        coordinator = NativeCoordinator.__new__(NativeCoordinator)
        coordinator.project_root = ROOT
        self.assertEqual(coordinator._sandbox_policy({'action':'tester_observe'}, 'tester', {}),
                         {'type':'dangerFullAccess'})
        self.assertIn('源码只读', (ROOT / 'agents/tester.toml').read_text())
        for config, expected in [('workspace-write','dangerFullAccess'), ('read-only','readOnly')]:
            with self.subTest(config=config), patch.object(coordinator, '_role_config', return_value=('',config)):
                for action in ('tester_observe','tester_author','tester_blackbox'):
                    self.assertEqual(coordinator._sandbox_policy({'action':action}, 'tester', {}), {'type':expected})


if __name__ == '__main__':
    unittest.main()
