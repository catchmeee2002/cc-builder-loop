from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from runtime.codex_builder_loop.process import run_owned_command


class OwnedProcessRuntimeTest(unittest.TestCase):
    def test_completed_command_records_identity_and_group_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owned-process-") as raw:
            result = run_owned_command(
                [sys.executable, "-c", "print('ready')"],
                cwd=raw,
                env={"PATH": "/usr/bin:/bin"},
                timeout=5,
                executable_identity={"path": sys.executable},
            )
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"].strip(), "ready")
        self.assertTrue(result["cleanup"]["process_group_gone"])
        self.assertEqual(result["process_identity"]["pgid"], result["process_identity"]["pid"])

    def test_timeout_reaps_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owned-process-") as raw:
            result = run_owned_command(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=raw,
                env={"PATH": "/usr/bin:/bin"},
                timeout=0.1,
                executable_identity={"path": sys.executable},
            )
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["cleanup"]["process_group_gone"])
        self.assertIsNotNone(result["process_identity"]["exit_code"])

    def test_timeout_reaps_descendant_in_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owned-process-") as raw:
            result = run_owned_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys,time; "
                        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                        "time.sleep(30)"
                    ),
                ],
                cwd=raw,
                env={"PATH": "/usr/bin:/bin"},
                timeout=0.1,
                executable_identity={"path": sys.executable},
            )
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["cleanup"]["process_group_gone"])
        self.assertEqual(result["cleanup"]["state"], "cleaned")

    def test_command_cwd_is_not_changed_by_process_helper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="owned-process-") as raw:
            result = run_owned_command(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.getcwd())",
                ],
                cwd=raw,
                env={"PATH": "/usr/bin:/bin"},
                timeout=5,
                executable_identity={"path": sys.executable},
            )
        self.assertEqual(Path(result["stdout"].strip()), Path(raw))


if __name__ == "__main__":
    unittest.main()
