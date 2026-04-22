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
6. **循环外接力**：PASS 后 builder.md 接力（reviewer → doc-maintainer → commit）

## 启动流程

```bash
bash ~/.claude/skills/builder-loop/scripts/setup-builder-loop.sh "$TASK_DESCRIPTION"
```

读 loop.yml → 可选 worktree → 生成状态文件 `builder-loop.local.md`（iter=0 / HEAD / 配置快照）→ 首次 PASS_CMD。

## Stop Hook

`~/.claude/scripts/builder-loop-stop.sh`：检测 `builder-loop.local.md` 且 active=true → 跑 PASS_CMD：
- PASS → 删状态文件，builder 接力
- FAIL → extract-error + early-stop-check → 写回状态文件 → 注入下轮

### 兜底激活（硬门禁）

当 builder 跳过激活流程（未调 setup-builder-loop.sh）时，Stop hook 提供最后防线：
- **触发条件**：loop.yml 存在 + 有代码改动（git diff 或近 30 分钟 commit）+ 无状态文件
- **行为**：自动调 `setup-builder-loop.sh --no-worktree` 创建状态文件，然后走正常 PASS_CMD 流程
- **`--no-worktree`**：兜底激活时代码已在主干，跳过 worktree 创建以避免丢失改动
- **安全兜底**：setup 失败 → 放行（不阻断 CC）；无改动 → 放行（不误触发）

## 状态文件 schema（`.claude/builder-loop.local.md`）

```yaml
active: true
iter: 3
max_iter: 5
start_head: abc1234
worktree_path: /path/...       # worktree 时
task_description: |
  ...
source_dirs: "src,lib"
test_dirs: "tests,spec"
plan_file: ".claude/plans/..."
last_pass_stage: test
last_error_hash: deadbeef
last_error_count: 7
stopped_reason: ""
created_at: "2026-04-18T..."
```

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

## 版本交付历史

详见 `README.md` 第 7 节。涵盖 V1.0 基础循环、V1.1 强隔离+worktree+仲裁、V1.2 改动分级、V1.3 任务回顾。
