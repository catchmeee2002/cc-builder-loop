# 跨越界污染系统性修复 — V3.2 边界隔离

## 背景 & 目标

worktree 模式下反复出现三类越界污染事故：
1. **CWD 身份绑定失败**：stop hook / subagent 靠 CWD 猜归属，串台操作别的 session 的 loop（6 条 improvements）
2. **Dirty 边界不隔离**：setup 把主仓所有 dirty 一锅端进 worktree，跨任务文件被 commit 进 loop（3 条）
3. **Stop hook 作用域失控**：兜底激活在暂停项目空跑 NOOP 死循环 / abandon 后仍推 commit（3 条）

成功标准：
- stop hook 只操作 local.md 声明的 slug，找不到就 exit 0 放行
- setup 默认干净 worktree，不带主仓 dirty
- merge-and-cleanup 拒绝合并有未提交改动的 worktree
- 现有 fixture 全绿 + 新增 fixture 覆盖三类场景

## 预估改动级别

**L2 实现改动**。签名不变（setup/merge-and-cleanup/locate-state 对外调用方式不变），内部逻辑改。setup 新增 `--touched-files` 参数属于 additive，不破坏现有调用。

## 约束 & 边界

- 不改 CC 源码，只用 hook/skill/agent 扩展机制
- bare 模式（无 worktree）的 loop 本方案不覆盖（逐步淘汰中）
- `builder-loop.local.md` 是项目级单文件，多 session 同项目并发时 last-setup-wins（用户确认此场景罕见，可接受）
- dotfiles 侧 `builder.md` 需同步改（跨仓依赖，见文件地图）

## 技术选型

| 方案 | 描述 | 结论 |
|------|------|------|
| CWD 猜测（现状） | locate-state 5 层策略按 CWD 匹配 state | ❌ 排除 — 多 session 串台、subagent CWD 永远是主仓 |
| worktree CWD 严格匹配 | hook 只在 CWD 落在 worktree 目录时操作 | ❌ 排除 — CC stop hook CWD 永远是主仓，worktree 模式下永远不匹配 |
| **local.md slug 精确绑定** | setup 写 slug 到 local.md，hook 读 slug 精确找 state | ✅ 选定 — 不依赖 CWD，不改 CC 协议，自然消除兜底激活 |

<!-- role:shared -->

## 方案设计

### 改动 1：stop hook slug 精确绑定（治 CWD 串台 + 兜底激活死循环）

**现状**：stop hook 调 locate-state.sh，5 层策略按 CWD 猜。策略 5 是"唯一 active worktree 自动绑定"，主仓 CWD 兜底。兜底激活在无 state 时自动创建并启动 loop。

**改为**：
1. stop hook 读 `{CWD}/.claude/builder-loop.local.md` 的 `slug` 字段
2. 用 slug 直接拼 state 路径：`{CWD}/.claude/builder-loop/state/{slug}.yml`
3. state 存在且 phase=active 或 passed_pending_review → 正常流程
4. state 不存在 / local.md 不存在 / slug 为空 → exit 0 放行
5. **删除兜底激活整段逻辑**（V3.0 要求 builder 先 setup 再写代码，不依赖 hook 自动启动）
6. locate-state.sh 保留策略 1-4（路径精确匹配仍有用），**删除策略 5**（主仓 CWD 猜测）

### 改动 2：setup 默认干净 worktree（治跨任务 dirty 污染）

**现状**：setup 检测主仓 dirty → stash push → worktree add → stash apply。所有 dirty 无差别带入。

**改为**：
1. **V3.0 正常路径**（builder.md 前置 loop 检查调用）：默认行为改为 `--no-stash`，worktree 从 HEAD 干净创建
2. 新增 `--touched-files file1,file2,...` 参数：只 stash 指定文件 → 只 apply 指定文件到 worktree
3. 无 `--touched-files` 且主仓有 dirty → stderr 提示"主仓有 N 个未提交文件，不带入 worktree。如需带入请传 --touched-files"
4. 保留 `--no-stash` flag（显式 skip，兼容）
5. state 里 `worktree_mode` 字段：无 dirty → "clean"，有 --touched-files → "selective"，原全量 dirty → 废弃不再写

### 改动 3：merge-and-cleanup worktree dirty 前检查（治未提交改动丢失）

**现状**：merge-and-cleanup.sh 阶段 1 直接跑 ff merge，worktree 有未提交改动时 merge 是 no-op（"Already up to date"），然后阶段 2 `worktree remove --force` 把未提交改动物理删除。

**改为**：
1. 阶段 1 开头，ff merge 之前，检查 worktree `git -C $WORKTREE_PATH status --porcelain`
2. 非空 → `echo "ERROR worktree-has-uncommitted-changes"` + exit 3
3. stderr 输出具体文件列表 + 提示"请先在 worktree 内 commit 或 abandon"
4. worktree 和 state 都保留不动

### 改动 4：builder.md prompt 更新（治 subagent 写错仓 + 声明 --touched-files）

**dotfiles 侧**（`~/.claude/commands/builder.md`）：

1. **前置 loop 检查段**：setup 调用去掉 stash（默认行为已改）。补一段：如果 builder 已在主仓编辑了文件再接入 loop，传 `--touched-files <已改文件列表>`
2. **步骤 3.5 spawn doc-maintainer 段**：必传 `worktree_path`（跟 tester 一致）。当前 tester 已有此字段，doc-maintainer 缺失
3. **步骤 2 spawn reviewer 段**：reviewer prompt 必含 worktree_path，让 reviewer 的 git diff 命令在正确路径下执行

<!-- /role -->

