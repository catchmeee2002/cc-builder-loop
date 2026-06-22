# Reviewer 模型兼容 max + copilot 双路径

<!-- role:shared -->

## 背景 & 目标

### 观察到的故障

2026-04-24 一次 Builder 任务触发 reviewer 后台运行，reviewer 返回 API 参数错误：**"haiku 不支持 xhigh effort"**。Builder 按 `builder.md` 的 "最多 2 次 haiku" 规则重试一次，仍有可能踩同一错误（侥幸型重试）。

### 根因

- 全局配置 `~/.claude/settings.json` 设了 `"effortLevel": "xhigh"`
- reviewer agent frontmatter 声明 `model: haiku`
- CC spawn subagent 时把全局 effortLevel 传给 haiku → haiku 不支持该 effort → 调用失败
- 两条 API 路径行为不同：
  - **max 方案**（Claude Max 官方 Anthropic 端点）：haiku 的 reasoning_effort 上限受限（典型为 medium/low，不支持 high/xhigh）
  - **copilot 方案**（`copilot-api` 代理 GitHub Copilot，配合 `/copilot-cheap` 热身代理）：Copilot 中转会对 effort 参数做转换/忽略，但模型可用矩阵与官方不完全一致

### 目标

让 reviewer 在 **max** 与 **copilot** 两条路径下都能稳定 spawn 成功，首次就返回 `REVIEW_SUMMARY`。消除"靠盲重试一次兜底"的侥幸模式。

### 成功标准

1. 新机器装好 dotfiles + cc-builder-loop 后，`~/.claude/settings.json` 保持 `effortLevel: xhigh` 不变，Builder 触发 reviewer 一次成功
2. 在 max 方案和 copilot 方案两条路径下均能通过 E2E 配置一致性测试
3. 若出现未知 API 错误，Builder 能按新的退路逻辑（降 effort / 换模型）重试一次，而不是盲重试同一组合

## 预估改动级别

**L2（实现改动）**。frontmatter 模型字段、builder.md retry 文案、兜底文档三处同步修改；新增一个 E2E 脚本。无新接口、无新模块。理由：依赖项目内约定已存在，只是把默认模型从 haiku 升级到 sonnet 并细化 retry 策略。

## 约束 & 边界

1. **不改 CC 源码**（cc-builder-loop 项目原则）
2. **不动全局 `effortLevel: xhigh`**（它影响主 Claude 行为，用户已显式偏好 xhigh，不应为了迁就 reviewer 而降级全局）
3. **跨仓改动**：dotfiles（`~/.claude/my-dotfiles/claude/.claude/`）+ cc-builder-loop 两仓均要改，方案落盘在 cc-builder-loop，但 Builder 执行时需同步编辑 dotfiles 源文件
4. **向后兼容**：已有 builder-loop 接入项目不需要改它们的 `.claude/loop.yml`，本次只动 reviewer/builder 层
5. **E2E 不强依赖实时 API**：测试脚本以配置一致性 lint 为主，live smoke 为可选开关

## 验收标准

1. `~/.claude/agents/reviewer.md` frontmatter `model` 字段为 sonnet（或其他同时支持 xhigh 的模型），非 haiku
2. `~/.claude/commands/builder.md` 步骤 3 retry 段落措辞与新模型一致，且含"首次 API 错误 → 降 effort/换模型 再重试"的策略
3. `skills/builder-loop/docs/reviewer-fallback.md` 无残留 "haiku" 字样（如果旧文案有）
4. 新增脚本 `skills/builder-loop/fixtures/e2e/test-reviewer-compat.sh` 运行：
   - 子测 A（配置 lint）：必须通过
   - 子测 B（live smoke，可选 `--live`）：在 max 或 copilot 环境里 spawn 一次真 reviewer，返回 `REVIEW_SUMMARY` 视为通过
5. 手动冒烟：按当前聊天里的场景复现一次 Builder 触发 reviewer，不再踩 "haiku + xhigh" 错误

<!-- /role -->

<!-- role:builder -->

## 技术选型

### 方案 A（采用）：reviewer 默认模型升级为 sonnet

