# V4.0: Reviewer 吸收 Judge — plan 完成度检查 + 判据层统一

<!-- role:shared -->

## 背景 & 目标

Builder PASS 后声称 plan 完成但实际文件清单完成率仅 33%（divine-word 项目第二次发生同一 failure mode）。根因分析（meta-think 3 生成器 + 4 判别器 + CoVe/指差呼称研究）得出：

1. Judge 当前"没有锚点"——只看 diff 判完成度，跟 builder 自证没本质区别
2. Judge 和 reviewer 在独立性层级上完全相同（都是独立 agent），维持两个是同层级冗余
3. 研究确认：LLM 同 context 自检无效，但能纠正以外部输入形式呈现的错误

**目标**：Reviewer 吸收 judge 全部职责，成为唯一的独立 agent 判据层。Reviewer 新增 Phase 0（plan 完成度检查 + early exit），接受 plan 文件输入作为判据锚点。Judge 作为独立组件废弃。

**成功标准**：
- Reviewer Phase 0 能在"plan 列了 15 个文件操作但只完成 5 个"的场景下输出 🔴 打回 builder
- 现有 reward hacking 正则检测能力无损保留（下沉到 stop hook 机械层）
- FAIL 分支 retry_transient 检测能力保留
- 所有已接入项目无需改 loop.yml 即可继续工作

## 预估改动级别

L3（新接口/模块）— reviewer 新增 Phase 0 接口 + plan_path 输入协议 + plan-checklist 标签规范。

## 约束 & 边界

- 不能碰：loop.yml 格式、pass_cmd 执行逻辑、phase 状态机（active/passed_pending_review）
- 必须兼容：已有 state 文件（含 e2e_plan_path 字段的老 state）、judge 未配置的项目
- reward hacking Layer 2 正则必须保留，从 judge 移到 stop hook
- FAIL 分支 retry_transient 保留在 stop hook（不进 reviewer）

<!-- /role -->

<!-- role:builder -->

## 技术选型

| 方案 | 核心动作 | 排除理由 |
|------|---------|---------|
| **Judge 升级（给 judge 加 plan 输入）** | 单次 API call + plan checklist | judge 无工具链（不能 Read 文件验证），能力天花板低；与 reviewer 同层冗余 |
| **✅ Reviewer 吸收 judge** | reviewer 加 Phase 0 + early exit | 复用已有工具链 + 独立 agent，Phase 0 不过直接打回省时间 |
| **新增独立 plan checker agent** | 专门验 plan 完成度 | 又多一个同层 agent，复杂度反增 |

## 方案设计

### 流程对比

**当前（V3.x）**：
```
PASS_CMD → e2e(V3.8) → judge → phase=passed_pending_review → reviewer → merge
```

**V4.0 后**：
```
PASS_CMD → reward_hacking_regex(机械层) → e2e(V3.8) → phase=passed_pending_review(含 plan_path) → reviewer(Phase 0 + Phase 1) → merge
```

### Reviewer 新增 Phase 0 设计

Reviewer 审查流程变为两阶段，Phase 0 不过直接返回（early exit）：

- **Phase 0: Plan 完成度检查**
  - 输入：plan_path 指向的 plan 文件中 `<!-- plan-checklist -->` 标签内容（执行任务列表 + 文件地图）
  - 动作：逐步骤语义判断"这个步骤的意图是否在代码中体现"（Read 文件 + grep 验证）
  - 不过：输出 🔴 findings 列出未完成步骤 → 结束审查 → builder 回去实现
  - 过：进入 Phase 1

- **Phase 1: 代码质量审查**（原 reviewer 职责，不变）

### 自然 loop 机制（无需重构状态机）

1. Reviewer Phase 0 报 🔴 → builder 回去写代码
2. Builder 改了 worktree → stop hook 触发 → L1 检测到 worktree 改动 → phase 自愈回 active
3. PASS_CMD 重跑 → 过了 → phase=passed_pending_review → reviewer 重跑
4. Reviewer Phase 0 再检查 → 过了 → Phase 1 代码审查

### State 字段变更

