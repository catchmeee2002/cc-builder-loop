#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    if not path.exists():
        return {"roles": {}}
    return json.loads(path.read_text())


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--role", choices=["tester", "reviewer"], required=True)
    parser.add_argument("--op", choices=["create", "follow-up", "inspect"], required=True)
    parser.add_argument("--thread-id", default="")
    args = parser.parse_args()

    path = Path(args.state)
    data = load(path)
    roles = data.setdefault("roles", {})
    record = roles.setdefault(
        args.role,
        {"thread_id": "", "create_count": 0, "follow_up_count": 0},
    )

    if args.op == "create":
        if record["thread_id"]:
            print(json.dumps({"status": "FATAL", "reason": "duplicate_create"}))
            return 2
        record["create_count"] += 1
        record["thread_id"] = f"{args.role}-thread-{record['create_count']}"
        save(path, data)
        print(json.dumps({"status": "READY", "action": "create", **record}))
        return 0

    if args.op == "follow-up":
        if not record["thread_id"]:
            print(json.dumps({"status": "FATAL", "reason": "missing_thread"}))
            return 2
        if args.thread_id != record["thread_id"]:
            print(json.dumps({"status": "FATAL", "reason": "thread_mismatch"}))
            return 2
        record["follow_up_count"] += 1
        save(path, data)
        print(json.dumps({"status": "READY", "action": "follow_up", **record}))
        return 0

    print(json.dumps({"status": "READY", **record}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
