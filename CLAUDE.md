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

**注册的 4 个 hook**：

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
├── scripts/                    # Stop hook + tester 隔离 hook + reviewer 时序 hook（6 个 .sh）
└── agents/                     # tester.md + arbiter.md
```

## 5. 已交付能力（V1.0~V1.6）

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

详见 `skills/builder-loop/README.md`。

## 6. 开发原则

- **不改 CC 源码**：所有功能基于 CC 的 hook / skill / agent 扩展机制实现
- **可破坏性升级**：升级允许不兼容已接入项目的 loop.yml，但必须手动更新所有已接入项目确保继续可用
