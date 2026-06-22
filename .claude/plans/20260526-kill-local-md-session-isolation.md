# 干掉 local.md — CWD→state 直接匹配实现 session 并发隔离

<!-- role:shared -->

## 背景 & 目标

`builder-loop.local.md` 是项目级单例文件（一个项目只有一份），V3.2 引入作为 session→slug 的快速指针。但多 session 同项目并发时，后来的 session 会覆写 local.md 指向自己的 slug，且 builder 前置检查把别人的 active loop 误认为残留执行 abandon，导致误杀。

**目标**：彻底移除 `builder-loop.local.md`，stop hook / builder.md / fixture 全部改用 `locate-state.sh` 的 CWD→state 匹配。worktree 模式下 N 个 session 完全并发互不干扰（唯一交叉 = merge 冲突走 arbiter）。bare 模式不要求并发，但不被 worktree loop 干扰。

**成功标准**：
1. 两个 session 同时 setup + 跑 loop，各自只操作自己的 state
2. 原有 fixture 全部 PASS（无回归）
3. 新 fixture 覆盖并发隔离场景
4. 已接入项目残留的 local.md 不影响新版运行（静默忽略）

## 预估改动级别

L2 实现改动 — 不改对外接口（SKILL.md schema 不加新字段），改 stop hook 内部定位逻辑 + 删 setup 写入 + 改 builder.md prompt + 适配 ~12 个 fixture。

## 约束 & 边界

- `locate-state.sh` 的 5 级匹配策略已经能处理所有场景，**不需要新写定位逻辑**
- bare 模式不支持并发（CWD 都是主仓根，无法区分 session）
- 不依赖 CC 暴露新环境变量（`session_id` 在 hook stdin 有但此方案不依赖）
- 不改 `loop.yml` 格式（客户配置文件保持稳定）
- 不改 state.yml schema（不加新字段）
- **跨仓依赖**：`~/.claude/commands/builder.md`（dotfiles）需同步改，走 sync-checklist

<!-- /role -->

<!-- role:builder -->

## 技术选型

### 方案 A（已选）：干掉 local.md，stop hook 调 locate-state.sh

- stop hook L154-186 的 local.md 读取块 → 替换为 `STATE_FILE="$(bash "$SKILL_DIR/locate-state.sh" "$CWD")"`
- setup L423-429 的 local.md 写入块 → 删除
- builder.md 读 local.md 的所有引用 → 改为调 locate-state.sh 或直接读 state.yml
- 符合设计哲学「每份数据只有一个家」

### 方案 B（否决）：local.md 按 slug 拆分

- `local-<slug>.md` 多文件，各 session 各写各的
- ❌ 但 local 和 state 两份数据重复存 slug/worktree_path/state_file，违反「一份数据一个家」
- ❌ 两份可能漂移（某方改了另一方没跟）

## 方案设计

### 核心变更

**1. stop hook 定位逻辑**（`builder-loop-stop.sh` L154-186）

替换前（V3.2 local.md 读取）：
```
从 CWD 向上找 .claude/builder-loop.local.md → 读 slug → 拼 state 路径
```

替换后：
```
STATE_FILE="$(bash "$SKILL_DIR/locate-state.sh" "$CWD" 2>/dev/null)"
if [ -z "$STATE_FILE" ] || [ ! -f "$STATE_FILE" ]; then
  exit 0  # 找不到 state = 当前 CWD 无 active loop = 放行
fi
PROJECT_ROOT=（从 STATE_FILE 路径反推，或从 state 读 main_repo_path）
```

locate-state.sh 的 5 级策略已覆盖所有场景：
- 策略 2：CWD 在 `.claude/worktrees/<slug>/` 下 → 直接推 slug（worktree 模式主路径）
- 策略 3：遍历 state/*.yml 比对 worktree_path（worktree 模式 fallback）
- 策略 4：兜底 `__main__.yml`（bare 模式）
- 策略 5：恰好 1 个 active state 时自动绑定

**2. setup 不再写 local.md**（`setup-builder-loop.sh` L423-429）

删除整个 `cat > "$LOCAL_MD" <<LOCALEOF ... LOCALEOF` 块。setup 的输出（stdout/stderr）已包含 slug + worktree_path，builder.md 直接从输出读取。

**3. builder.md 前置检查改写**（dotfiles 跨仓）

当前读 local.md 做决策的 4 处引用全部替换：

| 当前 | 替换为 |
|------|--------|
| Read local.md 拿 `plan_file` | `bash locate-state.sh` 拿 state → Read state.yml 拿 `plan_file` |
| Read local.md 拿 `worktree_path` 并 cd | setup stdout 已输出 worktree_path，直接从输出读 |
| 检查 local.md 存在且 active=true | `bash locate-state.sh "$(pwd)"` 有输出 = 已 setup |
| abandon-loop 传 state_file | 从 locate-state.sh 返回的路径传入 |

**4. 旧 local.md 兼容**

- stop hook / setup / builder.md 不再读写 local.md
- 残留的 local.md 文件静默忽略（不报错不删除）
- 不做自动迁移（local.md 内容全在 state.yml 里有，无信息丢失）

### 匹配优先级（locate-state.sh 已实现）

```
CWD 在 .claude/worktrees/<slug>/ 下?
  → YES: 直接拼 state/<slug>.yml                    # worktree 模式，精确
  → NO:  遍历 state/*.yml 找 worktree_path 匹配 CWD  # worktree 模式，fallback
         → 无匹配: 试 __main__.yml                    # bare 模式
         → 仍无: 恰好 1 个 active → 自动绑定           # 兜底
         → 否则: exit 1（未找到）
```

## 风险 & 应对

| 风险 | 概率 | 应对 |
|------|------|------|
| locate-state.sh 比 local.md 慢（扫目录 vs 读单文件） | 低 — state 文件通常 <5 个 | 可接受延迟；如需优化可缓存 locate 结果到 /tmp |
| LLM cd 出 worktree 后 stop hook CWD 回到主仓 | 低 — builder.md 指令约束在 worktree 内工作 | locate-state.sh 策略 5 兜底（唯一 active state 自动绑定） |
| builder.md prompt 从"读一个文件"变成"跑一个脚本"，LLM 执行变数增大 | 中 | prompt 给出精确的 bash 命令，不让 LLM 自行构造 |
| dotfiles builder.md 改动未同步 | 中 | sync-checklist 条目 + install.sh 不涉及（只改 prompt 文字） |

## 文件地图

### 本仓（cc-builder-loop）

| 文件 | 改动 |
|------|------|
| `scripts/builder-loop-stop.sh` L154-186 | **重写**：local.md 读取块 → locate-state.sh 调用 |
| `skills/builder-loop/scripts/setup-builder-loop.sh` L423-429 | **删除**：local.md 写入块 |
| `skills/builder-loop/SKILL.md` | **更新**：「Session 指针 (V3.2)」段改写 |
| `skills/builder-loop/fixtures/e2e/harness.sh` L99, L161 | **删除**：local.md 从 .gitignore 列表 + setup_loop_env helper 中移除 |
| `skills/builder-loop/fixtures/e2e/test-stop-hook-slug-binding.sh` L24 | **删除**：local.md 创建 |
| `skills/builder-loop/fixtures/e2e/test-old-state-compat.sh` L46 | **删除**：local.md 创建 |
| `skills/builder-loop/fixtures/e2e/test-judge-edge-cases.sh` L180 | **删除**：local.md 创建 |
| `skills/builder-loop/fixtures/e2e/test-judge-integration.sh` L212 | **删除**：local.md 创建 |
| `skills/builder-loop/fixtures/e2e/test-pass-cmd-runs-worktree.sh` L138 | **删除**：local.md 创建 |
| `skills/builder-loop/fixtures/e2e/test-multi-worktree-feedback.sh` L84, L114 | **删除**：local.md 创建（2 处） |
| `skills/builder-loop/fixtures/e2e/test-zombie-selfheal.sh` L44 | **删除**：local.md 创建 |
| `skills/builder-loop/fixtures/e2e/test-cross-session-isolation.sh` | **重写**：移除 local.md 依赖，改用 CWD 匹配验证 |
| `skills/builder-loop/fixtures/e2e/test-stop-hook-debug-log.sh` L77, L133, L159 | **删除**：local.md 创建（3 处） |
| **新建** `fixtures/e2e/test-concurrent-session-isolation.sh` | 并发隔离 fixture |
| `improvements.md` | 关闭高优条目 |
| `CHANGELOG.md` | V3.4 条目 |

### 跨仓（dotfiles — sync-checklist）

| 文件 | 改动 |
|------|------|
| `~/.claude/commands/builder.md` | **改写**：前置 loop 检查段 + plan_file 读取 + abandon 调用 |

## 执行任务列表

1. **stop hook 定位逻辑重写** — `builder-loop-stop.sh` L154-186 替换为 locate-state.sh 调用，保留 PROJECT_ROOT / RUN_CWD 赋值语义
2. **setup 删除 local.md 写入** — `setup-builder-loop.sh` L423-429 整块删除
3. **harness.sh 适配** — 移除 local.md 从 .gitignore 列表和 setup_loop_env helper
4. **10 个现有 fixture 适配** — 逐个删除 local.md 创建块，确保 fixture 通过 locate-state.sh 的 CWD 匹配工作（state + worktree 目录结构已足够）
5. **重写 test-cross-session-isolation.sh** — 移除 local.md 依赖，验证两个 worktree 各自 stop hook 只操作自己 state
6. **新建 test-concurrent-session-isolation.sh** — 模拟 N=2 并发 setup + 并发 stop hook 场景
7. **SKILL.md 文档更新** — 「Session 指针 (V3.2)」段改写为「CWD→state 匹配 (V3.4)」
8. **builder.md 同步改动** — 前置 loop 检查 + plan_file 读取（走 sync-checklist）
9. **improvements.md 关闭条目 + CHANGELOG.md V3.4**

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标

验证移除 local.md 后：(1) 单 session 全生命周期不回归；(2) 多 session 并发隔离正确；(3) 旧 local.md 残留不干扰。

### 关键测试场景

**A. 并发隔离（核心新增）**
- A1: 两个 worktree 同时 active，各自 stop hook 只操作自己的 state
- A2: Session A active 时 Session B setup 不影响 A 的 state
- A3: Session A PASS + passed_pending_review 时 Session B 的 stop hook 不误触 A 的 reviewer 流程
- A4: 一个 worktree + 一个 bare 并存，bare 的 stop hook 不误绑 worktree 的 state

**B. 单 session 不回归**
- B1: worktree 模式完整生命周期（setup → FAIL → iter++ → PASS → reviewer_pending → merge-and-cleanup）
- B2: bare 模式完整生命周期
- B3: 早停（no_progress / max_iter）后 state 正确归档
- B4: abandon-loop 正常工作

**C. 兼容性**
- C1: 项目里残留旧 local.md 文件时，stop hook / setup 不报错不读它
- C2: locate-state.sh 在无 state 目录时 exit 1（新项目首次）

**D. 边界条件**
- D1: LLM cd 出 worktree 回到主仓后 stop hook 触发 — 策略 5 兜底
- D2: state 目录有多个 active state 时，CWD 在其中一个 worktree → 精确匹配（不误绑另一个）

### 测试深度

深度测试 — 新建 fixture 覆盖 A1-A4 + D1-D2，现有 fixture 全部跑通不回归。

<!-- /role -->

## 验收标准

1. `bash skills/builder-loop/fixtures/e2e/run-all.sh` 全 PASS
2. 新 fixture `test-concurrent-session-isolation.sh` 覆盖 A1-A4 场景，≥20 assertions
3. 项目根和所有已接入项目无 `builder-loop.local.md` 的读写（grep 验证）
4. builder.md 前置检查不再引用 local.md（grep 验证）
