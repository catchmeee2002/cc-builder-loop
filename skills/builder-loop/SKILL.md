---
name: builder-loop
description: "Builder 自闭环迭代 — 在 builder 完成改动后，以项目根 .claude/loop.yml 定义的 PASS_CMD（lint/type/test 多阶段）作为硬门禁，失败自动把错误喂回 builder 再跑一轮，直到 PASS 或命中上限/早停。Stop hook 截获 + 状态文件喂回机制，机器判定代替主观评审。Triggers on: builder 完成动作时 hook 自动触发；用户显式 /builder-loop；用户说『配置 loop』『接入 loop』『setup loop』『init loop』『给这项目配自闭环』时进入接入向导（生成 loop.yml）。"
---

# Builder Auto-Loop — 机器判定的多轮自闭环

## 核心规则

1. **触发**：项目根有 `.claude/loop.yml` → builder 调 `setup-builder-loop.sh` → Stop hook 自动接管循环
2. **完成判定**：`loop.yml.pass_cmd` 数组顺序执行（lint→type→test），全过即 PASS
3. **失败反馈**：`extract-error.sh` 处理日志，通过状态文件注入下轮 prompt
4. **上限与早停**：硬上限 `max_iterations`（默认 5）+ 智能早停（无进展/反增长/保护路径被改）
5. **worktree 隔离**：`worktree.enabled=true` 时创建 git worktree，PASS 后三档合回
6. **⛔ 禁用 CC 内置 worktree**：禁止调用 `EnterWorktree` / `ExitWorktree`。worktree 一律由 `setup-builder-loop.sh` 通过 git CLI 创建
7. **循环外接力**：PASS 后 builder.md 接力（reviewer → doc-maintainer → commit）

## 启动流程

```bash
bash ~/.claude/skills/builder-loop/scripts/setup-builder-loop.sh "$TASK_DESCRIPTION"
```

读 loop.yml → 可选 worktree → 生成状态文件 `.claude/builder-loop/state/<slug>.yml`（iter=0 / HEAD / 配置快照）→ 首次 PASS_CMD。

**多状态并行**：每个 worktree loop 独占一份 state 文件，文件名 = branch slug（如 `1777040807-task-alpha.yml`）。bare loop（无 worktree）固定 `__main__.yml`。同项目可并行多个 loop 互不干扰，hook 按当前 CWD 通过 `locate-state.sh` 找到对应 state。

### setup-builder-loop.sh 选项与退出码

**选项**（均可选）：

| 选项 | 说明 | 默认 |
|------|------|------|
| `--no-worktree` | 强制 bare 模式（无 git worktree） | 读 loop.yml.worktree.enabled |
| `--no-stash` | 跳过主仓 dirty stash（V2.3+） | 仅当 --touched-files 才 stash |
| `--touched-files <a,b,c>` | 仅 stash 指定文件（逗号分隔）到 worktree | 不 stash |
| `--reuse-worktree <path>` | 复用已有孤儿 worktree（V3.3+）；`<path>` 必须绝对路径 | 新建 worktree |
| `--ignore-orphans` | 跳过孤儿 worktree 检测，直接新建（V3.3+） | 检测孤儿 |
| `<TASK_DESCRIPTION>` | 任务描述（用作 branch slug / state 文件名）| 必须 |

**互斥校验**：`--reuse-worktree` 和 `--no-worktree` 不能同时使用。

**退出码**：

| 码 | 含义 |
|----|------|
| 0 | 成功，状态文件已生成，首次 PASS_CMD 待执行 |
| 1 | 配置错误：项目根缺 `.claude/loop.yml` 或格式无效 |
| 2 | worktree 操作失败（git worktree add 失败 / --reuse-worktree 路径无效 / flag 互斥冲突 / git 特殊状态） |
| 3 | 探测失败（配置项无法解析） |
| 4 | 同 slug 已有 active loop（state 文件存在且 active=true）；手动 `rm <state_file>` 后重试 |
| 5 | Lock 超时（10s 内无法获取 setup lock，可能另一 setup 在运行） |
| 6 | 孤儿 worktree 检测（V3.3+）：目录存在但无对应 active state；stderr 列出选项：`--reuse-worktree <path>` / `--ignore-orphans` / 手动清理 |

