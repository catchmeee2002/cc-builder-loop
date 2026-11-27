"""B5：proof 的 ②③ 反例段按 concurrency.proof_groups 并行，候选段（①）仍串行。

given loop.yml `concurrency: {proof_groups: 2}`、proof_spec 有 3 个带 patch 的 mutation 组、
when `bl proof` 全部通过 / then groups 长度与顺序不变，且反例段的测试执行区间相互重叠，而候选段
不重叠。起点没有并发执行，反例段串行，重叠断言在 call 阶段失败（baseline-red）。

边界（proof_groups 缺省/1 行为不变；失败按最小组下标报告；组超时仍是超时类失败）与不变量
（候选段始终串行、失败聚合与串行一致）作为守卫留在同一文件，不列入 proof group。
"""

from __future__ import annotations

import time
from pathlib import Path

from conftest import (
    contract_with,
    implement_mul,
    mutation_patch,
    role_turn,
    write_patch_file,
    write_plan,
)
from keypath_helpers import loop_yml, start_with_loop, build_tester_payload


def _contract(n: int) -> dict:
    c = contract_with()
    c["mission"]["slug"] = f"keypath-groups-{n}"
    c["mission"]["objective"] = "Exercise proof group concurrency"
    c["mission"]["interfaces"] = []
    c["mission"]["behaviors"] = [
        {"id": f"B{i + 1}", "given": "g", "when": "w", "then": "t", "boundaries": [], "invariants": []}
        for i in range(n)
    ]
    return c


def _start_groups(repo, cli, n: int, proof_groups: int) -> tuple[dict, list[str]]:
    plan = repo.root / "plan-keypath.md"
    write_plan(repo.root, _contract(n), plan.name)
    ctx = start_with_loop(repo, cli, loop_yml(proof_groups=proof_groups), plan_path=plan)
    return ctx, [f"B{i + 1}" for i in range(n)]


def _body(tag: str, log: Path, *, check_mul: bool, sleep_when_mutated: float,
          sleep_always: float = 0.0) -> str:
    lines = [
        "import time",
        "",
        "from src.foo import mul",
        "",
        f"LOG = {str(log)!r}",
        f"TAG = {tag!r}",
        "",
        "",
        f"def test_{tag}():",
        "    start = time.monotonic()",
        "    ok = mul(3, 4) == 12",
    ]
    if check_mul:
        lines.append("    if not ok:")
        lines.append(f"        time.sleep({sleep_when_mutated})")
    if sleep_always:
        lines.append(f"    time.sleep({sleep_always})")
    lines += [
        "    end = time.monotonic()",
        '    with open(LOG, "a", encoding="utf-8") as fh:',
        '        fh.write("%s|%s|%.6f|%.6f\\n" % (TAG, "ok" if ok else "mutated", start, end))',
        "    assert ok" if check_mul else "    assert True",
    ]
    return "\n".join(lines) + "\n"


def _write_tests(ctx: dict, log: Path, n: int, *, slow_indices: set[int] | None = None,
                 ignore_mul: set[int] | None = None,
                 mutated_sleeps: dict[int, float] | None = None) -> list[str]:
    slow = slow_indices or set()
    ignore = ignore_mul or set()
    sleeps = mutated_sleeps or {}
    ids = []
    for i in range(n):
        tag = f"g{i}"
        body = _body(tag, log, check_mul=i not in ignore,
                     sleep_when_mutated=sleeps.get(i, 1.5),
                     sleep_always=(1.5 if i in slow else 0.0))
        path = ctx["tester_worktree"] / "tests" / f"test_keypath_{tag}.py"
        path.write_text(body, encoding="utf-8")
        ids.append(f"tests/test_keypath_{tag}.py::test_{tag}")
    return ids


def _groups(ids: list[str], bids: list[str], *, timeouts: list[int] | None = None) -> list[dict]:
    return [
        {"kind": "mutation", "behavior_ids": [bid], "test_ids": [tid],
         "timeout": (timeouts[i] if timeouts else 30)}
        for i, (bid, tid) in enumerate(zip(bids, ids))
    ]


