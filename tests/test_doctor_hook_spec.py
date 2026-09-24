"""B4: bl doctor 按 HOOK_SPEC 的全部 10 项 (事件, matcher-拆开的工具) 检查 CLAUDE_HOME/settings.json，
缺哪项就在 problems 里报哪项。冻结基线上 doctor 只检查 REQUIRED_MATCHERS 这 4 对（SendMessage /
SubagentHandback / PostToolUse Bash / PostToolUse TaskStop），删掉 SubagentStart 或 Stop 的注册
不会被当前 doctor 发现——下面的负向断言在起点代码上会在 call 阶段直接失败（baseline-red：真实调
`bl doctor`，不需要 import 新符号，纯黑盒）。

覆盖对象：doctor.py::missing_hooks（新符号，不在这里 import——通过 CLI/subprocess 的 JSON 输出验证）。
"""

from __future__ import annotations

import json

from conftest import HOOK_SPEC, write_full_claude_home


def _drop_event(repo, event: str) -> None:
    data = json.loads((repo.claude_home / "settings.json").read_text(encoding="utf-8"))
    data.get("hooks", {}).pop(event, None)
    (repo.claude_home / "settings.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _run_doctor(repo, cli) -> dict:
    return cli("doctor")


# ---------------------------------------------------------------- given/when/then


def test_b4_missing_subagentstart_flagged(repo, cli):
    _drop_event(repo, "SubagentStart")
    rep = _run_doctor(repo, cli)
    assert rep["healthy"] is False, rep
    assert any("SubagentStart" in p and "tester" in p for p in rep["problems"]), rep["problems"]


def test_b4_missing_stop_flagged(repo, cli):
    _drop_event(repo, "Stop")
    rep = _run_doctor(repo, cli)
    assert rep["healthy"] is False, rep
    assert any("Stop" in p for p in rep["problems"]), rep["problems"]


def test_b4_missing_sessionstart_flagged(repo, cli):
    _drop_event(repo, "SessionStart")
    rep = _run_doctor(repo, cli)
    assert rep["healthy"] is False, rep
    assert any("SessionStart" in p for p in rep["problems"]), rep["problems"]


def test_b4_missing_userpromptsubmit_flagged(repo, cli):
    _drop_event(repo, "UserPromptSubmit")
    rep = _run_doctor(repo, cli)
    assert rep["healthy"] is False, rep
    assert any("UserPromptSubmit" in p for p in rep["problems"]), rep["problems"]


# ---------------------------------------------------------------- 边界


def test_b4_boundary_fresh_install_is_healthy(repo, cli):
    """install.sh 刚写完的 CLAUDE_HOME：problems 里没有任何 hook 缺项，healthy True（本仓 proof runner 可用时）。"""
    write_full_claude_home(repo.claude_home)
    rep = _run_doctor(repo, cli)
    hook_problems = [p for p in rep["problems"] if any(ev in p for ev, _m, _t in HOOK_SPEC)]
    assert hook_problems == [], rep["problems"]


def test_b4_boundary_hook_spec_has_ten_events_matching_b5():
    assert len(HOOK_SPEC) == 10
    expected = {
        ("SessionStart", None, 5), ("Stop", None, 20),
        ("SubagentStart", "tester|reviewer", 10), ("SubagentStop", "tester|reviewer", 120),
        ("PreToolUse", "AskUserQuestion", 5), ("PreToolUse", "EnterWorktree|SubagentHandback", 120),
        ("PreToolUse", "Read|Grep|Glob|Write|Edit|MultiEdit|NotebookEdit|Bash|SendMessage", 5),
        ("PostToolUse", "AskUserQuestion", 5), ("PostToolUse", "SubagentHandback|Bash|TaskStop", 120),
        ("UserPromptSubmit", None, 5),
    }
    assert set(HOOK_SPEC) == expected


# ---------------------------------------------------------------- 不变量


def test_b4_invariant_symlink_check_unaffected(repo, cli):
    """doctor 其余检查（软链、孤儿 session、proof runner）不变：即使 hooks 齐全，断链照样被报出。"""
    (repo.claude_home / "skills").mkdir(parents=True, exist_ok=True)
    dangling = repo.claude_home / "skills" / "dangling"
    dangling.symlink_to("/nonexistent/target")
    rep = _run_doctor(repo, cli)
    assert rep["broken_symlinks"], rep
