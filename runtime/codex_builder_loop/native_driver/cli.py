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
    resume.add_argument("--reason")
    status = commands.add_parser("status")
    status.add_argument("--repo", default=".")
    status.add_argument("--run", required=True)
    return value


def emit(value: dict[str, Any], returncode: int) -> int:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return returncode


def emit_event(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def _exception_payload(exc: CorePortError | NativeDriverError | AppServerError) -> dict[str, Any]:
    if isinstance(exc, CorePortError):
        payload = dict(exc.payload)
        details = payload.get("details")
        if details is None:
            details = {
                key: value
                for key, value in payload.items()
                if key not in {"status", "code", "message"}
            }
        return {
            "status": str(payload.get("status", "FATAL")),
            "code": str(payload.get("code", exc.code)),
            "message": str(payload.get("message", str(exc))),
            "details": details,
        }
    return {
        "status": str(getattr(exc, "status", "FATAL")),
        "code": str(exc.code),
        "message": str(exc),
        "details": getattr(exc, "details", None),
    }


def _failure_action(coordinator: NativeCoordinator | None) -> dict[str, Any] | None:
    action = coordinator.current_action if coordinator is not None else None
    if not isinstance(action, dict):
        return None
    action_id = action.get("action_id")
    name = action.get("action")
    reason = action.get("reason")
    if not all(isinstance(item, str) and item for item in (action_id, name, reason)):
        return None
    return {"action_id": action_id, "action": name, "reason": reason}


def _persist_fatal(
    *,
    core: CorePort,
    repo: Path,
    run_id: str,
    exc: CorePortError | NativeDriverError | AppServerError,
    coordinator: NativeCoordinator | None,
) -> tuple[dict[str, Any], int]:
    original = _exception_payload(exc)
    failure = {
        "source": "native_driver",
        **original,
        "action": _failure_action(coordinator),
    }
    try:
        core.call(
            "record-driver-failure",
            "--repo",
            str(repo),
            "--run",
            run_id,
            "--failure",
            "-",
            "--driver-runtime-kind",
            "native",
            input_value=failure,
        )
        completed = core.call(
            "complete-driver-failure",
            "--repo",
            str(repo),
            "--run",
            run_id,
            "--driver-runtime-kind",
            "native",
        )
    except CorePortError as recovery_error:
        recovery = dict(recovery_error.payload)
        recovery["run_id"] = run_id
        recovery["original_driver_failure"] = original
        return recovery, 1 if recovery_error.status in {"FAIL", "NEEDS_USER"} else 2
    if completed.get("phase") == "finalized":
        return (
            {
                "status": "FINALIZED",
                "run_id": run_id,
                "phase": "finalized",
                "driver_failure": completed.get("driver_failure"),
                "recovered_failure": original,
            },
            0,
        )
    return (
        {
            **original,
            "run_id": run_id,
            "phase": completed.get("phase"),
            "driver_failure": completed.get("driver_failure"),
        },
        2,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    core = CorePort()
    repo = Path(args.repo).resolve()
    run_may_exist = args.command == "resume"
    coordinator: NativeCoordinator | None = None
    try:
        if args.command == "status":
            return emit(
                core.call("driver-context", "--repo", str(repo), "--run", args.run), 0
            )
        capability = probe_app_server(args.codex_bin)
        transport = AppServerTransport(codex_bin=args.codex_bin)
        if args.command == "resume":
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
            dispatch_renewal_reason = None
            if args.reason is not None:
                if not args.reason.strip():
                    raise NativeDriverError(
                        "dispatch renewal requires a non-empty reason",
                        code="NATIVE_DISPATCH_RENEWAL_REASON_REQUIRED",
                        status="NEEDS_USER",
                    )
                dispatch = context.get("dispatch_intent")
                if (
                    isinstance(dispatch, dict)
                    and int(dispatch.get("attempt", 1)) >= 3
                    and dispatch.get("state") in {"prepared", "in_flight", "exhausted"}
                ):
                    dispatch_renewal_reason = args.reason
                else:
                    decision = core.call(
                        "driver-next", "--repo", str(repo), "--run", args.run
                    )
                    if not (
                        decision.get("status") == "NEEDS_USER"
                        and decision.get("action") == "architecture_review"
                        and decision.get("reason") == "tester_correction_limit_reached"
                        and isinstance(
                            decision.get("tester_correction_review_binding"), str
                        )
                    ):
                        raise NativeDriverError(
                            "dispatch renewal is not available for the current run state",
                            code="NATIVE_DISPATCH_RENEWAL_NOT_AVAILABLE",
                            status="NEEDS_USER",
                        )
                    core.call(
                        "authorize-tester-correction",
                        "--repo",
                        str(repo),
                        "--run",
                        args.run,
                        "--action-id",
                        str(decision["action_id"]),
                        "--review-binding",
                        str(decision["tester_correction_review_binding"]),
                        "--reason",
                        args.reason,
                        "--driver-runtime-kind",
                        "native",
                        "--allow-runtime-transition",
                    )
            coordinator = NativeCoordinator(
                repo=repo,
                run_id=args.run,
                core=core,
                transport=transport,
                dispatch_renewal_reason=dispatch_renewal_reason,
                event_sink=emit_event,
            )
        with transport:
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
                run_may_exist = True
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
                coordinator = NativeCoordinator(
                    repo=repo,
                    run_id=args.run,
                    core=core,
                    transport=transport,
                    event_sink=emit_event,
                )
            if coordinator is None:
                raise NativeDriverError(
                    "Native Driver coordinator was not initialized",
                    code="NATIVE_DRIVER_COORDINATOR_MISSING",
                )
            result = coordinator.run()
            result_status = result.get("status")
            return emit(
                result,
                0 if result_status == "FINALIZED" else 2 if result_status == "FAILED" else 1,
            )
    except (CorePortError, NativeDriverError, AppServerError) as exc:
        if isinstance(exc, AppServerError) and coordinator is not None and run_may_exist:
            try:
                retry = coordinator.retry_transport_failure(exc)
            except (CorePortError, NativeDriverError) as retry_error:
                exc = retry_error
            else:
                if retry is not None:
                    return emit(retry, 1)
        status = str(getattr(exc, "status", "FATAL"))
        if status == "FATAL" and run_may_exist:
            payload, returncode = _persist_fatal(
                core=core,
                repo=repo,
                run_id=args.run,
                exc=exc,
                coordinator=coordinator,
            )
            return emit(payload, returncode)
        payload = _exception_payload(exc)
        return emit(payload, 1 if status in {"FAIL", "NEEDS_USER"} else 2)


if __name__ == "__main__":
    raise SystemExit(main())
