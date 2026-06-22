# V3.0 reviewer-as-gate 重构 + 多 worktree session 隔离

> 来源：`.claude/improvements.md` 多条同源候选合并：
> - 跨 session 串扰（2026-05-08）
> - 同 session 多 worktree 反馈静默丢失（2026-05-08）
> - reviewer 退化为事后建议 V3.0 框架级重构（2026-05-01）
> - WIP 节流（2026-05-08）
> - AskUserQuestion 期间 hook 自激空转（2026-04-29）

<!-- role:shared -->

## 背景 & 目标

**背景**：当前 hook 一发 stop event 立即 merge worktree 进主线再让 builder spawn reviewer，导致两个连锁问题：① reviewer 退化成事后建议（阻塞级反馈合不掉）② hook 通过 stderr 喊话且 CC 路由不可控，多 session / 多 worktree 必然串扰或丢消息。

**目标**：把 hook 行为从「主动喊话 + 立即 merge」改成「挂牌子 + builder 主动拉取」。

具体三件事：

1. **拆 merge 时机**：PASS 后只在 worktree 内 commit、不 merge、不删 worktree。reviewer 通过后由 builder 主动调 merge-and-cleanup.sh。
2. **挂牌子代替喊话**：把 reviewer-params.json 合并进 state/<slug>.yml 的 reviewer_pending 段，按 slug 天然隔离。Builder 自检 cwd 含 worktree → 推 slug → 看自己 state.phase。
3. **hook 加多层闸**：L1（phase）+ L2A（AskUserQuestion）+ L2B（worktree 无改动）+ L3（pause 文件）静默不该跑的场景。

**预估改动级别**：L3（架构级 — 新建 2 个脚本 + state schema 加段 + 多个 hook 脚本逻辑改写 + builder.md 加自检 + 8 个新 fixture）。Builder 根据实际 diff 确认或修正。

## 验收标准

- **跨 session 不串扰**：双 session 并发 fixture 下，B session 在 A session 跑 PASS 时不会因 hook stderr 收到无关 reviewer 触发
- **同 session 多 worktree 不丢消息**：连续 setup 两个 worktree，第二轮 PASS 后 builder 自检能找到第二轮的牌子并主动 spawn reviewer
- **AskUserQuestion 静默**：transcript 末是 pending AskUserQuestion 时 hook 不跑 PASS_CMD
- **思考 / 讨论静默**：worktree HEAD 不变 + git status 空时 hook 不跑 PASS_CMD
- **pause 兜底静默**：`.claude/builder-loop/<slug>.pause` 存在时 hook 不跑 PASS_CMD
- **reviewer 阻塞主线无污染**：reviewer 给 🔴 阻塞反馈时 commit 仍在 worktree branch、未进主线
- **bare 模式行为不变**：bare loop（slug=`__main__`）仍走 PASS-then-commit-then-review 当前流程，不进 passed_pending_review
- **8 个新 fixture 全过 + 现有 fixture 零回归**

## 状态机定义

```
ø（无 state）
  │
  │ setup-builder-loop.sh
  ▼
active
  │
  ├─ PASS_CMD 通过 ─→ passed_pending_review
  ├─ PASS_CMD 失败 + iter < max ─→ active 自身（iter++）
  ├─ PASS_CMD 失败 + iter == max ─→ AskUserQuestion → ø 或 active
  ├─ 用户 abandon-loop.sh ─→ ø
  └─ hook 多闸命中 ─→ active 自身（静默 exit 0）

passed_pending_review
  │
  ├─ reviewer 0🔴 ─→ builder merge-and-cleanup.sh ─→ ø
  ├─ reviewer 🟡/🔵 ─→ builder 改代码（dirty 出现）─→ active
  ├─ reviewer 🔴 阻塞 + 用户继续修 ─→ active
  ├─ reviewer 🔴 阻塞 + 用户 abandon ─→ ø
  ├─ hook 检测到 worktree dirty ─→ phase=active 兜底自愈
  └─ hook 触发 ─→ L1 闸命中静默 exit 0
```

## state schema（新增字段）

