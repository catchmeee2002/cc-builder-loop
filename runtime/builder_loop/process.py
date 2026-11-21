"""门禁命令的执行边界：一次调用独占一个进程组，返回前回收本次启动的子孙（#363）。

runtime 只清理自己以 `start_new_session` 建出的那个进程组：不按进程名、命令行或其他 run 的 pid
查杀（原则三）。超时判定与进程清理在同一次调用里闭环，后续 machine 不再与残留进程争 CPU。
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import IO


def run(argv: list[str], *, cwd: Path | str, env: dict[str, str], timeout: int, stdout: IO[bytes]) -> tuple[int, bool]:
    """跑一个门禁命令，返回 `(returncode, timed_out)`。

    timed_out 只报告「runtime 按 timeout 终止了它」，returncode 相应为 124（与 coreutils timeout
    同一约定），调用方据此记 FAIL / INCONCLUSIVE / 组超时。直接进程被信号带走时不改语义（负
    returncode 原样返回），同组子孙同样回收——它们不会再有人收尸。
    """
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=stdout,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    timed_out = False
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc.pid)
        proc.wait()
        rc = 124
    else:
        if rc < 0:
            _kill_group(proc.pid)
    return rc, timed_out


def _kill_group(pgid: int) -> None:
    """强制终止整个进程组；组已经不在了按已清理处理。"""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass