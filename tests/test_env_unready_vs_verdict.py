"""env-unready-vs-verdict：stage 可声明 ready_cmd 探针；环境未就绪不再被记成判据结果。

B1：loop.yml 的 stage 可带 ready_cmd；Stage.to_json 只在声明时带该键，空串 / 非字符串是配置错误。
B2：preflight 里 ready_cmd 不通过（非 0 / 超时）→ INCONCLUSIVE + baseline_unready，不跑 stage.cmd。
B3：machine 里 ready_cmd 不通过 → negative MACHINE_STAGE_UNREADY，不记 machine_iter / failures / evidence。
B4：proof_runner 冒烟超时（TimeoutExpired）与「起不来」（OSError / 可执行文件缺失）走不同消息。
B5：preflight 快照创建失败 → INCONCLUSIVE + snapshot_error，不抛硬错、不留半成品快照目录。

证明名单见交卷的 proof_spec。文件名以 test_ 开头的 guard 用例只作集成后的回归防护，不列入
proof_spec（它们在改动前就成立，反例下不会变红）。
"""

from __future__ import annotations

import re
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from builder_loop import ledger as L
from builder_loop import machine as M
from builder_loop import proof as P
from builder_loop import worktree as W
from builder_loop.config import load_loop_config
from builder_loop.errors import Problem, fatal
from conftest import contract_with, write_plan

LOOP = Path(".claude") / "loop.yml"
LITE_REQUIRED = ["machine", "reviewer"]
PASS_CMD = "python3 -c 'import sys; sys.exit(0)'"
FAIL_CMD = "python3 -c 'import sys; sys.exit(1)'"
RUNNER = {"framework": "pytest", "cmd": "python3 -m pytest"}


