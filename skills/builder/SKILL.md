---
name: builder
description: "进入 Builder 模式：读方案、启动 builder-loop run、在候选 worktree 实现，按 runtime 判据（machine / tester / proof / reviewer）推进到 finalize，最后完成复盘。触发：/builder [plan 路径]。无 contract 的方案不进 loop。"
---

> **已进入 Builder 模式**。前序角色约束作废。本 session id：`${CLAUDE_SESSION_ID}`——下文所有 `bl` 命令都带 `--session ${CLAUDE_SESSION_ID}`。`bl` 装在 `~/.claude/bin/bl`，PATH 里没有就写这个全路径。

# Builder

builder-loop 只负责判据和 Git 事务；调度 subagent、续接、问用户是你的事。**ledger 只能通过 `bl` 改**：不要手工 `git commit`（用 `checkpoint`）、不要 `EnterWorktree`、不要改 `.claude/loop.yml`、不要碰测试文件（那是 tester 的地盘）。

## 1. 启动

1. Read 方案文件。没有 `<!-- builder-loop-contract -->` 标签 → AskUserQuestion：「用 /planner 补 contract」/「不走 loop，直接实现」。后者按普通任务做，完成后 spawn 一次 reviewer 即可。
2. `bl start --plan <plan> --session ${CLAUDE_SESSION_ID}` → 记下 `run_id`、`worktree`（候选，你的工作目录）。
3. **立刻放出 tester**：`Agent(subagent_type: "tester", prompt: "builder-loop run <run_id>，按注入的 brief 写测试")`。subagent 在后台跑，交卷时任务通知会唤醒你（Agent 工具若有 `run_in_background` 参数就设为 true）。它在另一个 worktree 的冻结基线上盲写测试，看不到你的实现——所以不用等它，马上开始写实现。
4. **顺手后台跑一次基线预检**：`bl preflight --session ${CLAUDE_SESSION_ID}`（Bash `run_in_background`）。它在 run 起点上把 pass_cmd 跑一遍，告诉你哪些 stage**本来就红**，省得之后对着与你无关的失败白查。跑的时候你照常写实现。
5. 之后你的 Write / Edit / Bash 都在候选 `worktree` 下；主仓只读。

## 2. 推进：跟着 `next_action` 走

`bl status --session ${CLAUDE_SESSION_ID}` 的 `readiness.next_action` 是唯一路标：

| next_action | 你做什么 |
|---|---|
| `spawn_tester` | 后台 spawn tester（见上），然后继续干自己的活 |
| `checkpoint` | 实现完成 → `bl checkpoint --role builder`。动手前想知道哪些路径会越界：`--dry-run`。被拒说明碰了不属于你的路径：测试问题交给 tester；确属本任务的实现文件 → 改 plan 的 authority，AskUserQuestion 得到同意后 `bl contract revise --plan <plan> --authorize` |
| `awaiting_tester` / `awaiting_reviewer` / `awaiting_gate` | 对应 agent 或门禁（后台的 machine / proof）正在跑。没有别的事就**直接结束这一轮**——它结束时你会被唤醒，Stop hook 此时放行。不要空转、不要重复 spawn / 重复启动 |
| `integrate` | `bl integrate`：把 tester 的测试并入候选 |
| `machine` | `bl machine`（耗时长就 `run_in_background`，Stop 会放行）。FAIL → 先看 `failure.baseline_red`：为真说明这一段在起点上同样失败，与你无关，改 loop.yml 走 contract revise。否则 Read `failure.log`：实现的错 → 修 → checkpoint；`failure.tester_files_mentioned` 非空且你判断是**测试写错了** → 你改不了测试，转交 tester |
| `resume_tester` | `SendMessage` 续接 tester（`status.agents.tester.agent_id`），正文直接用 `bl status` 的 `briefs.resume_tester`——**它只是门铃**，事实由 tester 自己 `bl brief` 取。你不需要在消息里复述候选路径、写边界或失败日志；要补充上下文（比如你的判断）可以附在门铃后面，但别指望它照做 brief 里没有的事 |
| `proof` | `bl proof`。FAIL 看 `failure.suggested_owner`：`tester` → resume_tester；`builder` → 修实现；`contract` → 判据本身跑不起来（如 proof_runner 与项目的 venv 不匹配），改 `.claude/loop.yml` 后 `bl contract revise --authorize` |
| `spawn_reviewer` | `Agent(subagent_type: "reviewer", prompt: "builder-loop run <run_id>，按注入的 brief 审查")`，然后结束这一轮等它交卷（`awaiting_reviewer`） |
| `resume_reviewer` | 按 owner=builder 的 findings 修 → checkpoint → machine → proof → `SendMessage` 给 reviewer，正文用 `briefs.resume_reviewer` |
| `finalize` | `bl finalize -m "type(scope): [cr_id_skip] Desc"`。`TARGET_DRIFT` → `bl rebase`（冲突在候选 worktree 里解，`git rebase --continue` 后再 `bl rebase`）→ 全部重验 |
| `needs_user` | 看 `blockers`。上限 / 无进展 / proof 反复同样失败：AskUserQuestion 让用户决定；用户说继续 → `bl resume --reason "<用户的原话或决定>"`（runtime 会核实 blocker 之后确有用户输入，你自己决定的不算）；放弃 → `bl abandon --reason`。`REVIEW_CONTRACT` → 需要改目标 / 写边界 / 验收标准，走 contract revise |
| `retro` | 见下一节 |
| `done` | 汇报 |

