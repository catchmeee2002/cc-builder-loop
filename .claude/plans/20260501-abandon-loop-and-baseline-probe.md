# Abandon-loop 出口 + 异步 baseline probe（V2.6）

> ⚠️ **版本号修正**：方案首写时用 V2.5，后发现主仓在对话期间已实施 V2.5（stop hook observability）+ V2.5.1（hotfix）。版本号被占用，本方案改为 **V2.6**。下文涉及 V2.5 的引用一律更正为 V2.6（CLAUDE.md §5 / §7.x / loop.yml stage 名 / state 字段都按 V2.6 命名）。

<!-- role:shared -->

## ⚡ Phase 进度跟踪（每完成一阶段必须立即更新）

> ⛔ **硬要求**：每个 Phase 完成后，本表必须当场更新；commit hash / 验证状态 / 坑点笔记不写完不准切下一阶段。
> 跨周接手时，第一件事先看本表当前状态，确定从哪个 Phase 续接。

| Phase | 范围 | 状态 | commit ref | E2E 验证 | 坑点笔记 |
|-------|------|------|-----------|---------|---------|
| **Phase 1** | abandon-loop.sh + dotfiles A3 关键词识别 + test-abandon-loop-flow.sh + CLAUDE.md §5 V2.6 段 + §7.12 + loop.yml v26 stage | ✅ 已完成 | 本仓: `2934b15` (auto-commit) + reviewer fix commit (待打) / dotfiles: 待 commit | ✅ 42/42 通过（reviewer fix 后仍 42/42） | (1) install/uninstall 不需改（整目录软链已生效）；(2) fixture 临时仓必须建 `.claude/loop.yml` 让 locate-state.sh 策略 1 锚定 PROJECT_ROOT；(3) commit-msg hook 全局拦截 fixture 内 git commit 必须 `chore(test): [cr_id_skip] Xxx` 格式；(4) `bash ... \|\| true` 让命令替换 exit 永远 0，捕获非 0 退出码用 `_out=$(cmd 2>&1); _ec=$?` |
| **Phase 2** | run-baseline-probe.sh + refresh-baseline-probe.sh + setup fork + state schema 4 字段 + run-pass-cmd.sh 抽函数 + 2 fixture + CLAUDE.md §7.13 | ⬜ 未启动 | — | — | — |
| **Phase 3** | builder-loop-stop.sh 差集归因 + merge-worktree-back kill probe + dotfiles A2 段 + 2 fixture + Phase 3 loop.yml stages + CLAUDE.md §7.14 + .gitignore 规则 | ⬜ 未启动 | — | — | — |

**状态图例**：⬜ 未启动 / 🚧 进行中 / ✅ 已完成 / ⏸️ 暂停 / ❌ 撤销

**续接 checklist**（接手时按顺序确认）：
1. 本表上一个 ✅ 的 Phase commit 是否仍在 main？(`git log --oneline | grep V2\.6`)
2. 上个 Phase 的 e2e fixture 是否仍 PASS？(`bash skills/builder-loop/fixtures/e2e/<fixture>.sh`)
3. CLAUDE.md §5 V2.6 段落是否反映当前已交付能力（与本表 ✅ 行匹配）？
4. 上个 Phase 坑点笔记是否需在新 Phase 规避？

**变更日志**（追加式，不删除历史）：

- `2026-05-01` 方案首次写入。Phase 1 启动。
- `2026-05-01` 版本号修正 V2.5 → V2.6（V2.5 已被主仓 stop hook observability 占用）。Phase 1 文件清单同步：CLAUDE.md §7.11 → §7.12（V2.5 已占 §7.11）；§7.12 → §7.13；§7.13 → §7.14。loop.yml stage 前缀 v25 → v26。
- `2026-05-01` Phase 1 完成。abandon-loop.sh + 42 case fixture 全过 + dotfiles A3 关键词段 + CLAUDE.md V2.6 段 + §7.12 + loop.yml v26_abandon_loop_flow stage。**坑点笔记**：(1) install/uninstall 不需改 — 因为 abandon-loop.sh 在 `skills/builder-loop/scripts/` 下通过整目录软链 `~/.claude/skills/builder-loop/` 自动生效，不像 hook 入口需注册 settings.json。Phase 2/3 的 run-baseline-probe.sh / refresh-baseline-probe.sh 同理不需改 install。(2) fixture 临时仓必须 `mkdir -p .claude && 写 loop.yml` —— locate-state.sh 策略 1 要求 `.claude/loop.yml` 锚定 PROJECT_ROOT，不写就让 reviewer hook 误以为不在 loop 项目走 fallthrough。(3) commit-msg hook 全局拦截：fixture 内所有 git commit 必须 `chore(test): [cr_id_skip] Xxxx` 格式。(4) `bash ... || true` 让命令替换的 exit code 永远是 0，捕获非 0 退出码必须用 `_out="$(cmd 2>&1)"; _ec=$?` 不要 `|| true`。