- **新增** `plan_path`: 通用 plan 文件路径（setup 时写入）
- **废弃** `e2e_plan_path`: 改为 plan_path 的别名（stop hook 读时 fallback）
- **废弃** judge 相关 6 字段: last_judge_action, last_judge_confidence, last_judge_ts, consecutive_nudge_count, judge_active_model, judge_consecutive_failures

### Reward hacking Layer 2 正则下沉

从 run-judge-agent.sh 提取正则检测逻辑，内联到 stop hook PASS 分支（e2e 之前）：
- 文件匹配: loop.yml / pyproject.toml / pytest.ini / setup.cfg / conftest.py / tests*/**/*.py
- 内容匹配: --reruns / @pytest.mark.flaky / xfail / pytest.skip / @unittest.skip / -k "not X"
- 命中: exit 2 + `[builder-loop reward-hack-guard]` 段强制 AskUserQuestion 三选项

### FAIL 分支 retry_transient 保留

从 run-judge-agent.sh 提取 retry_transient 检测逻辑，简化为：
- 读 pass_cmd 错误日志，grep 瞬态关键词（API truncation / timeout / connection reset）
- 命中: 重跑 pass_cmd（不喂回 builder）
- 不命中: 正常 FAIL 流程（extract-error → 喂回 builder）

## 风险 & 应对

| 风险 | 影响 | 应对 |
|------|------|------|
| Reviewer 成本每轮增加 | 每次 loop PASS 都跑 reviewer（~200s） | Phase 0 early exit 降本；plan 不完整时 ~30s 就返回，比 judge+reviewer 更快 |
| 老 state 无 plan_path 字段 | 已接入项目老 state 文件缺该字段 | stop hook 读 plan_path 时 fallback 到 e2e_plan_path；两者都无则跳过 Phase 0 |
| Judge telemetry 丢失 | judge-trace.jsonl 不再生成 | reviewer report 已有分级 findings 覆盖同一信息；Phase 0 findings 纳入 reviewer report |
| Reviewer Phase 0 判断不准 | 语义判断可能漏判或误判 | Reviewer 有完整工具链（Read + grep），比 judge 单次 API call 准得多 |

## 文件地图

| 文件 | 改动 |
|------|------|
| `commands/planner.md` | 方案产物规范加 `<!-- plan-checklist -->` 标签要求 |
| `commands/builder.md` | setup 写 plan_path（替代 e2e_plan_path）；删 judge 相关处理；reviewer 🔴 处理不变 |
| `agents/reviewer.md` | 加 Phase 0（plan 完成度检查 + early exit）；accept plan_path 输入 |
| `scripts/builder-loop-stop.sh` | PASS 分支: 删 judge 调用(L576-670)，加 reward hacking 正则(从 judge L2 提取)；e2e 块改读 plan_path(L531)；reviewer-params 加 plan_path。FAIL 分支: 精简 retry_transient 为机械检测(L922-967) |
| `scripts/extract-e2e-cases.sh` | 不改脚本本身；调用方从 e2e_plan_path 改为 plan_path |
| `skills/builder-loop/SKILL.md` | state schema: 加 plan_path，标注 e2e_plan_path 和 judge 字段废弃 |
| `skills/builder-loop/docs/judge-agent.md` | 标注废弃，指向 reviewer Phase 0 |
| `skills/builder-loop/scripts/run-judge-agent.sh` | 标注废弃（保留文件，不删） |
| `skills/builder-loop/prompts/judge-system.md` | 标注废弃 |
| `CLAUDE.md` | 更新文档导航（judge-agent.md 标废弃）；更新 V3.0 关键事实段（删 judge 描述） |
| `CHANGELOG.md` | 加 V4.0 段 |

<!-- /role -->

<!-- role:shared -->

## 执行任务列表

<!-- plan-checklist -->

### Phase 1: Plan 规范 + State Schema（前置，后续 Phase 消费）

1. **改 `commands/planner.md`**：方案产物规范加 `<!-- plan-checklist -->` 标签说明 — 包裹"执行任务列表"和"文件地图"两段，用于 reviewer Phase 0 完成度检查
2. **改 `skills/builder-loop/SKILL.md`**：state schema 加 `plan_path` 字段说明；`e2e_plan_path` 和 6 个 judge 字段标注 `(V4.0 废弃)` 
3. **改 `commands/builder.md`**：setup 步骤里 e2e plan 注册改为 plan_path 注册（检测 `<!-- plan-checklist -->` 或 `<!-- e2e-cases -->` 任一存在即写 plan_path）

