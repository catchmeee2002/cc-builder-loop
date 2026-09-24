"""B6：brief 给出 ledger 的绝对路径，两个角色都能直接读它而不必在文件系统里搜索。

覆盖对象：runtime/builder_loop/brief.py::build 新增 `ledger` 键，render 的文本形态含该路径与
提示句子。
"""

from __future__ import annotations

import builder_loop.ledger as L
from builder_loop import brief as B

LEDGER_HINT = "要看 evidence 细节就只读这个 ledger，不要在文件系统里搜索它。"


def test_b6_build_dict_has_ledger_key_matching_start_output(started, cli):
    lg = L.load(started["ledger"])
    root = started["repo"].root
    t = B.build(lg, root, "tester")
    r = B.build(lg, root, "reviewer")
    assert t.get("ledger") == str(started["ledger"]), t
    assert r.get("ledger") == str(started["ledger"]), r


def test_b6_text_output_mentions_ledger_path_and_hint(started, cli):
    out_t = cli("brief", "--session", "S1", "--role", "tester")
    out_r = cli("brief", "--session", "S1", "--role", "reviewer")
    assert str(started["ledger"]) in out_t, out_t
    assert str(started["ledger"]) in out_r, out_r
    assert LEDGER_HINT in out_t, out_t
    assert LEDGER_HINT in out_r, out_r


def test_b6_boundary_resumed_brief_same_ledger_path(started, cli, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    first = cli("brief", "--session", "S1", "--role", "tester", "--json")["ledger"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    second = cli("brief", "--session", "S1", "--role", "tester", "--json")["ledger"]
    assert first == second == str(started["ledger"])


def test_b6_invariant_existing_fields_kept(started, cli):
    lg = L.load(started["ledger"])
    root = started["repo"].root
    t = B.build(lg, root, "tester")
    r = B.build(lg, root, "reviewer")
    for k in ("run_id", "role", "mission", "behaviors", "worktree", "write_paths", "result_format"):
        assert k in t, (k, t)
    for k in ("run_id", "role", "mission", "behaviors", "candidate_worktree", "diff_range", "result_format"):
        assert k in r, (k, r)