```yaml
# .claude/builder-loop/state/<slug>.yml
slug: <slug>
phase: active                   # 新增：active | passed_pending_review
active: true                    # 保留（仅写不读做新决策，V3.x 下掉）
owner_cwd: "<path>"
iter: <n>
max_iter: 5
last_iter_head: <sha>           # 新增 — L2 闸 B 用
project_root: "<path>"
main_repo_path: "<path>"
worktree_path: "<path>"
start_head: <sha>
task_description: "..."
consecutive_nudge_count: <n>
reviewer_pending:               # 新增段，phase=passed_pending_review 时才写
  pass_start_head: <sha>
  reviewer_files: [<files>]
  diff_file: ".claude/reviewer-diff-<slug>.txt"
  report_path: ".claude/review_reports/<slug>_<ts>.md"
  written_at: <iso>
cleanup_phase: ""               # 新增 — merge-and-cleanup 幂等用：ff_merged | worktree_removed | state_removed
```

## 验收的关键场景对照

| improvements 条目 | fixture | 断言 |
|---|---|---|
| 跨 session 串扰（A 场景） | test-cross-session-isolation.sh | B session hook 触发时不读 A 的 state，不注入 reviewer 触发 |
| 同 session 多 worktree 丢消息（B 场景） | test-multi-worktree-feedback.sh | 第二轮 worktree PASS 后 state/<slug-2>.yml 写 reviewer_pending；builder 自检命中 |
| AskUserQuestion 自激空转 | test-askuserquestion-silence.sh | transcript 末尾构造 pending AskUserQuestion，hook 静默 exit 0 + 不跑 PASS_CMD |
| WIP 节流 | test-no-diff-silence.sh | worktree HEAD 不变 + git status 空时 hook 静默 exit 0 |
| reviewer 退化（V3.0） | test-passed-pending-review-lifecycle.sh | PASS → passed_pending_review；reviewer 0🔴 → merge-and-cleanup → ø；reviewer 🔴 + abandon → ø（worktree 保留） |

<!-- /role -->

<!-- role:builder -->

## 约束 & 边界

**不能碰**：
- bare 模式行为（slug=`__main__`）保持当前 PASS-then-commit-then-review 流程
- ~/.claude/settings.json 已注册的 hook 条目（不增不减，仅修改 hook 脚本内部逻辑）
- 现有 e2e fixture（test-stop-hook-debug-log.sh / test-locate-state-strategy5.sh / test-judge-* 等）全部要继续过
- arbitration（worktree rebase 冲突仲裁）流程
- dirty stash apply（V2.3）流程
- V2.5 stop-hook-debug.log + diagnose-stop-hook.sh
- reward hacking 防御（state.active=false / state.phase / state.iter 直接编辑仍被 PreToolUse 拒）

**必须兼容**：
- copilot 用户当前行为
- 已接入项目的老 state 文件（V2.7 之前创建，无 phase / last_iter_head 字段）
- 老 state 的兜底：hook 检测到字段缺失 → 不走兼容老路径，stderr 注入「[builder-loop] 检测到老版 state X（缺 phase 字段），无法走 V3.0 流程。请用 abandon-loop.sh 处理或手动添加字段」让 builder AskUserQuestion 用户决策

## 技术选型

### A：状态机字段 — phase 主判 + active 仅兼容保留

| 选项 | 选/弃 |
|------|------|
| **phase 主判 + active 写不读** | ✅ 选定 — 表达力清晰，三态 ø/active/passed_pending_review 完全枚举；老 state 兜底走"phase 缺失则 active 字段决策"；技术债靠文档下掉计划管住 |
| active 主判 + phase 辅助 | ❌ 弃 — 两字段必须同步，漂移风险高 |
| 完全删 active | ❌ 弃 — 破坏老 state，需迁移脚本（用户已否过） |

### B：消息类文件按 slug 拆 — 合并到 state.reviewer_pending 段

| 选项 | 选/弃 |
|------|------|
| **reviewer-params.json 直接合并到 state.reviewer_pending 段；reviewer-diff.txt 改名 reviewer-diff-<slug>.txt** | ✅ 选定 — state 本就按 slug 拆，零成本继承；少一种漂移源（state 与 reviewer-params 内容一致性问题消失） |
| 保留 reviewer-params.json 但路径加 slug | ❌ 弃 — 多一份冗余文件 |
| 写 reviewer-pending/ 子目录每 slug 一文件 | ❌ 弃 — 跟 state 重复表达 |

