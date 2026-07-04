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

## Subagent lifecycle hooks（已退役）

> V5.0 隔离范式变更：per-agent-type 锁机制（SubagentStart/SubagentStop hook + lock-utils）已整体退役。CC 内核的 `subagent_type` 字段在真实环境从未可靠提供，整套机制从落地起空转。subagent 独立性来自架构天然属性（独立推理实例），不依赖外部隔离机制。写边界退地基（prompt 职责声明 + diff 审查 + PASS_CMD）。详见 [CHANGELOG V5.0](../../CHANGELOG.md)。

## Stop Hook

`~/.claude/scripts/builder-loop-stop.sh`：按 CWD 调用 `locate-state.sh` 找本 worktree 对应 state → 检测 active=true → 多层闸过滤非目标场景 → 跑 PASS_CMD → PASS 时转交 `handle-pass-result.sh` 统一处理。

**V3.0 多层闸（PASS_CMD 之前自动识别非目标场景静默退出）**：

| 闸 | 触发条件 | 静默原因 |
|----|---------|---------|
| L1 | `state.phase=passed_pending_review\|e2e_pending` | 等 reviewer 审查 / tester 跑 e2e（自愈：passed_pending_review dirty/新 commit → active；e2e_pending 仅 new_commit/verified_head 自愈，dirty 不自愈防 tester 循环） |
| L2A | transcript 末尾是 pending AskUserQuestion（无 tool_result） | builder 在等用户答 |
| L2B | worktree HEAD == `state.last_iter_head` 且 git status 空 | builder 在思考 / 讨论，没改代码 |
| L3 | `.claude/builder-loop/<slug>.pause` 文件存在 | builder 主动 pause |

**PASS_CMD 通过后 → `handle-pass-result.sh <state_file> <next_iter> <run_cwd> <project_root>`**（worktree / bare 统一走此路径，V4.1；V5.4 从 Stop hook ~230 行内联逻辑提取为独立脚本，`run-pass-cmd.sh` + 本脚本可由 builder 直接调用完成一轮迭代、不必等 Stop event，详见 [CHANGELOG V5.4](../../CHANGELOG.md)）：依次跑 e2e 检测 → reward hacking 检测 → commit + state 写入，stdout 落一行 JSON（`type` 字段），调用方按 type 分支：

| type | exit code | 触发条件 / 后续动作 |
|------|-----------|---------|
| `e2e_needed` | 2 | plan 含 e2e 用例且当前 HEAD 未被 `e2e_verified_head` 验收过 → 写 `phase=e2e_pending` → spawn/续接 tester 跑端到端验收 |
| `reward_hack` | 3 | diff 命中测试配置文件（`loop.yml`/`conftest.py`/`test_*` 等）+ 可疑关键字（`xfail`/`pytest.mark.skip`/`--reruns` 等）双命中 → AskUserQuestion 三选项决策，禁止单方面继续 commit |
| `pass` | 0 | 调 `loop-commit.sh` commit + 写 `state.phase=passed_pending_review` + `reviewer_pending` 段 + 落盘 `reviewer-diff-<slug>.txt`。Builder 收提示 → spawn reviewer → 反馈分支：0🔴 通过 → `merge-and-cleanup.sh <state>`（worktree: ff merge + 删 worktree + 删 state；bare: stash drop + 删 state）；🟡/🔵 → builder 修复 → dirty 触发 L1 自愈回 active → 下一轮 PASS_CMD；🔴 阻塞 → AskUserQuestion 让用户选 [继续修 / abandon-loop.sh] |
| `commit_error` | 4 | `loop-commit.sh` 失败 → 日志落 `.claude/loop-runs/commit-error-iter-<n>.log` → 提示用户检查工作目录后重试 |

- **FAIL**（PASS_CMD 本身未过）→ extract-error + early-stop-check → 写回状态文件 → 注入下轮

### CWD→state 匹配（V3.4, V5.1 增强）

stop hook 通过 `locate-state.sh` 用 CWD + session_id 匹配 state 文件（策略 1.5: `session_id` 精确匹配 `owner_session_id` → 策略 2: worktree 路径推 slug → 策略 3: worktree_path 字段匹配 → 策略 4: bare 模式 `__main__.yml` → 策略 5: 唯一 active 自动绑定 → 策略 6: 唯一未绑定 owner_session_id 的 active state 首次绑定）。无匹配 = exit 0 放行。V3.4 起 `.claude/builder-loop.local.md` 已移除。**V5.1 起不再假设 CWD 天然隔离**——CC session CWD 是会话级常量（主仓），Bash `cd` 不改它；多 worktree 并发靠 session_id（策略 1.5/6）而非 CWD 定位，详见 [CHANGELOG V5.1](../../CHANGELOG.md)。

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

# V4.3 subagent identity 段（builder spawn 时写入，用于 V4.3 续接路径）
subagents:
  tester:
    agent_id: "a0a40ff29f9fd0741"
    started_at: "2026-06-24T01:00:00+08:00"
    status: "running"
    transcript_path: ""
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

### Subagent 锁文件 schema（已退役）

> V5.0 隔离范式变更：per-agent-type 锁机制已整体退役，见 [CHANGELOG V5.0](../../CHANGELOG.md)。

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

choice JSON 含 pass_cmd / max_iterations / layout / worktree / e2e_cases_path（可选）。

`e2e_cases_path`：项目级 e2e 回归集 YAML 路径（相对于项目根）。tester 沉淀时往此文件追加 case。未设置则跳过沉淀。

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
  "e2e_cases_path": "scripts/e2e_cases.yaml",
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

详见 [`../../CHANGELOG.md`](../../CHANGELOG.md)。