## Subagent lifecycle hooks（V3.5 来源身份层）

V3.5 引入 subagent 白名单管理，确保只有预期的 agent 类型能落隔离锁。

**SubagentStart hook** (`~/.claude/scripts/subagent-start-guard.sh`)：
- 触发时机：任何 subagent 启动（无论哪个 session / agent_type）
- 行为：检查 `agent_type` 是否在白名单 `[tester, doc-maintainer, arbiter, reviewer]` 内 **且** `state.phase=active`
  - 白名单内 ✅ + active ✅ → 写 per-agent-type 锁文件 (`cc-subagent-{session_id}-{agent_type}.lock`)
  - 白名单内但非 active（如 `phase=passed_pending_review`） → 静默跳过（reviewer 等待中不落锁）
  - 非白名单 agent（如 inline workflow / unknown type） → 静默跳过（不受管）
  - 同时注入 worktree 边界上下文（source_dirs / test_dirs 隔离）
  - V4.3: tester / reviewer → 额外写 `state.subagents.<type>` 段（agent_id / started_at / status:running）

**SubagentStop hook** (`~/.claude/scripts/subagent-lock-clear.sh`)：
- 触发时机：任何 subagent 结束
- 行为：扫当前 session 的所有活跃锁（新旧格式），清除匹配当前 `agent_type` 的锁
  - 采用 TTL 1800s（30min）防止陈旧锁累积
  - 识别并清理旧锁格式 `cc-subagent-{session_id}.lock`（向后兼容）
  - V4.3: tester / reviewer → 更新 `state.subagents.<type>.status` = idle + `transcript_path`

**V3.5 前后对比**：

| 维度 | V3.4- | V3.5+ |
|------|-------|-------|
| 锁文件命名 | `cc-subagent-{session_id}.lock`（全局单锁） | `cc-subagent-{session_id}-{agent_type}.lock`（按类型分离） |
| SubagentStart 条件 | 无条件落锁 | 白名单 + active state 双条件 |
| SubagentStop 覆盖范围 | 仅 tester | 所有 managed agents |
| 支持场景 | 单 session 单 agent 类型 | 单 session 多 agent 类型并发（如 doc-maintainer + tester 同时跑） |

## Stop Hook

`~/.claude/scripts/builder-loop-stop.sh`：按 CWD 调用 `locate-state.sh` 找本 worktree 对应 state → 检测 active=true → 多层闸过滤非目标场景 → 跑 PASS_CMD。

**V3.0 多层闸（PASS_CMD 之前自动识别非目标场景静默退出）**：

| 闸 | 触发条件 | 静默原因 |
|----|---------|---------|
| L1 | `state.phase=passed_pending_review\|e2e_pending` | 等 reviewer 审查 / tester 跑 e2e（自愈：dirty/新 commit → active；e2e_pending + verified_head==HEAD → active） |
| L2A | transcript 末尾是 pending AskUserQuestion（无 tool_result） | builder 在等用户答 |
| L2B | worktree HEAD == `state.last_iter_head` 且 git status 空 | builder 在思考 / 讨论，没改代码 |
| L3 | `.claude/builder-loop/<slug>.pause` 文件存在 | builder 主动 pause |

**PASS_CMD 通过后**（worktree / bare 统一，V4.1）：
- 调 `loop-commit.sh` 在 `project_root` 内 commit + 写 `state.phase=passed_pending_review` + 写 `reviewer_pending` 段 + 落盘 `reviewer-diff-<slug>.txt`。Builder 收 stderr 提示 → spawn reviewer → 反馈分支：
  - 0🔴 通过 → builder 调 `merge-and-cleanup.sh <state>`（worktree: ff merge + 删 worktree + 删 state；bare: stash drop + 删 state）
  - 🟡/🔵 → builder 修复 → dirty 触发 L1 自愈回 active → 下一轮 PASS_CMD
  - 🔴 阻塞 → AskUserQuestion 让用户选 [继续修 / abandon-loop.sh]
- **FAIL** → extract-error + early-stop-check → 写回状态文件 → 注入下轮

### CWD→state 匹配（V3.4）

