from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from harness import ROOT, run_process


class ReviewerReplacementBlackboxTest(unittest.TestCase):
    def test_reviewer_replacement_keeps_source_and_capability_boundaries(self) -> None:
        helper = ROOT / "tests" / "helpers" / "reviewer_replacement_blackbox.py"
        completed = run_process(
            [sys.executable, helper],
            cwd=ROOT,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertTrue(
            lines,
            f"blackbox returned no JSON\nstdout={completed.stdout}\nstderr={completed.stderr}",
        )
        payload = json.loads(lines[-1])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(payload["result"], "pass")
        self.assertEqual(payload["replacement_action"], "replace_reviewer")
        self.assertEqual(payload["source_turn_count"], 1)
        self.assertTrue(payload["compaction_preferred"])


if __name__ == "__main__":
    unittest.main()
