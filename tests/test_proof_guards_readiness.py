"""B7：guard 的 patch 缺省 / 已打不到当前候选时，要把「补 patch / 重生成 patch」的待办提前到 readiness
与 tester brief，而不是等 proof 阶段才报错。

given tester 与 machine 已就绪、tester 已集成、候选对 tester 可读：
- 某 group 的 guard 没有 patch → 其 behavior id 进 `missing_patch_groups`、`evidence.missing_patch` 为真、
  readiness.next_action=resume_tester、tester brief 的 todo 含 add_mutation_patch 且 behaviors 含该 id；
- guard 的 patch 已打不到当前候选 HEAD → 其 behavior id 进 `stale_patch_groups`、todo 含 stale_mutation_patch。

起点代码只统计 mutation 组、对 guards 视而不见，所以这些正向断言在起点上 call 阶段失败（baseline-red）。
「同一 behavior 两处缺 patch 只出现一次」「guard 全带可打上的 patch 时两个列表都不含它」是守卫。
"""

from __future__ import annotations

from builder_loop import brief as brief_mod
from builder_loop import evidence
from builder_loop import ledger as L
from builder_loop import proof as P

from conftest import implement_mul, mutation_patch, role_turn, write_mul_test

GROUP_TEST = "tests/test_mul.py::test_mul"
GUARD_FILE = "tests/test_guard_mul.py"
GUARD_TEST = f"{GUARD_FILE}::test_guard_mul"
GUARD_SRC = "from src.foo import mul\n\n\ndef test_guard_mul():\n    assert mul(2, 3) == 6\n"
# 打不到 src/foo.py 的补丁：上下文对不上
STALE_PATCH = (
    "diff --git a/src/foo.py b/src/foo.py\n"
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,2 +1,2 @@\n"
    "-zzz\n"
    "+yyy\n"
)


def _ready_after_integrate(started, cli, hook) -> None:
    """baseline-red 组（自己的行为不需要 mutation patch）+ 一个缺 patch 的 guard，集成 + machine PASS。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    (twt / GUARD_FILE).write_text(GUARD_SRC, encoding="utf-8")
    spec = {"role": "tester", "status": "pass", "behaviors_covered": ["B1"], "proof_spec": {"groups": [
        {"kind": "baseline-red", "behavior_ids": ["B1"], "test_ids": [GROUP_TEST], "timeout": 60,
         "guards": [{"test_ids": [GUARD_TEST]}]}]}}
    r = role_turn(hook, "tester", "T1", spec, start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"


def _set_stale_guard_patch(started) -> None:
    with L.mutate(started["ledger"]) as lg2:
        lg2["proof_spec"]["groups"][0]["guards"][0]["patch"] = STALE_PATCH


def _todo(lg, repo_root, what: str) -> list[dict]:
    return [t for t in brief_mod.build(lg, repo_root, "tester")["todo"] if t.get("what") == what]


# ---------------------------------------------------------------- guard 缺 patch（baseline-red）


def test_b7_guard_missing_patch_listed_in_missing_patch_groups(started, cli, hook):
    _ready_after_integrate(started, cli, hook)
    lg = L.load(started["ledger"])
    assert P.missing_patch_groups(lg["proof_spec"]) == ["B1"]


def test_b7_guard_missing_patch_sets_missing_patch_and_resume_tester(started, cli, hook):
    _ready_after_integrate(started, cli, hook)
    lg = L.load(started["ledger"])
    assert evidence.missing_patch(lg) is True
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "resume_tester"


def test_b7_guard_missing_patch_brief_todo_add_mutation_patch(started, cli, hook):
    _ready_after_integrate(started, cli, hook)
    lg = L.load(started["ledger"])
    todo = _todo(lg, started["repo"].root, "add_mutation_patch")
    assert todo and "B1" in todo[0]["behaviors"], todo


# ---------------------------------------------------------------- guard patch 已打不上（baseline-red）


def test_b7_stale_guard_patch_listed_in_stale_patch_groups(started, cli, hook):
    _ready_after_integrate(started, cli, hook)
    _set_stale_guard_patch(started)
    lg = L.load(started["ledger"])
    assert P.stale_patch_groups(lg, started["repo"].root) == ["B1"]


def test_b7_stale_guard_patch_brief_todo_stale_mutation_patch(started, cli, hook):
    _ready_after_integrate(started, cli, hook)
    _set_stale_guard_patch(started)
    lg = L.load(started["ledger"])
    todo = _todo(lg, started["repo"].root, "stale_mutation_patch")
    assert todo and "B1" in todo[0]["behaviors"], todo


# ---------------------------------------------------------------- 守卫（不列入 proof group）


def test_guard_b7_one_behavior_missing_patch_listed_once(started, cli, hook):
    """边界：同一 behavior 既有 mutation 组缺 patch 又有 guard 缺 patch 时，behavior id 只出现一次。"""
    spec = {"groups": [{"kind": "mutation", "behavior_ids": ["B1"], "test_ids": [GROUP_TEST], "timeout": 60,
                        "guards": [{"test_ids": [GUARD_TEST]}]}]}
    assert P.missing_patch_groups(spec) == ["B1"]


def test_guard_b7_applicable_guard_patch_in_no_list(started, cli, hook):
    """边界：guards 全部带 patch 且都能打上时，两个列表都不含该 behavior。"""
    _ready_after_integrate(started, cli, hook)
    patch = mutation_patch(started["worktree"])
    with L.mutate(started["ledger"]) as lg2:
        lg2["proof_spec"]["groups"][0]["guards"][0]["patch"] = patch
    lg = L.load(started["ledger"])
    assert P.missing_patch_groups(lg["proof_spec"]) == []
    assert P.stale_patch_groups(lg, started["repo"].root) == []