#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    retrospective = "retrospective-status" in sys.argv[1:]
    prefix = "MOCK_RETROSPECTIVE" if retrospective else "MOCK_RUNTIME"
    status = os.environ.get(f"{prefix}_STATUS", "NOOP")
    message = os.environ.get(f"{prefix}_MESSAGE", f"mock status {status}")
    run_id = os.environ.get("MOCK_RUNTIME_RUN_ID", "fixture-run")
    payload = {"status": status, "message": message, "run_id": run_id}
    if retrospective:
        payload.update(
            {
                "owner_session_id": os.environ.get(
                    "MOCK_RETROSPECTIVE_SESSION_ID", "hook-fixture-session"
                ),
                "required_block": os.environ.get(
                    "MOCK_RETROSPECTIVE_BLOCK", f"fixture retrospective {status}"
                ),
            }
        )
        if "MOCK_RETROSPECTIVE_USER_BLOCK" in os.environ:
            payload["required_user_block"] = os.environ[
                "MOCK_RETROSPECTIVE_USER_BLOCK"
            ]
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
