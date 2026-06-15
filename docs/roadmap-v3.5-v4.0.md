[保质期: V4.0 完成, owner: hongyu, 正向归宿: CHANGELOG.md（V3.5/V4.0 各自版本段）]

# V3.5 + V4.0 路线图


> **代号对照**：identity = subagent 身份层 | doc-gate = 文档评估机械化 | lsp-lint = LSP/mypy 接入 | schema-out = agent 输出契约化 | no-barrier = pipeline 无阻塞 | resume = 崩溃恢复 | budget = token 预算感知
> 这是规划文档，不是历史。V3.5/V4.0 各自版本完成后，该版本对应内容应迁去 CHANGELOG（过去时态），本文件对应段落覆写为"已落地"指针；两个版本都完成后整文件归档（git 留痕、内容删）。
> 单一 SoT 原则：跨多版本的"进行中规划"以本文件为准；具体开工时 V3.5 三件事各自 `/planner` 生成 `.claude/plans/` 下子方案，本文件不重复 plan 细节。

---

## 1. 背景 & 目标

### 1.1 触发上下文

`improvements.md` 候选清单 30+ 条堆积，其中**至少三个同根因 ≥3 次复现**——违反 design-philosophy 原则五（同一问题三次即架构缺陷）+ 原则七（本仓自身受这些原则约束）。当前候选清单态 = "等独立任务挑出来落地"，事实上原则五的兑现机制在本仓自身失效（dogfooding 缺口）。

继续在 improvements.md 上单点打补丁会让缺口持续扩大；本路线图把同根因升级为架构改造，分两个版本消化。

### 1.2 目标（V3.5 + V4.0 合计）

消化 improvements.md 中**至少 19 条同根因 TODO**（三类合计），同时承接 `docs/cc-loop-tracking.md` §2 借鉴清单 P0 四项。具体拆解见 §6 方案设计。

### 1.3 成功标准（合并的）

- 三类同根因（subagent 落点 / step 3.5 主观裁量 / reviewer 无符号语义）在 V4.0 完成后**不再新增同模式条目**——以 V4.0 上线后 8 周内 improvements.md 新增条目零同根因为机械化验收点。
- P0 四项各自 fixture 验证通过且接入 V3.5/V4.0 dogfooding 循环。
- design-philosophy 原则零（机器判据 ground truth 锚点）在 reviewer 这层得到实物落地（lsp-lint 件事，方向 3 轻量版）。

---

## 2. 决策来源（不写决策结果，写 frame 校准过程，避免后人误读为"理所当然"）

本路线图 frame 经过 4 轮校准（来自 metacog 项目 2026-06-03 长对话产物）：

1. **frame-0**："演进路线要不要调"被识别为表层问法——真问题是 improvements.md 状态喊出的 dogfooding 失败。
2. **frame-1**：4 个互斥方向铺过（并行 K builder / D-I-V 三 agent / 判据轴展开 / 工程-理论 loop 分裂）。
3. **frame-2**：曾考虑"架构本身过时、下个大版本重洗"，校准后退回渐进路径——`cc-loop-tracking.md` §4 三维度图 + P0 四项是真产物，"架构过时"无具体撑不住证据。
4. **frame-3**：swap 警报误判（误以为推渐进路径是 swap 用户原话"问题不需要动自然消失"——校准后澄清"问题消失"恰是 identity/doc-gate/lsp-lint 架构改造的副产品，跟渐进路径不冲突）。

**未接的 frame**（未死、留桌见 §10）：
- "深度维 = 入场券不是护城河"那一刀（cc-loop-tracking §4 的 self-positioning 是否经得起 C5 试金石审）。
- builder-loop 作为 metacog 框架工程化载体之一这个身份。

---

## 3. 预估改动级别

整体 **L3（新接口/模块）**——V3.5/V4.0 合计涉及 hook 隔离层重写、4 套 agent prompt 输出契约升级、PASS_CMD schema 扩展（多语言指称判据接入）。

