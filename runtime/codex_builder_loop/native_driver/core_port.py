from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


class CorePortError(RuntimeError):
    def __init__(self, message: str, *, payload: dict[str, Any], returncode: int):
        super().__init__(message)
        self.payload = payload
        self.returncode = returncode
        self.code = str(payload.get("code", "CORE_PORT_ERROR"))
        self.status = str(payload.get("status", "FATAL"))


class CorePort:
    def __init__(self, *, command: Sequence[str] | None = None):
        if command is None:
            configured = os.environ.get("CODEX_BUILDER_LOOP_BIN")
            if configured:
                command = [configured]
            else:
                project_root = Path(__file__).resolve().parents[3]
                command = [sys.executable, str(project_root / "scripts" / "codex-builder-loop.py")]
        self.command = list(command)

    def call(self, *args: str, input_value: Any | None = None) -> dict[str, Any]:
        input_text = None
        if input_value is not None:
            input_text = json.dumps(input_value, ensure_ascii=False, separators=(",", ":"))
        result = subprocess.run(
            [*self.command, "assurance", "--experimental-v4", *args],
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CorePortError(
                result.stderr.strip() or result.stdout.strip() or "Core returned invalid JSON",
                payload={"status": "FATAL", "code": "CORE_PORT_INVALID_JSON"},
                returncode=result.returncode,
            ) from exc
        if result.returncode != 0:
            raise CorePortError(
                str(payload.get("message", "Core command failed")),
                payload=payload,
                returncode=result.returncode,
            )
        if not isinstance(payload, dict):
            raise CorePortError(
                "Core returned a non-object payload",
                payload={"status": "FATAL", "code": "CORE_PORT_INVALID_PAYLOAD"},
                returncode=result.returncode,
            )
        return payload

    def start(
        self,
        *,
        repo: Path,
        run_id: str,
        session_id: str,
        contract: dict[str, Any],
        runtime_version: str,
        protocol_schema_digest: str,
        protocol_canary_digest: str | None = None,
    ) -> dict[str, Any]:
        return self.call(
            "start",
            "--repo",
            str(repo),
            "--run",
            run_id,
            "--session-id",
            session_id,
            "--contract",
            "-",
            "--driver-kind",
            "native",
            "--driver-transport",
            "codex_app_server",
            "--driver-runtime-version",
            runtime_version,
            "--driver-protocol-schema-digest",
            protocol_schema_digest,
            *(
                ["--driver-protocol-canary-digest", protocol_canary_digest]
                if protocol_canary_digest
                else []
            ),
            input_value=contract,
        )
