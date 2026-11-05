"""State invariants observed through CLI, notifications and owned durable files.

These scenarios are composed into the blackbox TestCase, so the blackbox command
also exercises retention and ownership.  They do not import production code or
assume a state filename, record layout, hash algorithm or module decomposition.
"""
from __future__ import annotations

import json
import re
import time

from tests.test_lark_listener_transport import (
    DAY, OTHER, OWNER, batch_id, message, named, nodes, proc_identity, text_of,
)


def documents(text):
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == '{':
            try:
                result, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            yield result


def versions(row):
    """Read explicit message/version pairs, without fixing notification layout."""
    text = row['message']
    result = {}
    for doc in documents(text):
        for node in nodes(doc):
            if not isinstance(node, dict) or not isinstance(node.get('message_id'), str):
                continue
            version = next((node[key] for key in ('version', 'version_id', 'version_digest')
                            if key in node and node[key] is not None), None)
            if version is not None:
                result.setdefault(node['message_id'], set()).add(text_of(version))
    if not result:
        for match in re.finditer(
                r'message_id[\s`"\'*]*[:=][\s`"\'*]*(om_[\w-]+)[^\n]*?'
                r'version(?:_id|_digest)?[\s`"\'*]*[:=][\s`"\'*]*([\w.:-]+)', text):
            result.setdefault(match.group(1), set()).add(match.group(2))
    if not result:
        raise AssertionError('Notification lacks explicit message/version pairs: ' + text)
    return result


def has_fact(value, word):
    """Recognize named facts/enums, never a word embedded in message prose."""
    for node in nodes(value):
        if isinstance(node, str) and re.fullmatch(r'[A-Za-z_]+', node):
            tokens = node.lower().split('_')
            if word in tokens and not {'not', 'unconfirmed', 'failed'}.intersection(tokens):
                return True
        if isinstance(node, dict):
            for key, item in node.items():
                tokens = key.lower().split('_')
                if (word in tokens and 'not' not in tokens
                        and item not in (None, False, '', [], {})):
                    return True
    return False


