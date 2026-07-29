from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
ACTIVE_PHASES = {
    "active",
    "integration_conflict",
    "finalize_conflict",
    "continuity_failure",
    "iteration_limit",
    "no_progress",
    "architecture_review_required",
    "finalized_cleanup",
}


class LifecycleDeliveryError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def runtime_root() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        base = Path(configured).expanduser()
    else:
        base = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        base = base / f"codex-builder-loop-{os.getuid()}"
        return _secure_directory(base)
    return _secure_directory(base / "codex-builder-loop")


def _secure_directory(path: Path) -> Path:
    current = path.expanduser()
    if current.exists() and current.is_symlink():
        raise LifecycleDeliveryError(
            f"lifecycle runtime directory is a symlink: {current}",
            code="LIFECYCLE_RUNTIME_SYMLINK",
        )
    current.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = current.resolve()
    info = current.stat()
    if info.st_uid != os.getuid():
        raise LifecycleDeliveryError(
            f"lifecycle runtime directory has another owner: {current}",
            code="LIFECYCLE_RUNTIME_OWNER",
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(current, 0o700)
    return current


def session_key(session_id: str) -> str:
    if not session_id:
        raise LifecycleDeliveryError(
            "lifecycle delivery requires a session id",
            code="LIFECYCLE_SESSION_REQUIRED",
        )
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def session_dir(session_id: str) -> Path:
    sessions = _secure_directory(runtime_root() / "sessions")
    return _secure_directory(sessions / session_key(session_id))


def route_path(session_id: str) -> Path:
    return session_dir(session_id) / "route.json"


def inbox_dir(session_id: str) -> Path:
    return _secure_directory(session_dir(session_id) / "inbox")


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp.exists():
            temp.unlink()


def read_json_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LifecycleDeliveryError(
            f"lifecycle file is not a regular file: {path}",
            code="LIFECYCLE_FILE_INVALID",
        )
    info = path.stat()
    if info.st_uid != os.getuid():
        raise LifecycleDeliveryError(
            f"lifecycle file has another owner: {path}",
            code="LIFECYCLE_FILE_OWNER",
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise LifecycleDeliveryError(
            f"lifecycle file permissions are not private: {path}",
            code="LIFECYCLE_FILE_PERMISSIONS",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleDeliveryError(
            f"cannot read lifecycle JSON: {path}: {exc}",
            code="LIFECYCLE_JSON_INVALID",
        ) from exc
    if not isinstance(value, dict):
        raise LifecycleDeliveryError(
            f"lifecycle JSON is not an object: {path}",
            code="LIFECYCLE_JSON_INVALID",
        )
    return value


def route_binding(session_id: str, repo_root: str, run_id: str) -> str:
    raw = json.dumps(
        [session_id, str(Path(repo_root).resolve()), run_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def register_route(
    *,
    session_id: str,
    repo_root: str,
    run_id: str,
    ledger_path: str,
    tester_start_attestation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "repo_root": str(Path(repo_root).resolve()),
        "run_id": run_id,
        "ledger_path": str(Path(ledger_path).resolve()),
        "binding_sha256": route_binding(session_id, repo_root, run_id),
        "accepting_events": True,
        "tester_start_attestation": tester_start_attestation,
        "created_at": utc_now(),
    }
    atomic_write_json(route_path(session_id), route)
    inbox_dir(session_id)
    return route


def load_route(session_id: str) -> dict[str, Any] | None:
    path = route_path(session_id)
    if not path.exists():
        return None
    route = read_json_file(path)
    required = {
        "schema_version",
        "session_id",
        "repo_root",
        "run_id",
        "ledger_path",
        "binding_sha256",
        "accepting_events",
        "tester_start_attestation",
        "created_at",
    }
    legacy_required = required - {"accepting_events", "tester_start_attestation"}
    if (
        frozenset(route) not in {frozenset(required), frozenset(legacy_required)}
        or route.get("schema_version") != SCHEMA_VERSION
    ):
        raise LifecycleDeliveryError(
            "lifecycle route does not match schema version 1",
            code="LIFECYCLE_ROUTE_INVALID",
        )
    route.setdefault("accepting_events", True)
    route.setdefault("tester_start_attestation", None)
    if route.get("session_id") != session_id:
        raise LifecycleDeliveryError(
            "lifecycle route session does not match lookup session",
            code="LIFECYCLE_ROUTE_SESSION_MISMATCH",
        )
    expected = route_binding(
        session_id, str(route.get("repo_root")), str(route.get("run_id"))
    )
    if route.get("binding_sha256") != expected:
        raise LifecycleDeliveryError(
            "lifecycle route binding digest does not match",
            code="LIFECYCLE_ROUTE_BINDING_MISMATCH",
        )
    return route


def set_tester_start_attestation(
    session_id: str, attestation: dict[str, Any] | None
) -> dict[str, Any]:
    route = load_route(session_id)
    if route is None:
        raise LifecycleDeliveryError(
            "lifecycle route is missing for this session",
            code="LIFECYCLE_ROUTE_MISSING",
        )
    route["tester_start_attestation"] = attestation
    atomic_write_json(route_path(session_id), route)
    return route


def deactivate_route(session_id: str) -> dict[str, Any] | None:
    route = load_route(session_id)
    if route is None:
        return None
    route["accepting_events"] = False
    route["tester_start_attestation"] = None
    atomic_write_json(route_path(session_id), route)
    return route


def remove_route(session_id: str, *, require_empty: bool = True) -> bool:
    inbox = inbox_dir(session_id)
    if require_empty and any(inbox.iterdir()):
        return False
    path = route_path(session_id)
    if path.exists() and not path.is_symlink():
        path.unlink()
    return True


def event_id(value: dict[str, Any]) -> str:
    stable = {
        key: value.get(key)
        for key in (
            "schema_version",
            "binding_sha256",
            "session_id",
            "run_id",
            "role",
            "agent_id",
            "turn_id",
            "event",
            "result",
            "tester_baseline",
        )
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def enqueue_event(
    *,
    session_id: str,
    role: str,
    agent_id: str,
    turn_id: str,
    event: str,
    result: str | None,
) -> tuple[dict[str, Any] | None, Path | None]:
    route = load_route(session_id)
    if route is None:
        raise LifecycleDeliveryError(
            "lifecycle route is missing for this session",
            code="LIFECYCLE_ROUTE_MISSING",
        )
    if route.get("accepting_events") is not True:
        raise LifecycleDeliveryError(
            "lifecycle route is not accepting new agent events",
            code="LIFECYCLE_ROUTE_STALE",
        )
    tester_baseline = (
        route.get("tester_start_attestation")
        if role == "tester" and event == "start"
        else None
    )
    if role == "tester" and event == "start" and not isinstance(
        tester_baseline, dict
    ):
        raise LifecycleDeliveryError(
            "Tester start attestation was not prepared before spawn",
            code="TESTER_START_ATTESTATION_MISSING",
        )
    envelope: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "binding_sha256": route["binding_sha256"],
        "session_id": session_id,
        "run_id": route["run_id"],
        "repo_root": route["repo_root"],
        "role": role,
        "agent_id": agent_id,
        "turn_id": turn_id,
        "event": event,
        "result": result,
        "tester_baseline": tester_baseline,
        "captured_at": utc_now(),
    }
    envelope["event_id"] = event_id(envelope)
    destination = inbox_dir(session_id) / f"{envelope['event_id']}.json"
    if destination.exists():
        existing = read_json_file(destination)
        if (
            existing.get("event_id") != envelope["event_id"]
            or event_id(existing) != envelope["event_id"]
        ):
            raise LifecycleDeliveryError(
                "lifecycle event id collision",
                code="LIFECYCLE_EVENT_COLLISION",
            )
        return existing, destination
    atomic_write_json(destination, envelope)
    return envelope, destination


def queued_event_paths(session_id: str) -> list[Path]:
    paths = list(inbox_dir(session_id).glob("*.json"))

    def order(path: Path) -> tuple[str, int, str]:
        try:
            value = read_json_file(path)
            lifecycle_order = 0 if value.get("event") == "start" else 1
            return str(value.get("captured_at") or ""), lifecycle_order, path.name
        except LifecycleDeliveryError:
            return "", 2, path.name

    return sorted(paths, key=order)


def delivery_facts(
    *, session_id: str, repo_root: str, run_id: str
) -> dict[str, Any]:
    try:
        route = load_route(session_id)
        if route is None:
            locator = "missing"
        elif (
            route.get("repo_root") != str(Path(repo_root).resolve())
            or route.get("run_id") != run_id
        ):
            locator = "stale"
        else:
            locator = "ready"
        paths = queued_event_paths(session_id)
        oldest = None
        blocked = None
        if paths:
            captured = []
            for path in paths:
                try:
                    captured.append(str(read_json_file(path).get("captured_at") or ""))
                except LifecycleDeliveryError as exc:
                    captured.append("")
                    if blocked is None:
                        blocked = {
                            "event_file": path.name,
                            "code": exc.code,
                            "message": str(exc),
                        }
            values = [value for value in captured if value]
            oldest = min(values) if values else None
        return {
            "locator": locator,
            "queued_count": len(paths),
            "oldest_queued_at": oldest,
            "blocked_event": blocked,
        }
    except LifecycleDeliveryError as exc:
        return {
            "locator": "invalid",
            "queued_count": 0,
            "oldest_queued_at": None,
            "blocked_event": {"code": exc.code, "message": str(exc)},
        }