## 风险 & 应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 去掉兜底激活后，builder 忘记 setup → loop 不启动 | loop 不自动跑，需用户手动 | builder.md 已有"前置 loop 检查"硬要求；setup 未执行时 local.md 不存在，hook 静默放行，不会报错误信息 |
| local.md 被多 session 覆写 | last-setup-wins，前一个 session 的 hook 读到新 slug | 用户确认多 session 同项目并发罕见；真撞时 hook 找不到旧 slug 的 state → exit 0 放行（安全失败） |
| 老项目的 state 文件没有 local.md | hook 找不到 local.md → exit 0 → loop 不推进 | 老 state 需手动重新 setup；或 hook 兜底读 state 目录唯一文件（简易迁移） |
| --touched-files 传漏/传错 | 该带的文件没带进 worktree | stderr 提示"主仓有 dirty 未带入"让 builder 察觉；传错文件只是 worktree 多了不必要的文件，不会丢数据 |

## 文件地图

| 文件路径 | 改动 |
|----------|------|
| `skills/builder-loop/scripts/setup-builder-loop.sh` | 默认 --no-stash + 新增 --touched-files 参数 |
| `skills/builder-loop/scripts/merge-and-cleanup.sh` | 阶段 1 前加 worktree dirty check |
| `skills/builder-loop/scripts/locate-state.sh` | 删除策略 5 |
| `scripts/builder-loop-stop.sh` | 入口改为读 local.md slug 精确匹配 + 删除兜底激活段 |
| `~/.claude/commands/builder.md`（dotfiles） | 前置 loop 检查段 + 步骤 3.5 doc-maintainer worktree_path + 步骤 2 reviewer worktree_path |
| `skills/builder-loop/fixtures/e2e/test-dirty-stash-flow.sh` | 适配：原 dirty-stash 测试改为验证 --no-stash 默认行为 + --touched-files 选择性带入 |
| `skills/builder-loop/fixtures/e2e/test-merge-and-cleanup-dirty-abort.sh` | **新增**：worktree dirty 时 merge-and-cleanup abort + 保留现场 |
| `skills/builder-loop/fixtures/e2e/test-stop-hook-slug-binding.sh` | **新增**：有 local.md → 只操作对应 slug；无 local.md → exit 0 放行；错 slug → exit 0 |
| `skills/builder-loop/fixtures/e2e/test-no-fallback-activation.sh` | **新增**：有 loop.yml + dirty 但无 local.md → hook exit 0（不自动启动 loop） |

<!-- role:tester -->

## 测试计划

### 测试目标
验证三类越界污染场景被修复，且正常 loop 流程不回归。

### 关键测试场景

**A. stop hook slug 绑定**
- A1: local.md 存在且 slug 匹配 active state → hook 正常操作（EC=2）
- A2: local.md 不存在 → hook exit 0 放行
- A3: local.md slug 指向不存在的 state → hook exit 0 放行
- A4: local.md slug 指向 abandoned state（非 active）→ hook exit 0 放行

**B. setup 干净 worktree**
- B1: 主仓有 dirty + 默认调用（无 --touched-files）→ worktree 干净，dirty 留在主仓
- B2: 主仓有 dirty + --touched-files 指定 2 个文件 → worktree 只含这 2 个文件的改动
- B3: 主仓无 dirty → worktree 干净（行为不变）

**C. merge-and-cleanup dirty abort**
- C1: worktree 有未提交改动 → ERROR worktree-has-uncommitted-changes (exit 3) + worktree/state 保留
- C2: worktree 干净 → 正常 MERGED (exit 0)

**D. 兜底激活已移除**
- D1: 有 loop.yml + 有 dirty + 无 local.md + 无 state → hook exit 0（不自动启动）

### 测试深度
快速：每个场景一条 fixture 即可，跑通断言。

<!-- /role -->

## 执行任务列表

1. **merge-and-cleanup.sh 加 worktree dirty check**（最小改动，独立于其他任务）
   - 阶段 1 开头、BRANCH 取值之后加 `git status --porcelain` 检查
   - 非空 → stderr 列文件 + stdout ERROR + exit 3
   - 新增 fixture `test-merge-and-cleanup-dirty-abort.sh`

2. **setup-builder-loop.sh 改默认行为**
   - 默认 stash 行为反转：无 flag 时等价于 --no-stash（不 stash 不 apply）
   - 新增 `--touched-files file1,file2,...` 参数：只 stash + apply 这些文件
   - 主仓有 dirty 时 stderr 提示（不 exit，只提醒）
   - 适配 fixture `test-dirty-stash-flow.sh`

3. **stop hook + locate-state 改 slug 精确绑定**
   - stop hook 入口：先读 `{CWD}/.claude/builder-loop.local.md` 的 slug
   - 有 slug → 直接拼 state 路径，跳过 locate-state
   - 无 slug → exit 0（替代原兜底激活）
   - locate-state.sh 删除策略 5
   - 删除 stop hook 兜底激活整段
   - 新增 fixture `test-stop-hook-slug-binding.sh` + `test-no-fallback-activation.sh`

4. **builder.md prompt 更新**（dotfiles 侧）
   - 前置 loop 检查段：声明 setup 默认不带 dirty + --touched-files 用法
   - 步骤 3.5：doc-maintainer spawn 必传 worktree_path
   - 步骤 2：reviewer spawn 带 worktree_path

5. **全量回归**：跑所有现有 fixture 确认不回归

## 验收标准

- 新增 3-4 个 fixture 全绿
- 现有 30+ 个 fixture 全绿（特别关注 test-dirty-stash-flow / test-locate-state-strategy5 / test-cross-session-isolation）
- builder.md diff 覆盖 setup / reviewer / doc-maintainer 三处