class StateScenarios:
    def test_owner_confirmation_is_batch_scoped_and_immutable(self):
        s = self.s
        s.pages([[message('om_first', 'first immutable payload')]])
        s.read_once()
        first = s.wait(lambda: s.batches())[-1]
        before = versions(first)
        self.assertIn('om_first', before)
        s.receive(first, OTHER)
        # A rejected foreign ACK must leave the same batch eligible for retry.
        s.advance(301)
        s.restart()
        retry = s.wait(lambda: [r for r in s.batches()[1:] if batch_id(r['message']) == batch_id(first['message'])])[-1]
        self.assertEqual(versions(retry), before)
        result = s.receive(first)
        again = s.receive(first)
        self.assertIn(batch_id(first['message']), text_of(result))
        self.assertIn(batch_id(first['message']), text_of(again))
        s.pages([[message('om_first', 'first immutable payload'), message('om_later', 'later payload')]])
        count = len(s.batches())
        s.restart()
        later = s.wait(lambda: s.batches()[count:])[-1]
        self.assertNotEqual(batch_id(first['message']), batch_id(later['message']))
        self.assertEqual(set(versions(later)), {'om_later'})
        s.receive(first)  # Repeating an old ACK cannot consume the later batch.
        s.advance(301)
        count = len(s.batches())
        s.restart()
        last = s.wait(lambda: s.batches()[count:])[-1]
        self.assertEqual(batch_id(last['message']), batch_id(later['message']))
        s.receive(later)
        self.assertFalse(has_fact(result, 'business_complete'))

    def test_ack_age_not_message_age_controls_seven_day_body_retention(self):
        s = self.s
        marker = 'RETENTION_BODY_7d_c85d0a6c'
        s.pages([[message('om_retained', marker, stamp=1)]])
        s.read_once()
        batch = s.wait(lambda: s.batches())[-1]
        s.advance(3 * DAY)
        s.receive(batch)
        self.assertIn(marker.encode(), s.state_bytes())
        s.stop()
        s.advance(7 * DAY - 1)
        s.read_once()
        self.assertIn(marker.encode(), s.state_bytes(), 'Body must survive until received_at + 7 days')
        s.stop()
        s.advance(1)
        count = len(s.batches())
        s.read_once()
        s.wait(lambda: marker.encode() not in s.state_bytes(), description='body GC at exactly seven days')
        time.sleep(.2)
        self.assertEqual(len(s.batches()), count, 'Active minimal version index must prevent replay after body GC')
        self.assertIn(batch_id(batch['message']).encode(), s.state_bytes(), 'Terminal receipt must survive seven days')
        s.stop()
        s.advance(23 * DAY - 1)
        s.read_once()
        self.assertIn(batch_id(batch['message']).encode(), s.state_bytes())
        s.stop()
        s.advance(1)
        s.read_once()
        s.wait(lambda: batch_id(batch['message']).encode() not in s.state_bytes(),
               description='terminal receipt GC at exactly thirty days')
        self.assertEqual(len(s.batches()), count, 'Thirty-day receipt GC must not erase the active dedupe index')

    def test_unreceived_body_survives_thirty_days_and_reminder_is_bounded(self):
        s = self.s
        marker = 'UNRECEIVED_BODY_30d_dfe4c952'
        s.pages([[message('om_unreceived', marker)]])
        s.read_once()
        batch = s.wait(lambda: s.batches())[-1]
        bid = batch_id(batch['message'])
        # Exhaust the three delivery attempts without acknowledging anything.
        for attempt in range(2):
            count = len(s.batches())
            s.advance(301)
            s.restart()
            s.wait(lambda: len(s.batches()) > count, description='bounded retry')
        self.assertEqual(sum(batch_id(r['message']) == bid for r in s.batches()), 3)
        self.assertTrue(has_fact(s.status(), 'attention'), 'Unconfirmed batch must remain visible after three attempts')
        s.stop()
        s.advance(30 * DAY)
        before = len(s.queues())
        s.read_once()
        s.wait(lambda: len(s.queues()) > before, description='thirty-day unreceived reminder')
        self.assertIn(marker.encode(), s.state_bytes())
        reminders = len(s.queues())
        s.advance(301)
        s.restart()
        time.sleep(.2)
        self.assertEqual(len(s.queues()), reminders, 'Aged unreceived reminder must not repeat every restart')
        s.receive(batch)

    def test_private_storage_and_status_read_only(self):
        s = self.s
        s.pages([[message('om_private', 'private body')]])
        s.read_once()
        row = s.wait(lambda: s.batches())[-1]
        self.assertEqual(s.state.stat().st_mode & 0o777, 0o700)
        for path in s.state.rglob('*'):
            self.assertFalse(path.is_symlink(), str(path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600, str(path))
        s.receive(row)
        worker = s.latest_worker()
        s.stop()
        s.wait(lambda: proc_identity(worker['pid']) is None
               or proc_identity(worker['pid'])['state'] == 'Z', description='stopped worker exit')
        before = {str(p.relative_to(s.state)): p.read_bytes() for p in s.state.rglob('*') if p.is_file()}
        calls = len(s.calls())
        result = s.status()
        after = {str(p.relative_to(s.state)): p.read_bytes() for p in s.state.rglob('*') if p.is_file()}
        self.assertEqual(before, after, 'status must not mutate durable state')
        self.assertEqual(len(s.calls()), calls, 'status must not poll a remote')
        self.assertNotIn('fixture-private-error-envelope-do-not-forward', text_of(result))
        self.assertNotIn('business_complete', text_of(result))

    def test_stopped_settled_retention_reestablishes_read_boundary(self):
        s = self.s
        s.pages([[message('om_boundary', 'old settled content')]])
        s.read_once()
        row = s.wait(lambda: s.batches())[-1]
        s.receive(row)
        s.stop()
        s.advance(31 * DAY)
        result = s.read_once()
        rendered = text_of(result).lower()
        self.assertRegex(rendered, r'boundary|backlog|读取边界',
                         'A cleared stopped index requires an explicit new read boundary')
        self.assertNotIn(batch_id(row['message']).encode(), s.state_bytes(),
                         'Settled stopped receipts must be cleaned after thirty days')


# Oracle self-checks are author checks, not listener behavioral proof.
import unittest


class StateOracleTests(unittest.TestCase):
    def test_prose_cannot_masquerade_as_a_delivery_fact(self):
        self.assertFalse(has_fact({'messages': [{'text': 'must not claim queued'}], 'queued': False}, 'queued'))
        self.assertTrue(has_fact({'delivery': {'state': 'queued'}}, 'queued'))
        self.assertFalse(has_fact({'unknown_sends': []}, 'unknown'))
        self.assertTrue(has_fact({'unknown_sends': [{'request_id': 'pending'}]}, 'unknown'))

    def test_message_versions_are_explicit_and_payload_bound(self):
        row = {'message': json.dumps({'batch_id': 'b', 'messages': [
            {'message_id': 'om_x', 'version': 'v1', 'content': 'hello'},
            {'message_id': 'om_y', 'version': 'v2', 'content': 'world'}]})}
        self.assertEqual(versions(row), {'om_x': {'"v1"'}, 'om_y': {'"v2"'}})
        with self.assertRaises(AssertionError):
            versions({'message': 'om_x hello, with no explicit version'})

    def test_nested_receipt_field_names_do_not_fix_an_output_layout(self):
        self.assertEqual(named({'outer': [{'batch_id': 'a'}, {'inner': {'batch_id': 'b'}}]}, 'batch_id'), ['a', 'b'])
