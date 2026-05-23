# cc-builder-loop — Builder 自闭环迭代

> 把 builder 模式从「单次执行」升级为「机器判定的多轮自闭环」。
> 项目接入只需在项目根放 `.claude/loop.yml`，定义 `pass_cmd`（lint/test 等通过条件），
> builder 改完代码会自动跑 PASS_CMD，失败自动喂回让 builder 再改，直到 PASS 或达到上限。

> **与 CC 官方 `/loop` skill 的边界**：本仓库 = 机器判据驱动的**代码迭代闭环**（PASS_CMD 客观判据 + worktree 隔离 + reward hacking 防御）；官方 `/loop`（v2.1.121+）= `ScheduleWakeup` 驱动的**通用步频再触发器**（LLM 主观判停）。判据维度不同，**不要叠用**。版本跟踪见 [`skills/builder-loop/docs/cc-loop-tracking.md`](skills/builder-loop/docs/cc-loop-tracking.md)。

---

## 设计哲学

本项目是 LLM 驱动的发散系统——Claude Code 的行为不确定，中间件层互相耦合，打补丁会滚雪球。以下 6 条原则是所有设计决策的判据，详见 [`docs/design-philosophy.md`](docs/design-philosophy.md)。

1. **机器判据，不是 LLM 意见**——状态转移只靠客观可验证的信号，不靠 LLM 的主观判断
2. **每份数据有且只有一个家**——同一个事实不存两个地方，冗余必漂移
3. **显式授权，默认拒绝**——不认识的东西不碰，宁可多一步显式声明
4. **改输入条件，不改输出约束**——改数据结构让正确行为自然发生，不在末端堆特判
5. **同一个问题出现三次就是架构缺陷**——找共同根因一次修掉，不单点修
6. **契约先于实现**——先定义输入/输出/副作用契约，测试验证契约不验证实现

---

## 文档导航

| 文档 | 定位 | 何时读 |
|------|------|--------|
| [`docs/design-philosophy.md`](docs/design-philosophy.md) | 设计哲学（6 条原则） | 做设计决策 / 评估方案时 |
| [`CHANGELOG.md`](CHANGELOG.md) | 各版本交付能力（V1.0~V3.2） | 需要了解历史版本做了什么时 |
| [`docs/troubleshooting.md`](skills/builder-loop/docs/troubleshooting.md) | 排查手册（§7.1~7.12） | stop hook / judge / worktree / state 出问题时 |
| [`docs/sync-checklist.md`](skills/builder-loop/docs/sync-checklist.md) | 改动同步 checklist | 本仓 commit 后需同步操作时 |
| [`docs/judge-agent.md`](skills/builder-loop/docs/judge-agent.md) | Judge agent 设计与配置 | judge 相关开发 / 排查时 |
| [`docs/arbiter-flow.md`](skills/builder-loop/docs/arbiter-flow.md) | Rebase 冲突仲裁流程 | merge 冲突时 |
| [`docs/cc-loop-tracking.md`](skills/builder-loop/docs/cc-loop-tracking.md) | CC 官方 /loop 版本跟踪 | 评估官方能力是否可替代时 |
| [`skills/builder-loop/README.md`](skills/builder-loop/README.md) | SKILL 使用说明 | 了解用户侧接入流程时 |

---

## V3.0 reviewer-as-gate 关键事实

