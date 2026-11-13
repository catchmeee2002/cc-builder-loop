"""Complete canonical role input shared by delivery and bounded retrieval."""
from __future__ import annotations
import copy
import json
import tomllib
from pathlib import Path
from typing import Any, Mapping
from .core import canonical_context_projection
from .models import digest
from .driver_contract import AGENT_ACTION_CAPABILITIES
from .role_input import build_role_input, project_role_input, RoleInputError
from .result_application import ResultSemantics, ResultApplicationError as NativeDriverError
PROMPT_EVIDENCE_ACTIONS = frozenset({"tester_author", "tester_fix", "tester_observe", "tester_blackbox", "reviewer_preflight", "reviewer_final"})
PROMPT_BLACKBOX_ACTIONS = frozenset({"tester_blackbox", "reviewer_preflight", "reviewer_final"})

class RoleInputBuilder:
    _proof_test_id_hints = staticmethod(ResultSemantics._proof_test_id_hints)

    def __init__(self, project_root: Path, run_id: str):
        self.project_root = project_root
        self.run_id = run_id
        for attribute, filename in (("problem_schema", "codex-problem-report.schema.json"),
                                    ("evidence_schema", "assurance-v4-evidence.schema.json"),
                                    ("blackbox_case_schema", "codex-blackbox-case.schema.json"),
                                    ("proof_schema", "codex-test-proof.schema.json")):
            setattr(self, attribute, json.loads((project_root / "schema" / filename).read_text()))

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


    @staticmethod
    def _tester_instructions(text: str, *, common: bool = False, author: bool = False) -> str:
        """Select explicit sections of the versioned role definition, not model output."""
        if not common and not author:
            return text
        try:
            if common:
                start = text.index("以独立 tester 身份工作。")
                end = text.index("v4 `phase=proof`")
                safety_start = text.index("测试替身或 monkeypatch")
                safety_end = text.index("v4 `phase=blackbox`", safety_start)
                return (text[start:end] + text[safety_start:safety_end]
                        + "\n执行当前输入 role_phase_instructions 指定的阶段；其他阶段在后续输入中提供。")
            start = text.index("phase=author：\n")
            end = text.index("phase=proof_diagnose：", start)
            return text[start:end]
        except ValueError as exc:
            raise NativeDriverError("Tester phase instruction boundaries are missing",
                                    code="NATIVE_ROLE_INSTRUCTIONS_INVALID") from exc


    def build(self, action: dict[str, Any], role: str, context: dict[str, Any]) -> str:
        try:
            payload = build_role_input({**context, "run_id": self.run_id}, action, role)
        except RoleInputError as exc:
            raise NativeDriverError(str(exc), code=exc.code, status=exc.status, details=exc.details) from exc
        payload["problem_report_schema"] = self.problem_schema
        payload["result_field_contract"] = self._result_field_contract(str(action["action"]))
        if action["action"] in {"tester_proof", "tester_proof_diagnose"} and context.get("artifact_root"):
            payload["proof_patch_directory"] = str(Path(context["artifact_root"]) / ("patches-" + str(action["action_id"])))
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
        if action.get("public_prerequisite_recovery") is not None:
            payload["public_prerequisite_recovery"] = copy.deepcopy(action["public_prerequisite_recovery"])
            payload["prerequisite_completion_rule"] = (
                "Complete only the listed missing public prerequisite paths within this Builder work unit. "
                "Keep the frozen interfaces, authority and acceptance unchanged. Commit the candidate; "
                "do not create Tester source or publication yourself. If specifications or permissions "
                "are insufficient, report the precise problem instead of guessing."
            )
        action_name = str(action["action"])
        if action_name in PROMPT_EVIDENCE_ACTIONS:
            payload["evidence_report_schema"] = self.evidence_schema
        if action_name in PROMPT_BLACKBOX_ACTIONS:
            payload["blackbox_case_schema"] = self.blackbox_case_schema
        if role == "tester":
            definition = tomllib.loads((self.project_root / "agents" / "tester.toml").read_text())
            if payload.get("phase") == "correct":
                payload["role_phase_instructions"] = payload["correction_rule"]
            else:
                payload["role_phase_instructions"] = self._tester_instructions(
                    str(definition["developer_instructions"]),
                    author=action_name in {"tester_author", "tester_fix"},
                )
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
        if action_name in {"tester_proof", "tester_proof_diagnose"}:
            payload["proof_spec_schema"] = self.proof_schema
            payload["proof_test_id_hints"] = self._proof_test_id_hints(context)
            if action_name == "tester_proof":
                payload["proof_execution_rule"] = (
                    "When frozen assurance.proof_runners are available, prefer schema_version=2 and "
                    "runner_id instead of argv; Core appends canonical test_ids to the frozen command. "
                    "Otherwise choose argv that executes exactly the declared canonical test_ids. Do not reuse "
                    "unittest discover -s with ids that include the omitted start-directory prefix."
                )
        if action_name == "tester_proof_diagnose":
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
        if action_name == "tester_machine_diagnose":
            payload["machine_failure"] = copy.deepcopy(action.get("machine_failure"))
            payload["machine_diagnosis_rule"] = (
                "Do not edit files or rerun the command as a replacement for Core. Classify each "
                "independent failure from the frozen contract, persisted command result, Tester "
                "source and bound artifacts. When command output_ref is present, read output_ref.path "
                "in bounded sections. The configured Builder-loop CLI provides assurance --experimental-v4 "
                "read-machine-output --repo <repo> --run <run> "
                "--digest <output_ref.digest> before attributing failures missing from the tail. "
                "Route implementation defects to builder, Tester-owned "
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
        payload = project_role_input(payload)
        return payload


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
                "current_validity": self._prompt_source_ref(context.get("evidence_gates", {}), "/evidence_gates"),
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
        if AGENT_ACTION_CAPABILITIES[action].result_payload == "proof_spec":
            return {
                "evidence_report": "must_be_null",
                "proof_spec": "required_object_unless_problem_report",
                "problem_report": "object_only_when_blocked_or_target_change_required_else_null",
            }
        if action == "tester_proof_diagnose":
            return {
                "evidence_report": "must_be_null",
                "proof_spec": "changed_replacement_object_for_spec_only_correction_else_null",
                "problem_report": "required_non_empty_for_source_or_contract_problem_else_null",
            }
        if action in {"reviewer_preflight", "reviewer_final"}:
            return {
                "evidence_report": "required_on_pass_or_observation_disposition_else_null",
                "proof_spec": "must_be_null",
                "problem_report": "object_only_when_findings_blocked_or_target_change_required_else_null",
            }
        if action in {"tester_author", "tester_fix"}:
            return {
                "evidence_report": "required_on_tests_ready_or_tester_problem_disposition_else_null",
                "proof_spec": "must_be_null",
                "problem_report": "required_non_empty_when_blocked_else_null",
            }
        if action == "tester_machine_diagnose":
            return {
                "evidence_report": "must_be_null",
                "proof_spec": "must_be_null",
                "problem_report": "required_non_empty_with_supported_problem_owner",
            }
        return {
            "evidence_report": "required_on_pass_or_review_problem_disposition_else_null",
            "proof_spec": "must_be_null",
            "problem_report": "object_only_when_findings_blocked_or_target_change_required_else_null",
        }
