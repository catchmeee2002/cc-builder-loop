"""Foreground contention is visible without abandoning durable worker state."""
import json
import time
import unittest
from tests.test_lark_listener_transport import Sandbox, message, batch_id


class ContentionTests(unittest.TestCase):
    def test_busy_commands_preserve_state_and_resume_after_unlock(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.pages([[message('om_contention', 'preserve this pending message')]])
        s.read_once()
        queued = s.wait(lambda: s.batches())[-1]
        s.stop()
        before = (s.state / 'state.json').read_bytes()
        requests = {
            'status': {}, 'start': {}, 'stop': {},
            'receive': {'batch_id': batch_id(queued['message'])},
            'reply': {'request_id': 'stable-contention', 'reply_to': 'om_contention', 'text': 'reply'},
        }
        with (s.state / 'state.lock').open('a') as lock:
            s.fcntl.flock(lock, s.fcntl.LOCK_EX)
            for operation, request in requests.items():
                with self.subTest(operation=operation):
                    result = s.command(operation, request, expected=2)
                    self.assertEqual(result, {'status': 'DEGRADED', 'code': 'STATE_BUSY'})
                    self.assertEqual((s.state / 'state.json').read_bytes(), before)
        self.assertFalse(s.calls('+messages-reply'))
        self.assertEqual(s.status()['status'], 'STOPPED')
        s.receive(queued)
        s.read_once()
        self.assertEqual(len(s.batches()), 1)
        s.stop()

    def test_brief_contention_completes_without_false_busy(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.read_once()
        s.stop()
        with (s.state / 'state.lock').open('a') as lock:
            s.fcntl.flock(lock, s.fcntl.LOCK_EX)
            proc = s.launch('status')
            time.sleep(.2)
            self.assertIsNone(proc.poll())
        out, err = proc.communicate(timeout=8)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(json.loads(out)['status'], 'STOPPED')

    def test_worker_keeps_pending_page_across_foreground_lock_budget(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.pages([[message('om_delayed_commit', 'page survives lock contention')]])
        s.change(block={'op': '+threads-messages-list', 'name': 'page'})
        s.read_once()
        s.wait(lambda: (s.root / 'page.entered').exists())
        identity = s.latest_worker()
        from tests.test_lark_listener_transport import proc_identity
        with (s.state / 'state.lock').open('a') as lock:
            s.fcntl.flock(lock, s.fcntl.LOCK_EX)
            (s.root / 'page.release').touch()
            time.sleep(2.3)
            actual = proc_identity(identity['pid'])
            self.assertIsNotNone(actual)
            self.assertEqual(actual['starttime'], identity['starttime'])
            self.assertNotEqual(actual['state'], 'Z')
        queued = s.wait(lambda: s.batches())[-1]
        self.assertIn('om_delayed_commit', queued['message'])
        s.receive(queued)
        self.assertEqual(len(s.batches()), 1)