---

## 背景 & 目标

**背景**：当前 builder-loop 的安全门是单向的——「保护 PASS_CMD 不被 reward hacking 绕过」做得很完整（Edit state / 跳 PASS_CMD / 强制 commit 都有拦截），但**用户主动想停掉本次 loop**没有合法出口。典型触发场景（来自 improvements.md `2026-04-30` 与 BOT 项目活样本）：

- 跨 PR tech debt 浮出：iter 1 PASS_CMD fail 在测试 `tests/unit/test_event_loop_daily_report.py::test_no_proxy_always_1`，commit `acbce43`（4-29 切 SDK）后 obsolete，**与本期 DeepPerf 改动完全无关**
- 隔壁 worktree 在另一条分支修这个 obsolete 测试（commit `a898fdd`），还没合 master
- 用户在 AskUserQuestion 已明确选「我手动验 + 绕 loop 提交」，但 builder 实际遭遇三层阻塞：
  1. `Edit state.yml active=false` 被 PreToolUse 权限拦
  2. spawn reviewer 被 `reviewer-timing-check.sh` 拦（active=true）
  3. 没有专门的 abandon 入口
- 最终：loop 在用户决策期间继续跑 iter 2/3/4（每轮 5s 真实跑 + builder 每轮回复消耗 input/output token），**完全无意义**

**目标**：

1. **中断侧**：用户在 loop 进行中能用低摩擦方式停掉 loop（保留 worktree 改动，待外部条件满足后手动 cherry-pick / rebase）
2. **预防侧**：setup 时异步跑一次 baseline probe，把 baseline 已 broken 的 fail 集合圈出来，让中断侧的归因从「启发式（路径交集）」升级为「精确判定（fail 集合差集）」
3. **形成闭环**：A2（builder 主动诱导）+ A3（用户自然语言）→ 同一条 abandon 出口；setup baseline probe 异步并行，不阻塞主线

**成功标准**：三个关键场景都能跳过 —— ① BOT 复现（隔壁 PR 引入 broken test）→ builder 归因 → user 确认 → abandon；② 用户主动说「停掉loop」→ builder 识别 → 走 AskUserQuestion → abandon；③ probe 失败/timeout → builder 走原路径不闹事。

**预估改动级别**：L3（新接口 + 新模块 + state schema 扩展 + 跨仓 prompt 改造）。Builder 根据实际 diff 确认或修正。

---

## 约束 & 边界

**不能碰**：
- 现有 V1.x ~ V2.4 全部 state schema 字段（向后兼容；新加字段缺省视为初始值）
- `merge-worktree-back.sh` 现有 PASS / EARLY_STOP / 异常合并三条主路径（abandon 是**第四条独立出口**，不复用 merge）
- `reviewer-timing-check.sh` 拦截语义（abandon 把 state 归档后 hook 自动放行，**不需要**单独加 user-override 通道）
- `set -euo pipefail` 风格；新脚本严格遵循
- V2.3 reward-hack-guard 三选项注入路径（abandon 不是 reward hacking 灰色地带，AskUserQuestion 不提供「强制 commit 跳 loop」选项）

**必须兼容**：
- bare 模式（`worktree_path` 空）— probe 也用临时 worktree 跑（**统一一刀切**），不让 bare 变成特殊路径
- V2.3 dirty stash 流程 — abandon 触发时同 EARLY_STOP 流程（还原 stash 到主仓 + 写 legacy/ .info 留现场）
- V2.4 locate-state.sh 策略 5 — abandon 后 state 归档到 legacy/，下次 stop hook 找不到 active state 自然走 bootstrap / 静默放行
- 老 V2.4 state 文件缺新字段 → 下游脚本 `read_field || true` 兜底
- copilot-proxy / sk-ant-key 双路径凭证（V2.1 ~ V2.3 路径）— probe 不需调 LLM，**不依赖凭证**

**性能边界**：
- baseline probe 临时 worktree 创建：< 3s
- baseline probe 跑完整 PASS_CMD：与一轮 iter PASS_CMD 同量级（70~180s）
- abandon 入口本身：< 2s（ kill probe pid + remove 临时 worktree + 归档 state + 还原 stash）

