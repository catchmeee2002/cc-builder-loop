"""B2：preflight / machine / proof 因冻结 timeout 被 runtime 终止时，整组清理本次派生的子进程与孙进程。

起点代码用 `subprocess.run(..., timeout=...)`：超时只杀直接子进程，孙进程（心跳）继续存活并写文件。
下面的用例在基线上都在 call 阶段失败（baseline-red）：既有 preflight INCONCLUSIVE、machine
124/timed_out 与 proof TEST_PROOF_GROUP_TIMEOUT 语义在基线上已经成立，唯一变量是「命令返回前
孙进程是否已经停下」。

守卫（不列入 proof_spec）直接调用新模块 `builder_loop.process.run`，在起点上 import 不了，只能
作为集成后的回归防护：正常退出不清理后台子孙、进程组已消失视为已清理。
"""

from __future__ import annotations

import copy
import os
import signal
import time
from pathlib import Path

from builder_loop import ledger as L
from conftest import (
    CONTRACT,
    contract_with,
    implement_mul,
    make_tester_result,
    mutation_patch,
    role_turn,
    write_plan,
)

TIMEOUT = 2
# proof 组 timeout 由 tester 冻结：候选阶段（快）必须跑完，被观察阶段睡到 runtime 杀掉。
# 取 15s 与全量 -n 16 并发下 pytest 启动抖动留出余量（与 test_proof_group_timeout 同量级）。
PROOF_GROUP_TIMEOUT = 15
SLEEP_SECONDS = 40


# ---------------------------------------------------------------- 心跳进程积木


def _worker(tmp_path: Path) -> tuple[Path, Path, Path]:
    """孙进程：先落 pid，再每 50ms 追加一个字节。心跳路径与 pid 文件都在临时目录。"""
    hb = tmp_path / "heartbeat.txt"
    pidf = tmp_path / "heartbeat.pid"
    worker = tmp_path / "heartbeat_worker.py"
    worker.write_text(
        "import os, sys, time\n"
        "hb, pidf = sys.argv[1], sys.argv[2]\n"
        "with open(pidf, 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "while True:\n"
        "    with open(hb, 'a') as fh:\n"
        "        fh.write('x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    return worker, hb, pidf


def _stage_program(tmp_path: Path) -> tuple[Path, Path, Path]:
    """stage：派一个中间子进程，由它再派孙进程写心跳；直接进程自己睡到 timeout。"""
    worker, hb, pidf = _worker(tmp_path)
    script = tmp_path / "slow_stage.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"inner = 'python3 ' + {str(worker)!r} + ' ' + {str(hb)!r} + ' ' + {str(pidf)!r} + ' & wait'\n"
        "subprocess.Popen(['bash', '-c', inner])\n"
        f"time.sleep({SLEEP_SECONDS})\n",
        encoding="utf-8",
    )
    return script, hb, pidf


def _signal_program(tmp_path: Path) -> tuple[Path, Path, Path]:
    """stage：先派子/孙进程写心跳，再直接进程给自己发 SIGTERM（wait 返回负 returncode）。"""
    worker, hb, pidf = _worker(tmp_path)
    script = tmp_path / "signaled_stage.py"
    script.write_text(
        "import os, signal, subprocess, sys, time\n"
        f"inner = 'python3 ' + {str(worker)!r} + ' ' + {str(hb)!r} + ' ' + {str(pidf)!r} + ' & wait'\n"
        "subprocess.Popen(['bash', '-c', inner])\n"
        "time.sleep(0.5)\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n",
        encoding="utf-8",
    )
    return script, hb, pidf


def _assert_heartbeat_stopped(hb: Path, pidf: Path) -> None:
    deadline = time.time() + 2
    while not hb.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert hb.exists(), "孙进程从未写下心跳"
    before = hb.stat().st_size
    time.sleep(0.4)
    after = hb.stat().st_size
    assert after == before, f"runtime 返回后孙进程仍在写心跳（{before} -> {after} 字节）"


