import unittest
from harness import init_repo, cleanup_repo
from tests.test_compact_profile_contract import compact_contract
from runtime.codex_builder_loop.assurance_v4 import core, driver


class HoldTest(unittest.TestCase):
    def setUp(self):
        self.repo = init_repo()
        self.addCleanup(cleanup_repo, self.repo)
        core.start(self.repo, "held-run", "owner-session", compact_contract())

    def test_hold_survives_readback_and_release_recomputes_action(self):
        held = core.hold_run(self.repo, "held-run", session_id="owner-session", reason="inspect")
        self.assertEqual(held["status"], "HELD")
        self.assertEqual(driver.next_action(self.repo, "held-run")["status"], "HELD")
        with self.assertRaises(core.AssuranceError):
            core.finalize(self.repo, "held-run", "should not finalize")
        core.hold_run(self.repo, "held-run", session_id="owner-session", reason="continue", release_id=held["hold"]["id"])
        action = driver.next_action(self.repo, "held-run")
        self.assertNotEqual(action["status"], "HELD")
        self.assertNotEqual(action["action"], "finalize")

    def test_wrong_session_and_stale_release_fail(self):
        with self.assertRaises(core.AssuranceError):
            core.hold_run(self.repo, "held-run", session_id="other", reason="inspect")
        core.hold_run(self.repo, "held-run", session_id="owner-session", reason="inspect")
        with self.assertRaises(core.AssuranceError):
            core.hold_run(self.repo, "held-run", session_id="owner-session", reason="continue", release_id="stale")

    def test_ready_hold_allows_work_but_keeps_finalize_blocked(self):
        core.hold_run(self.repo, "held-run", session_id="owner-session", reason="review delivery", when="ready")
        self.assertNotEqual(driver.next_action(self.repo, "held-run")["status"], "HELD")
        with self.assertRaises(core.AssuranceError):
            core.finalize(self.repo, "held-run", "blocked")

    def test_held_mission_cannot_change_and_strand_release(self):
        import copy
        from runtime.codex_builder_loop.assurance_v4.store import read_ledger
        held = core.hold_run(self.repo, "held-run", session_id="owner-session", reason="inspect")
        mission = copy.deepcopy(read_ledger(self.repo, "held-run")["facets"]["mission"])
        mission["revision"] += 1
        with self.assertRaises(core.AssuranceError) as caught:
            core.update_facet(self.repo, "held-run", "mission", mission, semantic_revision=True)
        self.assertEqual(caught.exception.code, "HOLD_MISSION_REVISION_BLOCKED")
        core.hold_run(self.repo, "held-run", session_id="owner-session", reason="continue", release_id=held["hold"]["id"])
