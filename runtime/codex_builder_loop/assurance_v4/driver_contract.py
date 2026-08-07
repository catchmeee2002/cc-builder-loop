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


AGENT_ACTION_CAPABILITIES = MappingProxyType(
    {
        "builder_implement": AgentActionCapability("builder", "role_identity"),
        "builder_fix": AgentActionCapability("builder", "role_identity"),
        "builder_recompose_fix": AgentActionCapability("builder", "role_identity"),
        "tester_author": AgentActionCapability("tester", "tester_source"),
        "tester_fix": AgentActionCapability("tester", "tester_source"),
        "tester_recompose_fix": AgentActionCapability("tester", "tester_source"),
        "tester_proof": AgentActionCapability("tester", "existing_tester_source"),
        "tester_proof_diagnose": AgentActionCapability(
            "tester", "existing_tester_source"
        ),
        "tester_machine_diagnose": AgentActionCapability(
            "tester", "tester_identity"
        ),
        "tester_blackbox": AgentActionCapability("tester", "tester_identity"),
        "reviewer_preflight": AgentActionCapability("reviewer", "role_identity"),
        "reviewer_final": AgentActionCapability("reviewer", "role_identity"),
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
