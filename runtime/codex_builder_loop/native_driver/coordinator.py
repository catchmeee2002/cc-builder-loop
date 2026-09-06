from __future__ import annotations

import copy
import json
import hashlib
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..assurance_v4.driver_contract import (
    AGENT_ACTION_CAPABILITIES,
    AgentActionCapability,
)
from ..assurance_v4.core import canonical_context_projection
from ..assurance_v4.models import ContractError, digest, validate_agent_result
from .app_server import AppServerError, AppServerTransport, TurnResult
from .core_port import CorePort, CorePortError
from .transport_failures import (
    classify_app_server_failure,
    classify_turn_failure,
    is_missing_rollout_failure,
    is_retryable_transport_failure,
)


REVIEWER_COMPACTION_FAILURE_CODES = {
    "responseStreamDisconnected",
    "missingAgentResult",
}
ROLE_RESULT_VALIDATION_FAILURE_CODES = frozenset(
    {
        "AGENT_RESULT_INVALID",
        "EVIDENCE_REPORT_INVALID",
        "TEST_PROOF_SPEC_INVALID",
        "PROBLEM_REPORT_INVALID",
    }
)


class NativeDriverError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: str = "FATAL", details: Any = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


class NativeCoordinator:
    def __init__(
        self,
        *,
        repo: Path,
        run_id: str,
        core: CorePort,
        transport: AppServerTransport | None,
        project_root: Path | None = None,
        dispatch_renewal_reason: str | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        thread_compaction_available: bool = False,
        builder_mode: str = "native_thread",
        root_session_id: str | None = None,
    ):
        self.repo = repo.resolve()
        self.run_id = run_id
        self.core = core
        self.transport = transport
        self.project_root = project_root or Path(__file__).resolve().parents[3]
        self.output_schema = json.loads(
            (
                self.project_root
                / "schema"
                / "assurance-v4-native-agent-wire-result.schema.json"
            ).read_text()
        )
        self.output_schema_digest = digest(self.output_schema)
        self.problem_schema = self._load_schema("codex-problem-report.schema.json")
        self.evidence_schema = self._load_schema("assurance-v4-evidence.schema.json")
        self.blackbox_case_schema = self._load_schema("codex-blackbox-case.schema.json")
        self.proof_schema = self._load_schema("codex-test-proof.schema.json")
        self._active_threads: set[str] = set()
        self.current_action: dict[str, Any] | None = None
        self.current_dispatch: dict[str, Any] | None = None
        self._event_sink = event_sink
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._sleep_fn = sleep_fn or time.sleep
        self._dispatch_renewal_reason = (
            dispatch_renewal_reason.strip()
            if isinstance(dispatch_renewal_reason, str) and dispatch_renewal_reason.strip()
            else None
        )
        self._thread_compaction_available = thread_compaction_available
        self.builder_mode = builder_mode
        self.root_session_id = root_session_id

    def _transport_generation(self) -> str | None:
        value = getattr(self.transport, "generation", None)
        return value if isinstance(value, str) and value else None

    def _timeout_profile_digest(self) -> str:
        profile = {
            "turn_idle_seconds": getattr(self.transport, "turn_idle_timeout", 30.0),
            "turn_total_seconds": getattr(
                self.transport, "turn_total_timeout", 3600.0
            ),
            "compaction_total_seconds": getattr(
                self.transport, "compaction_total_timeout", 600.0
            ),
        }
        return hashlib.sha256(
            json.dumps(
                profile,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _checkpoint_wire(
        self, action_id: str, *, expected_generation: str | None = None
    ) -> None:
        snapshot = getattr(self.transport, "wire_snapshot", None)
        if not callable(snapshot):
            return
        value = snapshot()
        if not isinstance(value, dict):
            return
        generation = value.get("generation")
        sequence = value.get("sequence")
        event_digest = value.get("event_digest")
        if (
            not isinstance(generation, str)
            or not isinstance(sequence, int)
            or not isinstance(event_digest, str)
            or expected_generation is None
            or generation != expected_generation
        ):
            return
        self.core.call(
            "record-dispatch-wire",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            action_id,
            "--native-transport-generation",
            generation,
            "--sequence",
            str(sequence),
            "--event-digest",
            event_digest,
            "--driver-runtime-kind",
            "native",
        )

    def run(self) -> dict[str, Any]:
        while True:
            self.current_action = None
            self.current_dispatch = None
            action = self.core.call(
                "driver-next", "--repo", str(self.repo), "--run", self.run_id
            )
            self.current_action = action
            if action.get("driver_protocol_version") != 1:
                raise NativeDriverError(
                    "Core DriverPort version is unsupported",
                    code="NATIVE_DRIVER_PORT_INCOMPATIBLE",
                )
            status = action.get("status")
            if status == "NEEDS_USER":
                return {"status": "NEEDS_USER", "run_id": self.run_id, "decision": action}
            if status == "STOP":
                phase = action.get("reason")
                return {
                    "status": (
                        "FINALIZED"
                        if phase == "finalized"
                        else "FAILED"
                        if phase == "failed"
                        else "STOPPED"
                    ),
                    "run_id": self.run_id,
                    "decision": action,
                }
            name = str(action.get("action"))
            capability = AGENT_ACTION_CAPABILITIES.get(name)
            if capability is not None:
                if capability.role == "builder" and self.builder_mode == "root_session":
                    return self._root_builder_handoff(action)
                if self.transport is None:
                    return {
                        "status": "TRANSPORT_HANDOFF",
                        "run_id": self.run_id,
                        "action": name,
                        "action_id": action.get("action_id"),
                        "reason": "native_transport_required",
                    }
                self._run_agent_action(action, capability)
                continue
            if name == "checkpoint_builder":
                self._simple("checkpoint-builder", action)
            elif name == "apply_engineering_correction":
                self._simple("apply-engineering-correction", action)
            elif name == "replace_tester":
                self._replace_tester(action)
            elif name == "replace_reviewer":
                self._replace_reviewer(action)
            elif name == "publish_prerequisites":
                self._simple("publish-prerequisites", action)
            elif name == "verify_machine":
                self._simple("verify-machine", action)
            elif name == "verify_preflight":
                self._simple("verify-preflight", action)
            elif name == "scan_doc_references":
                self._simple("scan-doc-references", action)
            elif name == "prepare_deployment":
                self._simple("prepare-deployment", action)
            elif name == "restore_deployment":
                self._simple("restore-deployment", action)
            elif name == "restore_superseded_environment":
                self._simple("restore-superseded-environment", action)
            elif name == "complete_supersede_transfer":
                self._simple("complete-supersede-transfer", action)
            elif name == "complete_blackbox":
                self._simple("complete-blackbox", action)
            elif name in {"rematerialize_target", "recompose_candidate"}:
                self._simple("recompose-candidate", action)
            elif name == "recover_finalize":
                self._simple("recover-finalize", action)
            elif name == "complete_driver_failure":
                args = [
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--driver-runtime-kind",
                    "native",
                ]
                if self.builder_mode == "root_session":
                    args.extend(["--owner-session-id", str(self.root_session_id)])
                self.core.call("complete-driver-failure", *args)
            elif name == "rehydrate_dispatch":
                self._rehydrate_dispatch(action)
            elif name == "rotate_context":
                self._rotate_context(action)
            elif name == "finalize":
                self.core.call(
                    "finalize",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--message",
                    "feat(builder): [cr_id_skip] Deliver Accepted Plan",
                    "--action-id",
                    str(action["action_id"]),
                    "--driver-runtime-kind",
                    "native",
                )
            elif name in {"contract_decision", "continuity_decision", "architecture_review"}:
                return {"status": "NEEDS_USER", "run_id": self.run_id, "decision": action}
            else:
                raise NativeDriverError(
                    f"unsupported Core action: {name}",
                    code="NATIVE_DRIVER_ACTION_UNSUPPORTED",
                    details=action,
                )

    def _root_builder_handoff(self, action: dict[str, Any]) -> dict[str, Any]:
        context = self._context()
        owner = context["facets"]["execution"]["agents"].get("builder")
        if (
            not isinstance(owner, dict)
            or owner.get("mode") != "root_session"
            or owner.get("session_id") != self.root_session_id
        ):
            agent_id = f"codex-root-session:{self.root_session_id or self.run_id}"
            self.core.call(
                "prepare-builder",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--agent-id",
                agent_id,
                "--owner-mode",
                "root_session",
                "--owner-session-id",
                str(self.root_session_id),
                "--action-id",
                str(action["action_id"]),
                "--driver-runtime-kind",
                "native",
            )
            action = self.core.call(
                "driver-next", "--repo", str(self.repo), "--run", self.run_id
            )
            context = self._context()
            owner = context["facets"]["execution"]["agents"].get("builder")
        if not isinstance(owner, dict):
            raise NativeDriverError(
                "root Builder owner preparation did not persist",
                code="NATIVE_ROOT_BUILDER_OWNER_MISSING",
                status="NEEDS_USER",
            )
        pending = context.get("dispatch_intent")
        prompt = self._prompt(action, "builder", context)
        return {
            "status": "BUILDER_HANDOFF",
            "run_id": self.run_id,
            "action": action.get("action"),
            "action_id": action.get("action_id"),
            "reason": action.get("reason"),
            "work_unit_id": action.get("work_unit_id"),
            "work_unit": copy.deepcopy(action.get("work_unit")),
            "work_unit_progress": copy.deepcopy(
                action.get("work_unit_progress")
            ),
            "parallel_ready": action.get("parallel_ready", False),
            "parallel_work_units": copy.deepcopy(
                action.get("parallel_work_units", [])
            ),
            "builder_owner": owner,
            "candidate_worktree": context.get("candidate_worktree"),
            "target_start_head": context.get("target_start_head"),
            "dispatch_state": (
                pending.get("state")
                if isinstance(pending, dict)
                else "unprepared"
            ),
            "dispatch_intent": (
                copy.deepcopy(pending) if isinstance(pending, dict) else None
            ),
            "prompt_digest": digest(prompt),
            "output_schema_digest": self.output_schema_digest,
            "result_schema": self.output_schema,
            "submit_command": "assurance apply-root-builder-result",
        }

    def _simple(self, command: str, action: dict[str, Any]) -> None:
        args = [
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(action["action_id"]),
            "--driver-runtime-kind",
            "native",
        ]
        if command == "checkpoint-builder" and self.builder_mode == "root_session":
            args.extend(["--owner-session-id", str(self.root_session_id)])
        if command == "complete-driver-failure" and self.builder_mode == "root_session":
            args.extend(["--owner-session-id", str(self.root_session_id)])
        if command == "recompose-candidate" and self.builder_mode == "root_session":
            args.extend(["--owner-session-id", str(self.root_session_id)])
        self.core.call(
            command,
            *args,
        )

    def _replace_tester(self, action: dict[str, Any]) -> None:
        context = self._context()
        replacement = context.get("tester_replacement_intent")
        problem = action.get("problem")
        problem_key = (
            replacement.get("problem_key")
            if isinstance(replacement, dict)
            else problem.get("key")
            if isinstance(problem, dict)
            else action.get("problem_key")
        )
        if not isinstance(problem_key, str) or not problem_key:
            raise NativeDriverError(
                "Tester replacement action has no problem binding",
                code="NATIVE_TESTER_REPLACEMENT_PROBLEM_MISSING",
                status="NEEDS_USER",
            )
        action_id = str(
            replacement["action_id"]
            if isinstance(replacement, dict)
            else action["action_id"]
        )
        if not isinstance(replacement, dict):
            self.core.call(
                "begin-tester-replacement",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--problem-key",
                problem_key,
                "--driver-runtime-kind",
                "native",
            )
            replacement = self._context().get("tester_replacement_intent")
        if not isinstance(replacement, dict):
            raise NativeDriverError(
                "Tester replacement intent was not persisted",
                code="NATIVE_TESTER_REPLACEMENT_INTENT_MISSING",
            )
        new_agent = replacement.get("new_agent")
        if not isinstance(new_agent, dict):
            instructions, _sandbox = self._role_config("tester")
            thread_id = self.transport.start_thread(
                cwd=str(replacement["worktree"]),
                developer_instructions=instructions,
                sandbox="danger-full-access",
            )
            new_agent = {
                "agent_id": f"codex-app-server:{thread_id}",
                "thread_id": thread_id,
            }
            self.core.call(
                "bind-tester-replacement",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--agent-id",
                new_agent["agent_id"],
                "--thread-id",
                thread_id,
                "--driver-runtime-kind",
                "native",
            )
            self._active_threads.add(thread_id)
        self.core.call(
            "complete-tester-replacement",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            action_id,
            "--driver-runtime-kind",
            "native",
        )

    def _replace_reviewer(self, action: dict[str, Any]) -> None:
        if self.transport is None:
            raise NativeDriverError(
                "Native transport is required for Reviewer replacement",
                code="NATIVE_REVIEWER_REPLACEMENT_TRANSPORT_REQUIRED",
                status="NEEDS_USER",
            )
        context = self._context()
        replacement = context.get("reviewer_replacement_intent")
        if not isinstance(replacement, dict):
            raise NativeDriverError(
                "Reviewer replacement intent was not persisted",
                code="NATIVE_REVIEWER_REPLACEMENT_INTENT_MISSING",
                status="NEEDS_USER",
            )
        self._assert_reviewer_replacement_source(replacement)
        action_id = str(replacement["action_id"])
        new_agent = replacement.get("new_agent")
        if not isinstance(new_agent, dict):
            instructions, _sandbox = self._role_config("reviewer")
            thread_id = self.transport.start_thread(
                cwd=str(context["candidate_worktree"]),
                developer_instructions=instructions,
                sandbox="danger-full-access",
            )
            new_agent = {
                "agent_id": f"codex-app-server:{thread_id}",
                "thread_id": thread_id,
            }
            self.core.call(
                "bind-reviewer-replacement",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--agent-id",
                new_agent["agent_id"],
                "--thread-id",
                thread_id,
                "--driver-runtime-kind",
                "native",
            )
            self._active_threads.add(thread_id)
        self._assert_reviewer_replacement_source(replacement)
        self.core.call(
            "complete-reviewer-replacement",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            action_id,
            "--driver-runtime-kind",
            "native",
        )

    def _renew_tester_bootstrap_after_missing_rollout(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
        error: AppServerError,
    ) -> bool:
        if action.get("action") != "tester_author" or not is_missing_rollout_failure(error):
            return False
        replacement = context.get("tester_replacement_intent")
        source = context["facets"]["execution"].get("tester_source")
        if (
            not isinstance(replacement, dict)
            or replacement.get("stage") != "awaiting_first_turn"
            or not isinstance(source, dict)
            or source.get("agent") != replacement.get("new_agent")
            or source.get("head") != source.get("base_head")
            or source.get("files") != []
        ):
            return False
        dispatch = context.get("dispatch_intent")
        if dispatch is not None and not (
            isinstance(dispatch, dict)
            and dispatch.get("action") == "tester_author"
            and dispatch.get("role") == "tester"
            and dispatch.get("state") == "prepared"
            and dispatch.get("turn_id") is None
            and dispatch.get("thread_id")
            == replacement.get("new_agent", {}).get("thread_id")
            and dispatch.get("activation_state") == "pending"
        ):
            return False
        replacement_action_id = str(replacement["action_id"])
        problem_key = str(replacement["problem_key"])
        self.core.call(
            "begin-tester-replacement",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            replacement_action_id,
            "--problem-key",
            problem_key,
            "--renew-bootstrap",
            "--driver-runtime-kind",
            "native",
        )
        renewed = self._context().get("tester_replacement_intent")
        if not isinstance(renewed, dict) or renewed.get("new_agent") is not None:
            return True
        instructions, _sandbox = self._role_config("tester")
        thread_id = self.transport.start_thread(
            cwd=str(source["worktree"]),
            developer_instructions=instructions,
            sandbox="danger-full-access",
        )
        self.core.call(
            "bind-tester-replacement",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            replacement_action_id,
            "--agent-id",
            f"codex-app-server:{thread_id}",
            "--thread-id",
            thread_id,
            "--driver-runtime-kind",
            "native",
        )
        self._active_threads.add(thread_id)
        return True

    def _run_agent_action(
        self,
        action: dict[str, Any],
        capability: AgentActionCapability,
    ) -> None:
        if self.transport is None:
            raise NativeDriverError(
                "Native transport is required for this non-root Builder action",
                code="NATIVE_ROOT_TRANSPORT_REQUIRED",
                status="NEEDS_USER",
            )
        self.current_dispatch = None
        context = self._context()
        role = capability.role
        agent = context["facets"]["execution"]["agents"].get(role)
        tester_source = context["facets"]["execution"].get("tester_source")
        if role == "tester" and capability.preparation == "existing_tester_source":
            if not isinstance(agent, dict) or not isinstance(tester_source, dict):
                raise NativeDriverError(
                    "Tester source continuity is missing for this action",
                    code="NATIVE_TESTER_SOURCE_MISSING",
                    status="NEEDS_USER",
                    details={"action": action.get("action")},
                )
        elif not isinstance(agent, dict) or (
            role == "tester"
            and capability.preparation == "tester_source"
            and not isinstance(tester_source, dict)
        ):
            self._prepare_role(action, capability, context)
            return
        if self._start_context_rotation(action, role, context):
            return
        _projection, projection_digest = self._projection_for_action(
            action, role, context
        )
        pending = context.get("dispatch_intent")
        if (
            isinstance(pending, dict)
            and isinstance(pending.get("context_projection_digest"), str)
            and pending.get("context_projection_digest") != projection_digest
        ):
            raise NativeDriverError(
                "work unit context projection changed before dispatch",
                code="NATIVE_WORK_UNIT_PROJECTION_DRIFT",
                status="NEEDS_USER",
                details={
                    "expected": pending.get("context_projection_digest"),
                    "actual": projection_digest,
                    "work_unit_id": pending.get("work_unit_id"),
                },
        )
        if isinstance(pending, dict):
            self.current_dispatch = copy.deepcopy(pending)
            if (
                role == "builder"
                and pending.get("state") == "completed"
                and not pending.get("result_path")
                and not any(
                    item.get("owner") == "builder" and item.get("status") == "open"
                    for item in context.get("problems", [])
                )
            ):
                self.core.call(
                    "consume-dispatch",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--action-id",
                    str(action["action_id"]),
                    "--consumer-source",
                    "native_driver",
                )
                return
            result = self._recover_dispatch(pending, role, context, action)
            if result is None:
                return
            self._apply_agent_result(action, role, result, context)
            return
        prompt = self._prompt(action, role, context)
        transport_generation = self._transport_generation()
        dispatch_args = [
            "begin-dispatch",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(action["action_id"]),
            "--action",
            str(action["action"]),
            "--role",
            role,
            "--thread-id",
            str(agent["thread_id"]),
            "--prompt-digest",
            digest(prompt),
            "--output-schema-digest",
            self.output_schema_digest,
        ]
        if isinstance(action.get("work_unit_id"), str):
            dispatch_args.extend(
                [
                    "--work-unit-id",
                    str(action["work_unit_id"]),
                    "--context-projection-digest",
                    projection_digest,
                ]
            )
        if transport_generation is not None:
            dispatch_args.extend(
                [
                    "--native-transport-generation",
                    transport_generation,
                    "--timeout-profile-digest",
                    self._timeout_profile_digest(),
                ]
            )
        begun = self.core.call(
            *dispatch_args,
        )
        begun_dispatch = begun.get("dispatch_intent")
        self.current_dispatch = (
            copy.deepcopy(begun_dispatch)
            if isinstance(begun_dispatch, dict)
            else {
                "action_id": str(action["action_id"]),
                "action": str(action["action"]),
                "role": role,
                "thread_id": agent.get("thread_id"),
                "state": "prepared",
            }
        )
        activation_enabled = (
            isinstance(begun.get("dispatch_intent"), Mapping)
            and "activation_state" in begun["dispatch_intent"]
        )
        self._activate_role_thread(action, role, context, str(agent["thread_id"]))
        if activation_enabled:
            self.core.call(
                "record-dispatch-activation",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                str(action["action_id"]),
                "--state",
                "activated",
                "--driver-runtime-kind",
                "native",
            )
        turn = self.transport.run_turn(
            thread_id=agent["thread_id"],
            prompt=prompt,
            output_schema=self.output_schema,
            action_id=f"{action['action_id']}:1",
            cwd=self._turn_cwd(action, role, context),
            sandbox_policy=self._sandbox_policy(action, role, context),
            on_started=lambda turn_id: self.core.call(
                "bind-dispatch-turn",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                str(action["action_id"]),
                "--turn-id",
                turn_id,
            ),
        )
        self._checkpoint_wire(
            str(action["action_id"]),
            expected_generation=transport_generation,
        )
        if self._retry_interrupted_turn(turn, action, context):
            return
        if self._retry_turn_failure(turn, str(action["action_id"])):
            return
        result = self._parse_action_result_or_retry(
            str(action["action"]),
            turn,
            str(action["action_id"]),
        )
        if result is None:
            return
        if not self._complete_dispatch_or_retry(
            str(action["action_id"]),
            str(action["action"]),
            result,
        ):
            return
        self._apply_agent_result(action, role, result, self._context())

    @staticmethod
    def _interrupted_turn_is_empty(turn: TurnResult) -> bool:
        if turn.status != "interrupted" or turn.text:
            return False
        return True

    def _retry_interrupted_turn(
        self,
        turn: TurnResult,
        action: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        if not self._interrupted_turn_is_empty(turn):
            return False
        intent = context.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("state") != "in_flight":
            return False
        observation = {
            "candidate_head": context["facets"]["execution"].get("candidate_head"),
            "target_start_head": context.get("target_start_head"),
            "evidence": context.get("evidence", {}),
            "publication": context.get("publication"),
            "deployment_transaction": context.get("deployment_transaction"),
        }
        if intent.get("dispatch_observation_digest") != digest(observation):
            return False
        self._assert_builder_retry_safe(
            str(action["action_id"]), action_name=str(action["action"])
        )
        try:
            self.core.call(
                "retry-dispatch",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                str(action["action_id"]),
                "--failure-code",
                "interruptedNoOutput",
                "--interrupted-retry",
            )
        except CorePortError as exc:
            if exc.status in {"FAIL", "NEEDS_USER"}:
                raise NativeDriverError(
                    "interrupted dispatch requires explicit user recovery",
                    code="NATIVE_DISPATCH_INTERRUPTED_REQUIRES_USER",
                    status="NEEDS_USER",
                    details=exc.payload,
                ) from exc
            raise
        return True

    def _prepare_role(
        self,
        action: dict[str, Any],
        capability: AgentActionCapability,
        context: dict[str, Any],
    ) -> None:
        role = capability.role
        existing_agent = context["facets"]["execution"]["agents"].get(role)
        if isinstance(existing_agent, dict):
            thread_id = str(existing_agent["thread_id"])
            agent_id = str(existing_agent["agent_id"])
        else:
            instructions, _sandbox = self._role_config(role)
            cwd = context["candidate_worktree"] if role == "builder" else context["repo_root"]
            thread_id = self.transport.start_thread(
                cwd=cwd,
                developer_instructions=instructions,
                sandbox="danger-full-access",
            )
            agent_id = f"codex-app-server:{thread_id}"
            self._active_threads.add(thread_id)
        command = f"prepare-{role}"
        args = [
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--agent-id",
            agent_id,
            "--thread-id",
            thread_id,
            "--action-id",
            str(action["action_id"]),
            "--driver-runtime-kind",
            "native",
        ]
        if role == "tester" and capability.preparation == "tester_identity":
            args.append("--identity-only")
        self.core.call(command, *args)

    def _recover_dispatch(
        self,
        pending: dict[str, Any],
        role: str,
        context: dict[str, Any],
        action: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if pending.get("state") == "completed":
            path = Path(str(pending["result_path"]))
            try:
                result = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise NativeDriverError(
                    "persisted dispatch result is unreadable",
                    code="NATIVE_DISPATCH_RESULT_INVALID",
                    status="NEEDS_USER",
                    details={"path": str(path), "error": str(exc)},
                ) from exc
            if digest(result) != pending.get("result_digest"):
                raise NativeDriverError(
                    "persisted dispatch result digest changed",
                    code="NATIVE_DISPATCH_RESULT_DRIFT",
                    status="NEEDS_USER",
                )
            try:
                return validate_agent_result(result)
            except ContractError as exc:
                raise NativeDriverError(
                    "persisted dispatch result schema is invalid",
                    code="NATIVE_DISPATCH_RESULT_INVALID",
                    status="NEEDS_USER",
                    details={
                        "validation_code": exc.code,
                        **copy.deepcopy(exc.details),
                    },
                ) from exc
        current = action or self.current_action
        if not isinstance(current, dict):
            raise NativeDriverError(
                "current Core action is missing for dispatch recovery",
                code="NATIVE_DISPATCH_ACTION_DRIFT",
                status="NEEDS_USER",
            )
        if (
            current.get("action_id") != pending.get("action_id")
            or current.get("action") != pending.get("action")
        ):
            raise NativeDriverError(
                "prepared dispatch action identity changed",
                code="NATIVE_DISPATCH_ACTION_DRIFT",
                status="NEEDS_USER",
            )
        agent = context["facets"]["execution"]["agents"].get(role)
        if (
            not isinstance(agent, dict)
            or pending.get("role") != role
            or pending.get("thread_id") != agent.get("thread_id")
        ):
            raise NativeDriverError(
                "prepared dispatch role or thread identity changed",
                code="NATIVE_DISPATCH_IDENTITY_DRIFT",
                status="NEEDS_USER",
            )
        if pending.get("output_schema_digest") != self.output_schema_digest:
            raise NativeDriverError(
                "prepared dispatch output schema changed",
                code="NATIVE_DISPATCH_OUTPUT_SCHEMA_DRIFT",
                status="NEEDS_USER",
            )
        prompt = self._prompt(current, role, context)
        if digest(prompt) != pending.get("prompt_digest"):
            raise NativeDriverError(
                "prepared dispatch prompt cannot be reconstructed",
                code="NATIVE_DISPATCH_PROMPT_DRIFT",
                status="NEEDS_USER",
            )
        self._wait_for_retry(pending)
        thread_id = str(pending["thread_id"])
        activation_state = pending.get("activation_state")
        if pending.get("state") != "exhausted":
            if activation_state == "unknown":
                raise NativeDriverError(
                    "dispatch activation outcome is unknown",
                    code="NATIVE_DISPATCH_ACTIVATION_UNKNOWN",
                    status="NEEDS_USER",
                    details={
                        "action_id": pending.get("action_id"),
                        "thread_id": thread_id,
                        "failure_code": pending.get("activation_failure_code"),
                    },
                )
            if activation_state == "pending":
                if pending.get("turn_id") is not None:
                    raise NativeDriverError(
                        "pending dispatch has a bound turn before activation",
                        code="NATIVE_DISPATCH_ACTIVATION_STATE_INVALID",
                        status="NEEDS_USER",
                        details={"action_id": pending.get("action_id")},
                    )
                self._activate_role_thread(current, role, context, thread_id)
                self.core.call(
                    "record-dispatch-activation",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--action-id",
                    str(pending["action_id"]),
                    "--state",
                    "activated",
                    "--driver-runtime-kind",
                    "native",
                )
            elif activation_state == "activated":
                self._active_threads.add(thread_id)
            else:
                instructions, sandbox = self._role_config(role)
                self.transport.resume_thread(
                    thread_id=thread_id,
                    cwd=self._turn_cwd(current, role, context),
                    developer_instructions=instructions,
                    sandbox="danger-full-access",
                )
        if pending.get("state") == "exhausted":
            # An exhausted source only needs read/compaction/replacement
            # recovery.  Do not resume the old thread before proving its tail;
            # a closed or otherwise unavailable source must remain diagnosable.
            thread = self.transport.read_thread(thread_id)
            compacted = self._recover_reviewer_thread_compaction(pending, thread)
            if compacted is not None:
                return None
            if (
                role == "reviewer"
                and pending.get("action") in {"reviewer_preflight", "reviewer_final"}
                and self._start_reviewer_replacement(str(pending["action_id"]))
            ):
                return None
            if self._start_canonical_rehydration(
                str(pending["action_id"]),
                action_name=str(pending.get("action")),
            ):
                return None
            raise NativeDriverError(
                "Native role transport failed three times",
                code="NATIVE_DISPATCH_RETRY_EXHAUSTED",
                status="NEEDS_USER",
                details={
                    "failure_code": pending.get("failure_code"),
                    "attempt": pending.get("attempt"),
                    "generation": pending.get("generation"),
                },
            )
        thread = self.transport.read_thread(thread_id)
        attempt = int(pending.get("attempt", 1))
        client_id = self._dispatch_client_id(pending)
        matches = [
            turn
            for turn in thread.get("turns", [])
            if turn.get("id") == pending.get("turn_id")
            or self._turn_client_id(turn) == client_id
        ]
        if not matches and pending.get("state") == "prepared" and not pending.get("turn_id"):
            turn = self.transport.run_turn(
                thread_id=thread_id,
                prompt=prompt,
                output_schema=self.output_schema,
                action_id=client_id,
                cwd=self._turn_cwd(current, role, context),
                sandbox_policy=self._sandbox_policy(current, role, context),
                on_started=lambda turn_id: self.core.call(
                    "bind-dispatch-turn",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--action-id",
                    str(pending["action_id"]),
                    "--turn-id",
                    turn_id,
                ),
            )
            self._checkpoint_wire(
                str(pending["action_id"]),
                expected_generation=pending.get("native_transport_generation"),
            )
            if self._retry_turn_failure(turn, str(pending["action_id"])):
                return None
            result = self._parse_action_result_or_retry(
                str(pending["action"]),
                turn,
                str(pending["action_id"]),
            )
            if result is None:
                return None
            if not self._complete_dispatch_or_retry(
                str(pending["action_id"]),
                str(pending["action"]),
                result,
            ):
                return None
            return result
        if len(matches) == 1 and matches[0].get("status") == "failed":
            failure_code = self._turn_failure_code(matches[0])
            if is_retryable_transport_failure(failure_code):
                self._schedule_dispatch_retry(
                    str(pending["action_id"]),
                    failure_code,
                    action_name=str(pending["action"]),
                )
                return None
        if (
            len(matches) == 1
            and matches[0].get("status") == "completed"
            and self._turn_agent_text(matches[0]) is None
        ):
            self._schedule_dispatch_retry(
                str(pending["action_id"]),
                "missingAgentResult",
                action_name=str(pending["action"]),
            )
            return None
        if len(matches) == 1 and matches[0].get("status") in {"inProgress", "in_progress"}:
            turn = self.transport.wait_turn(
                thread_id=thread_id, turn_id=str(matches[0]["id"])
            )
            self._checkpoint_wire(
                str(pending["action_id"]),
                expected_generation=pending.get("native_transport_generation"),
            )
            if self._retry_interrupted_turn(turn, current, context):
                return None
            if self._retry_turn_failure(turn, str(pending["action_id"])):
                return None
            result = self._parse_action_result_or_retry(
                str(pending["action"]),
                turn,
                str(pending["action_id"]),
            )
            if result is None:
                return None
            if not self._complete_dispatch_or_retry(
                str(pending["action_id"]),
                str(pending["action"]),
                result,
            ):
                return None
            return result
        if len(matches) != 1 or matches[0].get("status") not in {"completed", "failed", "interrupted"}:
            raise NativeDriverError(
                "pending Codex turn cannot be uniquely recovered",
                code="NATIVE_DISPATCH_CONTINUITY_FAILURE",
                status="NEEDS_USER",
                details={"thread_id": thread_id, "turn_id": pending.get("turn_id")},
            )
        turn_value = matches[0]
        text = self._turn_agent_text(turn_value) or ""
        turn_result = TurnResult(
            turn_id=str(turn_value["id"]),
            status=str(turn_value["status"]),
            text=text,
            error=turn_value.get("error"),
        )
        if self._retry_interrupted_turn(turn_result, current, context):
            return None
        result = self._parse_action_result_or_retry(
            str(pending["action"]),
            turn_result,
            str(pending["action_id"]),
        )
        if result is None:
            return None
        if pending.get("state") == "prepared":
            self.core.call(
                "bind-dispatch-turn",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                str(pending["action_id"]),
                "--turn-id",
                str(turn_value["id"]),
            )
        if not self._complete_dispatch_or_retry(
            str(pending["action_id"]),
            str(pending["action"]),
            result,
        ):
            return None
        return result

    def _apply_agent_result(
        self,
        action: dict[str, Any],
        role: str,
        result: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        action_id = str(action["action_id"])
        agent = context["facets"]["execution"]["agents"][role]
        problem = result.get("problem_report")
        proof_spec: dict[str, Any] | None = None
        proof_failure_already_persisted = False
        if action["action"] == "tester_proof" and not isinstance(problem, dict):
            value = result.get("proof_spec")
            if not isinstance(value, dict):
                raise NativeDriverError(
                    "Tester returned no proof spec", code="NATIVE_PROOF_SPEC_MISSING"
                )
            proof_spec = self._bind_proof_test_ids(value, context)
            result = {**result, "proof_spec": proof_spec}
        elif action["action"] == "tester_proof_diagnose" and isinstance(
            result.get("proof_spec"), dict
        ):
            proof_spec = self._bind_proof_test_ids(result["proof_spec"], context)
            result = {**result, "proof_spec": proof_spec}
        if proof_spec is not None:
            proof_failure_already_persisted = self._persisted_proof_failure_matches(
                context.get("proof_failure"),
                context.get("proof_failure_state"),
                action_id,
                proof_spec,
                agent,
            )
        if action["action"] == "tester_proof_diagnose":
            source_failure = action.get("proof_failure")
            if (
                not isinstance(source_failure, dict)
                and context.get("proof_failure_state") == "current"
            ):
                source_failure = context.get("proof_failure")
            if not proof_failure_already_persisted:
                self._validate_proof_diagnosis(
                    result,
                    proof_failure=source_failure,
                )
        if action["action"] == "tester_machine_diagnose":
            self._validate_machine_diagnosis(result)
        if isinstance(problem, dict):
            self.core.call(
                "record-problems",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--report",
                "-",
                "--role",
                role,
                "--agent-id",
                str(agent["agent_id"]),
                "--thread-id",
                str(agent["thread_id"]),
                "--action-id",
                action_id,
                "--driver-runtime-kind",
                "native",
                input_value=problem,
            )
        elif action["action"] in {"builder_recompose_fix", "tester_recompose_fix"}:
            self.core.call(
                "recompose-candidate",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--driver-runtime-kind",
                "native",
            )
        elif action["action"] in {"builder_implement", "builder_fix"}:
            self.core.call(
                "checkpoint-builder",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--driver-runtime-kind",
                "native",
            )
        elif action["action"] in {"tester_author", "tester_fix"}:
            self.core.call(
                "integrate-tester",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--driver-runtime-kind",
                "native",
            )
            evidence = result.get("evidence_report")
            if not isinstance(evidence, dict):
                raise NativeDriverError("Tester returned no evidence", code="NATIVE_TESTER_EVIDENCE_MISSING")
            integrated = self._context()
            integrated_execution = integrated["facets"]["execution"]
            integrated_head = integrated_execution.get("candidate_head")
            if not isinstance(integrated_head, str):
                raise NativeDriverError(
                    "Tester integration produced no candidate HEAD",
                    code="NATIVE_TESTER_INTEGRATION_HEAD_MISSING",
                )
            tester_source = integrated_execution.get("tester_source")
            if not isinstance(tester_source, dict):
                raise NativeDriverError(
                    "Tester integration produced no source identity",
                    code="NATIVE_TESTER_SOURCE_IDENTITY_MISSING",
                )
            self._record_evidence(
                "tester",
                self._bind_tester_evidence_identity(
                    evidence,
                    candidate_head=integrated_head,
                    tester_source=tester_source,
                ),
                action_id,
            )
        elif action["action"] in {"tester_proof", "tester_proof_diagnose"}:
            assert proof_spec is not None
            if not proof_failure_already_persisted:
                try:
                    self.core.call(
                        "prove-tests",
                        "--repo",
                        str(self.repo),
                        "--run",
                        self.run_id,
                        "--spec",
                        "-",
                        "--agent-id",
                        str(agent["agent_id"]),
                        "--thread-id",
                        str(agent["thread_id"]),
                        "--action-id",
                        action_id,
                        "--driver-runtime-kind",
                        "native",
                        input_value=proof_spec,
                    )
                except CorePortError as exc:
                    failed = self._context()
                    if not self._proof_failure_matches(
                        failed.get("proof_failure"),
                        failed.get("proof_failure_state"),
                        action_id,
                        exc,
                    ):
                        raise
        elif action["action"] == "tester_blackbox":
            evidence = result.get("evidence_report")
            if not isinstance(evidence, dict):
                raise NativeDriverError("Tester returned no blackbox evidence", code="NATIVE_BLACKBOX_MISSING")
            if isinstance(context["facets"]["execution"].get("deployment"), dict):
                try:
                    self.core.call(
                        "stage-blackbox",
                        "--repo",
                        str(self.repo),
                        "--run",
                        self.run_id,
                        "--report",
                        "-",
                        "--action-id",
                        action_id,
                        "--driver-runtime-kind",
                        "native",
                        input_value=evidence,
                    )
                except CorePortError as exc:
                    self.core.call(
                        "require-deployment-restore",
                        "--repo",
                        str(self.repo),
                        "--run",
                        self.run_id,
                        "--failure-code",
                        exc.code,
                        "--action-id",
                        action_id,
                        "--driver-runtime-kind",
                        "native",
                    )
                    return
            else:
                self._record_evidence("blackbox", evidence, action_id)
        elif action["action"] == "reviewer_preflight":
            evidence = result.get("evidence_report")
            if not isinstance(evidence, dict):
                raise NativeDriverError(
                    "Reviewer preflight returned no evidence",
                    code="NATIVE_REVIEW_PREFLIGHT_EVIDENCE_MISSING",
                )
            self._record_evidence(
                "reviewer_preflight",
                self._evidence_kind(evidence, "reviewer_preflight"),
                action_id,
            )
        elif action["action"] == "reviewer_final":
            evidence = result.get("evidence_report")
            if not isinstance(evidence, dict):
                raise NativeDriverError("Reviewer returned no evidence", code="NATIVE_REVIEW_EVIDENCE_MISSING")
            required = set(context["facets"]["assurance"]["required"])
            if "reviewer" in required:
                self._record_evidence("reviewer", self._evidence_kind(evidence, "reviewer"), action_id)
            if "doc_review" in required:
                self._record_evidence("doc_review", self._evidence_kind(evidence, "doc_review"), action_id)
        self.core.call(
            "consume-dispatch",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            action_id,
            "--consumer-source",
            "native_driver",
        )

    def _record_evidence(self, kind: str, evidence: dict[str, Any], action_id: str) -> None:
        self.core.call(
            "record-evidence",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--kind",
            kind,
            "--report",
            "-",
            "--action-id",
            action_id,
            "--driver-runtime-kind",
            "native",
            input_value=evidence,
        )

    @staticmethod
    def _evidence_kind(evidence: dict[str, Any], kind: str) -> dict[str, Any]:
        value = copy.deepcopy(evidence)
        value["kind"] = kind
        return value

    @staticmethod
    def _bind_evidence_candidate(evidence: dict[str, Any], candidate_head: str) -> dict[str, Any]:
        value = copy.deepcopy(evidence)
        value["candidate_head"] = candidate_head
        return value

    @staticmethod
    def _bind_tester_evidence_identity(
        evidence: dict[str, Any],
        *,
        candidate_head: str,
        tester_source: dict[str, Any],
    ) -> dict[str, Any]:
        value = copy.deepcopy(evidence)
        value["candidate_head"] = candidate_head
        details = value.get("details")
        if isinstance(details, dict):
            details["source_head"] = tester_source.get("head")
            details["files"] = copy.deepcopy(tester_source.get("files"))
        return value

    @staticmethod
    def _proof_test_id_hints(context: dict[str, Any]) -> list[dict[str, str]]:
        source = context["facets"]["execution"].get("tester_source")
        if not isinstance(source, dict):
            return []
        hints: list[dict[str, str]] = []
        for item in source.get("files", []):
            path = item.get("path")
            if not isinstance(path, str):
                continue
            hint = {"path": path}
            if path.endswith(".py"):
                hint["unittest_module"] = path[:-3].replace("/", ".")
                hint["pytest_prefix"] = path + "::"
            hints.append(hint)
        return hints

    def _bind_proof_test_ids(
        self, spec: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        from ..core import proof_test_source_path

        value = copy.deepcopy(spec)
        facets = context["facets"]
        source = facets["execution"].get("tester_source")
        if not isinstance(source, dict):
            return value
        tester_head = str(source["head"])
        tester_patterns = facets["authority"]["tester_write"]
        modules = [
            item["path"][:-3].replace("/", ".")
            for item in source.get("files", [])
            if isinstance(item.get("path"), str) and item["path"].endswith(".py")
        ]
        for group in value.get("groups", []):
            argv = group.get("argv", [])
            framework = "unittest" if "unittest" in argv else "pytest" if "pytest" in argv else ""
            if not framework:
                continue
            normalized_ids = []
            for test_id in group.get("test_ids", []):
                if proof_test_source_path(
                    self.repo, tester_head, framework, test_id, tester_patterns
                ) is not None:
                    normalized_ids.append(test_id)
                    continue
                candidates: set[str] = set()
                for module in modules:
                    basename = module.rsplit(".", 1)[-1]
                    if framework == "unittest":
                        if test_id == basename or test_id.startswith(basename + "."):
                            candidates.add(module + test_id[len(basename) :])
                        else:
                            candidates.add(module + "." + test_id)
                    elif "::" in test_id:
                        file_part, suffix = test_id.split("::", 1)
                        path = module.replace(".", "/") + ".py"
                        if file_part == Path(path).name:
                            candidates.add(path + "::" + suffix)
                bound = sorted(
                    candidate
                    for candidate in candidates
                    if proof_test_source_path(
                        self.repo, tester_head, framework, candidate, tester_patterns
                    ) is not None
                )
                normalized_ids.append(bound[0] if len(bound) == 1 else test_id)
            group["test_ids"] = normalized_ids
        return value

    def _context(self) -> dict[str, Any]:
        return self.core.call(
            "driver-context", "--repo", str(self.repo), "--run", self.run_id
        )

    def _canonical_projection(
        self,
        action: dict[str, Any],
        role: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        return canonical_context_projection(
            context,
            action_id=action.get("action_id"),
            action=str(action.get("action", "")),
            role=role,
            work_unit_id=(
                str(action["work_unit_id"])
                if isinstance(action.get("work_unit_id"), str)
                else None
            ),
            work_unit=(
                action.get("work_unit")
                if isinstance(action.get("work_unit"), dict)
                else None
            ),
        )

    def _projection_for_action(
        self,
        action: dict[str, Any],
        role: str,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        projected = action.get("context_projection")
        projected_digest = action.get("context_projection_digest")
        if projected is not None or projected_digest is not None:
            if not isinstance(projected, Mapping) or not isinstance(
                projected_digest, str
            ):
                raise NativeDriverError(
                    "Core returned an incomplete context projection",
                    code="NATIVE_CONTEXT_PROJECTION_INVALID",
                    status="NEEDS_USER",
                    details={
                        "action_id": action.get("action_id"),
                        "has_projection": isinstance(projected, Mapping),
                        "has_digest": isinstance(projected_digest, str),
                    },
                )
            projection = copy.deepcopy(dict(projected))
            actual_digest = digest(projection)
            if actual_digest != projected_digest:
                raise NativeDriverError(
                    "Core context projection digest changed on the Native wire",
                    code="NATIVE_CONTEXT_PROJECTION_DIGEST_MISMATCH",
                    status="NEEDS_USER",
                    details={
                        "action_id": action.get("action_id"),
                        "expected": projected_digest,
                        "actual": actual_digest,
                    },
                )
            return projection, projected_digest
        projection = self._canonical_projection(action, role, context)
        return projection, digest(projection)

    def _spawn_claim_id(self, operation: str, intent: dict[str, Any]) -> str:
        return digest(
            {
                "operation": operation,
                "run_id": self.run_id,
                "action_id": intent.get("action_id"),
                "work_unit_id": intent.get("work_unit_id"),
                "source_thread_id": intent.get("source_thread_id"),
                "source_generation": intent.get("source_generation"),
                "source_attempt": intent.get("source_attempt"),
            }
        )

    def _reconstruct_dispatch_action(
        self, context: dict[str, Any], pending: dict[str, Any]
    ) -> dict[str, Any]:
        from ..assurance_v4.driver import _dispatch_action

        return _dispatch_action(context, self.run_id, pending)

    def _rehydrate_dispatch(self, action: dict[str, Any]) -> None:
        if self.transport is None:
            raise NativeDriverError(
                "Native transport is required for dispatch rehydration",
                code="NATIVE_REHYDRATION_TRANSPORT_REQUIRED",
                status="NEEDS_USER",
            )
        context = self._context()
        intent = context.get("dispatch_rehydration_intent")
        pending = context.get("dispatch_intent")
        if (
            not isinstance(intent, dict)
            or not isinstance(pending, dict)
            or intent.get("action_id") != action.get("action_id")
        ):
            raise NativeDriverError(
                "dispatch rehydration intent is missing",
                code="NATIVE_REHYDRATION_INTENT_MISSING",
                status="NEEDS_USER",
            )
        if intent.get("state") != "prepared":
            raise NativeDriverError(
                "dispatch rehydration intent is not ready",
                code="NATIVE_REHYDRATION_INTENT_INVALID",
                status="NEEDS_USER",
            )
        if intent.get("spawn_state") != "spawned":
            raise NativeDriverError(
                "dispatch rehydration has an unresolved thread spawn",
                code="NATIVE_REHYDRATION_SPAWN_UNRESOLVED",
                status="NEEDS_USER",
                details={"intent": copy.deepcopy(intent)},
            )
        new_thread_id = intent.get("new_thread_id")
        new_agent_id = intent.get("new_agent_id")
        if not isinstance(new_thread_id, str) or not isinstance(
            new_agent_id, str
        ):
            raise NativeDriverError(
                "dispatch rehydration spawn identity is missing",
                code="NATIVE_REHYDRATION_SPAWN_IDENTITY_MISSING",
                status="NEEDS_USER",
            )
        try:
            self.transport.read_thread(new_thread_id)
        except AppServerError as exc:
            raise NativeDriverError(
                "recorded rehydration thread cannot be read",
                code="NATIVE_REHYDRATION_THREAD_UNAVAILABLE",
                status="NEEDS_USER",
                details={"thread_id": new_thread_id, "source_code": exc.code},
            ) from exc
        role = str(intent["role"])
        current = self._reconstruct_dispatch_action(context, pending)
        thread_id = new_thread_id
        agent_id = new_agent_id
        prospective = copy.deepcopy(context)
        prospective_facets = prospective.get("facets")
        if isinstance(prospective_facets, dict):
            execution = prospective_facets.get("execution")
            if isinstance(execution, dict):
                execution.setdefault("agents", {})[role] = {
                    "agent_id": agent_id,
                    "thread_id": thread_id,
                }
                source = execution.get("tester_source")
                if role == "tester" and isinstance(source, dict):
                    source["agent"] = copy.deepcopy(execution["agents"][role])
        prospective_action = self._reconstruct_dispatch_action(
            prospective, pending
        )
        new_prompt_digest = digest(
            self._prompt(prospective_action, role, prospective)
        )
        self.core.call(
            "bind-dispatch-rehydration",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(intent["action_id"]),
            "--new-agent-id",
            agent_id,
            "--new-thread-id",
            thread_id,
            "--prompt-digest",
            new_prompt_digest,
            "--driver-runtime-kind",
            "native",
        )
        self._active_threads.add(thread_id)

    def _start_canonical_rehydration(
        self, action_id: str, action_name: str | None = None
    ) -> bool:
        if self.transport is None:
            return False
        if not isinstance(self.current_action, dict) or not isinstance(
            self.current_action.get("work_unit_id"), str
        ):
            return False
        context = self._context()
        pending = context.get("dispatch_intent")
        existing_intent = context.get("dispatch_rehydration_intent")
        if isinstance(existing_intent, dict):
            if (
                existing_intent.get("action_id") == action_id
                and existing_intent.get("spawn_state") == "spawned"
                and isinstance(pending, dict)
            ):
                self._rehydrate_dispatch(
                    {
                        "action": pending.get("action"),
                        "action_id": action_id,
                        "work_unit_id": pending.get("work_unit_id"),
                    }
                )
                return True
            return False
        if (
            not isinstance(pending, dict)
            or pending.get("action_id") != action_id
            or pending.get("state") != "exhausted"
            or not isinstance(pending.get("work_unit_id"), str)
        ):
            return False
        role = str(pending.get("role"))
        if role == "builder" and self.builder_mode == "root_session":
            return False
        try:
            thread = self.transport.read_thread(str(pending.get("thread_id")))
        except AppServerError:
            return False
        if not self._empty_rehydratable_tail(pending, thread):
            return False
        action = self.current_action
        if not isinstance(action, dict) or action.get("action_id") != action_id:
            action = {
                "action": pending.get("action"),
                "action_id": action_id,
                "work_unit_id": pending.get("work_unit_id"),
            }
        projection, projection_digest = self._projection_for_action(
            action, role, context
        )
        self.core.call(
            "begin-dispatch-rehydration",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            action_id,
            "--context-projection-digest",
            projection_digest,
            "--driver-runtime-kind",
            "native",
        )
        refreshed = self._context()
        intent = refreshed.get("dispatch_rehydration_intent")
        if not isinstance(intent, dict):
            return True
        if intent.get("state") != "prepared":
            raise NativeDriverError(
                "dispatch rehydration intent is not prepared",
                code="NATIVE_REHYDRATION_INTENT_INVALID",
                status="NEEDS_USER",
                details={"intent": intent},
            )
        if intent.get("spawn_state") == "spawned":
            self._rehydrate_dispatch(
                {
                    "action": pending.get("action"),
                    "action_id": action_id,
                    "work_unit_id": pending.get("work_unit_id"),
                }
            )
            return True
        if intent.get("spawn_state") == "claimed":
            raise NativeDriverError(
                "dispatch rehydration spawn may have happened before interruption",
                code="NATIVE_REHYDRATION_SPAWN_UNRESOLVED",
                status="NEEDS_USER",
                details={"intent": copy.deepcopy(intent)},
            )
        claim_id = self._spawn_claim_id("dispatch_rehydration", intent)
        self.core.call(
            "claim-dispatch-rehydration",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            action_id,
            "--claim-id",
            claim_id,
            "--driver-runtime-kind",
            "native",
        )
        refreshed = self._context()
        intent = refreshed.get("dispatch_rehydration_intent")
        if not isinstance(intent, dict) or intent.get("spawn_state") != "claimed":
            raise NativeDriverError(
                "dispatch rehydration spawn claim was not persisted",
                code="NATIVE_REHYDRATION_SPAWN_CLAIM_MISSING",
                status="NEEDS_USER",
            )
        instructions, _sandbox = self._role_config(role)
        thread_id = self.transport.start_thread(
            cwd=self._turn_cwd(action, role, refreshed),
            developer_instructions=instructions,
            sandbox="danger-full-access",
        )
        agent_id = f"codex-app-server:{thread_id}"
        self.core.call(
            "record-dispatch-rehydration-spawn",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            action_id,
            "--claim-id",
            claim_id,
            "--new-agent-id",
            agent_id,
            "--new-thread-id",
            thread_id,
            "--driver-runtime-kind",
            "native",
        )
        refreshed = self._context()
        prospective = copy.deepcopy(refreshed)
        prospective_facets = prospective.get("facets")
        if isinstance(prospective_facets, dict):
            execution = prospective_facets.get("execution")
            if isinstance(execution, dict):
                execution.setdefault("agents", {})[role] = {
                    "agent_id": agent_id,
                    "thread_id": thread_id,
                }
                source = execution.get("tester_source")
                if role == "tester" and isinstance(source, dict):
                    source["agent"] = copy.deepcopy(execution["agents"][role])
        prospective_action = self._reconstruct_dispatch_action(
            prospective, pending
        )
        new_prompt_digest = digest(
            self._prompt(prospective_action, role, prospective)
        )
        self.core.call(
            "bind-dispatch-rehydration",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            action_id,
            "--new-agent-id",
            agent_id,
            "--new-thread-id",
            thread_id,
            "--prompt-digest",
            new_prompt_digest,
            "--driver-runtime-kind",
            "native",
        )
        self._active_threads.add(thread_id)
        return True

    def _rotate_context(self, action: dict[str, Any]) -> None:
        if self.transport is None:
            raise NativeDriverError(
                "Native transport is required for context rotation",
                code="NATIVE_CONTEXT_ROTATION_TRANSPORT_REQUIRED",
                status="NEEDS_USER",
            )
        context = self._context()
        intent = context.get("context_rotation_intent")
        if not isinstance(intent, dict) or intent.get("state") != "prepared":
            raise NativeDriverError(
                "context rotation intent is missing",
                code="NATIVE_CONTEXT_ROTATION_INTENT_MISSING",
                status="NEEDS_USER",
            )
        if intent.get("spawn_state") != "spawned":
            raise NativeDriverError(
                "context rotation has an unresolved thread spawn",
                code="NATIVE_CONTEXT_ROTATION_SPAWN_UNRESOLVED",
                status="NEEDS_USER",
                details={"intent": copy.deepcopy(intent)},
            )
        thread_id = intent.get("new_thread_id")
        agent_id = intent.get("new_agent_id")
        if not isinstance(thread_id, str) or not isinstance(agent_id, str):
            raise NativeDriverError(
                "context rotation spawn identity is missing",
                code="NATIVE_CONTEXT_ROTATION_SPAWN_IDENTITY_MISSING",
                status="NEEDS_USER",
            )
        try:
            self.transport.read_thread(thread_id)
        except AppServerError as exc:
            raise NativeDriverError(
                "recorded context rotation thread cannot be read",
                code="NATIVE_CONTEXT_ROTATION_THREAD_UNAVAILABLE",
                status="NEEDS_USER",
                details={"thread_id": thread_id, "source_code": exc.code},
            ) from exc
        role = str(intent["role"])
        current = {
            "action": "work_unit",
            "action_id": action.get("action_id"),
            "work_unit_id": intent.get("work_unit_id"),
        }
        self.core.call(
            "bind-context-rotation",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--new-agent-id",
            agent_id,
            "--new-thread-id",
            thread_id,
            "--driver-runtime-kind",
            "native",
        )
        self._active_threads.add(thread_id)

    def _start_context_rotation(
        self, action: dict[str, Any], role: str, context: dict[str, Any]
    ) -> bool:
        if (
            self.transport is None
            or (self.builder_mode == "root_session" and role == "builder")
        ):
            return False
        if not isinstance(action.get("work_unit_id"), str):
            return False
        if context.get("dispatch_intent") is not None:
            return False
        progress = context.get("work_unit_progress")
        if not isinstance(progress, dict):
            return False
        agent = (
            context.get("facets", {})
            .get("execution", {})
            .get("agents", {})
            .get(role)
        )
        if not isinstance(agent, dict) or not isinstance(
            agent.get("thread_id"), str
        ):
            return False
        usage = progress.get("thread_usage", {}).get(agent["thread_id"], 0)
        limit = (
            context.get("progress_policy", {}).get("max_units_per_thread")
            if isinstance(context.get("progress_policy"), dict)
            else None
        )
        if not isinstance(limit, int) or usage < limit:
            return False
        _projection, projection_digest = self._projection_for_action(
            action, role, context
        )
        self.core.call(
            "begin-context-rotation",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--role",
            role,
            "--work-unit-id",
            str(action["work_unit_id"]),
            "--action-id",
            str(action.get("action_id")),
            "--action",
            str(action.get("action", "work_unit")),
            "--context-projection-digest",
            projection_digest,
            "--driver-runtime-kind",
            "native",
        )
        refreshed = self._context()
        intent = refreshed.get("context_rotation_intent")
        if not isinstance(intent, dict):
            return True
        if intent.get("state") != "prepared":
            raise NativeDriverError(
                "context rotation intent is invalid",
                code="NATIVE_CONTEXT_ROTATION_INTENT_INVALID",
                status="NEEDS_USER",
                details={"intent": intent},
            )
        if intent.get("spawn_state") == "spawned":
            self._rotate_context(
                {
                    "action_id": action.get("action_id"),
                    "work_unit_id": action.get("work_unit_id"),
                }
            )
            return True
        if intent.get("spawn_state") == "claimed":
            raise NativeDriverError(
                "context rotation spawn may have happened before interruption",
                code="NATIVE_CONTEXT_ROTATION_SPAWN_UNRESOLVED",
                status="NEEDS_USER",
                details={"intent": copy.deepcopy(intent)},
            )
        claim_id = self._spawn_claim_id("context_rotation", intent)
        self.core.call(
            "claim-context-rotation",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(intent["action_id"]),
            "--claim-id",
            claim_id,
            "--driver-runtime-kind",
            "native",
        )
        refreshed = self._context()
        intent = refreshed.get("context_rotation_intent")
        if not isinstance(intent, dict) or intent.get("spawn_state") != "claimed":
            raise NativeDriverError(
                "context rotation spawn claim was not persisted",
                code="NATIVE_CONTEXT_ROTATION_SPAWN_CLAIM_MISSING",
                status="NEEDS_USER",
            )
        instructions, _sandbox = self._role_config(role)
        thread_id = self.transport.start_thread(
            cwd=self._turn_cwd(action, role, refreshed),
            developer_instructions=instructions,
            sandbox="danger-full-access",
        )
        agent_id = f"codex-app-server:{thread_id}"
        self.core.call(
            "record-context-rotation-spawn",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(intent["action_id"]),
            "--claim-id",
            claim_id,
            "--new-agent-id",
            agent_id,
            "--new-thread-id",
            thread_id,
            "--driver-runtime-kind",
            "native",
        )
        self._rotate_context(
            {
                "action_id": action.get("action_id"),
                "work_unit_id": action.get("work_unit_id"),
            }
        )
        return True

    def _activate_role_thread(
        self,
        action: dict[str, Any],
        role: str,
        context: dict[str, Any],
        thread_id: str,
    ) -> None:
        if thread_id in self._active_threads:
            return
        instructions, _sandbox = self._role_config(role)
        self.transport.resume_thread(
            thread_id=thread_id,
            cwd=self._turn_cwd(action, role, context),
            developer_instructions=instructions,
            sandbox="danger-full-access",
        )
        self._active_threads.add(thread_id)

    def _role_config(self, role: str) -> tuple[str, str]:
        value = tomllib.loads((self.project_root / "agents" / f"{role}.toml").read_text())
        instructions = str(value["developer_instructions"])
        instructions += (
            "\nNative Driver transport rule: return the raw JSON object required by the supplied "
            "output schema. Do not add marker lines, Markdown fences, commentary, or extra keys. "
            "Each non-null evidence_report, proof_spec, or problem_report value must be a compact "
            "JSON string; the Native Driver normalizes it back to the public object schema."
        )
        instructions += (
            "\nAssurance v4 recovery rule: when the supplied contract enables "
            "execution.recovery_policy.mode=automatic_nonsemantic, an owner=plan problem may use "
            "decision_request.kind=engineering_correction only for one exact Assurance-facet "
            "delta that preserves Mission, Authority, acceptance, trust boundaries, external "
            "targets and existing command semantics while monotonically strengthening assurance. "
            "Use facet_change or product_choice for every other plan-owned decision; do not label "
            "an uncertain or semantic change as engineering_correction."
        )
        return instructions, str(value["sandbox_mode"])

    def _prompt(self, action: dict[str, Any], role: str, context: dict[str, Any]) -> str:
        payload = {
            "assurance_schema_version": 4,
            "run_id": self.run_id,
            "action_id": action["action_id"],
            "action": action["action"],
            "phase": {
                "builder_recompose_fix": "recompose",
                "tester_author": "author",
                "tester_fix": "author",
                "tester_recompose_fix": "recompose",
                "tester_proof": "proof",
                "tester_proof_diagnose": "proof_diagnose",
                "tester_machine_diagnose": "machine_diagnose",
                "tester_blackbox": "blackbox",
                "reviewer_preflight": "preflight",
                "reviewer_final": "final",
            }.get(str(action["action"]), "implement"),
            "role": role,
            "contract": context["facets"],
            "target_start_head": context["target_start_head"],
            "candidate_worktree": action.get("candidate_worktree", context["candidate_worktree"]),
            "publication": context.get("publication"),
            "recomposition": action.get("recomposition"),
            "evidence": context.get("evidence"),
            "doc_reference_scan": context.get("doc_reference_scan"),
            "doc_reference_scan_state": context.get("doc_reference_scan_state"),
            "problems": context.get("problems"),
            "problem_report_schema": self.problem_schema,
            "result_field_contract": self._result_field_contract(str(action["action"])),
        }
        if (
            isinstance(context.get("progress_policy"), dict)
            and context["progress_policy"].get("mode") == "bounded_rehydration"
        ):
            projection, projection_digest = self._projection_for_action(
                action, role, context
            )
            payload["context_source"] = "canonical"
            payload["canonical_projection"] = projection
            payload["context_projection_digest"] = projection_digest
            payload["work_unit_id"] = action.get("work_unit_id")
            payload["work_unit"] = copy.deepcopy(action.get("work_unit"))
            payload["work_unit_progress"] = copy.deepcopy(
                projection.get("work_unit_progress")
            )
        if role in {"tester", "reviewer"}:
            payload["evidence_report_schema"] = self.evidence_schema
            payload["blackbox_case_schema"] = self.blackbox_case_schema
        if role == "tester":
            payload["test_identity_contract"] = {
                "unittest": (
                    "Canonical test ids are repository-root dotted source paths plus class and "
                    "method, for example tests.test_calc.Case.test_value. Author package "
                    "__init__.py files when needed so those ids are importable. For proof, use "
                    "the canonical ids as unittest argv instead of a discovery command that "
                    "reports shorter start-directory-relative ids."
                ),
                "pytest": (
                    "Canonical test ids start with the repository-relative Tester-owned file, "
                    "for example tests/test_calc.py::test_value."
                ),
            }
        if role == "reviewer":
            payload["prompt_contract_version"] = 2
            payload["review_input_contract"] = self._review_input_contract(
                context, phase=str(payload["phase"])
            )
        if action.get("action") in {"tester_proof", "tester_proof_diagnose"}:
            payload["proof_spec_schema"] = self.proof_schema
            payload["proof_test_id_hints"] = self._proof_test_id_hints(context)
            if action.get("action") == "tester_proof":
                payload["proof_execution_rule"] = (
                    "Choose argv that executes exactly the declared canonical test_ids. Do not reuse "
                    "unittest discover -s with ids that include the omitted start-directory prefix."
                )
        if action.get("action") == "tester_proof_diagnose":
            payload["proof_failure"] = copy.deepcopy(action.get("proof_failure"))
            payload["proof_diagnosis_rule"] = (
                "Do not edit files or rerun the proof as a replacement for Core. Classify each "
                "independent cause from the frozen contract, persisted spec, structured result, "
                "and bound artifacts. When only proof execution input is wrong, return result="
                "tests_ready with one changed replacement proof_spec and no problem_report; Core "
                "alone reruns it. Use owner=builder for a candidate implementation defect, "
                "owner=tester for Tester-owned source or fixture changes, and owner=plan only when "
                "the frozen target, authority, or acceptance contract must change."
            )
        if action.get("action") == "tester_machine_diagnose":
            payload["machine_failure"] = copy.deepcopy(action.get("machine_failure"))
            payload["machine_diagnosis_rule"] = (
                "Do not edit files or rerun the command as a replacement for Core. Classify each "
                "independent failure from the frozen contract, persisted command result, Tester "
                "source and bound artifacts. Route implementation defects to builder, Tester-owned "
                "tests or harness defects to tester, frozen-contract changes to plan, repository "
                "defects to current_project, Builder-loop defects to builder_loop, and environment "
                "failures to external_platform."
            )
        json_options: dict[str, Any] = {
            "ensure_ascii": False,
            "sort_keys": True,
        }
        if role == "reviewer":
            json_options["separators"] = (",", ":")
        else:
            json_options["indent"] = 2
        return "CBL_ACTION_ID:" + str(action["action_id"]) + "\n" + json.dumps(
            payload, **json_options
        )

    @staticmethod
    def _prompt_source_ref(value: Any, pointer: str) -> dict[str, Any]:
        return {
            "source": "current_prompt_payload",
            "json_pointer": pointer,
            "digest": digest(value),
        }

    def _review_input_contract(
        self, context: dict[str, Any], *, phase: str
    ) -> dict[str, Any]:
        facets = context["facets"]
        mission = facets["mission"]
        execution = facets["execution"]
        documentation_only = mission.get("delivery_kind") == "documentation"
        spec_head = str(context["target_start_head"])
        candidate_head = str(execution["candidate_head"])
        return {
            "review_phase": phase,
            "accepted_plan": self._prompt_source_ref(facets, "/contract"),
            "spec_head": spec_head,
            "candidate_head": candidate_head,
            "integrated_head": candidate_head,
            "complete_diff": {
                "source": "bound_git_range",
                "cwd": context["candidate_worktree"],
                "argv": [
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--binary",
                    f"{spec_head}..{candidate_head}",
                ],
                "required": True,
            },
            "verification_mode": (
                "L1-documentation-only" if documentation_only else "assurance-v4"
            ),
            "plan_level": "L1" if documentation_only else "assurance-v4",
            "plan_checklist": {
                "behaviors": mission.get("behaviors", []),
                "acceptance_cases": mission.get("acceptance_cases", []),
                "trust_boundaries": mission.get("trust_boundaries", []),
            },
            "documentation_spec": (
                {
                    "objective": mission.get("objective"),
                    "acceptance_cases": mission.get("acceptance_cases", []),
                    "authorized_paths": facets["authority"].get("builder_write", []),
                }
                if documentation_only
                else None
            ),
            "documentation_policy_path": str(self.project_root / "policies" / "doc-policy.md"),
            "doc_reference_scan": self._prompt_source_ref(
                context.get("doc_reference_scan"), "/doc_reference_scan"
            ),
            "doc_reference_scan_state": context.get("doc_reference_scan_state"),
            "pre_turn_gates": {
                "required": (
                    [
                        *(
                            ["tester"]
                            if "tester" in facets["assurance"].get("required", [])
                            else []
                        ),
                        *(
                            ["preflight"]
                            if facets["assurance"].get("preflight_before_proof")
                            and any(
                                item.get("run_before_full_suite")
                                for item in facets["assurance"].get(
                                    "machine_commands", []
                                )
                            )
                            else []
                        ),
                    ]
                    if phase == "preflight"
                    else facets["assurance"].get("required", [])
                ),
                "evidence": self._prompt_source_ref(
                    context.get("evidence", {}), "/evidence"
                ),
                "doc_reference_scan": self._prompt_source_ref(
                    context.get("doc_reference_scan"), "/doc_reference_scan"
                ),
                "doc_reference_scan_state": context.get("doc_reference_scan_state"),
                "publication": self._prompt_source_ref(
                    context.get("publication"), "/publication"
                ),
                "requirement_rule": (
                    "Only names in required are mandatory. When tester is absent, Tester "
                    "author, source, integration, and tester evidence are not gates; blackbox "
                    "still requires the ledger-bound Tester identity."
                ),
            },
            "mapping_note": (
                "Resolve each current_prompt_payload JSON Pointer against this same prompt and "
                "verify its digest before use. Assurance v4 uses /contract as the accepted plan; "
                "its mission behaviors, acceptance cases, and trust boundaries are the plan "
                "checklist. For an L1 documentation delivery they are also the documentation "
                "specification. Legacy sidecar plan-checklist or documentation-spec files are not "
                "separate v4 gates."
            ),
        }

    @staticmethod
    def _result_field_contract(action: str) -> dict[str, str]:
        if action in {
            "builder_implement",
            "builder_fix",
            "builder_recompose_fix",
            "tester_recompose_fix",
        }:
            return {
                "evidence_report": "must_be_null",
                "proof_spec": "must_be_null",
                "problem_report": "object_only_when_blocked_or_target_change_required_else_null",
            }
        if action == "tester_proof_diagnose":
            return {
                "evidence_report": "must_be_null",
                "proof_spec": "changed_replacement_object_for_spec_only_correction_else_null",
                "problem_report": "required_non_empty_for_source_or_contract_problem_else_null",
            }
        if action == "tester_machine_diagnose":
            return {
                "evidence_report": "must_be_null",
                "proof_spec": "must_be_null",
                "problem_report": "required_non_empty_with_supported_problem_owner",
            }
        return {
            "evidence_report": "required_on_pass_else_null",
            "proof_spec": "must_be_null",
            "problem_report": "object_only_when_findings_blocked_or_target_change_required_else_null",
        }

    @staticmethod
    def _normalize_action_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(result)
        if isinstance(value.get("problem_report"), dict) and action != "tester_proof_diagnose":
            value["evidence_report"] = None
            value["proof_spec"] = None
            return value
        if action in {
            "builder_implement",
            "builder_fix",
            "builder_recompose_fix",
            "tester_recompose_fix",
        }:
            value["evidence_report"] = None
            value["proof_spec"] = None
        elif action == "tester_proof":
            value["evidence_report"] = None
        elif action == "tester_proof_diagnose":
            value["evidence_report"] = None
        elif action == "tester_machine_diagnose":
            value["evidence_report"] = None
            value["proof_spec"] = None
        else:
            value["proof_spec"] = None
        return value

    @staticmethod
    def _validate_proof_diagnosis(
        result: dict[str, Any],
        *,
        proof_failure: Any = None,
    ) -> None:
        spec = result.get("proof_spec")
        report = result.get("problem_report")
        problems = report.get("problems") if isinstance(report, dict) else None
        if isinstance(spec, dict):
            if isinstance(report, dict):
                raise NativeDriverError(
                    "proof diagnosis returned both a replacement spec and a problem",
                    code="NATIVE_PROOF_DIAGNOSIS_AMBIGUOUS",
                )
            if result.get("result") != "tests_ready":
                raise NativeDriverError(
                    "proof diagnosis returned an invalid replacement result",
                    code="NATIVE_PROOF_DIAGNOSIS_INVALID",
                )
            prior_digest = (
                proof_failure.get("spec_digest")
                if isinstance(proof_failure, dict)
                else None
            )
            if not isinstance(prior_digest, str):
                raise NativeDriverError(
                    "proof diagnosis has no persisted source spec",
                    code="NATIVE_PROOF_DIAGNOSIS_FAILURE_MISSING",
                )
            if digest(spec) == prior_digest:
                raise NativeDriverError(
                    "proof diagnosis replacement spec did not change",
                    code="NATIVE_PROOF_DIAGNOSIS_NO_PROGRESS",
                )
            return
        if result.get("result") not in {"fail", "blocked", "target_change_required"}:
            raise NativeDriverError(
                "proof diagnosis returned an invalid terminal result",
                code="NATIVE_PROOF_DIAGNOSIS_INVALID",
            )
        if not isinstance(problems, list) or not problems:
            raise NativeDriverError(
                "proof diagnosis returned no problems",
                code="NATIVE_PROOF_DIAGNOSIS_MISSING",
            )
        invalid = [
            item.get("owner")
            for item in problems
            if not isinstance(item, dict)
            or item.get("owner") not in {"builder", "tester", "plan"}
        ]
        if invalid:
            raise NativeDriverError(
                "proof diagnosis returned an unsupported owner",
                code="NATIVE_PROOF_DIAGNOSIS_OWNER_INVALID",
                details={"owners": invalid},
            )

    @staticmethod
    def _validate_machine_diagnosis(result: dict[str, Any]) -> None:
        report = result.get("problem_report")
        problems = report.get("problems") if isinstance(report, dict) else None
        if result.get("result") not in {"fail", "blocked", "target_change_required"}:
            raise NativeDriverError(
                "machine diagnosis returned an invalid terminal result",
                code="NATIVE_MACHINE_DIAGNOSIS_INVALID",
            )
        if not isinstance(problems, list) or not problems:
            raise NativeDriverError(
                "machine diagnosis returned no problems",
                code="NATIVE_MACHINE_DIAGNOSIS_MISSING",
            )
        allowed = {
            "builder",
            "tester",
            "plan",
            "current_project",
            "builder_loop",
            "external_platform",
        }
        invalid = [
            item.get("owner")
            for item in problems
            if not isinstance(item, dict) or item.get("owner") not in allowed
        ]
        if invalid:
            raise NativeDriverError(
                "machine diagnosis returned an unsupported owner",
                code="NATIVE_MACHINE_DIAGNOSIS_OWNER_INVALID",
                details={"owners": invalid},
            )

    @staticmethod
    def _proof_failure_matches(
        value: Any,
        state: Any,
        action_id: str,
        error: CorePortError,
    ) -> bool:
        failure = value.get("failure") if isinstance(value, dict) else None
        return bool(
            state == "current"
            and value.get("action_id") == action_id
            and isinstance(value.get("failure_digest"), str)
            and isinstance(failure, dict)
            and failure.get("code") == error.code
            and failure.get("status") == error.status
        )

    @staticmethod
    def _persisted_proof_failure_matches(
        value: Any,
        state: Any,
        action_id: str,
        spec: dict[str, Any],
        agent: dict[str, Any],
    ) -> bool:
        producer = value.get("producer") if isinstance(value, dict) else None
        return bool(
            isinstance(value, dict)
            and state == "current"
            and value.get("action_id") == action_id
            and value.get("spec") == spec
            and value.get("spec_digest") == digest(spec)
            and isinstance(value.get("failure_digest"), str)
            and producer
            == {
                "role": "tester",
                "agent_id": agent.get("agent_id"),
                "thread_id": agent.get("thread_id"),
            }
        )

    def _turn_cwd(self, action: dict[str, Any], role: str, context: dict[str, Any]) -> str:
        if action.get("action") == "builder_recompose_fix":
            return str(action["candidate_worktree"])
        if action.get("action") == "tester_recompose_fix":
            source = action.get("tester_source")
            if isinstance(source, dict) and isinstance(source.get("worktree"), str):
                return str(source["worktree"])
        if role == "builder" or action.get("action") in {
            "tester_blackbox",
            "reviewer_preflight",
            "reviewer_final",
        }:
            return str(context["candidate_worktree"])
        source = context["facets"]["execution"].get("tester_source")
        if role == "tester" and isinstance(source, dict):
            return str(source["worktree"])
        return str(context["repo_root"])

    def _sandbox_policy(
        self, action: dict[str, Any], role: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        return {"type": "dangerFullAccess"}

    def _start_reviewer_thread_compaction(
        self, action_id: str
    ) -> dict[str, Any] | None:
        if not self._thread_compaction_available:
            return None
        context = self._context()
        pending = context.get("dispatch_intent")
        if not isinstance(pending, dict) or pending.get("action_id") != action_id:
            return None
        thread_id = pending.get("thread_id")
        if not isinstance(thread_id, str):
            return None
        try:
            thread = self.transport.read_thread(thread_id)
        except AppServerError as exc:
            raise NativeDriverError(
                "Reviewer thread could not be inspected before compaction",
                code="NATIVE_DISPATCH_COMPACTION_INSPECTION_FAILED",
                status="NEEDS_USER",
                details={"source_code": exc.code, "source_details": exc.details},
            ) from exc
        return self._recover_reviewer_thread_compaction(pending, thread)

    def _recover_reviewer_thread_compaction(
        self, pending: dict[str, Any], thread: dict[str, Any]
    ) -> dict[str, Any] | None:
        recovery = pending.get("compaction_recovery")
        if isinstance(recovery, dict) and recovery.get("state") == "completed":
            return None
        eligible = bool(
            pending.get("state") == "exhausted"
            and int(pending.get("attempt", 1)) >= 3
            and pending.get("role") == "reviewer"
            and pending.get("action") in {"reviewer_preflight", "reviewer_final"}
            and pending.get("failure_code") in REVIEWER_COMPACTION_FAILURE_CODES
        )
        if not eligible:
            return None
        if not self._thread_compaction_available:
            if isinstance(recovery, dict) and recovery.get("state") == "prepared":
                raise NativeDriverError(
                    "persisted Reviewer compaction requires an unavailable App Server capability",
                    code="NATIVE_DISPATCH_COMPACTION_UNAVAILABLE",
                    status="NEEDS_USER",
                )
            return None
        turns = self._thread_turns(thread)
        if not isinstance(recovery, dict):
            if not turns or not self._empty_exhausted_tail(pending, turns[-1]):
                return None
            self.core.call(
                "prepare-dispatch-compaction",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                str(pending["action_id"]),
                "--prior-turn-count",
                str(len(turns)),
                "--prior-tail-turn-id",
                str(turns[-1]["id"]),
                "--prior-turns-digest",
                digest(turns),
                "--driver-runtime-kind",
                "native",
            )
            refreshed = self._context().get("dispatch_intent")
            if not isinstance(refreshed, dict):
                raise NativeDriverError(
                    "prepared Reviewer compaction disappeared",
                    code="NATIVE_DISPATCH_COMPACTION_INTENT_MISSING",
                    status="NEEDS_USER",
                )
            pending = refreshed
            recovery = pending.get("compaction_recovery")
        if not isinstance(recovery, dict) or recovery.get("state") != "prepared":
            raise NativeDriverError(
                "Reviewer compaction intent is invalid",
                code="NATIVE_DISPATCH_COMPACTION_INTENT_INVALID",
                status="NEEDS_USER",
            )
        return self._complete_reviewer_thread_compaction(pending, thread)

    @staticmethod
    def _thread_turns(thread: dict[str, Any]) -> list[dict[str, Any]]:
        raw = thread.get("turns")
        if not isinstance(raw, list) or any(
            not isinstance(turn, dict)
            or not isinstance(turn.get("id"), str)
            or not isinstance(turn.get("items", []), list)
            for turn in raw
        ):
            raise NativeDriverError(
                "Reviewer thread history is not readable",
                code="NATIVE_DISPATCH_COMPACTION_THREAD_INVALID",
                status="NEEDS_USER",
            )
        return raw

    def _empty_exhausted_tail(
        self, pending: dict[str, Any], turn: dict[str, Any]
    ) -> bool:
        if (
            turn.get("id") != pending.get("turn_id")
            or self._turn_agent_text(turn) is not None
        ):
            return False
        if any(
            not isinstance(item, dict)
            or item.get("type") not in {"reasoning"}
            for item in turn.get("items", [])
        ):
            return False
        status = turn.get("status")
        failure_code = pending.get("failure_code")
        if status == "failed":
            return self._turn_failure_code(turn) == failure_code
        return status == "completed" and failure_code == "missingAgentResult"

    def _empty_rehydratable_tail(
        self, pending: dict[str, Any], thread: dict[str, Any]
    ) -> bool:
        turns = self._thread_turns(thread)
        if not turns:
            return False
        turn = turns[-1]
        if (
            turn.get("id") != pending.get("turn_id")
            or self._turn_agent_text(turn) is not None
            or turn.get("status") not in {"failed", "completed"}
        ):
            return False
        failure_code = pending.get("failure_code")
        if turn.get("status") == "failed" and self._turn_failure_code(turn) != failure_code:
            return False
        if turn.get("status") == "completed" and failure_code != "missingAgentResult":
            return False
        return all(
            isinstance(item, dict) and item.get("type") in {"reasoning"}
            for item in turn.get("items", [])
        )

    def _complete_reviewer_thread_compaction(
        self, pending: dict[str, Any], thread: dict[str, Any]
    ) -> dict[str, Any]:
        recovery = pending["compaction_recovery"]
        turns = self._thread_turns(thread)
        prior_count = int(recovery["prior_turn_count"])
        if (
            len(turns) < prior_count
            or turns[prior_count - 1].get("id") != recovery["prior_tail_turn_id"]
            or digest(turns[:prior_count]) != recovery["prior_turns_digest"]
        ):
            raise NativeDriverError(
                "Reviewer thread changed before compaction recovery",
                code="NATIVE_DISPATCH_COMPACTION_PREFIX_DRIFT",
                status="NEEDS_USER",
            )
        appended = turns[prior_count:]
        if len(appended) > 1:
            raise NativeDriverError(
                "Reviewer thread gained unrelated turns during compaction recovery",
                code="NATIVE_DISPATCH_COMPACTION_TAIL_DRIFT",
                status="NEEDS_USER",
                details={"appended_turn_ids": [turn.get("id") for turn in appended]},
            )
        transport_result = None
        try:
            if not appended:
                self._emit_event(
                    {
                        "event": "native_driver_thread_compaction_started",
                        "run_id": self.run_id,
                        "action_id": pending.get("action_id"),
                        "thread_id": recovery["thread_id"],
                        "source_generation": recovery["source_generation"],
                    }
                )
                transport_result = self.transport.compact_thread(
                    str(recovery["thread_id"])
                )
                thread = self.transport.read_thread(str(recovery["thread_id"]))
                turns = self._thread_turns(thread)
                appended = turns[prior_count:]
            elif appended[0].get("status") in {"inProgress", "in_progress"}:
                turn = self.transport.wait_turn(
                    thread_id=str(recovery["thread_id"]),
                    turn_id=str(appended[0]["id"]),
                )
                if turn.status != "completed":
                    raise AppServerError(
                        "thread compaction turn failed",
                        code="NATIVE_THREAD_COMPACTION_FAILED",
                        details={
                            "turn_id": turn.turn_id,
                            "status": turn.status,
                            "error": turn.error,
                        },
                    )
                thread = self.transport.read_thread(str(recovery["thread_id"]))
                turns = self._thread_turns(thread)
                appended = turns[prior_count:]
        except AppServerError as exc:
            raise NativeDriverError(
                "Reviewer thread compaction did not complete",
                code="NATIVE_DISPATCH_COMPACTION_FAILED",
                status="NEEDS_USER",
                details={"source_code": exc.code, "source_details": exc.details},
            ) from exc
        if len(appended) != 1:
            raise NativeDriverError(
                "Reviewer thread compaction result is ambiguous",
                code="NATIVE_DISPATCH_COMPACTION_RESULT_AMBIGUOUS",
                status="NEEDS_USER",
                details={"appended_turn_count": len(appended)},
            )
        compaction_turn = appended[0]
        items = [
            item
            for item in compaction_turn.get("items", [])
            if isinstance(item, dict) and item.get("type") == "contextCompaction"
        ]
        forbidden = [
            item.get("type")
            for item in compaction_turn.get("items", [])
            if isinstance(item, dict)
            and item.get("type")
            not in {"contextCompaction", "reasoning"}
        ]
        if (
            compaction_turn.get("status") != "completed"
            or len(items) != 1
            or forbidden
        ):
            raise NativeDriverError(
                "Reviewer thread compaction turn is not a pure completed compaction",
                code="NATIVE_DISPATCH_COMPACTION_RESULT_INVALID",
                status="NEEDS_USER",
                details={
                    "turn_id": compaction_turn.get("id"),
                    "status": compaction_turn.get("status"),
                    "forbidden_item_types": forbidden,
                },
            )
        if transport_result is not None and (
            transport_result.turn_id != compaction_turn.get("id")
            or transport_result.item_id != items[0].get("id")
        ):
            raise NativeDriverError(
                "Reviewer compaction read-back identity changed",
                code="NATIVE_DISPATCH_COMPACTION_RESULT_DRIFT",
                status="NEEDS_USER",
            )
        completed = self.core.call(
            "complete-dispatch-compaction",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(pending["action_id"]),
            "--compaction-turn-id",
            str(compaction_turn["id"]),
            "--context-item-id",
            str(items[0]["id"]),
            "--compaction-turn-digest",
            digest(compaction_turn),
            "--compaction-duration-ms",
            str(
                compaction_turn.get("durationMs")
                if isinstance(compaction_turn.get("durationMs"), int)
                else 0
            ),
            "--observed-turn-count",
            str(len(turns)),
            "--driver-runtime-kind",
            "native",
        )
        self._emit_event(
            {
                "event": "native_driver_thread_compaction_completed",
                "run_id": self.run_id,
                "action_id": pending.get("action_id"),
                "thread_id": recovery["thread_id"],
                "compaction_turn_id": compaction_turn["id"],
            }
        )
        return completed

    def _emit_event(self, value: dict[str, Any]) -> None:
        if self._event_sink is not None:
            self._event_sink(copy.deepcopy(value))

    def _wait_for_retry(self, pending: dict[str, Any]) -> None:
        value = pending.get("retry_not_before")
        if not isinstance(value, str):
            return
        try:
            deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise NativeDriverError(
                "dispatch retry deadline is invalid",
                code="NATIVE_DISPATCH_RETRY_DEADLINE_INVALID",
                status="NEEDS_USER",
            ) from exc
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        while True:
            remaining = max(0.0, (deadline - self._now_fn()).total_seconds())
            self._emit_event(
                {
                    "event": "native_driver_retry_waiting",
                    "run_id": self.run_id,
                    "action_id": pending.get("action_id"),
                    "action": pending.get("action"),
                    "attempt": pending.get("attempt"),
                    "failure_code": pending.get("failure_code"),
                    "retry_not_before": value,
                    "remaining_seconds": int(remaining + 0.999),
                }
            )
            if remaining <= 0:
                return
            self._sleep_fn(min(10.0, remaining))

    def _load_schema(self, name: str) -> dict[str, Any]:
        return json.loads((self.project_root / "schema" / name).read_text())

    @staticmethod
    def _turn_client_id(turn: dict[str, Any]) -> str | None:
        for item in turn.get("items", []):
            if item.get("type") == "userMessage" and isinstance(item.get("clientId"), str):
                return item["clientId"]
        return None

    @staticmethod
    def _dispatch_client_id(pending: dict[str, Any]) -> str:
        action_id = str(pending["action_id"])
        attempt = int(pending.get("attempt", 1))
        generation = int(pending.get("generation", 1))
        if generation == 1:
            return f"{action_id}:{attempt}"
        return f"{action_id}:g{generation}:{attempt}"

    @staticmethod
    def _turn_failure_code(turn: dict[str, Any]) -> str:
        return classify_turn_failure(turn.get("error"))

    @staticmethod
    def _turn_result_failure_code(turn: TurnResult) -> str:
        return classify_turn_failure(turn.error)

    def _retry_turn_failure(self, turn: TurnResult, action_id: str) -> bool:
        if turn.status == "completed":
            return False
        failure_code = self._turn_result_failure_code(turn)
        if not is_retryable_transport_failure(failure_code):
            context = self._context()
            deployment = context.get("deployment_transaction")
            if isinstance(deployment, dict) and deployment.get("state") == "deployed":
                self.core.call(
                    "require-deployment-restore",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--failure-code",
                    failure_code,
                    "--action-id",
                    action_id,
                    "--driver-runtime-kind",
                    "native",
                )
                return True
            return False
        action_name = (
            self.current_action.get("action")
            if isinstance(self.current_action, dict)
            and self.current_action.get("action_id") == action_id
            else None
        )
        self._schedule_dispatch_retry(
            action_id, failure_code, action_name=action_name
        )
        return True

    def _parse_action_result_or_retry(
        self,
        action: str,
        turn: TurnResult,
        action_id: str,
    ) -> dict[str, Any] | None:
        try:
            parsed = self._parse_turn(turn)
        except NativeDriverError as exc:
            if exc.code not in {
                "NATIVE_ROLE_RESULT_INVALID",
                "NATIVE_ROLE_RESULT_INVALID_JSON",
            }:
                raise
            self._schedule_dispatch_retry(
                action_id,
                exc.code,
                action_name=action,
                failure_details=exc.details,
            )
            return None
        normalized = self._normalize_action_result(action, parsed)
        try:
            return validate_agent_result(normalized)
        except ContractError as exc:
            if exc.code not in ROLE_RESULT_VALIDATION_FAILURE_CODES:
                raise
            self._schedule_dispatch_retry(
                action_id,
                exc.code,
                action_name=action,
                failure_details=exc.details,
            )
            return None

    def _complete_dispatch_or_retry(
        self,
        action_id: str,
        action: str,
        result: dict[str, Any],
    ) -> bool:
        try:
            self.core.call(
                "complete-dispatch",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--result",
                "-",
                input_value=result,
            )
        except CorePortError as exc:
            if exc.code not in ROLE_RESULT_VALIDATION_FAILURE_CODES:
                raise
            self._schedule_dispatch_retry(
                action_id,
                exc.code,
                action_name=action,
                failure_details=self._core_error_details(exc),
            )
            return False
        return True

    @staticmethod
    def _core_error_details(error: CorePortError) -> Any | None:
        details = error.payload.get("details")
        if details is not None:
            return copy.deepcopy(details)
        value = {
            key: copy.deepcopy(item)
            for key, item in error.payload.items()
            if key not in {"status", "code", "message"}
        }
        return value or None

    def _schedule_dispatch_retry(
        self,
        action_id: str,
        failure_code: str,
        *,
        action_name: str | None = None,
        failure_details: Any | None = None,
    ) -> dict[str, Any]:
        self._assert_builder_retry_safe(action_id, action_name=action_name)
        try:
            retry_args = [
                "retry-dispatch",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--failure-code",
                failure_code,
            ]
            if failure_details is not None:
                retry_args.extend(["--failure-details", "-"])
            return self.core.call(
                *retry_args,
                input_value=failure_details,
            )
        except CorePortError as error:
            if error.code != "NATIVE_DISPATCH_RETRY_EXHAUSTED":
                raise
            compacted = self._start_reviewer_thread_compaction(action_id)
            if compacted is not None:
                self._dispatch_renewal_reason = None
                return compacted
            if (
                self._dispatch_renewal_reason is None
                and action_name in {"reviewer_preflight", "reviewer_final"}
                and self._start_reviewer_replacement(action_id)
            ):
                return self._context()
            if (
                self._dispatch_renewal_reason is None
                and self._start_canonical_rehydration(
                    action_id, action_name=action_name
                )
            ):
                return self._context()
            if self._dispatch_renewal_reason is None:
                raise
            context = self._context()
            intent = context.get("dispatch_intent")
            if not (
                isinstance(intent, dict)
                and intent.get("action_id") == action_id
                and intent.get("state") == "exhausted"
            ):
                raise
            reason = self._dispatch_renewal_reason
            renewed = self.core.call(
                "renew-dispatch",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--reason",
                reason,
                "--driver-runtime-kind",
                "native",
            )
            self._dispatch_renewal_reason = None
            return renewed

    def _assert_builder_retry_safe(
        self, action_id: str, *, action_name: str | None = None
    ) -> None:
        if action_name is None and isinstance(self.current_action, dict):
            if self.current_action.get("action_id") == action_id:
                action_name = self.current_action.get("action")
        if action_name not in {
            "builder_implement",
            "builder_fix",
            "builder_recompose_fix",
        }:
            return
        context = self._context()
        intent = context.get("dispatch_intent")
        if not isinstance(intent, dict) or intent.get("action_id") != action_id:
            return
        baseline = intent.get("candidate_manifest_digest")
        if not isinstance(baseline, str):
            return
        observation = context.get("candidate_observation")
        current = (
            observation.get("manifest_digest")
            if isinstance(observation, dict)
            else None
        )
        if current == baseline:
            return
        raise NativeDriverError(
            "Builder retry is blocked after a candidate side effect",
            code="NATIVE_BUILDER_SIDE_EFFECT_RETRY_BLOCKED",
            status="FATAL",
            details={
                "baseline_manifest_digest": baseline,
                "candidate_observation": observation,
            },
        )

    def _start_reviewer_replacement(self, action_id: str) -> bool:
        """Persist replacement only for a pure, empty exhausted Reviewer tail."""

        if self._thread_compaction_available or self.transport is None:
            return False
        context = self._context()
        pending = context.get("dispatch_intent")
        existing_replacement = context.get("reviewer_replacement_intent")
        if (
            isinstance(existing_replacement, dict)
            and existing_replacement.get("action_id") == action_id
        ):
            return True
        if not (
            isinstance(pending, dict)
            and pending.get("action_id") == action_id
            and pending.get("role") == "reviewer"
            and pending.get("state") == "exhausted"
            and pending.get("failure_code") in REVIEWER_COMPACTION_FAILURE_CODES
        ):
            return False
        thread_id = pending.get("thread_id")
        if not isinstance(thread_id, str):
            return False
        try:
            thread = self.transport.read_thread(thread_id)
        except AppServerError:
            return False
        turns = self._thread_turns(thread)
        if not turns or not self._empty_exhausted_tail(pending, turns[-1]):
            return False
        candidate_head = context["facets"]["execution"].get("candidate_head")
        if not isinstance(candidate_head, str):
            return False
        observation = {
            "candidate_head": candidate_head,
            "target_start_head": context.get("target_start_head"),
            "evidence": context.get("evidence", {}),
            "publication": context.get("publication"),
            "deployment_transaction": context.get("deployment_transaction"),
        }
        observation_digest = digest(observation)
        if observation_digest != pending.get("dispatch_observation_digest"):
            return False
        try:
            self.core.call(
                "begin-reviewer-replacement",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--source-generation",
                str(pending.get("generation")),
                "--source-attempt",
                str(pending.get("attempt")),
                "--failure-code",
                str(pending.get("failure_code")),
                "--thread-id",
                thread_id,
                "--turn-id",
                str(pending.get("turn_id")),
                "--prompt-digest",
                str(pending.get("prompt_digest")),
                "--output-schema-digest",
                str(pending.get("output_schema_digest")),
                "--dispatch-observation-digest",
                observation_digest,
                "--candidate-head",
                candidate_head,
                "--thread-observation-digest",
                digest(turns),
                "--driver-runtime-kind",
                "native",
            )
        except CorePortError as error:
            if error.code == "REVIEWER_REPLACEMENT_LIMIT_REACHED":
                raise NativeDriverError(
                    "Reviewer replacement limit reached",
                    code="NATIVE_REVIEWER_REPLACEMENT_LIMIT_REACHED",
                    status="NEEDS_USER",
                    details=error.payload,
                ) from error
            if error.status == "NEEDS_USER":
                raise NativeDriverError(
                    "Reviewer replacement source was no longer eligible",
                    code=error.code,
                    status="NEEDS_USER",
                    details=error.payload,
                ) from error
            raise
        return True

    def _assert_reviewer_replacement_source(
        self, replacement: dict[str, Any]
    ) -> dict[str, Any]:
        if self.transport is None:
            raise NativeDriverError(
                "Reviewer replacement source cannot be inspected without transport",
                code="NATIVE_REVIEWER_REPLACEMENT_TRANSPORT_REQUIRED",
                status="NEEDS_USER",
            )
        thread_id = replacement.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise NativeDriverError(
                "Reviewer replacement source thread is missing",
                code="NATIVE_REVIEWER_REPLACEMENT_SOURCE_INVALID",
                status="NEEDS_USER",
            )
        try:
            thread = self.transport.read_thread(thread_id)
        except AppServerError as exc:
            raise NativeDriverError(
                "Reviewer replacement source thread could not be read",
                code="NATIVE_REVIEWER_REPLACEMENT_SOURCE_UNREADABLE",
                status="NEEDS_USER",
                details={"source_code": exc.code, "source_details": exc.details},
            ) from exc
        turns = self._thread_turns(thread)
        if digest(turns) != replacement.get("thread_observation_digest"):
            raise NativeDriverError(
                "Reviewer replacement source thread changed",
                code="NATIVE_REVIEWER_REPLACEMENT_SOURCE_DRIFT",
                status="NEEDS_USER",
            )
        pending = {
            "turn_id": replacement.get("turn_id"),
            "failure_code": replacement.get("failure_code"),
        }
        if not turns or not self._empty_exhausted_tail(pending, turns[-1]):
            raise NativeDriverError(
                "Reviewer replacement source is no longer a pure empty tail",
                code="NATIVE_REVIEWER_REPLACEMENT_SOURCE_INELIGIBLE",
                status="NEEDS_USER",
            )
        return thread

    def retry_transport_failure(self, error: AppServerError) -> dict[str, Any] | None:
        action = self.current_action
        if not isinstance(action, dict):
            action = self.core.call(
                "driver-next", "--repo", str(self.repo), "--run", self.run_id
            )
            self.current_action = action
        action_id = action.get("action_id")
        action_name = action.get("action")
        capability = AGENT_ACTION_CAPABILITIES.get(str(action_name))
        if not isinstance(action_id, str) or capability is None:
            return None
        context = self._context()
        intent = context.get("dispatch_intent")
        agent = context["facets"]["execution"]["agents"].get(capability.role)
        if not (
            isinstance(intent, dict)
            and intent.get("action_id") == action_id
            and intent.get("action") == action_name
            and intent.get("role") == capability.role
            and intent.get("state") in {"prepared", "in_flight"}
            and isinstance(agent, dict)
            and intent.get("thread_id") == agent.get("thread_id")
        ):
            return None
        pre_activation = (
            intent.get("state") == "prepared"
            and intent.get("turn_id") is None
            and intent.get("activation_state") in {"pending", "unknown"}
        )
        if (
            pre_activation
            and intent.get("activation_state") == "unknown"
        ):
            raise NativeDriverError(
                "dispatch activation outcome is unknown",
                code="NATIVE_DISPATCH_ACTIVATION_UNKNOWN",
                status="NEEDS_USER",
                details={
                    "action_id": action_id,
                    "failure_code": intent.get("activation_failure_code"),
                },
            )
        if pre_activation and is_missing_rollout_failure(error):
            if (
                action_name == "tester_author"
                and self._renew_tester_bootstrap_after_missing_rollout(
                    action, context, error
                )
            ):
                return self._context()
            if action_name == "tester_author" and capability.role == "tester":
                details = error.details
                args = [
                    "record-tester-bootstrap-failure",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--action-id",
                    action_id,
                    "--failure-code",
                    error.code,
                    "--failure-message",
                    str(error),
                    "--driver-runtime-kind",
                    "native",
                ]
                if details is not None:
                    args.extend(["--failure-details", "-"])
                return self.core.call(*args, input_value=details)
        failure_code = classify_app_server_failure(error)
        if failure_code is None or not is_retryable_transport_failure(failure_code):
            if pre_activation and intent.get("activation_state") == "pending":
                details = error.details
                args = [
                    "record-dispatch-activation",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--action-id",
                    action_id,
                    "--state",
                    "unknown",
                    "--failure-code",
                    error.code,
                    "--driver-runtime-kind",
                    "native",
                ]
                if details is not None:
                    args.extend(["--failure-details", "-"])
                self.core.call(*args, input_value=details)
                raise NativeDriverError(
                    "Native dispatch activation outcome is unknown",
                    code="NATIVE_DISPATCH_ACTIVATION_UNKNOWN",
                    status="NEEDS_USER",
                    details={
                        "action_id": action_id,
                        "failure_code": error.code,
                        "failure_details": details,
                    },
                )
            return None
        try:
            payload = self._schedule_dispatch_retry(
                action_id, failure_code, action_name=str(action_name)
            )
        except CorePortError as retry_error:
            if retry_error.status not in {"FAIL", "NEEDS_USER"}:
                raise
            payload = dict(retry_error.payload)
            payload.setdefault("run_id", self.run_id)
        payload["transport_retry"] = {
            "source_code": error.code,
            "failure_code": failure_code,
        }
        return payload

    @staticmethod
    def _turn_agent_text(turn: dict[str, Any]) -> str | None:
        text = None
        for item in turn.get("items", []):
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                text = item["text"]
        return text

    @staticmethod
    def _parse_turn(turn: TurnResult) -> dict[str, Any]:
        if turn.status != "completed":
            raise NativeDriverError(
                "Codex role turn did not complete",
                code="NATIVE_ROLE_TURN_FAILED",
                status="NEEDS_USER" if turn.status == "interrupted" else "FATAL",
                details=turn.error,
            )
        try:
            value = json.loads(turn.text)
        except json.JSONDecodeError as exc:
            raise NativeDriverError(
                "Codex role returned invalid structured output",
                code="NATIVE_ROLE_RESULT_INVALID_JSON",
                details={"path": "$", "error": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise NativeDriverError(
                "Codex role returned a non-object",
                code="NATIVE_ROLE_RESULT_INVALID",
                details={"path": "$", "actual_type": type(value).__name__},
            )
        normalized = copy.deepcopy(value)
        for field in ("evidence_report", "proof_spec", "problem_report"):
            nested = normalized.get(field)
            if nested is None:
                continue
            if not isinstance(nested, str):
                raise NativeDriverError(
                    f"Codex role returned non-string {field} on the Native wire",
                    code="NATIVE_ROLE_RESULT_INVALID",
                    details={
                        "path": field,
                        "actual_type": type(nested).__name__,
                    },
                )
            try:
                normalized[field] = json.loads(nested)
            except json.JSONDecodeError as exc:
                raise NativeDriverError(
                    f"Codex role returned invalid {field} JSON",
                    code="NATIVE_ROLE_RESULT_INVALID_JSON",
                    details={"path": field, "error": str(exc)},
                ) from exc
        return normalized
