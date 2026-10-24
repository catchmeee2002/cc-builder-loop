"""Current caller identity supplied by the trusted Codex command environment.

This is an identity-consistency boundary, not OS-level authentication. Never
infer the caller from a ledger, a transcript, or an explicit CLI argument.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


SOURCE = "CODEX_THREAD_ID"
# Keep the transport identifier opaque; do not couple ownership to UUID versions.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class SessionIdentityError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def payload(self) -> dict[str, str]:
        return {"status": "NEEDS_USER", "code": self.code,
                "message": str(self), "source": SOURCE}


def current_session_id(
    expected: str | None = None, *, environ: Mapping[str, str] | None = None,
) -> str:
    """Read the host identity verbatim; an expected ID is only an assertion.

    Missing, empty or malformed host input cannot be repaired with ``expected``.
    No normalization is performed because owner continuity requires exact IDs.
    """
    value = (os.environ if environ is None else environ).get(SOURCE)
    if not value:
        raise SessionIdentityError(
            "NATIVE_SESSION_IDENTITY_MISSING",
            "Codex did not supply CODEX_THREAD_ID; invoke this operation from the owning Codex session.",
        )
    if not _IDENTIFIER.fullmatch(value):
        raise SessionIdentityError(
            "NATIVE_SESSION_IDENTITY_INVALID", "Codex supplied an invalid thread identifier.",
        )
    if expected is not None and value != expected:
        raise SessionIdentityError(
            "NATIVE_SESSION_IDENTITY_MISMATCH",
            "The current Codex session does not match the asserted session or recorded root owner.",
        )
    return value
