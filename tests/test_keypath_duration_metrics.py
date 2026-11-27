"""B8：关键路径埋点 duration_s 不进入证据投影。

given 跑完一次 preflight，或跑完一次全通过的 proof / when 读 ledger 的 preflight 事件或 proof
evidence details / then preflight 每个 stage 含正整数 duration_s，proof 每个 group 含正整数
duration_s。起点 stage / group 记录里没有 duration_s，取它会 KeyError → call 阶段失败
（baseline-red）。

边界（同一输入连续两次 proof 的 dependency_digest 相等；duration_s 不为负）与不变量
（dependency_digest 计算不读取 duration_s）作为守卫留在同一文件，不列入 proof group。
"""

from __future__ import annotations

from builder_loop import evidence
from builder_loop import ledger as L

from conftest import drive_to_proof_pass
from keypath_helpers import loop_yml, start_with_loop


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _preflight_event(ledger_path) -> dict:
    lg = L.load(ledger_path)
    evs = [e for e in lg["events"] if e.get("kind") == "preflight"]
    assert evs, "preflight 没有写事件"
    return evs[-1]


def _proof_groups(ledger_path) -> list[dict]:
    lg = L.load(ledger_path)
    rec = lg["evidence"].get("proof") or {}
    assert rec.get("status") == "pass", rec
    return (rec.get("details") or {}).get("groups") or []


# ---------------------------------------------------------------- 正向（baseline-red）


def test_b8_preflight_stages_have_positive_duration(repo, cli):
    ctx = start_with_loop(repo, cli, loop_yml(stage_cmd="python3 -c 'import time; time.sleep(1.1)'"))
    cli("preflight", "--session", "S1")
    ev = _preflight_event(ctx["ledger"])
    assert ev["stages"], ev
    for stage in ev["stages"]:
        assert _positive_int(stage["duration_s"]), stage


def test_b8_proof_groups_have_positive_duration(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    groups = _proof_groups(started["ledger"])
    assert groups, groups
    for group in groups:
        assert _positive_int(group["duration_s"]), group


# ---------------------------------------------------------------- 边界（守卫）


def test_b8_boundary_dependency_digest_stable_across_two_proofs(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    first = L.load(started["ledger"])["evidence"]["proof"]["dependency_digest"]
    out = cli("proof", "--session", "S1")
    assert out["result"] == "PASS", out
    second = L.load(started["ledger"])["evidence"]["proof"]["dependency_digest"]
    assert first == second, (first, second)


# ---------------------------------------------------------------- 不变量（守卫）


def test_b8_invariant_digest_ignores_duration_s(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    root = started["repo"].root
    before = evidence.dependency_digest(L.load(started["ledger"]), "proof", root)
    with L.mutate(started["ledger"]) as lg:
        for group in (lg["evidence"]["proof"].get("details") or {}).get("groups") or []:
            group["duration_s"] = 999999
    after = evidence.dependency_digest(L.load(started["ledger"]), "proof", root)
    assert before == after, (before, after)