具体每件事的级别：
- identity 来源身份层：L3（hook 接口 + lock 文件 schema 变）
- StructuredOutput（schema-out）：L3（4 套 agent prompt 契约 + 校验重试层是新接口）
- doc-gate step 3.5 机械化：L2（builder.md prompt 改 + 机械检测清单是逻辑变化无新接口）
- lsp-lint LSP/mypy 轻量：L3（PASS_CMD schema 加新 stage 类型）
- no-barrier pipeline：L2（loop 流程改造，不涉及对外接口）
- resume resume journal：L3（state 加 journal 段 + 启动 restore 是新接口）
- budget budget 感知：L2（max_iterations 改动态读取）

Builder 实施时按实际 diff 确认或修正级别。

---

## 4. 约束 & 边界

### 4.1 必须保持

- design-philosophy 七原则不漂——尤其原则一（判据分层）、原则四（改输入条件不改输出约束）、原则六（契约先于实现）。
- 已落地的 V3.0 reviewer-as-gate phase 状态机契约（active / passed_pending_review）不破坏；V3.3 worktree 复用 / V3.4 多 session 隔离行为兼容。
- `loop.yml` schema 老格式兼容性——每次 schema 改动必须有"老 state 缺新字段时降级路径"。
- CC 不改源码——所有改动基于 hook / skill / agent 扩展机制。

### 4.2 不能碰

- PASS_CMD 二值判据这个地基——StructuredOutput / LSP 接入都是上层 LLM 判据契约化或机器判据轴扩展，**不替换地基**。
- `active` 字段虽然 V3.x 渐进下掉，但本期不强制清理（详见 CLAUDE.md "active 字段下掉计划"）——避免本期任务范围被技术债拖累。

### 4.3 显式不做（不上元修复）

"同根因 ≥3 次自动升板"机制（脚本扫 improvements.md tag、阈值触发 PR 候选）**不进 V3.5、不进 V4.0**。决策依据：

- 节省 S-M 工作量。
- 保留 C4 目标作者权（人决定何时启动夯实修复，不被工具替代）。
- 代价显式：未来再积 ≥3 条同根因仍依赖人手动翻 improvements.md。后续若复发同样症状，再考虑加自动化。

---

## 5. 技术选型（4 方向曾铺过 → 收敛到渐进路径）

铺过的 4 个互斥方向（来自 metacog 长对话）：

| 方向 | 简述 | 本期处理 |
|------|------|---------|
| **1 · 并行 K builder + judge 收敛** | "1 builder × N 轮" → "K builder × 1 轮" | 不采纳本期。cc-loop-tracking §4 "正交可组合"已规划用 workflow × builder-loop 外部套娃实现，等 workflow 成熟后再评估，不做内部并行版 |
| **2 · 拆 designer/implementer/verifier 三 agent** | builder 拆三 agent | 不采纳激进版。**StructuredOutput（schema-out）= 温和版**——4 套 agent 输出契约化是同源方向 |
| **3 · 判据轴展开（多轴判据基建）** | 框架级判据轴清单（自洽 / 指称 / 覆盖 / 约束） | 不采纳完整版。**lsp-lint 件事 = 轻量版**——只接 LSP/mypy 子集填补指称轴空白 |
| **4 · 工程 loop + 理论 loop 分裂** | 两套 loop 共享基建但 prompt/闸/判据完全不同 | 不采纳本期。承认 builder-loop 当前 sweet spot 是工程类任务；理论富项目（如 metacog）未来再评估 |

收敛理由：cc-loop-tracking.md §2 P0 四项已是经过 metacog 框架审过的 high-leverage 借鉴；加上消化三类同根因，本期 7 件事已足够大。激进重做无具体撑不住的证据。

---

## 6. 方案设计

### 6.1 V3.5 / V4.0 内容分配

**V3.5（关键路径前段）**：

