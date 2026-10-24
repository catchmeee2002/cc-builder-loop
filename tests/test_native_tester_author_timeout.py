"""Tester author deadlines: delayed completion versus bounded failure."""
import unittest
from unittest.mock import Mock, patch

from runtime.codex_builder_loop.native_driver.app_server import (
    AppServerError, AppServerTransport, TurnTimeoutProfile, timeout_profile_for_action,
)

from runtime.codex_builder_loop.native_driver.coordinator import (
    NativeCoordinator, NativeDriverError,
)


class TesterAuthorTimeoutTests(unittest.TestCase):
    def wait_for_delayed_completion(self, action, delay, elapsed=0):
        transport = AppServerTransport(executable_identity={'fixture': True})
        profile = timeout_profile_for_action(action)
        clock = [0.0]
        budgets = []

        def receive(*, timeout):
            budgets.append(timeout)
            clock[0] += min(delay, timeout)
            if timeout < delay:
                raise AppServerError('synthetic silence', code='NATIVE_APP_SERVER_TIMEOUT')
            return {'method': 'turn/completed', 'params': {'threadId': 'thread',
                    'turn': {'id': 'turn', 'status': 'completed', 'items': []}}}

        # First clock read establishes the deadline; later reads include elapsed work.
        calls = [0]
        def now():
            calls[0] += 1
            return clock[0] + (elapsed if calls[0] > 1 else 0)

        with patch('runtime.codex_builder_loop.native_driver.app_server.time.monotonic', side_effect=now), \
                patch.object(transport, '_next_message', side_effect=receive):
            result = transport.wait_turn(thread_id='thread', turn_id='turn', timeout_profile=profile)
        return result, budgets, profile

    def test_tester_author_can_complete_after_old_idle_limit(self):
        result, budgets, profile = self.wait_for_delayed_completion('tester_author', 151)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(budgets, [360.0])
        self.assertEqual(profile.turn_total_seconds, 7200.0)
        self.assertNotEqual(profile.profile_digest, TurnTimeoutProfile(120.0, 7200.0, 600.0).profile_digest)

    def test_other_actions_keep_their_idle_boundary(self):
        for action in ('tester_fix', 'tester_recompose_fix', 'reviewer_final', 'builder_implement',
                       'tester_proof', 'tester_proof_diagnose', 'tester_blackbox', ''):
            with self.subTest(action=action), self.assertRaises(AppServerError) as raised:
                self.wait_for_delayed_completion(action, 200)
            self.assertEqual(raised.exception.code, 'NATIVE_APP_SERVER_TIMEOUT')

    def test_longer_silence_is_still_bounded(self):
        with self.assertRaises(AppServerError) as raised:
            self.wait_for_delayed_completion('tester_author', 400)
        self.assertEqual(raised.exception.code, 'NATIVE_APP_SERVER_TIMEOUT')

    def test_frozen_previous_author_profile_is_not_silently_migrated(self):
        coordinator = NativeCoordinator.__new__(NativeCoordinator)
        coordinator.core = Mock()
        pending = {
            'state': 'exhausted', 'action': 'tester_author', 'action_id': 'author',
            'generation': 1, 'attempt': 3,
            'timeout_profile_digest': TurnTimeoutProfile(120.0, 7200.0, 600.0).profile_digest,
        }
        original = dict(pending)
        with self.assertRaises(NativeDriverError) as raised:
            coordinator._migrate_pending_timeout_profile(pending, {})
        self.assertEqual(raised.exception.code, 'NATIVE_DISPATCH_TIMEOUT_PROFILE_DRIFT')
        self.assertEqual(pending, original)
        coordinator.core.call.assert_not_called()

    def test_remaining_total_budget_wins_over_extended_idle(self):
        with self.assertRaises(AppServerError) as raised:
            self.wait_for_delayed_completion('tester_author', 200, elapsed=7100)
        self.assertEqual(raised.exception.code, 'NATIVE_APP_SERVER_TURN_TIMEOUT')
        self.assertEqual(raised.exception.details['timeout_seconds'], 7200.0)


if __name__ == '__main__':
    unittest.main()
