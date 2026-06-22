# E2E 验收 stage — 独立 tester 行为验证

## 背景 & 目标

divine-word 项目暴露了 builder-loop 的结构性缺口：PASS_CMD（编译/lint/单元测试）全绿但功能不完整——28 step plan 有两个 step 完全没实现，builder 声称完成。根因：PASS_CMD 只验证代码正确性，不验证运行时行为。

**目标**：在 PASS_CMD 全过后、judge 之前，加一个**独立 tester subagent 驱动的端到端行为验证**阶段。tester 读取 plan 中的行为验收用例，实际启动 app、驱动浏览器/CLI/API 验证行为，报告用例级 pass/fail。

**成功标准**：
1. plan 带 `<!-- e2e-cases -->` 标签的行为用例时，stop hook 自动拉 tester 做 e2e 验收
2. tester 只看用例 + app 运行态，不看源码/transcript/diff（独立性）
3. e2e 失败时用例级 pass/fail 清单喂回 builder，builder 据此修代码并重新迭代
4. plan 无 e2e-cases 标签时行为不变（向后兼容）

## 预估改动级别

**L3**（新接口/模块）——state schema 新字段、stop hook 新分支、tester 新模式、plan 格式扩展。

## 约束 & 边界

<!-- role:shared -->

### 必须保持
- 现有 PASS_CMD 行为不变——e2e 是 PASS_CMD 全过后的**追加阶段**，不替代不修改 PASS_CMD
- loop.yml schema 不变——e2e 是 plan 驱动的（任务级），不是项目配置驱动的（项目级）
- tester 现有"写测试文件"模式不变——e2e 是新增模式，根据输入区分
- V3.0 phase 状态机契约不变（active / passed_pending_review）
- 独立性原则：tester e2e 模式下只看用例文本 + app 运行态

### 不能碰
- run-pass-cmd.sh 现有 stage 执行逻辑
- builder-loop-stop.sh 的 FAIL 路径
- 现有 tester.md 的"写测试文件"模式的输入/输出契约

### 显式不做
- loop.yml 里不加 e2e 相关字段
- 不做 e2e 用例的自动生成（用例由 planner 阶段人/LLM 定义）
- 不做 e2e 结果的持久化/趋势分析

<!-- /role -->

## 技术选型

### 方案 A（推荐）：stop hook 内嵌 e2e 分支

PASS_CMD 全过后，在 judge 之前插入 e2e 验证分支：

```
PASS_CMD 全过
  ↓
state 有 e2e_plan_path？
  ├─ 否 → 走现有 judge 流程
  └─ 是 → extract-e2e-cases.sh 提取用例
         ↓
       spawn tester (e2e 模式)
         ├─ PASS → 走 judge 流程
         └─ FAIL → 喂回 pass/fail 清单，exit 2
```

**优点**：不引入新脚本入口，stop hook 已有 subagent spawn 经验（judge agent）。
**排除的替代**：方案 B（run-pass-cmd.sh 加 type: agent）——需要改 loop.yml schema，违背"e2e 是任务级不是项目级"。

## 方案设计

<!-- role:builder -->

### 1. Plan 格式扩展

plan 文件中用 HTML 注释标签包裹行为验收用例：

```markdown
<!-- e2e-cases -->
- 启动 app (python main.py --port 8080)
- 打开浏览器访问 localhost:8080
- 点击「开始游戏」按钮
- 应能看到至少1个 rival 文明出现在地图上
- 点击 rival 图标，应弹出包含名称和实力的属性面板
<!-- /e2e-cases -->
```

标签可出现在 plan 任意位置（通常在验收标准段内）。shared 视图——builder 和 tester 都能看到。

### 2. State 文件扩展

新增字段 `e2e_plan_path`：

```yaml
e2e_plan_path: .claude/plans/20260620-civilization-epic.md
```

- builder setup 时检测 plan 文件有 `<!-- e2e-cases -->` 标签 → 写此字段
- plan 无标签 → 不写此字段（向后兼容）
- stop hook 读此字段决定是否拉 e2e tester

### 3. extract-e2e-cases.sh

新增脚本，从 plan 文件提取 `<!-- e2e-cases -->` 到 `<!-- /e2e-cases -->` 之间的内容。

输入：plan 文件路径
输出：stdout 打印用例文本（纯 markdown 列表）
退出码：0 = 提取成功，1 = 无标签或文件不存在

### 4. Tester e2e 模式

tester.md 扩展，根据输入区分模式：

