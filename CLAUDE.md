# cc-builder-loop — Builder 自闭环迭代

> 把 builder 模式从「单次执行」升级为「机器判定的多轮自闭环」。
> 项目接入只需在项目根放 `.claude/loop.yml`，定义 `pass_cmd`（lint/test 等通过条件），
> builder 改完代码会自动跑 PASS_CMD，失败自动喂回让 builder 再改，直到 PASS 或达到上限。

## 1. 链接映射表

install.sh 创建以下软链，把仓库文件映射到 CC 运行时路径：

| 仓库路径 | 运行时路径 | 链接方式 | 用途 |
|----------|-----------|---------|------|
| `skills/builder-loop/` | `~/.claude/skills/builder-loop/` | `ln -sfn` 整目录 | CC 自动发现 SKILL.md |
| `scripts/builder-loop-stop.sh` | `~/.claude/scripts/builder-loop-stop.sh` | `ln -sf` 逐文件 | Stop hook 入口 |
| `scripts/tester-lock-write.sh` | `~/.claude/scripts/tester-lock-write.sh` | `ln -sf` 逐文件 | SubagentStart hook |
| `scripts/tester-lock-check.sh` | `~/.claude/scripts/tester-lock-check.sh` | `ln -sf` 逐文件 | PreToolUse hook |
| `scripts/tester-lock-clear.sh` | `~/.claude/scripts/tester-lock-clear.sh` | `ln -sf` 逐文件 | SubagentStop hook |
| `scripts/reviewer-timing-check.sh` | `~/.claude/scripts/reviewer-timing-check.sh` | `ln -sf` 逐文件 | PreToolUse hook（Agent） |
| `agents/tester.md` | `~/.claude/agents/tester.md` | `ln -sf` 逐文件 | tester subagent |
| `agents/arbiter.md` | `~/.claude/agents/arbiter.md` | `ln -sf` 逐文件 | 仲裁 subagent |
| *(install.sh)* | `~/.claude/settings.json` hooks 段 | python3 增量合并 | 5 个 hook 条目 |

**注册的 5 个 hook**：

| Hook 类型 | Matcher | 脚本 | 作用 |
|-----------|---------|------|------|
| Stop | 无（全局） | builder-loop-stop.sh | 每次 CC Stop 时检查是否需要继续循环 |
| SubagentStart | `tester` | tester-lock-write.sh | tester 启动时落隔离锁 |
| SubagentStop | `tester` | tester-lock-clear.sh | tester 结束时清锁 |
| PreToolUse | `Read\|Grep\|Glob` | tester-lock-check.sh | 拦截 tester 对 source_dirs 的读操作 |
| PreToolUse | `Agent` | reviewer-timing-check.sh | 拦截 loop 活跃期的 reviewer spawn |

## 2. 部署指南

```bash
# 安装（幂等，可重复跑）
cd /mnt/hongyu.liao_docker/cc-builder-loop
./install.sh

# 卸载
./uninstall.sh

# 验证
ls -la ~/.claude/skills/builder-loop/SKILL.md  # 应指向本仓库
```

**前置依赖**：
- `~/.claude/` 目录已存在（通常由 dotfiles 的 `stow claude` 创建）
- python3（hook 注册用）
- jq（可选，install.sh 实际用 python3 做 JSON 操作）

**新机器部署顺序**：先 `my-dotfiles/install.sh`（stow 创建 `~/.claude/`），后 `cc-builder-loop/install.sh`。

## 3. 与 dotfiles 的依赖关系

本仓库是**自包含**的，但运行时依赖 dotfiles 中的以下共享文件：

| dotfiles 文件 | 本仓库依赖方式 |
|---------------|---------------|
| `~/.claude/commands/builder.md` | builder 模式定义，含 loop.yml 检测 / setup 调用 / tester 触发等 loop 逻辑 |
| `~/.claude/commands/planner.md` | planner 模式定义，含 3 视图区块约定 |
| `~/.claude/agents/reviewer.md` | reviewer 定义，含 TESTER_HINT 输出格式 |

**路径约定**：所有脚本引用都通过 `~/.claude/` 前缀的运行时路径（如 `~/.claude/skills/builder-loop/scripts/xxx.sh`），不直接引用仓库路径。

## 4. 目录结构

```
cc-builder-loop/
├── install.sh / uninstall.sh   # 部署/卸载
├── CLAUDE.md                   # 本文件
├── skills/builder-loop/        # CC skill（含 SKILL.md、scripts/、fixtures/e2e/、schema/）
├── scripts/                    # Stop hook + tester 隔离 hook + reviewer 时序 hook（5 个 .sh）
└── agents/                     # tester.md + arbiter.md
```

## 5. 已交付能力（V1.0~V1.8）

