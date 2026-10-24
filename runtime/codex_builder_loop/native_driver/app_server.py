from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import select
import threading
import time
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, IO

from ..process import capture_process_identity, digest as process_digest, reap_process_group


class AppServerError(RuntimeError):
    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class AppServerCapability:
    runtime_version: str
    protocol_schema_digest: str
    thread_compaction: bool
    protocol_canary_digest: str | None = None
    executable_identity: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    status: str
    text: str
    error: Any = None


@dataclass(frozen=True)
class ThreadCompactionResult:
    turn_id: str
    item_id: str


REQUIRED_PROTOCOL_TOKENS = (
    '"thread/start"',
    '"thread/resume"',
    '"thread/read"',
    '"turn/start"',
    '"turn/interrupt"',
    '"developerInstructions"',
    '"outputSchema"',
    '"clientUserMessageId"',
)

TURN_IDLE_TIMEOUT_SECONDS = 120.0
TESTER_AUTHOR_IDLE_TIMEOUT_SECONDS = 360.0
INITIALIZE_TIMEOUT_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 30.0
TURN_TOTAL_TIMEOUT_SECONDS = 3600.0
LONG_TESTER_TURN_TOTAL_TIMEOUT_SECONDS = 7200.0
COMPACTION_TOTAL_TIMEOUT_SECONDS = 600.0
LONG_TESTER_ACTIONS = frozenset(
    {
        "tester_author",
        "tester_fix",
        "tester_recompose_fix",
    }
)
PROTOCOL_CANARY_ATTEMPTS = 3
PROTOCOL_CANARY_RETRY_DELAY_SECONDS = 0.5
PROCESS_CLEANUP_GRACE_SECONDS = 5.0
STDERR_CAPTURE_LIMIT_BYTES = 16 * 1024
STDERR_READ_CHUNK_BYTES = 64 * 1024

_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_AUTH_HEADER_RE = re.compile(r"(?i)((?:proxy-)?authorization\s*[:=]\s*)(?:basic|bearer)\s+[^\s,;\"'}]+")
_URL_USERINFO_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:authorization|api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|"
    r"password|passwd|secret|token)\s*[:=]\s*)(['\"]?)[^,\s'\"}]+"
)
_ENV_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:[a-z0-9]+_)*(?:api_key|access_token|secret(?:_key)?|password)"
    r"\s*=\s*)(['\"]?)[^,\s'\"}]+"
)
_TOKEN_SHAPE_RE = re.compile(
    r"(?i)\b(?:sk-(?:proj-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]{12,}|"
    r"xox[baprs]-[a-z0-9-]{12,}|eyj[a-z0-9_-]{20,}\.[a-z0-9_-]{10,}\."
    r"[a-z0-9_-]{10,})"
)


@dataclass(frozen=True)
class TurnTimeoutProfile:
    turn_idle_seconds: float
    turn_total_seconds: float
    compaction_total_seconds: float

    def as_dict(self) -> dict[str, float]:
        return {
            "turn_idle_seconds": self.turn_idle_seconds,
            "turn_total_seconds": self.turn_total_seconds,
            "compaction_total_seconds": self.compaction_total_seconds,
        }

    @property
    def profile_digest(self) -> str:
        return _canonical_digest(self.as_dict())


def timeout_profile_for_action(action: str) -> TurnTimeoutProfile:
    return TurnTimeoutProfile(
        turn_idle_seconds=(
            TESTER_AUTHOR_IDLE_TIMEOUT_SECONDS
            if action == "tester_author"
            else TURN_IDLE_TIMEOUT_SECONDS
        ),
        turn_total_seconds=(
            LONG_TESTER_TURN_TOTAL_TIMEOUT_SECONDS
            if action in LONG_TESTER_ACTIONS
            else TURN_TOTAL_TIMEOUT_SECONDS
        ),
        compaction_total_seconds=COMPACTION_TOTAL_TIMEOUT_SECONDS,
    )


def default_timeout_profile() -> TurnTimeoutProfile:
    return timeout_profile_for_action("")


def compaction_timeout_profile() -> TurnTimeoutProfile:
    return TurnTimeoutProfile(
        turn_idle_seconds=TURN_IDLE_TIMEOUT_SECONDS,
        turn_total_seconds=COMPACTION_TOTAL_TIMEOUT_SECONDS,
        compaction_total_seconds=COMPACTION_TOTAL_TIMEOUT_SECONDS,
    )