- **改动**：`~/.claude/agents/reviewer.md` frontmatter `model: haiku` → `model: sonnet`
- **兼容性**：sonnet 在 max 与 copilot 两条路径下都稳定支持 xhigh
- **成本**：相对 haiku 每次 reviewer 多烧 5~10× token，但 reviewer 只审 diff（通常 < 200 行），实际多出的金额较低
- **推荐理由**：低风险一次性消除主容错场景

### 方案 B（排除）：haiku + frontmatter 锁低 effort

CC 的 subagent frontmatter 目前只声明 `model: <name>`，**未公开 reasoning_effort/effortLevel 覆盖字段**（grep 验证无相关字段）。即使能写入也依赖未文档化的 CC 内部行为，升级 CC 时易破。排除。

### 方案 C（排除）：动态探测 API 路径选型

在 reviewer 启动时读 `ANTHROPIC_BASE_URL` 判定 max/copilot 再选模型。引入运行时逻辑但收益有限（sonnet 在两条路径都稳），且让 reviewer agent 变得非纯声明式。排除。

### 方案 D（补充，与方案 A 组合采用）：Builder-side 退路

reviewer 首次失败时，Builder 解析错误消息：
- 命中 `effort|reasoning|not supported|invalid` 关键词 → 第 2 次 spawn 时在 prompt 里显式加一句"⚠️ 前一次因 reasoning effort 不兼容失败，本次请在 assistant 文本中不发起任何需要高 effort 的元操作，采用标准审查流程"（**注**：effort 由 CC 内核从 settings 传入，Builder 无法改参数；唯一能做的是传一次 `model` 覆盖—— 用 Task 工具 spawn 时无此入口）
- 命中 `rate_limit|overloaded` → 标准第 2 次重试
- 其他错误 → 直接走兜底

因 Builder 无法通过 spawn 参数覆盖 subagent 的 model/effort，方案 D 的实际作用退化为 **"错误分类 + 重试/兜底决策"**（不再盲重试），不再是"换模型"。仍有价值：快速诊断、缩短失败路径。

## 方案设计

### 整体架构

```
Builder 完成改动
   │
   ▼
spawn reviewer (model=sonnet, 默认 effort=xhigh 由全局配置传入)
   │
   ├── OK → 正常流程 (REVIEW_SUMMARY → commit)
   │
   └── API 错误
        │
        ├── 错误分类（关键词匹配）
        │    ├── effort/reasoning 类 → 已在 sonnet 上不应出现，若仍出现直接兜底（视为 CC 内核问题）
        │    ├── rate_limit/overloaded → 标准重试（sleep 后再 spawn）
        │    └── 其他（网络/5xx） → 标准重试 1 次
        │
        └── 仍失败 → Read reviewer-fallback.md 执行轻量自审
```

### 改动清单

| # | 文件 | 改动内容 |
|---|------|----------|
| 1 | `~/.claude/my-dotfiles/claude/.claude/agents/reviewer.md` | frontmatter `model: haiku` → `model: sonnet` |
| 2 | `~/.claude/my-dotfiles/claude/.claude/commands/builder.md` | 步骤 3 的 "最多 2 次 haiku" 改为 "最多 2 次 sonnet，错误分类决定重试策略"；新增错误分类说明段 |
| 3 | `skills/builder-loop/docs/reviewer-fallback.md` | 如含 "haiku" 文案则同步更新 |
| 4 | `skills/builder-loop/fixtures/e2e/test-reviewer-compat.sh` | 新增 E2E 脚本，含 lint + 可选 live smoke |
| 5 | `CLAUDE.md` 第 5 节 | 新增一行 `V1.7: Reviewer 模型默认 sonnet（兼容 max/copilot 双路径）` |

### 文件地图（探索结果）

**已探索**：

