---
name: builder-loop
description: "Builder 自闭环迭代 — 在 builder 完成改动后，以项目根 .claude/loop.yml 定义的 PASS_CMD（lint/type/test 多阶段）作为硬门禁，失败自动把错误喂回 builder 再跑一轮，直到 PASS 或命中上限/早停。Stop hook 截获 + 状态文件喂回机制，机器判定代替主观评审。Triggers on: builder 完成动作时 hook 自动触发；用户显式 /builder-loop；用户说『配置 loop』『接入 loop』『setup loop』『init loop』『给这项目配自闭环』时进入接入向导（生成 loop.yml）。"
---

# Builder Auto-Loop — 机器判定的多轮自闭环

> 详细方案见 `/mnt/hongyu.liao_docker/.claude/plans/20260418-builder-auto-loop.md`，本文件只摘录运行时关键约定。

## 核心规则

1. **触发模型**：项目根存在 `.claude/loop.yml` 时，**builder 显式调用 `setup-builder-loop.sh`** 创建状态文件 → 此后由 Stop hook 检测状态文件存在自动接管循环。未配 loop.yml 则零侵入。
2. **完成判定**：以 `loop.yml.pass_cmd` 数组顺序执行（lint→type→test），全过即 PASS，任一阶段非 0 退出即失败。
3. **失败反馈**：`extract-error.sh` 处理日志（V1 = full 模式 + 精确脱敏），通过状态文件 `.claude/builder-loop.local.md` 注入下轮 prompt 让 builder 继续修。
4. **上限与早停**：硬上限 `max_iterations`（默认 5）+ 智能早停（无进展 / 反增长 / 保护路径被改）。
5. **隔离与 worktree**：`loop.yml.worktree.enabled=true` 时创建 git worktree 隔离，PASS 后 fast-forward / rebase / 仲裁三档合回主分支。
6. **循环外接力**：循环 PASS 后由 builder.md 的原步骤 3 接力（reviewer → doc-maintainer → commit）。

## 启动流程

用户进入 builder 模式 + 完成改动后，builder.md 检测到 `.claude/loop.yml` 存在 → 调用：

```bash
bash ~/.claude/skills/builder-loop/scripts/setup-builder-loop.sh "$TASK_DESCRIPTION"
```

`setup-builder-loop.sh` 会：
1. 读 `.claude/loop.yml`（不存在则报错退出）
2. （可选）`EnterWorktree` 进入隔离分支
3. 生成状态文件 `.claude/builder-loop.local.md`，含 iter=0 / 起始 HEAD / 配置快照
4. 触发首次 PASS_CMD

## Stop Hook 衔接

`~/.claude/scripts/builder-loop-stop.sh` 在每次 Stop 时被调用：
- 检测当前项目根有无 `.claude/builder-loop.local.md` 且 active=true
- 没有 → 立即放行（exit 0），不影响其他 Stop 行为
- 有 → 调 `run-pass-cmd.sh` 跑 PASS_CMD：
  - PASS → 删状态文件，原 builder 流程接力
  - FAIL → 调 `extract-error.sh` + `early-stop-check.sh` → 写回状态文件 → 注入下轮 prompt 让 CC 继续

## 状态文件 schema（`.claude/builder-loop.local.md`）

```yaml
active: true                # false 时 hook 不介入
iter: 3                     # 当前轮次
max_iter: 5                 # 上限
start_head: abc1234         # 进入循环时的 git HEAD
worktree_path: /path/...    # worktree.enabled=true 时的隔离路径（V1.1 已实装）
task_description: |         # 启动时传入的任务描述
  ...
source_dirs: "src,lib"      # 拍平为顶层 csv，供 early-stop-check.sh 直接 grep
test_dirs: "tests,spec"     # 同上；保护路径作弊检测用
plan_file: ".claude/plans/20260418-foo.md"  # 方案文件路径（V1.1+），setup 时自动探测最近的
last_pass_stage: test       # 上轮失败的阶段
last_error_hash: deadbeef   # 用于无进展早停
last_error_count: 7         # 用于反增长早停
stopped_reason: ""          # 早停原因（空表示仍在跑）
created_at: "2026-04-18T..."
```

