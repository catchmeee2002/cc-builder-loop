"""B1：loop.yml 冻结的 proof_runner.framework=generic 时，tester 的 brief 把 generic 的拒绝条件
提前摆出来——默认 strong 下限的 behavior 只给 ["mutation"]（不含 baseline-red），reviewed-boundaries
下限的 behavior 给 ["mutation","reviewed-boundaries"]；渲染文本在 runner 命令那一行之后带字面锚句
`generic 只许 mutation；每组恰好 1 个 test_id；不能用 baseline-red`。

起点代码在 generic 下仍按 floor 给出含 baseline-red 的完整列表、渲染也没有这句锚句，所以下面的
正向断言在起点上都会在 call 阶段失败（baseline-red）。framework=pytest 的列表、reviewer brief、
RESULT_RULE 是既有的现役行为，作为守卫留在同一文件（不列入 proof group）。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import brief as brief_mod
from builder_loop import ledger as L
from builder_loop import proof as P

from conftest import PROOF_RUNNER_CMD, PYTEST_CMD, contract_with, write_plan

ANCHOR = "generic 只许 mutation；每组恰好 1 个 test_id；不能用 baseline-red"
RUNNER_LINE = "测试命令由项目冻结，不需要你给 argv"


def _start_generic(repo, cli, contract: dict | None = None) -> dict:
    """用一个 framework=generic 的 loop.yml 起 run，返回 cli start 的输出（含 ledger 路径）。"""
    (repo.root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: test\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\nmax_iterations: 3\n"
        'proof_runner:\n  framework: generic\n  cmd: "bash"\n'
    )
    write_plan(repo.root, contract or contract_with())
    return cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")


def _tester_brief(repo, ledger_path: str) -> dict:
    lg = L.load(Path(ledger_path))
    return brief_mod.build(lg, repo.root, "tester")


def _start_pytest(repo, cli, contract: dict | None = None) -> dict:
    """framework=pytest 的对照 run（reviewer finding 1：pytest + reviewed 下限的组合）。"""
    (repo.root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: test\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\nmax_iterations: 3\n"
        f"proof_runner:\n  framework: pytest\n  cmd: \"{PROOF_RUNNER_CMD}\"\n"
    )
    write_plan(repo.root, contract or contract_with())
    return cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")


def _norm_line(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", text)).strip()


def _reviewed_contract() -> dict:
    c = contract_with()
    c["mission"]["behaviors"][0]["proof"] = "reviewed-boundaries"
    return c


# ---------------------------------------------------------------- B1：正向（起点上 call 阶段失败）


def test_b1_generic_strong_floor_allows_only_mutation(repo, cli):
    out = _start_generic(repo, cli)
    b = _tester_brief(repo, out["ledger"])
    assert b["behaviors"][0]["proof_kinds_allowed"] == ["mutation"], b["behaviors"][0]


def test_b1_generic_reviewed_floor_drops_baseline_red(repo, cli):
    out = _start_generic(repo, cli, _reviewed_contract())
    b = _tester_brief(repo, out["ledger"])
    assert b["behaviors"][0]["proof_kinds_allowed"] == ["mutation", "reviewed-boundaries"], b["behaviors"][0]


def test_b1_kinds_allowed_generic_matrix():
    """接口直接核对：generic 下 strong → 只有 mutation；reviewed 下限再多一项 reviewed-boundaries。"""
    assert P.kinds_allowed("generic", "strong") == ["mutation"]
    assert P.kinds_allowed("generic", "reviewed-boundaries") == ["mutation", "reviewed-boundaries"]


def test_b1_generic_anchor_after_runner_line(repo, cli):
    out = _start_generic(repo, cli)
    text = brief_mod.render(_tester_brief(repo, out["ledger"]))
    lines = [_norm_line(ln) for ln in text.splitlines()]
    runner_idx = next((i for i, ln in enumerate(lines) if _norm_line(RUNNER_LINE) in ln), None)
    anchor_idx = next((i for i, ln in enumerate(lines) if _norm_line(ANCHOR) in ln), None)
    assert anchor_idx is not None, f"anchor missing:\n{text}"
    assert runner_idx is not None, f"runner line missing:\n{text}"
    assert anchor_idx > runner_idx, f"anchor 应在 runner 命令那一行之后:\n{text}"


# ---------------------------------------------------------------- B1：边界 / 不变量（守卫）


def test_b1_boundary_pytest_keeps_existing_kinds(started):
    """边界：framework=pytest 时列表保持 ["baseline-red","mutation"]（reviewed 下限多一项）。"""
    lg = L.load(started["ledger"])
    b = brief_mod.build(lg, started["repo"].root, "tester")
    assert b["behaviors"][0]["proof_kinds_allowed"] == ["baseline-red", "mutation"], b["behaviors"][0]


def test_b1_boundary_pytest_reviewed_floor_keeps_reviewed_boundaries(repo, cli):
    """边界（reviewer finding 1）：pytest + reviewed-boundaries 下限时多一项 reviewed-boundaries，
    且 generic 锚句仍不出现。"""
    out = _start_pytest(repo, cli, _reviewed_contract())
    b = _tester_brief(repo, out["ledger"])
    assert b["behaviors"][0]["proof_kinds_allowed"] == ["baseline-red", "mutation", "reviewed-boundaries"], b["behaviors"][0]
    assert P.kinds_allowed("pytest", "reviewed-boundaries") == ["baseline-red", "mutation", "reviewed-boundaries"]
    assert _norm_line(ANCHOR) not in _norm_line(brief_mod.render(b))


def test_b1_boundary_pytest_render_has_no_generic_anchor(started):
    lg = L.load(started["ledger"])
    text = brief_mod.render(brief_mod.build(lg, started["repo"].root, "tester"))
    assert _norm_line(ANCHOR) not in _norm_line(text), text


def test_b1_invariant_reviewer_brief_unaffected(repo, cli):
    out = _start_generic(repo, cli)
    lg = L.load(Path(out["ledger"]))
    b = brief_mod.build(lg, repo.root, "reviewer")
    assert "proof_kinds_allowed" not in b["behaviors"][0], b["behaviors"][0]
    assert _norm_line(ANCHOR) not in _norm_line(brief_mod.render(b))


def test_b1_invariant_result_rule_still_rendered(repo, cli):
    out = _start_generic(repo, cli)
    text = brief_mod.render(_tester_brief(repo, out["ledger"]))
    assert _norm_line("proof_spec 每组的 behavior_ids 恰好 1 个") in _norm_line(text), text
    assert "patch_file" in text, text