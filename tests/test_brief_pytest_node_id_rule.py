"""B2：proof_runner.framework=pytest 时，tester 的 brief 渲染文本要带字面锚句
`pytest 下 test_ids 的文件部分必须是 .py；测试语言与 framework 不符时交 status=insufficient_spec`，
把「测试语言与 framework 不符时只能交 insufficient_spec」的拒绝条件提前到 brief。

起点代码的渲染没有这句锚句，所以正向断言在起点上会在 call 阶段失败（baseline-red）。
framework=generic 不含该锚句、既有 `--collect-only` 与 node id 核对提示语保持——都是守卫。
"""

from __future__ import annotations

import re
from pathlib import Path

from builder_loop import brief as brief_mod
from builder_loop import ledger as L

from conftest import PROOF_RUNNER_CMD, PYTEST_CMD, contract_with, write_plan

ANCHOR = "pytest 下 test_ids 的文件部分必须是 .py；测试语言与 framework 不符时交 status=insufficient_spec"
COLLECT_HINT = "--collect-only"
NODE_ID_HINT = "逐条核对 proof_spec 里的 node id 与文件中的实际定义一致"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", text)).strip()


def _render_tester_brief(repo, ledger_path: str) -> str:
    lg = L.load(Path(ledger_path))
    return brief_mod.render(brief_mod.build(lg, repo.root, "tester"))


def _start_generic(repo, cli) -> dict:
    (repo.root / ".claude" / "loop.yml").write_text(
        f"pass_cmd:\n  - stage: test\n    cmd: \"{PYTEST_CMD}\"\n    timeout: 60\nmax_iterations: 3\n"
        'proof_runner:\n  framework: generic\n  cmd: "bash"\n'
    )
    write_plan(repo.root, contract_with())
    # loop.yml 现在从目标提交冻结（B1）：先提交再 start
    repo.commit_all("chore(fixture): [cr_id_skip] Generic loop config")
    return cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")


# ---------------------------------------------------------------- B2：正向（起点上 call 阶段失败）


def test_b2_pytest_brief_states_file_part_and_language_rule(started):
    text = _render_tester_brief(started["repo"], started["ledger"])
    assert _norm(ANCHOR) in _norm(text), text


# ---------------------------------------------------------------- B2：边界 / 不变量（守卫）


def test_b2_boundary_generic_brief_has_no_pytest_anchor(repo, cli):
    out = _start_generic(repo, cli)
    text = _render_tester_brief(repo, out["ledger"])
    assert _norm(ANCHOR) not in _norm(text), text


def test_b2_invariant_collect_only_and_node_id_hints_preserved(started):
    text = _render_tester_brief(started["repo"], started["ledger"])
    assert COLLECT_HINT in text, text
    assert _norm(NODE_ID_HINT) in _norm(text), text