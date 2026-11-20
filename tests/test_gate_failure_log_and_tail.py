"""B2：门禁失败的结果只给指向完整日志的 log，不再给 tail。

given bl machine stage / bl proof runner 已执行并失败且完整日志超过旧 tail 长度（4000 / 3000 字符）
→ 读取命令返回的 JSON failure 不含 tail、含 log；log 指向的文件同时保留开头与结尾。

起点代码两边都带 tail，下面的正向断言在 call 阶段失败（baseline-red）。边界与不变量（结构性
proof 失败不伪造 log、PASS/FAIL 与重复失败签名不变）在起点上已成立，是守卫，不列入 proof_spec。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import ledger as L
from conftest import (
    PROOF_RUNNER_CMD,
    contract_with,
    implement_mul,
    make_tester_result,
    mutation_patch,
    role_turn,
    write_plan,
)

# 首尾标记夹住超过旧 tail（4000 字符）的输出：整份日志从 HEADMARK 开始、到 ENDMARK 结束
LONG_STAGE_CMD = "echo HEADMARK; seq 1 20000; echo 'see tests/test_foo.py'; echo ENDMARK; exit 1"

# 开头（HEADMARK）与结尾（ENDMARK）之间隔开远超旧 tail 长度的输出：两个标记都在 = 完整日志
LONG_TEST = (
    "def test_long_output():\n"
    "    print('HEADMARK')\n"
    "    for i in range(20000):\n"
    "        print('line', i)\n"
    "    print('ENDMARK')\n"
    "    assert False, 'boom'\n"
)

# 同一次运行里改写候选的 tracked 文件（src/foo.py）——proof 判 PROOF_WORKTREE_MUTATED。
# 长输出必须来自真正进报告的内容：pytest -q 会吞掉通过用例的 stdout，只有失败断言的 message
# 才会被完整打印（proof_runner.cmd 是冻结的，不能靠 -s / --tb）
MUTATING_TEST = (
    "from pathlib import Path\n"
    "from src.foo import mul\n\n\n"
    "def test_mutating():\n"
    "    assert mul(3, 4) == 12\n"
    "    p = Path('src/foo.py')\n"
    "    p.write_text(p.read_text() + '\\n# mutated by test\\n')\n"
    "    assert False, 'HEADMARK\\n' + ('x' * 20000) + '\\nENDMARK'\n"
)


def _start_lite(repo, cli, max_iterations: int = 5):
    (repo.root / ".claude" / "loop.yml").write_text(
        f'pass_cmd:\n  - stage: unit\n    cmd: "{LONG_STAGE_CMD}"\n    timeout: 60\n'
        f"max_iterations: {max_iterations}\n"
        f'proof_runner:\n  framework: pytest\n  cmd: "{PROOF_RUNNER_CMD}"\n',
        encoding="utf-8",
    )
    repo.commit_all("chore(fixture): [cr_id_skip] Long failing machine stage")
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    return Path(out["worktree"]), Path(out["ledger"])


def _assert_full_log(failure: dict, expected_markers: tuple[str, ...], min_bytes: int = 4000) -> None:
    assert "failure_summary_cmd" not in failure, failure
    log = failure.get("log")
    assert isinstance(log, str) and log, failure
    p = Path(log)
    assert p.is_file()
    data = p.read_bytes()
    assert len(data) > min_bytes, len(data)
    text = data.decode("utf-8", "replace")
    for marker in expected_markers:
        assert marker in text, (marker, text[:200])
    assert "tail" not in failure, failure


def test_b2_machine_failure_drops_tail_and_keeps_full_log(repo, cli):
    wt, lp = _start_lite(repo, cli)
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    res = cli("machine", "--session", "S1", expect=1)
    assert res["result"] == "FAIL"
    failure = res["failure"]
    _assert_full_log(failure, ("HEADMARK", "ENDMARK", "see tests/test_foo.py"))
    # 完整日志同时保留开头与结尾：stage 输出被整份落盘，不是只留尾部
    text = Path(failure["log"]).read_text(encoding="utf-8", errors="replace")
    assert text.strip().startswith("HEADMARK") and text.strip().endswith("ENDMARK"), (text[:200], text[-200:])
    # 同一条失败记录写进 ledger 的 details 时也不带 tail
    assert "tail" not in L.load(lp)["evidence"]["machine"]["details"]["failure"]


def test_b2_proof_runner_failure_drops_tail_and_keeps_full_log(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_long.py").write_text(LONG_TEST)
    ids = ["tests/test_long.py::test_long_output"]
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=ids), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt), test_ids=ids))
    assert r["code"] == 0, r
    out = cli("proof", "--session", "S1", expect=1)
    assert out["result"] == "FAIL"
    failure = out["failure"]
    assert failure["code"] == "TEST_PROOF_CANDIDATE_FAILED", failure
    _assert_full_log(failure, ("HEADMARK", "ENDMARK", "boom"))


def test_b2_proof_worktree_mutated_failure_drops_tail_and_keeps_full_log(started, cli, hook):
    """另一条「runner 已执行」的 proof 失败（PROOF_WORKTREE_MUTATED）同样只给完整 log，不给 tail。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    # runner 执行时改写候选里的 tracked 文件，并在同一次运行里产出足够长的输出
    (twt / "tests" / "test_mutating.py").write_text(MUTATING_TEST)
    ids = ["tests/test_mutating.py::test_mutating"]
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", test_ids=ids), start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    r = role_turn(hook, "tester", "T1", make_tester_result("mutation", mutation_patch(wt), test_ids=ids))
    assert r["code"] == 0, r
    out = cli("proof", "--session", "S1", expect=1)
    assert out["result"] == "FAIL"
    failure = out["failure"]
    assert failure["code"] == "PROOF_WORKTREE_MUTATED", failure
    _assert_full_log(failure, ("HEADMARK", "ENDMARK"))


# ---------------------------------------------------------------- 守卫：边界与不变量（不列入 proof_spec）


def test_guard_structural_proof_failure_does_not_fabricate_log(started, cli, hook):
    """边界：未执行 runner 的结构性 proof 失败（缺 mutation patch）不伪造 log。"""
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_mul.py").write_text(
        "from src.foo import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n"
    )
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)  # patch 留空
    cli("integrate", "--session", "S1")
    out = cli("proof", "--session", "S1", expect=1)
    assert out["failure"]["code"] == "TEST_MUTATION_PATCH_MISSING"
    assert "log" not in out["failure"] or out["failure"].get("log") is None


def test_guard_repeat_signature_and_no_progress_unchanged(repo, cli):
    """不变量：同一失败重复出现时签名相同、repeat_count 照常累加，machine_iter 按运行计数。"""
    wt, lp = _start_lite(repo, cli)
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    first = cli("machine", "--session", "S1", expect=1)
    second = cli("machine", "--session", "S1", expect=1)
    assert first["result"] == second["result"] == "FAIL"
    assert first["failure"]["signature"] == second["failure"]["signature"]
    assert second["repeat_count"] == 2
    lg = L.load(lp)
    assert lg["counters"]["machine_iter"] == 2
    assert len(lg["failures"]["machine"]) == 2
