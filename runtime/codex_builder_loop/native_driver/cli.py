from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from ..assurance_v4.models import load_json_source
from ..process import process_group_gone, read_proc_identity
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
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--repo", default=".")
    doctor.add_argument("--run", required=True)
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
    transport: Any = None,
) -> tuple[dict[str, Any], int]:
    original = _exception_payload(exc)
    failure = {
        "source": "native_driver",
        **original,
        "action": _failure_action(coordinator),
    }
    receipt_fn = getattr(transport, "diagnostic_receipt", None)
    if callable(receipt_fn):
        try:
            failure["diagnostic_receipt"] = receipt_fn(
                failure_code=original["code"],
                turn_error=original.get("details"),
            )
        except (OSError, TypeError, ValueError):
            pass
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


def _persist_transport_cleanup(
    *,
    core: CorePort,
    repo: Path,
    run_id: str,
    transport: Any,
) -> None:
    observation = getattr(transport, "cleanup_observation", None)
    snapshot_fn = getattr(transport, "runtime_snapshot", None)
    if not isinstance(observation, dict) or not callable(snapshot_fn):
        return
    snapshot = snapshot_fn()
    if not isinstance(snapshot, dict):
        return
    generation = snapshot.get("generation")
    process_identity = snapshot.get("process_identity")
    state = observation.get("state")
    if (
        not isinstance(generation, str)
        or generation == "unbound"
        or not isinstance(process_identity, dict)
        or state not in {"cleaned", "cleanup_unknown"}
    ):
        return
    core.call(
        "record-transport-cleanup",
        "--repo",
        str(repo),
        "--run",
        run_id,
        "--generation",
        generation,
        "--process-identity",
        "-",
        "--state",
        "completed" if state == "cleaned" else "unknown",
        "--term-attempt",
        str(int(observation.get("term_attempt", 0))),
        "--kill-attempt",
        str(int(observation.get("kill_attempt", 0))),
        "--driver-runtime-kind",
        "native",
        *(
            ["--process-group-gone"]
            if observation.get("process_group_gone") is True
            else []
        ),
        input_value=process_identity,
    )


def _verify_resume_executable_identity(
    context: dict[str, Any],
    capability: Any,
) -> None:
    runtime = context.get("driver_runtime")
    if not isinstance(runtime, dict):
        return
    frozen = runtime.get("native_transport")
    current = getattr(capability, "executable_identity", None)
    if not isinstance(frozen, dict) or not isinstance(current, dict):
        return
    frozen_executable = frozen.get("executable_identity")
    if frozen_executable != current:
        raise NativeDriverError(
            "Native Driver executable identity changed",
            code="NATIVE_DRIVER_EXECUTABLE_IDENTITY_DRIFT",
            status="NEEDS_USER",
            details={
                "expected": frozen_executable,
                "actual": current,
            },
        )


def _reconcile_previous_transport(
    *,
    core: CorePort,
    repo: Path,
    run_id: str,
    context: dict[str, Any],
) -> None:
    runtime = context.get("driver_runtime")
    native_transport = (
        runtime.get("native_transport")
        if isinstance(runtime, dict)
        else None
    )
    if not isinstance(native_transport, dict):
        return
    if native_transport.get("state") == "cleaned":
        return
    if native_transport.get("state") == "cleanup_unknown":
        raise NativeDriverError(
            "previous Native transport cleanup is unknown",
            code="NATIVE_TRANSPORT_PREVIOUS_CLEANUP_UNKNOWN",
            status="NEEDS_USER",
        )
    process_identity = native_transport.get("process_identity")
    generation = native_transport.get("generation")
    if not isinstance(process_identity, dict) or not isinstance(generation, str):
        return
    pid = process_identity.get("pid")
    pgid = process_identity.get("pgid")
    starttime = process_identity.get("starttime")
    if not isinstance(pid, int) or not isinstance(pgid, int) or not isinstance(
        starttime, str
    ):
        raise NativeDriverError(
            "previous Native transport process identity is incomplete",
            code="NATIVE_TRANSPORT_PREVIOUS_IDENTITY_INVALID",
            status="NEEDS_USER",
        )
    live = read_proc_identity(pid)
    group_gone = process_group_gone(pgid)
    identity_match = bool(
        isinstance(live, dict)
        and live.get("pgid") == pgid
        and live.get("starttime") == starttime
    )
    if identity_match and not group_gone:
        raise NativeDriverError(
            "previous Native transport process is still alive",
            code="NATIVE_TRANSPORT_PREVIOUS_PROCESS_ALIVE",
            status="NEEDS_USER",
            details={"pid": pid, "pgid": pgid, "generation": generation},
        )
    if not group_gone:
        raise NativeDriverError(
            "previous Native transport process group is unresolved",
            code="NATIVE_TRANSPORT_PREVIOUS_PROCESS_UNKNOWN",
            status="NEEDS_USER",
            details={"pid": pid, "pgid": pgid, "generation": generation},
        )
    reconciled = dict(process_identity)
    reconciled["exited_at"] = datetime.now(timezone.utc).isoformat()
    reconciled["exit_code"] = None
    reconciled["process_group_gone"] = True
    core.call(
        "record-transport-cleanup",
        "--repo",
        str(repo),
        "--run",
        run_id,
        "--generation",
        generation,
        "--process-identity",
        "-",
        "--state",
        "completed",
        "--term-attempt",
        "0",
        "--kill-attempt",
        "0",
        "--process-group-gone",
        "--driver-runtime-kind",
        "native",
        input_value=reconciled,
    )


