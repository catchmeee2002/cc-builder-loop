"""Public authorization guidance survives actual CLI dispatch consumption and projection."""
import copy
import json
import unittest
from pathlib import Path
from tests import test_full_driver_v4_contract as fixture
from runtime.codex_builder_loop.assurance_v4 import core, driver, role_input
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator


class TesterAuthorGuidanceTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FullDriverV4ContractTest()
        self.f.setUp()
        self.repo = self.f.repo
        self.run = "public-guidance"
        contract = fixture.contract_for(self.repo)
        contract["execution"]["driver_enforced"] = False
        path = self.f.write_json("contract.json", contract)
        rc, result = self.f.invoke("start", "--repo", self.repo, "--run", self.run,
            "--session-id", "owner", "--contract", path, "--driver-kind", "native",
            "--driver-transport", "codex_app_server", "--driver-runtime-version", "fixture",
            "--driver-protocol-schema-digest", "a" * 64)
        self.assertEqual(rc, 0, result)
        self.run_path = Path(result["candidate_worktree"]).parent
        ledger = self.f.load_ledger(self.run_path)
        ledger["problems"].append({"key": "fixture-scope", "owner": "tester", "status": "open",
            "summary": "Correction", "details": "Old suggestion: compare unrelated counts.",
            "producer": {"role": "tester", **ledger["facets"]["execution"]["agents"]["tester"]},
            "candidate_head": ledger["facets"]["execution"]["candidate_head"], "recorded_at": core.now()})
        for i in range(3):
            aid = f"{i+1:064x}"
            ledger["events"].extend([
                {"at": core.now(), "kind": "dispatch_prepared", "details": {"action": "tester_fix", "action_id": aid}},
                {"at": core.now(), "kind": "dispatch_consumed", "details": {"action_id": aid}}])
        core.save_ledger(self.repo, ledger)
        self.before = self.f.load_ledger(self.run_path)
        self.blocked = driver.next_action(self.repo, self.run)
        self.assertEqual(self.blocked["reason"], "tester_correction_limit_reached")
        self.text = "公开纠正：\n按独立夹具核对身份、内容和数量；不要假定两次 event_count 相等。\n保留首次再升级内容与无重放检查。"

    def tearDown(self):
        self.f.tearDown()

    def authorize(self, text=None, *, action_id=None):
        args = ["--repo", self.repo, "--run", self.run,
            "--action-id", action_id or self.blocked["action_id"],
            "--review-binding", self.blocked["tester_correction_review_binding"],
            "--reason", "AUDIT_ONLY candidate/private-path runtime-secret",
            "--driver-runtime-kind", "native"]
        if text is not None:
            args += ["--author-instructions", text]
        return self.f.invoke("authorize-tester-correction", *args)

    def prompt(self, action):
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.run, core=object(),
            transport=object(), project_root=fixture.ROOT)
        return json.loads(coordinator._prompt(action, "tester", core.driver_context(self.repo, self.run)).split("\n", 1)[1])

    def test_cli_guidance_survives_consumption_and_rebuilt_native_prompt(self):
        rc, result = self.authorize(self.text)
        self.assertEqual(rc, 0, result)
        action = driver.next_action(self.repo, self.run)
        first = self.prompt(action)
        self.assertEqual(first["tester_correction_instructions"], {"text": self.text})
        self.assertNotIn("AUDIT_ONLY", json.dumps(first))
        self.assertNotIn("runtime-secret", json.dumps(first))
        self.assertNotIn("tester_correction_authorization", first)
        public = role_input.role_context(self.repo, self.run, action["action_id"])
        self.assertEqual(public["tester_correction_instructions"], {"text": self.text})
        core.begin_dispatch(self.repo, self.run, action_id=action["action_id"], action="tester_fix",
            role="tester", thread_id=action["agent"]["thread_id"],
            prompt_digest="b" * 64, output_schema_digest="c" * 64)
        pending = driver.next_action(self.repo, self.run)
        self.assertEqual(self.prompt(pending), first)
        core.bind_dispatch_turn(self.repo, self.run, action_id=action["action_id"], turn_id="turn-guidance")
        self.assertEqual(self.prompt(driver.next_action(self.repo, self.run)), first)
        core.retry_dispatch(self.repo, self.run, action_id=action["action_id"],
            failure_code="DECISION_REQUEST_INVALID", failure_details={"cause": "DECISION_CHANGES_REQUIRED"})
        self.assertEqual(self.prompt(driver.next_action(self.repo, self.run)), first)
        after = self.f.load_ledger(self.run_path)
        self.assertEqual(after["facets"], self.before["facets"])
        self.assertEqual(after["problems"], self.before["problems"])
        self.assertEqual(sum(e["kind"] == "tester_correction_authorization_consumed" for e in after["events"]), 1)
        path = self.run_path / "ledger.json"
        original = path.read_bytes()
        self.assertEqual(self.authorize(self.text)[0], 0)
        self.assertEqual(path.read_bytes(), original)
        rc, rejected = self.authorize(self.text + " Changed")
        self.assertNotEqual(rc, 0)
        self.assertEqual(rejected["code"], "TESTER_CORRECTION_AUTHORIZATION_CONFLICT")
        self.assertEqual(path.read_bytes(), original)

    def test_omitted_guidance_keeps_legacy_authorization_private(self):
        self.assertEqual(self.authorize()[0], 0)
        action = driver.next_action(self.repo, self.run)
        self.assertNotIn("tester_correction_instructions", self.prompt(action))
        self.assertNotIn("AUDIT_ONLY", json.dumps(self.prompt(action)))

    def test_blank_and_stale_guidance_do_not_mutate_ledger(self):
        path = self.run_path / "ledger.json"
        before = path.read_bytes()
        for text, aid, code in [("  ", None, "TESTER_CORRECTION_INSTRUCTIONS_INVALID"),
                                (self.text, "f" * 64, "TESTER_CORRECTION_REVIEW_STALE")]:
            rc, result = self.authorize(text, action_id=aid)
            self.assertNotEqual(rc, 0)
            self.assertEqual(result["code"], code)
            self.assertEqual(path.read_bytes(), before)

    def test_projection_excludes_nested_metadata_and_fresh_author_guidance(self):
        self.assertEqual(self.authorize(self.text)[0], 0)
        action = driver.next_action(self.repo, self.run)
        value = self.prompt(action)
        value["tester_correction_instructions"]["candidate_head"] = "secret-candidate"
        projected = role_input.project_role_input(value)
        self.assertEqual(projected["tester_correction_instructions"], {"text": self.text})
        self.assertNotIn("secret-candidate", json.dumps(projected))
        original_digest = projected["role_input_digest"]
        projected["tester_correction_instructions"]["text"] += " More public guidance"
        self.assertNotEqual(role_input.project_role_input(projected)["role_input_digest"], original_digest)
        projected["action"] = "tester_author"
        self.assertNotIn("tester_correction_instructions", role_input.project_role_input(projected))
