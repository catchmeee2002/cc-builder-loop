"""B6：retro.signals 在「可加速但用户没配置」时给出 config_hints。

given loop.yml 未显式设置 concurrency.proof_groups、ledger 的 proof evidence 中 groups 数量 >= 2 /
when 调 retro.signals / then 顶层 config_hints 是数组，含 `key` 为 concurrency.proof_groups 的项。
起点返回的 dict 没有 config_hints，取它会 KeyError → call 阶段失败（baseline-red）。

边界（显式设置 proof_groups 或 groups < 2 时不提示）与不变量（signals 列表内容不变；retro record 的
dispositions 覆盖校验只覆盖 signals）作为守卫留在同一文件，不列入 proof group。
"""

from __future__ import annotations

from pathlib import Path

from builder_loop import ledger as L
from builder_loop import retro

from keypath_helpers import loop_yml, start_with_loop

HINT_KEY = "concurrency.proof_groups"


def _abandon(cli) -> None:
    cli("abandon", "--session", "S1", "--reason", "测试 retro config_hints")


def _inject_proof_groups(ledger_path: Path, n: int) -> None:
    with L.mutate(ledger_path) as lg:
        lg["evidence"]["proof"] = {
            "status": "pass",
            "at": L.now_iso(),
            "candidate_head": lg["candidate"].get("head"),
            "agent_id": None,
            "details": {"attempt": 1, "groups": [{"behavior_id": f"B{i + 1}"} for i in range(n)]},
            "dependency_digest": "injected",
        }


def _hint_keys(out: dict) -> list[str]:
    return [h.get("key") for h in out["config_hints"] if isinstance(h, dict)]


# ---------------------------------------------------------------- 正向（baseline-red）


def test_b6_config_hint_for_proof_groups(started, cli):
    _abandon(cli)
    _inject_proof_groups(started["ledger"], 2)
    out = retro.signals(started["ledger"])
    assert isinstance(out["config_hints"], list), out
    assert HINT_KEY in _hint_keys(out), out["config_hints"]


# ---------------------------------------------------------------- 边界（守卫）


def test_b6_boundary_explicit_proof_groups_has_no_hint(repo, cli):
    ctx = start_with_loop(repo, cli, loop_yml(proof_groups=2))
    _abandon(cli)
    _inject_proof_groups(ctx["ledger"], 2)
    out = retro.signals(ctx["ledger"])
    assert HINT_KEY not in _hint_keys(out), out["config_hints"]


def test_b6_boundary_single_proof_group_has_no_hint(started, cli):
    _abandon(cli)
    _inject_proof_groups(started["ledger"], 1)
    out = retro.signals(started["ledger"])
    assert HINT_KEY not in _hint_keys(out), out["config_hints"]


# ---------------------------------------------------------------- 不变量（守卫）


def test_b6_invariant_signals_list_unchanged(started, cli):
    """config_hints 是新增的旁路信息，不改 signals 列表本身。"""
    _abandon(cli)
    _inject_proof_groups(started["ledger"], 2)
    with L.mutate(started["ledger"]) as lg:
        lg["failures"]["machine"].append({"signature": "sig-x", "stage": "test"})
    out = retro.signals(started["ledger"])
    ids = [s["id"] for s in out["signals"]]
    assert "S-machine-failures" in ids, ids
    assert HINT_KEY in _hint_keys(out), out["config_hints"]


def test_b6_invariant_record_coverage_only_covers_signals(started, cli):
    """retro record 的 dispositions 只需覆盖 signals，不受 config_hints 影响。"""
    _abandon(cli)
    _inject_proof_groups(started["ledger"], 2)
    out = retro.signals(started["ledger"])
    assert HINT_KEY in _hint_keys(out)
    dispositions = [{"signal_id": s["id"], "route": "not_incident", "reason": "测试覆盖校验"} for s in out["signals"]]
    res = retro.record(started["ledger"], {"dispositions": dispositions, "observations": []})
    assert res["recorded"] is True, res