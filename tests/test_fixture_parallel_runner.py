import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1] / 'scripts/run-fixtures.py'


class ParallelFixturesTest(unittest.TestCase):
    def test_serial_and_parallel_have_same_tests_and_propagate_failure(self):
        with tempfile.TemporaryDirectory(prefix='fixture-runner-test-') as raw:
            root = Path(raw)
            for name in ('one', 'two'):
                (root / f'test_{name}.py').write_text('import unittest\nclass TestValue(unittest.TestCase):\n    def test_value(self): self.assertEqual(1, 1)\n')
            command = [sys.executable, str(RUNNER), '--start-dir', str(root)]
            ids = subprocess.run([*command, '--list-json'], capture_output=True, text=True)
            self.assertEqual(ids.returncode, 0, ids.stderr)
            self.assertEqual(len(json.loads(ids.stdout)), 2)
            for jobs in (1, 2):
                completed = subprocess.run([*command, '--jobs', str(jobs)], capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertIn('Fixtures: 2/2', completed.stdout)
            (root / 'test_two.py').write_text('import unittest\nclass TestValue(unittest.TestCase):\n    def test_value(self): self.fail("expected fixture failure")\n')
            completed = subprocess.run([*command, '--jobs', '2'], capture_output=True, text=True)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn('expected fixture failure', completed.stdout)

    def test_import_failure_cannot_report_empty_success(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'test_broken.py').write_text('raise RuntimeError("broken import")\n')
            completed = subprocess.run([sys.executable, str(RUNNER), '--start-dir', str(root)], capture_output=True, text=True)
            self.assertNotEqual(completed.returncode, 0)

    def test_nested_same_named_modules_share_one_inventory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = 'import unittest\nclass TestValue(unittest.TestCase):\n    def test_value(self): self.assertEqual(1, 1)\n'
            (root / 'test_same.py').write_text(source)
            for package in ('left', 'right'):
                (root / package).mkdir()
                (root / package / '__init__.py').write_text('')
                (root / package / 'test_same.py').write_text(source)
            command = [sys.executable, str(RUNNER), '--start-dir', str(root)]
            collected = subprocess.run([*command, '--list-json'], capture_output=True, text=True, check=True)
            self.assertEqual(len(json.loads(collected.stdout)), 3)
            for jobs in (1, 3):
                done = subprocess.run([*command, '--jobs', str(jobs)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn('Fixtures: 3/3', done.stdout)
            (root / 'right' / 'test_same.py').write_text(source.replace('self.assertEqual(1, 1)', 'self.fail("nested failure")'))
            done = subprocess.run([*command, '--jobs', '3'], capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn('nested failure', done.stdout)
