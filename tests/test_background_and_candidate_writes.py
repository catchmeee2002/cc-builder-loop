"""role-background-and-candidate-writes：
B1 hook 拒绝 tester/reviewer 以 run_in_background 起 Bash；
B2 tester 的 Bash 在集成前后都不能触及候选 worktree 路径（只有候选分支名例外，且仅限集成后）；
B3 add_mutation_patch 待办的措辞新增一句「生成 patch 不要碰候选 worktree」；
B4 agents/tester.md 与 agents/reviewer.md 各自新增同一句「不要用 run_in_background」的硬约束。
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import drive_to_proof_pass, implement_mul, make_tester_result, role_turn, send_message, write_mul_test

ROOT = Path(__file__).resolve().parents[1]


def _denied(r) -> bool:
    return bool(r["json"]) and r["json"]["hookSpecificOutput"]["permissionDecision"] == "deny"


def _reason(r) -> str:
    return r["json"]["hookSpecificOutput"]["permissionDecisionReason"]


def normalize(text: str) -> str:
    """去掉 markdown 强调标记与反引号，连续空白归一为一个空格。"""
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------- B1


def test_run_in_background_denied_for_tester_and_reviewer(started, hook):
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    hook("SubagentStart", {"session_id": "S1", "agent_id": "R1", "agent_type": "reviewer"})
    bg = {"command": "python3 -m pytest -q tests", "run_in_background": True}

    r = hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Bash", "tool_input": bg})
    assert _denied(r) and "后台" in _reason(r)

    r2 = hook("PreToolUse", {"session_id": "S1", "agent_type": "reviewer", "agent_id": "R1", "tool_name": "Bash", "tool_input": bg})
    assert _denied(r2) and "后台" in _reason(r2)

    # 边界：同一命令不带 run_in_background（或为 false）→ 不拒绝
    fg = {"command": "python3 -m pytest -q tests"}
    assert not _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Bash", "tool_input": fg}))
    fg_false = {"command": "python3 -m pytest -q tests", "run_in_background": False}
    assert not _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "reviewer", "agent_id": "R1", "tool_name": "Bash", "tool_input": fg_false}))

    # 边界：reviewer 的前台只读 Bash 不被拒
    assert not _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "reviewer", "agent_id": "R1", "tool_name": "Bash", "tool_input": {"command": "git log -1"}}))

    # 边界：主 session（无 agent_type）run_in_background=true → 不拒绝
    assert not _denied(hook("PreToolUse", {"session_id": "S1", "tool_name": "Bash", "tool_input": bg}))

    # 边界：agent_type 不是 tester/reviewer（例如 general-purpose）→ 不拒绝
    assert not _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "general-purpose", "agent_id": "G1", "tool_name": "Bash", "tool_input": bg}))

    # 不变量：reviewer 调用 Write/Edit 仍被拒
    assert _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "reviewer", "agent_id": "R1", "tool_name": "Write",
                                        "tool_input": {"file_path": str(started["worktree"] / "src" / "foo.py")}}))
    # 不变量：tester 写 tester_write 之外的路径仍被拒
    assert _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Write",
                                        "tool_input": {"file_path": str(started["tester_worktree"] / "src" / "foo.py")}}))


# ---------------------------------------------------------------- B2


def test_tester_bash_still_blocked_from_candidate_after_integration(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")

    def bash(cmd: str):
        return hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Bash", "tool_input": {"command": cmd}})

    r = bash(f"sed -i s/a/b/ {wt}/src/foo.py")
    assert _denied(r) and "候选" in _reason(r)
    assert _denied(bash(f"cd {wt} && git diff"))
    # realpath 形式的等价路径（带 `..`）同样拒绝
    assert _denied(bash(f"cat {twt}/../builder/src/foo.py"))

    # 边界：只含候选分支名、不含候选 worktree 路径 → 不拒绝
    assert not _denied(bash(f"git show builder-loop/{started['run_id']}/candidate:src/foo.py"))

    # 边界：Read 读候选实现 → 不拒绝（集成后读隔离解除）
    assert not _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Read",
                                            "tool_input": {"file_path": f"{wt}/src/foo.py"}}))
    # 边界：Grep/Glob 显式 path 为候选 worktree → 不拒绝
    assert not _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Grep",
                                            "tool_input": {"pattern": "mul", "path": str(wt)}}))
    assert not _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Glob",
                                            "tool_input": {"pattern": "**/*.py", "path": str(wt)}}))

    # 不变量：Write/Edit 仍不能写候选
    assert _denied(hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Write",
                                        "tool_input": {"file_path": f"{wt}/src/foo.py"}}))
    # 不变量：tester 在自己 worktree 内跑的命令不被拒
    assert not _denied(bash("python3 -m pytest -q tests/test_mul.py"))


def test_tester_bash_branch_name_blocked_before_integration(started, hook):
    """边界（现有语义）：集成前 command 含候选分支名同样拒绝。"""
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    r = hook("PreToolUse", {"session_id": "S1", "agent_type": "tester", "agent_id": "T1", "tool_name": "Bash",
                             "tool_input": {"command": f"git show builder-loop/{started['run_id']}/candidate:src/foo.py"}})
    assert _denied(r)


# ---------------------------------------------------------------- B3


# result-channel run（B1）之后：mutation patch 改走 patch_file 交付，措辞相应更新为
# "git diff 重定向到文件 + 交文件绝对路径"，不再是旧版"git diff 即得 patch"（inline 交付）。
ADD_MUTATION_PATCH_ANCHOR = (
    "生成 patch 时不要改候选 worktree 里的文件，也不要在里面跑命令：在你的 worktree 之外建一个临时目录并 "
    "git init，用 git show <候选分支>:<路径> 按原相对路径取出文件并提交，改完后 git diff > <临时目录>/<behavior>.patch，"
    "把这个文件的绝对路径填进该组的 patch_file。"
)


def test_add_mutation_patch_todo_explains_how_to_build_patch_without_touching_candidate(started, cli, hook):
    wt, twt = started["worktree"], started["tester_worktree"]
    hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})
    implement_mul(wt)
    cli("checkpoint", "--session", "S1", "--role", "builder")
    write_mul_test(twt)
    role_turn(hook, "tester", "T1", make_tester_result("mutation"), start=False)
    cli("integrate", "--session", "S1")

    b = cli("brief", "--session", "S1", "--role", "tester", "--json")
    todo = next(t for t in b["todo"] if t["what"] == "add_mutation_patch")
    assert normalize(ADD_MUTATION_PATCH_ANCHOR) in normalize(todo["why"])
    # 不变量：原有的「只破坏该 behavior 的 unified diff」说明保留
    assert "只破坏该 behavior 的 unified diff" in todo["why"]

    # SubagentStart 注入的上下文与 brief 同源，同样含这句（真续接：先 SendMessage）
    send_message(hook, "T1")
    ctx = hook("SubagentStart", {"session_id": "S1", "agent_id": "T1", "agent_type": "tester"})["json"]["hookSpecificOutput"]["additionalContext"]
    assert normalize(ADD_MUTATION_PATCH_ANCHOR) in normalize(ctx)


def test_add_mutation_patch_todo_absent_when_no_group_missing_patch(started, cli, hook):
    """边界：没有缺 patch 的 mutation 组时，brief 不出现 add_mutation_patch 待办。"""
    drive_to_proof_pass(started, cli, hook)
    b = cli("brief", "--session", "S1", "--role", "tester", "--json")
    assert not any(t["what"] == "add_mutation_patch" for t in b["todo"])


# ---------------------------------------------------------------- B4


RUN_IN_BACKGROUND_ANCHOR = (
    "不要用 run_in_background 起后台任务：交卷后它会把你反复唤醒；"
    "需要跑久的命令就前台执行并给足 timeout，只跑与你的结论有关的测试文件。"
)


def test_tester_md_hard_constraints_include_no_background_rule():
    text = (ROOT / "agents" / "tester.md").read_text(encoding="utf-8")
    start = text.index("## 硬约束")
    end = text.index("## ", start + len("## 硬约束"))
    section = text[start:end]
    assert normalize(RUN_IN_BACKGROUND_ANCHOR) in normalize(section)
    # 不变量：原有第 1-6 条仍在
    for n in range(1, 7):
        assert f"\n{n}. " in section, section


def test_reviewer_md_includes_no_background_rule_before_checklist():
    text = (ROOT / "agents" / "reviewer.md").read_text(encoding="utf-8")
    start = text.index("# Reviewer")
    checklist_idx = text.index("## 审查清单")
    section = text[start:checklist_idx]
    assert normalize(RUN_IN_BACKGROUND_ANCHOR) in normalize(section)
    # 边界：这句出现在 `## 审查清单` 之前（已经由切片保证，这里再显式核对一次绝对位置）
    anchor_start = text.index(RUN_IN_BACKGROUND_ANCHOR[:10])
    assert anchor_start < checklist_idx