stop hook 通过 `locate-state.sh` 用 CWD 匹配 state 文件（策略 2: worktree 路径推 slug → 策略 3: worktree_path 字段匹配 → 策略 4: bare 模式 `__main__.yml` → 策略 5: 唯一 active 自动绑定）。无匹配 = exit 0 放行。V3.4 起 `.claude/builder-loop.local.md` 已移除，多 session 并发各用各的 worktree CWD 天然隔离。

## 状态文件 schema（`.claude/builder-loop/state/<slug>.yml`）

```yaml
active: true                     # V3.x 后渐进下掉（仅写不读做新决策；详见 improvements.md「active 下掉计划」）
phase: "active"                  # V3.0 新增：active / e2e_pending / passed_pending_review；hook 主判用此字段
slug: "1777040807-task-alpha"    # = 文件名；bare loop 时 slug="__main__"
owner_cwd: "/path/to/main-repo"  # setup 时所在 CWD（一般 = main_repo_path）
owner_session_id: "abc123..."    # V3.7 新增：stop hook 首次绑定时写入的 CC session_id
                                 # 后续 stop hook 校验匹配，不匹配 → 静默跳过（防并发 session 越界）
iter: 3
max_iter: 5
last_iter_head: abc1234          # V3.0 新增：上一轮 PASS_CMD 后 worktree HEAD short sha；L2B 闸用
project_root: /path/to/worktree  # V2.0 起 = "干活的地方"（worktree 启用 = worktree path / bare = 主仓）
                                 # PASS_CMD 在此跑、loop.yml 从此读，所以 worktree 内改 loop.yml 加 stage 立即生效
main_repo_path: /path/to/main    # V2.0 起新增；永远是主仓（git merge / branch / worktree prune 在此）
                                 # 老 V1.x state 缺该字段时下游脚本按"project_root 等于主仓"的旧语义兜底
start_head: abc1234              # setup 时主仓 HEAD
worktree_path: /path/...         # worktree 启用时 = project_root；bare 时为空
worktree_mode: clean             # V2.3 新增：clean / selective / bare / reuse
                                 #   clean    = worktree from HEAD（无 stash）
                                 #   selective = 主仓 dirty 已 stash 并 apply 到 worktree（V3.2+ 替代 dirty）
                                 #   bare     = --no-worktree（直接跑主仓，V4.1 起与 worktree 行为对齐：reviewer-as-gate）
                                 #   reuse    = 复用已有孤儿 worktree（V3.3+ --reuse-worktree）
pre_loop_stash_ref: ""           # V2.3 新增：worktree_mode=dirty 时的 git stash commit hash
                                 # 用 commit hash 不用 stash@{N} index — 多 builder 并行安全
                                 # PASS 路径 merge 后自动 drop / EARLY_STOP 路径 apply 还原主仓
pre_loop_dirty_files: ""         # V2.3 新增：进 stash 的文件清单（逗号分隔）
                                 # auto-commit message body 列文件用，让 PR review 看到边界
task_description: |
  ...
source_dirs: "src,lib"
test_dirs: "tests,spec"
last_pass_stage: test
last_error_hash: deadbeef
last_error_count: 7
stopped_reason: ""
cleanup_phase: ""                # V3.0 新增：merge-and-cleanup.sh 幂等用 — ff_merged / worktree_removed
created_at: "2026-04-18T..."

# V3.0 reviewer-as-gate 段（仅 phase=passed_pending_review 时存在）
reviewer_pending:
  pass_start_head: "abc1234"     # loop 起始 HEAD
  reviewer_files: "a.py,b.py"    # 改动文件 — comma-separated string（builder 解析时 split(',')；不是 YAML list）
  diff_file: ".../reviewer-diff-<slug>.txt"
  report_path: ".../review_reports/<proj>_<slug>_<ts>.md"
  written_at: "2026-05-09T..."

# V4.0 plan 路径（plan 含 <!-- plan-checklist --> 或 <!-- e2e-cases --> 标签时写入）
plan_path: ".claude/plans/20260620-xxx.md"      # 通用 plan 文件路径（setup 时写入）

# V4.3 subagent identity 段（tester + reviewer 写入，SubagentStart/Stop hook 自动维护）
subagents:
  tester:
    agent_id: "a0a40ff29f9fd0741"               # CC SubagentStart hook 提供的 agent_id
    started_at: "2026-06-24T01:00:00+08:00"
    status: "running"                            # running（SubagentStart 写）| idle（SubagentStop 写）
    transcript_path: ""                          # SubagentStop 时写入 agent_transcript_path
  reviewer:
    agent_id: "b1b51gg30g0ge1852"
    started_at: "..."
    status: "idle"
    transcript_path: "/path/..."

# V3.8 e2e behavioral verification 段
e2e_verified_head: "abc1234"                    # e2e 验收通过时的 HEAD commit；与当前 HEAD 一致则跳过 e2e

# (V4.0 废弃) e2e_plan_path — 改用 plan_path，stop hook 读时 fallback
# e2e_plan_path: ".claude/plans/20260620-xxx.md"

# (V4.0 废弃) V1.9 judge agent 字段 — judge 已被 reviewer Phase 0 吸收
# last_judge_action / last_judge_confidence / last_judge_ts / consecutive_nudge_count
# judge_active_model / judge_consecutive_failures
```

