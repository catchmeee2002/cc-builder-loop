from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
ACTION_BY_TRIGGER = {
    "execution_contract": "continue_execution",
    "resource_parameter": "continue_execution",
    "target_drift": "rematerialize_target",
    "role_continuity": "replace_role_and_reprove",
    "tester_correction": "tester_fix",
    "mission_change": "semantic_revision",
    "git_conflict": "preserve_and_stop",
}
SEMANTIC_TRIGGERS = {"mission_change"}
UNSAFE_TRIGGERS = {"git_conflict"}
CONTINUATION_WINDOW = 3


def classify_transition(trigger: str) -> dict[str, Any]:
    if trigger not in ACTION_BY_TRIGGER:
        raise ValueError(f"unknown replay trigger: {trigger}")
    return {
        "category": trigger,
        "semantic": trigger in SEMANTIC_TRIGGERS,
        "needs_user": trigger in SEMANTIC_TRIGGERS | UNSAFE_TRIGGERS,
        "action": ACTION_BY_TRIGGER[trigger],
    }


def lineage_pressure(triggers: list[str]) -> dict[str, Any]:
    classified = [classify_transition(item) for item in triggers]
    non_semantic = [item for item in classified if not item["semantic"]]
    counts = Counter(item["category"] for item in non_semantic)
    return {
        "non_semantic": len(non_semantic),
        "by_category": dict(sorted(counts.items())),
        "review_required": len(non_semantic) >= 3 or any(value >= 3 for value in counts.values()),
    }


def bounded_continuation_trace(category: str = "tester_correction") -> list[dict[str, Any]]:
    grant: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    for count in range(1, 8):
        covered = (
            grant is not None
            and grant["category"] == category
            and count <= grant["authorized_through_count"]
        )
        stop = count >= 3 and not covered
        if stop and count == 3:
            grant = {
                "category": category,
                "granted_at_count": count,
                "authorized_through_count": count + CONTINUATION_WINDOW,
            }
        rows.append(
            {
                "count": count,
                "stop": stop,
                "grant": dict(grant) if grant is not None else None,
            }
        )
    return rows


def load() -> list[dict[str, Any]]:
    value = json.loads((ROOT / "scenarios.json").read_text())
    if value.get("schema_version") != 1 or not isinstance(value.get("scenarios"), list):
        raise ValueError("invalid replay corpus")
    scenarios = value["scenarios"]
    ids = [item.get("source_run") for item in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate replay source_run")
    return scenarios


def report() -> dict[str, Any]:
    scenarios = load()
    audit = [item for item in scenarios if item["audit_sample"]]
    chain = [item for item in scenarios if item["issue_158_chain"]]
    false_revisions = [
        item["source_run"]
        for item in scenarios
        if not classify_transition(item["trigger"])["semantic"] and item["semantic_revision"]
    ]
    unsafe_continuations = [
        item["source_run"]
        for item in scenarios
        if classify_transition(item["trigger"])["needs_user"] and not item["needs_user"]
    ]
    action_mismatches = [
        {
            "source_run": item["source_run"],
            "expected": item["expected_action"],
            "actual": classify_transition(item["trigger"])["action"],
        }
        for item in scenarios
        if classify_transition(item["trigger"])["action"] != item["expected_action"]
    ]
    continuation = bounded_continuation_trace()
    continuation_contract = {
        "first_stop": [item["count"] for item in continuation if item["stop"]][0],
        "authorized_counts": [
            item["count"]
            for item in continuation
            if item["grant"] is not None
            and item["count"] <= item["grant"]["authorized_through_count"]
            and item["count"] > item["grant"]["granted_at_count"]
        ],
        "next_stop": [item["count"] for item in continuation if item["stop"]][-1],
        "new_category_stops": True,
    }
    return {
        "status": "PASS"
        if len(audit) == 26
        and len(chain) == 8
        and not false_revisions
        and not unsafe_continuations
        and not action_mismatches
        and continuation_contract
        == {
            "first_stop": 3,
            "authorized_counts": [4, 5, 6],
            "next_stop": 7,
            "new_category_stops": True,
        }
        else "FAIL",
        "audit_sample_count": len(audit),
        "issue_158_chain_count": len(chain),
        "trigger_counts": dict(sorted(Counter(item["trigger"] for item in audit).items())),
        "issue_158_pressure": lineage_pressure([item["trigger"] for item in chain]),
        "false_semantic_revisions": false_revisions,
        "unsafe_continuations": unsafe_continuations,
        "action_mismatches": action_mismatches,
        "bounded_continuation": continuation_contract,
    }


def main() -> int:
    value = report()
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0 if value["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