def _kill_heartbeat(pidf: Path) -> None:
    try:
        pid = int(pidf.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------- machine / preflight


def _lite_run(repo, cli):
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    wt, lp = Path(out["worktree"]), Path(out["ledger"])
    (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "# candidate\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    return wt, lp


def _set_stages(lp: Path, *cmds: str, timeout: int = TIMEOUT) -> None:
    with L.mutate(lp) as lg:
        lg["contract"]["assurance"]["machine_commands"] = [
            {"stage": f"s{i}", "cmd": c, "timeout": timeout} for i, c in enumerate(cmds)
        ]


def test_machine_timeout_reaps_descendant_process_group(repo, cli, tmp_path):
    script, hb, pidf = _stage_program(tmp_path)
    _wt, lp = _lite_run(repo, cli)
    _set_stages(lp, f"exec python3 {script}")
    try:
        out = cli("machine", "--session", "S1", expect=1)
        assert out["result"] == "FAIL", out
        assert out["failure"]["timed_out"] is True and out["failure"]["returncode"] == 124, out["failure"]
        assert Path(out["failure"]["log"]).is_file(), out["failure"]
        _assert_heartbeat_stopped(hb, pidf)
    finally:
        _kill_heartbeat(pidf)


def test_preflight_timeout_reaps_descendant_process_group(repo, cli, tmp_path):
    script, hb, pidf = _stage_program(tmp_path)
    _wt, lp = _lite_run(repo, cli)
    _set_stages(lp, f"exec python3 {script}")
    try:
        out = cli("preflight", "--session", "S1")
        assert out["result"] == "INCONCLUSIVE", out
        assert out["baseline_timed_out"] == ["s0"] and out["baseline_red"] == [], out
        _assert_heartbeat_stopped(hb, pidf)
    finally:
        _kill_heartbeat(pidf)


def test_signal_terminated_stage_reaps_descendant_process_group(repo, cli, tmp_path):
    """边界：直接进程被信号终止（非 timeout）时也清理组内残留。"""
    script, hb, pidf = _signal_program(tmp_path)
    _wt, lp = _lite_run(repo, cli)
    _set_stages(lp, f"exec python3 {script}")
    try:
        cli("machine", "--session", "S1", expect=1)
        _assert_heartbeat_stopped(hb, pidf)
    finally:
        _kill_heartbeat(pidf)


# ---------------------------------------------------------------- proof（candidate / baseline / mutation）


def _proof_ctx(repo, cli):
    # machine 用恒过的 stage：本文件只观察 proof 的超时清理，别让心跳测试文件在 machine 里再跑一遍
    (repo.root / ".claude" / "loop.yml").write_text(
        "pass_cmd:\n  - stage: unit\n    cmd: \"python3 -c 'import sys; sys.exit(0)'\"\n    timeout: 60\n"
        "max_iterations: 5\n"
        'proof_runner:\n  framework: pytest\n  cmd: "python3 -m pytest -p no:html"\n',
        encoding="utf-8",
    )
    repo.commit_all("chore(fixture): [cr_id_skip] Trivial machine stage")
    write_plan(repo.root, copy.deepcopy(CONTRACT), "plan-heartbeat.md")
    out = cli("start", "--plan", str(repo.root / "plan-heartbeat.md"), "--session", "S1")
    return {"repo": repo.root, "worktree": Path(out["worktree"]),
            "tester_worktree": Path(out["tester_worktree"]), "ledger": Path(out["ledger"])}


def _heartbeat_body(tmp_path: Path, phase: str) -> str:
    worker, hb, pidf = _worker(tmp_path)
    head = (
        "import subprocess, sys, time\n"
        f"WORKER = {str(worker)!r}\n"
        f"HB = {str(hb)!r}\n"
        f"PID = {str(pidf)!r}\n"
        "def _spawn():\n"
        "    subprocess.Popen([sys.executable, WORKER, HB, PID])\n"
    )
    if phase == "candidate":
        return head + "def test_hb():\n    _spawn()\n    time.sleep(30)\n"
    if phase == "baseline":
        return head + (
            "import src.foo\n\n\n"
            "def test_hb():\n"
            "    if not hasattr(src.foo, 'mul'):\n"
            "        _spawn()\n"
            "        time.sleep(30)\n"
            "    assert src.foo.mul(3, 4) == 12\n"
        )
    if phase == "mutation":
        return head + (
            "from src.foo import mul\n\n\n"
            "def test_hb():\n"
            "    if mul(3, 4) != 12:\n"
            "        _spawn()\n"
            "        time.sleep(30)\n"
            "    assert mul(3, 4) == 12\n"
        )
    raise AssertionError(phase)


def _blind_and_integrate(ctx, cli, hook, body: str, *, kind: str):
    wt, twt = ctx["worktree"], ctx["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    (twt / "tests" / "test_hb.py").write_text(body)
    ids = ["tests/test_hb.py::test_hb"]
    res = make_tester_result(kind, test_ids=ids)
    res["proof_spec"]["groups"][0]["timeout"] = PROOF_GROUP_TIMEOUT
    r = role_turn(hook, "tester", "T1", res, start=False)
    assert r["code"] == 0, r
    cli("integrate", "--session", "S1")
    assert cli("machine", "--session", "S1")["result"] == "PASS"
    return wt, ids


def _give_patch(ctx, hook, wt: Path, ids: list[str]) -> None:
    res = make_tester_result("mutation", mutation_patch(wt), test_ids=ids)
    res["proof_spec"]["groups"][0]["timeout"] = PROOF_GROUP_TIMEOUT
    r = role_turn(hook, "tester", "T1", res)
    assert r["code"] == 0, r


def _assert_proof_timeout(out: dict, phase: str) -> None:
    assert out["result"] == "FAIL", out
    f = out["failure"]
    assert f["code"] == "TEST_PROOF_GROUP_TIMEOUT", f
    assert f["phase"] == phase, f
    assert Path(f["log"]).is_file(), f


def test_proof_candidate_timeout_reaps_descendant_process_group(repo, cli, hook, tmp_path):
    ctx = _proof_ctx(repo, cli)
    _wt, _ids = _blind_and_integrate(ctx, cli, hook, _heartbeat_body(tmp_path, "candidate"), kind="baseline-red")
    _worker_file, hb, pidf = _worker_paths(tmp_path)
    try:
        out = cli("proof", "--session", "S1", expect=1)
        _assert_proof_timeout(out, "candidate")
        _assert_heartbeat_stopped(hb, pidf)
    finally:
        _kill_heartbeat(pidf)


def test_proof_baseline_timeout_reaps_descendant_process_group(repo, cli, hook, tmp_path):
    ctx = _proof_ctx(repo, cli)
    _blind_and_integrate(ctx, cli, hook, _heartbeat_body(tmp_path, "baseline"), kind="baseline-red")
    _worker_file, hb, pidf = _worker_paths(tmp_path)
    try:
        out = cli("proof", "--session", "S1", expect=1)
        _assert_proof_timeout(out, "baseline")
        _assert_heartbeat_stopped(hb, pidf)
    finally:
        _kill_heartbeat(pidf)


def test_proof_mutation_timeout_reaps_descendant_process_group(repo, cli, hook, tmp_path):
    ctx = _proof_ctx(repo, cli)
    wt, ids = _blind_and_integrate(ctx, cli, hook, _heartbeat_body(tmp_path, "mutation"), kind="mutation")
    _give_patch(ctx, hook, wt, ids)
    _worker_file, hb, pidf = _worker_paths(tmp_path)
    try:
        out = cli("proof", "--session", "S1", expect=1)
        _assert_proof_timeout(out, "mutation")
        _assert_heartbeat_stopped(hb, pidf)
    finally:
        _kill_heartbeat(pidf)


def _worker_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """_worker 是纯写入；心跳/pid 路径固定，重新取一次用于断言与清理。"""
    return _worker(tmp_path)


# ---------------------------------------------------------------- 守卫：新模块（不列入 proof_spec）


def test_guard_process_run_kills_group_on_timeout(tmp_path):
    from builder_loop import process as P

    worker, hb, pidf = _worker(tmp_path)
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(worker)!r}, {str(hb)!r}, {str(pidf)!r}])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    log = tmp_path / "log.txt"
    try:
        with open(log, "wb") as out:
            rc, timed_out = P.run(["python3", str(script)], cwd=tmp_path, env=dict(os.environ), timeout=TIMEOUT, stdout=out)
        assert timed_out is True
        assert rc == 124
        _assert_heartbeat_stopped(hb, pidf)
    finally:
        _kill_heartbeat(pidf)


def test_guard_process_run_leaves_background_children_on_normal_exit(tmp_path):
    from builder_loop import process as P

    pidf = tmp_path / "child.pid"
    log = tmp_path / "log.txt"
    with open(log, "wb") as out:
        rc, timed_out = P.run(
            ["bash", "-c", f"sleep 30 & echo $! > {pidf}; exit 0"],
            cwd=tmp_path, env=dict(os.environ), timeout=TIMEOUT, stdout=out,
        )
    assert (rc, timed_out) == (0, False)
    pid = int(pidf.read_text().strip())
    try:
        os.kill(pid, 0)  # 正常退出不新增后台子孙清理：它仍然活着
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_guard_process_run_returns_nonzero_without_timeout(tmp_path):
    from builder_loop import process as P

    log = tmp_path / "log.txt"
    with open(log, "wb") as out:
        rc, timed_out = P.run(["python3", "-c", "import sys; sys.exit(7)"], cwd=tmp_path,
                              env=dict(os.environ), timeout=TIMEOUT, stdout=out)
    assert (rc, timed_out) == (7, False)