from pathlib import Path
import unittest
from harness import init_repo, cleanup_repo, head, commit_all, git
from tests.test_compact_profile_contract import compact_contract
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.assurance_v4.store import read_ledger


class TesterDeletionTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.addCleanup(cleanup_repo, self.repo)

    def test_deletion_integrates_and_replay_is_idempotent(self):
        contract = compact_contract()
        started = core.start(self.repo, 'delete-run', 'owner', contract)
        candidate = Path(started['candidate_worktree'])
        (candidate / 'src/calc.py').write_text('def add(a, b):\n    return a + b + 0\n')
        commit_all(candidate, 'candidate')
        core.checkpoint_builder(self.repo, 'delete-run')
        core.prepare_tester(self.repo, 'delete-run', 'tester', 'tester-thread')
        ledger = read_ledger(self.repo, 'delete-run')
        source = Path(ledger['facets']['execution']['tester_source']['worktree'])
        (source / 'tests/test_calc.py').unlink()
        (source / 'tests/test_replacement.py').write_text('import unittest\nfrom src.calc import add\nclass AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1,2),3)\n')
        commit_all(source, 'replace obsolete test')
        core.integrate_tester(self.repo, 'delete-run')
        integrated = head(candidate)
        self.assertFalse((candidate / 'tests/test_calc.py').exists())
        self.assertTrue((candidate / 'tests/test_replacement.py').exists())
        ledger = read_ledger(self.repo, 'delete-run')
        entry = next(item for item in ledger['facets']['execution']['tester_source']['files'] if item['path']=='tests/test_calc.py')
        self.assertEqual(entry['operation'], 'delete')
        self.assertIsNone(entry['blob'])
        core.integrate_tester(self.repo, 'delete-run')
        self.assertEqual(head(candidate), integrated)
        (self.repo / 'tests/test_calc.py').write_text('def test_concurrent(): assert True\n')
        target = commit_all(self.repo, 'concurrent target change')
        core.recompose_candidate(self.repo, 'delete-run')
        waiting = read_ledger(self.repo, 'delete-run')
        self.assertEqual(waiting['recomposition_intent']['state'], 'waiting_tester')
        self.assertEqual(head(self.repo), target)
        self.assertIn('test_concurrent', (self.repo / 'tests/test_calc.py').read_text())
        staging = Path(waiting['recomposition_intent']['tester_worktree'])
        git(staging, 'rm', '--', 'tests/test_calc.py')
        commit_all(staging, 'resolve deletion conflict')
        core.recompose_candidate(self.repo, 'delete-run')
        recomposed = read_ledger(self.repo, 'delete-run')
        self.assertIsNone(recomposed['recomposition_intent'])
        self.assertFalse((candidate / 'tests/test_calc.py').exists())
        self.assertTrue((candidate / 'tests/test_replacement.py').exists())
        self.assertEqual(head(self.repo), target)

    def test_absence_without_base_and_symlink_are_not_deletions(self):
        base = head(self.repo)
        with self.assertRaises(core.AssuranceError):
            core._tester_entry(self.repo, base, base, 'tests/never-existed.py')
        (self.repo / 'tests/test_calc.py').unlink()
        (self.repo / 'tests/test_calc.py').symlink_to('../src/calc.py')
        changed = commit_all(self.repo, 'replace with link')
        with self.assertRaises(core.AssuranceError):
            core._tester_entry(self.repo, base, changed, 'tests/test_calc.py')
