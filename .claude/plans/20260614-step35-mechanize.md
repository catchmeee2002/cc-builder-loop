# V3.5-B: Step 3.5 机械化检测 + doc-lint 误判修复

<!-- role:shared -->

## 背景 & 目标

消化 improvements.md 中 ≥6 条同根因：step 3.5 doc 评估漏触发（4 次：6-10/5-28/5-24/5-11）+ doc-lint 签名变更误判（6-11）+ doc-lint 黑名单漏词（6-03）。

两个子问题：
1. **Step 3.5 漏触发**：builder.md 的 doc 评估输出格式不强制逐类交代，做完一类就滑过另一类
2. **doc-lint.sh 误判**：只看 diff `-` 行提取 DELETED_SYMBOLS，不对照 `+` 行，签名变更被当删除；通用词黑名单缺 append/clear

**成功标准**：
- step 3.5 输出必须包含 A 类和 B 类两行状态（缺任何一行 = 违规）
- doc-lint 对「签名变更但函数未删」的场景不误报
- 现有 test-doc-lint.sh fixture 无回归 + 新增 case 覆盖签名变更

## 验收标准

1. doc-lint fixture 全 PASS（含新增 case：签名变更不误判、append/clear 过滤）
2. builder.md step 3.5 段改为结构化两行输出格式
3. 下次真实 builder 任务 dogfood 验证 step 3.5 两行并列输出

<!-- /role -->

<!-- role:builder -->

## 预估改动级别

L2（实现改动）——prompt 逻辑改 + 脚本 bug 修复，无新接口。

## 约束 & 边界

- builder.md 在 `~/.claude/commands/builder.md`（dotfiles 仓库），本次直接 Edit
- doc-lint.sh 在 `skills/builder-loop/scripts/doc-lint.sh`（cc-builder-loop 仓库）
- 不改 doc-lint 的整体架构（仍是 DELETED_FILES + DELETED_SYMBOLS → 扫 .md）
- 黑名单补词不改现有排除逻辑（只加词）

## 技术选型

| 方案 | 描述 | 结论 |
|------|------|------|
| **纯 prompt 结构化输出** | 改 builder.md step 3.5 为强制两行并列格式（A 类 / B 类各一行，必须显式写「命中」或「未命中(理由)」） | ✅ 采纳。同模式（步骤 5 四档并列）已证明有效 |
| 检测脚本 doc-eval-check.sh | 从 diff 自动提取 A/B 类信号，输出 JSON | ❌ 排除。开发量大，且 A/B 类判定需要语义理解（如"行为变更"），纯 diff 扫难覆盖 |

## 方案设计

### 子问题 1：Step 3.5 结构化输出

改 `~/.claude/commands/builder.md` step 3.5 段，输出格式从自由文本改为：

```
📄 doc-A: 命中 → spawn doc-maintainer
  └ <命中了哪条 checklist + 简述>
📄 doc-B: 未命中（无版本条目/TODO/设计文档变更）
```

规则：两行都必须写。缺任何一行 = 违规（同步骤 5「不许跳过」模式）。

### 子问题 2：doc-lint 双向过滤

`doc-lint.sh` 第 37-49 行提取 DELETED_SYMBOLS 后，新增：
1. 同样从 `+` 行提取 ADDED_SYMBOLS（同 pattern：def/class/function）
2. 过滤掉 DELETED_SYMBOLS ∩ ADDED_SYMBOLS（签名变更而非真正删除）

### 子问题 3：黑名单补词

`doc-lint.sh` 第 91 行 case 语句加 `append|clear`。

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| 双向过滤误把真删除的同名新函数排除 | 极低概率——同 commit 内删 foo() 又加 foo() 的场景几乎不存在；即使发生，doc-lint 失灵不阻塞（只是漏报不误报） |
| prompt 改后 builder 仍然跳过 | 步骤 5 同模式运行 3 周无复现，概率低；万一复现下一步上脚本检测 |

## 文件地图

| 文件 | 操作 | 改动点 |
|------|------|--------|
| `~/.claude/commands/builder.md` | 改 | step 3.5 输出格式改为两行并列 |
| `skills/builder-loop/scripts/doc-lint.sh` | 改 | 第 37-49 行后加 ADDED_SYMBOLS 提取 + 交集过滤；第 91 行 case 加 append\|clear |
| `skills/builder-loop/fixtures/e2e/test-doc-lint.sh` | 改 | 新增 Case 6（签名变更不误判）+ Case 7（append/clear 过滤） |
| `CLAUDE.md` | 不改 | step 3.5 无结构性变更，不需更新映射表 |

## 执行任务列表

### Phase 1：doc-lint 修复
1. 改 `doc-lint.sh`：DELETED_SYMBOLS 提取后，同样从 `+` 行提取 ADDED_SYMBOLS，过滤交集
2. 改 `doc-lint.sh`：黑名单 case 语句加 `append|clear`
3. 跑现有 test-doc-lint.sh 无回归

### Phase 2：doc-lint fixture
4. `test-doc-lint.sh` 加 Case 6：函数签名变更（def foo(a) → def foo(a, b)），doc 引用 foo → exit 0（不误判）
5. `test-doc-lint.sh` 加 Case 7：删 def append(...)，doc 引用 append → exit 0（黑名单过滤）

### Phase 3：builder.md prompt 改造
6. Edit `~/.claude/commands/builder.md` step 3.5 段：
   - 输出格式改为两行并列（A 类 + B 类各一行）
   - 每行必须写「命中」或「未命中(理由)」
   - 加规则：「两行都必须写。缺任何一行 = 违规」

### Phase 4：验证
7. 全量 test-doc-lint.sh PASS
8. 下次 builder 任务 dogfood step 3.5

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标
验证 doc-lint 双向过滤正确工作，不引入回归。

### 关键测试场景

1. **Case 6：签名变更不误判**
   - 初始：`def send_card(msg):` + doc 引用 `send_card`
   - 改动：`def send_card(msg, timeout=30):` （签名变了但函数还在）
   - 断言：exit 0（不误报）

2. **Case 7：append/clear 黑名单**
   - 初始：`def append(data):` + `def clear():`
   - 改动：删掉两个函数
   - doc 引用 `append`、`clear`
   - 断言：exit 0（黑名单过滤掉通用词）

3. **回归：现有 Case 1-5 不变**

### 测试深度
fixture 覆盖 doc-lint，prompt 改动靠 dogfood。

<!-- /role -->
