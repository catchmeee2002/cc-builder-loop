"""B3：machine stage 的直接进程被信号终止（wait 返回负 returncode）是一次无效观察。

起点代码把任何非 0 退出（含负 returncode）都记成 machine FAIL：写 evidence、追加
failures.machine、machine_iter +1。下面的用例在基线上都在 call 阶段失败（baseline-red）。

守卫（不列入 proof_spec）：输入投影同时变化时 MACHINE_INPUT_CHANGED 优先、普通正 returncode
与 runtime timeout 仍为 FAIL——这些在基线上已经成立，只作为集成后的回归防护。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import evidence
from builder_loop import ledger as L
from conftest import ROOT, contract_with, write_plan

BL = str(ROOT / "bin" / "bl")
SIGNAL_TERM = "exec python3 -c 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'"
SIGNAL_KILL = "exec python3 -c 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'"
PASS_CMD = "python3 -c 'import sys; sys.exit(0)'"
FAIL_CMD = "python3 -c 'import sys; sys.exit(1)'"
TIMEOUT_CMD = "exec python3 -c 'import time; time.sleep(30)'"


def _lite_run(repo, cli, *cmds: str, timeout: int = 30):
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    wt, lp = Path(out["worktree"]), Path(out["ledger"])
    with L.mutate(lp) as lg:
        lg["contract"]["assurance"]["machine_commands"] = [
            {"stage": f"s{i}", "cmd": c, "timeout": timeout} for i, c in enumerate(cmds)
        ]
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "# candidate\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    return wt, lp


def _assert_signal(value, signum: int, name: str) -> None:
    assert value in (signum, str(signum), name), f"signal 应表达 {name}，实际 {value!r}"


def _assert_invalid_observation(lp: Path, before: dict, *, stage: str) -> dict:
    after = L.load(lp)
    assert after["evidence"]["machine"] == before["evidence"]["machine"], after["evidence"]["machine"]
    assert after["failures"]["machine"] == before["failures"]["machine"], after["failures"]["machine"]
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"], after["counters"]
    ev = after["events"][-1]
    assert ev["kind"] == "machine_stage_signaled", ev
    assert ev["candidate_head"] == before["candidate"]["head"], ev
    assert ev["dependency_digest"] == evidence.dependency_digest(before, "machine", Path(before["repo"]["root"])), ev
    return after


# ---------------------------------------------------------------- 正向：负 returncode 是无效观察


def test_machine_sigterm_is_invalid_observation(repo, cli):
    _wt, lp = _lite_run(repo, cli, SIGNAL_TERM)
    before = L.load(lp)
    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_STAGE_SIGNALED", out
    assert out["exit_code"] == 1, out
    d = out["details"]
    assert d["stage"] == "s0", d
    assert isinstance(d["returncode"], int) and d["returncode"] < 0, d
    _assert_signal(d["signal"], 15, "SIGTERM")
    assert Path(d["log"]).is_file(), d
    assert [s["stage"] for s in d["stages"]] == ["s0"], d
    _assert_invalid_observation(lp, before, stage="s0")


def test_machine_sigkill_uses_same_semantics(repo, cli):
    """外部、自身发信号或崩溃采用同一语义：换一种信号仍走 MACHINE_STAGE_SIGNALED。"""
    _wt, lp = _lite_run(repo, cli, SIGNAL_KILL)
    before = L.load(lp)
    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_STAGE_SIGNALED", out
    assert out["exit_code"] == 1, out
    d = out["details"]
    assert isinstance(d["returncode"], int) and d["returncode"] < 0, d
    _assert_signal(d["signal"], 9, "SIGKILL")
    _assert_invalid_observation(lp, before, stage="s0")


def test_signal_observation_can_be_rerun_without_resume_or_retry(repo, cli):
    """输入不变可直接重跑：runtime 不自动重试，也不把信号计入迭代上限。"""
    _wt, lp = _lite_run(repo, cli, SIGNAL_TERM)
    before = L.load(lp)

    first = cli("machine", "--session", "S1", expect=1)
    second = cli("machine", "--session", "S1", expect=1)

    assert first["code"] == second["code"] == "MACHINE_STAGE_SIGNALED"
    after = L.load(lp)
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    assert after["failures"]["machine"] == []
    assert after["evidence"]["machine"] is None
    assert [e["kind"] for e in after["events"]].count("machine_stage_signaled") == 2
    # 不是 MAX_ITERATIONS / NO_PROGRESS：不需要 resume 授权
    assert cli("status", "--session", "S1")["readiness"]["blockers"] == []
    assert cli("status", "--session", "S1")["readiness"]["next_action"] == "machine"


def test_preflight_signal_is_inconclusive_not_baseline_red(repo, cli):
    _wt, lp = _lite_run(repo, cli, SIGNAL_TERM)
    out = cli("preflight", "--session", "S1")

    assert out["result"] == "INCONCLUSIVE", out
    assert out["baseline_signaled"] == ["s0"], out
    assert out["baseline_red"] == [], out
    assert out["baseline_timed_out"] == [], out
    lg = L.load(lp)
    assert not any(lg["evidence"].values())
    assert lg["counters"]["machine_iter"] == 0
    ev = [e for e in L.events_of(lg, "preflight")][-1]
    assert ev["baseline_red"] == [], ev


def test_signal_does_not_erase_an_existing_valid_pass(repo, cli, tmp_path):
    """不变量：信号作废绝不生成或删除有效 PASS。同一输入先 PASS，再让 stage 自发信号。"""
    marker = tmp_path / "signal-now"
    cmd = (
        "exec python3 -c 'import os, signal, sys; "
        f"sys.exit(0) if not os.path.exists(\"{marker}\") else os.kill(os.getpid(), signal.SIGTERM)'"
    )
    _wt, lp = _lite_run(repo, cli, cmd)

    first = cli("machine", "--session", "S1")
    assert first["result"] == "PASS", first
    passed = L.load(lp)
    pass_record = passed["evidence"]["machine"]
    assert pass_record["status"] == "pass"
    iters = passed["counters"]["machine_iter"]

    marker.write_text("x", encoding="utf-8")
    out = cli("machine", "--session", "S1", expect=1)
    assert out["code"] == "MACHINE_STAGE_SIGNALED", out

    after = L.load(lp)
    assert after["evidence"]["machine"] == pass_record, after["evidence"]["machine"]
    assert after["counters"]["machine_iter"] == iters
    assert after["failures"]["machine"] == []
    assert evidence.state(after, "machine", Path(after["repo"]["root"])) == evidence.STATE_PASS


# ---------------------------------------------------------------- 守卫：边界与不变量（不列入 proof_spec）


def test_guard_input_change_takes_priority_over_signal(repo, cli):
    checkpointing_signal = (
        "echo '# extra' > src/extra.py && "
        f"{BL} --repo {repo.root} checkpoint --session S1 --role builder && "
        "exec python3 -c 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'"
    )
    _wt, lp = _lite_run(repo, cli, checkpointing_signal)
    before = L.load(lp)

    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_INPUT_CHANGED", out
    after = L.load(lp)
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    assert not any(e["kind"] == "machine_stage_signaled" for e in after["events"])


def test_guard_positive_returncode_still_fails(repo, cli):
    _wt, lp = _lite_run(repo, cli, FAIL_CMD)
    out = cli("machine", "--session", "S1", expect=1)
    assert out["result"] == "FAIL", out
    lg = L.load(lp)
    assert lg["evidence"]["machine"]["status"] == "fail"
    assert len(lg["failures"]["machine"]) == 1
    assert lg["counters"]["machine_iter"] == 1


def test_guard_runtime_timeout_still_fails(repo, cli):
    _wt, lp = _lite_run(repo, cli, TIMEOUT_CMD, timeout=2)
    out = cli("machine", "--session", "S1", expect=1)
    assert out["result"] == "FAIL", out
    assert out["failure"]["timed_out"] is True and out["failure"]["returncode"] == 124, out["failure"]
    lg = L.load(lp)
    assert lg["evidence"]["machine"]["status"] == "fail"
    assert lg["counters"]["machine_iter"] == 1