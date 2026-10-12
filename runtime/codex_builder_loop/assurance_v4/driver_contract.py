from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal


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