### Subagent 锁文件 schema（V3.5+）

文件路径：`/tmp/cc-subagent-{session_id}-{agent_type}.lock`（或 `$ISOLATION_LOCK_DIR` 若已设定）

文件格式（YAML）：
```yaml
session_id: "abc1234def"              # CC session ID
agent_type: "tester"                  # managed agent 类型（tester / doc-maintainer / arbiter / reviewer）
start_ts: 1718374800                  # 锁创建时间戳（秒）
pid: 12345                            # hook 进程 ID
```

**V3.5 主要变更**：

- **旧格式**（V3.4-）：`/tmp/cc-subagent-{session_id}.lock`（单一锁文件，所有 agent 共用）
- **新格式**（V3.5+）：`/tmp/cc-subagent-{session_id}-{agent_type}.lock`（按 agent_type 分离，支持并发不同 agent）
- **后向兼容**：subagent-lock-check.sh 和清理脚本都识别旧格式，不强制迁移；旧 lock 文件 TTL 1800s（30min）自动清理

**Managed agent 白名单**（仅这些类型落锁）：`tester`, `doc-maintainer`, `arbiter`, `reviewer`

**公共函数库**：`~/.claude/scripts/lock-utils.sh`（V3.5 新增）提供：
- `bl_lock_path <session_id> <agent_type>` — 新锁文件路径
- `bl_legacy_lock_path <session_id>` — 旧锁文件路径（兼容）
- `bl_find_active_locks <session_id>` — 查找一个 session 的所有活跃锁（新旧格式）
- `bl_read_lock_field <lock_file> <field>` — 读锁文件字段
- `bl_cleanup_stale_locks <session_id> [ttl_sec]` — 清理超期锁
- `bl_is_managed_agent <agent_type>` — 检查是否白名单内 agent

### 旧 schema 迁移

V1.6 之前的旧 state 或残留的 `.claude/builder-loop.local.md` 可一次性迁移：

```bash
bash ~/.claude/skills/builder-loop/scripts/migrate-state.sh <project_root>
```

脚本会把旧 state 搬到新目录。V3.4 起 `local.md` 已移除（stop hook 改用 CWD→state 匹配），残留文件被静默忽略。

## 与其他 loop 类 skill 共存

用独立状态文件和 Stop hook 入口，不与其他 loop skill 共享状态。Stop hook 仅在检测到本 skill 状态文件时介入，否则放行。

## 智能提示（builder.md 无 loop.yml 时引用此段）

按以下顺序评估，命中豁免 → 静默跳过（直接走 Reviewer）：

1. **白名单**：`git remote get-url origin` 含 luna6/app → 跳过
2. **用户已拒**：项目根有 `.claude/loop-init-skipped` → 跳过
3. **无测试栈**：`probe-project-stack.sh` 输出 `test_framework == "unknown"` → 跳过

都没豁免 → AskUserQuestion 问用户：
- 是，现在配 → 进入下方接入向导
- 这次不要 → 跳过，不写标记
- 永远别问 → `touch .claude/loop-init-skipped`