**安全边界**：
- abandon-loop.sh **必须传 reason**（写入 state.stopped_reason 用于审计），无 reason 直接 exit 2 拒绝
- A3 自然语言识别**严格白名单 + 锚词限定**（必含 "loop" 或 "abandon"）+ 仅在 builder 上一轮收到 `[builder-loop ...]` stderr 注入后的下一轮 reply 识别 — 防止普通对话误触发
- abandon 后 worktree **保留不删**（用户后续手动 cherry-pick / rebase / 纯丢弃）

<!-- /role -->

<!-- role:builder -->

## 技术选型

### 方案 A（推荐）：新出口 + 异步 probe + 严格差集语义

**核心结构**：
1. **新增 4 个脚本**：abandon-loop.sh / run-baseline-probe.sh / refresh-baseline-probe.sh，加 install.sh 软链
2. **state schema 扩展 4 个字段**（baseline_probe_pid / baseline_probe_worktree / baseline_probe_started_at / baseline_probe_status）
3. **新文件**：`<P>/.claude/builder-loop/baseline-probe/<slug>.json` 存 baseline_fail_set
4. **dotfiles 改 builder.md**：A2 归因决策段 + A3 关键词识别段
5. **stop hook 改造**：fail 注入时附带 baseline_fail_set 比较结果

**优点**：
- 一刀切临时 worktree，无 inplace 模式分支，复杂度低
- 异步 probe 不阻塞 setup，主线零延迟
- abandon 是不可逆语义，与 EARLY_STOP 对齐，不需要 resume 路径（重新 setup 即可）
- 严格差集语义不靠路径启发式，归因精确

**缺点**：
- 后台进程管理（nohup setsid pid 跟踪）有一定脆弱性
- 临时 worktree 多管理一个 git 状态机
- 跨仓改动（cc-builder-loop + dotfiles 仓）

### 方案 B：仅 abandon 出口，不做 probe

只实现 abandon-loop.sh + dotfiles 加 A3 关键词识别。归因仍走启发式（fail 文件 ∩ changed_files）。

**优点**：实施快、不引入后台进程
**缺点**：归因辅助仍是软判定，BOT 场景的「user 怎么知道这个 fail 不是我责任」靠 builder 启发式判断容易出错

### 方案 C：仅 probe 不做 abandon

只做 baseline probe，让 stop hook 在 PASS_CMD fail 时自动判断 ⊆ baseline → 静默 exit 0 跳过本轮。

**优点**：完全机械化，user 无需介入
**缺点**：失去 user 决策的合法性（机制层主动做了判断），不可控；如果 baseline_fail_set 误判（环境差异等）会让真正的回归被静默吞掉

**结论**：选 A。B 缺归因精度，C 失去 user 决策权。

---

## 方案设计

### 架构图

```
setup-builder-loop.sh "<task>"
  ├─ V2.3 dirty stash + worktree create（不变）
  ├─ V2.3 ensure_gitignore_rules（追加 baseline-probe 路径）
  ├─ 写 state（含新 4 字段，默认 status=pending）
  └─ 后台 fork run-baseline-probe.sh（nohup setsid）
                              │
                              └─→ 临时 worktree on start_head
                                   .claude/worktrees/baseline-<slug>
                                   跑完整 PASS_CMD（reuse run-pass-cmd.sh）
                                   写 baseline-probe/<slug>.json (status: running → done/failed)
                                   git worktree remove 临时 worktree
                                   清 state.baseline_probe_pid

stop hook PASS_CMD 跑完 → fail 注入路径
  ├─ 读 baseline-probe/<slug>.json
  │   ├─ status=done → iter_fail_set vs baseline_fail_set 差集
  │   │   ├─ iter ⊆ baseline + baseline 非空 → fail 注入文案标 "[abandon-candidate]"
  │   │   ├─ iter ⊋ baseline → 注入"新增 fail" 列表（屏蔽 baseline）
  │   │   └─ 交叉 → 注入差集（仅本期责任部分）
  │   ├─ status=running → fallback 启发式（fail 文件 ∩ changed_files）
  │   └─ status=failed/missing → 不做归因，走原路径
  └─ exit 2 + stderr 注入

builder 收到 fail 注入后（A2 路径）
  ├─ 读 state + baseline-probe/<slug>.json + changed_files
  ├─ 判断 "[abandon-candidate]" 标记 OR baseline 差集为空
  └─ 是 → 主动 AskUserQuestion 二选项「继续修 / abandon 等外部修复」
       → 用户选 abandon → builder Bash 调 abandon-loop.sh "<reason>"

builder 上一轮收到 [builder-loop ...] stderr 注入后下一轮 reply（A3 路径）
  ├─ 读用户消息匹配白名单：停下loop / 停掉loop / 停止loop / 中止loop / abandon loop
  └─ 命中 → 主动 AskUserQuestion 一选项确认「确认 abandon? reason=...」
       → 用户确认 → builder Bash 调 abandon-loop.sh "<reason>"

abandon-loop.sh "<reason>"
  ├─ kill state.baseline_probe_pid（如存在）
  ├─ git worktree remove --force state.baseline_probe_worktree（如存在）
  ├─ 还原主仓 stash（V2.3 路径，如 worktree_mode=dirty）
  ├─ 写 trace event "ABANDON" + reason
  ├─ archive_to_legacy state → legacy/<ts>-abandon_<reason>.bak
  ├─ 写 legacy/<ts>-abandon_<reason>.info（worktree path / stash hash / changed_files）
  └─ exit 0 + stdout 输出 "Loop abandoned. Worktree kept at: <path>. Reason: <reason>."
       worktree 保留 + branch 保留 + 用户后续手动处理

refresh-baseline-probe.sh
  ├─ kill 旧 baseline_probe_pid（如 running）
  ├─ git worktree remove --force 旧临时 worktree（如存在）
  ├─ rm baseline-probe/<slug>.json
  └─ 重新 fork run-baseline-probe.sh（用主仓最新 HEAD 重跑）
```

