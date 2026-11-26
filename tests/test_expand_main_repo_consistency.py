"""B1/B2：`{main_repo}` 展开收敛到一处实现。

B1：machine 执行 stage（run_machine / run_preflight）时，`cmd` / `ready_cmd` 里的 `{main_repo}`
    被替换成主仓根的绝对路径，命令串不再含字面占位符；不含占位符时原文透传；主仓路径含空格时
    逐字替换（不做额外 shell 转义）。
B2：同一个 cmd 与同一个主仓根，machine 侧与 proof 侧各自展开得到的字符串完全相同；两侧都
    不含占位符时结果都是原文。

B1/B2 的 cmd 展开是本次新增的 machine 侧行为，起点的 `_run_stage` 根本没有 main_repo 参数、
也不会替换占位符，所以下列 proof 用例在 call 阶段失败（baseline-red）。
「proof_runner.cmd 展开结果不变」只作守卫，不列入 proof_spec。

主路径（run_preflight）只用公开命令与「交给 bash 的 argv」做断言；含空格的主仓路径只能由
`_run_stage(cwd, stage, log_path, env, main_repo)`（contract 明确列出的接口）直接驱动。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from builder_loop import ledger as L
from builder_loop import machine as M
from builder_loop import proof as P
from builder_loop import worktree as W

from conftest import contract_with, write_plan


def _fake_process_run(monkeypatch):
    """不真的起进程，只记录交给 bash 的完整 argv。"""
    seen: list[list[str]] = []

    def fake(argv, *, cwd, env, timeout, stdout):
        seen.append(list(argv))
        return 0, False

    monkeypatch.setattr(M.process, "run", fake)
    return seen


def _record_process_run(monkeypatch):
    """记录 argv，然后照常执行（preflight 会用）。"""
    seen: list[list[str]] = []
    real = M.process.run

    def wrapper(argv, **kwargs):
        seen.append(list(argv))
        return real(argv, **kwargs)

    monkeypatch.setattr(M.process, "run", wrapper)
    return seen


def _bash_commands(seen: list[list[str]]) -> list[str]:
    return [argv[2] for argv in seen if len(argv) == 3 and argv[0] == "bash" and argv[1] == "-c"]


def _stage(cmd: str, *, ready_cmd: str | None = None) -> dict:
    stage = {"stage": "t", "cmd": cmd, "timeout": 10}
    if ready_cmd is not None:
        stage["ready_cmd"] = ready_cmd
    return stage


def _start_lite_run(repo, cli, commands: list[dict]) -> Path:
    """起一个只要求 machine/reviewer 的 run，并把 machine_commands 写成 given 的样子。"""
    write_plan(repo.root, contract_with(**{"assurance.required": ["machine", "reviewer"]}), "lite.md")
    out = cli("start", "--plan", str(repo.root / "lite.md"), "--session", "S1")
    lp = Path(out["ledger"])
    with L.mutate(lp) as lg:
        lg["contract"]["assurance"]["machine_commands"] = commands
    return lp


def _capture_snapshot_file(monkeypatch, name: str) -> dict:
    """preflight 的临时 worktree 被清理前，记录其中某个文件的内容（返回 holder dict）。"""
    holder: dict[str, str | None] = {}
    real = W.temp_worktree

    @contextmanager
    def wrapper(*args, **kwargs):
        with real(*args, **kwargs) as path:
            try:
                yield path
            finally:
                target = path / name
                holder["content"] = target.read_text(encoding="utf-8") if target.exists() else None

    monkeypatch.setattr(W, "temp_worktree", wrapper)
    return holder


# ---------------------------------------------------------------- B1


def test_b1_preflight_expands_ready_cmd_and_cmd(repo, cli, monkeypatch):
    """preflight 走真实路径：ready_cmd 与 cmd 里的占位符都被替换。

    探针 `test -e {main_repo}/ready.ok` 未展开则不通过 → stage 被归为 unready、cmd 根本不跑；
    cmd 未展开则 cmd_out.txt 会是字面占位符。既看交给 bash 的 argv，也看 cmd 的副作用。
    """
    (repo.root / "ready.ok").write_text("", encoding="utf-8")
    _start_lite_run(repo, cli, [
        {"stage": "t", "cmd": "echo {main_repo}/cmd > cmd_out.txt", "timeout": 10,
         "ready_cmd": "test -e {main_repo}/ready.ok"},
    ])
    holder = _capture_snapshot_file(monkeypatch, "cmd_out.txt")
    seen = _record_process_run(monkeypatch)

    out = cli("preflight", "--session", "S1")

    assert out["result"] == "GREEN", out
    assert out["baseline_unready"] == [], out
    commands = _bash_commands(seen)
    assert f"test -e {repo.root}/ready.ok" in commands, commands
    assert f"echo {repo.root}/cmd > cmd_out.txt" in commands, commands
    assert not any("{main_repo}" in c for c in commands), commands
    assert (holder.get("content") or "").strip() == f"{repo.root}/cmd", holder


def test_b1_stage_cmd_expansion_and_boundaries(tmp_path, monkeypatch):
    """`_run_stage` 交给 bash 的命令串：占位符展开 + 无占位符透传 + 主仓路径含空格逐字替换。"""
    seen = _fake_process_run(monkeypatch)
    main_repo = tmp_path / "main_repo"
    spaced = tmp_path / "main repo with space"
    spaced.mkdir()

    M._run_stage(
        tmp_path, _stage("{main_repo}/.venv/bin/python -c 'pass'"),
        tmp_path / "stage1.log", dict(os.environ), main_repo,
    )
    M._run_stage(tmp_path, _stage("echo hi"), tmp_path / "stage2.log", dict(os.environ), main_repo)
    M._run_stage(tmp_path, _stage("{main_repo}/x"), tmp_path / "stage3.log", dict(os.environ), spaced)

    assert seen[0] == ["bash", "-c", f"{main_repo}/.venv/bin/python -c 'pass'"], seen[0]
    assert "{main_repo}" not in seen[0][2], seen[0][2]
    assert seen[1] == ["bash", "-c", "echo hi"], seen[1]
    assert seen[2] == ["bash", "-c", f"{spaced}/x"], seen[2]
    # 含空格的主仓路径逐字替换：不额外引号 / 转义
    assert "'" not in seen[2][2] and '"' not in seen[2][2], seen[2][2]


def test_guard_proof_runner_cmd_expansion_unchanged(tmp_path):
    """守卫（不列入 proof_spec）：proof_runner.cmd 的 `{main_repo}` 展开结果不变。"""
    main_repo = tmp_path / "main_repo"
    argv = P.build_argv(
        {"framework": "pytest", "cmd": "{main_repo}/.venv/bin/py -m pytest"},
        ["tests/test_x.py::test_a"], tmp_path / "junit.xml", main_repo,
    )
    assert argv[:3] == [f"{main_repo}/.venv/bin/py", "-m", "pytest"], argv


# ---------------------------------------------------------------- B2


def test_b2_machine_and_proof_expand_identically(repo, cli, monkeypatch):
    """同一 cmd / 同一主仓根：machine 侧（交给 bash 的命令串）与 proof 侧展开结果相同；
    两侧都无占位符时都是原文。"""
    _start_lite_run(repo, cli, [
        {"stage": "placeholder", "cmd": "{main_repo}/x", "timeout": 10},
        {"stage": "plain", "cmd": "true", "timeout": 10},
    ])
    seen = _record_process_run(monkeypatch)

    cli("preflight", "--session", "S1")  # 基线红与否不重要，只看交给 bash 的 argv

    commands = _bash_commands(seen)
    machine_cmd = f"{repo.root}/x"
    proof_argv = P.build_argv({"framework": "generic", "cmd": "{main_repo}/x"}, [], repo.root / "junit.xml", repo.root)
    assert machine_cmd in commands, commands
    assert machine_cmd == proof_argv[0], (machine_cmd, proof_argv)

    # 边界：两侧都不含占位符 → 都是原文
    plain_proof = P.build_argv({"framework": "generic", "cmd": "true"}, [], repo.root / "junit.xml", repo.root)
    assert "true" in commands, commands
    assert plain_proof == ["true"], plain_proof