from __future__ import annotations

import copy
import json
import hashlib
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..assurance_v4.role_input import build_role_input, project_role_input, project_retry_feedback, RoleInputError

from ..assurance_v4.driver_contract import (
    AGENT_ACTION_CAPABILITIES,
    ROLE_RESULT_VALIDATION_FAILURE_CODES,
    WORKSPACE_ROUTING_FAILURE_CODES,
    REVIEWER_COMPACTION_FAILURE_CODES,
    reviewer_compaction_eligible,
    dispatch_exhaustion_view,
    AgentActionCapability,
)
from ..assurance_v4.core import canonical_context_projection, _resolve_proof_group, AssuranceError
from ..assurance_v4.models import ContractError, digest, proof_semantic_digest, validate_agent_result, validate_action_result
from .app_server import (
    LONG_TESTER_ACTIONS,
    AppServerError,
    AppServerTransport,
    _redact_value,
    TurnResult,
    TurnTimeoutProfile,
    compaction_timeout_profile,
    default_timeout_profile,
    timeout_profile_for_action,
)
from .core_port import CorePort, CorePortError
from ..assurance_v4.result_application import ResultSemantics, ResultApplicationError as NativeDriverError
from .transport_failures import (
    classify_app_server_failure,
    classify_turn_failure,
    is_missing_rollout_failure,
    is_retryable_transport_failure,
)


PROMPT_EVIDENCE_ACTIONS = frozenset(
    {
        "tester_author",
        "tester_fix",
        "tester_observe",
        "tester_blackbox",
        "reviewer_preflight",
        "reviewer_final",
    }
)
PROMPT_BLACKBOX_ACTIONS = frozenset(
    {"tester_blackbox", "reviewer_preflight", "reviewer_final"}
)


from .role_transport import RoleTransport


class NativeCoordinator(RoleTransport):
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
            if status == "HELD":
                return {"status": "HELD", "run_id": self.run_id, "decision": action}
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
                completed = action.get("dispatch")
                if self.transport is None and not (
                    isinstance(completed, dict) and completed.get("state") == "completed"
                    and completed.get("result_path")
                ):
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
            "public_prerequisite_recovery": copy.deepcopy(
                action.get("public_prerequisite_recovery")
            ),
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


    def _prepare_context(self, action, role, context):
        return self._start_context_rotation(action, role, context)
