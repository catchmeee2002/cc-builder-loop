# builder-loop V8 架构

## 边界

```
Claude Code 原生                     builder-loop runtime（bl）
─────────────────                    ─────────────────────────
主 session（/builder）  ──bl start──▶ contract 冻结、候选 worktree、ledger、session 绑定
   │ Write/Edit 候选     ──checkpoint▶ 写边界校验 + git commit（唯一进入候选的途径）
   │                     ──machine───▶ pass_cmd 在候选内执行 → machine evidence
   ├─ Agent(tester)  ─SubagentStart─▶ 登记 agent_id，注入 worktree/边界/behaviors
   │      └──────────SubagentStop──▶ 解析 BUILDER_LOOP_RESULT → tester checkpoint + evidence + proof_spec
   │                     ──proof─────▶ 候选全绿 → baseline-red / mutation / reviewed-boundaries → proof evidence
   ├─ Agent(reviewer) ─SubagentStart─▶ 注入 diff 范围、前置 evidence 摘要
   │      └──────────SubagentStop──▶ reviewer evidence（绑候选 HEAD + 前置 evidence digest）
   │  SendMessage 续接同一 agent_id（复审 / 修 proof）
   └─ Stop hook ◀────────────────── run 未终态 → exit 2 + next_action；waiting_for_user / 停滞 3 次 → 放行
                         ──finalize──▶ commit-tree + update-ref CAS → 删 worktree → terminal
```

runtime 不保存"下一步让谁做"：`readiness()` 每次从 evidence 状态派生 `next_action`。ledger 唯一写入者是 `bl`（`ledger.mutate` flock + seq+1 + 原子写）；模型改 ledger 或绕过 checkpoint 提交都会在下一次 `assert_candidate_identity` 时被拒。

## 模块

| 模块 | 职责 |
|---|---|
| `config` | `.claude/loop.yml` → `pass_cmd[] / max_iterations / worktree.root`；PyYAML 缺席时用内置子集解析 |
| `contract` | 标签提取、校验、三面 digest、`classify_change`（MISSION_REVISION / AUTHORITY_EXPAND / ASSURANCE_DOWNGRADE）、glob 匹配 |
| `ledger` | schema 校验、mutate、session 指针（`~/.claude/builder-loop/sessions/<sid>.json`，读后回核 owner） |
| `worktree` | 候选 worktree（`../builder-loop-worktrees/<repo>/<run_id>`，分支 `builder-loop/<run_id>`）、临时 worktree 上下文、身份/clean 断言 |
| `run` | start / status / checkpoint（按角色写边界分类：protected → tester_owned / builder_owned → outside_authority）/ abandon / contract revise |
| `machine` | pass_cmd 三态（PASS / FAIL / FATAL）、超时、执行后 worktree 变更检测、失败签名归一化 |
| `evidence` | 投影、`state()`（missing / pass / fail / stale）、`readiness()` |
| `proof` | spec 校验（group↔behavior 双射、命令白名单、patch 路径约束）、四步执行、失败签名 |
| `finalize` | 前置检查、commit-tree（可选 `--run-commit-hook` 在临时 worktree 跑 hook 并比对 tree）、intent、CAS、checkout 同步、恢复、rebase |
| `hooks` | 六个 handler + 结果标记解析 + 角色上下文注入 |
| `doctor` | 只读诊断 |

## evidence 投影（dependency_digest 的输入）

| kind | 投影 | 何时 stale |
|---|---|---|
| machine | 三面 digest、候选 HEAD、tester 文件 `{path, blob}` | 任何 checkpoint、tester 提交、contract 变化 |
| tester | tester 文件 blob、mission digest | 测试文件被改/删、mission 变 |
| proof | 候选 HEAD、tester 文件、behavior ids、proof_spec digest | 候选或测试变化、spec 变化 |
| reviewer | 候选 HEAD、三面 digest、machine/tester/proof 各自 `{status, dependency_digest, state}` | 候选变化、任一前置证据变化或失效 |

tester 文件集合 = 所有 `role=tester` checkpoint 提交路径的并集（在当前候选上仍存在者）；`declared_files` 只作参考。