def _redact_text(value: str) -> tuple[str, int]:
    redactions = 0

    def replace_bearer(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return "Bearer [REDACTED]"

    def replace_auth_header(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return match.group(1) + "[REDACTED]"

    value = _AUTH_HEADER_RE.sub(replace_auth_header, value)
    value = _BEARER_RE.sub(replace_bearer, value)

    def replace_url_userinfo(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return match.group(1) + "[REDACTED]@"

    value = _URL_USERINFO_RE.sub(replace_url_userinfo, value)

    def replace_assignment(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return f"{match.group(1)}[REDACTED]"

    value = _SECRET_ASSIGNMENT_RE.sub(replace_assignment, value)
    value = _ENV_SECRET_ASSIGNMENT_RE.sub(replace_assignment, value)

    def replace_token_shape(match: re.Match[str]) -> str:
        nonlocal redactions
        redactions += 1
        return "[REDACTED]"

    value = _TOKEN_SHAPE_RE.sub(replace_token_shape, value)
    return value, redactions


def _redact_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        redacted: list[Any] = []
        count = 0
        for item in value:
            item_value, item_count = _redact_value(item)
            redacted.append(item_value)
            count += item_count
        return redacted, count
    if isinstance(value, dict):
        redacted_dict: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in {
                "authorization", "proxyauthorization", "token", "accesstoken",
                "refreshtoken", "idtoken", "apikey", "password", "passwd",
                "secret", "clientsecret",
            }:
                redacted_dict[key] = "[REDACTED]"
                count += 1
                continue
            item_value, item_count = _redact_value(item)
            redacted_dict[key] = item_value
            count += item_count
        return redacted_dict, count
    return value, 0


def _canonical_digest(value: Any) -> str:
    return process_digest(value)


def _resolved_executable(value: str) -> Path:
    requested = Path(value).expanduser()
    if requested.is_absolute() or "/" in value:
        return requested.resolve()
    resolved = shutil.which(value)
    return Path(resolved).resolve() if resolved else requested


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _trusted_cache_parent() -> Path:
    candidates = [Path("/var/tmp"), Path("/tmp")]
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            info = resolved.stat()
            if (
                resolved == candidate
                and stat.S_ISDIR(info.st_mode)
                and info.st_uid == 0
                and info.st_mode & stat.S_ISVTX
                and os.access(resolved, os.W_OK | os.X_OK)
            ):
                return resolved
        except OSError:
            continue
    raise AppServerError(
        "no trusted external temporary directory is available",
        code="NATIVE_DRIVER_CACHE_ROOT_UNAVAILABLE",
    )


def probe_app_server(
    codex_bin: str = "codex", *, strict_protocol: bool = False,
    command_timeout: float | None = None,
) -> AppServerCapability:
    version = subprocess.run(
        [codex_bin, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=command_timeout,
    )
    if version.returncode != 0 or not version.stdout.strip():
        raise AppServerError(
            version.stderr.strip() or "Codex version probe failed",
            code="NATIVE_DRIVER_CODEX_UNAVAILABLE",
        )
    with tempfile.TemporaryDirectory(prefix="codex-native-driver-schema-") as raw:
        generated = subprocess.run(
            [codex_bin, "app-server", "generate-json-schema", "--out", raw],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=command_timeout,
        )
        schema_path = Path(raw) / "codex_app_server_protocol.schemas.json"
        if generated.returncode != 0 or not schema_path.is_file():
            raise AppServerError(
                generated.stderr.strip() or "Codex App Server schema generation failed",
                code="NATIVE_DRIVER_PROTOCOL_UNAVAILABLE",
            )
        content = schema_path.read_bytes()
        text = content.decode("utf-8")
        missing = [token for token in REQUIRED_PROTOCOL_TOKENS if token not in text]
        if missing:
            raise AppServerError(
                "Codex App Server lacks required Native Driver capabilities",
                code="NATIVE_DRIVER_PROTOCOL_INCOMPATIBLE",
                details={"missing": missing},
            )
        canary_digest: str | None = None
        last_error: AppServerError | None = None
        for attempt in range(1, PROTOCOL_CANARY_ATTEMPTS + 1):
            try:
                with tempfile.TemporaryDirectory(
                    prefix="codex-native-driver-canary-"
                ) as canary_cwd:
                    with AppServerTransport(
                        codex_bin=codex_bin, strict_protocol=strict_protocol
                    ) as transport:
                        thread_id = transport.start_thread(
                            cwd=canary_cwd,
                            developer_instructions="protocol canary",
                            sandbox="danger-full-access",
                        )
                        canary_digest = hashlib.sha256(
                            json.dumps(
                                {
                                    "sequence": [
                                        "initialize",
                                        "initialized",
                                        "thread/start",
                                    ],
                                    "thread_id_observed": bool(thread_id),
                                },
                                sort_keys=True,
                            ).encode()
                        ).hexdigest()
                    if not transport.cleanup_observation or not transport.cleanup_observation.get("process_group_gone"):
                        raise AppServerError(
                            "Protocol canary cleanup could not be verified",
                            code="NATIVE_APP_SERVER_CLEANUP_UNVERIFIED",
                        )
                break
            except AppServerError as exc:
                last_error = exc
                retryable = exc.code in {
                    "NATIVE_APP_SERVER_TIMEOUT",
                    "NATIVE_APP_SERVER_DISCONNECTED",
                }
                if not retryable or attempt == PROTOCOL_CANARY_ATTEMPTS:
                    code = (
                        "NATIVE_DRIVER_PROTOCOL_UNAVAILABLE"
                        if retryable
                        else "NATIVE_DRIVER_PROTOCOL_INCOMPATIBLE"
                    )
                    raise AppServerError(
                        "Codex App Server protocol canary failed",
                        code=code,
                        details={
                            "canary_code": exc.code,
                            "canary_details": exc.details,
                            "attempts": attempt,
                        },
                    ) from exc
                time.sleep(PROTOCOL_CANARY_RETRY_DELAY_SECONDS)
        if canary_digest is None and last_error is not None:
            raise last_error
        return AppServerCapability(
            runtime_version=version.stdout.strip(),
            protocol_schema_digest=hashlib.sha256(content).hexdigest(),
            thread_compaction='"thread/compact/start"' in text,
            protocol_canary_digest=canary_digest,
            executable_identity={
                "requested": codex_bin,
                "resolved": str(_resolved_executable(codex_bin)),
                "sha256": _file_sha256(_resolved_executable(codex_bin)),
                "runtime_version": version.stdout.strip(),
                "protocol_schema_digest": hashlib.sha256(content).hexdigest(),
                "protocol_canary_digest": canary_digest,
            },
        )


class AppServerTransport:
    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        strict_protocol: bool = False,
        executable_identity: dict[str, Any] | None = None,
        turn_idle_timeout: float = TURN_IDLE_TIMEOUT_SECONDS,
        turn_total_timeout: float = TURN_TOTAL_TIMEOUT_SECONDS,
        compaction_total_timeout: float = COMPACTION_TOTAL_TIMEOUT_SECONDS,
    ):
        self.codex_bin = codex_bin
        self.strict_protocol = strict_protocol
        self.executable_identity = executable_identity or self._fallback_executable_identity()
        self.turn_idle_timeout = float(turn_idle_timeout)
        self.turn_total_timeout = float(turn_total_timeout)
        self.compaction_total_timeout = float(compaction_total_timeout)
        self.process: subprocess.Popen[Any] | None = None
        self._next_id = 1
        self._deferred: list[dict[str, Any]] = []
        self._python_cache_root: Path | None = None
        self._read_buffer = b""
        self._wire_sequence = 0
        self._last_wire_event_digest: str | None = None
        self._process_identity: dict[str, Any] | None = None
        self._generation: str | None = None
        self._transport_state = "absent"
        self._cleanup_observation: dict[str, Any] | None = None
        self._stderr_buffer = bytearray()
        self._stderr_bytes = 0
        self._stderr_hasher = hashlib.sha256()
        self._stderr_truncated = False
        self._stderr_thread: threading.Thread | None = None
        self._stderr_drain_error: str | None = None
        self._reset_receive_diagnostics()

    def _reset_receive_diagnostics(self) -> None:
        self._diagnostic_epoch = time.perf_counter()
        self._diagnostic_utc = datetime.now(timezone.utc).isoformat()
        self._receive_events: deque[dict[str, Any]] = deque(maxlen=128)
        self._receive_event_count = 0
        self._received_bytes = 0
        self._pre_cleanup_snapshot: dict[str, Any] | None = None

    def _observe(self, kind: str, **fields: Any) -> None:
        # Metadata only: callers must never pass protocol bodies or tool text.
        self._receive_event_count += 1
        self._receive_events.append({
            "kind": kind, "offset_seconds": time.perf_counter() - self._diagnostic_epoch,
            **fields,
        })

    @staticmethod
    def _metadata(value: Any) -> str | int | None:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            return _redact_text(value)[0][:128]
        return None

    def receive_snapshot(self) -> dict[str, Any]:
        return {
            "utc_anchor": self._diagnostic_utc,
            "offset_seconds": time.perf_counter() - self._diagnostic_epoch,
            "received_bytes": self._received_bytes,
            "wire": self.wire_snapshot(),
            "partial_bytes": len(self._read_buffer),
            "deferred_messages": len(self._deferred),
            "partial_sha256": hashlib.sha256(self._read_buffer).hexdigest(),
            "event_count": self._receive_event_count,
            "dropped_events": max(0, self._receive_event_count - len(self._receive_events)),
            "events": [dict(event) for event in self._receive_events],
        }

    def _fallback_executable_identity(self) -> dict[str, Any]:
        resolved = _resolved_executable(self.codex_bin)
        try:
            executable_sha = _file_sha256(resolved)
        except OSError:
            executable_sha = "0" * 64
        return {
            "requested": self.codex_bin,
            "resolved": str(resolved),
            "sha256": executable_sha,
            "runtime_version": "unknown",
            "protocol_schema_digest": "0" * 64,
            "protocol_canary_digest": None,
        }

    def __enter__(self) -> "AppServerTransport":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def generation(self) -> str | None:
        return self._generation

    @property
    def cleanup_observation(self) -> dict[str, Any] | None:
        return dict(self._cleanup_observation) if self._cleanup_observation else None

    def runtime_snapshot(self) -> dict[str, Any]:
        return {
            "contract_version": 2,
            "generation": self._generation or "unbound",
            "state": self._transport_state,
            "executable_identity": dict(self.executable_identity),
            "process_identity": (
                dict(self._process_identity)
                if self._process_identity is not None
                else None
            ),
        }

    def wire_snapshot(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "sequence": self._wire_sequence,
            "event_digest": self._last_wire_event_digest,
        }

    def diagnostic_receipt(
        self,
        *,
        failure_code: str,
        turn_error: Any = None,
    ) -> dict[str, Any]:
        summary, stderr_redactions = _redact_text(
            bytes(self._stderr_buffer).decode("utf-8", errors="replace")
        )
        redacted_error, error_redactions = _redact_value(turn_error)
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "failure_code": failure_code,
            "transport_generation": self._generation,
            "stderr_bytes": self._stderr_bytes,
            "stderr_sha256": self._stderr_hasher.hexdigest(),
            "stderr_summary": summary[:STDERR_CAPTURE_LIMIT_BYTES],
            "stderr_truncated": self._stderr_truncated,
            "redaction_count": stderr_redactions + error_redactions,
            "turn_error": redacted_error,
            "cleanup_observation": self.cleanup_observation,
            "receive": self._pre_cleanup_snapshot or self.receive_snapshot(),
            "lifecycle_tail": [dict(event) for event in self._receive_events
                               if event["kind"] in {"close_started", "close_completed"}],
        }
        receipt["receipt_digest"] = _canonical_digest(receipt)
        return receipt

    def _reset_stderr_capture(self) -> None:
        self._stderr_buffer = bytearray()
        self._stderr_bytes = 0
        self._stderr_hasher = hashlib.sha256()
        self._stderr_truncated = False
        self._stderr_thread = None
        self._stderr_drain_error = None

    def _drain_stderr(self, stream: IO[Any]) -> None:
        try:
            while True:
                chunk = stream.read(STDERR_READ_CHUNK_BYTES)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                self._stderr_bytes += len(chunk)
                self._stderr_hasher.update(chunk)
                remaining = STDERR_CAPTURE_LIMIT_BYTES - len(self._stderr_buffer)
                if remaining > 0:
                    self._stderr_buffer.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    self._stderr_truncated = True
        except (OSError, ValueError) as exc:
            self._stderr_drain_error = str(exc)

    def start(self) -> None:
        if self.process is not None:
            return
        self._transport_state = "spawning"
        self._deferred.clear()
        self._read_buffer = b""
        self._wire_sequence = 0
        self._last_wire_event_digest = None
        self._cleanup_observation = None
        self._reset_stderr_capture()
        self._reset_receive_diagnostics()
        cache_parent = _trusted_cache_parent()
        cache_root = Path(
            tempfile.mkdtemp(
                prefix="codex-native-driver-pycache-",
                dir=cache_parent,
            )
        )
        environment = os.environ.copy()
        environment["PYTHONPYCACHEPREFIX"] = str(cache_root)
        environment["TMPDIR"] = str(cache_parent)
        environment.pop("TEMP", None)
        environment.pop("TMP", None)
        self._python_cache_root = cache_root
        try:
            self.process = subprocess.Popen(
                [self.codex_bin, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                env=environment,
                process_group=0,
            )
            if self.process.stderr is not None:
                self._stderr_thread = threading.Thread(
                    target=self._drain_stderr,
                    args=(self.process.stderr,),
                    name="codex-app-server-stderr",
                    daemon=True,
                )
                self._stderr_thread.start()
            try:
                proc = capture_process_identity(
                    self.process,
                    argv=[self.codex_bin, "app-server", "--stdio"],
                    executable_identity=self.executable_identity,
                )
            except RuntimeError as exc:
                raise AppServerError(
                    "App Server process identity could not be observed",
                    code="NATIVE_APP_SERVER_PROCESS_IDENTITY_MISSING",
                ) from exc
            self._process_identity = proc
            self._generation = _canonical_digest(
                {
                    "executable_identity": self.executable_identity,
                    "process_identity": proc,
                }
            )
            self._transport_state = "initializing"
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "cc_builder_loop_native_driver",
                        "title": "CC Builder Loop Native Driver",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            )
            self._send({"method": "initialized", "params": {}})
            self._transport_state = "ready"
        except Exception:
            self.close()
            raise

    def close(self) -> dict[str, Any] | None:
        process = self.process
        self.process = None
        try:
            if process is None:
                return self._cleanup_observation
            try:
                self._pre_cleanup_snapshot = self.receive_snapshot()
            except Exception as exc:
                # Observability must never prevent process-group cleanup.
                self._pre_cleanup_snapshot = {"diagnostic_error": type(exc).__name__}
            self._observe("close_started")
            self._transport_state = "draining"
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if self._process_identity is None:
                self._process_identity = capture_process_identity(
                    process,
                    argv=[self.codex_bin, "app-server", "--stdio"],
                    executable_identity=self.executable_identity,
                )
            cleanup = reap_process_group(
                process,
                process_identity=self._process_identity,
                grace_seconds=PROCESS_CLEANUP_GRACE_SECONDS,
            )
            if process.stdout:
                try:
                    process.stdout.close()
                except OSError:
                    pass
            if process.stderr:
                try:
                    process.stderr.close()
                except OSError:
                    pass
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1.0)
                self._stderr_thread = None
            group_gone = bool(cleanup["process_group_gone"])
            self._transport_state = cleanup["state"]
            self._cleanup_observation = {
                "generation": self._generation,
                "pid": process.pid,
                "pgid": self._process_identity["pgid"],
                "term_attempt": cleanup["term_attempt"],
                "kill_attempt": cleanup["kill_attempt"],
                "returncode": cleanup["returncode"],
                "process_group_gone": group_gone,
                "state": self._transport_state,
            }
            self._observe("close_completed", process_group_gone=group_gone)
            self._deferred.clear()
            self._read_buffer = b""
            return dict(self._cleanup_observation)
        finally:
            cache_root = self._python_cache_root
            self._python_cache_root = None
            if cache_root is not None:
                shutil.rmtree(cache_root, ignore_errors=True)

    def start_thread(
        self,
        *,
        cwd: str,
        developer_instructions: str,
        sandbox: str,
    ) -> str:
        result = self._request(
            "thread/start",
            {
                "cwd": cwd,
                "developerInstructions": developer_instructions,
                "approvalPolicy": "never",
                "sandbox": sandbox,
            },
        )
        thread = result.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("thread/start returned no identity", code="NATIVE_THREAD_ID_MISSING")
        return thread_id

    def resume_thread(
        self,
        *,
        thread_id: str,
        cwd: str,
        developer_instructions: str,
        sandbox: str,
    ) -> None:
        result = self._request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": cwd,
                "developerInstructions": developer_instructions,
                "approvalPolicy": "never",
                "sandbox": sandbox,
            },
        )
        actual = result.get("thread", {}).get("id")
        if actual != thread_id:
            raise AppServerError("thread/resume identity mismatch", code="NATIVE_THREAD_ID_MISMATCH")

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        return self._request("thread/read", {"threadId": thread_id, "includeTurns": True}).get(
            "thread", {}
        )

    def run_turn(
        self,
        *,
        thread_id: str,
        prompt: str,
        output_schema: dict[str, Any],
        action_id: str,
        timeout_profile: TurnTimeoutProfile | None = None,
        cwd: str | None = None,
        sandbox_policy: dict[str, Any] | None = None,
        on_started: Callable[[str], None] | None = None,
    ) -> TurnResult:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "outputSchema": output_schema,
            "clientUserMessageId": action_id,
            "approvalPolicy": "never",
        }
        if cwd is not None:
            params["cwd"] = cwd
        if sandbox_policy is not None:
            params["sandboxPolicy"] = sandbox_policy
        started = self._request("turn/start", params)
        turn = started.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerError("turn/start returned no identity", code="NATIVE_TURN_ID_MISSING")
        if on_started is not None:
            on_started(turn_id)
        return self.wait_turn(
            thread_id=thread_id,
            turn_id=turn_id,
            timeout_profile=timeout_profile,
        )

    def wait_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        timeout_profile: TurnTimeoutProfile | None = None,
    ) -> TurnResult:
        profile = timeout_profile or TurnTimeoutProfile(
            turn_idle_seconds=self.turn_idle_timeout,
            turn_total_seconds=self.turn_total_timeout,
            compaction_total_seconds=self.compaction_total_timeout,
        )
        self._observe("wait_turn", thread_id=self._metadata(thread_id),
                      turn_id=self._metadata(turn_id), **profile.as_dict())
        last_text = ""
        deadline = time.monotonic() + profile.turn_total_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._observe("turn_total_timeout", remaining_total_seconds=remaining)
                raise AppServerError(
                    "Codex App Server turn total timeout",
                    code="NATIVE_APP_SERVER_TURN_TIMEOUT",
                    details={
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "timeout_seconds": profile.turn_total_seconds,
                        "timeout_profile_digest": profile.profile_digest,
                    },
                )
            try:
                message = self._next_message(
                    timeout=min(profile.turn_idle_seconds, remaining)
                )
            except AppServerError as exc:
                remaining_total = deadline - time.monotonic()
                self._observe("turn_wait_failed", code=exc.code,
                              remaining_total_seconds=remaining_total,
                              idle_seconds=profile.turn_idle_seconds)
                if (
                    exc.code == "NATIVE_APP_SERVER_TIMEOUT"
                    and remaining_total <= 0
                ):
                    raise AppServerError(
                        "Codex App Server turn total timeout",
                        code="NATIVE_APP_SERVER_TURN_TIMEOUT",
                        details={
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "timeout_seconds": profile.turn_total_seconds,
                            "timeout_profile_digest": profile.profile_digest,
                        },
                    ) from exc
                raise
            if "id" in message and "method" in message:
                self._answer_server_request(message)
                continue
            method = message.get("method")
            params_value = message.get("params", {})
            if method == "item/completed" and params_value.get("threadId") == thread_id:
                item = params_value.get("item", {})
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    last_text = item["text"]
            if method == "turn/completed" and params_value.get("threadId") == thread_id:
                turn = params_value.get("turn", {})
                if turn.get("id") != turn_id:
                    continue
                for item in turn.get("items", []):
                    if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                        last_text = item["text"]
                return TurnResult(
                    turn_id=turn_id,
                    status=str(turn.get("status", "unknown")),
                    text=last_text,
                    error=turn.get("error"),
                )

    def compact_thread(self, thread_id: str) -> ThreadCompactionResult:
        self._request("thread/compact/start", {"threadId": thread_id})
        compaction_turn_id: str | None = None
        compaction_item_id: str | None = None
        deadline = time.monotonic() + self.compaction_total_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError(
                    "Codex App Server compaction total timeout",
                    code="NATIVE_APP_SERVER_COMPACTION_TIMEOUT",
                    details={
                        "thread_id": thread_id,
                        "timeout_seconds": self.compaction_total_timeout,
                    },
                )
            try:
                message = self._next_message(
                    timeout=min(self.turn_idle_timeout, remaining)
                )
            except AppServerError as exc:
                remaining_total = deadline - time.monotonic()
                self._observe("compaction_wait_failed", code=exc.code,
                              remaining_total_seconds=remaining_total,
                              idle_seconds=self.turn_idle_timeout)
                if (
                    exc.code == "NATIVE_APP_SERVER_TIMEOUT"
                    and remaining_total <= 0
                ):
                    raise AppServerError(
                        "Codex App Server compaction total timeout",
                        code="NATIVE_APP_SERVER_COMPACTION_TIMEOUT",
                        details={
                            "thread_id": thread_id,
                            "timeout_seconds": self.compaction_total_timeout,
                        },
                    ) from exc
                raise
            if "id" in message and "method" in message:
                self._answer_server_request(message)
                continue
            method = message.get("method")
            params = message.get("params", {})
            if not isinstance(params, dict) or params.get("threadId") != thread_id:
                continue
            if method in {"item/started", "item/completed"}:
                item = params.get("item", {})
                if isinstance(item, dict) and item.get("type") == "contextCompaction":
                    turn_id = params.get("turnId")
                    item_id = item.get("id")
                    if not isinstance(turn_id, str) or not isinstance(item_id, str):
                        raise AppServerError(
                            "thread compaction item lacks identity",
                            code="NATIVE_THREAD_COMPACTION_IDENTITY_MISSING",
                        )
                    if compaction_turn_id not in {None, turn_id} or compaction_item_id not in {
                        None,
                        item_id,
                    }:
                        raise AppServerError(
                            "thread compaction identity changed",
                            code="NATIVE_THREAD_COMPACTION_IDENTITY_DRIFT",
                        )
                    compaction_turn_id = turn_id
                    compaction_item_id = item_id
            if method != "turn/completed":
                continue
            turn = params.get("turn", {})
            if not isinstance(turn, dict) or turn.get("id") != compaction_turn_id:
                continue
            if turn.get("status") != "completed" or compaction_item_id is None:
                raise AppServerError(
                    "thread compaction did not complete",
                    code="NATIVE_THREAD_COMPACTION_FAILED",
                    details={
                        "turn_id": turn.get("id"),
                        "status": turn.get("status"),
                        "error": turn.get("error"),
                    },
                )
            return ThreadCompactionResult(
                turn_id=compaction_turn_id,
                item_id=compaction_item_id,
            )

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._observe("request_started", method=self._metadata(method), request_id=request_id)
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "method": method, "id": request_id, "params": params})
        timeout = INITIALIZE_TIMEOUT_SECONDS if method == "initialize" else REQUEST_TIMEOUT_SECONDS
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._observe("request_timeout", method=self._metadata(method), timeout_seconds=timeout)
                raise AppServerError("Codex App Server request timed out",
                                     code="NATIVE_APP_SERVER_TIMEOUT",
                                     details={"method": method, "timeout_seconds": timeout})
            message = self._read(timeout=remaining)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    error = message["error"]
                    if not isinstance(error, dict):
                        raise AppServerError("invalid App Server error response", code="NATIVE_APP_SERVER_PROTOCOL_ERROR")
                    raise AppServerError(
                        str(error.get("message", "App Server request failed")),
                        code="NATIVE_APP_SERVER_REQUEST_FAILED",
                        details={"method": method, "error": error},
                    )
                result = message.get("result", {})
                return result if isinstance(result, dict) else {"value": result}
            if "id" in message and "method" in message:
                self._answer_server_request(message)
            else:
                self._deferred.append(message)

    def _send(self, value: dict[str, Any]) -> None:
        process = self.process
        stream: IO[Any] | None = process.stdin if process is not None else None
        if stream is None:
            raise AppServerError("App Server is not running", code="NATIVE_APP_SERVER_NOT_RUNNING")
        try:
            payload = dict(value)
            payload.setdefault("jsonrpc", "2.0")
            stream.write(
                (
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
                ).encode("utf-8")
            )
            stream.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerError(str(exc), code="NATIVE_APP_SERVER_DISCONNECTED") from exc

    def _read(self, timeout: float = 10.0) -> dict[str, Any]:
        process = self.process
        stream: IO[Any] | None = process.stdout if process is not None else None
        if stream is None:
            raise AppServerError("App Server is not running", code="NATIVE_APP_SERVER_NOT_RUNNING")
        self._observe("read_started", timeout_seconds=timeout)
        deadline = time.monotonic() + timeout
        while b"\n" not in self._read_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._observe("read_timeout", timeout_seconds=timeout, partial_bytes=len(self._read_buffer))
                raise AppServerError(
                    "Codex App Server read timed out",
                    code="NATIVE_APP_SERVER_TIMEOUT",
                    details={"timeout_seconds": timeout},
                )
            try:
                ready, _, _ = select.select([stream.fileno()], [], [], remaining)
            except (OSError, ValueError):
                ready = [stream]
            if not ready:
                self._observe("select_timeout", timeout_seconds=timeout, partial_bytes=len(self._read_buffer))
                raise AppServerError(
                    "Codex App Server read timed out",
                    code="NATIVE_APP_SERVER_TIMEOUT",
                    details={"timeout_seconds": timeout},
                )
            try:
                chunk = os.read(stream.fileno(), 65536)
            except OSError as exc:
                raise AppServerError(
                    str(exc), code="NATIVE_APP_SERVER_DISCONNECTED"
                ) from exc
            if not chunk:
                self._observe("stdout_eof", partial_bytes=len(self._read_buffer))
                code = process.poll() if process is not None else None
                raise AppServerError(
                    "Codex App Server closed its output",
                    code="NATIVE_APP_SERVER_DISCONNECTED",
                    details={
                        "returncode": code,
                        "partial_bytes": len(self._read_buffer),
                        "partial_digest": hashlib.sha256(
                            self._read_buffer
                        ).hexdigest(),
                    },
                )
            self._received_bytes += len(chunk)
            self._observe("bytes_received", byte_count=len(chunk))
            self._read_buffer += chunk
        line, self._read_buffer = self._read_buffer.split(b"\n", 1)
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerError("invalid App Server JSON", code="NATIVE_APP_SERVER_PROTOCOL_ERROR") from exc
        if not isinstance(value, dict):
            raise AppServerError("invalid App Server message", code="NATIVE_APP_SERVER_PROTOCOL_ERROR")
        params = value.get("params")
        params = params if isinstance(params, dict) else {}
        turn = params.get("turn")
        turn = turn if isinstance(turn, dict) else {}
        item = params.get("item")
        item = item if isinstance(item, dict) else {}
        self._observe("message_parsed", byte_count=len(line),
                      sha256=hashlib.sha256(line).hexdigest(),
                      method=self._metadata(value.get("method")),
                      request_id=self._metadata(value.get("id")),
                      thread_id=self._metadata(params.get("threadId")),
                      turn_id=self._metadata(params.get("turnId", turn.get("id"))),
                      item_type=self._metadata(item.get("type")),
                      sequence=self._wire_sequence + 1)
        self._wire_sequence += 1
        self._last_wire_event_digest = _canonical_digest(
            {
                "generation": self._generation,
                "sequence": self._wire_sequence,
                "message": value,
            }
        )
        if self.strict_protocol:
            jsonrpc = value.get("jsonrpc")
            if jsonrpc is not None and jsonrpc != "2.0":
                raise AppServerError(
                    "App Server message has an invalid JSON-RPC version",
                    code="NATIVE_DRIVER_PROTOCOL_INCOMPATIBLE",
                    details={"message_digest": hashlib.sha256(line).hexdigest()},
                )
            method = value.get("method")
            has_method = isinstance(method, str) and bool(method)
            has_id = "id" in value
            has_result = "result" in value
            has_error = "error" in value
            valid_response = has_id and (has_result ^ has_error) and not has_method
            valid_request = has_method and (not has_id or "params" in value)
            if not valid_response and not valid_request:
                raise AppServerError(
                    "App Server message is not a valid request, notification, or response",
                    code="NATIVE_DRIVER_PROTOCOL_INCOMPATIBLE",
                    details={"message_digest": hashlib.sha256(line).hexdigest()},
                )
        return value

    def _next_message(self, timeout: float = 10.0) -> dict[str, Any]:
        if self._deferred:
            return self._deferred.pop(0)
        return self._read(timeout=timeout)

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        self._observe("server_request", method=self._metadata(method),
                      request_id=self._metadata(message.get("id")),
                      decision="decline" if "Approval" in method or "requestApproval" in method else "unsupported")
        if "Approval" in method or "requestApproval" in method:
            self._send({"id": message["id"], "result": {"decision": "decline"}})
            return
        self._send(
            {
                "id": message["id"],
                "error": {"code": -32601, "message": "Native Driver does not service this request"},
            }
        )


