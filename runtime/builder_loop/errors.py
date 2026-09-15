"""统一错误类型。退出码约定：
0 成功 / 1 判据为负（正常结果）/ 2 配置或用法错误（FATAL）/ 3 需用户决策。"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_NEGATIVE = 1
EXIT_FATAL = 2
EXIT_NEEDS_USER = 3


class Problem(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None, exit_code: int = EXIT_FATAL):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def to_json(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message, "details": self.details, "exit_code": self.exit_code}


def negative(code: str, message: str, **details: Any) -> Problem:
    return Problem(code, message, details=details, exit_code=EXIT_NEGATIVE)


def fatal(code: str, message: str, **details: Any) -> Problem:
    return Problem(code, message, details=details, exit_code=EXIT_FATAL)


def needs_user(code: str, message: str, **details: Any) -> Problem:
    return Problem(code, message, details=details, exit_code=EXIT_NEEDS_USER)
