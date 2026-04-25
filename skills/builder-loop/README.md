# builder-loop skill — 开发与维护文档

> **给后来 CC / 维护者的提示**：本文件解释这个 skill 的目录布局、为什么放在 my-dotfiles 仓里、如何修改、如何验证。如果你接手 builder-loop 维护或扩展，先看这份文档再动手。

## 1. 这是什么

`builder-loop` 是一个 Claude Code skill，把 builder 模式从「单次执行」升级为「机器判定的多轮自闭环」：builder 改完代码 → 跑 `loop.yml.pass_cmd`（lint/type/test）→ 失败把错误喂回让 builder 再改 → 直到 PASS 或达到上限。

详细方案（背景、决策、风险、V1~V3 演进、TBD 项）见：

| 文档 | 路径 | 用途 |
|------|------|------|
| **完整方案** | `/mnt/hongyu.liao_docker/.claude/plans/20260418-builder-auto-loop.md` | Planner 模式产出，含 12 章决策溯源、四重防护、调度器演进路径 |
| SKILL.md | 本目录 `SKILL.md` | 运行时关键约定 + 接入向导流程 |
| README.md | 本文件 | 维护开发指南 |

> ⚠️ 方案文件物理位于 hongyu_docker 项目下（产出它的项目根），**不在 my-dotfiles 仓内**（hongyu_docker 不是这个 dotfiles 仓的子项目）。如果该路径找不到，直接以 SKILL.md 作为运行时唯一真实来源。

## 2. 为什么和 my-dotfiles 个人快照仓库在一起

**简短答案**：`~/.claude/` 整目录已经被 my-dotfiles 通过 GNU stow 管理了，新增 skill 自然落在同一仓库里，不用单开仓库增加运维。

**完整背景**：
- my-dotfiles 是用户的个人 dotfiles 仓库（GitHub: `catchmeee2002/my-dotfiles`），用 stow 把 `claude/.claude/` 目录树软链到 `~/.claude/`
- builder-loop 是「全局 CC 配置增强」，物理位置在 `~/my-dotfiles/claude/.claude/skills/builder-loop/`，运行时位置在 `~/.claude/skills/builder-loop/`（install.sh 同步）
- 这种共置带来的收益：跨机器迁移免重新部署、版本回滚靠 git、commit 走 dotfiles 的 cr_id_skip 门禁、脱敏 hook 自动扫描

**和 my-dotfiles 的边界**：
- builder-loop 全部代码 + 文档放在 `~/my-dotfiles/claude/.claude/skills/builder-loop/` 内
- 所有逻辑必须**自包含**：不依赖 my-dotfiles 其他文件，未来抽出去做开源 skill 不会断
- 唯一外部依赖：`~/.claude/scripts/builder-loop-stop.sh`（Stop hook 入口，因 CC 要求 hook 在 scripts 目录）和 `~/.claude/settings.json`（hook 注册）—— 这两处也都在 dotfiles 仓里

## 3. 目录布局

```
~/my-dotfiles/claude/.claude/skills/builder-loop/
├── SKILL.md                    # CC 加载入口，运行时关键约定
├── README.md                   # ← 本文件，开发维护说明
├── scripts/
│   ├── setup-builder-loop.sh   # 启动循环：读配置 + 进 worktree + 建状态文件
│   ├── probe-project-stack.sh  # 接入向导：探测语言栈/测试框架/lint/layout，输出 JSON
│   ├── init-loop-config.sh     # 接入向导：写 loop.yml + 追加 .gitignore（纯 bash，可独立调用）
│   ├── run-pass-cmd.sh         # 按阶段跑 PASS_CMD，日志落 .claude/loop-runs/
│   ├── extract-error.sh        # 错误反馈处理器（V1=full+脱敏）
│   ├── early-stop-check.sh     # 早停判据（无进展/反增长/保护路径）
│   ├── split-plan-by-role.sh   # 方案文件按 <!-- role:xxx --> 区块过滤
│   ├── merge-worktree-back.sh  # V1.1 worktree 合回主干（fast-forward/rebase/仲裁标记）
│   └── run-apply-arbitration.sh # V1.1 仲裁 patch 应用（解析 arbiter 输出/apply/retry merge）
└── schema/
    └── loop.schema.yml         # 项目层 .claude/loop.yml 字段规范
```

