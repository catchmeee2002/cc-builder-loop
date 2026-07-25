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
            for test_id in item["test_ids"]:
                self.assertTrue(
                    (ROOT / "tests" / f"{test_id}.py").is_file(),
                    f"legacy disposition references missing test module: {test_id}",
                )
        self.assertEqual(counts, {"covered": 28, "rescue": 8, "retired": 15})

        by_fixture = {item["fixture"]: item for item in entries}
        self.assertEqual(
            by_fixture["test-extract-e2e-cases.sh"]["replacement"],
            "canonical v3 structured e2e-cases marker",
        )
        self.assertIn(
            "test_planning_v3_e2e_contract",
            by_fixture["test-old-state-compat.sh"]["test_ids"],
        )


if __name__ == "__main__":
    unittest.main()
