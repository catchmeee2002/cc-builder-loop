import unittest
from runtime.codex_builder_loop.assurance_v4.core import role_observations


class ProofDiscriminationTest(unittest.TestCase):
    def test_red_green_error_and_missing_remain_distinct(self):
        records = [{"id": "red", "outcome": "failed", "failure_kind": "assertion"}, {"id": "green", "outcome": "passed"}, {"id": "error", "outcome": "failed", "failure_kind": "non-assertion"}]
        ledger = {"facets": {"execution": {"candidate_head": "head"}}, "evidence": {"proof": {"details": {"results": [{"method": "mutation", "test_ids": ["red", "green", "error", "missing"], "mutation": {"supervisor_observation": {"tests": records}}}]}}}}
        rows = role_observations(ledger)["proof_discrimination"]
        self.assertEqual([r["state"] for r in rows], ["discriminates", "does_not_discriminate", "invalid_observation", "missing"])
        self.assertNotIn("role_observations", ledger)

    def test_reviewed_boundary_does_not_invent_mutation_observations(self):
        ledger = {"facets": {"execution": {}}, "evidence": {"proof": {"details": {"results": [{"method": "reviewed-boundaries", "test_ids": ["one"]}]}}}}
        self.assertEqual(role_observations(ledger)["proof_discrimination"][0]["state"], "not_applicable")