### C：hook 多层闸顺序（早闸优先）

闸顺序（成本由低到高）：
1. **L1 phase**：`grep '^phase:' state.yml` → passed_pending_review 静默 exit 0
2. **L2A AskUserQuestion**：解析 transcript_path 的 jsonl 末尾，看最后 assistant message 是否 AskUserQuestion tool_use 且无 tool_result
3. **L2B worktree 无改动**：`git rev-parse HEAD == state.last_iter_head` && `git status --porcelain` 空
4. **L3 pause 文件**：`[ -f .claude/builder-loop/<slug>.pause ]`

不命中任何闸 → 进 PASS_CMD 主路径。

### D：merge-worktree-back.sh 拆分

| 选项 | 选/弃 |
|------|------|
| **拆为 worktree-commit-only.sh（hook 调）+ merge-and-cleanup.sh（builder 调）；merge-worktree-back.sh 保留作 bare 模式入口** | ✅ 选定 — 单一职责，bare 兼容路径有出口 |
| 一个脚本两个 mode（--commit-only / --merge-cleanup） | ❌ 弃 — flag 入口比新文件读起来绕 |
| 完全废弃 merge-worktree-back.sh | ❌ 弃 — 破坏 bare 模式 |

### E：Builder 自检触发条件

精确条件（仅在 cwd 含 worktree 时跑，普通对话零负担）：

```bash
case "$(pwd)" in
  */.claude/worktrees/*)
    slug=$(basename "$(pwd)")
    state=".claude/builder-loop/state/${slug}.yml"
    if [ -f "$state" ]; then
      phase=$(grep '^phase:' "$state" | awk '{print $2}')
      [ "$phase" = "passed_pending_review" ] && spawn reviewer
    fi
    ;;
  *)
    # 主仓 cwd / 普通对话：跳过自检
    ;;
esac
```

兜底场景（builder 在 worktree 模式但 cwd 跑回主仓）：builder.md 加段「如果你曾经 setup 过 loop，回复前可 ls .claude/builder-loop/state/ 看是否有 phase=passed_pending_review 待你处理」——非强制约束，仅提示。

## 方案设计

### 流程图（worktree 模式）

```
窗口 1 setup loop（slug=AAA）
  → 建 worktree + state.yml(phase=active, last_iter_head=<start_head>)
  → cwd 切到 worktree

builder 改代码 → CC stop event → hook 触发
  ↓
hook 闸顺序：
  L1 phase=active                  → 通过
  L2A transcript 末非 AskUserQuestion → 通过
  L2B HEAD != last_iter_head OR dirty 非空 → 通过
  L3 pause 文件不存在              → 通过
  ↓
跑 PASS_CMD
  ↓
  ├─ FAIL → state.iter++ + stderr 注入失败反馈 + exit 2
  │
  └─ PASS → 调 worktree-commit-only.sh
            ↓
            worktree 内 git add -A + git commit
            ↓
            写 state.phase=passed_pending_review
                state.last_iter_head=<new HEAD>
                state.reviewer_pending={...}
            ↓
            写 reviewer-diff-<slug>.txt
            ↓
            stderr 注入「[builder-loop] phase=passed_pending_review，
                        请检查 .claude/builder-loop/state/<slug>.yml」
            ↓
            exit 2 → CC 自动续接 builder reply

窗口 1 builder 自动续接 reply
  ↓
自检：cwd 含 worktrees/AAA → slug=AAA → state.phase=passed_pending_review
  ↓
Read state.yml 拿 reviewer_pending 段 → spawn reviewer

reviewer 给反馈
  ├─ 0 🔴 → merge-and-cleanup.sh AAA
  │         ├─ state.cleanup_phase=ff_merged
  │         ├─ git merge --ff-only loop/AAA
  │         ├─ state.cleanup_phase=worktree_removed
  │         ├─ git worktree remove
  │         └─ rm state.yml
  │
  ├─ 🟡/🔵 → builder Edit/Write → dirty 出现
  │          → hook 下一轮 phase 自愈回 active
  │          → 跑 PASS_CMD（iter++）
  │
  └─ 🔴 阻塞 → AskUserQuestion [继续修 / abandon]
              → 继续修：同 🟡 路径
              → abandon：abandon-loop.sh AAA --keep-worktree
                        → rm state.yml + 保 worktree 给用户决策

窗口 2 同时跑无关任务（cwd 不含 worktree）：
  hook 触发 → locate-state 找不到当前 cwd 对应 state → exit 0 静默
  ↓
  即便 stderr 路由错误塞给窗口 2，窗口 2 builder 自检 cwd 不含 worktree → 跳过 → 不影响
```