def _blind_and_integrate(ctx: dict, cli, hook, ids: list[str], bids: list[str]) -> None:
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(ctx["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    r = role_turn(hook, "tester", "T1", build_tester_payload(_groups(ids, bids), bids), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"


def _deliver_patches(ctx: dict, hook, ids: list[str], bids: list[str],
                     *, timeouts: list[int] | None = None) -> None:
    patch_file = str(write_patch_file(mutation_patch(ctx["worktree"])))
    groups = _groups(ids, bids, timeouts=timeouts)
    for g in groups:
        g["patch_file"] = patch_file
    r = role_turn(hook, "tester", "T1", build_tester_payload(groups, bids))
    assert r["code"] == 0, r


def _rows(log: Path) -> list[tuple[str, str, float, float]]:
    out = []
    if not log.exists():
        return out
    for line in log.read_text(encoding="utf-8").splitlines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        tag, flag, start, end = parts
        out.append((tag, flag, float(start), float(end)))
    return out


def _overlaps(intervals: list[tuple[float, float]]) -> bool:
    for i in range(len(intervals)):
        for j in range(i + 1, len(intervals)):
            a0, a1 = intervals[i]
            b0, b1 = intervals[j]
            if max(a0, b0) < min(a1, b1):
                return True
    return False


def _no_overlap(intervals: list[tuple[float, float]]) -> bool:
    ordered = sorted(intervals)
    return all(ordered[i][1] <= ordered[i + 1][0] + 1e-6 for i in range(len(ordered) - 1))


# ---------------------------------------------------------------- 正向（baseline-red）


def test_b5_counterexample_groups_run_concurrently(repo, cli, hook, tmp_path):
    log = tmp_path / "trace.log"
    ctx, bids = _start_groups(repo, cli, 3, 2)
    ids = _write_tests(ctx, log, 3)
    _blind_and_integrate(ctx, cli, hook, ids, bids)
    _deliver_patches(ctx, hook, ids, bids)
    log.write_text("", encoding="utf-8")  # 只看 proof 自己的执行区间

    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    assert [g["behavior_id"] for g in out["groups"]] == bids, out["groups"]

    rows = _rows(log)
    candidate = [(s, e) for _t, flag, s, e in rows if flag == "ok"]
    mutated = [(s, e) for _t, flag, s, e in rows if flag == "mutated"]
    assert len(candidate) == 3, rows
    assert len(mutated) == 3, rows
    assert _no_overlap(candidate), f"候选段本应串行，实际重叠：{candidate}"
    assert _overlaps(mutated), f"反例段未并发执行：{mutated}"


# ---------------------------------------------------------------- 边界（守卫，不列入 proof group）


def test_b5_boundary_default_proof_groups_still_serial(repo, cli, hook, tmp_path):
    """proof_groups 缺省（1）时行为与改动前一致：3 组都执行、候选段串行、反例段也串行。"""
    log = tmp_path / "trace.log"
    ctx, bids = _start_groups(repo, cli, 3, 1)
    ids = _write_tests(ctx, log, 3)
    _blind_and_integrate(ctx, cli, hook, ids, bids)
    _deliver_patches(ctx, hook, ids, bids)
    log.write_text("", encoding="utf-8")

    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    assert [g["behavior_id"] for g in out["groups"]] == bids
    rows = _rows(log)
    candidate = [(s, e) for _t, flag, s, e in rows if flag == "ok"]
    mutated = [(s, e) for _t, flag, s, e in rows if flag == "mutated"]
    assert _no_overlap(candidate) and _no_overlap(mutated), rows


def test_b5_boundary_failure_reported_by_smallest_group_index(repo, cli, hook, tmp_path):
    """并发下组 1 先完成失败、组 0 后完成失败时，仍按最小组下标报组 0。"""
    log = tmp_path / "trace.log"
    ctx, bids = _start_groups(repo, cli, 2, 2)
    # 两组都不检查 mul（patch 后仍绿 = TEST_MUTATION_SURVIVED）；组 0 更慢
    ids = _write_tests(ctx, log, 2, slow_indices={0}, ignore_mul={0, 1})
    _blind_and_integrate(ctx, cli, hook, ids, bids)
    _deliver_patches(ctx, hook, ids, bids)

    out = cli("proof", "--session", "S1", expect=1)
    assert out["result"] == "FAIL", out
    assert out["failure"]["code"] == "TEST_MUTATION_SURVIVED", out["failure"]
    assert out["failure"]["group"] == 0, out["failure"]


def test_b5_boundary_group_timeout_stays_timeout_failure(repo, cli, hook, tmp_path):
    """并发模式下某组 timeout 仍是 TEST_PROOF_GROUP_TIMEOUT（归 tester），不转成测试失败。"""
    log = tmp_path / "trace.log"
    ctx, bids = _start_groups(repo, cli, 2, 2)
    ids = _write_tests(ctx, log, 2, mutated_sleeps={0: 20.0})
    _blind_and_integrate(ctx, cli, hook, ids, bids)
    _deliver_patches(ctx, hook, ids, bids, timeouts=[3, 30])

    out = cli("proof", "--session", "S1", expect=1)
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_GROUP_TIMEOUT", f
    assert f["suggested_owner"] == "tester", f
    assert f["group"] == 0, f