def _doctor_payload(context: dict[str, Any]) -> dict[str, Any]:
    runtime = context.get("driver_runtime")
    native_transport = (
        runtime.get("native_transport")
        if isinstance(runtime, dict)
        else None
    )
    if not isinstance(native_transport, dict):
        return {
            "status": "READY",
            "diagnostic_state": "legacy-unbound",
            "transport": None,
        }
    process_identity = native_transport.get("process_identity")
    if not isinstance(process_identity, dict):
        return {
            "status": "READY",
            "diagnostic_state": "no-live-process",
            "transport": native_transport,
        }
    pid = process_identity.get("pid")
    live = read_proc_identity(pid) if isinstance(pid, int) else None
    identity_match = bool(
        isinstance(live, dict)
        and live.get("pgid") == process_identity.get("pgid")
        and live.get("starttime") == process_identity.get("starttime")
    )
    group_gone = (
        process_group_gone(int(process_identity["pgid"]))
        if isinstance(process_identity.get("pgid"), int)
        else None
    )
    state = "ready"
    if native_transport.get("state") == "cleanup_unknown" or (
        identity_match and group_gone is False
    ):
        state = "needs_user"
    elif native_transport.get("state") in {"ready", "initializing", "spawning"} and (
        not identity_match or group_gone is not False
    ):
        state = "needs_user"
    return {
        "status": "NEEDS_USER" if state == "needs_user" else "READY",
        "diagnostic_state": state,
        "transport": {
            **native_transport,
            "live_process_identity": live,
            "identity_match": identity_match,
            "process_group_gone": group_gone,
        },
        "dispatch_intent": context.get("dispatch_intent"),
        "transport_cleanup_intent": context.get("transport_cleanup_intent"),
        "deferred_wait": context.get("deferred_wait"),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    core = CorePort()
    repo = Path(args.repo).resolve()
    run_may_exist = args.command == "resume"
    coordinator: NativeCoordinator | None = None
    transport: Any = None
    try:
        if args.command in {"status", "doctor"}:
            context = core.call(
                "driver-context", "--repo", str(repo), "--run", args.run
            )
            if args.command == "doctor":
                payload = _doctor_payload(context)
                payload["run_id"] = args.run
                return emit(payload, 0 if payload["status"] == "READY" else 1)
            return emit(
                context, 0
            )
        capability = probe_app_server(args.codex_bin, strict_protocol=True)
        transport = AppServerTransport(
            codex_bin=args.codex_bin,
            strict_protocol=True,
            executable_identity=getattr(capability, "executable_identity", None),
        )
        if args.command == "resume":
            context = core.call(
                "driver-context", "--repo", str(repo), "--run", args.run
            )
            _verify_resume_executable_identity(context, capability)
            _reconcile_previous_transport(
                core=core,
                repo=repo,
                run_id=args.run,
                context=context,
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
                thread_compaction_available=getattr(
                    capability, "thread_compaction", False
                ),
            )
        result_payload: dict[str, Any] | None = None
        result_returncode: int | None = None
        with transport:
            if args.command == "resume" and hasattr(transport, "runtime_snapshot"):
                core.call(
                    "bind-native-transport",
                    "--repo",
                    str(repo),
                    "--run",
                    args.run,
                    "--transport",
                    "-",
                    "--driver-runtime-kind",
                    "native",
                    input_value=transport.runtime_snapshot(),
                )
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
                    protocol_canary_digest=getattr(capability, "protocol_canary_digest", None),
                    native_transport=(
                        transport.runtime_snapshot()
                        if hasattr(transport, "runtime_snapshot")
                        else None
                    ),
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
                    thread_compaction_available=getattr(
                        capability, "thread_compaction", False
                    ),
                )
            if coordinator is None:
                raise NativeDriverError(
                    "Native Driver coordinator was not initialized",
                    code="NATIVE_DRIVER_COORDINATOR_MISSING",
                )
            result = coordinator.run()
            result_status = result.get("status")
            result_payload = result
            result_returncode = (
                0 if result_status == "FINALIZED" else 2 if result_status == "FAILED" else 1
            )
        if result_payload is not None and result_returncode is not None:
            try:
                if run_may_exist:
                    _persist_transport_cleanup(
                        core=core,
                        repo=repo,
                        run_id=args.run,
                        transport=transport,
                    )
            except CorePortError as cleanup_error:
                return emit(
                    {
                        "status": "NEEDS_USER",
                        "code": "NATIVE_TRANSPORT_CLEANUP_RECORD_FAILED",
                        "message": str(cleanup_error),
                        "details": {
                            "cleanup_error": cleanup_error.payload,
                            "original_result": result_payload,
                        },
                    },
                    1,
                )
            return emit(result_payload, result_returncode)
    except (CorePortError, NativeDriverError, AppServerError) as exc:
        if run_may_exist and transport is not None:
            try:
                _persist_transport_cleanup(
                    core=core,
                    repo=repo,
                    run_id=args.run,
                    transport=transport,
                )
            except CorePortError as cleanup_error:
                return emit(
                    {
                        "status": "NEEDS_USER",
                        "code": "NATIVE_TRANSPORT_CLEANUP_RECORD_FAILED",
                        "message": str(cleanup_error),
                        "details": {
                            "cleanup_error": cleanup_error.payload,
                            "original_error": _exception_payload(exc),
                        },
                    },
                    1,
                )
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
                transport=transport,
            )
            return emit(payload, returncode)
        payload = _exception_payload(exc)
        return emit(payload, 1 if status in {"FAIL", "NEEDS_USER"} else 2)


if __name__ == "__main__":
    raise SystemExit(main())
