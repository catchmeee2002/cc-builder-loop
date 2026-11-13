"""Host identity and cleanup facts shared by single-operation and frozen adapters."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from ..process import process_group_gone, read_proc_identity
from ..assurance_v4.models import digest
from ..assurance_v4.result_application import ResultApplicationError as NativeDriverError
from .core_port import CorePort

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
    grant = context.get("native_host_transition") or {}
    if frozen_executable != current and not (grant.get("from") == frozen_executable and grant.get("to") == current):
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


def _root_session_identity(session_id: str) -> dict[str, Any]:
    agent_id = f"codex-root-session:{session_id}"
    return {
        "session_id": session_id,
        "agent_id": agent_id,
        "identity_digest": digest(
            {"mode": "root_session", "session_id": session_id, "agent_id": agent_id}
        ),
    }


def _root_runtime(
    session_id: str,
    *,
    native_transport: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": "native",
        "protocol_version": 1,
        "transport": "root_session",
        "runtime_version": "root-session",
        "protocol_schema_digest": digest(
            {"schema_version": 1, "mode": "root_session"}
        ),
        "protocol_canary_digest": None,
        "root_session_identity": _root_session_identity(session_id),
        "native_transport": native_transport,
    }
