"""Synthetic platform turns with real root-session/Core/Git delivery transactions."""
from pathlib import Path
from harness import ROOT, init_repo, commit_all, git
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.native_driver.app_server import TurnResult
from runtime.codex_builder_loop.native_driver.cli import _root_session_identity
from tests.test_assurance_v4_contract import contract_for
from tests import test_result_application_contract as application

ERROR = {"codexErrorInfo": "other", "message": "workspace routing discovery timed out"}


class RootRoutingFixture(application.ResultApplicationContractTest):
    def setUp(self):
        self.repo = init_repo({"src/calc.py": "def add(a, b):\n    return a + b - 1\n"})
        self.run_id = "root-routing-recovery"
        contract = contract_for(self.repo)
        contract["execution"].update(driver_enforced=True, agents={},
            builder_runtime={"schema_version": 1, "mode": "root_session"})
        contract["assurance"]["required"].insert(2, "proof")
        started = core.start(self.repo, self.run_id, "root-session", contract,
            driver_runtime={"kind": "native", "protocol_version": 1,
                "transport": "root_session", "runtime_version": "fixture",
                "protocol_schema_digest": "a" * 64,
                "root_session_identity": _root_session_identity("root-session")})
        self.candidate = Path(started["candidate_worktree"])
        action = driver.next_action(self.repo, self.run_id)
        core.prepare_builder(self.repo, self.run_id, "codex-root-session:root-session",
            owner_mode="root_session", session_id="root-session")
        self.action = driver.next_action(self.repo, self.run_id)
        core.begin_dispatch(self.repo, self.run_id, action_id=self.action["action_id"],
            action=self.action["action"], role="builder", thread_id=None,
            owner_session_id="root-session", prompt_digest="b" * 64, output_schema_digest="c" * 64,
            work_unit_id=self.action.get("work_unit_id"),
            context_projection_digest=self.action.get("context_projection_digest"))
        (self.candidate / "src/calc.py").write_text("def add(a, b):\n    return sum((a, b))\n")
        commit_all(self.candidate, "implement calculator")
        self.result = {"result": "implemented", "evidence_report": None,
                       "problem_report": None, "proof_spec": None}
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action["action_id"], result_value=self.result, owner_session_id="root-session")
        core.apply_dispatch_result(self.repo, self.run_id, action_id=self.action["action_id"],
            driver_runtime_kind="native", owner_session_id="root-session")
        core.scan_doc_references(self.repo, self.run_id)
        core.prepare_tester(self.repo, self.run_id, "tester", "tester-thread")
        self.action = self.begin("tester")

    def begin(self, role):
        from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
        from runtime.codex_builder_loop.native_driver.core_port import CorePort
        from runtime.codex_builder_loop.assurance_v4.models import digest
        action = driver.next_action(self.repo, self.run_id)
        coordinator = NativeCoordinator(repo=self.repo, run_id=self.run_id, core=CorePort(),
            transport=None, project_root=ROOT, builder_mode="root_session", root_session_id="root-session")
        context = coordinator._context()
        prompt = coordinator._prompt(action, role, context)
        core.begin_dispatch(self.repo, self.run_id, action_id=action["action_id"], action=action["action"],
            role=role, thread_id=role + "-thread", prompt_digest=digest(prompt),
            output_schema_digest=coordinator.output_schema_digest,
            work_unit_id=action.get("work_unit_id"),
            context_projection_digest=action.get("context_projection_digest"))
        return action

    error = ERROR

    def fail_turn(self):
        intent = read_ledger(self.repo, self.run_id)["dispatch_intent"]
        turn_id = f"turn-g{intent['generation']}-a{intent['attempt']}"
        core.bind_dispatch_turn(self.repo, self.run_id, action_id=self.action["action_id"], turn_id=turn_id)
        return TurnResult(turn_id=turn_id, status="failed", text="", error=dict(self.error))

    def finish_author(self):
        ledger = read_ledger(self.repo, self.run_id)
        source = ledger["facets"]["execution"]["tester_source"]
        worktree = Path(source["worktree"])
        (worktree / "tests/test_recovery.py").write_text(
            "import unittest\nfrom src.calc import add\n"
            "class RecoveryTest(unittest.TestCase):\n"
            "    def test_negative(self):\n        self.assertEqual(add(-1, -2), -3)\n",
            encoding="utf-8")
        (worktree / "tests/__init__.py").write_text("", encoding="utf-8")
        source_head = commit_all(worktree, "author negative calculator test")
        self.result = {
            "result": "tests_ready", "problem_report": None, "proof_spec": None,
            "evidence_report": {
                "schema_version": 1, "kind": "tester", "status": "pass",
                "candidate_head": ledger["facets"]["execution"]["candidate_head"],
                "producer": {"role": "tester", "agent_id": "tester", "thread_id": "tester-thread"},
                "details": {"result": "tests_ready", "source_head": source_head, "files": [
                    {"path": "tests/test_recovery.py", "blob": git(worktree, "rev-parse", source_head + ":tests/test_recovery.py")}
                ]},
            },
        }
        core.complete_dispatch(self.repo, self.run_id, action_id=self.action["action_id"],
                               result_value=self.result)

        self.apply()
        core.scan_doc_references(self.repo, self.run_id)


class FailedTurnTransport:
    def __init__(self, turn):
        self.thread = {"id": "tester-thread", "turns": [
            {"id": turn.turn_id, "status": "failed", "items": [], "error": turn.error}]}

    def read_thread(self, thread_id):
        return self.thread
