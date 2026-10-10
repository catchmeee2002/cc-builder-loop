import sys
import unittest
from harness import init_repo, cleanup_repo, head, commit_all
from runtime.codex_builder_loop.assurance_v4 import core


class RoleProjectionTest(unittest.TestCase):
    def test_target_drift_exposes_interacting_paths(self):
        repo = init_repo()
        self.addCleanup(cleanup_repo, repo)
        old = head(repo)
        (repo / 'src/calc.py').write_text('def add(a,b): return a+b+0\n')
        target = commit_all(repo, 'target')
        (repo / 'src/calc.py').write_text('def add(a,b): return sum((a,b))\n')
        candidate = commit_all(repo, 'candidate')
        ledger = {'repo_root': str(repo), 'facets': {'execution': {'candidate_head': candidate}}, 'events': [{'kind': 'target_rematerialized', 'details': {'old_target_head': old, 'new_target_head': target}}]}
        value = core.role_observations(ledger)
        self.assertEqual(value['target_drift'][0]['candidate_overlap'], ['src/calc.py'])

    def test_real_unittest_runner_startup_uses_empty_collection(self):
        repo = init_repo()
        self.addCleanup(cleanup_repo, repo)
        command = {'argv': [sys.executable, '-m', 'unittest'], 'timeout_seconds': 20}
        self.assertTrue(core._probe_proof_runner(repo, command, sys.executable))