### bare 模式（保持当前行为）

```
bare loop（slug=__main__）
  → 不建 worktree，cwd 在主仓
  → state/__main__.yml(phase=active, worktree_path="")

builder 在主仓改代码 → CC stop → hook 触发
  ↓
hook 闸顺序：跟 worktree 模式一样
  ↓
PASS_CMD 通过 → 走当前 V2.x 行为：
  - 主仓直接 commit（已有 dirty stash 流程）
  - 写 .claude/reviewer-params.json（保留全局唯一文件，bare 同时只一个 loop 不撞）
  - state.phase 不进 passed_pending_review，直接保 active
  - stderr 注入「请 spawn reviewer」（保留当前喊话）
  - rm state.yml（PASS 即结束）
```

bare 不走 reviewer-as-gate 的事前审，文档显式说明这是设计差异。

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| L1 闸把 hook 卡死循环（passed_pending_review 永远不解锁） | builder 自检拉 reviewer / hook 检测 worktree dirty 自愈回 active / abandon-loop 兜底，三道出路 |
| L2B 误判（builder 改了代码但被识别为没改） | 条件极难触发（除非 git stash 自动化），fixture 覆盖正反 |
| L3 pause 被 builder 滥用绕 PASS_CMD | abandon-loop / state.active=false 仍是禁止编辑（reward hacking 防御保留），pause 只是临时静默不破坏 state；fixture 加 pause 删除后能恢复跑 PASS_CMD |
| merge-and-cleanup.sh 中途失败 | state.cleanup_phase 字段记进度，幂等设计；ff_merged 后 worktree 删失败重试时跳过 ff merge 直接重删 |
| bare 模式跟 worktree 模式行为差异让用户混淆 | 文档显式区分；setup-builder-loop.sh 末尾输出当前模式（"bare 模式：PASS 后立即 commit + 事后审 / worktree 模式：PASS 后挂牌等审"） |
| 老 state（V2.7 之前）字段缺失 | hook 检测到缺 phase / last_iter_head → stderr 注入老 state 警告 → builder AskUserQuestion 用户决策（abandon / 手动补字段） |
| active 字段技术债 | improvements.md 立项跟踪 + CLAUDE.md / CHANGELOG.md 标 V3.x 下掉计划，写进方案文件 |
| reviewer 跑挂 / 报告路径错 | builder spawn reviewer 后 verify report_path 文件存在 + 非空，不存在 AskUserQuestion 用户决策（重 spawn / 跳过 / abandon） |

**退路**：

- L1/L2/L3 闸如果误静默 → 用户主动 reply 一句话触发新一轮 hook 检查（多闸不命中即跑 PASS_CMD）
- merge-and-cleanup 卡住 → 手动 git merge + git worktree remove + rm state（按 cleanup_phase 字段定位中断点）
- 老 state 处理出错 → abandon-loop.sh 万能出口

## 文件地图

### 修改

- `skills/builder-loop/scripts/setup-builder-loop.sh`
  - state 写入加 `phase: active` + `last_iter_head: ${START_HEAD}` 字段
  - worktree 模式末尾输出「mode=worktree（reviewer-as-gate）」；bare 模式输出「mode=bare（事后审）」

- `scripts/builder-loop-stop.sh`
  - 顶部加 4 道闸（L1/L2A/L2B/L3）顺序检查
  - PASS 路径改写：worktree 模式调 worktree-commit-only.sh + 写 state.phase=passed_pending_review + state.reviewer_pending；bare 模式保留当前行为（写全局 reviewer-params.json）
  - stderr 注入文案改成「[builder-loop] phase=passed_pending_review，请检查 .claude/builder-loop/state/<slug>.yml」（worktree 模式）/ 保留「请 spawn reviewer」（bare 模式）

