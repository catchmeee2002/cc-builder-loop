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

    def test_reviewer_uses_configured_execution_permissions(self):
        driver = object.__new__(NativeCoordinator)
        driver.project_root = Path(__file__).resolve().parents[1]
        self.assertEqual(driver._role_config("reviewer")[1], "danger-full-access")
        self.assertEqual(driver._sandbox_policy({}, "reviewer", {}), {"type": "dangerFullAccess"})
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

    def test_role_permission_override_is_used_consistently(self):
        from unittest.mock import patch
        driver = object.__new__(NativeCoordinator)
        with patch.object(driver, '_role_config', return_value=('review only', 'read-only')):
            self.assertEqual(driver._sandbox_policy({}, 'reviewer', {}), {'type': 'readOnly'})
