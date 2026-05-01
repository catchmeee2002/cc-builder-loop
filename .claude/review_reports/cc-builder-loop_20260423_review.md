# 代码审查报告

**时间**：2026-04-23 04:23
**范围**：merge-worktree-back.sh, builder-loop-stop.sh, builder.md
**深度**：深度（架构关键路径）

## 变更概览

本次改动修复 Builder-Loop 三大严重 Bug：
1. **数据丢失**：worktree 未提交改动被 cleanup 删除 → 前置 auto-commit
2. **Reviewer 误报**：读旧代码 → start_head 传递 + 时序强化
3. **MERGED 假阳性**：merge 实际为 no-op → 空合并检测

---

## 详细审查

| 级别 | 位置 | 问题及建议 |
|------|------|-----------|
| 🔵 | merge-worktree-back.sh:130 | 缩进格式：行首多一空格（`WT_STATUS=`），建议删除 |
| 🔵 | merge-worktree-back.sh:132 | auto-commit 中 iter 默认值为 0（行 133）是合理的保守值，但建议日志记录 iter 不存在时的提示 |
| 🟢 | merge-worktree-back.sh:142-147 | 空合并检测逻辑正确：短 hash 对比有效，NOOP 输出防止误报 |
| 🟢 | builder-loop-stop.sh:203 | PASS_START_HEAD 提取时序安全（先读后删），sed 模式与 merge 脚本一致 |
| 🟢 | builder-loop-stop.sh:208-214 | 消息格式清晰，hard rule 警告突出，exit 2 续接机制正确 |
| 🔵 | builder-loop-stop.sh:213 | stderr 消息中 `git diff ${PASS_START_HEAD}..HEAD` 注释建议改为 backtick 围绕命令以提高可读性 |
| 🟢 | builder.md:56 | L3 时序修正明确：「等 loop PASS 后再 spawn reviewer」阻止了误报根本原因 |
| 🟢 | builder.md:72-75 | hard rule 用四段对比（错误示例+正确流程）强度足够，符合规范 |
| 🟢 | builder.md:86-88 | diff 获取两种场景区分清晰，命令格式准确（完整 hash vs short hash 没有混淆） |

---

## 关键风险评估

### 改动 1：auto-commit + 空合并检测（merge-worktree-back.sh）

**安全性**：HIGH
- START_HEAD 为短 hash（setup 时 `--short`），行 142 对比类型一致
- auto-commit 消息格式规范（`chore(loop): auto-commit iter ${ITER_NUM}`）
- cleanup 在对比后才执行，不会误删分支

**遗留项**：
- 若 grep 找不到 iter: 字段，awk 返回空，默认为 0（可接受）
- 应在成功 auto-commit 后日志记录「iter=N auto-commit completed」便于 trace

### 改动 2：PASS_START_HEAD + stderr 消息（builder-loop-stop.sh）

**安全性**：HIGH
- 读取在 rm 前，时序正确
- exit 2 让 LLM 读到 stderr，builder 可从消息中提取 start_head
- 消息中预留了警告：旧 reviewer 结果作废、diff 获取方法

**遗留项**：
- 若 start_head 为空（grep 失败），则 `start_head=` 后为空，builder 需容错
- 建议在消息中加防守：`start_head=${PASS_START_HEAD:-$(git rev-parse --short HEAD)}`

### 改动 3：builder.md 文档修正

**安全性**：HIGH
- 时序约束改为硬规则，阻止 L3 任务在 tester 后立即 spawn reviewer
- hard rule 用 `⛔` 标记 + 错误/正确流程四项对比，足以引起重视
- diff 命令区分完整

**遗留项**：
- 文档依赖于 Stop hook 返回 `start_head=xxx` 格式，若 hook 改动需同步更新文档

---

## 测试覆盖度评估

| 场景 | 现状 | 建议 |
|------|------|------|
| 正常 PASS 流程 | ✅ | E2E 验证 PASS 消息中 start_head 值正确 |
| worktree 无改动（NOOP） | ✅ | 验证短 hash 对比逻辑（测试 auto-commit 前后 HEAD 相等） |
| worktree 有未提交改动 | ⚠️ 缺 | 测试 auto-commit 成功、iter 提取、commit message 格式 |
| rebase 冲突→仲裁 | ✅ | start_head 在仲裁过程中是否正确透传（builder.md 已说明等仲裁后再 reviewer） |
| builder 读 PASS 消息 | ⚠️ 缺 | 测试 builder 正确解析 `start_head=xxx` 并构造 `git diff <start_head>..HEAD` |

---

## 总体结论

**质量**：通过

三个改动形成完整闭环：
- **merge-worktree-back.sh**：代码层防丢失（auto-commit）+ 防假阳性（空合并检测）
- **builder-loop-stop.sh**：数据层传递 start_head 供 reviewer 差分使用
- **builder.md**：规则层强制时序约束，阻止 reviewer 读旧代码

**改进建议**（可选）**：
1. auto-commit 后加日志记录便于 trace.jsonl 排查
2. PASS_START_HEAD 提取失败时添加防守（`${VAR:-fallback}`）
3. 编写 E2E 测试覆盖 worktree 有改动 + auto-commit + NOOP 判断的全流程

---

**代码审查完成**

