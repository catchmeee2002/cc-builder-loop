"""B3：proof 组因 runtime 超时被终止时，失败码是 TEST_PROOF_GROUP_TIMEOUT、owner=tester，并带
phase / group / behavior / timeout / returncode / log；tester brief 出现对应 fix_proof。

三个执行阶段各自覆盖（candidate / baseline / mutation）。起点代码把超时混进既有失败码
（candidate → TEST_PROOF_CANDIDATE_FAILED，baseline → TEST_BASELINE_RED_NOT_PROVEN，
mutation → TEST_MUTATION_SURVIVED），所以下面三条正向断言在 call 阶段失败（baseline-red）。

边界（命令自行 exit 124 不用新码、真实 runner 启动失败仍归 contract）与不变量（timeout 不算鉴别力）
在起点上已成立或不属于新码，作为守卫不列入 proof_spec。
"""

from __future__ import annotations

import copy
from pathlib import Path

from builder_loop import brief as brief_mod
from builder_loop import ledger as L
from conftest import CONTRACT, implement_mul, make_tester_result, mutation_patch, role_turn, write_plan

GROUP_TIMEOUT = 15
SLEEP_SECONDS = 40

# candidate 阶段就超时：任何阶段都先睡
SLOW_ALWAYS = (
    "import time\n"
    "from src.foo import mul\n\n\n"
    "def test_mul():\n"
    f"    time.sleep({SLEEP_SECONDS})\n"
    "    assert mul(3, 4) == 12\n"
)

# baseline 阶段超时：候选上有 mul（快速通过），起点没有 mul（睡到 runtime 杀掉）
SLOW_WITHOUT_MUL = (
    "import time\n"
    "import src.foo\n\n\n"
    "def test_mul():\n"
    "    if not hasattr(src.foo, 'mul'):\n"
    f"        time.sleep({SLEEP_SECONDS})\n"
    "    assert src.foo.mul(3, 4) == 12\n"
)

# mutation 阶段超时：候选实现正确（快速通过），被破坏的实现（mul 不再是乘积）睡到 runtime 杀掉
SLOW_WHEN_MUTATED = (
    "import time\n"
    "from src.foo import mul\n\n\n"
    "def test_mul():\n"
    "    if mul(3, 4) != 12:\n"
    f"        time.sleep({SLEEP_SECONDS})\n"
    "    assert mul(3, 4) == 12\n"
)


def _start(repo, cli):
    # machine 用一条恒过的 stage：本文件只观察 proof 的 timeout 归路，别让 tester 的 sleep / exit
    # 测试文件在门禁里再跑一遍
    (repo.root / ".claude" / "loop.yml").write_text(
        'pass_cmd:\n  - stage: unit\n    cmd: "python3 -c \'import sys; sys.exit(0)\'"\n    timeout: 60\n'
        "max_iterations: 5\n"
        'proof_runner:\n  framework: pytest\n  cmd: "python3 -m pytest -p no:html"\n',
        encoding="utf-8",
    )
    repo.commit_all("chore(fixture): [cr_id_skip] Trivial machine stage")
    write_plan(repo.root, copy.deepcopy(CONTRACT), "plan-timeout.md")
    out = cli("start", "--plan", str(repo.root / "plan-timeout.md"), "--session", "S1")
    return {"repo": repo.root, "worktree": Path(out["worktree"]),
            "tester_worktree": Path(out["tester_worktree"]), "ledger": Path(out["ledger"])}