- `skills/builder-loop/scripts/locate-state.sh`
  - 加策略 0：cwd 含 `.claude/worktrees/<slug>/` 时直接 `basename "$(pwd)"` 推 slug，命中即返回
  - 现有策略 1-5 保留作 cwd 在主仓时的 fallback

- `skills/builder-loop/scripts/abandon-loop.sh`
  - 支持 phase=passed_pending_review 状态
  - 加 `--keep-worktree` flag（passed_pending_review 默认 keep；active 默认 delete）

- `skills/builder-loop/scripts/merge-worktree-back.sh`
  - 仅 bare 模式入口保留（PASS 后 ff merge 主仓 + 写 reviewer-params.json）
  - worktree 模式逻辑迁移到 worktree-commit-only.sh + merge-and-cleanup.sh

- `~/.claude/commands/builder.md`
  - 加 reviewer-as-gate 流程段（PASS → 自检 → spawn reviewer → 反馈分支）
  - 加自检触发条件（cwd 含 worktrees → 推 slug → 看 phase）
  - 加 pause / unpause 用法说明（长对话需要静默 hook 时）
  - 加老 state 处理指引（hook 注入老版警告时的 AskUserQuestion 选项）

- `CLAUDE.md`
  - state schema 新字段说明
  - hook 闸顺序说明
  - 标 active 字段下掉计划（"V3.x 下掉时间窗见 improvements.md"）

- `CHANGELOG.md`
  - 新增 V3.0 段：reviewer-as-gate / 多闸 hook / 文件按 slug 拆 / state schema 演进 / active 下掉计划

- `.claude/improvements.md`
  - 关闭 5 条已并入本期的候选（标记 ✅ V3.0 落地）
  - 新增「active 字段下掉计划」候选条目（V3.x 时间窗 + 触发条件 + 排查清单）

### 新增

- `skills/builder-loop/scripts/worktree-commit-only.sh`
  - 入参：state_file
  - 在 worktree 内 git add -A + git commit + 输出 new HEAD
  - 不动主仓、不删 worktree

- `skills/builder-loop/scripts/merge-and-cleanup.sh`
  - 入参：state_file
  - 幂等：读 state.cleanup_phase 决定从哪步续跑
  - ff_merged 阶段：git merge --ff-only loop/<slug>，写 cleanup_phase=ff_merged
  - worktree_removed 阶段：git worktree remove + 删分支，写 cleanup_phase=worktree_removed
  - state_removed 阶段：rm state.yml
  - 失败退出码 != 0，不主动 cleanup（让 builder/用户决策）

- `skills/builder-loop/fixtures/e2e/test-cross-session-isolation.sh`
  - 模拟两个 CC session 并发，session A 跑 PASS_CMD 通过 → 断言 session B 的 hook 触发不会注入 reviewer 触发到 B

- `skills/builder-loop/fixtures/e2e/test-multi-worktree-feedback.sh`
  - 同 CC session 连续 setup 两个 worktree → 两个都跑 PASS → 断言 state/<slug-2>.yml 含 reviewer_pending 段

- `skills/builder-loop/fixtures/e2e/test-askuserquestion-silence.sh`
  - 构造 transcript_path 末尾为 pending AskUserQuestion → 断言 hook 静默 exit 0 + 不跑 PASS_CMD

- `skills/builder-loop/fixtures/e2e/test-no-diff-silence.sh`
  - state.last_iter_head == HEAD + git status 空 → 断言 hook 静默 exit 0

- `skills/builder-loop/fixtures/e2e/test-pause-file.sh`
  - touch .claude/builder-loop/<slug>.pause → 断言 hook 静默；rm 后下次 hook 正常跑

- `skills/builder-loop/fixtures/e2e/test-passed-pending-review-lifecycle.sh`
  - PASS → phase=passed_pending_review → 模拟 reviewer 0🔴 → merge-and-cleanup → ø
  - PASS → reviewer 🔴 阻塞 → abandon → ø + worktree 保留
  - PASS → reviewer 🟡 → builder dirty → phase 自愈回 active

- `skills/builder-loop/fixtures/e2e/test-merge-and-cleanup-idempotent.sh`
  - 模拟 ff_merged 完成后中断 → 重跑 merge-and-cleanup.sh 应跳过 ff merge 直接 worktree remove
  - 模拟 worktree_removed 完成后中断 → 重跑应只 rm state

