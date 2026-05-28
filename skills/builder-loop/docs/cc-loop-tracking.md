# CC 官方自动化能力跟踪与借鉴

> 长期跟踪 Anthropic 官方「自动化 / 多步编排」能力演进：持续借鉴 + 避免冲突 + 在代码迭代闭环这个窄领域持续比官方更强。
> 当前跟踪两条线：
> - **`/loop`**（v2.1.79+）：LLM 主观判停的**通用步频再触发器**（cron / ScheduleWakeup）
> - **dynamic workflow**（v2.1.154+）：JS 脚本编排数十到数百 subagent 的**横向 fan-out 编排层**
>
> **边界**：cc-builder-loop = 机器判据驱动的**纵向收敛闭环**（PASS_CMD 客观判据 + worktree 隔离 + reward hacking 防御）。三者维度正交，**不要叠用**：/loop 是再触发器、workflow 是广度编排、本项目是深度收敛。

## 1. 官方版本快照

### 1.1 /loop（再触发器）

| CC 版本 | 模式 | 工具 | 续接通道 | 判停 |
|---------|------|------|---------|------|
| v2.1.79 | 静态 cron | `CronCreate` | cron 服务定时唤醒 | LLM 主观 |
| v2.1.121 | + 动态 self-pace | `ScheduleWakeup` | LLM 自填 prompt + delaySeconds∈[60, 3600] | LLM 主观（不调即结束） |

### 1.2 dynamic workflow（编排层）

| CC 版本 | 能力 | 本质 | 触发 | 判停 |
|---------|------|------|------|------|
| v2.1.154 | 后台编排 N 个 subagent（数十~数百） | Claude 写 JS 脚本（`agent()`/`parallel()`/`pipeline()`），runtime 后台执行 | 直说「create a workflow」/ 开 `ultracode`（effort=xhigh 自动判定） | LLM（agent 自报 + 可选对抗验证），终判仍 LLM |

要点：
- **半临时**：脚本默认即用即弃，落盘 session 目录（返回 scriptPath）；可 `resumeFromRunId`（journal 缓存未变前缀）/ 存 named workflow（`.claude/workflows/`）复用
- **隔离**：支持 `isolation:'worktree'`（per-agent worktree，slug `wf_<runId>-<idx>`，防并行写冲突）
- **资源**：token budget 感知（`budget.remaining()`）、并发 cap `min(16, cores-2)`
- **可用性**：研究预览，Max/Team/Enterprise + API/Bedrock/Vertex/Foundry（企业默认关）

**复查命令**（每次 CC 升级跑一次）：
```bash
npm ls -g @anthropic-ai/claude-code
strings $(which claude)*.exe 2>/dev/null | tr ';' '\n' | grep -iE 'ScheduleWakeup|<<autonomous-loop|/loop —|WorkflowTool|dynamic workflow|ultracode'
```
出现新 sentinel / 新工具 / 新模式 → 当期更新本表 + 评估借鉴清单。

## 2. 借鉴清单

| 借鉴点 | 来源 | 状态 | 落地位置 |
|--------|------|------|---------|
| `reason` 字段（短句解释决策） | `ScheduleWakeup` schema | 待评估 | `loop-trace.jsonl` 每行加 `decision_reason` |
| 迭代价值自检（time vs observable event） | `/loop` dynamic prompt | 待评估 | `prompts/judge-system.md` |
| `delaySeconds` 上下界 clamp | `ScheduleWakeup` runtime | 待评估 | judge agent timeout / 重试间隔参数化 |
| 凭证三层 fallback + env file | 自研早于官方 | 已采纳（V1.9/V2.1） | `run-judge-agent.sh` |
| 静态 cron 路径 | `CronCreate` | 不采纳 | 与 PASS_CMD 硬门禁冲突 |
| **schema 强制结构化输出**（工具层校验+重试） | workflow `StructuredOutput` | 待评估（P0） | judge/reviewer 输出契约，替正则解析（呼应原则六契约）|
| **token budget 感知**（动态迭代上限） | workflow `budget.remaining()` | 待评估（P0） | `max_iterations` 死数字 → budget 感知上限 |
| **pipeline 无 barrier 流水线** | workflow `pipeline()` | 待评估（P0） | 多任务并行时 item 各自流动，不卡 barrier |
| **resume journal**（缓存未变前缀） | workflow `resumeFromRunId` | 待评估（P0） | 崩溃恢复，比 V3.3 orphan 检测更干净 |