## readiness → next_action

顺序：terminal → `done`；有 blocker → `needs_user`；无 checkpoint → `checkpoint`；machine 非 pass → `machine`；tester 非 pass → `spawn_tester` / `resume_tester`；proof 非 pass → `proof`（proof fail 且 `suggested_owner=tester` 且 tester 已登记 → `resume_tester`）；reviewer 非 pass → `spawn_reviewer` / `resume_reviewer`；否则 `finalize`。

blocker：`WAITING_FOR_USER`（AskUserQuestion 挂起）、`MAX_ITERATIONS`（machine_iter ≥ 上限且未 pass）、`NO_PROGRESS`（machine 同签名 3 次）、`PROOF_STALL`（proof 同签名 3 次）。

## hook 接线

| event | matcher | 逻辑 |
|---|---|---|
| Stop | — | 无绑定 / 终态 / waiting → exit 0；`stop_hook_active` 且 seq 未变连续 3 次 → exit 0 + 提示；否则 exit 2 + readiness |
| SubagentStart | tester\|reviewer | 登记 `agents[role].agent_id`；`additionalContext` 注入角色上下文 |
| SubagentStop | tester\|reviewer | 只认登记 agent_id；标记缺失/不合规 → exit 2（≤2 次）→ 第 3 次记 fail；tester：checkpoint(role=tester) + validate_spec + evidence；reviewer：evidence |
| PreToolUse | AskUserQuestion | 写 `waiting_for_user` |
| PreToolUse | EnterWorktree | deny |
| PreToolUse | Write\|Edit\|MultiEdit | 仅对 tester/reviewer：reviewer 全拒；tester 只允许候选 worktree 内 tester_write |
| PostToolUse | AskUserQuestion | 清 waiting |
| UserPromptSubmit | — | 清 waiting |

所有 hook 首步 `lookup_session(session_id)`；hook 的 `cwd` 不参与定位。hook 内部错误只写 stderr 并 exit 0。

`agent_type` 对自定义 agent 返回其 frontmatter `name`（2026-09-16 在 CC 2.1.272 实测：matcher 生效、`last_assistant_message` 完整携带标记行），因此角色身份判定无需回退到 PreToolUse(Agent) 预登记。run 绑定时每次 hook 调用记一行 `~/.claude/builder-loop/hook-trace.jsonl` 供排查。

## 失败路径

| 情况 | 行为 |
|---|---|
| machine FAIL | evidence fail + failures.machine 追加；`repeat_count` 与 `remaining_iterations` 返回给 builder |
| pass_cmd 改了候选文件 | `failure.worktree_mutated`，视为 FAIL |
| proof 失败 | `failure.code` ∈ TEST_PROOF_CANDIDATE_FAILED / TEST_BASELINE_RED_NOT_PROVEN / TEST_MUTATION_SURVIVED / TEST_MUTATION_INVALID / PROOF_WORKTREE_MUTATED；`suggested_owner` 指向 builder 或 tester |
| reviewer blocked / blocking finding | evidence fail；builder 走 AskUserQuestion |
| 目标分支前进 | finalize → TARGET_DRIFT；`bl rebase` 在候选 worktree rebase，冲突返回文件列表由 builder 手工解；成功后 target_start_head 更新，全部 evidence 自然 stale |
| 主仓 dirty 与变更路径重叠 | finalize → DIRTY_OVERLAP，不写回 |
| finalize CAS 后中断 | ledger 留 `finalize_intent`；再次 finalize 按目标分支当前 HEAD（== expected 重放 CAS / == final 完成收尾 / 其他 → DIVERGED）恢复 |
| commit hook 改写 tree | `--run-commit-hook` 模式下 FINAL_COMMIT_TREE_MISMATCH，目标分支不动 |
| abandon | 终态 abandoned，worktree 与分支保留，session 解绑 |

## 首版未做（演进方向）

machine evidence 的 affects/exempt scope（当前每次 checkpoint 全量重跑）；`acceptance_cases` blackbox；reviewer 后台模式；`last_assistant_message` 超长截断边界（兜底为 exit 2 要求重发）。
