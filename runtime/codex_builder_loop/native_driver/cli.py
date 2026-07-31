from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from ..assurance_v4.models import load_json_source
from .app_server import AppServerError, AppServerTransport, probe_app_server
from .coordinator import NativeCoordinator, NativeDriverError
from .core_port import CorePort, CorePortError


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="codex-builder-loop native-driver")
    value.add_argument("--codex-bin", default="codex")
    commands = value.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--repo", default=".")
    start.add_argument("--run", required=True)
    start.add_argument("--session-id", required=True)
    start.add_argument("--contract", required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("--repo", default=".")
    resume.add_argument("--run", required=True)
    status = commands.add_parser("status")
    status.add_argument("--repo", default=".")
    status.add_argument("--run", required=True)
    return value


def emit(value: dict[str, Any], returncode: int) -> int:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return returncode


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    core = CorePort()
    try:
        repo = Path(args.repo).resolve()
        if args.command == "status":
            return emit(
                core.call("driver-context", "--repo", str(repo), "--run", args.run), 0
            )
        capability = probe_app_server(args.codex_bin)
        with AppServerTransport(codex_bin=args.codex_bin) as transport:
            if args.command == "start":
                raw = sys.stdin.read() if args.contract == "-" else None
                contract = load_json_source(args.contract, stdin_text=raw)
                started = core.start(
                    repo=repo,
                    run_id=args.run,
                    session_id=args.session_id,
                    contract=contract,
                    runtime_version=capability.runtime_version,
                    protocol_schema_digest=capability.protocol_schema_digest,
                )
                print(
                    json.dumps(
                        {
                            "event": "native_driver_run_started",
                            "run_id": args.run,
                            "candidate_worktree": started.get("candidate_worktree"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )
            else:
                context = core.call(
                    "driver-context", "--repo", str(repo), "--run", args.run
                )
                runtime = context.get("driver_runtime")
                if not isinstance(runtime, dict) or runtime.get("kind") != "native":
                    raise NativeDriverError(
                        "run is not owned by Native Driver",
                        code="NATIVE_DRIVER_NOT_OWNER",
                        status="NEEDS_USER",
                    )
                if runtime.get("protocol_version") != 1:
                    raise NativeDriverError(
                        "run uses an unsupported DriverPort version",
                        code="NATIVE_DRIVER_PORT_INCOMPATIBLE",
                        status="NEEDS_USER",
                    )
            result = NativeCoordinator(
                repo=repo,
                run_id=args.run,
                core=core,
                transport=transport,
            ).run()
            return emit(result, 0 if result.get("status") == "FINALIZED" else 1)
    except CorePortError as exc:
        return emit(exc.payload, 1 if exc.status in {"FAIL", "NEEDS_USER"} else 2)
    except (NativeDriverError, AppServerError) as exc:
        status = getattr(exc, "status", "FATAL")
        payload = {
            "status": status,
            "code": exc.code,
            "message": str(exc),
            "details": getattr(exc, "details", None),
        }
        return emit(payload, 1 if status == "NEEDS_USER" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