| 代号 | 名称 | 主要消化 TODO 类别 |
|------|------|---------------------|
| ~~**identity**~~ | ~~subagent 来源身份层~~ | ✅ 落地（2026-06-14） |
| **schema-out** | StructuredOutput 全套契约化 | ⏸ 搁置——blocked on CC Agent tool schema support。DEVIATION_FROM_SPEC 协议单独拆出 |
| ~~**doc-gate**~~ | ~~step 3.5 机械化检测~~ | ✅ 落地（2026-06-14） |

**V4.0（关键路径后段 + 工程化收益）**：

| 代号 | 名称 | 主要消化 TODO 类别 |
|------|------|---------------------|
| **lsp-lint** | LSP/mypy 轻量接入 | reviewer 无符号语义 / 大 diff 审查深度不足类（≥6 条） |
| no-barrier | pipeline 无 barrier | 工程化收益（无直接 TODO 消化） |
| resume | resume journal | 替代 V3.3 orphan 检测（部分迁移收益） |
| budget | budget 感知 | 工程化收益（max_iterations 动态化） |

### 6.2 关键路径 & 串并策略

```
identity (V3.5)
   ↓ (集成完成才能改 LLM 判据契约)
schema-out (V3.5)
   ↓ (契约化后 reviewer 输入才能消费机器判据)
lsp-lint (V4.0)
```

**部分并行策略（前一题用户决定）**：

- **设计阶段可并行**：identity 实施期间可启动 schema-out prompt 设计 + doc-gate 机械检测清单设计。
- **集成阶段串行**：schema-out 落地集成必须等 identity 完成；lsp-lint 落地集成必须等 schema-out 完成。
- **doc-gate / no-barrier / resume / budget 可随时插入**：与关键路径无强依赖。

V3.5 内部目标周期 4-6 周；V4.0 同。总周期 ≈ 2-3 个月（按一周两晚 dogfooding 节奏）。

### 6.3 三类同根因升级理由（细化）

**identity · 来源身份层**——`improvements.md` 至少 9 条同根因（2026-06-02 ×3 / 05-31 / 05-29 / 05-28 / 05-26 / 05-10 / 05-09），共同特征 = subagent / hook / session 之间缺"来源身份"判定层。cc-loop-tracking §3.2 已立项但状态是"等 CC 补 `agent_transcript_path`"——**等天上掉肉**。本期走 fallback 路径 identity：`agent_type` 反向白名单 + lock 加 `agent_id`，不阻塞等接口。

**schema-out · StructuredOutput 全套契约化**——P0 之 P0，三重原则命中（一/四/六）。落地后 4 套 agent（reviewer / judge / doc-maintainer / tester）输出从"自由文本 + 正则解析"升级为"schema 校验 + 自动重试"。顺带消化 [A2] DEVIATION_FROM_SPEC 协议 / [A2] schema 字段变更兼容 / 部分 reviewer 长 diff 误读类。

**doc-gate · step 3.5 机械化**——至少 4 条同根因（05-28 / 05-24 / 05-13 / 05-11），共同特征 = "靠 builder 主观裁量"。改输入条件不改输出约束（原则四）——把 doc 评估 / commit-before-merge / plan.md 更新 等场景做成机械触发清单（pattern match + 检测规则），消除主观判断点。

**lsp-lint · LSP/mypy 轻量接入**——至少 6 条同根因（06-03 / 05-31 / 05-26 ×2 / 05-10 / 05-09），共同特征 = reviewer 看 diff 没做符号语义层判断（方法名存在性 / re-export 完整性 / 长 diff 审查深度）。**承认 reviewer 在域穿越 / 指称漂移上结构性盲**（metacog C10：自洽探测器盲于保持自洽的错误），不靠它，靠正交仪器——LSP/mypy 子集填补指称轴空白，作为 PASS_CMD 新 stage 类型嵌入。这是 design-philosophy 原则零（"为自动化系统提供 ground truth 锚点"）在 reviewer 这层的实物落地。

---

## 7. 风险 & 应对

### 7.1 关键路径风险

