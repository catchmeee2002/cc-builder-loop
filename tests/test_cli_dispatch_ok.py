"""B1：`ok` 字段由进程内的 cli.dispatch 统一补全，而不是只由 main 写 stdout 时补。

行为变更前 `dispatch` 返回的 dict 里没有 `ok`，`main` 在 `_emit` 前才 `{"ok": ..., **out}`；
变更后 `dispatch`（包装 `_route`）自己补 `ok`，`main` 不再补。所以同一段判断逻辑在两种调用面上
（进程内 `dispatch`、子进程 `python -m builder_loop`）输出形态一致。

本文件只用 conftest 的 `repo` / `cli` fixture：进程内调用一律走 `cli`，不另造平行实现。
需要构造 `_route` 的各退出码分支时 monkeypatch `builder_loop.cli._route`（契约明确允许）。

proof kind = mutation：B1 的核心是新增内部路由 `_route` 与包装后的 `dispatch` 形态，起点上没有
`_route`，baseline-red 会因缺属性而「红得没有意义」，所以本组统一用 mutation 证明鉴别力。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import RUNTIME
from builder_loop.jsonutil import dumps


def _patch_route(monkeypatch, result, code):
    """把内部路由 `_route` 替换成固定返回值，构造 dispatch 包装层的各退出码分支。"""
    import builder_loop.cli as cli_mod

    def fake_route(args):
        return result, code

    monkeypatch.setattr(cli_mod, "_route", fake_route)


def _run_bl(repo, *args: str) -> subprocess.CompletedProcess:
    """真实子进程：经 cli.main 写 stdout，验证进程外形态。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(RUNTIME) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(
        [sys.executable, "-m", "builder_loop", "--repo", str(repo.root), *args],
        cwd=str(repo.root), capture_output=True, text=True, env=env,
    )


# ------------------------------------------------------------------ 主行为


def test_dispatch_inprocess_runs_dict_has_ok_true(cli):
    """进程内 dispatch 返回的第一项是 dict 且含 ok=True，第二项退出码为 0。"""
    out = cli("runs")
    assert isinstance(out, dict), out
    assert out["ok"] is True, out
    assert isinstance(out["runs"], list), out


def test_route_exit_code_1_sets_ok_true(cli, monkeypatch):
    """边界：`_route` 返回 code=1 时沿用 main 的 `code in (0, 1)` 判定 → ok 为 True。"""
    _patch_route(monkeypatch, {"x": 1}, 1)
    out = cli("runs", expect=1)
    assert out == {"ok": True, "x": 1}, out


def test_route_exit_code_2_sets_ok_false(cli, monkeypatch):
    """边界：`_route` 返回 code=2 → ok 为 False。"""
    _patch_route(monkeypatch, {"x": 1}, 2)
    out = cli("runs", expect=2)
    assert out == {"ok": False, "x": 1}, out


def test_route_exit_code_3_sets_ok_false(cli, monkeypatch):
    """边界：`_route` 返回 code=3 → ok 为 False。"""
    _patch_route(monkeypatch, {"x": 1}, 3)
    out = cli("runs", expect=3)
    assert out == {"ok": False, "x": 1}, out


# ------------------------------------------------------------------ 边界


def test_existing_ok_is_not_overwritten(cli, monkeypatch):
    """边界：dict 结果已自带 ok 键时保持其原值，dispatch 不覆盖。"""
    _patch_route(monkeypatch, {"ok": False, "x": 1}, 0)
    out = cli("runs", expect=0)
    assert out == {"ok": False, "x": 1}, out


def test_hook_tuple_result_is_not_augmented(cli, monkeypatch):
    """边界：hook 子命令 dispatch 返回的第一项仍是 (stdout, stderr) 元组，不被加字段。"""
    _patch_route(monkeypatch, ("stdout-text", "stderr-text"), 0)
    out = cli("hook", "PreToolUse", expect=0)
    assert isinstance(out, tuple), out
    assert out == ("stdout-text", "stderr-text"), out


def test_brief_text_result_is_not_wrapped(cli, monkeypatch):
    """边界：brief 的文本形态（返回 str）原样返回，不被包装成 dict。"""
    _patch_route(monkeypatch, "plain brief text", 0)
    out = cli("brief", "--role", "tester", expect=0)
    assert isinstance(out, str), out
    assert out == "plain brief text", out


def test_problem_is_not_swallowed(cli, monkeypatch):
    """边界：Problem 异常路径不变——dispatch 仍把它抛给调用方（由调用方 to_json）。"""
    import builder_loop.cli as cli_mod
    from builder_loop.errors import Problem

    def boom(args):
        raise Problem("BOOM", "boom-message", exit_code=2)

    monkeypatch.setattr(cli_mod, "_route", boom)
    out = cli("runs", expect=2)
    assert out["code"] == "BOOM", out
    assert out["ok"] is False, out


def test_ok_key_is_first_and_other_keys_keep_order(cli, monkeypatch):
    """边界：补全后的键顺序是 ok 最前，其余键保持原顺序。"""
    _patch_route(monkeypatch, {"x": 1, "y": 2}, 0)
    out = cli("runs", expect=0)
    assert list(out) == ["ok", "x", "y"], out


# ------------------------------------------------------------------ 进程外一致性


def test_subprocess_runs_stdout_matches_route_augmented_dumps(repo):
    """不变量：子进程 `python -m builder_loop runs` 的 stdout 就是 `dumps({"ok": True, **route})`，
    即 main 不再自己补 ok，也不会二次改写 dispatch 的返回值。"""
    from builder_loop.cli import build_parser
    import builder_loop.cli as cli_mod

    ns = build_parser().parse_args(["--repo", str(repo.root), "runs"])
    raw, code = cli_mod._route(ns)
    assert code == 0, code
    proc = _run_bl(repo, "runs")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.rstrip("\n") == dumps({"ok": True, **raw}), proc.stdout


def test_subprocess_runs_stdout_has_ok_first_and_runs(repo):
    """不变量：进程外 `runs` 输出 JSON 含 ok（在最前）与 runs，形态与进程内一致。"""
    proc = _run_bl(repo, "runs")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True, out
    assert isinstance(out["runs"], list), out
    assert list(out)[0] == "ok", out