- 收到 `spec_view` + `interface_signatures` → 现有"写测试文件"模式
- 收到 `e2e_cases` → e2e 执行模式

**e2e 模式输入**：
- `e2e_cases`：行为验收用例文本（从 plan 提取的纯文本）
- `worktree_path`：工作目录（用于启动 app）

**e2e 模式隔离约束**：
- 只看 `e2e_cases` 文本和 app 运行时状态
- 禁止读源码文件（复用现有 tester-lock-check.sh 的 source_dirs 隔离）
- 禁止读 builder transcript

**e2e 模式执行流程**：
1. 解析用例列表
2. 按用例步骤依次执行（启动 app、驱动浏览器/curl/CLI）
3. 每条用例判定 PASS/FAIL/SKIP
4. 执行完毕后清理（杀 app 进程）
5. 输出结果

**e2e 模式输出格式**：

成功（全部通过）：
```
E2E_SUMMARY: all_pass | total: 5, pass: 5, fail: 0, skip: 0
```

失败（有未通过）：
```
E2E_RESULT:
[PASS] 启动 app (python main.py --port 8080)
[PASS] 打开浏览器访问 localhost:8080
[PASS] 点击「开始游戏」按钮
[FAIL] 应能看到至少1个 rival 文明出现在地图上
  → 主界面 DOM 中未发现 rival 相关元素
[SKIP] 点击 rival 图标（依赖前一步）

E2E_SUMMARY: has_failure | total: 5, pass: 3, fail: 1, skip: 1
```

### 5. Stop hook 集成

在 builder-loop-stop.sh 的 PASS 路径中，judge agent 调用之前插入 e2e 分支：

```bash
# --- e2e verification (before judge) ---
E2E_PLAN_PATH=$(从 state 读 e2e_plan_path)
if [ -n "$E2E_PLAN_PATH" ] && [ -f "$E2E_PLAN_PATH" ]; then
    E2E_CASES=$(extract-e2e-cases.sh "$E2E_PLAN_PATH")
    if [ -n "$E2E_CASES" ]; then
        # spawn tester in e2e mode
        # 解析 E2E_SUMMARY
        # has_failure → stderr 注入 E2E_RESULT, exit 2
        # all_pass → 继续走 judge
    fi
fi
```

**e2e FAIL 时的 stderr 消息格式**：

```
[builder-loop] E2E 行为验收未通过。以下用例失败：

[FAIL] 应能看到至少1个 rival 文明出现在地图上
  → 主界面 DOM 中未发现 rival 相关元素

请根据失败用例修改代码，确保 app 运行时行为符合验收标准。
单元测试已全部通过，问题在于功能未在 app 中实际生效。
```

### 6. Planner 验收环节扩展

planner.md 的验收提问轮（Round 7）增加端到端选项：

提问："是否需要端到端行为验证？"
选项：
- "需要"——追问验收步骤（启动方式、操作流程、预期结果），写入 `<!-- e2e-cases -->` 标签
- "不需要"——跳过，按现有流程

### 7. Subagent 注册

- `subagent-start-guard.sh` 白名单加 tester e2e 模式识别
- tester e2e 模式的 lock 文件命名与现有 tester lock 兼容
- `worktree-write-guard.sh` 对 tester e2e 模式的写权限：只允许写临时文件（如 app 启动脚本），不允许写源码/测试

<!-- /role -->

## 风险 & 应对

| 风险 | 影响 | 应对 |
|------|------|------|
| tester spawn 失败（API 错误/模型不可用） | e2e 验证跳过，退化为现有行为 | 降级处理：spawn 失败 → 日志告警 + 跳过 e2e + 继续走 judge。不阻塞 loop |
| app 启动失败/端口占用 | e2e 所有用例 FAIL | tester 报告中明确区分"app 启动失败"和"用例验证失败"，builder 据此处理 |
| tester 执行时间过长（浏览器卡住） | loop 迭代变慢 | tester spawn 时设超时（默认 300s），超时按 FAIL 处理 |
| e2e 用例写得太模糊（"应该正常工作"） | tester 无法判定 PASS/FAIL | 属于 planner 质量问题，不在本方案范围。planner 验收提问时引导写具体可观察的预期 |
| tester 读到源码违反隔离 | 独立性被破坏 | 复用 tester-lock-check.sh 的 source_dirs 拦截机制 |

**退路**：e2e stage 完全由 state 的 `e2e_plan_path` 字段控制。该字段不存在时系统行为与 V3.x 完全一致。最坏情况下删除 e2e 相关代码、清除 state 字段即可回退。