| 风险 | 影响 | 应对 |
|------|------|------|
| identity 阻塞 schema-out 启动 → V3.5 周期延长 | 高 | 设计阶段允许并行启动 schema-out prompt 设计；集成串行 |
| schema-out 改 4 套 agent prompt 耦合面大、改一处撞他处 | 高 | 4 套 agent 各自的 schema + 校验重试层独立，但用同一套基础设施（公共 schema 定义 + 公共校验工具）；先做 reviewer 一套跑通 fixture 再扩 |
| lsp-lint 多语言适配跟接入项目栈差异（generator/BOT/EDB 各不同）| 中 | V4.0 起步只支持 Python（mypy）+ TypeScript（tsserver）；其他语言项目按需后续接入；项目侧 loop.yml 加可选字段 `referential_stage` |
| pipeline 改造跟 V3.0 phase 状态机交互复杂 | 中 | 设计先于实现（原则六）——写出 pipeline + phase 状态机交互的状态图作为 PR 前置条件 |

### 7.2 跨件事的风险

**StructuredOutput 跨 dotfiles 同步**：
- 4 套 agent 中 `reviewer.md` / `doc-maintainer.md` 同时被 cc-builder-loop 仓和 dotfiles（`~/.claude/agents/`）维护。schema-out 改造必须先确认 dotfiles 那侧是否同步——按 CLAUDE.md §3"与 dotfiles 的依赖关系"段处理。
- 应对：schema-out 落地前同步 dotfiles 仓的对应 agent 文件；写 sync-checklist 加一条"agent prompt schema 改造时双仓同步"。

**identity 落地后 5+ 条同根因消化的对账风险**：
- identity 落地后必须显式标记 improvements.md 对应条目状态（promoted to 架构改造 + V3.5 关闭），否则下一轮翻牌还会看到。
- 应对：identity 完成时一次性扫 improvements.md grep 关键词（"subagent 落点 / hook 撞 session / worktree 写错"）逐条标记。schema-out/B/C 完成时同样扫一次。

### 7.3 退路

- 任何一件事的 fixture 跑不通 → 在 V3.5 / V4.0 内推迟，下一版本再做；版本不为"必须含某件事"绑死。
- 整个路线图节奏失控 → 退回 improvements.md 单点修复模式，但显式承认 dogfooding 缺口扩大、原则五在本仓持续失效。

---

## 8. 文件地图

### 8.1 identity 触及

- `scripts/subagent-start-guard.sh`（agent_type 反向白名单 + lock 命名加 agent_id）
- `scripts/worktree-write-guard.sh`（按 agent_id 区分 lock）
- `scripts/tester-lock-check.sh` / `tester-lock-clear.sh`（lock 命名同步）
- `scripts/reviewer-timing-check.sh`（按 agent_id 而非 session_id 判定）
- `skills/builder-loop/docs/cc-loop-tracking.md` §3.2（路径 identity 状态从"待 CC 补接口"转"已落地 fallback"）
- 新增 fixture：`skills/builder-loop/fixtures/test-subagent-source-identity.sh`

### 8.2 schema-out 触及

- `agents/reviewer.md` + 全局 `~/.claude/agents/reviewer.md`（dotfiles 同步）
- `agents/doc-maintainer.md` + 全局 `~/.claude/agents/doc-maintainer.md`（dotfiles 同步）
- `agents/tester.md` + 全局 `~/.claude/agents/tester.md`（dotfiles 同步）
- `skills/builder-loop/prompts/judge-system.md`
- `skills/builder-loop/schema/`（新增 4 套输出 schema + 校验工具）
- `skills/builder-loop/scripts/run-judge-agent.sh`（接入 StructuredOutput 调用）
- 新增 fixture：`fixtures/test-structured-output-{reviewer,judge,doc-maintainer,tester}.sh`

### 8.3 doc-gate 触及

