"""Role-visible inputs derived from coordinator facts, never a second ledger.

The internal canonical projection still binds transaction recovery. It is not a
role brief: author visibility is an allowlist of public and same-producer facts.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

from .models import ContractError, digest

AUTHOR_ACTIONS = frozenset({"tester_author", "tester_fix"})
PUBLICATION_FIELDS = ("required", "generation", "paths", "head", "tree", "files", "manifest_digest")
SOURCE_FIELDS = ("base_head", "head", "branch", "worktree", "files", "replaces_files", "agent")
# Explicit public views, not whole-facet copies. Lineage remains in Core.
MISSION_FIELDS = ("delivery_kind", "revision", "objective", "behaviors", "interfaces",
                  "acceptance_cases", "trust_boundaries")
AUTHORITY_FIELDS = ("target_branch", "builder_write", "tester_write", "public_prerequisites",
                    "protected_support_paths", "external_targets")
ASSURANCE_FIELDS = ("profile", "required", "machine_commands", "preflight_before_proof",
                    "reviewer_preflight", "evidence_neutral_paths", "proof_runners")
COMMAND_FIELDS = ("id", "argv", "timeout_seconds", "expected_returncodes", "run_before_full_suite")
STATEMENT_FIELDS = ("id", "description")
OBSERVATION_FIELDS = ("surface_id", "surface_description", "execution_ids", "required_dimensions", "target_id")
MANIFEST_FIELDS = ("path", "blob", "operation", "base_blob", "base_mode")
AGENT_FIELDS = ("agent_id", "thread_id")
WORK_UNIT_FIELDS = ("id", "role", "objective", "depends_on", "scope", "completion")
AUTHOR_EXECUTION_FIELDS = ("commands", "driver_enforced", "recovery_policy", "progress_policy")
AUTHOR_FIELDS = (
    "assurance_schema_version", "run_id", "action_id", "action", "phase", "role",
    "contract", "target_start_head", "publication", "problems", "tester_worktree",
    "work_unit_id", "work_unit", "problem_report_schema", "evidence_report_schema",
    "result_field_contract", "test_identity_contract", "author_result_contract",
    "role_phase_instructions",
)


class RoleInputError(ContractError):
    status = "NEEDS_USER"


def _pick(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: copy.deepcopy(value[key]) for key in fields if key in value}


def _items(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [_pick(item, fields) for item in value]


def _public_mission(value: Any) -> dict[str, Any]:
    result = _pick(value, MISSION_FIELDS)
    for field in ("behaviors", "interfaces", "trust_boundaries"):
        if field in result:
            result[field] = _items(result[field], STATEMENT_FIELDS)
    if "acceptance_cases" in result:
        cases = _items(result["acceptance_cases"], (*STATEMENT_FIELDS, "observation"))
        for case in cases:
            if "observation" in case:
                case["observation"] = _pick(case["observation"], OBSERVATION_FIELDS)
        result["acceptance_cases"] = cases
    return result


def _public_authority(value: Any) -> dict[str, Any]:
    result = _pick(value, AUTHORITY_FIELDS)
    if "external_targets" in result:
        result["external_targets"] = _items(result["external_targets"], STATEMENT_FIELDS)
    return result


def _public_assurance(value: Any) -> dict[str, Any]:
    result = _pick(value, ASSURANCE_FIELDS)
    for field in ("machine_commands", "proof_runners"):
        if field in result:
            fields = COMMAND_FIELDS if field == "machine_commands" else ("id", "argv", "timeout_seconds")
            result[field] = _items(result[field], fields)
    if "evidence_neutral_paths" in result:
        result["evidence_neutral_paths"] = _pick(result["evidence_neutral_paths"], ("machine", "proof"))
    return result


def _public_source(value: Any) -> dict[str, Any]:
    result = _pick(value, SOURCE_FIELDS)
    for field in ("files", "replaces_files"):
        if field in result:
            result[field] = _items(result[field], MANIFEST_FIELDS)
    if "agent" in result:
        result["agent"] = _pick(result["agent"], AGENT_FIELDS)
    return result


def _public_work_unit(value: Any) -> dict[str, Any]:
    result = _pick(value, WORK_UNIT_FIELDS)
    if "scope" in result:
        result["scope"] = _pick(result["scope"], ("paths", "behavior_ids", "command_ids"))
    if "completion" in result:
        result["completion"] = _pick(result["completion"], ("kind", "required_observations"))
    return result


def project_role_input(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project a role payload; newly added coordinator fields are not author inputs.

    Public semantic fields are retained verbatim; Mission lineage is not public author input.
    Runtime Execution and publication are projected separately. Problems enter
    author only from the currently bound Tester, never by ownership alone.
    """
    result = copy.deepcopy(dict(payload))
    if result.get("role") != "tester":
        return result
    # Candidate change inventories are only Builder/Reviewer facts, in every phase.
    result.pop("doc_reference_scan", None)
    result.pop("doc_reference_scan_state", None)
    if result.get("action") not in AUTHOR_ACTIONS:
        return result
    result = _pick(result, AUTHOR_FIELDS)
    facets = result.get("contract") or {}
    execution = facets.get("execution") or {}
    source = _public_source(execution.get("tester_source"))
    agent = _pick((execution.get("agents") or {}).get("tester") or source.get("agent"), AGENT_FIELDS)
    foreign_corrections = [problem for problem in result.get("problems") or []
                           if isinstance(problem, Mapping)
                           and problem.get("status") == "open"
                           and problem.get("owner") == "tester"
                           and problem.get("producer") != {"role": "tester", **(agent or {})}]
    if foreign_corrections:
        raise RoleInputError(
            "Tester correction requires author-safe input; foreign reports cannot be forwarded or silently discarded",
            code="TESTER_CORRECTION_INPUT_UNSAFE",
            details={"problem_keys": [p.get("key") for p in foreign_corrections],
                     "recovery": "Preserve the reports; use an authorized recovery or successor with author-safe correction input. Do not discard findings or retry the same input."},
        )
    projected_execution = _pick(execution, AUTHOR_EXECUTION_FIELDS)
    if "commands" in projected_execution:
        projected_execution["commands"] = _items(projected_execution["commands"], COMMAND_FIELDS)
    for field, fields in (
        ("recovery_policy", ("schema_version", "mode", "continuation_window")),
        ("progress_policy", ("schema_version", "mode", "attempt_limit", "rehydration_limit",
                             "max_units_per_thread", "context_source")),
    ):
        if field in projected_execution:
            projected_execution[field] = _pick(projected_execution[field], fields)
    projected_execution["tester_source"] = source or None
    if agent:
        projected_execution["agents"] = {"tester": copy.deepcopy(agent)}
    # Dirty intake grants Builder access; it is not an author read capability.
    authority = _public_authority(facets.get("authority"))
    result["contract"] = {
        "schema_version": facets.get("schema_version", 4),
        "mission": _public_mission(facets.get("mission")),
        "authority": authority,
        "assurance": _public_assurance(facets.get("assurance")),
        "execution": projected_execution,
    }
    publication = result.get("publication") or {}
    result["publication"] = (_pick(publication, PUBLICATION_FIELDS)
                             if publication.get("required") is True else {"required": False})
    if "files" in result["publication"]:
        result["publication"]["files"] = _items(result["publication"]["files"], MANIFEST_FIELDS)
    if result.get("work_unit") is not None:
        result["work_unit"] = _public_work_unit(result["work_unit"])
    result["tester_worktree"] = source.get("worktree")
    result["problems"] = [_pick(problem, ("key", "summary", "details", "owner", "status", "producer", "producer_continuity")) for problem in result.get("problems") or []
                          if isinstance(problem, Mapping)
                          and agent
                          and problem.get("producer") == {"role": "tester", **agent}
                          and problem.get("owner") == "tester"]
    result["author_result_contract"] = {
        "result": "tests_ready only after all tests required by the current work_unit are committed; evidence_report.status=pass",
        "scope": (
            "Complete the current work_unit objective, scope and completion requirements. "
            "The full Mission remains the semantic contract, not a demand to author other pending units in this turn. "
            "Without a work_unit, complete the entire frozen author task."
        ),
        "execution": (
            "Keep working in this turn until the current unit is complete or a concrete obstacle prevents further work. "
            "Remaining work, a partial commit, or not yet running later integrated proof/blackbox is not itself a blocker. "
            "Run the author minimum checks possible on the public baseline; do not invent implementation or claim later gates passed."
        ),
        "evidence_candidate_head": (
            "Use details.source_head (your actual committed Tester HEAD) for the author report's "
            "candidate_head field. This author report describes Tester source, not an observed candidate. "
            "Core binds the resulting evidence to the actual integrated candidate after source integration."
        ),
    }
    # No candidate-dependent digest is advertised as a digest of this view.
    result["role_input_digest"] = digest(result)
    return result