### 接口签名

#### `abandon-loop.sh <state_file_path> <reason>`

```bash
#!/usr/bin/env bash
# abandon-loop.sh - 用户主动放弃本次 loop 的合法出口
#
# 参数：
#   $1 = state file path（必填，绝对路径）
#   $2 = reason（必填，自由文本，建议 < 80 字符）
#
# 退出码：
#   0 = 成功 abandon（state 已归档、worktree 已保留、stash 已还原）
#   2 = 参数错误 / state 不存在 / reason 为空
#
# 副作用：
#   - kill state.baseline_probe_pid
#   - remove state.baseline_probe_worktree
#   - 还原主仓 stash（仅 worktree_mode=dirty）
#   - state 归档到 legacy/<ts>-abandon_<reason>.bak
#   - 写 legacy/<ts>-abandon_<reason>.info
#   - 写 trace event "ABANDON"
#   - **不动** worktree_path 目录（保留，由用户处理）
```

#### `run-baseline-probe.sh <state_file_path>`（后台进程入口）

```bash
#!/usr/bin/env bash
# run-baseline-probe.sh - 临时 worktree 跑 baseline PASS_CMD
#
# 参数：
#   $1 = state file path（绝对路径）
#
# 退出码：
#   0 = 跑完（无论 baseline 是否有 fail，写 status=done）
#   1 = 异常退出（写 status=failed + reason）
#
# 副作用：
#   - git worktree add .claude/worktrees/baseline-<slug> at start_head
#   - 写 baseline-probe/<slug>.json (running → done/failed)
#   - 跑完后 git worktree remove --force baseline-<slug>
#   - 清 state.baseline_probe_pid
#
# 超时：默认 600s（10min），可由 loop.yml.baseline_probe.timeout_seconds 覆盖
# 失败 reason：worktree_create_failed / pass_cmd_crashed / timeout / disk_full / unknown
```

#### `refresh-baseline-probe.sh <state_file_path>`

```bash
#!/usr/bin/env bash
# refresh-baseline-probe.sh - 重跑 baseline probe（用户主动调）
#
# 参数：
#   $1 = state file path
#
# 副作用：
#   - kill 旧 probe pid
#   - rm 旧 baseline-probe/<slug>.json
#   - fork 新 run-baseline-probe.sh
#   - 用 git rev-parse HEAD（主仓最新）作为新 baseline
```

### state schema 扩展（4 字段）

```yaml
# 现有字段（V2.4）...
slug: "1777532136-deepperf"
active: true
iter: 0
worktree_path: "..."
worktree_mode: "dirty"
pre_loop_stash_ref: "abc123..."
# ...

# V2.6 新增
baseline_probe_status: "pending"  # pending|running|done|failed|skipped
baseline_probe_pid: 0              # 0 表示未启动 / 已清理
baseline_probe_worktree: ""        # 临时 worktree 路径
baseline_probe_started_at: ""      # ISO8601
```

### `<P>/.claude/builder-loop/baseline-probe/<slug>.json` 结构

```json
{
  "schema": 1,
  "slug": "1777532136-deepperf",
  "status": "done",
  "started_at": "2026-05-01T10:00:00Z",
  "completed_at": "2026-05-01T10:01:23Z",
  "git_head_at_start": "8107d9e...",
  "stages_total": 13,
  "stages_completed": 13,
  "baseline_fail_set": [
    {
      "stage": "test_unit",
      "test_id": "tests/unit/test_event_loop_daily_report.py::TestSendToLark::test_no_proxy_always_1",
      "summary": "AssertionError: Expected mock to be called once, called 0 times",
      "log_path": ".claude/loop-runs/baseline-probe/iter-0-test_unit.log"
    }
  ],
  "failure_reason": null
}
```