伴生改动（不在 skill 目录内但同属本特性）：
- `~/my-dotfiles/claude/.claude/scripts/builder-loop-stop.sh` — Stop hook 入口
- `~/my-dotfiles/claude/.claude/agents/tester.md` — tester subagent 定义
- `~/my-dotfiles/claude/.claude/commands/builder.md` — 在原步骤前插入循环分支判断
- `~/my-dotfiles/claude/.claude/agents/arbiter.md` — V1.1 仲裁 subagent（解 rebase 冲突）
- `~/my-dotfiles/claude/.claude/agents/reviewer.md` — V1.1 末尾输出 TESTER_HINT JSON 块
- `~/my-dotfiles/claude/.claude/commands/planner.md` — 方案模板增加 3 视图区块说明
- `~/my-dotfiles/claude/.claude/settings.json` — 注册 Stop hook
- `~/my-dotfiles/claude/.claude/scripts/README.md` — 追加 hook 文档条目

## 4. 项目层接入约定

业务项目接入 builder-loop 只需在项目根加两个文件：

```
<项目根>/.claude/
├── loop.yml          # 必须，定义 pass_cmd / max_iterations / layout 等
└── loop-runs/        # 自动生成（首次运行时），存每轮完整日志和 metrics.jsonl
                      # 建议加入项目 .gitignore
```

运行时还会出现 `<项目根>/.claude/builder-loop/state/*.yml`（每 loop 一份状态文件，多状态并行模式），建议把 `builder-loop/` 整个目录加入 .gitignore。

`loop.yml` 字段以 `schema/loop.schema.yml` 为准，最小示例：

```yaml
pass_cmd:
  - { stage: test, cmd: "pytest -x", timeout: 300 }
```

## 5. 修改与验证流程

```
1. 改 ~/my-dotfiles/claude/.claude/skills/builder-loop/ 下的源文件
2. cd ~/my-dotfiles && ./install.sh  # 幂等，把改动同步到 ~/.claude/
3. 在某个接入了 loop.yml 的项目里跑一次 builder，看自闭环行为
4. 看日志 <项目根>/.claude/loop-runs/iter-N-*.log 和 metrics.jsonl
5. commit 进 my-dotfiles：chore(skills): [cr_id_skip] Update builder-loop XXX
```

### 5.1 调试 hook 锁（V1.1+）

tester 强隔离通过 3 个 hook 脚本 + 锁文件实现。排查时可查阅：

```bash
# 锁文件位置（TTL 30 分钟内未被 SubagentStop 清理的遗留）
ls -la /tmp/cc-subagent-*.lock

# 锁写入日志（SubagentStart 时记录）
tail -f ~/.claude/logs/tester-lock-write-*.log

# 锁检查日志（PreToolUse 时记录 block/allow 决策）
tail -f ~/.claude/logs/tester-lock-check-*.log
```

### 5.2 空仓 fixture 验证

P0 修复后 setup-builder-loop.sh 支持在无 src/lib/app/pkg 的空仓环境下正常初始化（不再被 set -e 杀进程）。

验证 fixture：
```bash
bash ~/.claude/skills/builder-loop/fixtures/e2e/test-empty-repo.sh
```

该脚本会：1. 临时建空仓 + loop.yml  2. 跑 setup-builder-loop.sh  3. 检查状态文件生成  4. 清理临时目录。

### 5.3 完整 e2e fixture 套件（V1.1）

```bash
# 跑全部 e2e fixture
for f in ~/.claude/skills/builder-loop/fixtures/e2e/test-*.sh \
         ~/.claude/skills/builder-loop/fixtures/e2e/run-*.sh; do
  echo "=== $(basename "$f") ==="
  bash "$f" && echo "  → PASS" || echo "  → FAIL"
done
```

各 fixture 说明：

