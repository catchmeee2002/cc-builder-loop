# 角色提示词瘦身方案

## 背景 & 目标

builder.md（346行 18KB）+ SKILL.md（208行 11KB）合计 554 行 29KB 注入主上下文，占用 builder 工作 context window 过重。目标是在不丢失功能的前提下瘦身 ~45%（降至 ~300 行 ~16KB）。

**成功标准**：builder.md ≤ 220 行，SKILL.md ≤ 110 行，所有现有功能路径可复现。

## 预估改动级别

L1（纯文案）— 只改提示词/文档，无代码逻辑变化。

## 约束 & 边界

- 不改任何 `.sh` 脚本逻辑
- 不改 subagent 提示词（reviewer/tester/arbiter/doc-maintainer），它们独立上下文不影响主 session
- 不改 planner.md（89 行，已足够精简）
- 不改 tester.md 角色版（74 行，合理）
- 外置的冷路径文件必须自包含，builder Read 后即可执行，不依赖主提示词上下文

## 技术选型

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| A: 冷路径外置 + 冗余压缩 | 渐进式、风险低、可逐步验证 | 外置文件增加 Read 成本 | **推荐** |
| B: 整体重写为精简版 | 一步到位 | 高风险、难 diff 验证、容易丢功能 | 排除 |

## 风险 & 应对

- **冷路径遗忘**：触发条件写为一句话 + 文件路径，LLM 看到触发条件会自然 Read
- **压缩过度**：保留决策表和编号结构，只删叙述/解释文字

## 文件地图

| 文件 | 位置 | 改动 |
|------|------|------|
| `commands/builder.md` | `~/my-dotfiles/claude/.claude/commands/builder.md` | 主瘦身对象，346→~200行 |
| `skills/builder-loop/SKILL.md` | `~/.claude/skills/builder-loop/SKILL.md` (→本仓库) | 版本历史外移，208→~100行 |
| `skills/builder-loop/docs/arbiter-flow.md` | **新建** | 仲裁分支流程外置 |
| `skills/builder-loop/docs/reviewer-fallback.md` | **新建** | 兜底自审外置 |
| `skills/builder-loop/README.md` | 本仓库已有 | 承接版本历史叙述 |

## 执行任务列表

### 阶段 1: builder.md 瘦身（主战场）

**Task 1.1: 创建冷路径外置文件**
- 新建 `skills/builder-loop/docs/arbiter-flow.md`，从 builder.md 108~131 行提取仲裁分支流程
- 新建 `skills/builder-loop/docs/reviewer-fallback.md`，从 builder.md 308~329 行提取兜底自审流程
- 两个文件自包含，不依赖 builder.md 上下文

**Task 1.2: 删重复段落**
- 删除 builder.md 末尾 "启动 Tester 时透传方案视图" 段（333~346行），内容与步骤 3a+ 的 TESTER_HINT 段完全重复

**Task 1.3: 压缩 builder.md 各段落**

逐段压缩规则：

| 段落 | 当前行数 | 目标行数 | 压缩手法 |
|------|---------|---------|---------|
| 方案视图过滤 | 14 | 6 | 删 "为什么过滤" 解释块，只留操作步骤 |
| 改动分级 | 25 | 15 | 删 L3 额外步骤的详细说明，改为引用 TESTER_HINT 段 |
| loop.yml 不存在时的智能提示 | 23 | 5 | 移到 SKILL.md 接入向导，builder 只留一句"无 loop.yml → 见 SKILL.md 接入向导" |
| 仲裁分支流程 | 25 | 3 | 外置引用 |
| Reviewer 触发 | 70 | 40 | 删重试编排叙述，改为紧凑决策表；合并步骤 2+3 |
| TESTER_HINT 解析 | 25 | 15 | 删注意事项解释，只留执行步骤 |
| 文档更新评估 | 15 | 8 | 删判断表右半列，只留"需要"列 |
| 自动 commit | 25 | 10 | 删 bash 示例，只留 checklist |
| 任务回顾 | 15 | 10 | 压缩触发条件为一行列表 |
| 兜底自审 | 20 | 3 | 外置引用 |

**Task 1.4: 合并 loop.yml 不存在逻辑到 SKILL.md**
- 把 builder.md 69~83 行的"智能提示判断"逻辑移到 SKILL.md 的接入向导前
- builder.md 只留：`不存在 loop.yml → 见 builder-loop SKILL.md「智能提示」段`

### 阶段 2: SKILL.md 瘦身

**Task 2.1: 版本历史外移**
- 将 V1.1~V1.3 的详细交付记录（P0/P1/P2/P3/P4 描述，约 80 行）移到 README.md
- SKILL.md 只保留：核心规则(6条) + 启动流程 + Stop Hook 衔接 + 状态文件 schema + 接入向导

**Task 2.2: 接入向导压缩**
- Step 1~5 的 bash 代码块压缩为内联命令，删多余空行和注释
- choice JSON 示例只保留一个最小化版本

### 阶段 3: 验证

**Task 3.1: 行数验证**
- builder.md ≤ 220 行
- SKILL.md ≤ 110 行

**Task 3.2: 功能路径走查**
- 逐条对照原 builder.md 的功能点，确认瘦身后每个路径有明确入口（在主文件或外置引用）

## 验收标准

1. builder.md ≤ 220 行（当前 346 行，降幅 ~36%）
2. SKILL.md ≤ 110 行（当前 208 行，降幅 ~47%）
3. 合计主上下文占用 ≤ 330 行（当前 554 行，降幅 ~40%）
4. 所有现有功能路径在瘦身后仍有明确执行入口（主文件内 or 冷路径外置文件引用）
5. 不改任何 .sh 脚本、不改 subagent 提示词、不改 planner/tester 角色版