### `loop.yml.baseline_probe` 段（全部可选）

```yaml
baseline_probe:
  enabled: true                      # 默认 true，false 可整段关闭
  timeout_seconds: 600               # 默认 10min
  # 不提供 stages 字段 — 一刀切跑全部 stage
  # 不提供 environment 字段 — 一刀切临时 worktree
```

### A2 prompt 段（dotfiles `~/.claude/commands/builder.md`）

新增「步骤 X：fail 归因决策」段（在 stop hook fail 注入后第一步执行）：

```markdown
### 步骤 X：fail 归因决策（V2.6）

stop hook 喂回 fail 信息后，**第一步**判断 fail 是否本期责任：

1. 读 `<project_root>/.claude/builder-loop/baseline-probe/<slug>.json`
2. 判定：
   - 文件不存在 / status=running → 用启发式：fail 文件 ∩ changed_files 路径交集
   - status=done → 用差集：iter_fail_set - baseline_fail_set
   - status=failed → 不做归因，按 fail 内容修
3. 判定结果：
   - 责任集合为空 + baseline 非空 → **主动 AskUserQuestion** 二选项「继续修 / abandon 等外部修复」
   - 责任集合非空 → 修责任集合，**忽略** baseline 部分（不要被干扰）
4. 用户选 abandon → 调 `bash skills/builder-loop/scripts/abandon-loop.sh <state_file> "<reason>"`
```

### A3 关键词识别段（dotfiles `~/.claude/commands/builder.md`）

```markdown
### 步骤 Y：用户主动喊停识别（V2.6）

仅在**上一轮收到 `[builder-loop ...]` stderr 注入后的下一轮 user reply** 中识别。其他时机不识别。

白名单（含锚词 "loop" 或 "abandon"）：
- 停下loop / 停掉loop / 停止loop / 中止loop / abandon loop

命中 → 主动 AskUserQuestion 单确认「确认 abandon? reason=<待用户填>」→ 用户确认后调 abandon-loop.sh。

不识别：「停了」/「不修了」/「中止」单独出现 — 假阳性高，让用户重述。
```

### stop hook fail 注入改造

`scripts/builder-loop-stop.sh` PASS_CMD fail 路径加：
1. 读 baseline-probe/<slug>.json（jq 优先 / python3 fallback）
2. 计算 iter_fail_set vs baseline_fail_set 差集
3. fail 注入文案分两段：
   - 「本期新增 fail（建议修复）」
   - 「baseline 已存在 fail（不是本期责任）」
4. 全部 fail 都属于 baseline → 注入文案末尾追加 `[builder-loop attribution: 本轮所有 fail 在 baseline 都已存在，建议 abandon。]`

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标

验证 abandon 出口 + baseline probe 端到端可工作 + 异常路径不闹事。所有 fixture 走 e2e 黑盒：构造一个临时 git 仓 + loop.yml + 模拟 PASS_CMD fail/pass，断言外部可观察行为（state 文件状态、legacy 归档、stderr 内容、退出码）。

### 关键测试场景

#### 场景 1: BOT 复现 — 隔壁 PR 引入 broken baseline test

```
准备：临时仓 + loop.yml（pass_cmd 含一个 fail 测试 + 一个 pass 测试）
     setup-builder-loop "task" → 等 baseline probe 跑完
断言 1: baseline-probe/<slug>.json 存在 + status=done + baseline_fail_set 非空
断言 2: 模拟 builder Edit 一个无关文件 + 主动调 stop hook
        → fail 注入文案含 "[abandon-candidate]" 标记
断言 3: 模拟 builder 调 abandon-loop.sh "baseline broken not my fault"
        → state 归档到 legacy/ + stash 已还原 + worktree 保留 + branch 保留
        → exit 0 + stdout 含 worktree path
```

#### 场景 2: 用户主动喊停（A3 关键词识别）

```
准备：临时仓 + 跑到 stop hook fail 注入后
模拟用户消息: "停掉loop吧，这是上游的问题"
断言 1: builder 第一步应识别关键词 "停掉loop" 命中白名单
断言 2: builder 调 AskUserQuestion 确认 → 模拟用户答 "abandon"
断言 3: builder 调 abandon-loop.sh → state 归档成功
```

边界 case:
- "我中止loop这件事的争论" → 不识别（上下文不在 fail 注入后）
- "停了" 单独 → 不识别（不含锚词）
- "abandon" 单独 → 不识别（不含锚词 / 不含中文动词）

#### 场景 3: probe 失败 → builder 走原路径不闹事

