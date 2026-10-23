"""Successful wire observations remain useful after the in-memory tail rolls over."""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from runtime.codex_builder_loop.native_driver.app_server import AppServerTransport, TurnTimeoutProfile
from runtime.codex_builder_loop.native_driver.observation_journal import ObservationJournal, FOOTER_RESERVE


class ObservationJournalTests(unittest.TestCase):
    def test_success_retains_long_read_after_ring_eviction_without_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ObservationJournal(directory, utc_anchor='2026-01-01T00:00:00+00:00')
            t = AppServerTransport(executable_identity={'fixture': True})
            t._observation_journal = journal
            reader, writer = os.pipe()
            stream = os.fdopen(reader, 'rb', buffering=0)
            t.process = SimpleNamespace(stdout=stream)
            first = {'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
                     'item': {'type': 'agentMessage', 'text': 'PRIVATE_RESULT_BODY'}}}
            final = {'method': 'turn/completed', 'params': {'threadId': 'thread',
                     'turn': {'id': 'turn', 'status': 'completed', 'items': []}}}
            messages = [first] + [{'method': 'heartbeat'}] * 200 + [final]
            wire = b''.join((json.dumps(m)+'\n').encode() for m in messages)
            clock = [0.0]
            def delayed_read(*args):
                clock[0] = 180.0
                return wire
            try:
                os.write(writer, b'x')
                with patch('runtime.codex_builder_loop.native_driver.app_server.os.read', side_effect=delayed_read), \
                     patch('runtime.codex_builder_loop.native_driver.app_server.time.perf_counter', side_effect=lambda: clock[0]):
                    t._diagnostic_epoch = 0
                    result = t.wait_turn(thread_id='thread', turn_id='turn',
                                         timeout_profile=TurnTimeoutProfile(360, 7200, 600))
                self.assertEqual(result.status, 'completed')
                self.assertEqual(result.text, 'PRIVATE_RESULT_BODY')
                self.assertGreater(t.receive_snapshot()['dropped_events'], 0)
                journal.close(cleanup={'process_group_gone': True})
                text = journal.path.read_text()
                self.assertNotIn('PRIVATE_RESULT_BODY', text)
                records = [json.loads(line) for line in text.splitlines()]
                summary = records[-1]['turns'][0]
                self.assertEqual(summary['max_complete_frame_wait_seconds'], 180)
                self.assertEqual(summary['parsed_frames'], 202)
                self.assertEqual(summary['outcome'], 'completed')
                self.assertEqual(summary['longest_read_frame']['sequence'], 1)
                self.assertEqual(records[-1]['omitted_events'], 0)
                self.assertEqual(journal.path.stat().st_mode & 0o777, 0o600)
            finally:
                journal.close(cleanup=None)
                stream.close()
                os.close(writer)
                t.process = None

    def test_buffered_frame_does_not_count_prior_host_processing_as_read_wait(self):
        t = AppServerTransport(executable_identity={'fixture': True})
        t._read_buffer = b'{"method":"heartbeat"}\n'
        t.process = SimpleNamespace(stdout=io.BytesIO())
        with patch('runtime.codex_builder_loop.native_driver.app_server.time.perf_counter', return_value=500):
            t._diagnostic_epoch = 0
            t._read()
        self.assertEqual(t.receive_snapshot()['events'][-1]['complete_frame_wait_seconds'], 0)
        t.process = None

    def test_journal_limit_preserves_summary_and_reports_omissions(self):
        with tempfile.TemporaryDirectory() as directory:
            j = ObservationJournal(directory, utc_anchor='anchor', max_bytes=FOOTER_RESERVE+1000)
            j.observe(dict(kind='wait_turn', thread_id='t', turn_id='u', turn_idle_seconds=360,
                           turn_total_seconds=7200, offset_seconds=0))
            for n in range(500):
                j.observe(dict(kind='message_parsed', offset_seconds=n, sequence=n,
                               method='heartbeat', complete_frame_wait_seconds=float(n)))
            j.observe(dict(kind='turn_wait_completed', offset_seconds=501, code='completed'))
            j.close(cleanup=None)
            footer=json.loads(j.path.read_text().splitlines()[-1])
            self.assertGreater(footer['omitted_events'], 0)
            self.assertEqual(footer['turns'][0]['max_complete_frame_wait_seconds'], 499)
            self.assertLessEqual(j.path.stat().st_size, FOOTER_RESERVE+1000)

    def test_more_than_64_turns_reports_evicted_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            j=ObservationJournal(directory, utc_anchor='anchor')
            for n in range(70):
                j.observe(dict(kind='wait_turn', thread_id='t', turn_id=str(n), turn_idle_seconds=360,
                               turn_total_seconds=7200, offset_seconds=n))
                j.observe(dict(kind='turn_wait_completed', offset_seconds=n, code='completed'))
            j.close(cleanup=None)
            footer=json.loads(j.path.read_text().splitlines()[-1])
            self.assertEqual(len(footer['turns']),64)
            self.assertEqual(footer['evicted_turn_summaries'],6)

    def test_insecure_directory_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory,0o755)
            with self.assertRaises(ValueError):
                ObservationJournal(directory,utc_anchor='anchor')

    def test_write_failure_does_not_stop_observation_or_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            j=ObservationJournal(directory,utc_anchor='anchor')
            real=j.stream
            fake=SimpleNamespace(closed=False,write=lambda *a: (_ for _ in ()).throw(OSError('disk full')))
            j.stream=fake
            with self.assertWarns(RuntimeWarning):
                j.observe({'kind':'read_started','offset_seconds':0})
            self.assertTrue(j.failed)
            j.stream=real
            j.close(cleanup=None)


if __name__=='__main__':
    unittest.main()
