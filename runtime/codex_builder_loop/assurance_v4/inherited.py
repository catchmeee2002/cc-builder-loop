"""Immutable handoff obligations, discharged only by current final review evidence."""
from __future__ import annotations

import copy
from typing import Any, Mapping
from .models import digest


def inherited_review_input(ledger: Mapping[str, Any]) -> dict[str, Any]:
    obligations = []
    for problem in ledger.get("problems", []):
        origin = problem.get("inherited_from")
        if not origin:
            continue
        obligations.append({"obligation_id": digest({"origin": origin, "key": problem["key"]}),
                            "origin": copy.deepcopy(origin), "problem_key": problem["key"]})
    required = [k for k in ledger["facets"]["assurance"]["required"]
                if k not in {"reviewer", "doc_review"}]
    evidence = {k: ledger.get("evidence", {}).get(k) for k in required}
    return {"obligations": obligations, "required_evidence": required,
            "evidence_digest": digest({"candidate": ledger["facets"]["execution"].get("candidate_head"),
                                       "evidence": evidence}),
            "result_field": "details.inherited_problem_resolutions"}


def inherited_review_current(ledger: Mapping[str, Any], report: Mapping[str, Any]) -> bool:
    view = inherited_review_input(ledger)
    expected = {item["obligation_id"] for item in view["obligations"]}
    rows = report.get("details", {}).get("inherited_problem_resolutions", [])
    if not expected:
        return not rows
    from .core import evidence_state
    if any(evidence_state(ledger, k) != "pass" for k in view["required_evidence"]):
        return False
    return (len(rows) == len(expected) and {r.get("obligation_id") for r in rows} == expected
            and all(r.get("accepted") is True and isinstance(r.get("rationale"), str)
                    and bool(r["rationale"].strip()) and r.get("evidence_digest") == view["evidence_digest"]
                    for r in rows))


def inherited_resolved(ledger: Mapping[str, Any]) -> bool:
    from .core import evidence_state, work_unit_progress
    return (evidence_state(ledger, "reviewer") == "pass"
            and all(evidence_state(ledger, k) == "pass" for k in ledger["facets"]["assurance"]["required"])
            and not work_unit_progress(ledger).get("pending")
            and inherited_review_current(ledger, ledger.get("evidence", {}).get("reviewer", {})))


def handoff_problem_key(problem: Mapping[str, Any]) -> str:
    """Keep a new same-key report distinct from the immutable inherited obligation."""
    origin = problem.get("inherited_from")
    return ("inherited-" + digest({"origin": origin, "key": problem["key"]})
            if origin else str(problem["key"]))