| 脚本 | 验证场景 | 依赖 |
|------|---------|------|
| `test-empty-repo.sh` | P0: 空仓 setup 不被 set -e 杀 | 无 |
| `run-fixture.sh` | T6.1-T6.2: 完整循环（setup→FAIL→fix→PASS） | python3 + pytest |
| `test-isolation.sh` | T6.3: tester 隔离 hook 拦截 source_dirs | tester-lock-check.sh 软链 |
| `test-conflict.sh` | T6.4: rebase 冲突 → 仲裁标记 → mock 修复 → 合回 | merge-worktree-back.sh |
| `test-arbitration-apply.sh` | V1.1: run-apply-arbitration.sh 三场景（high→APPLIED / low→LOW_CONFIDENCE / bad patch→APPLY_FAILED） | run-apply-arbitration.sh + merge-worktree-back.sh |

## 6. 设计原则（修改时遵守）

1. **零侵入**：未配 loop.yml 的项目 builder 行为完全不变
2. **可独立运行**：所有脚本必须能在纯 bash + 项目根目录环境下跑通，不依赖 CC 运行时（这是为 V3 daemon 接入留的接口）
3. **状态文件相对路径**：所有 `<项目根>/.claude/...` 路径用相对，不写死绝对
4. **失败保底**：任何脚本失败都要保证 builder 能拿到「至少有信息」的反馈，不能给空字符串
5. **退出码语义清晰**：0=成功、非 0=失败 + stderr 输出原因

## 7. 演进路径

- **V1.0**（已完成）：脚本调度器 + 弱隔离
- **V1.1**（已完成，P0-P6 全部闭环）：强隔离（hook 锁机制）+ worktree 真接入 + rebase 仲裁 + tester 触发整合 + e2e fixture 套件 + 脚本健壮性加固
- **V1.2**（已完成）：改动分级（L1 跳过 loop / L2 正常 / L3 先 tester）
- **V1.3**（已完成）：任务回顾与知识沉淀（commit 后触发 `/memory`）
- **V1.7**（已完成）：Reviewer 默认模型升级为 sonnet，消除 haiku + xhigh effort 的不兼容；Builder retry 加错误分类（API 参数错误直接走兜底，不盲重试）；新增 `test-reviewer-compat.sh` 做配置 lint 与可选 live smoke。详见 CLAUDE.md 第 5 节版本清单
- **V1.8**（已完成）：多状态并行架构（state 文件迁移到 `.claude/builder-loop/state/<slug>.yml`；locate-state.sh 按 CWD 找对应 state；单项目支持并行多个 loop；migrate-state.sh 向后兼容迁移旧版本）；setup-builder-loop.sh 加 flock 防并发竞态；merge-worktree-back.sh 清理时自动删除 state
- **V1.8.1**（已完成）：僵尸 state 自愈（Stop hook 遇到 `active != true` 的僵尸 state → 归档到 `legacy/<ts>-zombie_inactive.bak` + 放行，防止下次 builder 误判为活跃 loop）；EARLY_STOP 立即通知（Stop hook 早停从"改字段 + exit 0"改为"归档 + exit 2 + stderr 注入"，让 builder 当场收到通知立即 AskUserQuestion，而非等下轮 prompt）；配合 V1.8 的 per-worktree state 隔离，彻底解决跨 session 多任务串味问题。顺带修复 merge-worktree-back.sh auto-commit message 加 `[cr_id_skip]` 兼容严格 commit-msg hook 的项目
- **V1.9**（已完成）：Judge agent — LLM 语义判据补 PASS_CMD 二值判据盲区。新增 `scripts/run-judge-agent.sh`（hook 内嵌 API 调用，凭证双路径兼容正版 OAuth + Copilot env，模型 ID 三层 fallback），stop hook PASS 分支在 merge-worktree-back 之前叠加判定（continue_nudge / stop_done / retry_transient 三态路由），FAIL 分支仅识 retry_transient，任何故障路径降级回 PASS_CMD 二值判据。配套 `prompts/judge-system.md` / `docs/judge-agent.md` / `known-risks.md`。Telemetry 落 `.claude/builder-loop/judge-trace.jsonl`，下一轮 stop hook 自动后置补 outcome 标签（仅 continue_nudge 类）
- **V2**：短命 orchestrator subagent 替代脚本调度（出现多 agent 仲裁需求时启动）
- **V3**：独立 daemon 编排多项目（单开仓库 `cc-orchestrator-daemon`，复用本 skill 的契约）

详见架构文档 `.claude/plans/builder-loop-architecture.md`。