class _ReadinessTransport(AppServerTransport):
    """Reject tool activity in a diagnostic-only model turn, including streamed items."""

    @staticmethod
    def check_items(items: Any) -> None:
        if not isinstance(items, list):
            raise AppServerError("Probe history has no item list", code="NATIVE_PROBE_HISTORY_INVALID")
        for item in items:
            if not isinstance(item, dict) or item.get("type") not in {
                "userMessage", "agentMessage", "reasoning",
            }:
                raise AppServerError(
                    "Probe observed unexpected activity; no readiness claim is possible",
                    code="NATIVE_PROBE_UNEXPECTED_ACTIVITY",
                )

    def _read(self, timeout: float = 10.0) -> dict[str, Any]:
        message = super()._read(timeout)
        params = message.get("params", {})
        if message.get("method") in {"item/started", "item/completed", "turn/completed"}:
            if not isinstance(params, dict):
                raise AppServerError("Invalid probe event parameters", code="NATIVE_PROBE_HISTORY_INVALID")
            if message.get("method") in {"item/started", "item/completed"}:
                self.check_items([params.get("item")])
            if message.get("method") == "turn/completed":
                turn = params.get("turn", {})
                if not isinstance(turn, dict):
                    raise AppServerError("Invalid probe completion event", code="NATIVE_PROBE_HISTORY_INVALID")
                self.check_items(turn.get("items", []))
        return message

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        super()._answer_server_request(message)
        raise AppServerError(
            "Probe received a server request requiring interaction",
            code="NATIVE_PROBE_INTERACTION_REQUIRED",
        )


