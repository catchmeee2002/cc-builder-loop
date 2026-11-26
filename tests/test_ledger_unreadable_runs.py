"""B5：runs 目录里不可读的 ledger 不拖垮整个列表。

given runs 目录下某子目录的 ledger.json 存在但当前用户不可读 / when 调用 `ledger.list_runs`
或执行 `bl runs` / then 不抛异常；该 run 以 unreadable 标记出现或按既有约定跳过，其余可读 run
正常列出。

起点上 `list_runs` 直接 `lp.is_file()`：EACCES 抛 PermissionError 冒出去，整条列表挂掉 —— 下列
proof 用例在 call 阶段失败（baseline-red）。「runs 目录不存在返回空列表」不随修复变化，只作守卫。
"""

from __future__ import annotations

import os

from builder_loop import ledger as L

READABLE_FIELDS = {
    "run_id", "schema", "legacy", "terminal", "retro_pending",
    "owner_session_id", "candidate_head", "ledger_path",
}


def _make_valid_run(repo_root, run_id: str) -> None:
    L.create(L.ledger_path(repo_root, run_id), L.new_ledger(run_id=run_id))


def test_b5_unreadable_run_does_not_break_listing(repo, cli):
    _make_valid_run(repo.root, "run-ok")
    bad = repo.root / L.RUNS_SUBDIR / "run-bad"
    bad.mkdir(parents=True)
    (bad / "ledger.json").write_text("{}\n", encoding="utf-8")
    os.chmod(bad, 0)
    try:
        runs = L.list_runs(repo.root)
        by_id = {r["run_id"]: r for r in runs}
        assert "run-ok" in by_id, by_id
        assert set(by_id["run-ok"]) == READABLE_FIELDS, by_id["run-ok"]
        # 不可读的那条：带 unreadable 标记出现，或按既有约定跳过；两种都不算错
        if "run-bad" in by_id:
            assert by_id["run-bad"].get("unreadable") is True, by_id["run-bad"]

        out = cli("runs")
        ids = {r["run_id"] for r in out["runs"]}
        assert "run-ok" in ids, ids
        if "run-bad" in ids:
            entry = next(r for r in out["runs"] if r["run_id"] == "run-bad")
            assert entry.get("unreadable") is True, entry
    finally:
        os.chmod(bad, 0o755)


def test_guard_missing_runs_dir_returns_empty(repo):
    """守卫（不列入 proof_spec）：runs 目录不存在时返回空列表，行为不变。"""
    assert not (repo.root / L.RUNS_SUBDIR).exists()
    assert L.list_runs(repo.root) == []