def build_role_input(context: Mapping[str, Any], action: Mapping[str, Any], role: str) -> dict[str, Any]:
    """Build the common role brief for Native and Skill adapters."""
    observations = copy.deepcopy(context.get("role_observations"))
    if isinstance(observations, dict):
        # Scheduling control must not change a pending role's prompt on
        # retry; Core still exposes and enforces hold at transaction boundaries.
        observations.pop("hold", None)
    payload = {
        "assurance_schema_version": 4,
        "run_id": context.get("run_id"),
        "action_id": action["action_id"],
        "action": action["action"],
        "phase": {
            "builder_recompose_fix": "recompose",
            "tester_author": "author",
            "tester_fix": "author",
            "tester_observe": "observe",
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
        "evidence_origin": context.get("evidence_origin"),
        "doc_reference_scan": context.get("doc_reference_scan"),
        "doc_reference_scan_state": context.get("doc_reference_scan_state"),
        "problems": [p for p in context.get("problems") or [] if not (role == "tester" and action["action"] in AUTHOR_ACTIONS and p.get("inherited_from"))],
        "inherited_problem_review": context.get("inherited_problem_review") if role == "reviewer" else None,
        "role_observations": observations,
    }
    if action.get("work_unit_id") is not None:
        payload["work_unit_id"] = action["work_unit_id"]
        payload["work_unit"] = copy.deepcopy(action.get("work_unit"))
    for action_name, field in (("tester_proof_diagnose", "proof_failure"),
                               ("tester_machine_diagnose", "machine_failure")):
        if action["action"] == action_name:
            payload[field] = copy.deepcopy(action.get(field))
    return project_role_input(payload)


def role_context(repo: Any, run_id: str, action_id: str) -> dict[str, Any]:
    """Read the current role input, rejecting a stale/non-role action (no mutation)."""
    from . import core, driver
    from .driver_contract import AGENT_ACTION_CAPABILITIES

    context = core.driver_context(repo, run_id)
    action = driver.next_action(repo, run_id)
    capability = AGENT_ACTION_CAPABILITIES.get(action.get("action"))
    if action.get("action_id") != action_id or capability is None:
        raise core.AssuranceError("role input requires the current role action",
                                  code="ROLE_INPUT_ACTION_MISMATCH", status="FAIL")
    return build_role_input(context, action, capability.role)


def project_retry_feedback(pending: Mapping[str, Any]) -> dict[str, Any]:
    """Author retries expose result diagnostics, not arbitrary Core error facts."""
    feedback = _pick(pending, ("action_id", "generation", "attempt", "failure_code", "failure_details"))
    if pending.get("action") in AUTHOR_ACTIONS:
        # Values here name the author's result/schema, never expected candidate
        # contents, arbitrary error bodies or nested execution diagnostics.
        details = _pick(pending.get("failure_details"), (
            "cause", "problem_key", "path", "schema_path", "validator", "actual_type",
            "stage", "result_bytes", "result_sha256",
        ))
        details["recovery"] = "correct the result against the supplied schema and resubmit with the same role identity"
        feedback["failure_details"] = details
    return feedback
