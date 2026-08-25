from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from harness import ROOT, run_process


class NativeBuilderFailureBlackboxTest(unittest.TestCase):
    def test_public_native_driver_blocks_dirty_builder_retry(self) -> None:
        helper = ROOT / "tests" / "helpers" / "native_builder_failure_blackbox.py"
        completed = run_process([sys.executable, helper], cwd=ROOT)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(
            lines,
            f"blackbox returned no JSON\nstdout={completed.stdout}\nstderr={completed.stderr}",
        )
        payload = json.loads(lines[-1])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["result"], "pass")
        self.assertEqual(payload["failure_code"], "NATIVE_BUILDER_SIDE_EFFECT_RETRY_BLOCKED")
        self.assertEqual(payload["turn_count"], 1)
        self.assertRegex(payload["manifest_digest"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