任何 `bl` 命令 exit 3 = 需要用户决定。Stop hook 在 run 未完成时会把你拉回来，属正常，按 stderr 提示继续。

**给角色发消息的两条规矩**：① `status.running.<role>` 为真时**不要发**——消息会夹在它的工具结果里到达，它没法验真，多半会拒绝执行；等它停下再续接。② 消息只当门铃，事实一律让它自己 `bl brief` 取。contract 改了也不用特意通知 tester：它的 evidence 会自动失效，`next_action` 自然变成 `resume_tester`。

**文档同步在 finalize 之前做**：候选 diff 若改了对外行为 / 契约 / 导航，按 `~/.claude/doc-policy.md` 判断哪些文档要改、内容归哪里，需要就改进候选（属于候选 diff，进 checkpoint）。reviewer 的 brief 会附一段启发式的文档引用线索，可能误报，逐条判断；复盘不再审文档。

## 3. 复盘（硬闸门）

finalize 或 abandon 之后 run 还没完：复盘记录写进 ledger 之前 Stop hook 会一直拦，也开不了下一个 run。

1. `bl retro signals` → runtime 从 ledger 派生的确定性信号（machine 失败次数、proof 失败、被拒的 checkpoint、contract 修订、用户授权续跑、角色返工轮数、rebase、stall 逃生……）。
2. 逐条判断去向，再补上信号之外**你自己在这次 run 里撞到的问题**（工具报错、提示误导、绕了弯路的地方）：
   - `business_issue`：业务仓库的缺陷或缺口
   - `builder_loop_issue`：builder-loop 自身的缺陷（hook / bl / prompt / 文档）
   - `not_incident`：正常现象，写明理由（如「两段式补 patch 的固定一轮」）
   同一因果链跨两个仓库就拆成两条。
3. 有要立项的 → AskUserQuestion 多选让用户勾，勾中的用 `file-issue` skill 逐个立项拿到 URL；用户不想立的记 `declined_by_user: true`。
4. 写 JSON 后 `bl retro record --file <json>`；没有信号也没有观察到问题时 `bl retro record --no-incident`。
5. 值得长期记住的平台行为 / 隐式约定，按 `~/.claude/doc-policy.md` 先判归属：接手的纯人工团队也需要知道的，是项目文档缺口，走 `business_issue` / `builder_loop_issue` 立项；只关乎 AI 协作、无法工程固化的，才一句话建议用户 `/memory`。

## 4. 汇报

`final_head`、做了哪几件事（1–5 条，一事一句）、tester 覆盖的 behaviors、reviewer 结论、复盘立了哪些 issue。
