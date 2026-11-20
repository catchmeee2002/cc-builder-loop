"""B5：tester 的 brief 文本（首次 spawn 与续接同一渲染）逐字包含「proof_spec.test_ids 只列用于
证明本 behavior 的测试……」这句规则，agents/tester.md 硬约束与之一致；同时退役 reviewer brief 的
undiscriminated 补偿提醒与对应清单条目。

起点代码的 brief 不含这句、reviewer brief 仍会渲染 undiscriminated 补偿提醒，所以正向断言与
「退役」断言在起点上都于 call 阶段失败（baseline-red）。

守卫（不列入 proof group）：proof_spec JSON 形状、每组一个 behavior、patch_file 方式不变；
守卫测试仍由 tester 交付、integrate 合入并由 machine 全量运行。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import brief, ledger as L

from conftest import drive_to_proof_pass, make_tester_result, role_turn, write_mul_test

ROOT = Path(__file__).resolve().parents[1]
TESTER_MD = ROOT / "agents" / "tester.md"
REVIEWER_MD = ROOT / "agents" / "reviewer.md"

ANCHOR = ("proof_spec.test_ids 只列用于证明本 behavior 的测试：每条在反例下都必须变红；"
          "只检查现役行为的守卫测试不要列入，它们仍由 machine 全量运行。")
UNDISCRIMINATED_OLD = "下面这些 test_id 在反例下从未变红"


def _normalize(text: str) -> str:
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------- B5：brief 文本


def test_b5_first_spawn_brief_contains_test_ids_rule(started, cli):
    text = cli("brief", "--session", "S1", "--role", "tester")
    assert _normalize(ANCHOR) in _normalize(text)


def test_b5_resumed_brief_rendering_is_identical(started, cli, hook):
    """边界：首次注入与续接重取一致——同一个渲染函数给出同一句。"""
    lg = L.load(started["ledger"])
    first = brief.render(brief.build(lg, started["repo"].root, "tester"))
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement = started["worktree"]
    (implement / "src" / "foo.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(started["tester_worktree"])
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")
    resumed = cli("brief", "--session", "S1", "--role", "tester")
    assert _normalize(ANCHOR) in _normalize(first)
    assert _normalize(ANCHOR) in _normalize(resumed)


def test_b5_tester_md_hard_constraints_carry_same_rule():
    """agents/tester.md 的硬约束与 brief 一致：既要点明声明 id 必须在反例下变红，也要点明守卫
    测试不列入（两处术语都取自 contract 冻结的那句锚句）。"""
    text = TESTER_MD.read_text(encoding="utf-8")
    start = text.index("## 硬约束")
    end = text.index("## ", start + len("## 硬约束"))
    section = _normalize(text[start:end])
    assert "变红" in section, section
    assert "守卫测试" in section, section


# ---------------------------------------------------------------- B5：退役 undiscriminated


def test_b5_reviewer_brief_no_longer_renders_undiscriminated_reminder(started, cli, hook):
    """把 per_id 里注入一条非 red 的声明 id（旧 runtime 的 evidence 形态）：旧实现会渲染补偿提醒，
    退役后不再出现。"""
    drive_to_proof_pass(started, cli, hook)
    with L.mutate(started["ledger"]) as lg:
        lg["evidence"]["proof"]["details"]["groups"][0]["counterexample"]["per_id"] = {
            "tests/test_mul.py::test_mul": "green",
        }
    text = cli("brief", "--session", "S1", "--role", "reviewer")
    assert UNDISCRIMINATED_OLD not in text


def test_b5_reviewer_md_checklist_drops_undiscriminated_item():
    section = REVIEWER_MD.read_text(encoding="utf-8")
    assert "从未变红的 test_id" not in section


# ---------------------------------------------------------------- 守卫（不列入 proof group）


def test_b5_guard_result_format_shape_unchanged(started, cli):
    """不变量：proof_spec JSON 形状（kind / behavior_ids / test_ids / timeout / patch_file）不变。"""
    fmt = brief.TESTER_RESULT_FORMAT
    assert '"kind":"baseline-red|mutation|reviewed-boundaries"' in fmt
    assert '"behavior_ids":["B1"]' in fmt and '"test_ids":["tests/test_x.py::test_a"]' in fmt
    assert '"patch_file"' in fmt


def test_b5_guard_one_behavior_per_group_rule_kept(started, cli):
    text = cli("brief", "--session", "S1", "--role", "tester")
    assert "每组" in text and "behavior_ids 恰好 1 个" in text


def test_b5_invariant_machine_still_runs_all_tests(started, cli):
    """不变量：守卫测试仍由 machine 全量运行（brief 仍告知 machine 会跑全量）。"""
    text = cli("brief", "--session", "S1", "--role", "tester")
    assert "machine" in text and "全量" in text
