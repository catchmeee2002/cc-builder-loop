"""B3/B4：run 定位遇不可读 root 时局部化并如实表达。

B3：run_ledger_roots 返回多个 root，前面若干个的 ledger 路径不可读（PermissionError），最后一个
    可读 root 含目标 run 的 ledger —— `bl brief --run <id> --role tester` 成功（exit 0）并输出
    完整 brief，`unreadable_roots` 列出那些不可读 root 的绝对路径。
B4：所有 root 都没有该 run 的 ledger、其中至少一个不可读 —— 命令非 0，错误码 RUN_NOT_FOUND，
    `details.unreadable_roots` 含不可读 root 的绝对路径，输出不含 Python traceback；
    没有不可读 root 时 `details.unreadable_roots` 是空数组、错误信息给出 searched 列表。

起点上 `_locate_run` 只吞 `Problem`，PermissionError 会一路冒出去（brief 直接 traceback，B4 拿不到
错误码），且 brief 里没有 `unreadable_roots` 字段 —— 下列 proof 用例在 call 阶段失败（baseline-red）。
「只有一个可读 root / 不可读 root 排在可读之后」这类不随修复变化的边界只作守卫，不列入 proof_spec。
"""

from __future__ import annotations

import json
import os

import pytest

from builder_loop import cli as cli_mod
from builder_loop import ledger as L
from builder_loop import run as run_mod


@pytest.fixture
def unreadable_root(tmp_path):
    """返回一个工厂：造出绝对路径 root，其 runs 目录存在但当前用户不可读。"""
    created = []

    def make(name: str):
        root = (tmp_path / name).resolve()
        runs = root / L.RUNS_SUBDIR
        runs.mkdir(parents=True)
        (runs / "some-run").mkdir()
        os.chmod(runs, 0)
        created.append(runs)
        return root

    yield make
    for runs in created:
        try:
            os.chmod(runs, 0o755)
        except OSError:
            pass


# ---------------------------------------------------------------- B3


def test_b3_brief_succeeds_past_unreadable_roots(started, cli, monkeypatch, unreadable_root):
    repo = started["repo"]
    run_id = L.load(started["ledger"])["run_id"]
    first, second = unreadable_root("u1"), unreadable_root("u2")
    monkeypatch.setattr(run_mod, "run_ledger_roots", lambda repo_arg: [first, second, repo.root])

    brief = cli("brief", "--run", run_id, "--role", "tester", "--json")

    assert brief["run_id"] == run_id, brief
    assert brief["role"] == "tester", brief
    assert brief["behaviors"], brief  # 完整 brief：behaviors / mission 都在
    assert set(brief["unreadable_roots"]) == {str(first), str(second)}, brief["unreadable_roots"]


def test_b3_boundary_unreadable_root_after_readable_still_succeeds(started, cli, monkeypatch, unreadable_root):
    """守卫（不列入 proof_spec）：不可读 root 排在可读 root 之后时同样成功。"""
    repo = started["repo"]
    run_id = L.load(started["ledger"])["run_id"]
    later = unreadable_root("late")
    monkeypatch.setattr(run_mod, "run_ledger_roots", lambda repo_arg: [repo.root, later])

    brief = cli("brief", "--run", run_id, "--role", "tester", "--json")

    assert brief["run_id"] == run_id, brief


def test_b3_guard_single_readable_root_has_no_unreadable_roots(started, cli, monkeypatch):
    """守卫（不列入 proof_spec）：只有一个可读 root 时不得报出 unreadable_roots（缺失或为空都合法，
    normal 字段集合不变）。"""
    repo = started["repo"]
    run_id = L.load(started["ledger"])["run_id"]
    monkeypatch.setattr(run_mod, "run_ledger_roots", lambda repo_arg: [repo.root])

    brief = cli("brief", "--run", run_id, "--role", "tester", "--json")

    assert not brief.get("unreadable_roots"), brief.get("unreadable_roots")
    for key in ("run_id", "role", "contract_revision", "mission", "behaviors", "ledger"):
        assert key in brief, (key, sorted(brief))


# ---------------------------------------------------------------- B4


def test_b4_missing_run_reports_unreadable_roots(repo, monkeypatch, capsys, unreadable_root):
    bad = unreadable_root("bad")
    monkeypatch.setattr(run_mod, "run_ledger_roots", lambda repo_arg: [bad, repo.root])

    rc = cli_mod.main(["--repo", str(repo.root), "brief", "--run", "no-such-run", "--role", "tester"])
    captured = capsys.readouterr()

    assert rc != 0, captured
    assert "Traceback" not in captured.err, captured.err
    payload = json.loads(captured.out)
    assert payload["ok"] is False, payload
    assert payload["code"] == "RUN_NOT_FOUND", payload
    assert set(payload["details"]["unreadable_roots"]) == {str(bad)}, payload["details"]
    assert str(repo.root) in payload["details"]["searched"], payload["details"]


def test_b4_boundary_no_unreadable_roots_empty_list(repo, monkeypatch, capsys):
    monkeypatch.setattr(run_mod, "run_ledger_roots", lambda repo_arg: [repo.root])

    rc = cli_mod.main(["--repo", str(repo.root), "brief", "--run", "no-such-run", "--role", "tester"])
    captured = capsys.readouterr()

    assert rc != 0, captured
    assert "Traceback" not in captured.err, captured.err
    payload = json.loads(captured.out)
    assert payload["code"] == "RUN_NOT_FOUND", payload
    assert payload["details"]["unreadable_roots"] == [], payload["details"]
    assert payload["details"]["searched"] == [str(repo.root)], payload["details"]