```
准备：构造一个会让 git worktree add 失败的环境（比如 disk full / start_head 不存在）
     setup-builder-loop "task"
断言 1: setup 主路径仍正常退出（exit 0），不被 probe 阻塞
断言 2: baseline-probe/<slug>.json status=failed + failure_reason 非空
断言 3: 后续 stop hook fail 注入文案不含 "[abandon-candidate]" + 走原启发式归因
断言 4: stderr 一行提示 "⚠️ baseline probe 失败 (reason=...)，归因走启发式 fallback"
```

### Fixture 清单（5 个 e2e）

| Fixture 名 | 覆盖场景 | 关键 assert 数量 |
|-----------|---------|----------------|
| `test-abandon-loop-flow.sh` | 场景 1 abandon 主路径 + 场景 2 关键词识别 + reason 必填校验 + 异常调用拒绝 | ~30 |
| `test-baseline-probe-async.sh` | setup fork probe 不阻塞 + 异步完成时 status 转换 + JSON 结构正确 + 临时 worktree 自清理 | ~25 |
| `test-baseline-probe-failure-fallback.sh` | 场景 3 probe 失败/timeout/worktree 创建失败 各分支 + builder 走原路径不闹事 | ~20 |
| `test-attribution-diff-set.sh` | 严格差集语义：⊆ / ⊋ / 交叉 / 全空 四种组合下 fail 注入文案不同 | ~25 |
| `test-refresh-after-abandon.sh` | abandon 后 worktree 保留 / branch 保留 / 重新 setup 起新 loop / refresh-baseline-probe.sh 重跑 | ~20 |

### 测试深度

**快速**（黑盒 e2e）。不要 spawn tester subagent 写微观单元级别——本期是流程改造，e2e 黑盒覆盖率足。

### 已知风险开口（可不在本期 fixture 覆盖）

- 后台 probe 进程僵尸化（CC 进程异常退出后 nohup 进程仍跑）— 可由 ps + grep slug 监控，本期不做自动清理
- 测试 ID 不稳定（pytest random order / parametrize 改名）→ 差集失效 — 列入 R2 风险，长期反馈出现频率高再考虑用 hash 而非 ID 比对

<!-- /role -->

<!-- role:builder -->

## 风险 & 应对

| 风险 | 应对 |
|-----|-----|
| **R1 后台 probe 进程僵尸化** | nohup setsid 启动 + state 记录 pid + setup-builder-loop 重启时检测旧 pid 是否存活；abandon / refresh / merge cleanup 三处显式 kill |
| **R2 临时 worktree 残留** | run-baseline-probe.sh 用 trap EXIT 兜底 git worktree remove --force；setup 启动时检测 `.claude/worktrees/baseline-*` 残留并清理（pid 不存活时） |
| **R3 A3 关键词假阳性** | hard 锚词 "loop" / "abandon" 必含 + 上下文限定（仅 fail 注入后下一轮）+ AskUserQuestion 单确认（不直接执行） |
| **R4 abandon 后用户改主意想恢复** | 不做 resume，重新 setup 即可（state 在 legacy/ 仅审计）；worktree 保留确保不丢改动；CLAUDE.md §7.11 写明 |
| **R5 refresh 时旧 probe 还没完** | refresh-baseline-probe.sh 先 kill 旧 pid + remove 旧 worktree + rm 旧 JSON → 再起新 probe |
| **R6 baseline 测试 ID 不稳定（pytest random / parametrize）** | 当前用 test_id 字符串精确匹配，差集会失效；列入 known-risks.md 长期跟踪，反馈高再考虑 hash |
| **R7 临时 worktree 与主 worktree 同时跑同一测试** | 两个 worktree 文件系统完全独立；端口/资源冲突属于项目侧 PASS_CMD 设计问题，本期不解决 |
| **R8 V2.4 老 state 缺新字段** | setup 写新 schema 时缺字段视为初始值（status=skipped）；下游脚本 `read_field || true` 兜底 |
| **R9 baseline_fail_set 提取依赖 PASS_CMD 输出格式** | 当前 run-pass-cmd.sh 已有 stage 日志归档机制，extract-error.sh 已有错误抽取逻辑；复用即可，不引入新解析器 |

**完全回退方法**：
1. `loop.yml.baseline_probe.enabled: false` → 不起 probe，A2 走启发式归因
2. 仓库回滚到 V2.4 → abandon-loop.sh 不存在，A3 关键词识别 prompt 自然失效

---

## 文件地图

### 新增

