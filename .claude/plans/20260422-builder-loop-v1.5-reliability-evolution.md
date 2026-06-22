# Builder-Loop V1.5 — 全新仓库可靠性 + 长期演进

> 基于 session c73ed060 暴露的 5 个问题，系统性修复 builder-loop 在全新仓库场景的可靠性，
> 同时完成长期演进（一键 init、NDJSON trace、E2E 测试）。一版到位。

## 背景 & 目标

**触发事件**：c73ed060 session 中 builder-loop 在全新仓库（Personal_Assistant_Bot）工作时：
1. P0: worktree 基于旧 HEAD，代码丢失需手动 cp
2. P1: builder 不自动初始化 loop，用户手动打断
3. P1: stop hook 输出格式错误（用了 PreToolUse 的 `decision:block`），loop 一轮都没跑
4. P2: loop.yml 格式不熟，首次写错
5. P2: planner 模式也触发 stop hook（无害但浪费）

**成功标准**：
1. 全新仓库从 `git init` → loop 第一轮完整跑通（E2E 验证）
2. 零手动干预：builder 自动检测 → setup → worktree → 写代码 → loop
3. 一键 init 模板：`loop init` 命令生成 loop.yml + 骨架
4. 可观测：每轮写 NDJSON trace 到 `.claude/loop-trace.jsonl`

## 预估改动级别

L2（实现改动）— 涉及多个脚本修改 + 新增 trace 模块，但无新接口/模块级变化。

## 约束 & 边界

- **不改 CC 源码**：所有功能基于 CC 的 hook / skill / agent 扩展机制实现
- **可破坏性升级**：允许不兼容已接入项目的 loop.yml，但必须手动更新所有已接入项目
- 已接入项目清单：hongyu_Repo / Personal_Assistant_Bot / cc-dcp

## 技术选型

### Stop hook 续接机制（核心决策）

**现状分析**：

当前 `builder-loop-stop.sh` 输出 `{"decision":"block","reason":"..."}` 格式。
这是 **PreToolUse hook 的格式**（权限审批用），Stop hook 不认。

CC 2.1.79 Stop hook 的 JSON API：
- `continue: false` → 阻止 CC 继续（让 CC 停下来），**不是让 loop 继续**
- `additionalContext` → 转为 `hook_additional_context` attachment → 转为 user message
- 纯 stdout text → 显示为 attachment，不注入 LLM context

**方案 A（已否决）：additionalContext 注入**

CC 源码确认：Stop hook 的 `hookSpecificOutput` switch/case **没有 Stop 事件**。
`additionalContext` 仅支持 PreToolUse/UserPromptSubmit/SessionStart/Setup/SubagentStart/PostToolUse。

**方案 B：exit code 2 + stderr（已验证，采用）** ✅

CC 对 Stop hook exit code 2 的处理链：
1. `hooks.ts:2648-2667` → 生成 `blockingError`，内容 = `[command]: <stderr>`
2. `stopHooks.ts:258-263` → yield `createUserMessage({content: "Stop hook feedback:\n..."})`
3. `query.ts:1282-1306` → blockingErrors 追加到消息历史 → `state = next; continue` → **LLM 继续**
4. `stopHookActive: true` 防死循环

改法：stop hook 需要续接时 `exit 2`，反馈写 stderr（不是 stdout）；不续接时 `exit 0`。

## 方案设计

### 架构变更总览

```
改动前（V1.4）：
  Builder写代码 → setup(创建worktree) → worktree无代码 → cp文件 → stop hook输出错误JSON → loop不跑

改动后（V1.5）：
  Builder检测loop.yml → setup(创建worktree) → cd worktree → 在worktree内写代码
    → stop hook输出additionalContext → CC注入为user message → builder继续修复 → loop跑通
```

### 变更点 1：Worktree 前置（修复 P0）

**改 builder.md**：builder 模式进入后的流程调整

```
旧流程：
  读方案 → 写代码 → 改动分级 → 有loop.yml? → setup → worktree（代码已丢失）

新流程：
  读方案 → 有loop.yml? → setup → 有worktree? → cd worktree → 写代码 → 改动分级
```

