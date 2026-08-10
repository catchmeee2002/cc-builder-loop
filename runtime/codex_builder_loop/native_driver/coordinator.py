from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

from ..assurance_v4.driver_contract import (
    AGENT_ACTION_CAPABILITIES,
    AgentActionCapability,
)
from ..assurance_v4.models import digest
from .app_server import AppServerError, AppServerTransport, TurnResult
from .core_port import CorePort, CorePortError
from .transport_failures import (
    classify_app_server_failure,
    classify_turn_failure,
    is_retryable_transport_failure,
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
        transport: AppServerTransport,
        project_root: Path | None = None,
        dispatch_renewal_reason: str | None = None,
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
        self._dispatch_renewal_reason = (
            dispatch_renewal_reason.strip()
            if isinstance(dispatch_renewal_reason, str) and dispatch_renewal_reason.strip()
            else None
        )

    def run(self) -> dict[str, Any]:
        while True:
            self.current_action = None
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
                self._run_agent_action(action, capability)
                continue
            if name == "checkpoint_builder":
                self._simple("checkpoint-builder", action)
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
                self.core.call(
                    "complete-driver-failure",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--driver-runtime-kind",
                    "native",
                )
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

    def _simple(self, command: str, action: dict[str, Any]) -> None:
        self.core.call(
            command,
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(action["action_id"]),
            "--driver-runtime-kind",
            "native",
        )

    def _run_agent_action(
        self,
        action: dict[str, Any],
        capability: AgentActionCapability,
    ) -> None:
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
        pending = context.get("dispatch_intent")
        if isinstance(pending, dict):
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
            result = self._recover_dispatch(pending, role, context)
            if result is None:
                return
            self._apply_agent_result(action, role, result, context)
            return
        prompt = self._prompt(action, role, context)
        self._activate_role_thread(action, role, context, str(agent["thread_id"]))
        self.core.call(
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
        if self._retry_turn_failure(turn, str(action["action_id"])):
            return
        result = self._normalize_action_result(
            str(action["action"]), self._parse_turn(turn)
        )
        self.core.call(
            "complete-dispatch",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(action["action_id"]),
            "--result",
            "-",
            input_value=result,
        )
        self._apply_agent_result(action, role, result, self._context())

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
        self, pending: dict[str, Any], role: str, context: dict[str, Any]
    ) -> dict[str, Any] | None:
        if pending.get("state") == "completed":
            path = Path(str(pending["result_path"]))
            result = json.loads(path.read_text())
            if digest(result) != pending.get("result_digest"):
                raise NativeDriverError(
                    "persisted dispatch result digest changed",
                    code="NATIVE_DISPATCH_RESULT_DRIFT",
                    status="NEEDS_USER",
                )
            return result
        thread_id = str(pending["thread_id"])
        instructions, sandbox = self._role_config(role)
        self.transport.resume_thread(
            thread_id=thread_id,
            cwd=self._turn_cwd(pending, role, context),
            developer_instructions=instructions,
            sandbox="danger-full-access",
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
            prompt = self._prompt(pending, role, context)
            if digest(prompt) != pending.get("prompt_digest"):
                raise NativeDriverError(
                    "prepared dispatch prompt cannot be reconstructed",
                    code="NATIVE_DISPATCH_PROMPT_DRIFT",
                    status="NEEDS_USER",
                )
            turn = self.transport.run_turn(
                thread_id=thread_id,
                prompt=prompt,
                output_schema=self.output_schema,
                action_id=client_id,
                cwd=self._turn_cwd(pending, role, context),
                sandbox_policy=self._sandbox_policy(pending, role, context),
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
            if self._retry_turn_failure(turn, str(pending["action_id"])):
                return None
            result = self._normalize_action_result(
                str(pending["action"]), self._parse_turn(turn)
            )
            self.core.call(
                "complete-dispatch",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                str(pending["action_id"]),
                "--result",
                "-",
                input_value=result,
            )
            return result
        if len(matches) == 1 and matches[0].get("status") == "failed":
            failure_code = self._turn_failure_code(matches[0])
            if is_retryable_transport_failure(failure_code):
                self._schedule_dispatch_retry(str(pending["action_id"]), failure_code)
                return None
        if (
            len(matches) == 1
            and matches[0].get("status") == "completed"
            and self._turn_agent_text(matches[0]) is None
        ):
            self._schedule_dispatch_retry(
                str(pending["action_id"]), "missingAgentResult"
            )
            return None
        if len(matches) == 1 and matches[0].get("status") in {"inProgress", "in_progress"}:
            turn = self.transport.wait_turn(
                thread_id=thread_id, turn_id=str(matches[0]["id"])
            )
            if self._retry_turn_failure(turn, str(pending["action_id"])):
                return None
            result = self._normalize_action_result(
                str(pending["action"]), self._parse_turn(turn)
            )
            self.core.call(
                "complete-dispatch",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                str(pending["action_id"]),
                "--result",
                "-",
                input_value=result,
            )
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
        result = self._normalize_action_result(
            str(pending["action"]),
            self._parse_turn(
                TurnResult(
                    turn_id=str(turn_value["id"]),
                    status=str(turn_value["status"]),
                    text=text,
                    error=turn_value.get("error"),
                )
            ),
        )
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
        self.core.call(
            "complete-dispatch",
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--action-id",
            str(pending["action_id"]),
            "--result",
            "-",
            input_value=result,
        )
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
        return "CBL_ACTION_ID:" + str(action["action_id"]) + "\n" + json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2
        )

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
            "accepted_plan": {
                "source": "canonical_assurance_v4_contract",
                "value": facets,
            },
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
            "doc_reference_scan": context.get("doc_reference_scan"),
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
                "evidence": context.get("evidence", {}),
                "doc_reference_scan": context.get("doc_reference_scan"),
                "doc_reference_scan_state": context.get("doc_reference_scan_state"),
                "publication": context.get("publication"),
                "requirement_rule": (
                    "Only names in required are mandatory. When tester is absent, Tester "
                    "author, source, integration, and tester evidence are not gates; blackbox "
                    "still requires the ledger-bound Tester identity."
                ),
            },
            "mapping_note": (
                "Assurance v4 uses the canonical contract above as the accepted plan. Its mission "
                "behaviors, acceptance cases, and trust boundaries are the plan checklist; for an "
                "L1 documentation delivery they are also the documentation specification. Legacy "
                "sidecar plan-checklist or documentation-spec files are not separate v4 gates."
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
        self._schedule_dispatch_retry(action_id, failure_code)
        return True

    def _schedule_dispatch_retry(
        self, action_id: str, failure_code: str
    ) -> dict[str, Any]:
        try:
            return self.core.call(
                "retry-dispatch",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                action_id,
                "--failure-code",
                failure_code,
            )
        except CorePortError as error:
            if (
                error.code != "NATIVE_DISPATCH_RETRY_EXHAUSTED"
                or self._dispatch_renewal_reason is None
            ):
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

    def retry_transport_failure(self, error: AppServerError) -> dict[str, Any] | None:
        failure_code = classify_app_server_failure(error)
        if failure_code is None or not is_retryable_transport_failure(failure_code):
            return None
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
        try:
            payload = self._schedule_dispatch_retry(action_id, failure_code)
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
            ) from exc
        if not isinstance(value, dict):
            raise NativeDriverError("Codex role returned a non-object", code="NATIVE_ROLE_RESULT_INVALID")
        normalized = copy.deepcopy(value)
        for field in ("evidence_report", "proof_spec", "problem_report"):
            nested = normalized.get(field)
            if nested is None:
                continue
            if not isinstance(nested, str):
                raise NativeDriverError(
                    f"Codex role returned non-string {field} on the Native wire",
                    code="NATIVE_ROLE_RESULT_INVALID",
                )
            try:
                normalized[field] = json.loads(nested)
            except json.JSONDecodeError as exc:
                raise NativeDriverError(
                    f"Codex role returned invalid {field} JSON",
                    code="NATIVE_ROLE_RESULT_INVALID_JSON",
                ) from exc
        return normalized
