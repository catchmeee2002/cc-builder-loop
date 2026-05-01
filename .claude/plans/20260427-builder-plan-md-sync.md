# Builder.md 加 plan.md 同步硬步骤

<!-- role:shared -->

## 背景 & 目标

**背景**：另一个 builder 把「`docs/plan.md` 维护」外包给 `doc-maintainer` subagent，违反 CLAUDE.md「builder 主体责任」硬规则，造成 plan.md 长期落后于代码状态。
该 builder 提的纠正方案是「写进项目记忆」——但 memory 是被动召回，下次会话能不能用上完全靠 LLM 自觉（被压缩/被忽略/注意力被新东西抢走都可能）。memory 不是强制约束机制。

**真正可行的强制位置**：
- **本任务采纳**：`~/.claude/commands/builder.md` 加新步骤——每次进 builder 模式 skill 自动加载，强制力高于 memory，跟现有「步骤 3.5 文档评估」同样的硬约束模式
- **明确不采纳**：`loop.yml` `pass_cmd` 加 `plan-sync` stage（业务文档维护塞进代码门禁是责任错配；mtime heuristic 在 worktree 模式下失效；跨项目通用性差）
- **明确不采纳**：reviewer.md 同步改（scope 隐含一个文件，依赖 builder 中档强制力即可，避免跨文件维护）
- **明确不采纳**：git pre-commit hook（plan.md 多数项目不入 git，hook 拿不到 staged 改动）
- **明确不采纳**：PostToolUse hook 监听 Edit（每次 Edit `.py` 都问会聒噪，体验崩）

**目标**：在 `~/.claude/commands/builder.md` 加一段「步骤 3.5.5 plan.md 同步检查」，作为 builder 主流程的强制步骤，与「步骤 3.5 文档评估」并列。
触发条件：项目根 `docs/plan.md` 存在。
强制力：必须 Read → 输出判断 → 需变更时 Edit。
显式禁止：把 plan.md 维护甩给 doc-maintainer。

**成功标准**：
1. builder.md 加了步骤 3.5.5，描述与现有步骤 3.5 文档评估同档（必须 Read / 必须显式输出 / 不允许静默跳过 / 不允许外包给 subagent）
2. `docs/plan.md` 不存在的项目自动 skip（输出 `📋 plan.md: skip (本项目无 docs/plan.md)`）
3. `docs/plan.md` 存在的项目，builder 在 commit 前必出现一行 `📋 plan.md: ...` 输出
4. 步骤位置：步骤 3.5（文档评估）之后、步骤 4（commit）之前
5. 跨项目通用：novel_writer / Personal_Assistant_Bot 等已有 `docs/plan.md` 的项目自动覆盖；非标项目自动 skip

## 预估改动级别

**L1 纯文案** — 单文件 prompt 改动，无逻辑/接口变化。

理由：只在 `~/.claude/commands/builder.md` 加一段 markdown 章节，不涉及代码、不涉及 hook、不涉及 schema。

## 约束 & 边界

**不能碰**：
- cc-builder-loop 仓**零改动**（与本任务正交，本仓是 loop 机制 owner，不负责 plan.md 维护）
- `~/.claude/agents/reviewer.md`**不动**（已在追问中明确不做双层防线）
- `~/.claude/agents/doc-maintainer.md` 内容**不动**（不需要让 doc-maintainer 拒绝 plan.md，因为 builder 主流程会在它 spawn 之前完成）

**必须兼容**：
- 现有 `~/.claude/commands/builder.md` 步骤 1~5 的语义不变（仅在 3.5 与 4 之间插入新章节）
- 项目根**无** `docs/plan.md` 时必须自动 skip 不报错（覆盖 cc-builder-loop / dotfiles / 大部分内部小项目）
- L1 / L2 / L3 改动级别的所有项目都走这条（保持 builder.md 步骤间的一致性，不为级别开特例）