def _blind_and_integrate(ctx, cli, hook, body: str, *, kind: str = "mutation"):
    wt, twt = ctx["worktree"], ctx["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_mul.py").write_text(body)
    ids = ["tests/test_mul.py::test_mul"]
    res = make_tester_result(kind, test_ids=ids)
    res["proof_spec"]["groups"][0]["timeout"] = GROUP_TIMEOUT
    r = role_turn(hook, "tester", "T1", res, start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    return wt, ids


def _give_patch(ctx, hook, wt, ids, *, kind: str = "mutation"):
    res = make_tester_result(kind, mutation_patch(wt), test_ids=ids)
    res["proof_spec"]["groups"][0]["timeout"] = GROUP_TIMEOUT
    r = role_turn(hook, "tester", "T1", res)
    assert r["code"] == 0, r


def _assert_timeout_failure(ctx, out, *, phase: str):
    assert out["result"] == "FAIL", out
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_GROUP_TIMEOUT", f
    assert f["suggested_owner"] == "tester", f
    assert f["phase"] == phase, f
    assert f["group"] == 0, f
    assert f["behavior"] == "B1", f
    assert f["timeout"] == GROUP_TIMEOUT, f
    assert isinstance(f["returncode"], int) and not isinstance(f["returncode"], bool), f
    assert Path(f["log"]).is_file(), f
    lg = L.load(ctx["ledger"])
    assert lg["failures"]["proof"][-1]["code"] == "TEST_PROOF_GROUP_TIMEOUT"
    todo = [t for t in brief_mod.build(lg, ctx["repo"], "tester")["todo"] if t.get("what") == "fix_proof"]
    assert todo and todo[0]["code"] == "TEST_PROOF_GROUP_TIMEOUT", todo


def test_b3_candidate_phase_timeout_is_group_timeout(repo, cli, hook):
    ctx = _start(repo, cli)
    wt, ids = _blind_and_integrate(ctx, cli, hook, SLOW_ALWAYS)
    _give_patch(ctx, hook, wt, ids)
    out = cli("proof", "--session", "S1", expect=1)
    _assert_timeout_failure(ctx, out, phase="candidate")


def test_b3_baseline_phase_timeout_is_group_timeout(repo, cli, hook):
    ctx = _start(repo, cli)
    _blind_and_integrate(ctx, cli, hook, SLOW_WITHOUT_MUL, kind="baseline-red")
    out = cli("proof", "--session", "S1", expect=1)
    _assert_timeout_failure(ctx, out, phase="baseline")


def test_b3_mutation_phase_timeout_is_group_timeout(repo, cli, hook):
    ctx = _start(repo, cli)
    wt, ids = _blind_and_integrate(ctx, cli, hook, SLOW_WHEN_MUTATED)
    _give_patch(ctx, hook, wt, ids)
    out = cli("proof", "--session", "S1", expect=1)
    _assert_timeout_failure(ctx, out, phase="mutation")


# ---------------------------------------------------------------- 守卫：边界与不变量（不列入 proof_spec）


def test_guard_self_exit_124_is_not_group_timeout(repo, cli, hook):
    """边界：命令自行 exit 124（不是 runtime 终止）时不得使用新失败码。"""
    ctx = _start(repo, cli)
    body = (
        "import os\n"
        "from src.foo import mul\n\n\n"
        "def test_mul():\n"
        "    os._exit(124)\n"
    )
    wt, ids = _blind_and_integrate(ctx, cli, hook, body)
    _give_patch(ctx, hook, wt, ids)  # 让 candidate 阶段真的执行 runner，再自行 exit 124
    out = cli("proof", "--session", "S1", expect=1)
    assert out["failure"]["code"] != "TEST_PROOF_GROUP_TIMEOUT", out["failure"]


def test_guard_real_runner_failure_still_contract(repo, cli, hook):
    """边界：真实 runner 启动失败仍是 TEST_PROOF_RUNNER_FAILED / contract。"""
    ctx = _start(repo, cli)
    wt, ids = _blind_and_integrate(ctx, cli, hook, SLOW_WHEN_MUTATED)
    _give_patch(ctx, hook, wt, ids)
    with L.mutate(ctx["ledger"]) as lg2:
        lg2["contract"]["assurance"]["proof_runner"] = {"framework": "pytest", "cmd": "definitely-not-here-pytest"}
    out = cli("proof", "--session", "S1", expect=1)
    assert out["failure"]["code"] == "TEST_PROOF_RUNNER_FAILED", out["failure"]
    assert out["failure"]["suggested_owner"] == "contract", out["failure"]
