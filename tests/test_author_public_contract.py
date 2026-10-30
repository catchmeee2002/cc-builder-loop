"""Public author input is independent of private recovery facts at every typed container."""
from __future__ import annotations

import copy
import json
import unittest

from tests import test_role_input_boundary as boundary
from runtime.codex_builder_loop.assurance_v4 import role_input as view


class AuthorPublicContractTest(unittest.TestCase):
    def setUp(self):
        self.fixture = boundary.RoleInputBoundaryTest()
        self.fixture.setUp()

    def test_real_native_prompt_excludes_successor_lineage_in_author_and_fix(self):
        f = self.fixture
        f.context["facets"]["mission"]["supersedes"] = {
            "run_id": "private-predecessor", "revision": 3,
            "mission_digest": "a" * 64, "candidate_head": "b" * 40,
        }
        for action in view.AUTHOR_ACTIONS:
            with self.subTest(action=action):
                before = copy.deepcopy(f.context)
                original = f.prompt({**f.action, "action": action})
                self.assertNotIn("supersedes", original["contract"]["mission"])
                self.assertNotIn("private-predecessor", json.dumps(original))
                self.assertEqual(before, f.context)
                f.context["facets"]["mission"]["supersedes"]["candidate_head"] = "c" * 40
                self.assertEqual(original, f.prompt({**f.action, "action": action}))
                # No broad string scrubbing: a public requirement containing the same
                # bytes is still an immutable user requirement, not a metadata channel.
                f.context["facets"]["mission"]["objective"] += " literal " + "c" * 40
                changed = f.prompt({**f.action, "action": action})
                self.assertIn("c" * 40, changed["contract"]["mission"]["objective"])
                self.assertNotEqual(original["role_input_digest"], changed["role_input_digest"])

    def test_unknown_nested_coordinator_fields_cannot_enter_public_objects(self):
        f = self.fixture
        f.context["facets"]["mission"]["acceptance_cases"][0]["observation"] = {
            "surface_id": "local-http", "surface_description": "Notification",
            "execution_ids": ["notify"], "required_dimensions": ["verify"],
        }
        f.context["publication"] = {
            "required": True, "generation": 1, "head": "3" * 40, "tree": "4" * 40,
            "paths": ["public.py"], "files": [{"path": "public.py", "blob": "5" * 40}],
            "manifest_digest": "6" * 64,
        }
        f.action["work_unit"].update(depends_on=[], scope={
            "paths": ["tests/test_x.py"], "behavior_ids": ["b"], "command_ids": []
        }, completion={"kind": "tester_source", "required_observations": ["tester_source_commit"]})
        deletion = {"path": "removed.py", "blob": None, "operation": "delete",
                    "base_blob": "7" * 40, "base_mode": "100644"}
        f.context["publication"]["files"].append(deletion)
        original = f.prompt()
        self.assertEqual(original["publication"]["files"][-1], deletion)
        def inject(value):
            if isinstance(value, dict):
                for child in list(value.values()):
                    inject(child)
                value["future_private_metadata"] = "PRIVATE-CANDIDATE"
            elif isinstance(value, list):
                for child in value:
                    inject(child)
        # These are all typed public containers, not schema documents or free-form text.
        for field in ("mission", "authority", "assurance", "execution"):
            inject(f.context["facets"][field])
        inject(f.context["publication"])
        inject(f.action["work_unit"])
        changed = f.prompt()
        self.assertNotIn("PRIVATE-CANDIDATE", json.dumps(changed))
        self.assertEqual(original, changed)

    def test_every_current_contract_field_has_an_explicit_visibility_decision(self):
        # New fields must receive a reviewed classification; schema growth cannot
        # silently make the whole containing facet public again.
        schema = json.loads((boundary.ROOT / "schema/assurance-v4-contract.schema.json").read_text())["$defs"]
        decisions = {
            "mission": set(view.MISSION_FIELDS) | {"supersedes"},
            "authority": set(view.AUTHORITY_FIELDS) | {"dirty_intake"},
            "assurance": set(view.ASSURANCE_FIELDS),
            "execution": set(view.AUTHOR_EXECUTION_FIELDS) | {
                "tester_source", "agents", "version", "candidate_head", "builder_files",
                "tester_files", "builder_runtime", "work_units", "cost_ancestry", "continuation",
                "carryover", "revision_transition", "prior_problem_dispositions", "prior_problems",
                "deployment", "dirty_snapshot",
            },
            "statement": set(view.STATEMENT_FIELDS),
            "acceptanceCase": set(view.STATEMENT_FIELDS) | {"observation"},
            "observation": set(view.OBSERVATION_FIELDS),
            "externalTarget": set(view.STATEMENT_FIELDS),
            "command": set(view.COMMAND_FIELDS),
            "agent": set(view.AGENT_FIELDS),
            "testerSource": set(view.SOURCE_FIELDS),
            "workUnit": set(view.WORK_UNIT_FIELDS),
            "workUnitScope": {"paths", "behavior_ids", "command_ids"},
            "workUnitCompletion": {"kind", "required_observations"},
            "progressPolicy": {"schema_version", "mode", "attempt_limit", "rehydration_limit",
                               "max_units_per_thread", "context_source"},
        }
        ledger_schema = json.loads((boundary.ROOT / "schema/assurance-v4-ledger.schema.json").read_text())["$defs"]
        self.assertEqual(set(ledger_schema["publication"]["properties"]), set(view.PUBLICATION_FIELDS) | {"candidate_head"})
        self.assertEqual(set(ledger_schema["publicationFile"]["properties"]), set(view.MANIFEST_FIELDS))
        for name, known in decisions.items():
            with self.subTest(container=name):
                self.assertEqual(set(schema[name]["properties"]), known)
        recovery_fields = set().union(*(set(v["properties"]) for v in schema["recoveryPolicy"]["oneOf"]))
        self.assertEqual(recovery_fields, {"schema_version", "mode", "continuation_window"})
        assurance = schema["assurance"]["properties"]
        self.assertEqual(set(assurance["proof_runners"]["items"]["properties"]), {"id", "argv", "timeout_seconds"})
        self.assertEqual(set(assurance["evidence_neutral_paths"]["properties"]), {"machine", "proof"})
        for field in ("files", "replaces_files"):
            self.assertEqual(set(schema["testerSource"]["properties"][field]["items"]["properties"]), set(view.MANIFEST_FIELDS))

    def test_public_behavior_interfaces_acceptance_and_scope_remain_complete(self):
        f = self.fixture
        original = f.prompt()
        for field in ("behaviors", "interfaces", "trust_boundaries", "acceptance_cases"):
            with self.subTest(field=field):
                context = copy.deepcopy(f.context)
                context["facets"]["mission"][field].append({"id": "new-obligation", "description": "Must remain visible"})
                changed = f.prompt(context=context)
                self.assertEqual(changed["contract"]["mission"][field], context["facets"]["mission"][field])
                self.assertNotEqual(original["role_input_digest"], changed["role_input_digest"])
