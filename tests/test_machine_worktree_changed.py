"""B1：machine 收尾发现候选工作树变化（tracked 修改 / 未忽略 untracked / HEAD 变化）时作废观察。

起点代码把工作树变化记成候选 FAIL（写 evidence、追加 failures.machine、machine_iter +1，
`failure.worktree_mutated`）。本行为要求：negative `MACHINE_WORKTREE_CHANGED`（exit 1），携带
paths / head_at_start / head_after / stages，只记一条绑定起跑 candidate_head、iter 与
dependency_digest 的 `machine_worktree_changed` event；不写 machine evidence、不追加
failures.machine、不增加 machine_iter。变化来源（builder / pass_cmd / 其他进程）不参与判断。

守卫（不列入 proof_spec，基线已成立，只作为集成后的回归防护）：输入投影同时变化时
`MACHINE_INPUT_CHANGED` 优先且不重复登记；stage 直接进程被信号终止时 `MACHINE_STAGE_SIGNALED`
优先；忽略文件不算变化；候选未变化时正常 PASS、正 returncode FAIL 与 runtime timeout FAIL 分类不变。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from builder_loop import evidence
from builder_loop import ledger as L
from conftest import ROOT, contract_with, git, write_plan

BL = str(ROOT / "bin" / "bl")
PASS_CMD = "python3 -c 'import sys; sys.exit(0)'"
MUTATE_TRACKED = "echo '# stage edit' >> src/foo.py"
MUTATE_UNTRACKED = "echo '# new' > src/extra.py"
COMMIT_CMD = (
    "echo '# c' > src/direct.py && git add -A && "
    "git -c core.hooksPath=/dev/null -c user.email=t@t -c user.name=t commit -q -m 'chore(x): [cr_id_skip] Direct'"
)


def _lite_run(repo, cli, *cmds: str, timeout: int = 30):
    """只有 machine + reviewer 的 run（不需要 tester），stage 换成给定命令后再 checkpoint 出干净候选。"""
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


def _assert_invalidated(lp: Path, before: dict) -> tuple[dict, dict]:
    """作废观察的全部账本事实：不写 evidence / failures / machine_iter，只追加一条绑定起跑输入的 event。"""
    after = L.load(lp)
    root = Path(before["repo"]["root"])
    assert after["evidence"]["machine"] == before["evidence"]["machine"], after["evidence"]["machine"]
    assert after["failures"]["machine"] == before["failures"]["machine"], after["failures"]["machine"]
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"], after["counters"]
    evs = L.events_of(after, "machine_worktree_changed")
    assert len(evs) == 1, evs
    assert after["events"][-1]["kind"] == "machine_worktree_changed", after["events"][-1]
    ev = evs[0]
    assert ev["candidate_head"] == before["candidate"]["head"], ev
    assert ev["iter"] == before["counters"]["machine_iter"] + 1, ev
    assert ev["dependency_digest"] == evidence.dependency_digest(before, "machine", root), ev
    return after, ev


# ---------------------------------------------------------------- 正向：工作树变化 = 无效观察


def test_tracked_modification_invalidates_observation(repo, cli):
    wt, lp = _lite_run(repo, cli, MUTATE_TRACKED)
    before = L.load(lp)
    head0 = before["candidate"]["head"]

    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_WORKTREE_CHANGED", out
    assert out["exit_code"] == 1, out
    d = out["details"]
    assert d["paths"] == ["src/foo.py"], d
    assert d["head_at_start"] == head0, d
    assert d["head_after"] == head0 and d["head_after"] == git(wt, "rev-parse", "HEAD"), d
    assert [s["stage"] for s in d["stages"]] == ["s0"], d
    assert Path(d["stages"][0]["log"]).is_file(), d
    _assert_invalidated(lp, before)
    # runtime 不恢复现场：改动仍在工作树里（等处理完再直接重跑）
    assert "src/foo.py" in git(wt, "status", "--porcelain")


def test_untracked_file_invalidates_observation_even_when_stage_passed(repo, cli):
    """stage 退出码 0（本会 PASS），但留下了未忽略的 untracked 文件：变化优先于普通 PASS。"""
    wt, lp = _lite_run(repo, cli, MUTATE_UNTRACKED)
    before = L.load(lp)

    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_WORKTREE_CHANGED", out
    assert out["details"]["paths"] == ["src/extra.py"], out["details"]
    _assert_invalidated(lp, before)
    assert (wt / "src" / "extra.py").is_file()


def test_head_change_invalidates_observation_and_head_is_not_rewritten(repo, cli):
    wt, lp = _lite_run(repo, cli, COMMIT_CMD)
    before = L.load(lp)
    head0 = before["candidate"]["head"]

    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_WORKTREE_CHANGED", out
    d = out["details"]
    assert d["head_at_start"] == head0, d
    assert d["head_after"] == git(wt, "rev-parse", "HEAD"), d
    assert d["head_after"] != d["head_at_start"], d
    assert d["paths"] == [], d
    _assert_invalidated(lp, before)
    # 不 reset HEAD、不改写 ledger 的 candidate.head
    assert L.load(lp)["candidate"]["head"] == head0
    assert git(wt, "rev-parse", "HEAD") == d["head_after"]


def test_worktree_change_beats_positive_returncode_fail(repo, cli):
    """边界：stage 以正 returncode 失败，但同样改了工作树 —— 变化优先，不算候选 FAIL。"""
    wt, lp = _lite_run(repo, cli, "echo '# stage edit' >> src/foo.py && python3 -c 'import sys; sys.exit(3)'")
    before = L.load(lp)

    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_WORKTREE_CHANGED", out
    assert out["details"]["paths"] == ["src/foo.py"], out["details"]
    _assert_invalidated(lp, before)


def test_worktree_change_beats_runtime_timeout_fail(repo, cli):
    """边界：stage 超时（timed_out），但同样改了工作树 —— 变化优先，不算候选 FAIL。"""
    wt, lp = _lite_run(
        repo, cli,
        "echo '# stage edit' >> src/foo.py && exec python3 -c 'import time; time.sleep(30)'",
        timeout=2,
    )
    before = L.load(lp)

    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_WORKTREE_CHANGED", out
    assert out["details"]["paths"] == ["src/foo.py"], out["details"]
    _assert_invalidated(lp, before)


def test_change_from_other_source_has_same_semantics(repo, cli, tmp_path):
    """来源不归因：不是 pass_cmd 自己写、而是执行期间另一个进程改的工作树，同样作废。"""
    go, done = tmp_path / "stage-started", tmp_path / "write-done"
    # stage 等外部进程写完再退出（同步文件，不靠 sleep 抢时序）
    wait_cmd = (f"touch {go}; i=0; while [ ! -e {done} ] && [ $i -lt 500 ]; do sleep 0.02; i=$((i+1)); done")
    wt, lp = _lite_run(repo, cli, wait_cmd)
    writer = subprocess.Popen(
        ["bash", "-c", f"while [ ! -e '{go}' ]; do sleep 0.02; done; echo '# outside' >> src/foo.py; touch '{done}'"],
        cwd=str(wt),
    )
    try:
        before = L.load(lp)
        out = cli("machine", "--session", "S1", expect=1)
    finally:
        writer.wait(timeout=15)
    assert writer.returncode == 0, writer.returncode
    assert done.is_file()

    assert out["code"] == "MACHINE_WORKTREE_CHANGED", out
    assert out["details"]["paths"] == ["src/foo.py"], out["details"]
    _assert_invalidated(lp, before)


def test_invalidation_can_be_rerun_directly_without_resume_or_retry(repo, cli, tmp_path):
    """runtime 不自动重试、不清理现场：处理完直接重跑即可，不占迭代、不需要 resume/retry。"""
    marker = tmp_path / "mutate-now"
    marker.write_text("x", encoding="utf-8")
    cmd = f"if [ -e '{marker}' ]; then echo '# stage edit' >> src/foo.py; fi"
    wt, lp = _lite_run(repo, cli, cmd)
    before = L.load(lp)

    first = cli("machine", "--session", "S1", expect=1)
    assert first["code"] == "MACHINE_WORKTREE_CHANGED", first
    st = cli("status", "--session", "S1")["readiness"]
    assert st["blockers"] == [] and st["next_action"] == "machine", st

    # 人工处理现场：还原改动、让 stage 不再写；之后直接重跑（不 resume、不 --retry）
    marker.unlink()
    git(wt, "checkout", "--", "src/foo.py")
    out = cli("machine", "--session", "S1")
    assert out["result"] == "PASS", out
    assert out["iter"] == before["counters"]["machine_iter"] + 1, out
    assert L.load(lp)["counters"]["machine_iter"] == before["counters"]["machine_iter"] + 1


def test_existing_valid_pass_is_not_erased_by_invalidation(repo, cli, tmp_path):
    """不变量：作废绝不删除既有有效 evidence，也不生成 PASS。"""
    marker = tmp_path / "mutate-now"
    cmd = f"if [ -e '{marker}' ]; then echo '# stage edit' >> src/foo.py; fi"
    wt, lp = _lite_run(repo, cli, cmd)

    first = cli("machine", "--session", "S1")
    assert first["result"] == "PASS", first
    passed = L.load(lp)
    pass_record = passed["evidence"]["machine"]
    iters = passed["counters"]["machine_iter"]

    marker.write_text("x", encoding="utf-8")
    out = cli("machine", "--session", "S1", expect=1)
    assert out["code"] == "MACHINE_WORKTREE_CHANGED", out

    after = L.load(lp)
    assert after["evidence"]["machine"] == pass_record, after["evidence"]["machine"]
    assert after["counters"]["machine_iter"] == iters, after["counters"]
    assert after["failures"]["machine"] == [], after["failures"]["machine"]
    assert evidence.state(after, "machine", Path(after["repo"]["root"])) == evidence.STATE_PASS


# ---------------------------------------------------------------- 守卫：边界与不变量（不列入 proof_spec）


def test_guard_input_change_takes_priority_over_worktree_change(repo, cli):
    """ledger 输入投影同时变化（builder 并发 checkpoint）时 MACHINE_INPUT_CHANGED 优先，且不重复登记。"""
    wt, lp = _lite_run(
        repo, cli,
        f"echo '# extra' > src/extra.py && {BL} --repo {repo.root} checkpoint --session S1 --role builder",
    )
    before = L.load(lp)

    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_INPUT_CHANGED", out
    after = L.load(lp)
    assert L.events_of(after, "machine_input_changed"), after["events"]
    assert not L.events_of(after, "machine_worktree_changed"), after["events"]
    assert after["failures"]["machine"] == before["failures"]["machine"]
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]


def test_guard_stage_signal_takes_priority_over_worktree_change(repo, cli):
    """stage 直接进程被信号终止时 MACHINE_STAGE_SIGNALED 优先，不再登记工作树变化。"""
    wt, lp = _lite_run(
        repo, cli,
        "echo '# stage edit' >> src/foo.py && exec python3 -c 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'",
    )
    before = L.load(lp)

    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_STAGE_SIGNALED", out
    after = L.load(lp)
    assert L.events_of(after, "machine_stage_signaled"), after["events"]
    assert not L.events_of(after, "machine_worktree_changed"), after["events"]
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]


def test_guard_ignored_untracked_file_is_not_a_change(repo, cli):
    """只算 tracked 修改与未忽略的 untracked：被 .gitignore 忽略的文件不是变化。"""
    wt, lp = _lite_run(repo, cli, "mkdir -p __pycache__ && touch __pycache__/x.pyc")

    out = cli("machine", "--session", "S1")

    assert out["result"] == "PASS", out


def test_guard_clean_candidate_still_passes(repo, cli):
    wt, lp = _lite_run(repo, cli, PASS_CMD)

    out = cli("machine", "--session", "S1")

    assert out["result"] == "PASS", out
    lg = L.load(lp)
    assert lg["evidence"]["machine"]["status"] == "pass"
    assert lg["counters"]["machine_iter"] == 1
    assert not L.events_of(lg, "machine_worktree_changed")


def test_guard_positive_returncode_still_fails(repo, cli):
    wt, lp = _lite_run(repo, cli, "python3 -c 'import sys; sys.exit(3)'")

    out = cli("machine", "--session", "S1", expect=1)

    assert out.get("code") != "MACHINE_WORKTREE_CHANGED", out
    assert out["result"] == "FAIL", out
    lg = L.load(lp)
    assert lg["evidence"]["machine"]["status"] == "fail"
    assert len(lg["failures"]["machine"]) == 1
    assert lg["counters"]["machine_iter"] == 1


def test_guard_runtime_timeout_still_fails(repo, cli):
    wt, lp = _lite_run(repo, cli, "exec python3 -c 'import time; time.sleep(30)'", timeout=2)

    out = cli("machine", "--session", "S1", expect=1)

    assert out.get("code") != "MACHINE_WORKTREE_CHANGED", out
    assert out["result"] == "FAIL", out
    assert out["failure"]["timed_out"] is True and out["failure"]["returncode"] == 124, out["failure"]
    lg = L.load(lp)
    assert lg["evidence"]["machine"]["status"] == "fail"
    assert lg["counters"]["machine_iter"] == 1