> P0 四项来源于 dynamic workflow，方向是**强化上层 LLM 判据 / 工程化**，不替换地基层 PASS_CMD（见 design-philosophy 原则一「判据分层」）。

## 3. 互斥防御

### 3.1 /loop

**用户层禁忌**：cc-builder-loop 已激活的项目**不要**叠用 `/loop` dynamic。`/loop` wake-up 不感知 builder-loop state → 撞 worktree / PASS_CMD 中途 / state 锁未释放。
**实现层防御**（待做）：探测当前对话是否在 `/loop` dynamic context（wake-up 标记），主动 skip bootstrap。

### 3.2 dynamic workflow

撞车按 workflow 是否隔离分两种（源码证据：`tools/AgentTool/runAgent.ts` / `utils/worktree.ts`）：

| workflow 形态 | cwd | 撞车风险 | 现状 |
|---------------|-----|---------|------|
| **隔离**（`isolation:'worktree'`） | `wf_<runId>-<idx>` 临时 worktree | 低 | **已天然防御**：cwd 无对应 builder-loop state → `locate-state.sh` 返空 → guard 脚本 `exit 0` skip |
| **inline**（默认） | builder-loop 主仓/worktree | **高** | **探测信号不足**（见下），降级用户层禁忌 |

**inline 撞车机制**（builder-loop active 期间在同会话开 inline workflow）：
1. `subagent-start-guard.sh` 用 cwd（主仓）定位到 builder-loop state → 落锁 + 注入 worktree 边界
2. 并行 N 个 workflow agent 覆盖同一 `cc-subagent-<session_id>.lock`（同 session_id）→ 锁混乱
3. `worktree-write-guard.sh` 用该锁把 workflow agent 的写强制限制进 builder-loop worktree → workflow 写自己的目标被拦

**探测信号现状**（关键发现，2026-05-29 查 CC 源码）：
- `SubagentStart` 输入只有 `session_id` / `cwd` / `agent_id` / `agent_type`（`coreSchemas.ts` SubagentStartHookInputSchema）——**无 `agent_transcript_path`**，拿不到 workflow 的 `subagents/workflows/<runId>/` 路径信号
- `agent_transcript_path` 仅 `SubagentStop` 输入才有
- → inline workflow agent 在 **start 阶段无干净信号**与 builder-loop 自己的 reviewer/tester 区分（cwd 同为主仓）

**实现层防御**（improvements 立项，待条件成熟）：
- 路径 A：`agent_type` 反向白名单——只对已知 builder-loop agent（reviewer/tester/arbiter/doc-maintainer）落锁+注入，其余 skip（行为变更，需 e2e）
- 路径 B：等 CC 在 `SubagentStart` 也暴露 `agent_transcript_path`，按 `/workflows/` 路径探测（最干净）
- §1 复查命令已加 `WorkflowTool` sentinel，CC 升级时跟踪该字段是否补齐

## 4. 持续超越官方的方向

| 维度 | /loop dynamic | dynamic workflow | cc-builder-loop |
|------|---------------|------------------|-----------------|
| 编排维度 | 时间（再触发） | 广度（fan-out N agent） | 深度（迭代收敛） |
| 判据 | LLM 主观 | LLM（自报+对抗验证），终判 LLM | PASS_CMD 退出码（客观二值）+ judge 二级 |
| 隔离 | 无 | per-agent worktree（防写冲突） | worktree + tester 锁 + reviewer 时序闸 |
| 失败回滚 | 无 | item throw → 落 null 跳过 | 三档合回 + arbiter 仲裁 |
| Reward hacking 防御 | 无 | 无（靠对抗验证缓解） | Layer1 LLM + Layer2 正则双层 |
| 可观测 | runtime telemetry（用户不可见） | `/workflows` 进度树 | NDJSON trace + judge-trace（本地） |

新发布的官方能力优先按"借鉴清单 → 状态评估 → 落地"三步走，避免照搬。**正交可组合的理想态**：workflow 把大任务 fan-out 成 N 个子任务，每个子任务内部用 builder-loop 式机器判据收敛（workflow stage 里 `agent()` 跑一个 PASS_CMD 闭环）。

## 5. 维护节奏

- 每次 `claude` 升级：跑 §1 复查命令 + 更新两条线快照表
- 每次 cc-builder-loop 大版本：复盘借鉴清单状态变迁
- 用户反馈撞车 case → 立即在 §3 对应小节补条目 + 同步 known-risks
- CC 在 `SubagentStart` 补 `agent_transcript_path` → §3.2 实现层防御转「可做」
