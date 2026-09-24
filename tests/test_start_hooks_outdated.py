"""B3: bl start 检查 CLAUDE_HOME/settings.json 对 HOOK_SPEC 的注册是否完整；缺项时拒绝启动
（HOOKS_OUTDATED，exit 3），不创建 run 目录 / worktree、不绑定 session。

冻结基线上 bl start 没有这项检查：下面的正向/负向断言在起点代码上要么在 call 阶段直接失败
（HOOKS_OUTDATED 没被抛出、run 正常建了起来），要么根本没有对应的错误码——新行为整体走
mutation（起点上没有这项检查可复现，但「集成后候选实现了什么」需要读实现才能配 patch）。

conftest 的 `repo` fixture 默认给出注册完整的 CLAUDE_HOME（write_full_claude_home），本文件的
负向用例在各自测试里从这份基础上删条目 / 改坏 / 清空。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from builder_loop import ledger as L

from conftest import HOOK_SPEC, ROOT, write_full_claude_home

INSTALL_SH = ROOT / "install.sh"


def _settings(repo) -> Path:
    return repo.claude_home / "settings.json"


def _load(repo) -> dict:
    return json.loads(_settings(repo).read_text(encoding="utf-8"))


def _save(repo, data: dict) -> None:
    _settings(repo).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _drop_matcher_tool(repo, event: str, tool: str) -> None:
    data = _load(repo)
    for m in data.get("hooks", {}).get(event, []):
        if m.get("matcher") and tool in m["matcher"].split("|"):
            parts = [p for p in m["matcher"].split("|") if p != tool]
            m["matcher"] = "|".join(parts)
    _save(repo, data)


def _snapshot(repo) -> tuple[set[str], list]:
    siblings = set(os.listdir(repo.root.parent))
    runs = L.list_runs(repo.root)
    return siblings, runs


# ---------------------------------------------------------------- given：primary scenario


def test_b3_missing_pair_blocks_start(repo, cli):
    before_siblings, before_runs = _snapshot(repo)
    assert L.lookup_session("S1") is None

    _drop_matcher_tool(repo, "PreToolUse", "SendMessage")

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=3)
    assert out["code"] == "HOOKS_OUTDATED", out
    missing = out["details"]["missing"]
    assert isinstance(missing, list) and missing
    assert any("PreToolUse" in m and "SendMessage" in m for m in missing), missing
    assert out["details"]["install"] == str(INSTALL_SH)

    after_siblings, after_runs = _snapshot(repo)
    assert after_siblings == before_siblings  # 没建 worktree（worktree 建在仓库同级目录）
    assert after_runs == before_runs == []     # 没建 run 目录
    assert L.lookup_session("S1") is None      # session 没绑定


def test_b3_install_sh_then_start_succeeds(repo, cli):
    _drop_matcher_tool(repo, "PreToolUse", "SendMessage")
    cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1", expect=3)

    write_full_claude_home(repo.claude_home)  # 等价于用 install.sh 在同一 CLAUDE_HOME 补全注册

    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S1")
    assert "run_id" in out
    assert L.lookup_session("S1") is not None


# ---------------------------------------------------------------- 边界


def test_b3_boundary_missing_settings_file_reports_all(repo, cli):
    _settings(repo).unlink()
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S2", expect=3)
    assert out["code"] == "HOOKS_OUTDATED", out
    missing = out["details"]["missing"]
    # HOOK_SPEC 里每个 (事件, matcher-or-tool) 组合都应体现在缺项报告里
    for event, matcher, _timeout in HOOK_SPEC:
        tools = matcher.split("|") if matcher else [None]
        for tool in tools:
            needle = event if tool is None else f"{event}"
            assert any(needle in m for m in missing), (event, tool, missing)


def test_b3_boundary_unparsable_settings_reports_all(repo, cli):
    _settings(repo).write_text("{not json", encoding="utf-8")
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S3", expect=3)
    assert out["code"] == "HOOKS_OUTDATED", out
    assert len(out["details"]["missing"]) >= len(HOOK_SPEC)


def test_b3_boundary_broken_script_link_reports_it(repo, cli):
    data = _load(repo)
    broken = "/nonexistent/bl-hook.sh"
    for matchers in data.get("hooks", {}).values():
        for m in matchers:
            for h in m.get("hooks", []):
                h["command"] = h["command"].replace(str(ROOT / "hooks" / "bl-hook.sh"), broken)
    _save(repo, data)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S4", expect=3)
    assert out["code"] == "HOOKS_OUTDATED", out
    assert any(broken in m for m in out["details"]["missing"]), out["details"]["missing"]


def test_b3_boundary_complete_registration_starts_clean(repo, cli):
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S5")
    assert "HOOKS_OUTDATED" not in json.dumps(out)


def test_b3_boundary_split_matcher_entries_still_complete(repo, cli):
    """同一事件的 matcher 被拆成多条 entry（Bash 一条、SendMessage 另一条）也算完整。"""
    data = _load(repo)
    pre = data["hooks"]["PreToolUse"]
    for m in pre:
        if m.get("matcher") and "SendMessage" in m["matcher"].split("|"):
            parts = [p for p in m["matcher"].split("|") if p != "SendMessage"]
            m["matcher"] = "|".join(parts)
            pre.append({"matcher": "SendMessage", "hooks": list(m["hooks"])})
            break
    _save(repo, data)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S6")
    assert "HOOKS_OUTDATED" not in json.dumps(out)


def test_b3_boundary_unrelated_settings_dont_affect(repo, cli):
    data = _load(repo)
    data["otherSetting"] = True
    data["hooks"]["SomeUnrelatedEvent"] = [{"matcher": "X", "hooks": [{"type": "command", "command": "echo hi"}]}]
    _save(repo, data)
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S7")
    assert "HOOKS_OUTDATED" not in json.dumps(out)


# ---------------------------------------------------------------- 不变量


def test_b3_invariant_run_already_active_checked_first(repo, cli):
    """已绑定未完成 run 的 session 仍报 RUN_ALREADY_ACTIVE，即使 hooks 也缺项。"""
    cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S8")
    _drop_matcher_tool(repo, "PreToolUse", "SendMessage")
    out = cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S8", expect=3)
    assert out["code"] == "RUN_ALREADY_ACTIVE", out


def test_b3_invariant_running_run_other_commands_unaffected(repo, cli):
    """已在运行的 run 的任何 bl 命令都不做这项检查：status 不受 hooks 缺项影响。"""
    cli("start", "--plan", str(repo.root / "plan.md"), "--session", "S9")
    _drop_matcher_tool(repo, "PreToolUse", "SendMessage")
    out = cli("status", "--session", "S9")
    assert "HOOKS_OUTDATED" not in json.dumps(out)