- `~/.claude/commands/builder.md` step 3.5（机械检测清单替代主观裁量）—— dotfiles
- 新增 `scripts/doc-trigger-detect.sh`（机械检测脚本）
- 新增 fixture：`fixtures/test-step-3.5-mechanical-detect.sh`

### 8.4 lsp-lint 触及

- 新增 `scripts/lsp-pass-stage.sh`（PASS_CMD 新 stage 入口）
- `skills/builder-loop/scripts/init-loop-config.sh`（loop.yml schema 加 `referential_stage` 字段）
- `skills/builder-loop/SKILL.md`（schema 段加新字段说明）
- 新增 fixture：`fixtures/test-lsp-stage-{python,typescript}.sh`
- `docs/design-philosophy.md`（在原则零段落加"指称轴 = 落地 stage 之一"的引用）

### 8.5 no-barrier / resume / budget 触及

- no-barrier pipeline：`scripts/builder-loop-stop.sh`（核心循环改造）+ `scripts/extract-error.sh` + 状态文件 schema 加段
- resume resume journal：`scripts/locate-state.sh`（journal 加载）+ state schema 加 `journal` 段 + `scripts/setup-builder-loop.sh`（启动时 restore 逻辑）
- budget budget 感知：`scripts/run-pass-cmd.sh`（max_iterations 动态读取）+ judge agent 联动

### 8.6 CHANGELOG 触及

- V3.5 / V4.0 各自版本段——每件事完成时追加（按现有 CHANGELOG 风格）

---

## 9. 执行任务列表（高层，具体步骤留子 plan）

### 9.1 V3.5（开工时 V3.5 三件事各自 /planner 生成 .claude/plans/ 下子方案）

**identity · 来源身份层**（M 量级）：
1. 设计 `agent_id` 在 lock 文件命名中的位置（`cc-subagent-<session_id>-<agent_id>.lock`）
2. 改 `subagent-start-guard.sh` 加 `agent_type` 反向白名单 + 落 agent_id 维度的 lock
3. 改 `worktree-write-guard.sh` / `tester-lock-check.sh` / `reviewer-timing-check.sh` 跟进 lock 命名
4. 写 fixture 覆盖：单 session 内并发 N 个 subagent 各落各的锁 / 非白名单 agent skip
5. 跑全套 e2e fixture 确认 V3.0/V3.3/V3.4 既有行为不破
6. 标记 `improvements.md` 9+ 条对应同根因为 "promoted to 架构改造 V3.5/identity"

**schema-out · StructuredOutput**（M-L 量级）：
1. 设计 4 套 agent 公共 schema 基础设施（`skills/builder-loop/schema/agent-output/`）
2. 先做 reviewer 一套：定义 schema → 改 `agents/reviewer.md` prompt → 接 StructuredOutput 校验/重试 → fixture
3. dotfiles 仓同步 `~/.claude/agents/reviewer.md`
4. 扩展到 doc-maintainer / tester / judge 三套（各自 schema + dotfiles 同步）
5. 在主循环引入 schema 校验失败的重试逻辑（带最大次数兜底）
6. 标记 `improvements.md` 3+ 条对应同根因（[A2] DEVIATION_FROM_SPEC / [A2] schema 字段变更 / reviewer 长 diff 误读）

**doc-gate · step 3.5 机械化**（S-M 量级）：
1. 列出当前主观裁量点清单（doc skip 判定 / commit-before-merge 判定 / plan.md 更新触发）
2. 设计机械检测规则（pattern match + 文件路径模式 + git status 信号）
3. 写 `scripts/doc-trigger-detect.sh` 实现规则
4. 改 dotfiles 的 `~/.claude/commands/builder.md` step 3.5 改用脚本输出
5. fixture 覆盖三类场景（应触发 doc 更新 / 应触发 commit-before-merge / 应触发 plan.md 更新）
6. 标记 `improvements.md` 4+ 条对应同根因

### 9.2 V4.0（待 V3.5 收尾时各件事 /planner）