无论选什么，本次 builder 继续走 Reviewer（不阻断）。

## 接入向导（用户说「配置 loop」时执行）

### Step 1: 探测项目栈

```bash
bash ~/.claude/skills/builder-loop/scripts/probe-project-stack.sh <项目根>
```

输出含 language / test_framework / lint_tools / source_dirs / test_dirs / recommended_pass_cmd。

### Step 2: AskUserQuestion x5

1. **通过条件**：recommended_pass_cmd 全套 / 只测试 / Other
2. **测试目录**：探测到的 test_dirs / Other
3. **上限轮数**：3 / **5（推荐）** / 10
4. **smoke test**：**是（推荐）** / 否
5. **worktree 隔离**：**否（简单项目）** / 是（多人协作）

### Step 3: 写 loop.yml

```bash
echo '<choice JSON>' | bash ~/.claude/skills/builder-loop/scripts/init-loop-config.sh <项目根>
```

choice JSON 含 pass_cmd / max_iterations / layout / worktree。

**choice JSON 格式示例**（pass_cmd 必须是对象数组，不是纯字符串数组）：

```json
{
  "pass_cmd": [
    {"stage": "doc-lint", "cmd": "bash ~/.claude/skills/builder-loop/scripts/doc-lint.sh", "timeout": 10},
    {"stage": "lint", "cmd": "ruff check src/", "timeout": 60},
    {"stage": "test", "cmd": "pytest tests/ -x", "timeout": 300}
  ],
  "max_iterations": 5,
  "layout": {"source_dirs": ["src"], "test_dirs": ["tests"]},
  "worktree": {"enabled": false}
}
```

### Step 4: smoke test

```bash
bash ~/.claude/skills/builder-loop/scripts/run-pass-cmd.sh <项目根> 0
```

PASS → `✅ 配置可用`；FAIL → `⚠️ smoke test 失败，请检查`（不阻断）。

### Step 5: 汇报

```
✅ 已接入 builder-loop
   配置文件：<项目根>/.claude/loop.yml
   PASS_CMD 阶段：<N> 个
   smoke test：<结果>
```

## 判定 agent（V1.9+）

PASS_CMD 二值判据之上叠加一道 **LLM 语义判定**，识别假完成 / 求助 / 偷懒 / 网络中断等盲区。

| 制品 | 路径 |
|------|------|
| 调用脚本 | `skills/builder-loop/scripts/run-judge-agent.sh` |
| 系统 prompt | `skills/builder-loop/prompts/judge-system.md` |
| 配置段 | `loop.yml.judge`（全部可选；缺省 enabled=true，凭证缺失自动降级回 PASS_CMD） |
| Telemetry | `.claude/builder-loop/judge-trace.jsonl`（每次 judge 调用一行 + outcome 后置补标） |
| 详细架构 | `docs/judge-agent.md` |
| 已知风险 | `known-risks.md`（R1~R4 开口项） |

**核心契约**：

- 三态判定：`continue_nudge` / `stop_done` / `retry_transient`（FAIL 分支额外占位 `continue_strict` 走原路径）
- 双路径凭证兼容：`ANTHROPIC_API_KEY` env（Copilot CC）优先于 `~/.claude.json` OAuth（正版 Max CC）
- 模型 ID 三层 fallback：`loop.yml.judge.model` > `$ANTHROPIC_DEFAULT_HAIKU_MODEL` > `claude-haiku-4-5`
- 任何故障路径都降级回 PASS_CMD 二值判据（不阻断既有流程）
- 防脱缰：iter 上限 + 连续 nudge 上限（默认 2）+ confidence 阈值（默认 0.5）

**快速验证**：

```bash
bash ~/.claude/skills/builder-loop/scripts/run-judge-agent.sh --self-check
```

输出当前凭证状态、模型选择、loop.yml 路径，**不调真实 API**。

## 版本交付历史

详见 [`../../CHANGELOG.md`](../../CHANGELOG.md)（V1.0 ~ V3.5）。当前最新 **V3.5 Subagent 来源身份层**（per-agent-type lock / 白名单双条件 / 通用清锁 / lock-utils 公共库）。
