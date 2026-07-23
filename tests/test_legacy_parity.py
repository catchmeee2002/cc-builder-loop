from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LegacyParityContractTest(unittest.TestCase):
    def test_every_legacy_fixture_has_one_frozen_disposition(self) -> None:
        path = ROOT / "tests" / "fixtures" / "legacy_feature_parity.json"
        entries = json.loads(path.read_text())
        self.assertEqual(len(entries), 51)
        fixtures = [item["fixture"] for item in entries]
        self.assertEqual(len(fixtures), len(set(fixtures)))
        self.assertTrue(all(name.startswith("test-") and name.endswith(".sh") for name in fixtures))
        counts = {status: 0 for status in ("covered", "rescue", "retired")}
        for item in entries:
            counts[item["status"]] += 1
            self.assertTrue(item["replacement"])
            self.assertTrue(item["rationale"])
            self.assertTrue(item["test_ids"])
        self.assertEqual(counts, {"covered": 28, "rescue": 8, "retired": 15})


if __name__ == "__main__":
    unittest.main()
