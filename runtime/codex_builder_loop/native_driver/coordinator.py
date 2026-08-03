from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

from ..assurance_v4.models import digest
from .app_server import AppServerError, AppServerTransport, TurnResult
from .core_port import CorePort, CorePortError


class NativeDriverError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: str = "FATAL", details: Any = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


AGENT_ACTION_ROLES = {
    "builder_implement": "builder",
    "builder_fix": "builder",
    "tester_author": "tester",
    "tester_fix": "tester",
    "tester_proof": "tester",
    "tester_blackbox": "tester",
    "reviewer_final": "reviewer",
}

RETRYABLE_TURN_FAILURES = {
    "serverOverloaded",
    "responseStreamConnectionFailed",
    "responseStreamDisconnected",
    "responseTooManyFailedAttempts",
    "httpConnectionFailed",
}


class NativeCoordinator:
    def __init__(
        self,
        *,
        repo: Path,
        run_id: str,
        core: CorePort,
        transport: AppServerTransport,
        project_root: Path | None = None,
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
        self.proof_schema = self._load_schema("codex-test-proof.schema.json")
        self._active_threads: set[str] = set()

    def run(self) -> dict[str, Any]:
        while True:
            action = self.core.call(
                "driver-next", "--repo", str(self.repo), "--run", self.run_id
            )
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
                    "status": "FINALIZED" if phase == "finalized" else "STOPPED",
                    "run_id": self.run_id,
                    "decision": action,
                }
            name = str(action.get("action"))
            if name in AGENT_ACTION_ROLES:
                self._run_agent_action(action, AGENT_ACTION_ROLES[name])
                continue
            if name == "checkpoint_builder":
                self._simple("checkpoint-builder", action)
            elif name == "publish_prerequisites":
                self._simple("publish-prerequisites", action)
            elif name == "verify_machine":
                self._simple("verify-machine", action)
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
            elif name == "rematerialize_target":
                self._simple("rematerialize-target", action)
            elif name == "recover_finalize":
                self._simple("recover-finalize", action)
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

    def _run_agent_action(self, action: dict[str, Any], role: str) -> None:
        context = self._context()
        agent = context["facets"]["execution"]["agents"].get(role)
        if not isinstance(agent, dict):
            self._prepare_role(action, role, context)
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

    def _prepare_role(self, action: dict[str, Any], role: str, context: dict[str, Any]) -> None:
        instructions, sandbox = self._role_config(role)
        cwd = context["candidate_worktree"] if role == "builder" else context["repo_root"]
        thread_id = self.transport.start_thread(
            cwd=cwd,
            developer_instructions=instructions,
            sandbox="danger-full-access",
        )
        self._active_threads.add(thread_id)
        command = f"prepare-{role}"
        self.core.call(
            command,
            "--repo",
            str(self.repo),
            "--run",
            self.run_id,
            "--agent-id",
            f"codex-app-server:{thread_id}",
            "--thread-id",
            thread_id,
            "--action-id",
            str(action["action_id"]),
            "--driver-runtime-kind",
            "native",
        )

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
        client_id = f"{pending['action_id']}:{attempt}"
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
            if failure_code in RETRYABLE_TURN_FAILURES:
                self.core.call(
                    "retry-dispatch",
                    "--repo",
                    str(self.repo),
                    "--run",
                    self.run_id,
                    "--action-id",
                    str(pending["action_id"]),
                    "--failure-code",
                    failure_code,
                )
                return None
        if (
            len(matches) == 1
            and matches[0].get("status") == "completed"
            and self._turn_agent_text(matches[0]) is None
        ):
            self.core.call(
                "retry-dispatch",
                "--repo",
                str(self.repo),
                "--run",
                self.run_id,
                "--action-id",
                str(pending["action_id"]),
                "--failure-code",
                "missingAgentResult",
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
            integrated_head = integrated["facets"]["execution"].get("candidate_head")
            if not isinstance(integrated_head, str):
                raise NativeDriverError(
                    "Tester integration produced no candidate HEAD",
                    code="NATIVE_TESTER_INTEGRATION_HEAD_MISSING",
                )
            self._record_evidence(
                "tester",
                self._bind_evidence_candidate(evidence, integrated_head),
                action_id,
            )
        elif action["action"] == "tester_proof":
            spec = result.get("proof_spec")
            if not isinstance(spec, dict):
                raise NativeDriverError("Tester returned no proof spec", code="NATIVE_PROOF_SPEC_MISSING")
            spec = self._bind_proof_test_ids(spec, context)
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
                input_value=spec,
            )
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
                "tester_author": "author",
                "tester_fix": "author",
                "tester_proof": "proof",
                "tester_blackbox": "blackbox",
                "reviewer_final": "final",
            }.get(str(action["action"]), "implement"),
            "role": role,
            "contract": context["facets"],
            "target_start_head": context["target_start_head"],
            "candidate_worktree": context["candidate_worktree"],
            "publication": context.get("publication"),
            "evidence": context.get("evidence"),
            "problems": context.get("problems"),
            "problem_report_schema": self.problem_schema,
            "result_field_contract": self._result_field_contract(str(action["action"])),
        }
        if role in {"tester", "reviewer"}:
            payload["evidence_report_schema"] = self.evidence_schema
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
            payload["review_input_contract"] = self._review_input_contract(context)
        if action.get("action") == "tester_proof":
            payload["proof_spec_schema"] = self.proof_schema
            payload["proof_test_id_hints"] = self._proof_test_id_hints(context)
            payload["proof_execution_rule"] = (
                "Choose argv that executes exactly the declared canonical test_ids. Do not reuse "
                "unittest discover -s with ids that include the omitted start-directory prefix."
            )
        return "CBL_ACTION_ID:" + str(action["action_id"]) + "\n" + json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2
        )

    def _review_input_contract(self, context: dict[str, Any]) -> dict[str, Any]:
        facets = context["facets"]
        mission = facets["mission"]
        execution = facets["execution"]
        documentation_only = mission.get("delivery_kind") == "documentation"
        spec_head = str(context["target_start_head"])
        candidate_head = str(execution["candidate_head"])
        return {
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
            "pre_turn_gates": {
                "required": facets["assurance"].get("required", []),
                "evidence": context.get("evidence", {}),
                "publication": context.get("publication"),
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
        if action in {"builder_implement", "builder_fix"}:
            return {
                "evidence_report": "must_be_null",
                "proof_spec": "must_be_null",
                "problem_report": "object_only_when_blocked_or_target_change_required_else_null",
            }
        if action == "tester_proof":
            return {
                "evidence_report": "must_be_null",
                "proof_spec": "required_unless_problem_report_is_non_null",
                "problem_report": "object_only_when_blocked_or_target_change_required_else_null",
            }
        return {
            "evidence_report": "required_on_pass_else_null",
            "proof_spec": "must_be_null",
            "problem_report": "object_only_when_findings_blocked_or_target_change_required_else_null",
        }

    @staticmethod
    def _normalize_action_result(action: str, result: dict[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(result)
        if isinstance(value.get("problem_report"), dict):
            value["evidence_report"] = None
            value["proof_spec"] = None
            return value
        if action in {"builder_implement", "builder_fix"}:
            value["evidence_report"] = None
            value["proof_spec"] = None
        elif action == "tester_proof":
            value["evidence_report"] = None
        else:
            value["proof_spec"] = None
        return value

    def _turn_cwd(self, action: dict[str, Any], role: str, context: dict[str, Any]) -> str:
        if role == "builder" or action.get("action") in {"tester_blackbox", "reviewer_final"}:
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
    def _turn_failure_code(turn: dict[str, Any]) -> str:
        value: Any = turn.get("error", {}).get("codexErrorInfo")
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and value:
            return str(next(iter(value)))
        return "other"

    @staticmethod
    def _turn_result_failure_code(turn: TurnResult) -> str:
        value: Any = turn.error.get("codexErrorInfo") if isinstance(turn.error, dict) else None
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and value:
            return str(next(iter(value)))
        return "other"

    def _retry_turn_failure(self, turn: TurnResult, action_id: str) -> bool:
        if turn.status == "completed":
            return False
        failure_code = self._turn_result_failure_code(turn)
        if failure_code not in RETRYABLE_TURN_FAILURES:
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
        self.core.call(
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
        return True

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