## 与其他 loop 类 skill 共存

builder-loop 用独立的状态文件 `.claude/builder-loop.local.md` 和独立的 Stop hook 入口 `~/.claude/scripts/builder-loop-stop.sh`，不与本机其他 loop 类 skill 共享任何状态。Stop hook 仅在检测到本 skill 的状态文件时介入，否则立即放行，互不干扰。

## V1.1 强隔离 + 方案三视图过滤（P0+P1）

**P0 bug 修复**：`setup-builder-loop.sh` 的 `detect_dirs()` 末尾加 `return 0` 修复空仓（无 src/lib/app/pkg 目录）与 `set -e` 冲突的杀进程 bug。

**P1.a 方案三视图过滤**：builder.md 在以下 3 处调用 `split-plan-by-role.sh` 过滤不同 role 的方案视图（依赖 state file 新增的 `plan_file` 字段，setup 时自动探测最近 `.claude/plans/*.md`）：
1. 读方案文件输出给 builder → 过滤 `builder` 视图（含 builder|shared 区块）
2. spawn reviewer → 过滤 `shared` 视图（仅 shared 区块，不含 reviewer 自带的约束）
3. spawn tester → 过滤 `tester` 视图（含 tester|shared 区块，用 split-plan-by-role.sh tester 替代旧 test_plan）

**P1.b tester 强隔离**：3 个新 hook 脚本实现多层防护（完整机制见 `~/.claude/scripts/tester-lock-*.sh` 脚本注释）：
- `SubagentStart(matcher=tester)` → `tester-lock-write.sh`：落锁文件 `${ISOLATION_LOCK_DIR:-/tmp}/cc-subagent-{session_id}.lock`（含 session_id/source_dirs_abs/TTL=30min）
- `PreToolUse(matcher=Read|Grep|Glob)` → `tester-lock-check.sh`：读锁判定身份，白名单优先（*.md、路径含 /test /tests /spec /__tests__），命中 source_dirs 则 exit 2 block
- `SubagentStop(matcher=tester)` → `tester-lock-clear.sh`：清锁并删除遗留文件

**硬约束（与 tester.md 对应）**：
- 阶段 1（读动作）：tester 只读通过白名单的路径，其他路径 block
- 阶段 2（工具调用）：tester 调 Read/Grep/Glob 前被 PreToolUse hook 二次检查
- 物理保障：锁文件中记录 `source_dirs_abs`（绝对路径集合），hook 检查时用相对路径换绝对再比较，确保目录穿越无效

## V1.1 worktree 真接入（P2）

`loop.yml` 支持 `worktree` 段配置隔离行为：

```yaml
worktree:
  enabled: true              # 默认 false（向后兼容）
  base_dir: .claude/worktrees # 存放目录
  branch_prefix: loop/        # 分支前缀
```

- `enabled=true` 时 `setup-builder-loop.sh` 创建 `git worktree add -b loop/<task_id>`
- `run-pass-cmd.sh` 自动切到 worktree 内执行 PASS_CMD
- PASS 后 `merge-worktree-back.sh` 合回主干（fast-forward → rebase → 标记仲裁三档）
- FAIL 或早停保留 worktree 供用户调查

## V1.1 仲裁路径（P3）

rebase 冲突时自动 spawn `arbiter` subagent（model=opus）解冲突：

1. `merge-worktree-back.sh` 检测冲突 → 写 `need_arbitration: true` 到状态文件
2. Stop hook 通过 `additionalContext` 通知 builder
3. builder spawn arbiter → 输出 `ARBITER_PATCH_BEGIN/END` + 信心度（high/medium/low）
4. `auto_apply_confidence` 控制自动应用门槛（默认 high）

```yaml
arbitration:
  max_attempts: 2
  auto_apply_confidence: high
```

## V1.1 tester 触发整合（P4）

reviewer 报告末尾固定输出 `BEGIN_TESTER_HINT` JSON 块，builder 步骤 3a+ 解析：