**改动落点仓库**：`~/.claude/commands/builder.md` 是 dotfiles 仓的文件（具体路径 `~/.hongyu.liao_debian12/my-dotfiles/claude/.claude/commands/builder.md`，`~/.claude/commands/builder.md` 是 stow 软链）。
本方案文件虽落在 cc-builder-loop 仓 `.claude/plans/`，但**实际改动文件在 dotfiles 仓**——builder 接手执行时建议 cd 到 dotfiles 仓再改。

## 技术选型

| 方案 | 描述 | 强制力 | 维护成本 | 选择 |
|------|------|--------|---------|------|
| A. 只写进 memory（已弃方案） | feedback 类 memory 写「plan.md 维护是 builder 主体责任」 | 弱（被动召回，可能被压缩/淹没） | 极低 | ❌（用户已认知不靠谱） |
| B. **builder.md 加步骤 3.5.5（本方案）** | 与步骤 3.5 文档评估并列，每次 builder 模式启动 skill 自动加载 | 中（每次注入，跟其他步骤同档） | 低（单文件单段） | ✅ |
| C. loop.yml pass_cmd 加 plan-sync stage | 机器层硬阻塞 | 强 | 中（每项目要写 check 脚本） | ❌ 三大硬伤：责任错配 / mtime 在 worktree 失效 / 跨项目通用性差 |
| D. PreToolUse hook 监听 Edit | 每次 Edit 文件触发检查 | 极强 | 高（hook 维护 + 噪声） | ❌ 聒噪体验崩 |
| E. git pre-commit hook | commit 时阻塞 | 强 | 低 | ❌ plan.md 多数项目不入 git，hook 拿不到 |
| F. reviewer.md 加判据 | 后置实证 | 中 | 中（双文件维护） | ❌ 用户已选「只改 builder.md」 |

**选 B 的理由**：
1. 跟现有「步骤 3.5 文档评估」语义/格式/强制力对齐，builder.md 步骤间一致性最好
2. 单文件改动，dotfiles 仓 commit 一次完成，全机器所有项目立即覆盖
3. 不引入新基建（不写脚本/不加 hook/不改 schema），改完就用
4. 失败模式可控：LLM 敷衍输出 `📋 plan.md: 已检查` 没真查时，最坏退化到现状（memory 也只能拦到这个程度），但形式上有强制声明义务，比纯 memory 强

## 方案设计

### 在 `~/.claude/commands/builder.md` 中插入新章节

**插入位置**：「步骤 3.5：文档评估」章节末尾的水平分隔线 `---` **之后**、「步骤 4：自动 commit」章节标题之前。

**新章节标题**：`## 步骤 3.5.5：plan.md 同步检查（独立判断，不依赖 doc-maintainer）`

**章节内容设计原则**：
- 跟「步骤 3.5 文档评估」**镜像同构**：触发条件 → 强制动作 → 必须显式输出 / 不允许静默跳过的硬规则
- 显式禁止「外包给 doc-maintainer」，并说清楚理由（doc-maintainer 是模块 CLAUDE.md 同步机制，不是 plan.md owner）
- 启发式不写死「grep #N」这种僵化规则——让 LLM 读全文判断，给软提示（看任务编号 / phase 段落 / changed_files 路径关键词）
- L1/L2/L3 各级别都走，但 L1 时大概率输出 skip（无业务任务进展对应 plan.md）

### 章节文案（草稿）