### Phase 2: Reviewer 扩展（消费 Phase 1 的 plan_path 协议）

4. **改 `agents/reviewer.md`**：加 Phase 0 plan 完成度检查。输入接受 plan_path。Phase 0 流程：读 plan-checklist 内容 → 逐步骤 Read/grep 验证 → 不过则 🔴 + early exit。过了进 Phase 1（原审查流程不变）
5. **改 `scripts/reviewer-timing-check.sh`**（如需）：确认 Phase 0 场景下 hook 行为无异常

### Phase 3: Stop Hook 重构（消费 Phase 1-2 的 reviewer 新能力）

6. **改 `scripts/builder-loop-stop.sh` PASS 分支**：
   - 删除 judge 调用块（约 L576-670 的 ~90 行）
   - 在 e2e 块之前加 reward hacking Layer 2 正则检测（从 run-judge-agent.sh 提取）
   - e2e 块读 plan_path（fallback e2e_plan_path）
   - reviewer-params.json 加 plan_path 字段
7. **改 `scripts/builder-loop-stop.sh` FAIL 分支**：
   - 精简 retry_transient 为机械关键词检测（不调 run-judge-agent.sh）
   - 删除 FAIL 分支 judge 调用（约 L922-967）
8. **改 `scripts/builder-loop-stop.sh` outcome 补标**：
   - 删除 judge trace backfill 块（约 L278-335）

### Phase 4: 文档 + 清理

9. **改 `CLAUDE.md`**：更新文档导航（judge-agent.md 标废弃）；更新 V3.0 关键事实段
10. **改 `CHANGELOG.md`**：加 V4.0 段
11. **标注废弃**：`skills/builder-loop/scripts/run-judge-agent.sh`、`skills/builder-loop/prompts/judge-system.md`、`skills/builder-loop/docs/judge-agent.md` 文件头部加废弃说明

### Phase 5: Fixture + 验收

12. **新建 fixture** `test-reviewer-plan-completion.sh`：模拟 reviewer Phase 0 行为（plan 有 5 步、只完成 2 步 → reviewer 报 🔴 列出未完成步骤）
13. **改造已有 judge fixture**：`test-judge-agent.sh` 等 5 个 fixture → 验证 stop hook 在无 judge 时的行为（PASS 直接到 reviewer-params、FAIL retry_transient 机械检测）
14. **新建 fixture** `test-reward-hacking-regex.sh`：验证 stop hook 内联的 reward hacking 正则检测（从 judge Layer 2 迁移后行为一致）
15. **实战验证**：divine-word 项目跑一轮完整 loop（含 plan-checklist），确认 Phase 0 能抓到遗漏

<!-- /plan-checklist -->

## 验收标准

1. divine-word 项目用含 `<!-- plan-checklist -->` 的 plan 跑 loop，故意少做几个文件 → reviewer Phase 0 报 🔴 打回
2. reward hacking 场景（改 conftest.py 加 xfail）→ stop hook 正则拦截（不依赖 judge）
3. FAIL + API truncation → retry_transient 机械检测生效
4. 老 state 文件（只有 e2e_plan_path 无 plan_path）→ fallback 正常工作
5. 无 plan 文件的项目 → Phase 0 跳过，Phase 1 正常审查

<!-- /role -->

<!-- role:tester -->

## 测试计划

**测试目标**：验证 reviewer 吸收 judge 后的行为正确性

**关键测试场景**：
- Reviewer Phase 0 在 plan 未完成时输出 🔴 并 early exit（不进 Phase 1）
- Reviewer Phase 0 在 plan 完成时放行进 Phase 1
- Stop hook PASS 分支无 judge 调用、reward hacking 正则生效
- Stop hook FAIL 分支 retry_transient 机械检测生效
- plan_path / e2e_plan_path fallback 链正确
- 无 plan_path 时 reviewer 跳过 Phase 0 直接 Phase 1

**测试深度**：深度 — 每个改动点至少一个 fixture 断言覆盖

<!-- /role -->
