from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

from harness import ROOT, cleanup_repo, commit_all, init_repo, run_process

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

from codex_builder_loop.assurance_v4 import core, driver  # noqa: E402
from codex_builder_loop.assurance_v4.models import digest  # noqa: E402
from codex_builder_loop.native_driver.coordinator import (  # noqa: E402
    NativeCoordinator,
    NativeDriverError,
)
from tests.test_native_driver_v1 import native_contract, root_native_contract  # noqa: E402


class BuilderRecoveryFacadeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_root_builder_facade_applies_and_replays_one_result(self) -> None:
        run_id = "root-builder-facade"
        session_id = "root-facade-session"
        core.start(
            self.repo,
            run_id,
            session_id,
            root_native_contract(self.repo),
            driver_runtime={
                "kind": "native",
                "protocol_version": 1,
                "transport": "root_session",
                "runtime_version": "root-session",
                "protocol_schema_digest": "a" * 64,
                "protocol_canary_digest": None,
                "root_session_identity": {
                    "session_id": session_id,
                    "agent_id": f"codex-root-session:{session_id}",
                    "identity_digest": digest(
                        {
                            "mode": "root_session",
                            "session_id": session_id,
                            "agent_id": f"codex-root-session:{session_id}",
                        }
                    ),
                },
                "native_transport": None,
            },
        )
        action = driver.next_action(self.repo, run_id)
        core.prepare_builder(
            self.repo,
            run_id,
            f"codex-root-session:{session_id}",
            owner_mode="root_session",
            session_id=session_id,
        )
        action = driver.next_action(self.repo, run_id)
        core.begin_dispatch(
            self.repo,
            run_id,
            action_id=action["action_id"],
            action=action["action"],
            role="builder",
            thread_id=None,
            owner_session_id=session_id,
            prompt_digest="b" * 64,
            output_schema_digest="c" * 64,
            driver_runtime_kind="native",
        )
        result = {
            "result": "implemented",
            "evidence_report": None,
            "proof_spec": None,
            "problem_report": None,
        }
        command = run_process(
            [
                sys.executable,
                ROOT / "scripts" / "codex-builder-loop.py",
                "assurance",
                "--experimental-v4",
                "apply-root-builder-result",
                "--repo",
                self.repo,
                "--run",
                run_id,
                "--action-id",
                action["action_id"],
                "--owner-session-id",
                session_id,
                "--result",
                "-",
            ],
            cwd=self.repo,
            input_text=json.dumps(result),
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        applied = json.loads(command.stdout.splitlines()[-1])
        context = core.driver_context(self.repo, run_id)
        self.assertIsNone(context["dispatch_intent"])
        self.assertEqual(applied["next_action"]["status"], "CONTINUE")
        self.assertEqual(
            [
                event["kind"]
                for event in json.loads(
                    (self.repo / ".git" / "builder-loop-assurance-v4" / "runs" / run_id / "ledger.json").read_text()
                )["events"]
                if event["kind"] in {"root_builder_result_applied", "dispatch_consumed"}
            ],
            ["root_builder_result_applied", "dispatch_consumed"],
        )
        replayed = core.apply_root_builder_result(
            self.repo,
            run_id,
            action_id=action["action_id"],
            owner_session_id=session_id,
        )
        self.assertIsNone(replayed["dispatch_intent"])

    def test_root_facade_rejects_native_dispatch_before_completion(self) -> None:
        run_id = "root-facade-native-rejection"
        session_id = "native-session"
        core.start(
            self.repo,
            run_id,
            session_id,
            native_contract(self.repo),
            driver_runtime={
                "kind": "native",
                "protocol_version": 1,
                "transport": "codex_app_server",
                "runtime_version": "test",
                "protocol_schema_digest": "a" * 64,
                "protocol_canary_digest": None,
                "native_transport": None,
                "root_session_identity": None,
            },
        )
        action = driver.next_action(self.repo, run_id)
        core.prepare_builder(
            self.repo,
            run_id,
            "native-builder",
            thread_id="native-builder-thread",
            owner_mode="native_thread",
        )
        action = driver.next_action(self.repo, run_id)
        core.begin_dispatch(
            self.repo,
            run_id,
            action_id=action["action_id"],
            action=action["action"],
            role="builder",
            thread_id="native-builder-thread",
            prompt_digest="b" * 64,
            output_schema_digest="c" * 64,
            driver_runtime_kind="native",
        )
        with self.assertRaises(core.AssuranceError) as raised:
            core.apply_root_builder_result(
                self.repo,
                run_id,
                action_id=action["action_id"],
                owner_session_id=session_id,
                result_value={
                    "result": "implemented",
                    "evidence_report": None,
                    "proof_spec": None,
                    "problem_report": None,
                },
            )
        self.assertEqual(raised.exception.code, "ROOT_BUILDER_MODE_REQUIRED")
        self.assertEqual(
            core.driver_context(self.repo, run_id)["dispatch_intent"]["state"],
            "prepared",
        )

    def test_reviewer_replacement_retires_exhausted_dispatch_and_identity(self) -> None:
        run_id = "reviewer-replacement-facade"
        session_id = "reviewer-replacement-session"
        contract = native_contract(self.repo)
        contract["assurance"]["required"] = ["reviewer"]
        core.start(
            self.repo,
            run_id,
            session_id,
            contract,
            driver_runtime={
                "kind": "native",
                "protocol_version": 1,
                "transport": "codex_app_server",
                "runtime_version": "test",
                "protocol_schema_digest": "a" * 64,
                "protocol_canary_digest": None,
                "native_transport": None,
                "root_session_identity": None,
            },
        )
        driver.next_action(self.repo, run_id)
        core.prepare_builder(
            self.repo,
            run_id,
            "builder-agent",
            thread_id="builder-thread",
            owner_mode="native_thread",
        )
        candidate = Path(core.driver_context(self.repo, run_id)["candidate_worktree"])
        (candidate / "src" / "reviewer_fixture.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        commit_all(candidate, "reviewer replacement candidate")
        checkpoint = driver.next_action(self.repo, run_id)
        core.checkpoint_builder(
            self.repo,
            run_id,
            action_id=checkpoint["action_id"],
        )
        scan = driver.next_action(self.repo, run_id)
        if scan["action"] == "scan_doc_references":
            core.scan_doc_references(self.repo, run_id)
        review = driver.next_action(self.repo, run_id)
        self.assertEqual(review["action"], "reviewer_final")
        core.prepare_reviewer(
            self.repo,
            run_id,
            "reviewer-old",
            "reviewer-old-thread",
        )
        review = driver.next_action(self.repo, run_id)
        core.begin_dispatch(
            self.repo,
            run_id,
            action_id=review["action_id"],
            action=review["action"],
            role="reviewer",
            thread_id="reviewer-old-thread",
            prompt_digest="b" * 64,
            output_schema_digest="c" * 64,
            driver_runtime_kind="native",
        )
        for index in range(3):
            core.bind_dispatch_turn(
                self.repo,
                run_id,
                action_id=review["action_id"],
                turn_id=f"reviewer-old-turn-{index + 1}",
            )
            if index < 2:
                core.retry_dispatch(
                    self.repo,
                    run_id,
                    action_id=review["action_id"],
                    failure_code="responseStreamDisconnected",
                )
        with self.assertRaises(core.AssuranceError):
            core.retry_dispatch(
                self.repo,
                run_id,
                action_id=review["action_id"],
                failure_code="responseStreamDisconnected",
            )
        ledger = json.loads(
            (
                self.repo
                / ".git"
                / "builder-loop-assurance-v4"
                / "runs"
                / run_id
                / "ledger.json"
            ).read_text()
        )
        pending = ledger["dispatch_intent"]
        replacement = core.begin_reviewer_replacement(
            self.repo,
            run_id,
            action_id=review["action_id"],
            source_generation=pending["generation"],
            source_attempt=pending["attempt"],
            failure_code=pending["failure_code"],
            thread_id=pending["thread_id"],
            turn_id=pending["turn_id"],
            prompt_digest=pending["prompt_digest"],
            output_schema_digest=pending["output_schema_digest"],
            dispatch_observation_digest=pending["dispatch_observation_digest"],
            candidate_head=ledger["facets"]["execution"]["candidate_head"],
            thread_observation_digest="d" * 64,
        )
        self.assertEqual(
            replacement["reviewer_replacement_intent"]["stage"], "prepared"
        )
        self.assertEqual(
            driver.next_action(self.repo, run_id)["action"], "replace_reviewer"
        )
        core.bind_reviewer_replacement(
            self.repo,
            run_id,
            action_id=review["action_id"],
            agent_id="reviewer-new",
            thread_id="reviewer-new-thread",
        )
        completed = core.complete_reviewer_replacement(
            self.repo,
            run_id,
            action_id=review["action_id"],
        )
        self.assertIsNone(completed["dispatch_intent"])
        completed_context = core.driver_context(self.repo, run_id)
        self.assertEqual(
            completed_context["facets"]["execution"]["agents"]["reviewer"],
            {"agent_id": "reviewer-new", "thread_id": "reviewer-new-thread"},
        )
        self.assertIsNone(completed_context["reviewer_replacement_intent"])
        self.assertEqual(
            completed["telemetry"]["lifecycle"]["reviewer_replacements"], 1
        )

    def test_native_reviewer_replacement_requires_unavailable_compaction(self) -> None:
        action_id = "a" * 64
        candidate_head = "b" * 40
        turns = [
            {
                "id": "reviewer-turn-3",
                "status": "failed",
                "items": [],
                "error": {
                    "codexErrorInfo": {"responseStreamDisconnected": {}}
                },
            }
        ]
        observation = {
            "candidate_head": candidate_head,
            "target_start_head": "c" * 40,
            "evidence": {},
            "publication": None,
            "deployment_transaction": None,
        }
        pending = {
            "action_id": action_id,
            "action": "reviewer_final",
            "role": "reviewer",
            "state": "exhausted",
            "attempt": 3,
            "generation": 1,
            "failure_code": "responseStreamDisconnected",
            "thread_id": "reviewer-thread",
            "turn_id": "reviewer-turn-3",
            "prompt_digest": "d" * 64,
            "output_schema_digest": "e" * 64,
            "dispatch_observation_digest": digest(observation),
        }
        context = {
            "dispatch_intent": pending,
            "target_start_head": observation["target_start_head"],
            "evidence": {},
            "publication": None,
            "deployment_transaction": None,
            "facets": {
                "execution": {
                    "candidate_head": candidate_head,
                    "agents": {
                        "reviewer": {
                            "agent_id": "reviewer-old",
                            "thread_id": "reviewer-thread",
                        }
                    },
                }
            },
        }

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return context
                if command == "begin-reviewer-replacement":
                    return {"status": "ACTIVE"}
                raise AssertionError(command)

        class FakeTransport:
            def read_thread(self, thread_id: str) -> dict:
                if thread_id != "reviewer-thread":
                    raise AssertionError(thread_id)
                return {"turns": turns}

        core_port = FakeCore()
        coordinator = NativeCoordinator(
            repo=self.repo,
            run_id="reviewer-replacement-coordinator",
            core=core_port,
            transport=FakeTransport(),
            project_root=Path(__file__).resolve().parents[1],
            thread_compaction_available=False,
        )
        self.assertTrue(coordinator._start_reviewer_replacement(action_id))
        self.assertIn("begin-reviewer-replacement", core_port.calls)

        no_call_core = FakeCore()
        unavailable_for_test = NativeCoordinator(
            repo=self.repo,
            run_id="reviewer-replacement-compaction-available",
            core=no_call_core,
            transport=FakeTransport(),
            project_root=Path(__file__).resolve().parents[1],
            thread_compaction_available=True,
        )
        self.assertFalse(
            unavailable_for_test._start_reviewer_replacement(action_id)
        )
        self.assertEqual(no_call_core.calls, [])

    def test_native_reviewer_replacement_rechecks_old_thread_before_switch(self) -> None:
        old_turns = [
            {
                "id": "reviewer-turn-3",
                "status": "failed",
                "items": [],
                "error": {
                    "codexErrorInfo": {"responseStreamDisconnected": {}}
                },
            }
        ]
        replacement = {
            "action_id": "a" * 64,
            "source_generation": 1,
            "source_attempt": 3,
            "failure_code": "responseStreamDisconnected",
            "thread_id": "reviewer-thread",
            "turn_id": "reviewer-turn-3",
            "thread_observation_digest": digest(old_turns),
            "new_agent": None,
        }

        class NoCallCore:
            def call(self, command: str, *args: str, input_value=None):
                if command == "driver-context":
                    return {
                        "reviewer_replacement_intent": replacement,
                        "candidate_worktree": str(repo),
                    }
                raise AssertionError(command)

        class ChangedTransport:
            def read_thread(self, _thread_id: str) -> dict:
                return {
                    "turns": [
                        *old_turns,
                        {"id": "unrelated", "status": "completed", "items": []},
                    ]
                }

        # The source check is intentionally exercised through the private
        # coordinator helper; Core owns the persisted transaction itself.
        coordinator = NativeCoordinator(
            repo=self.repo,
            run_id="reviewer-replacement-source-drift",
            core=NoCallCore(),
            transport=ChangedTransport(),
            project_root=Path(__file__).resolve().parents[1],
        )
        with self.assertRaises(NativeDriverError) as raised:
            coordinator._assert_reviewer_replacement_source(replacement)
        self.assertEqual(
            raised.exception.code, "NATIVE_REVIEWER_REPLACEMENT_SOURCE_DRIFT"
        )

    def test_exhausted_reviewer_recovery_does_not_resume_old_thread(self) -> None:
        action_id = "f" * 64
        target_head = "1" * 40
        candidate_head = "2" * 40
        context_base = {
            "run_id": "reviewer-recovery-resume",
            "target_start_head": target_head,
            "candidate_worktree": str(self.repo),
            "publication": None,
            "evidence": {},
            "problems": [],
            "facets": {
                "mission": {
                    "revision": 1,
                    "objective": "Recover a Reviewer dispatch.",
                    "behaviors": [{"id": "recover", "description": "recover"}],
                    "interfaces": [],
                    "acceptance_cases": [
                        {
                            "id": "recover",
                            "description": "recover",
                            "observation": {
                                "surface_id": "surface",
                                "surface_description": "surface",
                                "execution_ids": ["command"],
                                "required_dimensions": ["verify"],
                            },
                        }
                    ],
                    "trust_boundaries": [],
                },
                "authority": {
                    "target_branch": "main",
                    "builder_write": ["src/**"],
                    "tester_write": ["tests/**"],
                },
                "assurance": {"required": ["reviewer"]},
                "execution": {
                    "agents": {
                        "reviewer": {
                            "agent_id": "old-reviewer",
                            "thread_id": "old-reviewer-thread",
                        }
                    },
                    "candidate_head": candidate_head,
                    "tester_source": None,
                },
            },
            "deployment_transaction": None,
            "doc_reference_scan": None,
            "doc_reference_scan_state": "not_required",
        }
        observation = {
            "candidate_head": candidate_head,
            "target_start_head": target_head,
            "evidence": {},
            "publication": None,
            "deployment_transaction": None,
        }
        pending = {
            "action_id": action_id,
            "action": "reviewer_final",
            "role": "reviewer",
            "state": "exhausted",
            "attempt": 3,
            "generation": 1,
            "failure_code": "responseStreamDisconnected",
            "thread_id": "old-reviewer-thread",
            "turn_id": "old-reviewer-turn",
            "prompt_digest": "3" * 64,
            "output_schema_digest": "4" * 64,
            "dispatch_observation_digest": digest(observation),
        }
        context = {**context_base, "dispatch_intent": pending}
        old_turns = [
            {
                "id": "old-reviewer-turn",
                "status": "failed",
                "items": [],
                "error": {
                    "codexErrorInfo": {"responseStreamDisconnected": {}}
                },
            }
        ]

        class FakeCore:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "driver-context":
                    return context
                if command == "begin-reviewer-replacement":
                    return {"status": "ACTIVE"}
                raise AssertionError(command)

        class ReadOnlyTransport:
            def __init__(self) -> None:
                self.resumed = False

            def read_thread(self, thread_id: str) -> dict:
                if thread_id != "old-reviewer-thread":
                    raise AssertionError(thread_id)
                return {"turns": old_turns}

            def resume_thread(self, **_kwargs) -> None:
                self.resumed = True

        fake_core = FakeCore()
        transport = ReadOnlyTransport()
        coordinator = NativeCoordinator(
            repo=self.repo,
            run_id="reviewer-recovery-resume",
            core=fake_core,
            transport=transport,
            project_root=Path(__file__).resolve().parents[1],
            thread_compaction_available=False,
        )
        action = {
            "action": "reviewer_final",
            "action_id": action_id,
            "reason": "reviewer_final_exhausted",
        }
        prompt = coordinator._prompt(action, "reviewer", context)
        pending["prompt_digest"] = digest(prompt)
        pending["output_schema_digest"] = coordinator.output_schema_digest
        self.assertIsNone(
            coordinator._recover_dispatch(pending, "reviewer", context, action)
        )
        self.assertFalse(transport.resumed)
        self.assertIn("begin-reviewer-replacement", fake_core.calls)


if __name__ == "__main__":
    unittest.main()
