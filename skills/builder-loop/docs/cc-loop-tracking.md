# CC 官方 `/loop` 跟踪与借鉴

> 长期跟踪 Anthropic 官方 `/loop` skill 演进：持续借鉴 + 避免冲突 + 持续比官方更强。
> **边界**：cc-builder-loop = 机器判据驱动的**代码迭代闭环**（PASS_CMD 客观判据 + worktree 隔离 + reward hacking 防御）；官方 `/loop` = LLM 主观判停的**通用步频再触发器**。判据维度不同，**不要叠用**。

## 1. 官方版本快照

| CC 版本 | 模式 | 工具 | 续接通道 | 判停 |
|---------|------|------|---------|------|
| v2.1.79 | 静态 cron | `CronCreate` | cron 服务定时唤醒 | LLM 主观 |
| v2.1.121 | + 动态 self-pace | `ScheduleWakeup` | LLM 自填 prompt + delaySeconds∈[60, 3600] | LLM 主观（不调即结束） |

**复查命令**（每次 CC 升级跑一次）：
```bash
npm ls -g @anthropic-ai/claude-code
strings $(which claude)*.exe 2>/dev/null | tr ';' '\n' | grep -iE 'ScheduleWakeup|<<autonomous-loop|/loop —'
```
出现新 sentinel / 新工具 / 新模式 → 当期更新本表 + 评估借鉴清单。

## 2. 借鉴清单

| 借鉴点 | 来源 | 状态 | 落地位置 |
|--------|------|------|---------|
| `reason` 字段（短句解释决策） | `ScheduleWakeup` schema | 待评估 | `loop-trace.jsonl` 每行加 `decision_reason` |
| "What makes the next iteration worth running — time vs observable event?" 自检 | `/loop` dynamic prompt | 待评估 | `prompts/judge-system.md` |
| `delaySeconds` 上下界（60–3600）clamp | `ScheduleWakeup` runtime | 待评估 | judge agent timeout / 重试间隔参数化 |
| 凭证三层 fallback + env file | 自研早于官方 | 已采纳（V1.9/V2.1） | `run-judge-agent.sh` |
| 静态 cron 路径（用户指定 interval 跳过 LLM） | `CronCreate` | 不采纳 | 与 PASS_CMD 硬门禁冲突 |

## 3. 互斥防御

**用户层禁忌**：cc-builder-loop 已激活的项目**不要**叠用 `/loop` dynamic。`/loop` wake-up 时机不感知 builder-loop state → 撞 worktree 跑到一半 / PASS_CMD 中途 / state 锁未释放。

**实现层防御**（v3 待做）：探测当前对话是否在 `/loop` dynamic context（wake-up 标记），主动 skip bootstrap。

## 4. 持续超越官方的方向

| 维度 | 官方 `/loop` dynamic | cc-builder-loop |
|------|----------------------|-----------------|
| 判据 | LLM 主观判停 | PASS_CMD 退出码（客观二值）+ judge LLM 二级 |
| 隔离 | 无（主对话流） | worktree + tester 锁 + reviewer 时序闸门 |
| 失败回滚 | 无 | 三档合回 + arbiter 仲裁 |
| Reward hacking 防御 | 无 | Layer 1 LLM + Layer 2 正则双层 |
| 可观测 | runtime telemetry（用户不可见） | NDJSON trace + judge-trace（项目本地）|

新发布的官方能力优先按"借鉴清单 → 状态评估 → 落地"三步走，避免照搬。

## 5. 维护节奏

- 每次 `claude` 升级：跑 §1 复查命令 + 更新版本快照表
- 每次 cc-builder-loop 大版本（V2.x → V3）：复盘借鉴清单状态变迁
- 用户反馈撞车 case → 立即在 §3 互斥防御补条目 + 同步 known-risks R7