- `/mnt/hongyu.liao/.hongyu.liao_debian12/my-dotfiles/claude/.claude/agents/reviewer.md`：frontmatter 第 4 行 `model: haiku`，共 104 行。其余内容与 model 无关
- `/mnt/hongyu.liao/.hongyu.liao_debian12/my-dotfiles/claude/.claude/commands/builder.md` 第 96 行："重试：最多 2 次 haiku，全败走兜底。" 第 119 行 "3b 重试：直接重新 spawn reviewer。" 第 121 行 "3c 兜底：Read `~/.claude/skills/builder-loop/docs/reviewer-fallback.md` 按其执行。"
- `/mnt/hongyu.liao_docker/cc-builder-loop/skills/builder-loop/docs/reviewer-fallback.md`：当前 23 行，未出现 "haiku"。只需核对一次，无需改
- `/mnt/hongyu.liao_docker/cc-builder-loop/skills/builder-loop/fixtures/e2e/`：现有 `test-conflict.sh / test-isolation.sh / test-new-repo-loop.sh / test-empty-repo.sh / test-arbitration-apply.sh / run-fixture.sh`。新脚本命名遵循 `test-*.sh`
- `/mnt/hongyu.liao_docker/cc-builder-loop/CLAUDE.md` 第 5 节末尾含 V1.5/V1.6 版本条目，追加 V1.7 一行即可
- 全局 `~/.claude/settings.json`：`effortLevel: xhigh`，本次不动

**新建**：

- `skills/builder-loop/fixtures/e2e/test-reviewer-compat.sh`

## 执行任务列表

> Builder 按顺序执行。跨仓改动：先改 dotfiles，再改 cc-builder-loop，最后跑 E2E。

### T1 — 改 reviewer 默认模型

- Edit `/mnt/hongyu.liao/.hongyu.liao_debian12/my-dotfiles/claude/.claude/agents/reviewer.md`
- 第 4 行 `model: haiku` → `model: sonnet`
- 其他不动

### T2 — 改 builder.md retry 策略

- Edit `/mnt/hongyu.liao/.hongyu.liao_debian12/my-dotfiles/claude/.claude/commands/builder.md`
- 第 96 行 `重试：最多 2 次 haiku，全败走兜底。失败时 ...` 改为：

  ```
  重试：默认 sonnet（兼容 max / copilot 双路径）。首次 API 错误时先按下述策略分类，最多 2 次全败走兜底。失败时 `⚠️ Reviewer 失败（原因:<短描述>），正在重试...`

  错误分类（根据返回消息关键词匹配）：
  - `effort|reasoning|not supported|unsupported_parameter|invalid_request` → 视为内核配置问题，跳重试直接走兜底（3c）
  - `rate_limit|overloaded|timeout|5\d\d` → 标准重试（3b），间隔 ≥10s
  - 其他 → 标准重试（3b）
  ```

- "3b 重试" 一段末尾追加一句：`若第 1 次失败已被分类为"内核配置问题"，跳过 3b 直接 3c。`

### T3 — 核对 reviewer-fallback.md

- Read `/mnt/hongyu.liao_docker/cc-builder-loop/skills/builder-loop/docs/reviewer-fallback.md`
- 若含 "haiku" 字样，Edit 替换为 "sonnet"；当前不含则跳过此步

### T4 — 新增 E2E 脚本

- Write `/mnt/hongyu.liao_docker/cc-builder-loop/skills/builder-loop/fixtures/e2e/test-reviewer-compat.sh`
- `chmod +x` 加执行权限
- 脚本职责（详见 tester 视图）：
  - 子测 A：配置一致性 lint（必跑，离线）
  - 子测 B：live smoke（可选 `--live`，需 CC 环境）
- 失败返回非零退出码

### T5 — 更新 CLAUDE.md 版本说明

- Edit `/mnt/hongyu.liao_docker/cc-builder-loop/CLAUDE.md` 第 5 节末尾
- 在 `- **V1.6**: ...` 之后追加：

  ```
  - **V1.7**: Reviewer 默认模型 sonnet（兼容 max / copilot 双路径，消除 haiku+xhigh 失败场景）+ Builder retry 错误分类
  ```

### T6 — 手动冒烟 + 跑 E2E

