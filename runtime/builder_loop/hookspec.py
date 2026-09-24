"""hook 注册表：本版要在 settings.json 里注册哪些 hook 的唯一定义（原则二）。

install.sh 按它注册，doctor 与 `bl start` 按它检查（#309）。每项 (事件, matcher 或 None, timeout 秒)。
"""

from __future__ import annotations

HOOK_SPEC: tuple[tuple[str, str | None, int], ...] = (
    # 把本仓 bin/ 写进会话 PATH（经 CLAUDE_ENV_FILE），SKILL 与 runtime 提示里的裸 `bl` 才能直接用
    ("SessionStart", None, 5),
    ("Stop", None, 20),
    ("SubagentStart", "tester|reviewer", 10),
    ("SubagentStop", "tester|reviewer", 120),
    ("PreToolUse", "AskUserQuestion", 5),
    # SubagentHandback：角色交卷投递前的校验，不合规就 deny（#303）；要校验 proof_spec 与 patch，超时放宽
    ("PreToolUse", "EnterWorktree|SubagentHandback", 120),
    # tester 首次 integrate 前的读隔离 + 角色写边界 + 心跳续租；SendMessage = builder 续接角色的 intent（#306）。
    # 高频工具，靠 bl-hook.sh 的纯 bash 快速路径兜成本
    ("PreToolUse", "Read|Grep|Glob|Write|Edit|MultiEdit|NotebookEdit|Bash|SendMessage", 5),
    ("PostToolUse", "AskUserQuestion", 5),
    # 角色结果的唯一登记点：tester 登记要提交 worktree + 校验 proof_spec，超时与 SubagentStop 同级。
    # Bash：角色前台命令超时被转后台（backgroundTaskId）；TaskStop：builder 停掉它（#283 #308）
    ("PostToolUse", "SubagentHandback|Bash|TaskStop", 120),
    ("UserPromptSubmit", None, 5),
)


def required_pairs() -> list[tuple[str, str | None]]:
    """(事件, 工具)：matcher 按 `|` 拆开；None = 该事件要有一条不带 matcher 的注册。"""
    return [(ev, tool) for ev, matcher, _ in HOOK_SPEC for tool in (matcher.split("|") if matcher else [None])]
