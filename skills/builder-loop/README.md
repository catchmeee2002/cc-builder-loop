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
│   ├── handle-pass-result.sh   # V5.4: PASS 后统一处理（commit + state + e2e/reward-hack 检测）
│   ├── extract-error.sh        # 错误反馈处理器（V1=full+脱敏）
│   ├── early-stop-check.sh     # 早停判据（无进展/反增长/保护路径）
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
- `~/my-dotfiles/claude/.claude/commands/planner.md` — 方案模板（3 视图区块已退役，隔离范式变更）
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

### 5.1 调试 hook 锁（V1.1+，V3.5 更新）

subagent 强隔离通过多个 hook 脚本 + per-agent-type 锁文件实现。排查时可查阅：

```bash
# 锁文件位置（V3.5+ 按 agent_type 分离）
# V5.0: 认身份隔离 hook 已退役，锁文件不再产生
# 保留的日志：stop hook debug log
tail -f ~/.claude/logs/stop-hook-debug.log
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
| ~~`test-isolation.sh`~~ | ~~T6.3: tester 隔离 hook（V5.0 退役）~~ | ~~已删除~~ |
| `test-conflict.sh` | T6.4: rebase 冲突 → 仲裁标记 → mock 修复 → 合回 | merge-worktree-back.sh |
| `test-arbitration-apply.sh` | V1.1: run-apply-arbitration.sh 三场景（high→APPLIED / low→LOW_CONFIDENCE / bad patch→APPLY_FAILED） | run-apply-arbitration.sh + merge-worktree-back.sh |
| `test-judge-agent.sh` | V1.9: judge agent 单元（mock Anthropic API，9 case：env 凭证 / API 超时 / 500 / 非法 JSON / low confidence / 凭证全缺 / disabled / dot 模型规范化） | python3 内嵌 mock server |
| `test-judge-integration.sh` | V1.9: judge agent 集成（stop hook 全流程 7 case：PASS+stop_done / PASS+continue_nudge / 连续 nudge 上限强制 / 降级原路径 / FAIL+retry_transient / FAIL 降级 / disabled） | run-judge-agent.sh + builder-loop-stop.sh |
| `test-judge-edge-cases.sh` | V1.9.1: judge edge case（reviewer TESTER_HINT 4 case：stop_done 后 nudge 计数清零 / backfill 幂等 / self-check 凭证全缺 exit 1 / FAIL 分支脚本缺失降级原 V1.8 路径） | 同上 |
| `test-pass-cmd-runs-worktree.sh` | V2.0: PASS_CMD 在 worktree 内跑（19 case：state 写 main_repo_path / project_root=worktree / worktree 内 loop.yml 加 stage 立即生效 / 老 V1.x state 兼容 fallback / 含空格 mktemp 路径鲁棒性） | setup-builder-loop.sh + builder-loop-stop.sh |
| `test-bare-loop-merge.sh` | V4.1: bare loop reviewer-as-gate（bare PASS → phase=passed\_pending\_review / merge-and-cleanup bare 分支 → MERGED \_\_main\_\_ / 老 V1.x state 兼容） | loop-commit.sh + merge-and-cleanup.sh |
| `test-bare-reviewer-gate.sh` | V4.1: bare 模式完整 reviewer-as-gate 生命周期（16 assert：PASS → reviewer\_pending 段 / L1 自愈 / merge-and-cleanup bare 分支 / loop-commit NOOP） | loop-commit.sh + merge-and-cleanup.sh + builder-loop-stop.sh |
| `test-e2e-default-bare.sh` | V4.1: e2e plan → bare 默认（10 assert：--no-worktree → bare state / 对照组 worktree state） | setup-builder-loop.sh |
| `test-run-pass-cmd-args.sh` | V2.0: run-pass-cmd.sh 三参签名行为（13 case：三参 LOG_ROOT 决定日志归档 / 两参 缺省 LOG_ROOT=RUN_CWD / FAIL 消息含 LOG_ROOT 路径 / RUN_CWD 内 loop.yml 缺失 fallback 主仓 + stderr 警告） | reviewer hint 补测 |
| `test-nudge-max-reads-worktree.sh` | V2.0: stop hook nudge 上限优先读 worktree loop.yml（18 case：worktree max=1 触发强制 stop_done / worktree loop.yml 缺失 fallback 主仓 max=99 走 nudge 分支） | mock judge agent + reviewer hint 补测 |
| `test-judge-env-file-load.sh` | V2.1: judge env file 自动加载（12 case：主 env 干净时 source / 主 env 已设时不覆盖 / 文件不存在退回 V1.9 行为 / 语法错误 stderr WARN / loop.yml.credentials_file 项目级覆盖） | run-judge-agent.sh + 重定向 HOME |
| `test-judge-model-fallback.sh` | V2.1: sonnet → haiku 降级链（28 case：连续成功 / 1 失败计数 / 2 失败切 fallback retry / fallback 也失败 / 401-429 不计数 / parse_error 计数 / fallback 留空禁用降级 / 旧 state 兼容 / 改 primary 立即生效） | mock copilot-proxy + 状态机 |
| `test-tester-write-guard.sh` | V2.2: tester 跨目录写拦截（13 case：拒绝主仓 / 放行 worktree / 无锁 / bare loop 老锁兼容 / 等于 worktree 根 / 前缀部分匹配 / path traversal / 非 tester 放行） | tester-write-guard.sh + 锁文件 |
| `test-dirty-stash-flow.sh` | V2.3: 主仓 dirty stash + worktree apply（5 case / 25 assert：clean 路径 / dirty stash push / rebase 拒绝 / --no-stash 跳过 stash / stash 副本可还原） | setup-builder-loop.sh + git stash |
| `test-reward-hacking-detect.sh` | V2.3: reward hacking 检测（13 个配置 lint + 5 case / 25 assert：双命中 LLM+正则 / -k 'not X' 跨 grep 实现 / B4 控制组不命中） | run-judge-agent.sh + judge-system.md + builder-loop-stop.sh |
| `test-locate-state-strategy5.sh` | V2.4: locate-state.sh 策略 5（4 case / 21 assert：1 active 命中 / 多 active 不绑 + stop hook 诊断 stderr / 死 worktree 排除 / inactive 不参与 / setup 后端到端 cwd 警告 + locate 命中） | locate-state.sh + setup-builder-loop.sh + builder-loop-stop.sh |
| `test-stop-hook-debug-log.sh` | V2.5: stop hook 可观测性（V2.5.1 hotfix 后 7 case：debug log 基础写入 + phase 顺序 / IO 失败容忍 / 1 MB rotate 触发 .1-.5 / diagnose-stop-hook.sh 6 段 + 严格 dry-run / setup 末尾自检识别 hook 注册缺失 / 子目录 cwd 路径不分裂 / log_path 含空格不截断） | builder-loop-stop.sh + diagnose-stop-hook.sh + setup-builder-loop.sh |
| `test-abandon-loop-flow.sh` | V2.6 Phase 1: 用户主动 abandon loop 的合法中断出口（10 case / 42 assert：reason 必填 / state 存在性 / active 检查 / dirty stash 还原 / clean / bare loop / invalid stash conflict / 重复拒绝 / reviewer-timing-check.sh hook 集成） | abandon-loop.sh + builder-loop-stop.sh + reviewer-timing-check.sh |
| `test-new-repo-loop.sh` | V1.5: 新仓初始化场景（loop-init 一键、空仓 setup、首轮 PASS_CMD） | loop-init.sh + 全套 |
| `test-parallel-loop.sh` | V1.8: 多状态并行（同项目两个 worktree slug 各自走 PASS 路径） | locate-state.sh |
| `test-zombie-selfheal.sh` | V1.8.1: 僵尸 state 自愈（active=false 归档到 legacy/）+ EARLY_STOP 立即通知 | builder-loop-stop.sh |
| `test-stop-hook-cursor.sh` | V1.8.2: 兜底激活 HEAD 游标（同 commit 不重复触发） | builder-loop-stop.sh |
| `test-stop-hook-race-and-commit-msg.sh` | V1.8.3: 并发 flock 互斥 + auto-commit task_description 语义化 + PASS 分支 state 预读 | builder-loop-stop.sh + merge-worktree-back.sh |
| `test-reviewer-compat.sh` | V1.7: reviewer 模型默认 sonnet 配置 lint（可选 --live smoke） | reviewer.md |

## 6. 设计原则（修改时遵守）

1. **零侵入**：未配 loop.yml 的项目 builder 行为完全不变
2. **可独立运行**：所有脚本必须能在纯 bash + 项目根目录环境下跑通，不依赖 CC 运行时（这是为 V3 daemon 接入留的接口）
3. **状态文件相对路径**：所有 `<项目根>/.claude/...` 路径用相对，不写死绝对
4. **失败保底**：任何脚本失败都要保证 builder 能拿到「至少有信息」的反馈，不能给空字符串
5. **退出码语义清晰**：0=成功、非 0=失败 + stderr 输出原因

## 7. 演进路径

各版本交付能力（V1.0 ~ V7.0）详见 [`CHANGELOG.md`](../../CHANGELOG.md)。

**当前最新版本 V7.0（unit-test-spec）**：planner 必出结构化 `<!-- unit-test-spec -->` YAML（L2/L3），tester 直接消费结构化目标而非 plan 全文。详细变更见 CHANGELOG V7.0 段。

**长期演进路线**（不带版本号，按规模分类）：

- **中期**：短命 orchestrator subagent 替代脚本调度（出现多 agent 仲裁需求时启动）
- **长期**：独立 daemon 编排多项目（单开仓库 `cc-orchestrator-daemon`，复用本 skill 的契约）

详见架构文档 `.claude/plans/builder-loop-architecture.md`。