```markdown
## 步骤 3.5.5：plan.md 同步检查（独立判断，不依赖 doc-maintainer）

触发：项目根存在 `docs/plan.md`（不论是否入 git）。不存在 → 输出 `📋 plan.md: skip (本项目无 docs/plan.md)` 进入步骤 4。

**强制动作**（plan.md 存在时）：

1. Read `docs/plan.md`
2. 判断本次 changed_files / 任务进展是否对应 plan.md 中某条目（启发式：编号锚点 #N / phase 章节 / changed_files 路径关键词；不限制具体格式，按 plan.md 实际结构由你判断）
3. 输出**恰好一行**结论：
   - 找到相关条目且需更新 → Edit plan.md 加 ✅/🚧/作废 + 方案指针，输出 `📋 plan.md: 已更新条目 #X (✅完成/🚧进行中/作废)`
   - 找到相关条目但状态已正确 → 输出 `📋 plan.md: 已检查 (#X 状态正确，无需变更)`
   - 未找到相关条目 → 输出 `📋 plan.md: 已检查 (无对应条目，本次改动属 plan.md 范围外)`

**⛔ 不允许**：
- 把 plan.md 维护**甩给 doc-maintainer**。doc-maintainer 是模块 CLAUDE.md / 测试 CLAUDE.md 的同步机制，**不负责 plan.md**。Spawn doc-maintainer 的 prompt 中**不得**出现「同步 plan.md」「更新 plan.md」字样。
- 静默跳过。即便 L1 纯文案改动、reviewer 走兜底、loop 异常收尾，本步骤仍**必须出现一行 `📋 plan.md:` 输出**。

---
```

### 与现有流程的衔接

| 现有步骤 | 本步骤的关系 |
|----------|-------------|
| 步骤 3 / 3a / 3a+ / 3b / 3c（reviewer 流） | 平级，独立判断（与步骤 3.5 文档评估一样不依赖 reviewer 结果） |
| 步骤 3.5 文档评估 | 紧邻其后，**两条独立产出线**关系（plan.md 与模块 CLAUDE.md 是不同范畴的文档） |
| 步骤 4 自动 commit | 必须在 commit 之前完成（plan.md 改动需要被 commit 一并带入；如 plan.md 不入 git 则改完不影响 commit） |
| 步骤 5 任务回顾 | 不影响。本步骤产生的 plan.md 改动若属 A 类知识可在步骤 5 沉淀 |

## 风险 & 应对

| 风险 | 触发场景 | 应对 |
|------|---------|------|
| LLM 敷衍输出 `已检查` 但实际没真查 | 中档强制力的固有失败模式 | 接受。本任务不引入双层实证（reviewer.md 已明确不动）。比纯 memory 仍强一档（形式上有强制输出义务） |
| 项目 plan.md 路径不在 docs/ 下（如 `.claude/plan.md` / `PLAN.md`） | 用户提到「一半项目」用 docs/plan.md，剩下一半路径不一定 | 接受。本方案显式选硬编码 `docs/plan.md`，非标项目走 skip 路径——后续若有真实非标项目用户主动反馈，再升级为多候选路径 |
| 跟 doc-maintainer 的边界仍可能被新入门 LLM 跨过 | 新会话 LLM 看不到「煞笔 builder 反思」上下文 | 章节文案中显式写 `⛔ 不允许 spawn doc-maintainer 时把 plan.md 列为同步目标` 这条硬规则。同步动作禁止比职责声明更强 |
| dotfiles 仓改完后未 commit / 未 stow | 改了 source 文件但 `~/.claude/commands/builder.md` 软链未指向最新 | dotfiles 用 stow 已是软链，source 文件改完即时生效；用户自行 commit 即可（dotfiles 仓不接入 builder-loop） |
| L1 纯文案改动每次都强制输出一行 plan.md 状态 | 文案噪声风险 | 接受。一行输出成本极低，跟现有「步骤 3.5 文档评估」的 `📄 doc: skip` 同档，保持一致性 |

**退路**：本改动是单文件单段 prompt 增量，可直接 git revert dotfiles 仓对应 commit，零副作用。

## 文件地图

**改动文件**（dotfiles 仓）：

| 文件 | 改动 |
|------|------|
| `~/.hongyu.liao_debian12/my-dotfiles/claude/.claude/commands/builder.md` | 在「步骤 3.5 文档评估」末尾的 `---` 后、「步骤 4 自动 commit」标题前，插入新章节「步骤 3.5.5：plan.md 同步检查」 |

**不改文件**：
- cc-builder-loop 仓所有文件（包括本方案文件落地的 `.claude/plans/`）
- `~/.claude/agents/reviewer.md`
- `~/.claude/agents/doc-maintainer.md`
- `~/.claude/commands/planner.md`
- 任何业务项目的 `docs/plan.md`

**软链**（已就绪，不动）：
- `~/.claude/commands/builder.md` → `~/.hongyu.liao_debian12/my-dotfiles/claude/.claude/commands/builder.md`

## 执行任务列表

1. cd 到 dotfiles 仓 `~/.hongyu.liao_debian12/my-dotfiles/`
2. Read `claude/.claude/commands/builder.md`，定位「步骤 3.5：文档评估」章节末尾的 `---` 行（在「步骤 4：自动 commit」标题之前）
3. Edit：在该 `---` 之后、`## 步骤 4` 之前，插入「步骤 3.5.5：plan.md 同步检查」整段（文案见上方「方案设计 / 章节文案（草稿）」）
4. （可选）Read 一次确认插入位置正确、章节序号完整（步骤 3 → 3.5 → **3.5.5** → 4 → 5）
5. 在 dotfiles 仓 commit：`type` 用 `chore` 或 `docs`，scope 用 `claude`，描述如 `chore(claude): [cr_id_skip] Add plan.md sync check to builder.md step 3.5.5`
6. **不需要 reload**——`~/.claude/commands/builder.md` 是 stow 软链，下一次新会话进 builder 模式时立即生效

