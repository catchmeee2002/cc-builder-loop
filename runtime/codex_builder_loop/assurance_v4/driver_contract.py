from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping


# Failure classification selects an observation policy, never permission to retry.
# Keep the legacy timeout code readable; both variants require the same no-effect proof.
WORKSPACE_ROUTING_FAILURE_CODES = frozenset({
    "workspaceRoutingTimeout", "workspaceRoutingFailed",
})

REVIEWER_COMPACTION_FAILURE_CODES = frozenset({
    "responseStreamDisconnected", "missingAgentResult",
})


def reviewer_compaction_eligible(dispatch: Mapping[str, Any]) -> bool:
    """Pure admission check; tail, identity and side-effect proof remain in Core."""
    return bool(
        dispatch.get("state") == "exhausted"
        and int(dispatch.get("attempt", 1)) >= 3
        and dispatch.get("role") == "reviewer"
        and dispatch.get("action") in {"reviewer_preflight", "reviewer_final"}
        and dispatch.get("failure_code") in REVIEWER_COMPACTION_FAILURE_CODES
    )


def dispatch_exhaustion_view(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Read-only public facts, not permission to renew or replace a dispatch.

    Null unless the persisted dispatch is exhausted. All identity/counter fields
    come from that dispatch, including on historical terminal runs.
    """
    dispatch = context.get("dispatch_intent")
    if not isinstance(dispatch, Mapping) or dispatch.get("state") != "exhausted":
        return None
    return {"code": "NATIVE_DISPATCH_RETRY_EXHAUSTED", **{
        key: dispatch.get(key) for key in (
            "action_id", "action", "role", "thread_id", "attempt", "generation", "failure_code"
        )
    }}


def dispatch_exhaustion_blocked(context: Mapping[str, Any]) -> bool:
    """Exhaustion pauses an active run unless a bound recovery already exists.

    This projection neither probes transport nor decides which recovery is safe.
    Existing recovery/hold/terminal decisions retain their own precedence.
    """
    view = dispatch_exhaustion_view(context)
    if view is None or context.get("phase") != "active":
        return False
    dispatch = context["dispatch_intent"]
    compaction = dispatch.get("compaction_recovery")
    if isinstance(compaction, Mapping) and compaction.get("state") == "prepared":
        return False
    for key in ("dispatch_rehydration_intent", "reviewer_replacement_intent"):
        intent = context.get(key)
        if isinstance(intent, Mapping) and intent.get("action_id") == view["action_id"]:
            return False
    return True


AgentRole = Literal["builder", "tester", "reviewer"]
PreparationKind = Literal[
    "role_identity",
    "tester_identity",
    "tester_source",
    "existing_tester_source",
]


@dataclass(frozen=True)
class AgentActionCapability:
    role: AgentRole
    preparation: PreparationKind
    result_payload: Literal["none", "evidence_report", "proof_spec", "problem_report", "proof_diagnosis"] = "none"


AGENT_ACTION_CAPABILITIES = MappingProxyType(
    {
        "builder_implement": AgentActionCapability("builder", "role_identity"),
        "builder_fix": AgentActionCapability("builder", "role_identity"),
        "builder_recompose_fix": AgentActionCapability("builder", "role_identity"),
        "tester_author": AgentActionCapability("tester", "tester_source", "evidence_report"),
        "tester_fix": AgentActionCapability("tester", "tester_source", "evidence_report"),
        "tester_observe": AgentActionCapability("tester", "existing_tester_source", "evidence_report"),
        "tester_recompose_fix": AgentActionCapability("tester", "tester_source"),
        "tester_proof": AgentActionCapability("tester", "existing_tester_source", "proof_spec"),
        "tester_proof_diagnose": AgentActionCapability(
            "tester", "existing_tester_source", "proof_diagnosis"
        ),
        "tester_machine_diagnose": AgentActionCapability(
            "tester", "tester_identity", "problem_report"
        ),
        "tester_blackbox": AgentActionCapability("tester", "tester_identity", "evidence_report"),
        "reviewer_preflight": AgentActionCapability("reviewer", "role_identity", "evidence_report"),
        "reviewer_final": AgentActionCapability("reviewer", "role_identity", "evidence_report"),
    }
)


def actions_for_preparation(
    role: AgentRole,
    preparation: PreparationKind,
) -> frozenset[str]:
    return frozenset(
        action
        for action, capability in AGENT_ACTION_CAPABILITIES.items()
        if capability.role == role and capability.preparation == preparation
    )