V4.0 各件事的高层骨架（不展开 step 细节，V3.5 收尾时再 /planner 各件 plan）：

**lsp-lint · LSP/mypy 轻量接入**——支持 Python（mypy）和 TypeScript（tsserver）；loop.yml 加可选 `referential_stage`；fixture 双语言覆盖。

**no-barrier pipeline 无 barrier**——先写 pipeline + V3.0 phase 状态机的状态交互图作为前置条件；改造 builder-loop-stop.sh 核心循环。

**resume resume journal**——state 加 journal 段；setup 时检测 journal 优先级高于 V3.3 orphan 检测；老 state 缺 journal 段时降级走旧 orphan 路径。

**budget budget 感知**——loop.yml `max_iterations` 改可读 `budget.remaining()` 表达式；judge agent 联动；缺省值兜底。

---

## 10. 验收标准

### 10.1 V3.5 验收（机械化判据 + 人工兜底）

机械化（CI 可跑）：
- identity/schema-out/doc-gate 三件事各自 fixture 全 PASS
- `improvements.md` 至少 16 条同根因条目（identity 9+ / schema-out 3+ / doc-gate 4+）被标记 "promoted to 架构改造 V3.5"
- V3.0/V3.3/V3.4 既有 fixture 全套不退化（兼容性 fixture 100% PASS）

人工兜底（不可机械化）：
- identity 完成后跨 session 并发跑 ≥2 个 builder-loop 7 天无 lock 撞车日志
- schema-out 完成后 reviewer 误读 / schema 漂移类问题 4 周内零新增 improvements.md 条目
- doc-gate 完成后步骤 3.5 主观裁量 skip 类问题 4 周内零新增

### 10.2 V4.0 验收

机械化：
- C/no-barrier/resume/budget 各自 fixture 全 PASS
- `improvements.md` lsp-lint 类同根因（6+ 条）被标记 "promoted to 架构改造 V4.0"
- V3.5 既有 fixture 不退化

人工兜底：
- lsp-lint 完成后 reviewer 符号语义类问题 8 周内零新增
- no-barrier pipeline 改造后跨任务并行的 wall-clock 实测改善（具体数字交付时落 CHANGELOG）
- resume resume journal 在 ≥2 次模拟崩溃恢复 e2e 测试 PASS

### 10.3 整体路线图验收

V4.0 完成后 8 周观察期：
- 三类同根因（subagent 落点 / step 3.5 / reviewer 符号语义）零新增——核心成功标准
- 若有新增同模式条目，本路线图视为部分失败，触发 §10.4 复盘

### 10.4 失败复盘条件

如果 V4.0 完成 8 周后三类同根因新增 ≥1 条 → 复盘：
- 当前 fallback 方案（agent_type 反向白名单 / schema-out 4 套 / 机械检测 / LSP 子集）是否真正消除根因
- 是否要回到 "等 CC 补 agent_transcript_path / 重新评估方向 3 完整版"
- 复盘结论进入下一版本（V4.5 / V5.0）规划

---

## 11. 测试计划

### 11.1 总体策略

- 每件事**必须**带 fixture 覆盖（fixture 框架已有，见 `skills/builder-loop/fixtures/`）
- fixture 类型分两类：
  - **e2e fixture**（真实跑 setup → loop → PASS → cleanup 全流程）—— identity / no-barrier / resume
  - **unit fixture**（单脚本/单 schema 黑盒）—— schema-out / doc-gate / lsp-lint / budget

### 11.2 关键测试场景（路线图层级，子 plan 时各自展开）

**identity**：
- 单 session 单 subagent → 落锁文件名含 agent_id
- 单 session 并发 N agent → N 个独立 lock，互不覆盖
- 非白名单 agent_type（如 inline workflow）→ skip 落锁
- 既有 V3.0/V3.3/V3.4 e2e 不退化

