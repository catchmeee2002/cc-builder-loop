"""Persisted delivery history compatibility, without using live subscriptions."""
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/lark-listener/scripts'
SETUP = '''
import tempfile
from unittest.mock import patch
from store import Store
from worker import notify, batch_delivery
with tempfile.TemporaryDirectory() as root:
    store = Store(root)
    with store.transaction() as data:
        data['subscriptions']['s'] = dict(subscription_id='s', owner_session='owner', active=True,
                                          thread_id='topic', generation=1)
        data['batches']['b'] = dict(batch_id='b', subscription_id='s', owner_session='owner',
                                   delivery_ids=[], attempts=0, next_notify=0, received_at=None, receipts=[])
'''


class DeliveryHistoryTests(unittest.TestCase):
    def run_scenario(self, body):
        code = 'import sys\nsys.path.insert(0, ' + repr(str(SCRIPTS)) + ')\n' + SETUP + body
        result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_old_queued_receipt_survives_later_unknown_attempt(self):
        self.run_scenario('''
    with store.transaction() as data:
        b = data['batches']['b']
        b['attempts'] = 2
        b['receipts'] = [dict(state='queued', queue_id='old', owner_session='OWNER', at=1),
                         dict(state='unknown', at=2)]
    with patch('transport.queue') as queue:
        notify(store, 's')
        queue.assert_not_called()
    with store.transaction() as data:
        assert batch_delivery(data['batches']['b']) == 'waiting_receive'
        assert len(data['batches']['b']['receipts']) == 2
''')

    def test_capability_failure_preserves_budget_and_retries_only_after_restart(self):
        self.run_scenario('''
    with patch('transport.queue', return_value=dict(state='blocked', error='QUEUE_RESUME_UNSUPPORTED')) as queue:
        notify(store, 's')
        notify(store, 's')
        assert queue.call_count == 1
    with store.transaction() as data:
        b = data['batches']['b']
        assert b['attempts'] == 0
        assert batch_delivery(b) == 'capability_blocked'
        data['subscriptions']['s']['generation'] += 1
        b['next_notify'] = 0
    with patch('transport.queue', return_value=dict(state='queued', queue_id='new', owner_session='owner')) as queue:
        notify(store, 's')
        notify(store, 's')
        assert queue.call_count == 1
    with store.transaction() as data:
        b = data['batches']['b']
        assert b['attempts'] == 1
        assert [r['state'] for r in b['receipts']] == ['blocked', 'queued']
''')
