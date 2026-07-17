#!/usr/bin/env python3
from __future__ import annotations

import json
import os


def main() -> int:
    status = os.environ.get("MOCK_RUNTIME_STATUS", "NOOP")
    message = os.environ.get("MOCK_RUNTIME_MESSAGE", f"mock status {status}")
    run_id = os.environ.get("MOCK_RUNTIME_RUN_ID", "fixture-run")
    print(json.dumps({"status": status, "message": message, "run_id": run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