- 跑 `skills/builder-loop/fixtures/e2e/test-reviewer-compat.sh`（不带 `--live`）
- 若当前会话在 max 方案下可直接再 spawn 一次 reviewer（任意小改动），观察是否首次成功
- copilot 路径如尚未启动 `copilot-cheap`，在方案评审段人工提示用户后续自测

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| sonnet 成本比 haiku 高 5~10× | reviewer 只审 diff（通常 < 200 行），单次成本可控；若用户反馈过高再退回 haiku + 专门锁低 effort 的方案 |
| copilot-api 对 sonnet + xhigh 的支持细节可能变更 | E2E 的 live smoke 子测保留 `--live` 开关，定期在 copilot 环境跑一次验证 |
| 改 dotfiles 影响所有项目 | 此改动 net-positive（sonnet 兼容面更大），不破坏既有项目；若个别项目希望保持 haiku，可在各自 `.claude/agents/reviewer.md` 本地覆盖 |
| Builder 错误分类关键词未覆盖某些新错误 | 分类未命中走默认 "其他 → 标准重试"，保留原兜底通道，最坏情况等价于旧行为 |
| V1.6 reviewer-params.json 逻辑已依赖 haiku 假设 | 已 grep 确认只依赖参数 I/O，不绑定具体模型名，无需动 |

## 退路

若方案 A 上线后观察到 copilot 路径下 sonnet+xhigh 仍出错，回退为：
1. reviewer frontmatter 改 `model: haiku`（复原）
2. `~/.claude/settings.json` 新增 `"perSubagentEffort": { "reviewer": "medium" }`（如 CC 届时支持）或
3. 接受现状并让 Builder retry 使用 spawn 参数显式传 prompt hint 绕过 effort 问题

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标

验证 `test-reviewer-compat.sh` 端到端脚本本身正确，以及两条子测（配置 lint / live smoke）的行为符合契约。

### 关键测试场景

#### 子测 A：配置一致性 lint（必跑，离线）

脚本行为：

1. 校验 `~/.claude/agents/reviewer.md` frontmatter 含 `^model: sonnet\s*$` 且不含 `^model: haiku\s*$`
2. 校验 `~/.claude/commands/builder.md` 不含 `最多 2 次 haiku` 旧文案，且含 `sonnet` + `错误分类` 字样
3. 校验 `skills/builder-loop/docs/reviewer-fallback.md` 不含 `haiku`
4. 任一校验失败 → stderr 输出失败项 + exit 1；全通过 → stdout `✅ reviewer-compat lint PASS` + exit 0

#### 子测 B：live smoke（可选，`--live` 触发）

脚本行为：

1. 在临时目录建一个只有 `echo hello.py` 的 git repo
2. 触发一次"假 Builder spawn reviewer" 的最小路径：构造一份 `changed_files` / `diff_summary` / `report_path` 输入，用 CC CLI 的 `claude -p --no-tty` 或等价入口（如当前环境不支持则 **skip 不失败**）
3. 超时 60s；得到 `REVIEW_SUMMARY:` 视为通过；未得到视为失败
4. 未进入 live 流程（`--live` 未传）→ stdout `⏭  live smoke skipped`；live 流程不可用 → stdout `⏭  live smoke skipped (CC CLI unavailable)`

#### 边界测试

1. **dotfiles 未安装**：`~/.claude/agents/reviewer.md` 不存在 → 子测 A 输出清晰错误并 exit 1（不是 bash error）
2. **frontmatter 写了 sonnet 但大小写错（`model: Sonnet`）**：当前决定视为失败（严格匹配）
3. **两个路径都不可用时 `--live`**：skip 不失败
4. **脚本自身权限**：无 `chmod +x` 也能用 `bash test-reviewer-compat.sh` 跑通

### 测试深度

**快速**。配置 lint 是纯文本检查，用 grep/awk 即可完成。live smoke 是可选的烟雾测试，不求覆盖所有环境变体。总运行时长应 <3s（不含 live）。

### 目标测试目录

`/mnt/hongyu.liao_docker/cc-builder-loop/skills/builder-loop/fixtures/e2e/`

脚本文件名：`test-reviewer-compat.sh`

### 不测什么

- 不验证 sonnet 回答质量（那是模型问题，不是脚本问题）
- 不测 `~/.claude/settings.json` 的 effortLevel 值（用户偏好，不应绑在测试里）
- 不在 lint 子测里实际调 API（会引入外部依赖）

<!-- /role -->