def _start_run(repo, cli, commands=None, *, checkpoint=False, session="S1"):
    """起一个只要求 machine/reviewer 的 run；可把 machine_commands 直接写成 given 的样子。"""
    write_plan(repo.root, contract_with(**{"assurance.required": list(LITE_REQUIRED)}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", session)
    wt, lp = Path(out["worktree"]), Path(out["ledger"])
    if commands is not None:
        with L.mutate(lp) as lg:
            lg["contract"]["assurance"]["machine_commands"] = commands
    if checkpoint:
        (wt / "src" / "foo.py").write_text((wt / "src" / "foo.py").read_text() + "\n# candidate\n", encoding="utf-8")
        cli("checkpoint", "--session", session, "--role", "builder")
    return wt, lp


def _observe_snapshot(monkeypatch):
    """在 preflight 的临时 worktree 被清理前，记录其中有没有 stage.cmd 写下的 ran.txt。"""
    holder: dict = {}
    real = W.temp_worktree

    @contextmanager
    def wrapper(*args, **kwargs):
        with real(*args, **kwargs) as path:
            try:
                yield path
            finally:
                holder["ran"] = (path / "ran.txt").exists()

    monkeypatch.setattr(W, "temp_worktree", wrapper)
    return holder


# ---------------------------------------------------------------- B1


def test_b1_ready_cmd_roundtrips_and_absent_keeps_three_keys(repo):
    (repo.root / LOOP).write_text(
        "pass_cmd:\n"
        "  - stage: lint\n    cmd: \"true\"\n    timeout: 5\n    ready_cmd: \"command -v tsx\"\n"
        "  - stage: test\n    cmd: \"true\"\n    timeout: 7\n",
        encoding="utf-8",
    )
    cfg = load_loop_config(repo.root)
    with_probe, without = cfg.pass_cmd[0].to_json(), cfg.pass_cmd[1].to_json()
    assert with_probe.get("ready_cmd") == "command -v tsx", with_probe
    assert set(without) == {"stage", "cmd", "timeout"}, without
    assert without == {"stage": "test", "cmd": "true", "timeout": 7}, without


def test_b1_empty_ready_cmd_rejected(repo):
    (repo.root / LOOP).write_text(
        "pass_cmd:\n  - stage: test\n    cmd: \"true\"\n    ready_cmd: \"\"\n",
        encoding="utf-8",
    )
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_PASS_CMD_INVALID", ei.value.code
    assert ei.value.exit_code == 2, ei.value.exit_code


def test_b1_non_string_ready_cmd_rejected(repo):
    (repo.root / LOOP).write_text(
        "pass_cmd:\n  - stage: test\n    cmd: \"true\"\n    ready_cmd: 5\n",
        encoding="utf-8",
    )
    with pytest.raises(Problem) as ei:
        load_loop_config(repo.root)
    assert ei.value.code == "CONFIG_PASS_CMD_INVALID", ei.value.code
    assert ei.value.exit_code == 2, ei.value.exit_code


def test_b1_guard_no_ready_cmd_frozen_commands_unchanged(repo, cli):
    """不变量：没有 ready_cmd 的 loop.yml 冻结出的 machine_commands 逐字不变。"""
    (repo.root / LOOP).write_text(
        "pass_cmd:\n"
        "  - stage: lint\n    cmd: \"true\"\n    timeout: 5\n"
        "  - stage: test\n    cmd: \"true\"\n    timeout: 7\n",
        encoding="utf-8",
    )
    repo.commit_all()
    write_plan(repo.root, contract_with(**{"assurance.required": list(LITE_REQUIRED)}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    lg = L.load(Path(out["ledger"]))
    assert lg["contract"]["assurance"]["machine_commands"] == [
        {"stage": "lint", "cmd": "true", "timeout": 5},
        {"stage": "test", "cmd": "true", "timeout": 7},
    ]


# ---------------------------------------------------------------- B2


def test_b2_preflight_probe_failure_is_inconclusive_and_cmd_not_run(repo, cli, monkeypatch):
    _wt, lp = _start_run(
        repo, cli,
        [{"stage": "test", "cmd": "echo ran > ran.txt", "timeout": 10, "ready_cmd": "false"}],
    )
    holder = _observe_snapshot(monkeypatch)
    out = cli("preflight", "--session", "S1")  # 永远 exit 0
    assert out["result"] == "INCONCLUSIVE", out
    assert "test" in out["baseline_unready"], out
    assert "test" not in out["baseline_red"], out
    assert holder.get("ran") is False, holder  # 快照里没有 ran.txt：cmd 没跑
    assert M.baseline_unready(L.load(lp)) == ["test"]


def test_b2_preflight_probe_timeout_is_unready(repo, cli):
    slow_probe = "exec python3 -c 'import time; time.sleep(20)'"
    _wt, lp = _start_run(
        repo, cli,
        [{"stage": "test", "cmd": PASS_CMD, "timeout": 1, "ready_cmd": slow_probe}],
    )
    out = cli("preflight", "--session", "S1")
    assert out["result"] == "INCONCLUSIVE", out
    assert "test" in out["baseline_unready"], out
    assert "test" not in out["baseline_red"], out
    # 探针超时只归 unready：派生视图不得把它记成 stage 超时 / 信号 / 红
    assert out["baseline_timed_out"] == [], out
    lg = L.load(lp)
    assert M.baseline_unready(lg) == ["test"]
    assert M.baseline_timed_out(lg) == []
    assert M.baseline_signaled(lg) == []
    assert M.baseline_red(lg) == []


def test_b2_preflight_probe_signal_is_unready_not_signaled(repo, cli):
    """探针被信号带走同样只归 unready：派生视图不得把它记成 stage 被信号终止。"""
    signaled_probe = "exec python3 -c 'import os, signal; os.kill(os.getpid(), signal.SIGTERM)'"
    _wt, lp = _start_run(
        repo, cli,
        [{"stage": "test", "cmd": PASS_CMD, "timeout": 10, "ready_cmd": signaled_probe}],
    )
    out = cli("preflight", "--session", "S1")
    assert out["result"] == "INCONCLUSIVE", out
    assert "test" in out["baseline_unready"], out
    assert out["baseline_signaled"] == [], out
    assert out["baseline_timed_out"] == [], out
    lg = L.load(lp)
    assert M.baseline_unready(lg) == ["test"]
    assert M.baseline_signaled(lg) == []
    assert M.baseline_timed_out(lg) == []
    assert M.baseline_red(lg) == []


def test_b2_preflight_mixed_unready_and_red_classified_separately(repo, cli):
    _wt, _lp = _start_run(
        repo, cli,
        [
            {"stage": "red", "cmd": FAIL_CMD, "timeout": 10},
            {"stage": "unready", "cmd": PASS_CMD, "timeout": 10, "ready_cmd": "false"},
        ],
    )
    out = cli("preflight", "--session", "S1")
    assert out["baseline_red"] == ["red"], out
    assert "unready" in out["baseline_unready"], out
    assert "red" not in out["baseline_unready"], out


def test_b2_guard_probe_pass_runs_cmd_and_classifies_red(repo, cli):
    """探针 exit 0 时照常跑 cmd 并按原规则分类（现役行为的回归防护）。"""
    _wt, _lp = _start_run(
        repo, cli,
        [{"stage": "test", "cmd": FAIL_CMD, "timeout": 10, "ready_cmd": "true"}],
    )
    out = cli("preflight", "--session", "S1")
    assert out["result"] == "RED", out
    assert out["baseline_red"] == ["test"], out
    assert out["baseline_unready"] == [], out


def test_b2_guard_no_ready_cmd_classification_unchanged(repo, cli):
    """不变量：没有 ready_cmd 的 stage 的红分类不变，也不进 baseline_unready。"""
    _wt, _lp = _start_run(repo, cli, [{"stage": "test", "cmd": FAIL_CMD, "timeout": 10}])
    out = cli("preflight", "--session", "S1")
    assert out["result"] == "RED", out
    assert out["baseline_red"] == ["test"], out
    assert out["baseline_unready"] == [], out


# ---------------------------------------------------------------- B3


def _unready_events(lg):
    return [e for e in lg["events"] if e.get("kind") == "machine_stage_unready"]


def test_b3_machine_probe_failure_negative_without_iteration(repo, cli):
    _wt, lp = _start_run(
        repo, cli,
        [{"stage": "test", "cmd": PASS_CMD, "timeout": 30, "ready_cmd": "false"}],
        checkpoint=True,
    )
    before = L.load(lp)
    out = cli("machine", "--session", "S1", expect=1)

    assert out["code"] == "MACHINE_STAGE_UNREADY", out
    assert out["exit_code"] == 1, out
    assert "false" in out["message"], out["message"]  # 探针命令
    log_paths = re.findall(r"/[^\s\"'`]+\.log", out["message"])  # 日志路径
    assert log_paths and any(Path(p).exists() for p in log_paths), (out["message"], log_paths)

    after = L.load(lp)
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    assert after["failures"]["machine"] == before["failures"]["machine"]
    assert after["evidence"]["machine"] == before["evidence"]["machine"]
    events = _unready_events(after)
    assert len(events) == 1 and events[0].get("stage") == "test", events


def test_b3_machine_later_stage_probe_failure(repo, cli):
    _wt, lp = _start_run(
        repo, cli,
        [
            {"stage": "first", "cmd": PASS_CMD, "timeout": 30},
            {"stage": "second", "cmd": PASS_CMD, "timeout": 30, "ready_cmd": "false"},
        ],
        checkpoint=True,
    )
    before = L.load(lp)
    out = cli("machine", "--session", "S1", expect=1)
    assert out["code"] == "MACHINE_STAGE_UNREADY", out
    after = L.load(lp)
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    events = _unready_events(after)
    assert len(events) == 1 and events[0].get("stage") == "second", events


def test_b3_machine_probe_failure_rerun_same_error(repo, cli):
    _wt, lp = _start_run(
        repo, cli,
        [{"stage": "test", "cmd": PASS_CMD, "timeout": 30, "ready_cmd": "false"}],
        checkpoint=True,
    )
    before = L.load(lp)
    first = cli("machine", "--session", "S1", expect=1)
    second = cli("machine", "--session", "S1", expect=1)
    assert first["code"] == second["code"] == "MACHINE_STAGE_UNREADY", (first, second)

    after = L.load(lp)
    assert after["counters"]["machine_iter"] == before["counters"]["machine_iter"]
    assert after["failures"]["machine"] == []
    assert after["evidence"]["machine"] is None
    assert len(_unready_events(after)) == 2, _unready_events(after)


def test_b3_guard_probe_pass_behavior_unchanged(repo, cli):
    """探针通过时 machine 行为与改动前一致：照常执行、记 evidence / failures / machine_iter。"""
    _wt, lp = _start_run(
        repo, cli,
        [{"stage": "test", "cmd": FAIL_CMD, "timeout": 30, "ready_cmd": "true"}],
        checkpoint=True,
    )
    out = cli("machine", "--session", "S1", expect=1)
    assert out["result"] == "FAIL", out
    assert out["failure"]["stage"] == "test", out["failure"]
    after = L.load(lp)
    assert after["counters"]["machine_iter"] == 1
    assert after["evidence"]["machine"]["status"] == "fail"


def test_b3_guard_no_ready_cmd_pass_unchanged(repo, cli):
    """不变量：没有 ready_cmd 的 stage 通过行为不变。"""
    _wt, _lp = _start_run(repo, cli, [{"stage": "test", "cmd": PASS_CMD, "timeout": 30}], checkpoint=True)
    out = cli("machine", "--session", "S1")
    assert out["result"] == "PASS", out


# ---------------------------------------------------------------- B4


def test_b4_smoke_timeout_reports_smoke_timeout_not_startup_failure(repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="python3 -m pytest", timeout=120)

    monkeypatch.setattr(P.subprocess, "run", boom)
    with pytest.raises(Problem) as ei:
        P.smoke_runner(dict(RUNNER), repo.root)
    assert ei.value.code == "PROOF_RUNNER_UNAVAILABLE", ei.value.code
    assert "冒烟超时" in ei.value.message, ei.value.message
    assert "起不来" not in ei.value.message, ei.value.message
    hint = ei.value.details.get("hint") or ""
    assert "120" in hint, hint
    assert "再跑" in hint or "重跑" in hint, hint


def test_b4_guard_oserror_still_reports_startup_failure(repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("cannot exec")

    monkeypatch.setattr(P.subprocess, "run", boom)
    with pytest.raises(Problem) as ei:
        P.smoke_runner(dict(RUNNER), repo.root)
    assert ei.value.code == "PROOF_RUNNER_UNAVAILABLE", ei.value.code
    assert "起不来" in ei.value.message, ei.value.message
    assert "冒烟超时" not in ei.value.message, ei.value.message


def test_b4_guard_missing_executable_reports_unavailable(repo):
    runner = {"framework": "pytest", "cmd": "bl-no-such-runner-xyz -m pytest"}
    with pytest.raises(Problem) as ei:
        P.smoke_runner(runner, repo.root)
    assert ei.value.code == "PROOF_RUNNER_UNAVAILABLE", ei.value.code
    assert "bl-no-such-runner-xyz" in ei.value.message, ei.value.message


def test_b4_guard_non_5_exit_unchanged(repo, monkeypatch):
    def fake(*_args, **_kwargs):
        return subprocess.CompletedProcess(["python3"], 1, "", "plugins broken")

    monkeypatch.setattr(P.subprocess, "run", fake)
    with pytest.raises(Problem) as ei:
        P.smoke_runner(dict(RUNNER), repo.root)
    assert ei.value.code == "PROOF_RUNNER_UNAVAILABLE", ei.value.code
    assert "退出码 1" in ei.value.message, ei.value.message
    assert "冒烟超时" not in ei.value.message, ei.value.message


# ---------------------------------------------------------------- B5


def test_b5_snapshot_creation_failure_is_inconclusive_and_cleans_up(repo, cli, monkeypatch):
    _wt, _lp = _start_run(repo, cli, [{"stage": "test", "cmd": PASS_CMD, "timeout": 10}])
    parent = Path(repo.root).parent
    assert list(parent.glob(".bl-preflight-*")) == []

    def boom(repo_root, head, base_dir, name, overlay=None):  # noqa: ARG001
        partial = Path(base_dir) / name
        partial.mkdir(parents=True, exist_ok=True)
        (partial / "half.txt").write_text("x", encoding="utf-8")
        raise fatal("GIT_COMMAND_FAILED", "快照父目录不可写")

    monkeypatch.setattr(W, "temp_worktree", boom)
    out = cli("preflight", "--session", "S1")  # 不抛硬错：永远 exit 0

    assert out["result"] == "INCONCLUSIVE", out
    assert out["baseline_red"] == [], out
    assert "不可写" in (out.get("snapshot_error") or ""), out
    assert list(parent.glob(".bl-preflight-*")) == [], list(parent.glob(".bl-preflight-*"))


def test_b5_guard_successful_snapshot_behavior_unchanged(repo, cli):
    """不变量：快照创建成功时 preflight 的分类不变。"""
    _wt, _lp = _start_run(repo, cli, [{"stage": "test", "cmd": FAIL_CMD, "timeout": 10}])
    out = cli("preflight", "--session", "S1")
    assert out["result"] == "RED", out
    assert out["baseline_red"] == ["test"], out
    assert out.get("snapshot_error") is None, out