### 不动

- `~/.claude/agents/reviewer.md`（reviewer 输入仍是 changed_files / report_path 等，state.reviewer_pending 段字段名保持兼容）
- `~/.claude/agents/tester.md`
- `~/.claude/agents/arbiter.md`
- `~/.claude/agents/doc-maintainer.md`
- `scripts/tester-lock-*.sh` / `scripts/tester-write-guard.sh` / `scripts/reviewer-timing-check.sh`
- `skills/builder-loop/scripts/run-pass-cmd.sh` / `extract-error.sh` / `early-stop-check.sh` / `init-loop-config.sh` / `loop-init.sh` / `migrate-state.sh` / `probe-project-stack.sh` / `run-apply-arbitration.sh` / `run-judge-agent.sh`
- `skills/builder-loop/scripts/diagnose-stop-hook.sh`（V2.5 工具，不动）
- `install.sh` / `uninstall.sh`（V2.7 已加方案识别，不动）
- 现有所有 e2e fixture（必须继续过）

## 执行任务列表

按依赖顺序：

1. **state schema 加字段** — 改 setup-builder-loop.sh 写 state 时加 `phase` + `last_iter_head` 字段
2. **新建 worktree-commit-only.sh** — 提取 merge-worktree-back.sh 中"worktree 内 commit"逻辑
3. **新建 merge-and-cleanup.sh** — 提取 merge-worktree-back.sh 中"ff merge + cleanup"逻辑，加幂等 cleanup_phase 字段
4. **改 merge-worktree-back.sh** — 仅保留 bare 模式入口，worktree 模式直接 exec 到 worktree-commit-only.sh
5. **改 builder-loop-stop.sh — 加 L1/L2A/L2B/L3 四闸**
6. **改 builder-loop-stop.sh — PASS 路径改写**（worktree 模式调 commit-only + 写 reviewer_pending；bare 保留）
7. **改 locate-state.sh — 加策略 0**（cwd 含 worktree 时直接推 slug）
8. **改 abandon-loop.sh — 支持 passed_pending_review + --keep-worktree**
9. **改 ~/.claude/commands/builder.md** — 加自检 / pause / 老 state 处理 / reviewer-as-gate 流程段
10. **写 8 个新 fixture**
11. **跑全量 fixture 验证零回归**（含 V2.5/V2.6/V2.7 现有 fixture）
12. **CLAUDE.md / CHANGELOG.md / improvements.md 更新**（technical debt 标注 + V3.0 段 + 关闭已落地候选 + active 下掉计划立项）

<!-- /role -->

<!-- role:tester -->

## 测试目标

验证 V3.0 reviewer-as-gate 重构 + 多 worktree session 隔离 + hook 多层闸的端到端行为。

## 关键测试场景

### 跨 session 隔离（test-cross-session-isolation.sh）

构造两个独立的"CC session"目录（模拟两个并发会话），每个 session 跑各自的 builder-loop：

1. session A：setup-builder-loop slug=A，构造 worktree，cwd 切到 worktree A
2. session A：模拟 builder 写代码 + git commit → 触发 hook
3. session A hook：跑 PASS_CMD 通过 → 写 state-A.phase=passed_pending_review
4. session B：cwd 在主仓（不在任何 worktree），仅做无关查询
5. session B：触发 hook（模拟 stop event）
6. **断言**：session B 的 hook 触发后 state-A 不被改写、state-B 不存在、stderr 不含 reviewer 触发文案
7. **断言**：即便 stderr 错误路由给 session B，session B 的 builder 自检 cwd 不含 worktree → 不会误 spawn reviewer

### 同 session 多 worktree 反馈不丢（test-multi-worktree-feedback.sh）

1. setup slug=W1 → cwd=worktree W1
2. 改代码 → hook 跑 → state-W1.phase=passed_pending_review
3. 模拟 builder 处理完 W1，调 merge-and-cleanup → state-W1 删除
4. setup slug=W2（同 session）→ cwd=worktree W2
5. 改代码 → hook 跑 → state-W2.phase=passed_pending_review
6. **断言**：state-W2 含 reviewer_pending 段，diff_file 路径含 W2 slug
7. **断言**：state-W1 不存在（已被 merge-and-cleanup 删）