- `need_tester=true` 且循环活跃且 `missing_cases` 非空 → spawn tester 补测试
- tester 完成后重置 state file `iter=0`，下次 Stop 重跑 PASS_CMD 验证

## V1.2 改动分级（融合 hongyu_Repo 实践）

builder 汇报改动范围后，先判断改动级别再决定是否进 loop：

| 级别 | 定义 | loop 行为 |
|------|------|----------|
| L1 纯文案 | 只改注释/文档/配置/prompt，无逻辑变化 | 跳过 loop，直接 reviewer+commit |
| L2 实现改动 | 签名不变，改内部逻辑 | 进 loop（默认行为） |
| L3 新接口 | 新增签名/改返回结构/新模块 | 先 spawn tester 补测试 → 再进 loop |

防误判原则：向上保守（不确定时按更高级别处理）。planner 方案预估级别作参考锚点。

---

## 接入向导（用户说「配置 loop」时执行）

用户要把某个项目接入自闭环时，**不让用户手写 loop.yml**，而是按以下流程走：

### Step 1: 静默探测项目栈

```bash
PROBE_JSON="$(bash ~/.claude/skills/builder-loop/scripts/probe-project-stack.sh <项目根>)"
```

输出含：language / test_framework / lint_tools / source_dirs / test_dirs / recommended_pass_cmd。

### Step 2: AskUserQuestion ×4 收集决策

每个问题给「探测出的推荐默认 + 备选 + Other」选项卡：

1. **通过条件**：show recommended_pass_cmd 给用户选用全套 / 只测试 / Other
2. **测试目录**：show 探测到的 test_dirs 给用户确认 / Other
3. **上限轮数**：3 / **5 (推荐)** / 10 / Other
4. **是否立即跑 smoke test 验证**：是 (推荐) / 否

### Step 3: 调底层脚本写 loop.yml

```bash
echo "<choice JSON>" | bash ~/.claude/skills/builder-loop/scripts/init-loop-config.sh <项目根>
```

choice JSON 结构示例：
```json
{
  "pass_cmd": [{"stage":"test","cmd":"pytest -x","timeout":300}],
  "max_iterations": 5,
  "layout": {"source_dirs": ["src"], "test_dirs": ["tests"]},
  "task_description": "由 init 向导生成于 YYYY-MM-DD"
}
```

底层脚本会：写 `.claude/loop.yml` + 追加 `.gitignore` 两行 + mkdir `.claude/loop-runs/`。

### Step 4: smoke test（按用户选项）

```bash
bash ~/.claude/skills/builder-loop/scripts/run-pass-cmd.sh <项目根> 0
```

> **注意**：此处独立调用 `run-pass-cmd.sh`，传 iter=0 仅用于日志命名（生成 `iter-0-<stage>.log`）。该脚本只读 `loop.yml`，**不依赖状态文件**，因此 smoke test **不需要先调 setup-builder-loop.sh**（避免 smoke test 意外创建状态文件触发后续循环）。

PASS → 报「✅ 配置可用」；FAIL → 报「⚠️ smoke test 失败，loop.yml 已写但可能命令路径不对，请检查」（**不阻断接入**）。

### Step 5: 汇报 + 提示

```
✅ 已接入 builder-loop
   配置文件：<项目根>/.claude/loop.yml
   PASS_CMD 阶段：<N> 个
   smoke test：<结果>
后续：直接用 /builder 跑任务，循环自动接管。
```

> **底层脚本可独立调用**：`probe-project-stack.sh` 和 `init-loop-config.sh` 都是纯 bash，CC 之外也能跑（V3 daemon 会复用）。

V1.0 暂走「prompt 切片 + tester system prompt 自律 + 事后审计」弱隔离，V1.1 已交付强隔离 + worktree + 仲裁 + tester 触发。

## V1.3 任务回顾与知识沉淀

builder commit 后自动触发回顾步骤：当 loop 迭代 ≥2 轮、触发过仲裁/tester、reviewer 报 🔴、或识别到可沉淀知识时，用 `AskUserQuestion(multiSelect=true)` 提审候选知识点，用户勾选后调 `/memory` 落盘。详见 `commands/builder.md` 步骤 5。
