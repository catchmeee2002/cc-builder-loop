from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import select
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, IO


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
    codex_bin: str = "codex", *, strict_protocol: bool = False
) -> AppServerCapability:
    version = subprocess.run(
        [codex_bin, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
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
        try:
            with tempfile.TemporaryDirectory(prefix="codex-native-driver-canary-") as canary_cwd:
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
                                "sequence": ["initialize", "initialized", "thread/start"],
                                "thread_id": thread_id,
                            },
                            sort_keys=True,
                        ).encode()
                    ).hexdigest()
        except AppServerError as exc:
            raise AppServerError(
                "Codex App Server protocol canary failed",
                code="NATIVE_DRIVER_PROTOCOL_INCOMPATIBLE",
                details={"canary_code": exc.code, "canary_details": exc.details},
            ) from exc
        return AppServerCapability(
            runtime_version=version.stdout.strip(),
            protocol_schema_digest=hashlib.sha256(content).hexdigest(),
            thread_compaction='"thread/compact/start"' in text,
            protocol_canary_digest=canary_digest,
        )


class AppServerTransport:
    def __init__(self, *, codex_bin: str = "codex", strict_protocol: bool = False):
        self.codex_bin = codex_bin
        self.strict_protocol = strict_protocol
        self.process: subprocess.Popen[Any] | None = None
        self._next_id = 1
        self._deferred: list[dict[str, Any]] = []
        self._python_cache_root: Path | None = None
        self._read_buffer = b""

    def __enter__(self) -> "AppServerTransport":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        if self.process is not None:
            return
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
                stderr=subprocess.DEVNULL,
                text=False,
                bufsize=0,
                env=environment,
            )
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
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        process = self.process
        self.process = None
        try:
            if process is None:
                return
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
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
        thread_id = result.get("thread", {}).get("id")
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
        turn_id = started.get("turn", {}).get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerError("turn/start returned no identity", code="NATIVE_TURN_ID_MISSING")
        if on_started is not None:
            on_started(turn_id)
        return self.wait_turn(thread_id=thread_id, turn_id=turn_id)

    def wait_turn(self, *, thread_id: str, turn_id: str) -> TurnResult:
        last_text = ""
        while True:
            message = self._next_message(timeout=30.0)
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
        while True:
            message = self._next_message(timeout=30.0)
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
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "method": method, "id": request_id, "params": params})
        while True:
            timeout = 5.0 if method == "initialize" else 10.0
            message = self._read(timeout=timeout)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    error = message["error"]
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
        if b"\n" in self._read_buffer:
            ready = []
        else:
            try:
                ready, _, _ = select.select([stream.fileno()], [], [], timeout)
            except (OSError, ValueError):
                ready = [stream]
        if not ready and b"\n" not in self._read_buffer:
            raise AppServerError(
                "Codex App Server read timed out",
                code="NATIVE_APP_SERVER_TIMEOUT",
                details={"timeout_seconds": timeout},
            )
        if ready:
            try:
                self._read_buffer += os.read(stream.fileno(), 65536)
            except OSError as exc:
                raise AppServerError(
                    str(exc), code="NATIVE_APP_SERVER_DISCONNECTED"
                ) from exc
        if b"\n" not in self._read_buffer:
            code = process.poll() if process is not None else None
            raise AppServerError(
                "Codex App Server closed its output",
                code="NATIVE_APP_SERVER_DISCONNECTED",
                details={"returncode": code},
            )
        line, self._read_buffer = self._read_buffer.split(b"\n", 1)
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerError("invalid App Server JSON", code="NATIVE_APP_SERVER_PROTOCOL_ERROR") from exc
        if not isinstance(value, dict):
            raise AppServerError("invalid App Server message", code="NATIVE_APP_SERVER_PROTOCOL_ERROR")
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
        if "Approval" in method or "requestApproval" in method:
            self._send({"id": message["id"], "result": {"decision": "decline"}})
            return
        self._send(
            {
                "id": message["id"],
                "error": {"code": -32601, "message": "Native Driver does not service this request"},
            }
        )