### AskUserQuestion 静默（test-askuserquestion-silence.sh）

1. setup slug=X
2. 构造 transcript_path（jsonl）末尾为：assistant 的 tool_use AskUserQuestion，无后续 tool_result
3. 触发 hook，stdin 传 transcript_path
4. **断言**：hook exit 0（不是 exit 2 续接）
5. **断言**：state.iter 不变（PASS_CMD 没跑）
6. **断言**：loop-runs/iter-N-X.log 不新增

### worktree 无改动静默（test-no-diff-silence.sh）

1. setup slug=Y → 初始 state.last_iter_head=<sha>
2. 模拟 builder 一回合内完全没改动文件，HEAD 也没动
3. 触发 hook
4. **断言**：hook exit 0 + state.iter 不变 + loop-runs 不新增
5. 然后 builder 改一个文件（dirty 出现）
6. 再触发 hook
7. **断言**：hook 进 PASS_CMD 路径

### pause 文件兜底（test-pause-file.sh）

1. setup slug=Z
2. `touch .claude/builder-loop/Z.pause`
3. 触发 hook → 断言 exit 0 + 不跑 PASS_CMD
4. `rm .claude/builder-loop/Z.pause`
5. 触发 hook → 断言进 PASS_CMD 路径
6. **额外**：pause 文件存在但 phase=passed_pending_review → 断言仍静默（L1 + L3 都命中是允许的）

### passed_pending_review 全生命周期（test-passed-pending-review-lifecycle.sh）

子场景 A — reviewer 通过：
1. setup → 改代码 → PASS → phase=passed_pending_review
2. 模拟 builder 调 merge-and-cleanup.sh
3. **断言**：主仓 HEAD 含本轮 commit、worktree 目录已删、state 文件已删

子场景 B — reviewer 阻塞 + abandon：
1. setup → 改代码 → PASS → phase=passed_pending_review
2. 模拟 reviewer 给 🔴 → builder 调 abandon-loop.sh slug --keep-worktree
3. **断言**：state 文件已删、worktree 目录仍在、主仓 HEAD 未推进

子场景 C — reviewer 非阻塞 + 自愈：
1. setup → 改代码 → PASS → phase=passed_pending_review
2. 模拟 builder 在 worktree 改一个文件（dirty 出现）
3. 触发 hook
4. **断言**：phase 改回 active + PASS_CMD 重新跑

### merge-and-cleanup 幂等（test-merge-and-cleanup-idempotent.sh）

子场景 A — ff_merged 阶段中断后续跑：
1. setup → PASS → 调 merge-and-cleanup
2. 在 ff merge 完成后人为中断（state.cleanup_phase=ff_merged 已写、worktree 未删）
3. 重跑 merge-and-cleanup
4. **断言**：跳过 ff merge（防重复 merge）直接 worktree remove + rm state

子场景 B — worktree_removed 阶段中断后续跑：
1. 类似上面，在 worktree remove 完成、state 未删时中断
2. 重跑
3. **断言**：仅 rm state

## 测试深度

**深度**：每个 fixture 覆盖正反路径 + 现有 fixture 零回归。fixture 总数 8 个新增 + ~20 个现有必须过。

## 关键边界条件

- transcript_path 不存在 / 无法解析 → L2A 闸跳过（不静默，进 L2B）
- state.last_iter_head 字段缺失（老 state） → L2B 闸跳过（不静默，进 L3）
- worktree HEAD 解析失败 → L2B 跳过
- 老 state 缺 phase 字段 → hook 注入老版警告 + 不跑 PASS_CMD（让 builder AskUserQuestion 用户决策）
- bare 模式（worktree_path="" 或 slug=__main__） → 跳过 reviewer-as-gate 路径，走当前 V2.x 行为

## 不在测试范围

- reviewer 真实 spawn 行为（mock 即可，已有 test-reviewer-compat.sh 覆盖）
- arbitration 流程（test-arbitration-apply.sh 已覆盖）
- judge agent 行为（test-judge-* 系列已覆盖）
- bare 模式 PASS-then-commit-then-review 行为（保持当前，无需新覆盖）

<!-- /role -->