**改 setup-builder-loop.sh**：支持空仓库场景
- 当前：从 HEAD 创建 worktree 分支，HEAD 必须有 commit
- 新增：如果 git log 为空（全新仓库只有 init commit），允许基于唯一 commit 创建 worktree

### 变更点 2：Stop hook 输出格式（修复 P1-stop-hook）

**改 builder-loop-stop.sh**：所有 JSON 输出从 `{"decision":"block","reason":"..."}` 改为 `{"additionalContext":"..."}`

三个输出场景的改法：
1. **PASS**：`{"additionalContext":"[builder-loop] ✅ PASS_CMD 全部通过(iter N)。请继续 Reviewer → commit 流程。"}`
2. **FAIL**：`{"additionalContext":"[builder-loop iter N] FAIL at stage=X. 请修复：\n<error>"}`
3. **NEED_ARBITRATION**：`{"additionalContext":"[builder-loop] ⚠️ rebase冲突，请spawn arbiter..."}`

### 变更点 3：Builder 自动初始化 loop（修复 P1-init）

**改 builder.md**：builder 模式进入后增加前置检查

```
进入 builder 模式后，立即（在读方案之前）：
1. 检查 .claude/loop.yml 是否存在
2. 如果存在 → 读 loop.yml → 调 setup-builder-loop.sh → cd worktree（如启用）
3. 如果不存在但方案文件要求了测试计划 → 触发智能提示（接入向导）
```

**改 SKILL.md**：接入向导中补充 schema 示例 + pass_cmd 格式说明，减少首次配置出错。

### 变更点 4：NDJSON Trace（长期演进）

**新增 trace 写入**：在 run-pass-cmd.sh 和 builder-loop-stop.sh 中，每轮循环追加一行到 `.claude/loop-trace.jsonl`

```json
{"ts":"2026-04-22T10:28:11Z","iter":1,"stage":"test","result":"FAIL","duration_ms":12340,"error_hash":"a1b2c3","task":"Personal Assistant Bot MVP"}
{"ts":"2026-04-22T10:30:45Z","iter":2,"stage":"test","result":"PASS","duration_ms":8920,"error_hash":"","task":"Personal Assistant Bot MVP"}
```

字段：ts / iter / stage / result(PASS|FAIL|TIMEOUT|EARLY_STOP) / duration_ms / error_hash / task

### 变更点 5：一键 Init 模板（长期演进）

**新增脚本 `loop-init.sh`**：整合 probe + AskUserQuestion + init-loop-config 为一条命令

```bash
bash ~/.claude/skills/builder-loop/scripts/loop-init.sh <project_root>
```

流程：
1. 自动 probe 项目栈
2. 如果无 git 仓库，自动 `git init + git add -A + git commit`
3. 按探测结果推荐 pass_cmd
4. 生成 loop.yml（用 init-loop-config.sh）
5. 输出汇报

**SKILL.md 中注册为接入向导的 Step 0**（在 5 步向导之前的快捷入口）。

### 变更点 6：E2E 测试（验收保障）

**新增 fixtures/e2e/test-new-repo-loop.sh**：全新仓库端到端测试

```
1. 创建临时目录 + git init
2. 写一个简单 Python 文件（有意包含语法错误）
3. 调 loop-init.sh 生成 loop.yml
4. 调 setup-builder-loop.sh
5. 验证：状态文件存在 + worktree 创建成功
6. 手动跑 run-pass-cmd.sh → 验证 FAIL
7. 修复语法错误 → 再跑 → 验证 PASS
8. 验证 trace.jsonl 有记录
9. 清理
```

### 变更点 7：更新已接入项目

改动完成后，手动检查并更新以下项目的 loop.yml（如有格式变化）：
- `/mnt/hongyu.liao_docker/hongyu_Repo/.claude/loop.yml`
- `/mnt/hongyu.liao_docker/Personal_Assistant_Bot/.claude/loop.yml`
- `/mnt/hongyu.liao_docker/cc-dcp/.claude/loop.yml`