## 文件地图

| 文件 | 改动类型 | 改动点 |
|------|---------|--------|
| `scripts/builder-loop-stop.sh` | 修改 | PASS 路径加 e2e 分支（judge 之前） |
| `scripts/extract-e2e-cases.sh` | **新增** | 从 plan 提取 e2e-cases 标签内容 |
| `agents/tester.md` | 修改 | 新增 e2e 执行模式（输入区分 + 隔离约束 + 输出格式） |
| `skills/builder-loop/SKILL.md` | 修改 | state schema 加 `e2e_plan_path` 字段说明 |
| `scripts/subagent-start-guard.sh` | 修改 | tester e2e 模式白名单 |
| `scripts/worktree-write-guard.sh` | 修改 | tester e2e 模式写权限 |
| `install.sh` | 修改 | extract-e2e-cases.sh 软链注册 |
| `~/.claude/commands/planner.md` | 修改（dotfiles） | 验收提问轮加端到端选项 |
| `~/.claude/commands/builder.md` | 修改（dotfiles） | setup 步骤写 e2e_plan_path 到 state |

<!-- role:tester -->

## 测试计划

### fixture 测试

**fixture 1: extract-e2e-cases 提取**
- 输入：含 `<!-- e2e-cases -->` 标签的 plan 文件 → 输出用例文本
- 输入：无标签的 plan 文件 → exit 1
- 输入：标签为空 → exit 1
- 输入：多个标签段 → 全部提取拼接

**fixture 2: stop hook e2e 分支触发**
- state 有 e2e_plan_path + plan 有标签 → tester 被 spawn
- state 无 e2e_plan_path → 跳过 e2e，走现有 judge 流程
- state 有 e2e_plan_path 但 plan 文件不存在 → 日志告警 + 跳过
- tester spawn 失败 → 降级跳过 + 日志告警

**fixture 3: tester e2e 模式输出解析**
- 全部 PASS → stop hook 继续走 judge
- 有 FAIL → stop hook 注入错误到 stderr, exit 2
- 超时 → 按 FAIL 处理

### divine-word 实际场景验收

在 divine-word 项目上跑完整流程：
1. planner 写带 e2e-cases 的 plan（含 "应看到 rival 文明" 类用例）
2. builder-loop 迭代时 tester 自动做 e2e 验收
3. 验证：rival 模块未实现时 e2e 报 FAIL，实现后报 PASS

<!-- /role -->

## 执行任务列表

### Phase 1: 基础设施（plan 格式 + 提取脚本）

1. 新增 `scripts/extract-e2e-cases.sh`：sed/awk 提取 `<!-- e2e-cases -->` 到 `<!-- /e2e-cases -->` 之间内容
2. `install.sh` 加 extract-e2e-cases.sh 软链注册
3. fixture 覆盖提取脚本（有标签/无标签/空标签/多段）

### Phase 2: Tester 扩展

1. `agents/tester.md` 新增 e2e 执行模式段：输入区分、隔离约束、执行流程、输出格式
2. `scripts/subagent-start-guard.sh` 白名单适配
3. `scripts/worktree-write-guard.sh` tester e2e 写权限适配

### Phase 3: Stop hook 集成（依赖 Phase 1 + 2）

1. `skills/builder-loop/SKILL.md` state schema 加 `e2e_plan_path` 字段
2. `scripts/builder-loop-stop.sh` PASS 路径加 e2e 分支（judge 之前）
3. e2e FAIL → stderr 注入 pass/fail 清单 + exit 2
4. e2e PASS → 继续走 judge
5. 降级处理：spawn 失败 / plan 文件不存在 → 跳过 + 日志

### Phase 4: Planner/Builder 侧（dotfiles）

1. `~/.claude/commands/planner.md` 验收提问轮加端到端选项
2. `~/.claude/commands/builder.md` setup 步骤：检测 plan 有 e2e-cases → 写 e2e_plan_path 到 state

### Phase 5: 验收

1. fixture: stop hook e2e 分支触发/跳过/降级
2. fixture: tester e2e 输出解析
3. divine-word 实际场景测试

## 验收标准

机械化：
- extract-e2e-cases.sh fixture 全 PASS
- stop hook e2e 分支 fixture 全 PASS（触发/跳过/降级三条路径）
- plan 无 e2e-cases 时全套 V3.x 既有 fixture 不退化

人工：
- divine-word 项目上实际跑一次带 e2e-cases 的 plan，tester 能抓住功能未实现
