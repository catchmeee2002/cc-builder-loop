from __future__ import annotations

import json
import unittest
from pathlib import Path

from runtime.codex_builder_loop.assurance_v4 import core
from tests import test_result_application_contract as application_tests
from tests.helpers.native_delivery_recovery import complete_delivery, kill_after_saved_event


class NativeDeliveryRecoveryTest(unittest.TestCase):
    def test_recovery_corpus_reaches_real_finalization(self):
        corpus = json.loads((Path(__file__).parent / "fixtures/codex_delivery_recovery.json").read_text())
        for case in corpus["cases"]:
            with self.subTest(case=case["id"]):
                fixture = application_tests.ResultApplicationContractTest()
                fixture.setUp()
                try:
                    fixture.prepare_tester_result()
                    if case["id"] == "integration-sigkill":
                        kill_after_saved_event(fixture, "tester_source_integrated")
                    fixture.apply()
                    core.scan_doc_references(fixture.repo, fixture.run_id)
                    fixture.action = fixture.begin("tester")
                    fixture.result = fixture.proof_result(invalid=case["id"] == "proof-spec-correction")
                    core.complete_dispatch(fixture.repo, fixture.run_id,
                                           action_id=fixture.action["action_id"], result_value=fixture.result)
                    fixture.apply()
                    if case["id"] == "proof-spec-correction":
                        fixture.action = fixture.begin("tester")
                        fixture.assertEqual(fixture.action["action"], "tester_proof_diagnose")
                        fixture.result = fixture.proof_result()
                        core.complete_dispatch(fixture.repo, fixture.run_id,
                                               action_id=fixture.action["action_id"], result_value=fixture.result)
                        fixture.apply()
                    complete_delivery(fixture, kill_reviewer=case["id"] == "reviewer-application-sigkill")
                finally:
                    fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
