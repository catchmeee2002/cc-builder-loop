# cc-builder-loop — Builder 自闭环迭代

> 把 builder 模式从「单次执行」升级为「独立判据驱动的多轮自闭环」。
> 项目接入只需在项目根放 `.claude/loop.yml`，定义 `pass_cmd`（lint/test 等通过条件），
> builder 改完代码会自动跑 PASS_CMD，失败自动喂回让 builder 再改，直到 PASS 或达到上限。

> **与 CC 官方自动化能力的边界**：本仓库 = 独立判据驱动的**纵向收敛闭环**（PASS_CMD 客观判据 + 独立 agent 行为验证 + worktree 隔离 + reward hacking 防御）；官方 `/loop` = **通用步频再触发器**、dynamic workflow = **横向 fan-out 编排层**，二者判停均 LLM 主观。维度正交，**不要叠用**（尤其 builder-loop active 时勿在同会话开 inline workflow）。跟踪与互斥防御见 [`skills/builder-loop/docs/cc-loop-tracking.md`](skills/builder-loop/docs/cc-loop-tracking.md)。

---

## 设计哲学

本项目是 LLM 驱动的发散系统，所有设计决策的判据是一组原则，**唯一来源**见 [`docs/design-philosophy.md`](docs/design-philosophy.md)。核心：**判据按独立性分层**——纯机器判据（人定义+机器执行）做 ground truth 地基、独立 agent 判据（独立定义+独立执行）做行为验证层、同会话 LLM 判据补语义层；判据可信度的关键属性是独立性，不是「机器 vs LLM」。

> 原则正文只在 design-philosophy.md 维护，此处与 README 一律不复制全文（落实原则二「每份数据一个家」+ 原则七 dogfooding）。

---

## 文档导航

| 文档 | 定位 | 何时读 |
|------|------|--------|
| [`docs/design-philosophy.md`](docs/design-philosophy.md) | 设计哲学（判据分层等原则，SSOT 唯一来源） | 做设计决策 / 评估方案时 |
| [`CHANGELOG.md`](CHANGELOG.md) | 各版本交付能力（V1.0~V4.0） | 需要了解历史版本做了什么时 |
| [`docs/troubleshooting.md`](skills/builder-loop/docs/troubleshooting.md) | 排查手册（§7.1~7.12） | stop hook / worktree / state 出问题时 |
| [`docs/sync-checklist.md`](skills/builder-loop/docs/sync-checklist.md) | 改动同步 checklist | 本仓 commit 后需同步操作时 |
| [`docs/judge-agent.md`](skills/builder-loop/docs/judge-agent.md) | ~~Judge agent~~（V4.0 废弃，已被 reviewer Phase 0 吸收） | 仅供历史参考 |
| [`docs/arbiter-flow.md`](skills/builder-loop/docs/arbiter-flow.md) | Rebase 冲突仲裁流程 | merge 冲突时 |
| [`docs/cc-loop-tracking.md`](skills/builder-loop/docs/cc-loop-tracking.md) | CC 官方自动化能力跟踪（/loop + dynamic workflow） | 评估官方能力 / 互斥防御时 |
| [`skills/builder-loop/README.md`](skills/builder-loop/README.md) | SKILL 使用说明 | 了解用户侧接入流程时 |

---

## V3.0 reviewer-as-gate 关键事实

