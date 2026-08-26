from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from harness import cleanup_repo, commit_all, git, init_repo
from runtime.codex_builder_loop.assurance_v4 import core, driver
from runtime.codex_builder_loop.assurance_v4.models import ContractError, digest
from runtime.codex_builder_loop.assurance_v4.store import read_ledger
from runtime.codex_builder_loop.native_driver.coordinator import NativeCoordinator
from runtime.codex_builder_loop.native_driver.core_port import CorePort
from tests.test_assurance_v4_contract import contract_for


class WorkUnitContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def test_new_contract_requires_progress_policy_and_work_units(self) -> None:
        contract = contract_for(self.repo)
        contract["execution"].pop("progress_policy")
        with self.assertRaises(ContractError) as raised:
            core.validate_new_contract(contract)
        self.assertEqual(raised.exception.code, "PROGRESS_POLICY_REQUIRED")

        contract = contract_for(self.repo)
        contract["execution"].pop("work_units")
        with self.assertRaises(ContractError) as raised:
            core.validate_new_contract(contract)
        self.assertEqual(raised.exception.code, "WORK_UNITS_REQUIRED")

    def test_work_unit_dependency_cycle_is_rejected(self) -> None:
        contract = contract_for(self.repo)
        units = contract["execution"]["work_units"]
        units[0]["depends_on"] = ["reviewer-work-unit"]
        units[2]["depends_on"] = ["builder-work-unit"]
        with self.assertRaises(ContractError) as raised:
            core.validate_new_contract(contract)
        self.assertEqual(raised.exception.code, "WORK_UNIT_DEPENDENCY_CYCLE")

    def test_work_unit_scope_can_reference_deployment_commands(self) -> None:
        contract = contract_for(self.repo)
        contract["authority"]["external_targets"] = [
            {"id": "fixture-target", "description": "Disposable target."}
        ]

        def command(command_id: str) -> dict[str, object]:
            return {
                "id": command_id,
                "argv": ["bash", command_id],
                "timeout_seconds": 30,
            }

        contract["execution"]["deployment"] = {
            "target_id": "fixture-target",
            "artifact_path": "dist/app.bin",
            "build_command": command("deployment-build"),
            "deploy_command": command("deployment-deploy"),
            "probe_command": command("deployment-probe"),
            "restore_command": command("deployment-restore"),
        }
        contract["execution"]["work_units"][0]["scope"]["command_ids"] = [
            "deployment-build",
            "deployment-deploy",
            "deployment-probe",
            "deployment-restore",
        ]
        validated = core.validate_new_contract(contract)
        self.assertEqual(
            validated["execution"]["work_units"][0]["scope"]["command_ids"],
            [
                "deployment-build",
                "deployment-deploy",
                "deployment-probe",
                "deployment-restore",
            ],
        )

        contract["execution"]["work_units"][0]["scope"]["command_ids"] = [
            "deployment-missing"
        ]
        with self.assertRaises(ContractError) as raised:
            core.validate_new_contract(contract)
        self.assertEqual(raised.exception.code, "WORK_UNIT_SCOPE_REFERENCE_INVALID")

    def test_work_unit_projection_digest_is_core_verified(self) -> None:
        run_id = "work-unit-projection-verified"
        core.start(
            self.repo,
            run_id,
            "work-unit-session",
            contract_for(self.repo),
            driver_runtime={
                "kind": "native",
                "protocol_version": 1,
                "transport": "codex_app_server",
                "runtime_version": "work-unit-test",
                "protocol_schema_digest": "a" * 64,
            },
        )
        action = driver.next_action(self.repo, run_id)
        core.prepare_builder(
            self.repo,
            run_id,
            "builder-agent",
            "builder-thread",
            owner_mode="native_thread",
        )
        action = driver.next_action(self.repo, run_id)
        with self.assertRaises(core.AssuranceError) as raised:
            core.begin_dispatch(
                self.repo,
                run_id,
                action_id=action["action_id"],
                action=action["action"],
                role="builder",
                thread_id="builder-thread",
                prompt_digest="b" * 64,
                output_schema_digest="c" * 64,
                work_unit_id=action["work_unit_id"],
                context_projection_digest="f" * 64,
                driver_runtime_kind="native",
            )
        self.assertEqual(
            raised.exception.code, "WORK_UNIT_PROJECTION_DIGEST_MISMATCH"
        )
        self.assertIsNone(read_ledger(self.repo, run_id)["dispatch_intent"])

    def test_canonical_projection_excludes_physical_identity(self) -> None:
        run_id = "work-unit-canonical-projection"
        runtime = {
            "kind": "native",
            "protocol_version": 1,
            "transport": "codex_app_server",
            "runtime_version": "work-unit-test",
            "protocol_schema_digest": "a" * 64,
        }
        core.start(
            self.repo,
            run_id,
            "work-unit-session",
            contract_for(self.repo),
            driver_runtime=runtime,
        )
        action = driver.next_action(self.repo, run_id)
        ledger = read_ledger(self.repo, run_id)
        projection = core.canonical_context_projection(
            ledger,
            action_id=action["action_id"],
            action=action["action"],
            role="builder",
            work_unit_id=action["work_unit_id"],
        )
        serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        for value in (
            "assurance-v4-tester-thread",
            "assurance-v4-reviewer-thread",
            "builder-thread",
        ):
            self.assertNotIn(value, serialized)
        self.assertNotIn("agents", projection["facets"]["execution"])

        rotated = copy.deepcopy(ledger)
        rotated["facets"]["execution"]["agents"]["builder"] = {
            "agent_id": "rotated-agent",
            "thread_id": "rotated-thread",
        }
        rotated["facets"]["execution"]["version"] += 1
        self.assertEqual(
            core.canonical_context_projection_digest(
                ledger,
                action_id=action["action_id"],
                action=action["action"],
                role="builder",
                work_unit_id=action["work_unit_id"],
            ),
            core.canonical_context_projection_digest(
                rotated,
                action_id=action["action_id"],
                action=action["action"],
                role="builder",
                work_unit_id=action["work_unit_id"],
            ),
        )

    def test_rotation_mutations_require_native_runtime_owner(self) -> None:
        run_id = "work-unit-rotation-owner"
        core.start(
            self.repo,
            run_id,
            "work-unit-session",
            contract_for(self.repo),
            driver_runtime={
                "kind": "native",
                "protocol_version": 1,
                "transport": "codex_app_server",
                "runtime_version": "work-unit-test",
                "protocol_schema_digest": "a" * 64,
            },
        )
        with self.assertRaises(core.AssuranceError) as raised:
            core.begin_context_rotation(
                self.repo,
                run_id,
                role="builder",
                work_unit_id="builder-work-unit",
                context_projection_digest="a" * 64,
                driver_runtime_kind="full_driver_skill",
            )
        self.assertEqual(raised.exception.code, "DRIVER_RUNTIME_OWNER_MISMATCH")
        with self.assertRaises(core.AssuranceError) as raised:
            core.bind_context_rotation(
                self.repo,
                run_id,
                new_agent_id="new-agent",
                new_thread_id="new-thread",
                driver_runtime_kind="full_driver_skill",
            )
        self.assertEqual(raised.exception.code, "DRIVER_RUNTIME_OWNER_MISMATCH")

    def test_progress_is_derived_from_completion_events(self) -> None:
        contract = contract_for(self.repo)
        runtime = {
            "kind": "native",
            "protocol_version": 1,
            "transport": "codex_app_server",
            "runtime_version": "work-unit-test",
            "protocol_schema_digest": "a" * 64,
        }
        core.start(
            self.repo,
            "work-unit-progress",
            "work-unit-session",
            contract,
            driver_runtime=runtime,
        )
        before = read_ledger(self.repo, "work-unit-progress")
        progress = core.work_unit_progress(before)
        self.assertEqual(progress["completed"], [])
        self.assertEqual(progress["ready"], ["builder-work-unit", "tester-work-unit"])
        self.assertTrue(progress["parallel_ready"])
        self.assertEqual(
            progress["parallel_work_unit_ids"],
            ["builder-work-unit", "tester-work-unit"],
        )

        serial = copy.deepcopy(contract)
        serial["execution"]["work_units"][1]["depends_on"] = [
            "builder-work-unit"
        ]
        core.start(
            self.repo,
            "work-unit-serial-progress",
            "work-unit-session",
            serial,
            driver_runtime=runtime,
        )
        serial_ledger = read_ledger(self.repo, "work-unit-serial-progress")
        self.assertFalse(core.work_unit_progress(serial_ledger)["parallel_ready"])

        ledger = copy.deepcopy(before)
        ledger["events"].append(
            {
                "kind": "work_unit_completed",
                "at": ledger["updated_at"],
                "details": {
                    "work_unit_id": "builder-work-unit",
                    "role": "builder",
                    "completion_kind": "candidate_checkpoint",
                    "required_observation": "candidate_commit",
                    "thread_id": "builder-thread",
                },
            }
        )
        progress = core.work_unit_progress(ledger)
        self.assertEqual(progress["completed"], ["builder-work-unit"])
        self.assertEqual(progress["thread_usage"], {"builder-thread": 1})


class WorkUnitRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = init_repo()

    def tearDown(self) -> None:
        cleanup_repo(self.repo)

    def _runtime(self) -> dict[str, object]:
        return {
            "kind": "native",
            "protocol_version": 1,
            "transport": "codex_app_server",
            "runtime_version": "work-unit-test",
            "protocol_schema_digest": "a" * 64,
        }

    def test_builder_checkpoint_records_one_work_unit_and_keeps_other_facts(self) -> None:
        run_id = "work-unit-builder-checkpoint"
        started = core.start(
            self.repo,
            run_id,
            "work-unit-session",
            contract_for(self.repo),
            driver_runtime=self._runtime(),
        )
        core.prepare_builder(
            self.repo,
            run_id,
            "builder-agent",
            "builder-thread",
            owner_mode="native_thread",
        )
        action = driver.next_action(self.repo, run_id)
        self.assertEqual(action["action"], "builder_implement")
        self.assertEqual(action["work_unit_id"], "builder-work-unit")
        core.begin_dispatch(
            self.repo,
            run_id,
            action_id=action["action_id"],
            action=action["action"],
            role="builder",
            thread_id="builder-thread",
            prompt_digest="b" * 64,
            output_schema_digest="c" * 64,
            work_unit_id=action["work_unit_id"],
            context_projection_digest=core.canonical_context_projection_digest(
                read_ledger(self.repo, run_id),
                action_id=action["action_id"],
                action=action["action"],
                role="builder",
                work_unit_id=action["work_unit_id"],
            ),
            driver_runtime_kind="native",
        )
        core.complete_dispatch(
            self.repo,
            run_id,
            action_id=action["action_id"],
            result_value={
                "result": "implemented",
                "evidence_report": None,
                "proof_spec": None,
                "problem_report": None,
            },
        )
        worktree = Path(started["candidate_worktree"])
        (worktree / "src/calc.py").write_text(
            "def add(a, b):\n    return a + b + 0\n",
            encoding="utf-8",
        )
        commit_all(worktree, "feat(builder): [cr_id_skip] Complete Work Unit")
        core.checkpoint_builder(
            self.repo,
            run_id,
            action_id=action["action_id"],
        )
        core.consume_dispatch(
            self.repo,
            run_id,
            action_id=action["action_id"],
            consumer_source="native_driver",
        )
        ledger = read_ledger(self.repo, run_id)
        progress = core.work_unit_progress(ledger)
        self.assertEqual(progress["completed"], ["builder-work-unit"])
        self.assertEqual(
            [
                event["details"]["work_unit_id"]
                for event in ledger["events"]
                if event["kind"] == "work_unit_completed"
            ],
            ["builder-work-unit"],
        )
        self.assertTrue(ledger["builder_checkpointed"])

    def test_clean_exhausted_dispatch_rehydrates_once_without_resetting_history(self) -> None:
        run_id = "work-unit-rehydration"
        core.start(
            self.repo,
            run_id,
            "work-unit-session",
            contract_for(self.repo),
            driver_runtime=self._runtime(),
        )
        core.prepare_builder(
            self.repo,
            run_id,
            "old-builder-agent",
            "old-builder-thread",
            owner_mode="native_thread",
        )
        action = driver.next_action(self.repo, run_id)
        core.begin_dispatch(
            self.repo,
            run_id,
            action_id=action["action_id"],
            action=action["action"],
            role="builder",
            thread_id="old-builder-thread",
            prompt_digest="b" * 64,
            output_schema_digest="c" * 64,
            work_unit_id=action["work_unit_id"],
            context_projection_digest=core.canonical_context_projection_digest(
                read_ledger(self.repo, run_id),
                action_id=action["action_id"],
                action=action["action"],
                role="builder",
                work_unit_id=action["work_unit_id"],
            ),
            driver_runtime_kind="native",
        )
        core.bind_dispatch_turn(
            self.repo,
            run_id,
            action_id=action["action_id"],
            turn_id="turn-1",
        )
        for _ in range(3):
            try:
                core.retry_dispatch(
                    self.repo,
                    run_id,
                    action_id=action["action_id"],
                    failure_code="responseStreamDisconnected",
                )
            except core.AssuranceError as error:
                self.assertEqual(error.code, "NATIVE_DISPATCH_RETRY_EXHAUSTED")
        exhausted = read_ledger(self.repo, run_id)["dispatch_intent"]
        self.assertEqual(exhausted["state"], "exhausted")
        self.assertEqual(exhausted["attempt"], 3)
        self.assertEqual(exhausted["rehydration_count"], 0)

        core.begin_dispatch_rehydration(
            self.repo,
            run_id,
            action_id=action["action_id"],
            context_projection_digest=core.canonical_context_projection_digest(
                read_ledger(self.repo, run_id),
                action_id=action["action_id"],
                action=action["action"],
                role="builder",
                work_unit_id=action["work_unit_id"],
            ),
        )
        core.claim_dispatch_rehydration(
            self.repo,
            run_id,
            action_id=action["action_id"],
            claim_id="a" * 64,
        )
        unresolved = driver.next_action(self.repo, run_id)
        self.assertEqual(unresolved["status"], "NEEDS_USER")
        self.assertEqual(
            unresolved["reason"], "work_unit_rehydration_spawn_unresolved"
        )
        core.record_dispatch_rehydration_spawn(
            self.repo,
            run_id,
            action_id=action["action_id"],
            claim_id="a" * 64,
            new_agent_id="new-builder-agent",
            new_thread_id="new-builder-thread",
        )

        class ReadOnlyTransport:
            def __init__(self) -> None:
                self.starts = 0
                self.reads: list[str] = []

            def read_thread(self, thread_id: str) -> dict[str, object]:
                self.reads.append(thread_id)
                return {"id": thread_id, "turns": []}

            def start_thread(self, **_kwargs: object) -> str:
                self.starts += 1
                raise AssertionError("recorded spawn must not start another thread")

        transport = ReadOnlyTransport()
        native_core = CorePort()
        recovery_action = native_core.call(
            "driver-next", "--repo", str(self.repo), "--run", run_id
        )
        self.assertEqual(recovery_action["action"], "rehydrate_dispatch")
        coordinator = NativeCoordinator(
            repo=self.repo,
            run_id=run_id,
            core=native_core,
            transport=transport,
        )
        coordinator._rehydrate_dispatch(recovery_action)

        ledger = read_ledger(self.repo, run_id)
        pending = ledger["dispatch_intent"]
        self.assertEqual(pending["generation"], 2)
        self.assertEqual(pending["attempt"], 1)
        self.assertEqual(pending["unit_attempt"], 4)
        self.assertEqual(pending["rehydration_count"], 1)
        self.assertEqual(transport.starts, 0)
        self.assertEqual(transport.reads, ["new-builder-thread"])
        self.assertEqual(
            ledger["facets"]["execution"]["agents"]["builder"],
            {
                "agent_id": "new-builder-agent",
                "thread_id": "new-builder-thread",
            },
        )
        events = [
            event["kind"]
            for event in ledger["events"]
            if isinstance(event, dict)
        ]
        self.assertIn("dispatch_rehydration_prepared", events)
        self.assertIn("dispatch_rehydrated", events)

        with self.assertRaises(core.AssuranceError) as raised:
            core.begin_dispatch_rehydration(
                self.repo,
                run_id,
                action_id=action["action_id"],
                context_projection_digest="f" * 64,
            )
        self.assertEqual(raised.exception.code, "WORK_UNIT_REHYDRATION_STATE_INVALID")

    def test_rehydration_rejects_candidate_dirty_and_cannot_bypass_limit(self) -> None:
        run_id = "work-unit-rehydration-boundaries"
        started = core.start(
            self.repo,
            run_id,
            "work-unit-session",
            contract_for(self.repo),
            driver_runtime=self._runtime(),
        )
        core.prepare_builder(
            self.repo,
            run_id,
            "builder-agent",
            "builder-thread",
            owner_mode="native_thread",
        )
        action = driver.next_action(self.repo, run_id)
        projection = core.canonical_context_projection_digest(
            read_ledger(self.repo, run_id),
            action_id=action["action_id"],
            action=action["action"],
            role="builder",
            work_unit_id=action["work_unit_id"],
        )
        core.begin_dispatch(
            self.repo,
            run_id,
            action_id=action["action_id"],
            action=action["action"],
            role="builder",
            thread_id="builder-thread",
            prompt_digest="b" * 64,
            output_schema_digest="c" * 64,
            work_unit_id=action["work_unit_id"],
            context_projection_digest=projection,
        )
        core.bind_dispatch_turn(
            self.repo, run_id, action_id=action["action_id"], turn_id="turn-1"
        )
        for _ in range(3):
            try:
                core.retry_dispatch(
                    self.repo,
                    run_id,
                    action_id=action["action_id"],
                    failure_code="responseStreamDisconnected",
                )
            except core.AssuranceError:
                pass
        candidate = Path(started["candidate_worktree"])
        (candidate / "src" / "uncommitted.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        before = read_ledger(self.repo, run_id)
        with self.assertRaises(core.AssuranceError) as raised:
            core.begin_dispatch_rehydration(
                self.repo,
                run_id,
                action_id=action["action_id"],
                context_projection_digest=core.canonical_context_projection_digest(
                    before,
                    action_id=action["action_id"],
                    action=action["action"],
                    role="builder",
                    work_unit_id=action["work_unit_id"],
                ),
            )
        self.assertEqual(
            raised.exception.code, "WORK_UNIT_REHYDRATION_SIDE_EFFECT_BLOCKED"
        )
        self.assertIsNone(read_ledger(self.repo, run_id)["dispatch_rehydration_intent"])

        # Restore the exact clean source and complete one allowed rehydration.
        (candidate / "src" / "uncommitted.py").unlink()
        clean = read_ledger(self.repo, run_id)
        projection = core.canonical_context_projection_digest(
            clean,
            action_id=action["action_id"],
            action=action["action"],
            role="builder",
            work_unit_id=action["work_unit_id"],
        )
        core.begin_dispatch_rehydration(
            self.repo,
            run_id,
            action_id=action["action_id"],
            context_projection_digest=projection,
        )
        core.claim_dispatch_rehydration(
            self.repo, run_id, action_id=action["action_id"], claim_id="d" * 64
        )
        core.record_dispatch_rehydration_spawn(
            self.repo,
            run_id,
            action_id=action["action_id"],
            claim_id="d" * 64,
            new_agent_id="new-builder",
            new_thread_id="new-builder-thread",
        )
        core.bind_dispatch_rehydration(
            self.repo,
            run_id,
            action_id=action["action_id"],
            new_agent_id="new-builder",
            new_thread_id="new-builder-thread",
            prompt_digest="e" * 64,
        )
        for _ in range(3):
            try:
                core.retry_dispatch(
                    self.repo,
                    run_id,
                    action_id=action["action_id"],
                    failure_code="responseStreamDisconnected",
                )
            except core.AssuranceError:
                pass
        exhausted = read_ledger(self.repo, run_id)
        with self.assertRaises(core.AssuranceError) as raised:
            core.begin_dispatch_rehydration(
                self.repo,
                run_id,
                action_id=action["action_id"],
                context_projection_digest=core.canonical_context_projection_digest(
                    exhausted,
                    action_id=action["action_id"],
                    action=action["action"],
                    role="builder",
                    work_unit_id=action["work_unit_id"],
                ),
            )
        self.assertEqual(
            raised.exception.code, "WORK_UNIT_REHYDRATION_LIMIT_REACHED"
        )

    def test_interrupted_rehydration_spawn_cannot_start_another_thread(self) -> None:
        class FakeCore:
            def __init__(self) -> None:
                self.intent = {
                    "state": "prepared",
                    "action_id": "a" * 64,
                    "work_unit_id": "builder-work-unit",
                    "role": "builder",
                    "source_thread_id": "old-thread",
                    "source_generation": 1,
                    "source_attempt": 3,
                    "spawn_state": "pending",
                }
                self.pending = {
                    "action": "builder_implement",
                    "action_id": "a" * 64,
                    "role": "builder",
                    "work_unit_id": "builder-work-unit",
                    "state": "exhausted",
                }
                self.calls: list[str] = []

            def call(self, command: str, *args: str, input_value=None):
                self.calls.append(command)
                if command == "begin-dispatch-rehydration":
                    return {"status": "ACTIVE"}
                if command == "claim-dispatch-rehydration":
                    self.intent["spawn_state"] = "claimed"
                    self.intent["spawn_claim_id"] = "b" * 64
                    return {"status": "ACTIVE"}
                if command == "record-dispatch-rehydration-spawn":
                    raise RuntimeError("simulated interruption after thread/start")
                return {"status": "ACTIVE"}

        class FakeTransport:
            def __init__(self) -> None:
                self.starts = 0

            def read_thread(self, _thread_id: str) -> dict[str, object]:
                return {"turns": []}

            def start_thread(self, **_kwargs: object) -> str:
                self.starts += 1
                return "new-thread"

        fake_core = FakeCore()
        fake_transport = FakeTransport()
        coordinator = NativeCoordinator(
            repo=self.repo,
            run_id="work-unit-rehydration-crash-window",
            core=fake_core,
            transport=fake_transport,
        )
        coordinator.current_action = {
            "action": "builder_implement",
            "action_id": "a" * 64,
            "work_unit_id": "builder-work-unit",
        }
        context = {
            "dispatch_intent": fake_core.pending,
            "dispatch_rehydration_intent": None,
            "facets": {"execution": {"agents": {}}},
        }
        coordinator._context = lambda: {
            **context,
            "dispatch_rehydration_intent": (
                fake_core.intent
                if "begin-dispatch-rehydration" in fake_core.calls
                else None
            ),
        }
        coordinator._empty_rehydratable_tail = lambda *_args: True
        coordinator._projection_for_action = lambda *_args: ({}, "c" * 64)
        coordinator._role_config = lambda *_args: ("role", "danger-full-access")
        coordinator._turn_cwd = lambda *_args: str(self.repo)

        with self.assertRaisesRegex(
            RuntimeError, "simulated interruption after thread/start"
        ):
            coordinator._start_canonical_rehydration("a" * 64)
        self.assertEqual(fake_transport.starts, 1)

        # A second coordinator invocation sees the claimed-but-unresolved
        # intent and must leave the possible orphan untouched.
        self.assertFalse(coordinator._start_canonical_rehydration("a" * 64))
        self.assertEqual(fake_transport.starts, 1)
        self.assertIn("claim-dispatch-rehydration", fake_core.calls)

    def test_context_rotation_requires_three_completed_units_and_changes_only_identity(self) -> None:
        contract = contract_for(self.repo)
        contract["assurance"]["required"] = ["machine"]
        contract["authority"]["tester_write"] = []
        contract["execution"]["work_units"] = [
            {
                "id": f"builder-unit-{index}",
                "role": "builder",
                "objective": f"Complete Builder unit {index}.",
                "depends_on": [],
                "scope": {
                    "paths": [],
                    "behavior_ids": ["add-values"],
                    "command_ids": [],
                },
                "completion": {
                    "kind": "candidate_checkpoint",
                    "required_observations": ["candidate_commit"],
                },
            }
            for index in range(1, 5)
        ]
        run_id = "work-unit-context-rotation"
        started = core.start(
            self.repo,
            run_id,
            "work-unit-session",
            contract,
            driver_runtime=self._runtime(),
        )
        worktree = Path(started["candidate_worktree"])
        core.prepare_builder(
            self.repo,
            run_id,
            "rotation-builder",
            "rotation-thread-old",
            owner_mode="native_thread",
        )

        for index in range(1, 4):
            action = driver.next_action(self.repo, run_id)
            self.assertEqual(action["work_unit_id"], f"builder-unit-{index}")
            core.begin_dispatch(
                self.repo,
                run_id,
                action_id=action["action_id"],
                action=action["action"],
                role="builder",
                thread_id="rotation-thread-old",
                prompt_digest="b" * 64,
                output_schema_digest="c" * 64,
                work_unit_id=action["work_unit_id"],
                context_projection_digest=core.canonical_context_projection_digest(
                    read_ledger(self.repo, run_id),
                    action_id=action["action_id"],
                    action=action["action"],
                    role="builder",
                    work_unit_id=action["work_unit_id"],
                ),
                driver_runtime_kind="native",
            )
            core.complete_dispatch(
                self.repo,
                run_id,
                action_id=action["action_id"],
                result_value={
                    "result": "implemented",
                    "evidence_report": None,
                    "proof_spec": None,
                    "problem_report": None,
                },
            )
            path = worktree / "src" / f"rotation_{index}.py"
            path.write_text(f"VALUE = {index}\n", encoding="utf-8")
            commit_all(worktree, f"builder unit {index}")
            core.checkpoint_builder(
                self.repo,
                run_id,
                action_id=action["action_id"],
            )
            core.consume_dispatch(
                self.repo,
                run_id,
                action_id=action["action_id"],
                consumer_source="native_driver",
            )

        ledger = read_ledger(self.repo, run_id)
        progress = core.work_unit_progress(ledger)
        self.assertEqual(progress["completed"], [
            "builder-unit-1",
            "builder-unit-2",
            "builder-unit-3",
        ])
        self.assertEqual(progress["thread_usage"], {"rotation-thread-old": 3})

        next_action = driver.next_action(self.repo, run_id)
        self.assertEqual(next_action["work_unit_id"], "builder-unit-4")
        core.begin_context_rotation(
            self.repo,
            run_id,
            role="builder",
            work_unit_id="builder-unit-4",
            context_projection_digest=core.canonical_context_projection_digest(
                read_ledger(self.repo, run_id),
                action_id=next_action["action_id"],
                action=next_action["action"],
                role="builder",
                work_unit_id="builder-unit-4",
            ),
            action_id=next_action["action_id"],
            action=next_action["action"],
        )
        before = read_ledger(self.repo, run_id)
        self.assertIsNotNone(before["context_rotation_intent"])
        unresolved = driver.next_action(self.repo, run_id)
        self.assertEqual(unresolved["status"], "NEEDS_USER")
        self.assertEqual(
            unresolved["reason"], "context_rotation_spawn_unresolved"
        )
        core.claim_context_rotation(
            self.repo,
            run_id,
            action_id=next_action["action_id"],
            claim_id="b" * 64,
        )
        core.record_context_rotation_spawn(
            self.repo,
            run_id,
            action_id=next_action["action_id"],
            claim_id="b" * 64,
            new_agent_id="rotation-builder-new",
            new_thread_id="rotation-thread-new",
        )
        class ReadOnlyTransport:
            def __init__(self) -> None:
                self.starts = 0
                self.reads: list[str] = []

            def read_thread(self, thread_id: str) -> dict[str, object]:
                self.reads.append(thread_id)
                return {"id": thread_id, "turns": []}

            def start_thread(self, **_kwargs: object) -> str:
                self.starts += 1
                raise AssertionError("recorded spawn must not start another thread")

        transport = ReadOnlyTransport()
        native_core = CorePort()
        recovery_action = native_core.call(
            "driver-next", "--repo", str(self.repo), "--run", run_id
        )
        self.assertEqual(recovery_action["action"], "rotate_context")
        coordinator = NativeCoordinator(
            repo=self.repo,
            run_id=run_id,
            core=native_core,
            transport=transport,
        )
        coordinator._rotate_context(recovery_action)

        after = read_ledger(self.repo, run_id)
        self.assertIsNone(after["context_rotation_intent"])
        self.assertEqual(transport.starts, 0)
        self.assertEqual(transport.reads, ["rotation-thread-new"])
        self.assertEqual(
            after["facets"]["execution"]["agents"]["builder"],
            {
                "agent_id": "rotation-builder-new",
                "thread_id": "rotation-thread-new",
            },
        )
        self.assertEqual(
            core.work_unit_progress(after)["completed"],
            progress["completed"],
        )
        self.assertEqual(
            [
                event["kind"]
                for event in after["events"]
                if isinstance(event, dict)
                and event["kind"] == "context_thread_rotated"
            ],
            ["context_thread_rotated"],
        )


if __name__ == "__main__":
    unittest.main()