## 验收标准

**冒烟验证**（手动跑一遍）：
1. cat `~/.claude/commands/builder.md`，确认章节顺序为 步骤 3 / 3.5 / **3.5.5** / 4 / 5
2. 章节内含三个核心要素：触发条件、强制动作（Read + 输出三态之一）、⛔ 禁止外包
3. 文案中明确出现 `docs/plan.md` 路径硬编码

**端到端验证**（下一次 builder 模式跑实际任务）：
1. 在有 `docs/plan.md` 的项目（如 novel_writer / Personal_Assistant_Bot）跑 builder，commit 前应出现 `📋 plan.md: ...` 一行输出
2. 在无 `docs/plan.md` 的项目（如 cc-builder-loop / dotfiles 自身）跑 builder，commit 前应出现 `📋 plan.md: skip (本项目无 docs/plan.md)`
3. 即便 reviewer 走兜底（3c）或 loop 异常收尾，仍出现 `📋 plan.md:` 一行（与步骤 3.5 `📄 doc:` 行为对齐）

**反向验证**（确认禁止规则生效）：
- 下次跑实际任务时，spawn doc-maintainer 的 prompt 中**不应**出现「同步 plan.md」「更新 plan.md」「维护 plan.md」字样

## 测试计划（可选）

**测试目标**：验证 prompt 改动对 builder LLM 行为的实际影响。

**关键场景**：
| 场景 | 预期输出 | 验证方式 |
|------|---------|---------|
| 项目无 `docs/plan.md`（如 cc-builder-loop） | `📋 plan.md: skip (本项目无 docs/plan.md)` | 跑一次小任务观察 builder 输出 |
| 项目有 `docs/plan.md` 且本次改动对应某编号条目 | `📋 plan.md: 已更新条目 #X (...)` 并 plan.md 实际被 Edit | 在 novel_writer 跑一次小任务，diff plan.md |
| 项目有 `docs/plan.md` 但本次改动属范围外（如 lockfile / 配置文件） | `📋 plan.md: 已检查 (无对应条目，本次改动属 plan.md 范围外)` | 在 novel_writer 改 `pyproject.toml` |
| L1 纯文案改动 | 仍有 `📋 plan.md:` 输出（不为 L1 开特例） | 在 novel_writer 改一段注释 |