**V3.0 行为**：hook PASS 后**只 commit 不 merge**，等 reviewer 通过才合主线（详见 [CHANGELOG V3.0](CHANGELOG.md#v30-reviewer-as-gate-重构2026-05-09)）。V4.1 起 bare 模式行为对齐 worktree（统一 `loop-commit.sh` + reviewer-as-gate）。

**关键 state 字段**：`phase`（active / e2e_pending / passed_pending_review）+ `last_iter_head` + `reviewer_pending` 段 + `subagents` 段（V4.3 agent_id 追踪）+ `cleanup_phase`。详见 SKILL.md 「状态文件 schema」段。

**V5.4 执行模型变更**：PASS_CMD 主触发器从 Stop hook 改为 builder 显式调用（`run-pass-cmd.sh` + `handle-pass-result.sh`）。Stop hook 保留为 safety net——如果 fire，L1 闸看 phase=passed_pending_review → exit 0，不 double run。详见 [CHANGELOG V5.4](CHANGELOG.md)。

**Hook 闸顺序**（PASS_CMD 之前命中即静默 exit 0，V5.4 起 Stop hook 为 safety net）：
- L1 `phase=passed_pending_review|e2e_pending` → exit 0 + stderr 诊断（passed_pending_review: dirty/新 commit 自愈回 active；e2e_pending: 仅新 commit/verified_head 自愈，dirty 不自愈防 tester 写文件触发无限循环）
- L2A 末尾 pending AskUserQuestion → exit 0 + stderr 诊断
- L2B HEAD == last_iter_head + git status 空 → exit 0 + stderr 诊断
- L3 `.claude/builder-loop/<slug>.pause` 存在 → exit 0 + stderr 诊断

**[技术债] active 字段下掉计划**：V3.0 起 hook 主判用 phase 字段，`active: true` 仅写不读做新决策。下掉计划见 [`improvements.md`](improvements.md) 「active 字段下掉计划」候选条目；时间窗 V3.x 某版本统一 grep 全仓引用清单后移除。**禁止**在新代码里读 active 字段做决策。

---

## 1. 链接映射表

install.sh 创建以下软链，把仓库文件映射到 CC 运行时路径：

| 仓库路径 | 运行时路径 | 链接方式 | 用途 |
|----------|-----------|---------|------|
| `skills/builder-loop/` | `~/.claude/skills/builder-loop/` | `ln -sfn` 整目录 | CC 自动发现 SKILL.md |
| `scripts/builder-loop-stop.sh` | `~/.claude/scripts/builder-loop-stop.sh` | `ln -sf` 逐文件 | Stop hook 入口 |
| `scripts/reviewer-timing-check.sh` | `~/.claude/scripts/reviewer-timing-check.sh` | `ln -sf` 逐文件 | PreToolUse hook（Agent） |
| `agents/tester.md` | `~/.claude/agents/tester.md` | `ln -sf` 逐文件 | tester subagent |
| `agents/arbiter.md` | `~/.claude/agents/arbiter.md` | `ln -sf` 逐文件 | 仲裁 subagent |
| *(install.sh)* | `~/.claude/settings.json` hooks 段 | python3 幂等覆盖（V5.3 起无条件删旧+写新） | 2 个 hook 条目 |

**注册的 hook（方案差异）**：

| Hook 类型 | Matcher | 脚本 | 作用 | 方案 |
|-----------|---------|------|------|------|
| Stop | 无（全局） | builder-loop-stop.sh | 每次 CC Stop 时检查是否需要继续循环 | 全部 |
| PreToolUse | `Agent` | reviewer-timing-check.sh | 拦截 phase=active 期间的 reviewer spawn；phase=passed_pending_review 时放行 | 全部 |

**方案说明**：install.sh 在运行时读 `ANTHROPIC_BASE_URL` env 识别方案 — localhost/127.0.0.1 → copilot；其他 → max。当前仅 2 个 hook（builder-loop-stop.sh + reviewer-timing-check.sh）在两种方案下均注册。**认身份隔离 hook 已整体退役**——`subagent_type` 在真实环境读不到、隔离退地基，详见 [CHANGELOG 范式变更节](CHANGELOG.md)。

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
├── CHANGELOG.md                # 版本历史（V1.0~V3.7）
├── skills/builder-loop/        # CC skill（含 SKILL.md、scripts/、fixtures/e2e/、schema/、docs/）
├── scripts/                    # Stop hook + reviewer 时序检查 + e2e case 提取（3 个 .sh）
└── agents/                     # tester.md + arbiter.md
```

## 5. 开发原则

- **不改 CC 源码**：所有功能基于 CC 的 hook / skill / agent 扩展机制实现
- **可破坏性升级**：升级允许不兼容已接入项目的 loop.yml，但必须手动更新所有已接入项目确保继续可用
- **[HARD RULE] Prompt 只写"做什么"**：写 builder.md / SKILL.md / agent prompt / commands/*.md 时只下达 imperative 指令（操作步骤、判据、出口、约束），禁止写动机/原因/反向出题/"防偷懒"等心理说辞。设计思路写到代码注释或 `docs/`，不进 prompt
- **文档新鲜度不自证**：检查文档（CLAUDE.md / plan.md / SKILL.md 等）是否需要更新时，禁止 builder 跳过验证直接声称"没问题"。机械层负责把文档存在性不可跳过地呈现（如 diff-level-check 输出），LLM 层负责语义判断（哪段过期）——两层各司其职，不混