| 路径 | 用途 | 大小估计 |
|------|------|---------|
| `skills/builder-loop/scripts/abandon-loop.sh` | abandon 主入口 | ~120 行 |
| `skills/builder-loop/scripts/run-baseline-probe.sh` | 后台 probe 实现 | ~150 行 |
| `skills/builder-loop/scripts/refresh-baseline-probe.sh` | 重跑 probe | ~50 行 |
| `skills/builder-loop/fixtures/e2e/test-abandon-loop-flow.sh` | abandon 流程 fixture | ~250 行 |
| `skills/builder-loop/fixtures/e2e/test-baseline-probe-async.sh` | probe 异步 fixture | ~200 行 |
| `skills/builder-loop/fixtures/e2e/test-baseline-probe-failure-fallback.sh` | probe 失败 fixture | ~200 行 |
| `skills/builder-loop/fixtures/e2e/test-attribution-diff-set.sh` | 差集归因 fixture | ~200 行 |
| `skills/builder-loop/fixtures/e2e/test-refresh-after-abandon.sh` | refresh fixture | ~150 行 |

### 修改

| 路径 | 改动 |
|------|------|
| `skills/builder-loop/scripts/setup-builder-loop.sh` | state schema 加 4 字段 + setup 末尾 fork run-baseline-probe.sh + ensure_gitignore_rules 加 baseline-probe / worktrees/baseline-* |
| `skills/builder-loop/scripts/merge-worktree-back.sh` | cleanup_worktree 前 kill probe pid + remove 临时 worktree |
| `scripts/builder-loop-stop.sh` | PASS_CMD fail 注入路径读 baseline-probe/<slug>.json + 差集计算 + 文案分段 |
| `skills/builder-loop/scripts/run-pass-cmd.sh` | 抽取 fail 集合提取逻辑（baseline probe 复用） |
| `skills/builder-loop/scripts/init-loop-config.sh` | ensure_gitignore_rules 加 `.claude/builder-loop/baseline-probe/` + `.claude/worktrees/baseline-*` |
| `skills/builder-loop/schema/loop.schema.json` | 新增 baseline_probe 段 schema |
| `install.sh` | 新增 abandon-loop.sh / run-baseline-probe.sh / refresh-baseline-probe.sh 软链 |
| `uninstall.sh` | 同上反向 |
| `loop.yml`（cc-builder-loop 自身） | PASS_CMD 加 v26_abandon_loop_flow / v26_baseline_probe stages |
| `CLAUDE.md` | §5 加 V2.6 段 + §7 新增 §7.12~§7.14 排查 |
| `.gitignore` | 加 `.claude/builder-loop/baseline-probe/` + `.claude/worktrees/baseline-*` + `.claude/builder-loop/legacy/*-abandon_*.{bak,info}` |
| `~/.claude/commands/builder.md`（**dotfiles 仓**） | A2 归因决策段 + A3 关键词识别段；走 dotfiles 独立 commit |
| `~/.claude/settings.json` | （如需）PreToolUse permission 放行 abandon-loop.sh / refresh-baseline-probe.sh 调用 |

### 不动

| 路径 | 原因 |
|------|------|
| `scripts/reviewer-timing-check.sh` | abandon 后 state 已归档 → hook 自动放行；不需 user-override |
| `agents/tester.md` | abandon 流程不涉及 tester subagent |
| `agents/arbiter.md` | abandon 流程不涉及 arbiter |
| `skills/builder-loop/scripts/locate-state.sh` | abandon 后 state 在 legacy/，locate 自然找不到 active 候选 |

<!-- /role -->

<!-- role:shared -->

## 执行任务列表

> 按依赖顺序，每步标注「改哪个文件 / 做什么」。Builder 可逐项推进。

### Phase 1: abandon 出口（先上线，低风险）

1. **新增 `skills/builder-loop/scripts/abandon-loop.sh`**：参数校验 + kill probe pid + remove 临时 worktree + 还原 stash（V2.3 路径）+ archive_to_legacy + 写 .info + 写 trace + stdout 提示
2. **改 `install.sh`**：新增 abandon-loop.sh 软链；`uninstall.sh` 反向同步
3. **改 `~/.claude/settings.json`**（如需）：PreToolUse permission 放行 abandon-loop.sh 调用
4. **改 `~/.claude/commands/builder.md`**（dotfiles 仓）：加 A3 关键词识别段（含白名单 + 上下文限定 + AskUserQuestion 二确认 + 调脚本）
5. **新增 fixture `test-abandon-loop-flow.sh`**：覆盖 reason 必填 / 成功 abandon / state 归档 / worktree 保留 / stash 还原 / 重复调拒绝
6. **改 `CLAUDE.md`**：§5 加 V2.6 第一段（abandon-loop.sh）+ §7 加 §7.12（V2.5 已占 §7.11）