**schema-out**：
- reviewer schema 校验 PASS 路径
- reviewer schema 校验 FAIL 路径（自动重试 N 次后降级）
- 4 套 agent 各自的 schema 与代码生成的 state 字段一致性（test-skill-md-schema-consistency.sh 风格）
- dotfiles 同步：cc-builder-loop 仓 prompt vs ~/.claude/agents/ 同一文件 diff = 空

**doc-gate**：
- 应触发 doc 更新场景（新增对外文件）→ 检测脚本输出 trigger
- 不该触发场景（内部子流程改动）→ 输出 skip
- 历史 3 个误判 skip case（05-28 / 05-24 / 05-11）作为回归 fixture

**lsp-lint**：
- Python 项目 mypy 接入：方法名不存在 → fail；方法名存在 → pass
- TypeScript 项目 tsserver 接入：同上
- loop.yml 缺 referential_stage 字段 → skip 该 stage（向后兼容）

### 11.3 dogfooding 校验

cc-builder-loop 仓本身接入 builder-loop（`.claude/loop.yml` 存在），V3.5/V4.0 各件事完成后，在本仓自身跑一次 loop 应能跑通。这是原则七 dogfooding 的最终验收点。

---

## 12. 留桌 & 未结（前面对话产物，避免遗忘）

> 这一段是元层的——不是 V3.5/V4.0 要做的事，是路线图层级**没回答但悬置**的判断。等后续翻牌时翻。

### 12.1 "深度维 = 入场券不是护城河" 那一刀

`cc-loop-tracking.md` §4 把 cc-builder-loop 自定位为 "深度维"。按 C5 试金石审：
- 深度维核心 = PASS_CMD 退出码兜底 + worktree 隔离 + reward hacking 防御
- 这些 1-2 年内会被任何成熟 agent 框架自动收敛到（Devin / OpenHands / SWE-agent 都在补）
- 那 "深度维" 作为差异化 = **入场券**，不是护城河

护城河可能不在 "深度维" 标签里，而在：
- **判据分层哲学**的工程化（design-philosophy 原则零）
- **dogfooding 纪律**的工程化（原则七 + 元修复机制）

后者 cc-loop-tracking 反而没强调。这道戳本路线图未接——未来某天看清是 self-positioning 还是真护城河时翻。

### 12.2 builder-loop 是 metacog 框架工程化载体之一这个身份

cc-loop-tracking 定的范围是 "和 CC 官方能力的关系"。但 metacog 框架（C2/C5/C10/C12/C13）对 builder-loop 的反向输入这条线 cc-loop-tracking 完全没接通：
- 方向 3 完整版（判据轴展开）= metacog C13 工程化
- 方向 4（工程 loop + 理论 loop 分裂）= metacog C12 工程化

本期把 lsp-lint 件事当方向 3 轻量版做了，但完整版没做。**完整版要不要做的前提**是承认 builder-loop 是 metacog 框架的工程化载体之一这个身份——前面对话用户未接也未拒，留着。

### 12.3 dogfooding 元修复（同根因 ≥3 次自动升板）

本期不做（§4.3 已说）。代价显式承认：未来再积 ≥3 条同根因仍依赖人手动翻 improvements.md。若 V4.0 完成 8 周后又积出新的 ≥3 次同根因，本判断需复审——届时再决定要不要把元修复挂 V4.5/V5.0。

---

## 13. 路线图本身的更新规则（§8 覆写不 append）

- 任何件事开工 → 在 §9 对应件事段末尾标 `[in-progress: <子 plan 路径>]`
- 件事完成 → §9 对应段落覆写为 `[done: <CHANGELOG 版本段链接>]`，本路线图保留高层信息、细节进 CHANGELOG
- 风险 / 文件地图 / 验收标准的更新 → 直接覆写对应小节，不 append
- V3.5 完成 → §6.1 V3.5 段落标 "done"，§9.1 整段移除（细节进 CHANGELOG V3.5 段），§10.1 验收结果填回
- V4.0 完成 → 整文件标 `[archived: V4.0 收尾 <date>]`，git 留痕、git rm 删除本文件
