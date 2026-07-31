from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


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
        if item["trigger"] not in {"mission_change"} and item["semantic_revision"]
    ]
    unsafe_continuations = [
        item["source_run"]
        for item in scenarios
        if item["trigger"] in {"mission_change", "git_conflict"} and not item["needs_user"]
    ]
    return {
        "status": "PASS" if len(audit) == 26 and len(chain) == 8 and not false_revisions and not unsafe_continuations else "FAIL",
        "audit_sample_count": len(audit),
        "issue_158_chain_count": len(chain),
        "trigger_counts": dict(sorted(Counter(item["trigger"] for item in audit).items())),
        "false_semantic_revisions": false_revisions,
        "unsafe_continuations": unsafe_continuations,
    }


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, sort_keys=True))