**测试深度**：快速。本任务是 prompt 改动，不写自动化测试；冒烟 + 一次实际任务跑通即可。

<!-- /role -->

<!-- role:builder -->

## 实现提示（仅 builder 可读）

### 插入点精确定位

`~/.hongyu.liao_debian12/my-dotfiles/claude/.claude/commands/builder.md` 中已有的步骤 3.5 末尾结构：

```
> ⛔ 不允许静默跳过。即使 reviewer 走到 3c 兜底、或者任务是异常收尾（如 loop 被 auto-commit 失败打断后手动收尾），3.5 仍必须走一次——**reviewer 和文档是并列的两条独立产出线，不存在前者跳后者就能跳的关系**。

---

## 步骤 4：自动 commit（Reviewer 通过后）
```

Edit 锚点选 `> ⛔ 不允许静默跳过。即使 reviewer 走到 3c 兜底、或者任务是异常收尾（如 loop 被 auto-commit 失败打断后手动收尾），3.5 仍必须走一次——**reviewer 和文档是并列的两条独立产出线，不存在前者跳后者就能跳的关系**。\n\n---\n\n## 步骤 4：自动 commit（Reviewer 通过后）`，
new_string 在原文 `---` 与 `## 步骤 4` 之间插入新章节即可，整体结构保留。

### 章节文案（最终版，复制即可）

```markdown
---

## 步骤 3.5.5：plan.md 同步检查（独立判断，不依赖 doc-maintainer）

触发：项目根存在 `docs/plan.md`（不论是否入 git）。不存在 → 输出 `📋 plan.md: skip (本项目无 docs/plan.md)` 进入步骤 4。

**强制动作**（plan.md 存在时）：

1. Read `docs/plan.md`
2. 判断本次 changed_files / 任务进展是否对应 plan.md 中某条目（启发式：编号锚点 #N / phase 章节 / changed_files 路径关键词；不限制具体格式，按 plan.md 实际结构由你判断）
3. 输出**恰好一行**结论：
   - 找到相关条目且需更新 → Edit plan.md 加 ✅/🚧/作废 + 方案指针，输出 `📋 plan.md: 已更新条目 #X (✅完成/🚧进行中/作废)`
   - 找到相关条目但状态已正确 → 输出 `📋 plan.md: 已检查 (#X 状态正确，无需变更)`
   - 未找到相关条目 → 输出 `📋 plan.md: 已检查 (无对应条目，本次改动属 plan.md 范围外)`

**⛔ 不允许**：
- 把 plan.md 维护**甩给 doc-maintainer**。doc-maintainer 是模块 CLAUDE.md / 测试 CLAUDE.md 的同步机制，**不负责 plan.md**。Spawn doc-maintainer 的 prompt 中**不得**出现「同步 plan.md」「更新 plan.md」字样。
- 静默跳过。即便 L1 纯文案改动、reviewer 走兜底、loop 异常收尾，本步骤仍**必须出现一行 `📋 plan.md:` 输出**。

---
```

### 工程指引

- 改动文件在 dotfiles 仓不在 cc-builder-loop 仓——**不要在 cc-builder-loop 仓 commit**，本仓 `.claude/loop.yml` 不适用 dotfiles 改动
- builder 接手时**不要触发 setup-builder-loop.sh**（cc-builder-loop 仓的 worktree 与本任务无关），手动改 + dotfiles 仓 commit
- dotfiles 仓的 commit msg hook 应该跟 cc-builder-loop 同样要求 `[cr_id_skip]` 标记
- 改完后下一次新会话进 builder 模式时立即生效（stow 软链 + skill 重新加载）

<!-- /role -->

<!-- role:tester -->

N/A — 本任务为 prompt 文档改动，不需要自动化测试用例。
人工冒烟 + 下次任务实际验证即可（见 shared 视图的「测试计划」）。

<!-- /role -->