**V3.0 行为**：worktree 模式下 hook PASS 后**只 commit 不 merge**，等 reviewer 通过才合主线（详见 [CHANGELOG V3.0](CHANGELOG.md#v30-reviewer-as-gate-重构2026-05-09)）。bare 模式行为不变。

**关键 state 字段**：`phase`（active / passed_pending_review）+ `last_iter_head` + `reviewer_pending` 段 + `cleanup_phase`。详见 SKILL.md 「状态文件 schema」段。

**Hook 闸顺序**（PASS_CMD 之前命中即静默 exit 0）：
- L1 `phase=passed_pending_review` → 静默（worktree 改动时自愈回 active）
- L2A 末尾 pending AskUserQuestion → 静默
- L2B HEAD == last_iter_head + git status 空 → 静默
- L3 `.claude/builder-loop/<slug>.pause` 存在 → 静默

**[技术债] active 字段下掉计划**：V3.0 起 hook 主判用 phase 字段，`active: true` 仅写不读做新决策。下掉计划见 [`.claude/improvements.md`](.claude/improvements.md) 「active 字段下掉计划」候选条目；时间窗 V3.x 某版本统一 grep 全仓引用清单后移除。**禁止**在新代码里读 active 字段做决策。

---

## 1. 链接映射表

install.sh 创建以下软链，把仓库文件映射到 CC 运行时路径：

| 仓库路径 | 运行时路径 | 链接方式 | 用途 |
|----------|-----------|---------|------|
| `skills/builder-loop/` | `~/.claude/skills/builder-loop/` | `ln -sfn` 整目录 | CC 自动发现 SKILL.md |
| `scripts/builder-loop-stop.sh` | `~/.claude/scripts/builder-loop-stop.sh` | `ln -sf` 逐文件 | Stop hook 入口 |
| `scripts/subagent-start-guard.sh` | `~/.claude/scripts/subagent-start-guard.sh` | `ln -sf` 逐文件 | SubagentStart hook |
| `scripts/tester-lock-check.sh` | `~/.claude/scripts/tester-lock-check.sh` | `ln -sf` 逐文件 | PreToolUse hook |
| `scripts/tester-lock-clear.sh` | `~/.claude/scripts/tester-lock-clear.sh` | `ln -sf` 逐文件 | SubagentStop hook |
| `scripts/worktree-write-guard.sh` | `~/.claude/scripts/worktree-write-guard.sh` | `ln -sf` 逐文件 | PreToolUse hook（Write\|Edit\|MultiEdit）|
| `scripts/reviewer-timing-check.sh` | `~/.claude/scripts/reviewer-timing-check.sh` | `ln -sf` 逐文件 | PreToolUse hook（Agent） |
| `agents/tester.md` | `~/.claude/agents/tester.md` | `ln -sf` 逐文件 | tester subagent |
| `agents/arbiter.md` | `~/.claude/agents/arbiter.md` | `ln -sf` 逐文件 | 仲裁 subagent |
| *(install.sh)* | `~/.claude/settings.json` hooks 段 | python3 增量合并 | 5-6 个 hook 条目（取决于方案） |

**注册的 hook（方案差异）**：

| Hook 类型 | Matcher | 脚本 | 作用 | 方案 |
|-----------|---------|------|------|------|
| Stop | 无（全局） | builder-loop-stop.sh | 每次 CC Stop 时检查是否需要继续循环 | 全部 |
| SubagentStart | 无（全局） | subagent-start-guard.sh | 所有 subagent 启动时落锁 + 注入 worktree 边界上下文 | 全部 |
| SubagentStop | 无（全局） | tester-lock-clear.sh | subagent 结束时清锁 | 全部 |
| PreToolUse | `Read\|Grep\|Glob` | tester-lock-check.sh | 拦截 tester 对 source_dirs 的读操作 | 全部 |
| PreToolUse | `Write\|Edit\|MultiEdit` | worktree-write-guard.sh | 分级写路径防护：subagent 严格白名单 / builder 宽松放行+日志 | 全部 |
| PreToolUse | `Agent` | reviewer-timing-check.sh | 拦截 phase=active 期间的 reviewer spawn；phase=passed_pending_review 时放行 | 全部 |

**方案说明**：install.sh 在运行时读 `ANTHROPIC_BASE_URL` env 识别方案 — localhost/127.0.0.1 → copilot；其他 → max。V3.1 起所有 6 个 hook 在两种方案下均注册（worktree-write-guard.sh 取代 copilot 专属的 tester-write-guard.sh）。

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
- 建议设置 `ANTHROPIC_BASE_URL` env（决定 install.sh 选择的方案）

**新机器部署顺序**：先 `my-dotfiles/install.sh`（stow 创建 `~/.claude/`），后 `cc-builder-loop/install.sh`。

## 3. 与 dotfiles 的依赖关系

本仓库是**自包含**的，但运行时依赖 dotfiles 中的以下共享文件：

| dotfiles 文件 | 本仓库依赖方式 |
|---------------|---------------|
| `~/.claude/commands/builder.md` | builder 模式定义，含 loop.yml 检测 / setup 调用 / tester 触发等 loop 逻辑 |
| `~/.claude/commands/planner.md` | planner 模式定义，含 3 视图区块约定 |
| `~/.claude/agents/reviewer.md` | reviewer 定义，含 TESTER_HINT 输出格式 |

**路径约定**：所有脚本引用都通过 `~/.claude/` 前缀的运行时路径，不直接引用仓库路径。

**改动同步**：见 [`docs/sync-checklist.md`](skills/builder-loop/docs/sync-checklist.md)。

## 4. 目录结构

```
cc-builder-loop/
├── install.sh / uninstall.sh   # 部署/卸载
├── CLAUDE.md                   # 本文件
├── CHANGELOG.md                # 版本历史（V1.0~V2.6）
├── skills/builder-loop/        # CC skill（含 SKILL.md、scripts/、fixtures/e2e/、schema/、docs/）
├── scripts/                    # Stop hook + subagent 启动守卫 + tester 读隔离 + tester 锁清理 + worktree 写边界守卫 + reviewer 时序检查（6 个 .sh）
└── agents/                     # tester.md + arbiter.md
```

## 5. 开发原则

- **不改 CC 源码**：所有功能基于 CC 的 hook / skill / agent 扩展机制实现
- **可破坏性升级**：升级允许不兼容已接入项目的 loop.yml，但必须手动更新所有已接入项目确保继续可用
- **[HARD RULE] Prompt 只写"做什么"**：写 builder.md / SKILL.md / agent prompt / commands/*.md 时只下达 imperative 指令（操作步骤、判据、出口、约束），禁止写动机/原因/反向出题/"防偷懒"等心理说辞。设计思路写到代码注释或 `docs/`，不进 prompt
