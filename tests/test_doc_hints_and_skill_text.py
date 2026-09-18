"""reviewer brief 的文档引用线索（B1/B2）与 builder SKILL.md 的文档同步 / 复盘措辞（B3/B4）。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT, contract_with, git, write_plan

SKILL = ROOT / "skills" / "builder" / "SKILL.md"

GUIDE = "# Guide\n\n调用 `compute_total` 汇总\n\n另见 `other_fn` 的说明\n\n短名 `abc` 也在这里\n"
FOO = "def add(a, b):\n    return a + b\n\n\ndef compute_total(items):\n    return sum(items)\n\n\ndef abc():\n    return 1\n"


@pytest.fixture
def hint_run(repo, cli):
    """start 之前先把 compute_total / 文档提交进目标分支；builder 可写 docs。"""
    (repo.root / "src" / "foo.py").write_text(FOO)
    (repo.root / "src" / "other.py").write_text("def other_fn():\n    return 2\n")
    (repo.root / "docs").mkdir()
    (repo.root / "docs" / "guide.md").write_text(GUIDE)
    write_plan(repo.root, contract_with(**{"authority.builder_write": ["src/**", "docs/**"]}))
    repo.commit_all()
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    return {"repo": repo, "wt": Path(out["worktree"]), "start": out["target_start_head"]}


def _checkpoint(cli):
    cli("checkpoint", "--session", "S1", "--role", "builder")


def _rev(cli, *extra):
    return cli("brief", "--session", "S1", "--role", "reviewer", *extra)


def _remove_def(wt: Path, name: str, path: str = "src/foo.py") -> None:
    p = wt / path
    text = p.read_text()
    text = re.sub(rf"def {name}\(.*?(?=\n\n\ndef |\Z)", "", text, flags=re.S)
    p.write_text(text.rstrip("\n") + "\n" if text.strip() else "")


def test_hints_hit_when_definition_removed_but_doc_kept(hint_run, cli):
    _remove_def(hint_run["wt"], "compute_total")
    _checkpoint(cli)
    b = _rev(cli, "--json")
    h = b["doc_reference_hints"]
    assert isinstance(h, dict) and h["error"] is None
    assert isinstance(h["hits"], list) and all(isinstance(x, str) for x in h["hits"])
    hit = [x for x in h["hits"] if "docs/guide.md" in x and "compute_total" in x]
    assert hit
    text = _rev(cli)
    assert hit[0] in text and "启发式" in text and "误报" in text


def test_hints_empty_when_doc_line_also_removed(hint_run, cli):
    wt = hint_run["wt"]
    _remove_def(wt, "compute_total")
    g = wt / "docs" / "guide.md"
    g.write_text("".join(ln for ln in g.read_text().splitlines(True) if "compute_total" not in ln))
    _checkpoint(cli)
    h = _rev(cli, "--json")["doc_reference_hints"]
    assert h["hits"] == [] and h["error"] is None


def test_hints_empty_when_nothing_deleted(hint_run, cli):
    wt = hint_run["wt"]
    (wt / "src" / "foo.py").write_text(FOO + "\n\ndef brand_new():\n    return 3\n")
    _checkpoint(cli)
    h = _rev(cli, "--json")["doc_reference_hints"]
    assert h["hits"] == [] and h["error"] is None


def test_hints_ignore_short_symbol_names(hint_run, cli):
    _remove_def(hint_run["wt"], "abc")
    _checkpoint(cli)
    h = _rev(cli, "--json")["doc_reference_hints"]
    assert h["error"] is None and not any("abc" in x for x in h["hits"])


def test_hints_only_reflect_candidate_deletions_after_rebase(hint_run, cli):
    repo, wt = hint_run["repo"], hint_run["wt"]
    _remove_def(wt, "compute_total")
    _checkpoint(cli)
    # 目标分支上别人删掉 other_fn（文档仍提到它）
    (repo.root / "src" / "other.py").write_text("")
    repo.commit_all("fix(other): [cr_id_skip] Drop other_fn")
    rb = cli("rebase", "--session", "S1")
    assert rb["rebased"] and rb["conflicts"] == []
    h = _rev(cli, "--json")["doc_reference_hints"]
    assert h["error"] is None
    assert any("compute_total" in x for x in h["hits"])
    assert not any("other_fn" in x for x in h["hits"])


def test_tester_brief_has_no_hints_and_reviewer_fields_unchanged(hint_run, cli, hook):
    _remove_def(hint_run["wt"], "compute_total")
    _checkpoint(cli)
    assert "doc_reference_hints" not in cli("brief", "--session", "S1", "--role", "tester", "--json")
    b = _rev(cli, "--json")
    for k in ("candidate_worktree", "diff_range", "evidence", "review_focus", "todo", "result_format"):
        assert k in b
    assert b["candidate_worktree"] == str(hint_run["wt"])
    assert b["diff_range"].startswith(hint_run["start"])
    assert b["todo"][0]["what"] in ("review", "wait") or "what" in b["todo"][0]
    # 注入上下文与文本形态逐字一致
    r = hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    assert r["json"]["hookSpecificOutput"]["additionalContext"] == _rev(cli)


def test_hints_do_not_touch_ledger_or_readiness(hint_run, cli):
    _remove_def(hint_run["wt"], "compute_total")
    _checkpoint(cli)
    before = cli("status", "--session", "S1")["readiness"]
    lg_path = next((hint_run["repo"].root / ".claude" / "builder-loop").rglob("ledger*.json"), None)
    raw = lg_path.read_text() if lg_path else None
    _rev(cli, "--json")
    assert cli("status", "--session", "S1")["readiness"] == before
    if lg_path:
        assert lg_path.read_text() == raw
        assert "doc_reference_hints" not in raw


def test_hints_error_when_candidate_worktree_gone(hint_run, cli):
    _remove_def(hint_run["wt"], "compute_total")
    _checkpoint(cli)
    shutil.rmtree(hint_run["wt"])
    b = _rev(cli, "--json")
    h = b["doc_reference_hints"]
    assert h["hits"] == [] and isinstance(h["error"], str) and h["error"]
    for k in ("diff_range", "evidence", "review_focus", "todo", "result_format"):
        assert k in b
    text = _rev(cli)
    assert "Mission" in text and "Behaviors" in text and "BUILDER_LOOP_RESULT" in text
    assert "文档引用线索" in text and "不可用" in text


# ---------------------------------------------------------------- SKILL.md


def _sections() -> dict[str, str]:
    text = SKILL.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^(?=## )", text)
    out = {}
    for p in parts:
        m = re.match(r"## (\d)\.", p)
        if m:
            out[m.group(1)] = p
    return out


def test_skill_headings_and_name_intact():
    text = SKILL.read_text(encoding="utf-8")
    pos = [text.index(f"\n## {n}.") for n in "1234"]
    assert pos == sorted(pos)
    assert re.search(r"(?m)^name:\s*builder\s*$", text.split("---")[1])


def test_skill_doc_sync_hint_moved_before_finalize():
    s = _sections()
    assert "doc-policy" in s["2"]
    assert "finalize 前" not in s["4"]


def test_skill_retro_memory_step_goes_issue_first():
    s3 = _sections()["3"]
    assert "bl retro signals" in s3 and "bl retro record" in s3
    for cat in ("business_issue", "builder_loop_issue", "not_incident"):
        assert cat in s3
    steps = re.findall(r"(?ms)^\d+\..*?(?=^\d+\.|\Z)", s3)
    mem = [x for x in steps if "/memory" in x]
    assert len(mem) == 1 and s3.count("/memory") == 1
    step = mem[0]
    assert "doc-policy" in step
    issue_pos = [step.index(k) for k in ("business_issue", "builder_loop_issue") if k in step]
    assert issue_pos and step.index("/memory") > min(issue_pos)