### Phase 2: baseline probe（异步）

7. **改 `skills/builder-loop/scripts/run-pass-cmd.sh`**：抽取 fail 集合提取函数，让 baseline probe 复用
8. **新增 `skills/builder-loop/scripts/run-baseline-probe.sh`**：临时 worktree create + 跑 PASS_CMD + 写 JSON + cleanup（含 trap EXIT 兜底）
9. **改 `skills/builder-loop/scripts/setup-builder-loop.sh`**：state schema 加 4 字段 + 末尾 nohup setsid fork run-baseline-probe.sh + ensure_gitignore_rules 加新规则
10. **改 `skills/builder-loop/scripts/init-loop-config.sh`**：同步加 .gitignore 规则
11. **改 `skills/builder-loop/schema/loop.schema.json`**：加 baseline_probe 段
12. **新增 `skills/builder-loop/scripts/refresh-baseline-probe.sh`**
13. **改 `install.sh` / `uninstall.sh`**：新增 run-baseline-probe / refresh-baseline-probe 软链
14. **新增 fixture `test-baseline-probe-async.sh`** + `test-baseline-probe-failure-fallback.sh`
15. **改 `CLAUDE.md`**：§5 V2.6 段补 baseline probe 部分 + §7 加 §7.13

### Phase 3: 归因升级 + A2 接入

16. **改 `scripts/builder-loop-stop.sh`**：PASS_CMD fail 路径读 JSON + 差集计算 + 文案分段（"[abandon-candidate]" 标记）
17. **改 `skills/builder-loop/scripts/merge-worktree-back.sh`**：cleanup_worktree 前 kill probe pid + remove 临时 worktree
18. **改 `~/.claude/commands/builder.md`**（dotfiles 仓）：加 A2 归因决策段（步骤 X）
19. **新增 fixture `test-attribution-diff-set.sh`** + `test-refresh-after-abandon.sh`
20. **改 `loop.yml`**（cc-builder-loop 自身）：PASS_CMD 加 v26 stages
21. **改 `CLAUDE.md`**：§5 V2.6 段补 A2/A3 + §7 加 §7.14
22. **改 `.gitignore`**：加 baseline-probe / worktrees/baseline-* / legacy/*-abandon_* 规则

### Phase 4: 跨仓同步 commit

23. **本仓 commit**：`feat(builder-loop): [cr_id_skip] V2.6 abandon-loop + async baseline probe`
24. **dotfiles 仓 commit**：`feat(commands): [cr_id_skip] Builder.md A2/A3 abandon decision and keyword recognition`
25. **本仓 commit 后查 CLAUDE.md §3 同步 checklist**：确认软链/hook 注册 / matcher 改动同步

---

## 验收标准

按用户偏好：**场景验收为主，三个关键场景都能跳过**。

| 场景 | 验收方式 | 预期 |
|------|---------|------|
| **场景 1**：BOT 复现（baseline 已 broken + 本期改动无关） | 手动跑 e2e fixture `test-attribution-diff-set.sh` 中的对应 case | builder 收到 fail 注入应识别 abandon-candidate 标记 + 主动 AskUserQuestion + 模拟 user 选 abandon → 调脚本 → state 归档 |
| **场景 2**：用户主动喊「停掉loop」 | 手动跑 fixture `test-abandon-loop-flow.sh` 中的关键词识别 case | 仅在 fail 注入后下一轮触发 + AskUserQuestion 单确认 + 命中后调脚本 |
| **场景 3**：probe 失败/timeout | 手动跑 fixture `test-baseline-probe-failure-fallback.sh` | builder 走原启发式归因路径 + setup 不阻塞 + stderr 一行提示 |
| **机器验收**：5 个 e2e fixture 全过 | 跑完整 PASS_CMD（含 v26_* stages） | 退出码 0 |
| **回归验收**：V2.4 已有 fixture 不挂 | 跑 PASS_CMD 全套 | 全部 pre-V2.6 fixture 不受新字段影响 |

**长期验收**（不阻断本期合入，但要求一周内跟踪）：
- 真实 BOT 项目下次遇到「跨 PR baseline broken」时 abandon 一次着陆 → 验收闭环达成
- abandon 调用频率统计（trace event "ABANDON" 数量）— 反馈是否真有 trade-off 价值

<!-- /role -->

---

**方案已写入 `.claude/plans/20260501-abandon-loop-and-baseline-probe.md`，执行 `/builder` 即可开始执行。**

> Builder 接手时建议**分 Phase 实施 + 各自独立 PASS_CMD 验证**，不要一次性堆 25 步全做完。Phase 1（abandon-loop.sh + A3 识别）可以先单独跑通跑稳，再上 Phase 2/3。