## 风险 & 应对

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| `additionalContext` 字段 Stop hook 不支持 | 中 | P0 | 先用 E2E 验证；fallback 到文件状态 + builder.md 轮询 |
| Worktree 前置导致 builder 写代码前 cd 到空目录 | 低 | 中 | setup 后验证目录存在 + loop.yml 可访问 |
| 已接入项目 loop.yml 格式变化导致不兼容 | 确定 | 低 | 变更后立即手动更新 3 个项目 |
| 全新仓库无 commit 导致 worktree 创建失败 | 中 | 中 | setup 脚本检测空仓库，自动创建初始 commit |

**最大风险点**：`additionalContext` 验证是整个方案的关键路径。如果 CC 2.1.79 的 Stop hook 不支持此字段，需要 fallback 到方案 B（文件状态 + builder.md 轮询），这会增加不确定性。建议 T1 先做验证。

## 文件地图

| 文件路径 | 改动类型 | 改动内容 |
|----------|---------|---------|
| `scripts/builder-loop-stop.sh` | 修改 | JSON 输出格式 → additionalContext；增加 trace 写入 |
| `skills/builder-loop/scripts/setup-builder-loop.sh` | 修改 | 支持空仓库 worktree 创建 |
| `skills/builder-loop/scripts/run-pass-cmd.sh` | 修改 | 增加 trace 写入 |
| `skills/builder-loop/scripts/loop-init.sh` | 新增 | 一键 init 模板脚本 |
| `skills/builder-loop/SKILL.md` | 修改 | 接入向导流程调整 + schema 示例 |
| `skills/builder-loop/fixtures/e2e/test-new-repo-loop.sh` | 新增 | E2E 测试脚本 |
| `~/.claude/commands/builder.md` (dotfiles) | 修改 | loop 前置检查 + worktree cd 流程 |
| `CLAUDE.md` | 修改 | 已更新开发原则 + V1.5 能力 |

## 执行任务列表

### T1：验证 additionalContext 机制（关键路径）
- 在 `fixtures/e2e/` 下创建最简 stop hook 测试脚本
- 输出 `{"additionalContext":"test message"}` 观察 CC 是否将其注入为 user message
- 如果验证失败，改用 fallback 方案 B 并调整后续任务

### T2：修复 Stop hook 输出格式（修复 P1-stop-hook）
- 修改 `scripts/builder-loop-stop.sh`
- 所有 python3 JSON 输出块从 `decision/reason` 改为 `additionalContext`（或 fallback 格式）
- PASS / FAIL / NEED_ARBITRATION 三个分支全部改

### T3：Worktree 前置 + 空仓库支持（修复 P0）
- 修改 `setup-builder-loop.sh`：检测空仓库（只有 init commit）→ 正常创建 worktree
- 修改 `~/.claude/commands/builder.md`：builder 进入后先检测 loop.yml → setup → cd worktree → 再写代码

### T4：Builder 自动初始化 loop（修复 P1-init）
- 修改 `builder.md`：进入后无 loop.yml 时的智能提示时机从"改完代码后"提前到"读方案前"
- 修改 `SKILL.md`：补充 pass_cmd 对象格式的 schema 示例（修复 P2-格式）

### T5：NDJSON Trace
- 修改 `run-pass-cmd.sh`：每个 stage 完成后追加一行到 `.claude/loop-trace.jsonl`
- 修改 `builder-loop-stop.sh`：PASS/FAIL/EARLY_STOP 时各写一行 trace

### T6：一键 loop-init.sh
- 新增 `skills/builder-loop/scripts/loop-init.sh`
- 整合 probe + init-loop-config + git init 为一条命令
- SKILL.md 中注册为快捷入口

### T7：E2E 测试
- 新增 `fixtures/e2e/test-new-repo-loop.sh`
- 覆盖：空仓库 → init → setup → worktree → FAIL → 修复 → PASS → trace 验证

### T8：更新已接入项目
- 检查 hongyu_Repo / Personal_Assistant_Bot / cc-dcp 的 loop.yml
- 如有格式变化需同步更新
- 验证 stop hook 在这些项目中正常工作

## 验收标准

1. `test-new-repo-loop.sh` 全流程 PASS
2. 在 Personal_Assistant_Bot 重新测试 builder-loop，stop hook 正确触发续接
3. hongyu_Repo / cc-dcp 的 loop 不受破坏
4. `.claude/loop-trace.jsonl` 每轮有记录
5. `loop-init.sh` 在空目录下一键生成 loop.yml
