from __future__ import annotations

from typing import Any, Mapping

from .app_server import AppServerError


RETRYABLE_TRANSPORT_FAILURES = frozenset(
    {
        "serverOverloaded",
        "responseStreamConnectionFailed",
        "responseStreamDisconnected",
        "responseTooManyFailedAttempts",
        "httpConnectionFailed",
        "authUnavailable",
    }
)


def _codex_error_info(error: Any) -> str:
    if not isinstance(error, Mapping):
        return "other"
    value = error.get("codexErrorInfo")
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping) and value:
        return str(next(iter(value)))
    return "other"


def _known_stream_disconnect(error: Any) -> bool:
    if not isinstance(error, Mapping):
        return False
    message = error.get("message")
    if not isinstance(message, str):
        return False
    normalized = " ".join(message.lower().split())
    return (
        "stream disconnected before completion" in normalized
        and "response.completed" in normalized
    )


def _known_auth_unavailable(error: Any) -> bool:
    if not isinstance(error, Mapping):
        return False
    message = error.get("message")
    if not isinstance(message, str):
        return False
    normalized = " ".join(message.lower().split())
    return all(
        marker in normalized
        for marker in ("http 503", "auth_unavailable", "no auth available")
    )


def classify_turn_failure(error: Any) -> str:
    code = _codex_error_info(error)
    if code == "other" and _known_stream_disconnect(error):
        return "responseStreamDisconnected"
    if code == "other" and _known_auth_unavailable(error):
        return "authUnavailable"
    return code


def classify_app_server_failure(error: AppServerError) -> str | None:
    if error.code == "NATIVE_APP_SERVER_DISCONNECTED":
        return "responseStreamDisconnected"
    return None


def is_missing_rollout_failure(error: AppServerError) -> bool:
    if error.code != "NATIVE_APP_SERVER_REQUEST_FAILED":
        return False
    details = error.details if isinstance(error.details, Mapping) else {}
    if details.get("method") != "thread/resume":
        return False
    nested = details.get("error")
    message = nested.get("message") if isinstance(nested, Mapping) else str(error)
    normalized = " ".join(str(message).lower().split())
    return "no rollout found for thread id" in normalized


def is_retryable_transport_failure(code: str) -> bool:
    return code in RETRYABLE_TRANSPORT_FAILURES
