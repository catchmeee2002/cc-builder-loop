from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


PROCESS_CLEANUP_GRACE_SECONDS = 5.0


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def read_proc_identity(pid: int) -> dict[str, Any] | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stream:
            raw = stream.read()
    except OSError:
        return None
    _, separator, tail = raw.rpartition(")")
    if not separator:
        return None
    fields = tail.strip().split()
    if len(fields) < 20:
        return None
    try:
        return {
            "pid": pid,
            "pgid": int(fields[2]),
            "starttime": fields[19],
            "parent_pid": int(fields[1]),
        }
    except ValueError:
        return None


def process_group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def capture_process_identity(
    process: subprocess.Popen[Any],
    *,
    argv: Sequence[str],
    executable_identity: Mapping[str, Any],
) -> dict[str, Any]:
    observed = read_proc_identity(process.pid)
    if observed is None:
        raise RuntimeError("process identity could not be observed")
    identity = dict(observed)
    identity.update(
        {
            "argv_digest": digest(list(argv)),
            "executable_identity_digest": digest(dict(executable_identity)),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "exited_at": None,
            "exit_code": None,
            "process_group_gone": None,
        }
    )
    return identity


def reap_process_group(
    process: subprocess.Popen[Any],
    *,
    process_identity: dict[str, Any],
    grace_seconds: float = PROCESS_CLEANUP_GRACE_SECONDS,
) -> dict[str, Any]:
    pgid = int(process_identity["pgid"])
    term_attempt = 0
    kill_attempt = 0
    group_gone = process_group_gone(pgid)
    if not group_gone:
        term_attempt = 1
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            kill_attempt = 1
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                pass
        group_gone = process_group_gone(pgid)
        if not group_gone:
            kill_attempt = max(kill_attempt, 1)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                pass
            group_gone = process_group_gone(pgid)
    gone = group_gone
    process_identity["exited_at"] = datetime.now(timezone.utc).isoformat()
    process_identity["exit_code"] = process.returncode
    process_identity["process_group_gone"] = gone
    return {
        "term_attempt": term_attempt,
        "kill_attempt": kill_attempt,
        "returncode": process.returncode,
        "process_group_gone": gone,
        "state": "cleaned" if gone else "cleanup_unknown",
    }


def run_owned_command(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
    executable_identity: Mapping[str, Any],
) -> dict[str, Any]:
    def text_value(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else ""

    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        process_group=0,
    )
    try:
        identity = capture_process_identity(
            process,
            argv=argv,
            executable_identity=executable_identity,
        )
    except RuntimeError:
        process.kill()
        process.wait(timeout=1)
        raise
    timed_out = False
    stdout = ""
    stderr = ""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        cleanup = reap_process_group(
            process,
            process_identity=identity,
        )
        try:
            trailing_stdout, trailing_stderr = process.communicate(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            trailing_stdout, trailing_stderr = "", ""
        stdout = text_value(trailing_stdout or exc.stdout)
        stderr = text_value(trailing_stderr or exc.stderr)
    else:
        cleanup = reap_process_group(
            process,
            process_identity=identity,
        )
        stdout = text_value(stdout)
        stderr = text_value(stderr)
    return {
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "process_identity": identity,
        "cleanup": cleanup,
    }
