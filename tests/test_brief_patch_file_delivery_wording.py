"""result-channel run 的 B10: 任一进行中的 run，tester 执行 `bl brief`（文本输出）时含冻结锚句：
"proof_spec 每组的 behavior_ids 恰好 1 个；mutation 组用 patch_file 交 patch 文件的绝对路径
（git diff 重定向生成），不要把 patch 正文抄进结果行。"，其中的结果行模板含 "patch_file" 而不含
"patch":。边界：reviewer 的 brief 不含这句锚句。不变量：brief 的 ledger 路径与 LEDGER_HINT 仍在。

覆盖对象：brief.py 的 render() 与 TESTER_RESULT_FORMAT——冻结基线上没有这句锚句、
TESTER_RESULT_FORMAT 仍是 "patch" 键，下面的断言在起点代码上会在 call 阶段直接失败
（baseline-red）。
"""

from __future__ import annotations

from builder_loop import brief as brief_mod
from builder_loop import ledger as L

ANCHOR_B = ("proof_spec 每组的 behavior_ids 恰好 1 个；mutation 组用 patch_file 交 patch 文件的绝对路径"
           "（git diff 重定向生成），不要把 patch 正文抄进结果行。")


def test_b10_tester_brief_text_contains_anchor_sentence(started, cli):
    lg = L.load(started["ledger"])
    b = brief_mod.build(lg, started["repo"].root, "tester")
    text = brief_mod.render(b)
    assert ANCHOR_B in text, text


def test_b10_result_line_template_has_patch_file_not_patch_key(started, cli):
    lg = L.load(started["ledger"])
    b = brief_mod.build(lg, started["repo"].root, "tester")
    fmt = b["result_format"]
    assert '"patch_file"' in fmt, fmt
    assert '"patch":' not in fmt, fmt
    assert fmt == brief_mod.TESTER_RESULT_FORMAT


def test_b10_boundary_reviewer_brief_has_no_anchor_sentence(started, cli, hook):
    from conftest import implement_mul

    implement_mul(started["worktree"])
    cli("checkpoint", "--session", "S1", "--role", "builder")
    lg = L.load(started["ledger"])
    b = brief_mod.build(lg, started["repo"].root, "reviewer")
    text = brief_mod.render(b)
    assert ANCHOR_B not in text, text


def test_b10_invariant_ledger_path_and_hint_still_present(started, cli):
    lg = L.load(started["ledger"])
    b = brief_mod.build(lg, started["repo"].root, "tester")
    text = brief_mod.render(b)
    assert str(started["ledger"]) in text
    assert brief_mod.LEDGER_HINT in text
