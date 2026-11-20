"""B4：旧 runtime 留下的 proof PASS（dependency digest 不含 proof_rule=every-declared-case-red@1）
在新 runtime 上必须重算为 stale，reviewer 因前置证据失效不能沿用旧结论；新 proof 输入投影带上
proof_rule。起点代码的 projection 里没有 proof_rule，且模拟出的旧 digest 与当前 digest 相同
（state 仍 pass），所以下面的断言在起点上会在 call 阶段失败（baseline-red）。

用「把记录里的 proof dependency_digest 换成当前投影剥掉 proof_rule 后的 digest」模拟旧证据：
这正是旧 runtime 会写下的那份输入投影（新投影 = 旧投影 + proof_rule）。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from builder_loop import evidence
from builder_loop import ledger as L
from builder_loop.jsonutil import digest

from conftest import drive_to_proof_pass, reviewer_pass

PROOF_RULE_MARK = "every-declared-case-red"


def _old_projection(obj: Any) -> Any:
    """还原旧 runtime 会写下的那份投影：任何承载 proof_rule 标记的位置（键名含 proof_rule，
    或字符串值里含 every-declared-case-red）都予以剔除/替换。不假定实现把它放在哪一层。"""
    if isinstance(obj, dict):
        return {k: _old_projection(v) for k, v in obj.items()
                if "proof_rule" not in k and not (isinstance(v, str) and PROOF_RULE_MARK in v)}
    if isinstance(obj, list):
        return [_old_projection(v) for v in obj
                if not (isinstance(v, str) and PROOF_RULE_MARK in v)]
    return obj


def _rewrite_digest_as_old_runtime(ledger_path: Path, kind: str = "proof") -> None:
    lg = L.load(ledger_path)
    old = _old_projection(copy.deepcopy(evidence.projection(lg, kind, Path(lg["repo"]["root"]))))
    assert lg["evidence"][kind] is not None
    with L.mutate(ledger_path) as x:
        x["evidence"][kind]["dependency_digest"] = digest(old)


def _load(started):
    return L.load(started["ledger"]), Path(started["repo"].root)


def test_b4_old_proof_pass_without_proof_rule_is_stale(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    lg, root = _load(started)
    assert evidence.state(lg, "proof", root) == evidence.STATE_PASS  # 前提：先有一份真实的 pass

    _rewrite_digest_as_old_runtime(started["ledger"])
    lg2, root2 = _load(started)
    assert evidence.state(lg2, "proof", root2) == evidence.STATE_STALE


def test_b4_new_proof_projection_carries_proof_rule(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    lg, root = _load(started)
    proj = evidence.projection(lg, "proof", root)
    assert "proof_rule" in str(proj)
    assert PROOF_RULE_MARK in str(proj)


def test_b4_reviewer_cannot_reuse_conclusion_after_proof_goes_stale(started, cli, hook):
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    lg, root = _load(started)
    assert evidence.state(lg, "reviewer", root) == evidence.STATE_PASS

    _rewrite_digest_as_old_runtime(started["ledger"])
    lg2, root2 = _load(started)
    assert evidence.state(lg2, "proof", root2) == evidence.STATE_STALE
    assert evidence.state(lg2, "reviewer", root2) == evidence.STATE_STALE


def test_b4_boundary_machine_and_tester_stay_fresh(started, cli, hook):
    """边界：只让 proof 及下游 reviewer 失效，machine / tester 不受影响。"""
    drive_to_proof_pass(started, cli, hook)
    reviewer_pass(hook)
    lg, root = _load(started)
    assert evidence.state(lg, "machine", root) == evidence.STATE_PASS
    assert evidence.state(lg, "tester", root) == evidence.STATE_PASS

    _rewrite_digest_as_old_runtime(started["ledger"])
    lg2, root2 = _load(started)
    assert evidence.state(lg2, "machine", root2) == evidence.STATE_PASS
    assert evidence.state(lg2, "tester", root2) == evidence.STATE_PASS


def test_b4_invariant_no_new_ledger_field_and_events_untouched(started, cli, hook):
    """不变量：不新增 ledger 字段、不修改旧事件。"""
    drive_to_proof_pass(started, cli, hook)
    lg, _ = _load(started)
    keys_before = sorted(lg.keys())
    events_before = copy.deepcopy(lg["events"])
    _rewrite_digest_as_old_runtime(started["ledger"])
    lg2, _ = _load(started)
    assert sorted(lg2.keys()) == keys_before
    assert lg2["events"] == events_before
