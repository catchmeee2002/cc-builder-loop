"""Regression checks for test-process ownership and failure evidence."""
import json
import subprocess
import sys
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_lark_listener_transport import Sandbox, proc_identity


class HarnessTests(unittest.TestCase):
    def test_process_disappearing_during_proc_read_is_absent(self):
        with patch.object(Path, 'read_text', side_effect=ProcessLookupError):
            self.assertIsNone(proc_identity(123))

    def test_cleanup_drains_peer_and_preserves_unrelated_process(self):
        s = Sandbox()
        self.addCleanup(s.close)
        peer = s.root / 'lark-peer'
        peer.write_text(peer.read_text().replace('deadline = time.monotonic() + 20',
                          'time.sleep(3)\n    deadline = time.monotonic() + 20'))
        s.change(block={'op': '+threads-messages-list', 'name': 'drain'})
        s.read_once()
        s.wait(lambda: (s.root / 'drain.entered').exists())
        child = int((s.root / 'drain.entered').read_text())
        original = proc_identity(child)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            s.close()
            remaining = proc_identity(child)
            self.assertTrue(remaining is None or remaining['state'] == 'Z' or
                            remaining['starttime'] != original['starttime'])
            self.assertFalse(s.root.exists())
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_timeout_keeps_evidence_outside_deleted_sandbox(self):
        s = Sandbox()
        try:
            s.read_once()
            with (s.root / 'peer.lock').open('a') as lock:
                s.fcntl.flock(lock, s.fcntl.LOCK_EX)
                with self.assertRaises(AssertionError) as caught:
                    s.wait(lambda: False, timeout=.01)
            destination = Path(str(caught.exception).split('diagnostics=', 1)[1])
            self.addCleanup(lambda: shutil.rmtree(destination))
            snapshot = json.loads((destination / 'snapshot.json').read_text())
            self.assertEqual(snapshot['sandbox'], str(s.root))
            self.assertTrue(snapshot['processes'])
            s.close()
            self.assertTrue((destination / 'state.json').exists())
        finally:
            s.close()

    def test_command_timeout_captures_live_process_before_kill(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.entry.write_text('import time; time.sleep(30)\n')
        with self.assertRaises(AssertionError) as caught:
            s.command('start', timeout=.1)
        destination = Path(str(caught.exception).split('diagnostics=', 1)[1])
        self.addCleanup(lambda: shutil.rmtree(destination))
        snapshot = json.loads((destination / 'snapshot.json').read_text())
        rows = [row for row in snapshot['processes'] if str(s.entry) in row['cmdline']]
        self.assertEqual(len(rows), 1)
        self.assertIn('wchan', rows[0])
        self.assertIsNotNone(s.processes[-1].returncode)

    def test_command_timeout_still_reaps_when_snapshot_write_fails(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.entry.write_text('import time; time.sleep(30)\n')
        with patch.object(s, 'diagnostics', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(AssertionError, 'diagnostics=unavailable'):
                s.command('start', timeout=.1)
        self.assertIsNotNone(s.processes[-1].returncode)
