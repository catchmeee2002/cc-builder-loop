"""B4：loop.yml 的 concurrency.gates 允许不同门禁并行。

given `concurrency: {gates: 2}` 且 preflight 正持锁 / when 进入 machine 的 gate_lock / then
立即拿到锁，不再排队等 preflight 释放。起点 gate_lock 无条件 `_wait_released(preflight)`，
主场景要等子进程释放（约 2s），0.8s 的上限断言在 call 阶段失败（baseline-red）。

边界（gates 缺省或 1 仍等待；gates 为 0 / 负数报 CONFIG_CONCURRENCY_INVALID）与不变量
（同一门禁重复进入仍 GATE_BUSY）在起点上不成立或本来就成立，作为守卫不列入 proof group。
"""

from __future__ import annotations

import time

import pytest

from builder_loop import evidence
from builder_loop import ledger as L
from builder_loop.config import load_loop_config
from builder_loop.errors import Problem

from keypath_helpers import hold_gate, loop_yml, release_hold, start_with_loop

HOLD_SECONDS = 2.0


def _run_with_gates(repo, cli, gates: int) -> dict:
    return start_with_loop(repo, cli, loop_yml(gates=gates))


# ---------------------------------------------------------------- 正向（baseline-red）


def test_b4_machine_gate_not_blocked_by_preflight_when_gates_two(repo, cli, tmp_path):
    ctx = _run_with_gates(repo, cli, 2)
    lg = L.load(ctx["ledger"])
    proc = hold_gate(lg, evidence.GATE_PREFLIGHT, HOLD_SECONDS, tmp_path)
    try:
        started_at = time.monotonic()
        with evidence.gate_lock(lg, evidence.GATE_MACHINE):
            pass
        elapsed = time.monotonic() - started_at
    finally:
        release_hold(proc)
    assert elapsed < 0.8, f"machine 门禁等了 {elapsed:.2f}s，说明仍在串行等 preflight"


# ---------------------------------------------------------------- 边界（守卫）


def test_b4_boundary_gates_one_still_waits_for_preflight(repo, cli, tmp_path):
    ctx = _run_with_gates(repo, cli, 1)
    lg = L.load(ctx["ledger"])
    proc = hold_gate(lg, evidence.GATE_PREFLIGHT, HOLD_SECONDS, tmp_path)
    try:
        started_at = time.monotonic()
        with evidence.gate_lock(lg, evidence.GATE_MACHINE):
            pass
        elapsed = time.monotonic() - started_at
    finally:
        release_hold(proc)
    assert elapsed >= 1.0, f"gates=1 时应当等 preflight 释放，实际 {elapsed:.2f}s"


@pytest.mark.parametrize("gates", [0, -1])
def test_b4_boundary_invalid_gates_value(repo, cli, gates):
    (repo.root / ".claude" / "loop.yml").write_text(loop_yml(gates=gates), encoding="utf-8")
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_CONCURRENCY_INVALID", ei.value


# ---------------------------------------------------------------- 不变量（守卫）


def test_b4_invariant_same_gate_twice_is_gate_busy(repo, cli):
    ctx = _run_with_gates(repo, cli, 2)
    lg = L.load(ctx["ledger"])
    with evidence.gate_lock(lg, evidence.GATE_MACHINE):
        with pytest.raises(Problem) as ei:
            with evidence.gate_lock(lg, evidence.GATE_MACHINE):
                pass
    assert ei.value.code == "GATE_BUSY", ei.value