- 多阶段 PASS_CMD + 智能早停
- tester 强隔离（hook 锁机制）
- 方案文件三视图过滤（builder/tester/shared）
- worktree 真隔离 + 三档合回
- rebase 冲突仲裁（arbiter subagent）
- reviewer → tester 触发
- 改动分级（L1 跳过 / L2 正常 / L3 先 tester）
- 任务回顾与知识沉淀
- Stop hook 兜底激活（loop.yml 存在 + 有改动 + 无状态文件 → 自动启动 loop）
- **V1.5**: Stop hook 续接修复（exit 2 + stderr，取代无效的 JSON stdout）
- **V1.5**: Worktree 前置（builder 进入后先 setup 再写代码，避免代码丢失）
- **V1.5**: NDJSON trace（`.claude/loop-trace.jsonl`，每轮记录 iter/stage/result/duration）
- **V1.5**: 一键 init（`loop-init.sh` 整合 probe + init-loop-config + git init）
- **V1.5**: E2E 测试（全新仓库端到端验证）
- **V1.6**: Worktree auto-commit（merge 前自动提交未 commit 改动，防数据丢失）
- **V1.6**: Reviewer 时序硬门禁（PreToolUse hook 拦截 loop 活跃期的 reviewer spawn）
- **V1.6**: Reviewer 参数预计算（stop hook PASS 后写 reviewer-params.json，消除 LLM diff 计算依赖）
- **V1.7**: Reviewer 默认模型 sonnet（兼容 max / copilot 双路径，消除 haiku+xhigh 失败场景）+ Builder retry 错误分类（`effort/reasoning/not supported` 等 API 参数错误直接走兜底，不再盲重试）
- **V1.7**: E2E 新增 `test-reviewer-compat.sh`（配置 lint + 可选 `--live` smoke）
- **V1.8**: 多状态并行（state 文件从 `.claude/builder-loop.local.md` 迁移到 `.claude/builder-loop/state/<slug>.yml`；locate-state.sh 按 CWD 定位；单项目可并行多个 loop；migrate-state.sh 一键迁移旧版本）
- **V1.8.1**: 僵尸 state 自愈 + EARLY_STOP 立即通知
  - Stop hook 遇到 `active != true` 的 state → 归档到 `.claude/builder-loop/legacy/<ts>-zombie_inactive.bak` 后放行（原行为是保留僵尸，下次 builder 进场会误判为活跃 loop）
  - EARLY_STOP 路径从"改 active=false + exit 0"改为"归档 + exit 2 + stderr 注入"，builder 当场收到通知立即 AskUserQuestion（原行为需等到下轮 user prompt 才发现）
  - 配合 V1.8 的 per-worktree state 隔离，彻底闭环"同 session 多任务僵尸串味"问题（复现 session `81bdbe27`）
- **V1.8.2**: 兜底激活 HEAD 游标
  - Stop hook bootstrap 分支新增「已处理 HEAD 游标」（`.claude/builder-loop/last_processed_head`）：PASS / 异常 merge / EARLY_STOP 三处出口写入当前 HEAD，下次 Stop 时若 HEAD 未前进且无未提交改动则静默放行
  - 消除"推完 commit 后 30 分钟内每次对话反复触发 NOOP 空转 bootstrap"的自激循环（复现 session `3d62eb57`）
  - 降级保证：游标文件缺失/损坏/HEAD 读不到都自动退回旧行为

详见 `skills/builder-loop/README.md`。

## 6. 开发原则

- **不改 CC 源码**：所有功能基于 CC 的 hook / skill / agent 扩展机制实现
- **可破坏性升级**：升级允许不兼容已接入项目的 loop.yml，但必须手动更新所有已接入项目确保继续可用

## 7. 已知问题 / 排查手册

### 7.1 Stop hook 未触发测试 — 僵尸 state 文件 bug（2026-04-24 定位，V1.8.1 修复）

**现象**：builder 回复"✅ loop 已活跃"，但 Stop hook 没跑测试，session 直接停下（复现 session `81bdbe27`）。

**根因**：Stop hook 每次都正确触发，但读到 `active=false` 的僵尸 state（来自前一个任务手动编辑或早停遗留）后正确地放行了——这是设计行为。真正的问题是**僵尸 state 本身的存在**：同一 CC session 里连续做多个任务时，builder 可能跳过前置 setup，看到旧状态文件就假设"已活跃"，结果消费的是无效的僵尸。

**修复**（V1.8.1）：Stop hook 现在遇到 `active != true` 的 state 从"放行保留"改为"归档到 `legacy/<ts>-zombie_inactive.bak` + 放行"，下次 builder 进场无法再读到僵尸；同时 EARLY_STOP 从"改字段 + exit 0"改为"归档 + exit 2 注入"，让 builder 当场收到通知。

**排查步骤**：
1. 用 `ls -la <project_root>/.claude/builder-loop/` 查看是否有 `state/` 或 `legacy/` 目录
2. 若有 `legacy/*.bak`，用 `tail -20 /tmp/builder-loop-stop-debug.log` 查 hook 退出原因
3. 若日志显示 `active=false` 或 `stopped_reason` 存在，证实是僵尸 → V1.8.1 修复已生效
4. 若日志显示其他退出原因（如 `no_project_root`），按日志的 `EXIT_REASON` 字段诊断

### 7.2 Commit-msg hook 拦截导致 auto-commit 失败

**现象**：loop 跑到 merge-worktree-back.sh 时失败，错误信息含 "commit message" 或 "cr_id_skip"。

**根因**：当项目启用了严格的 commit-msg hook（如 guard-commit-msg.sh）时，auto-commit message 的格式必须合规。V1.8 的 message `"chore(loop): auto-commit iter N"` 不含必要的 `[cr_id_skip]` 标记，被 hook 拦截。

**修复**（V1.8.1）：merge-worktree-back.sh 的 auto-commit message 改为 `"chore(loop): [cr_id_skip] Auto-commit iter N"`，兼容所有启用 msg hook 的项目。

**排查步骤**：检查 merge-worktree-back.sh 第 138 行的 commit message，应含 `[cr_id_skip]` 标记。
