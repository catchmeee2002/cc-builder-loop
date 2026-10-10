from pathlib import Path
import unittest
from harness import init_repo, cleanup_repo, head, git
from runtime.codex_builder_loop.assurance_v4 import core
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator


class ReviewerIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.addCleanup(cleanup_repo, self.repo)
        self.ledger = {"candidate_worktree": str(self.repo), "facets": {"execution": {"candidate_head": head(self.repo)}}}
        self.ledger["reviewer_observation"] = core._reviewer_manifest(self.ledger)

    def test_read_only_policy_is_applied_to_reviewer_only(self):
        driver = object.__new__(NativeCoordinator)
        self.assertEqual(driver._sandbox_policy({}, "reviewer", {}), {"type": "readOnly"})
        self.assertEqual(driver._sandbox_policy({}, "builder", {}), {"type": "dangerFullAccess"})

    def test_worktree_and_index_only_changes_are_rejected(self):
        core._assert_reviewer_integrity(self.ledger)
        path = self.repo / "README.md"
        original = path.read_text()
        path.write_text("changed\n")
        git(self.repo, "add", "README.md")
        path.write_text(original)
        with self.assertRaisesRegex(core.AssuranceError, "changed"):
            core._assert_reviewer_integrity(self.ledger)

    def test_missing_observation_fails_closed(self):
        self.ledger.pop("reviewer_observation")
        with self.assertRaises(core.AssuranceError):
            core._assert_reviewer_integrity(self.ledger)

    def test_untracked_file_and_executable_bit_change_are_detected(self):
        (self.repo / "extra.txt").write_text("unexpected")
        with self.assertRaises(core.AssuranceError):
            core._assert_reviewer_integrity(self.ledger)
        (self.repo / "extra.txt").unlink()
        path = self.repo / "README.md"
        path.chmod(path.stat().st_mode | 0o111)
        with self.assertRaises(core.AssuranceError):
            core._assert_reviewer_integrity(self.ledger)

    def test_unavailable_sandbox_never_reports_capability(self):
        from unittest.mock import patch
        import subprocess
        from runtime.codex_builder_loop.native_driver.app_server import probe_read_only_sandbox, AppServerError
        with patch('runtime.codex_builder_loop.native_driver.app_server.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'namespace unavailable')):
            with self.assertRaises(AppServerError) as raised:
                probe_read_only_sandbox('codex')
        self.assertEqual(raised.exception.code, 'NATIVE_REVIEWER_SANDBOX_UNAVAILABLE')