def probe_model_request(codex_bin: str = "codex", *, timeout_seconds: float = 120.0) -> dict[str, Any]:
    """Explicit diagnostic, never a delivery gate or a run/dispatch mutation.

    Returns schema_version=1, status READY/NEEDS_USER, independent protocol/model
    statuses, executable identity, thread/turn identity, elapsed time and cleanup.
    READY requires a completed model turn, exact JSON {"ready": true}, matching
    readback without tool activity, and a reaped child process group. Proxy values
    are never returned. The turn deadline excludes bounded protocol/setup work.
    """
    started = time.monotonic()
    payload: dict[str, Any] = {
        "schema_version": 1, "status": "NEEDS_USER", "code": "NATIVE_PROBE_NOT_READY",
        "protocol_status": "NOT_CHECKED", "model_request_status": "NOT_CHECKED",
        "turn_timeout_seconds": timeout_seconds,
        "proxy_environment_keys": sorted(
            key for key in os.environ
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
            and os.environ[key]
        ),
    }
    transport: _ReadinessTransport | None = None
    try:
        capability = probe_app_server(codex_bin, strict_protocol=True, command_timeout=30.0)
        payload.update(protocol_status="READY", runtime_version=capability.runtime_version,
                       executable_identity=capability.executable_identity)
        transport = _ReadinessTransport(codex_bin=codex_bin, strict_protocol=True,
                                       executable_identity=capability.executable_identity)
        # Use only a disposable directory. This is an observation guard, not a
        # general filesystem sandbox; a deviating model must never yield READY.
        with tempfile.TemporaryDirectory(prefix="codex-native-readiness-") as cwd, transport:
            prompt = 'Diagnostic only. Do not call tools or read/write files. Return exactly {"ready":true}.'
            thread_id = transport.start_thread(cwd=cwd, developer_instructions=prompt,
                                               sandbox="danger-full-access")
            payload["thread_id"] = thread_id
            payload["model_request_status"] = "RUNNING"
            result = transport.run_turn(
                thread_id=thread_id, prompt=prompt,
                output_schema={"type": "object", "properties": {"ready": {"type": "boolean"}},
                               "required": ["ready"], "additionalProperties": False},
                action_id="native-readiness", cwd=cwd,
                sandbox_policy={"type": "dangerFullAccess"},
                timeout_profile=TurnTimeoutProfile(timeout_seconds, timeout_seconds, timeout_seconds),
                on_started=lambda turn_id: payload.update(turn_id=turn_id),
            )
            if result.status != "completed" or result.error is not None:
                raise AppServerError("Real model request did not complete", code="NATIVE_PROBE_MODEL_REQUEST_FAILED",
                                     details={"turn_status": result.status, "turn_error": result.error})
            history = transport.read_thread(thread_id)
            if not isinstance(history, dict):
                raise AppServerError("Invalid probe thread readback", code="NATIVE_PROBE_HISTORY_INVALID")
            turns = history.get("turns", [])
            tail = turns[-1] if isinstance(turns, list) and turns else None
            if (history.get("id") != thread_id or not isinstance(tail, dict)
                    or tail.get("id") != result.turn_id or tail.get("status") != "completed"
                    or tail.get("error") is not None):
                raise AppServerError("Probe turn readback mismatch", code="NATIVE_PROBE_HISTORY_INVALID")
            transport.check_items(tail.get("items"))
            texts = [item.get("text") for item in tail["items"] if item.get("type") == "agentMessage"]
            if not texts or texts[-1] != result.text:
                raise AppServerError("Probe response readback mismatch", code="NATIVE_PROBE_HISTORY_INVALID")
            # Exact scalar type: JSON 1 must not compare equal to boolean true.
            try:
                pairs = json.loads(result.text, object_pairs_hook=list)
            except (TypeError, ValueError):
                pairs = None
            if not (isinstance(pairs, list) and len(pairs) == 1
                    and isinstance(pairs[0], tuple) and pairs[0][0] == "ready"
                    and pairs[0][1] is True):
                raise AppServerError("Probe returned an unexpected response", code="NATIVE_PROBE_RESPONSE_INVALID")
            payload.update(status="READY", code="NATIVE_PROBE_READY", model_request_status="READY")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        if payload["protocol_status"] != "READY":
            payload["protocol_status"] = "FAILED"
        else:
            payload["model_request_status"] = "FAILED"
        payload.update(status="NEEDS_USER", code=getattr(exc, "code", "NATIVE_PROBE_EXECUTION_FAILED"),
                       error={"message": str(exc), "details": getattr(exc, "details", {})})
    finally:
        if transport is not None:
            try:
                cleanup = transport.close()
                payload["cleanup"] = cleanup
                if not cleanup or not cleanup.get("process_group_gone"):
                    payload.update(status="NEEDS_USER", code="NATIVE_PROBE_CLEANUP_UNVERIFIED")
            except (OSError, RuntimeError) as exc:
                payload.update(status="NEEDS_USER", code="NATIVE_PROBE_CLEANUP_FAILED",
                               cleanup_error=str(exc))
            payload["diagnostic"] = transport.diagnostic_receipt(failure_code=payload["code"],
                                                               turn_error=payload.get("error"))
        payload["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return _redact_value(payload)[0]
