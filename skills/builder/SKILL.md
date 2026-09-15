---
name: builder
description: "进入 Builder 模式：读方案、启动 builder-loop run、在候选 worktree 实现，按 runtime 判据（machine / tester / proof / reviewer）推进到 finalize。触发：/builder [plan 路径]。无 contract 的方案不进 loop。"
---

> **已进入 Builder 模式**。前序角色约束作废。本 session id：`${CLAUDE_SESSION_ID}`——下文所有 `bl` 命令都带 `--session ${CLAUDE_SESSION_ID}`。

# Builder

builder-loop 只负责判据和 Git 事务；调度 subagent、续接、问用户是你的事。**ledger 只能通过 `bl` 改**，不要手工 `git commit`（用 `checkpoint`）、不要 `EnterWorktree`、不要改 `.claude/loop.yml`。

## 1. 启动

1. Read 方案文件（对话里的路径，或用户指定）。方案没有 `<!-- builder-loop-contract -->` 标签 → AskUserQuestion：「用 /planner 补 contract」/「不走 loop，直接实现」。后者就按普通任务做，完成后 spawn 一次 reviewer 审查即可。
2. `bl start --plan <plan> --session ${CLAUDE_SESSION_ID}` → 记住输出里的 `run_id` 和 `worktree`。
3. 之后所有 Write / Edit / Bash 都在 `worktree` 下进行；主仓只读。

## 2. 推进：跟着 `next_action` 走

`bl status --session ${CLAUDE_SESSION_ID}` 的 `readiness.next_action` 是唯一的路标：

| next_action | 你做什么 |
|---|---|
| `checkpoint` | 实现完成 → `bl checkpoint --role builder`。被拒（越界 / protected）→ 撤回那些文件再来 |
| `machine` | `bl machine`。FAIL → Read 输出里的 `failure.log`，修实现，回到 checkpoint。`repeat_count ≥ 2` 时先换思路再改 |
| `spawn_tester` | `Agent(subagent_type: "tester", prompt: "builder-loop run <run_id>，按注入的上下文写测试和 proof_spec")`，**同步**等它结束。hook 会自动记 evidence，你只需再 `bl status` |
| `resume_tester` | `SendMessage(to: <status.agents.tester.agent_id>, message: <proof 失败的 failure 段 + 让它修正>)` |
| `proof` | `bl proof`。FAIL 看 `failure.suggested_owner`：`tester` → resume_tester；`builder` → 修实现 → checkpoint |
| `spawn_reviewer` | `Agent(subagent_type: "reviewer", prompt: "builder-loop run <run_id>，按注入的上下文审查")`，同步 |
| `resume_reviewer` | 按 findings 修 → checkpoint → machine → proof → `SendMessage(to: <reviewer agent_id>, message: "已修复 …，请复审")` |
| `finalize` | `bl finalize -m "type(scope): [cr_id_skip] Desc"`。`TARGET_DRIFT` → `bl rebase`（冲突就在 worktree 里解，`git rebase --continue` 后再 `bl rebase`）→ 全部 evidence 重验 |
| `needs_user` | 看 `blockers`，AskUserQuestion：继续修 / `bl abandon --reason "<原因>"` |
| `done` | 汇报 |

任何 `bl` 命令 exit 3 = 需要用户决定，同样走 AskUserQuestion。Stop hook 会在 run 未完成时把你拉回来，正常现象，按 stderr 提示继续。

## 3. 收尾

finalize 成功后汇报：`final_head`、改动了哪几件事（1–5 条，一事一句）、tester 覆盖的 behaviors、reviewer 结论。文档是否要同步按 `~/.claude/doc-policy.md` 判断，需要就在 finalize 前改（属于候选 diff 的一部分）。

值得长期记住的坑（平台行为、隐式约定）→ 任务结束后一句话建议用户 `/memory`，不打断流程。
