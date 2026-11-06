"""Native host migration preserves original role/evidence identity between turns."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_review_reconsideration as fixture
from runtime.codex_builder_loop.assurance_v4 import core, host_transition
from runtime.codex_builder_loop.native_driver import cli


class HostTransitionTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ReviewReconsiderationTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.assertEqual(self.f.request()[0], 0)
        self.repo, self.run = self.f.repo, self.f.run
        ledger = self.f.ledger()
        ledger['driver_runtime']['kind'] = 'native'
        self.old = dict(requested='codex', resolved='/test/codex', runtime_version='old',
                        sha256='a'*64, protocol_schema_digest='b'*64, protocol_canary_digest='c'*64)
        self.new = {**self.old, 'runtime_version':'new', 'sha256':'d'*64, 'protocol_schema_digest':'e'*64}
        proc = dict(pid=1001, pgid=1001, starttime='1', parent_pid=1,
                    argv_digest='d'*64, executable_identity_digest='e'*64,
                    started_at='2026-11-06T00:00:00+00:00', exited_at='2026-11-06T00:01:00+00:00',
                    exit_code=0, process_group_gone=True)
        ledger['driver_runtime']['native_transport'] = dict(contract_version=2, generation='old-generation',
            state='cleaned', executable_identity=self.old, process_identity=proc)
        ledger['transport_cleanup_intent'] = dict(source_generation='old-generation',state='completed',
            process_group_gone=True, process_identity=proc, term_attempt=1, kill_attempt=0,
            created_at='2026-11-06T00:01:00+00:00',updated_at='2026-11-06T00:01:00+00:00')
        core.save_ledger(self.repo, ledger)
        self.before = self.f.ledger()
        self.action = self.f.next()
        self.probe = dict(status='READY',code='NATIVE_PROBE_READY',executable_identity=self.new,
                          cleanup={'process_group_gone':True})
        self.observation = dict(snapshot_digest=host_transition.snapshot(self.before),
            executable_identity=self.new, probe=self.probe, cleanup={'process_group_gone':True},
            threads={r:i['thread_id'] for r,i in ledger['facets']['execution']['agents'].items() if i and i.get('thread_id')})

    def authorize(self, **kwargs):
        args=dict(session_id='owner',action_id=self.action['action_id'],reason='Upgrade host',observation=self.observation)
        args.update(kwargs)
        return host_transition.authorize(self.repo,self.run,**args)

    def test_authorize_resume_replay_and_new_generation(self):
        context=core.driver_context(self.repo,self.run)
        with self.assertRaises(cli.NativeDriverError):
            cli._verify_resume_executable_identity(context,SimpleNamespace(executable_identity=self.new))
        result=self.authorize()
        after=self.f.ledger()
        for key in ('facets','evidence','driver_runtime','problems'):
            self.assertEqual(after[key],self.before[key])
        self.assertEqual(self.authorize()['request_id'],result['request_id'])
        self.assertEqual(len([e for e in self.f.ledger()['events'] if e['kind']=='native_host_transition_authorized']),1)
        cli._verify_resume_executable_identity(core.driver_context(self.repo,self.run),SimpleNamespace(executable_identity=self.new))
        moved=copy.deepcopy(after['driver_runtime']['native_transport'])
        moved.update(generation='new-generation',state='ready',executable_identity=self.new)
        core.bind_native_transport(self.repo,self.run,native_transport=moved)
        self.assertIsNone(host_transition.current_grant(self.f.ledger()))
        cli._verify_resume_executable_identity(core.driver_context(self.repo,self.run),SimpleNamespace(executable_identity=self.new))
        self.assertEqual(self.f.next()['action'],'reviewer_preflight')

    def test_owner_action_probe_and_thread_rejections(self):
        for change in ({'session_id':'other'},{'action_id':'stale'}):
            with self.subTest(change=change),self.assertRaises(core.AssuranceError):self.authorize(**change)
        for key,value in [('threads',{}),('probe',{}),('snapshot_digest','0'*64)]:
            obs={**self.observation,key:value}
            with self.subTest(key=key),self.assertRaises(core.AssuranceError):self.authorize(observation=obs)
        self.assertEqual(self.f.ledger(),self.before)

    def test_grant_does_not_allow_further_host_or_evidence_drift(self):
        self.authorize()
        with self.assertRaises(cli.NativeDriverError):
            cli._verify_resume_executable_identity(core.driver_context(self.repo,self.run),SimpleNamespace(executable_identity={**self.new,'sha256':'f'*64}))
        ledger=self.f.ledger();ledger['evidence']['preflight']['status']='fail';core.save_ledger(self.repo,ledger)
        self.assertIsNone(core.driver_context(self.repo,self.run)['native_host_transition'])

    def test_pending_transport_and_git_residue_rejected(self):
        ledger=self.f.ledger();ledger['transport_cleanup_intent']['state']='unknown';core.save_ledger(self.repo,ledger)
        with self.assertRaises(core.AssuranceError):self.authorize()
        core.save_ledger(self.repo,self.before)
        from pathlib import Path
        (Path(self.before['candidate_worktree'])/'unknown.txt').write_text('keep')
        with self.assertRaises(core.AssuranceError):self.authorize()

    def test_native_cli_observes_original_threads_before_authorization(self):
        import tempfile
        from pathlib import Path
        from tests.test_native_network_probe import FAKE_SERVER
        with tempfile.TemporaryDirectory(prefix='host-protocol-') as root:
            executable = Path(root) / 'codex'
            executable.write_text(FAKE_SERVER)
            executable.chmod(0o755)
            with patch.object(cli, 'current_session_id', return_value='owner'):
                rc = cli.main(['--codex-bin', str(executable), 'migrate-host', '--repo', str(self.repo),
                    '--run', self.run, '--session-id', 'owner', '--action-id', self.action['action_id'],
                    '--reason', 'Upgrade host'])
        self.assertEqual(rc, 0)
        self.assertIsNotNone(host_transition.current_grant(self.f.ledger()))
