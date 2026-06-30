# Changelog — cc-builder-loop 已交付能力

> 从 CLAUDE.md §5 外移。记录各版本交付的能力与关键实现细节。

## V4.7 doc-lint / diff-level-check 默认 DIFF_BASE 修正（2026-06-30）

默认 DIFF_BASE 从 `HEAD~1` 改为 `HEAD`（只看 staged/unstaged）。`HEAD~1` 会把 loop 前的无关 commit（如 gitignore 瘦身）拉入判据输入，导致 339 处误报阻断 loop。SKILL.md pass_cmd 示例同步去掉显式 `HEAD~1`。fixture 各加 2 个 staged 场景用例（含 HEAD vs HEAD~1 回归守卫）。

## V4.6 CC 内置 worktree 干扰防御 + 文档新鲜度机械校验（2026-06-29）

**1. bgIsolation 防御**：setup 自动在项目 `.claude/settings.json` 写入 `bgIsolation: "none"`，防止 CC 内置 `EnterWorktree` 创建基于 main 的 worktree 与 builder-loop 的 HEAD-based worktree 冲突。SKILL.md 加禁令。

**2. doc_freshness_check**：diff-level-check.sh 输出新增 `doc_freshness_check` 字段，机械探测 `plan.md` / `docs/plan.md` 存在性。builder 步骤 3.5.5 以此字段为准逐文件 Read 检查过时性，禁止自证"已更新"。消化 improvements 6/21 条目（builder 步骤 3.5 允许自证、无机械校验）。

## V4.5 Stop hook 零子进程快速路径（2026-06-29）

在 V4.4 基础上进一步消除 no-op 路径的所有子进程 spawn（sed、bash locate-state.sh）和冗余 stat 调用。CWD 解析改 bash 内置字符串操作，locate-state 核心逻辑内联（先查 loop.yml → 再查 state 目录 → 有 state 才 fall through 到完整 locate-state.sh），SKILL_DIR 延迟到需要时才解析。无 `loop.yml` 的项目直接 exit 0 不写日志。fork+exec 从 2→0，stat 从 ~20→~5。NFS IO 压力大时从分钟级降到秒级以内。

## V4.4 Stop hook no-op fast path（2026-06-26）

无活跃 loop 时 stop hook 从 ~30s 降到 ~40ms。CWD 解析用 sed 替代 python3，locate-state 返回空后直接 exit 0 跳过所有 debug_log 和 python3 调用。有 `.claude/loop.yml` 的项目仍写 `{"phase":"no_op"}` 轻量日志（纯 bash）供 troubleshooting 区分"触发但无 state"和"未触发"。

## V4.3 Subagent Identity & Resume（2026-06-24）

subagent 从"匿名临时工"升级为"有身份的协作者"——state 文件追踪 agent_id，支持 SendMessage 续接。

**1. State schema 新增 `subagents` 段**
- 通用结构（按 agent_type 分键），V4.3 写入 tester + reviewer
- 字段：agent_id / started_at / status (running|idle) / transcript_path

**2. Hook 自动写入**
- SubagentStart hook 从 CC stdin JSON 读 `agent_id`（CC `coreSchemas.ts` 确认字段存在），写入 state + lock file
- SubagentStop hook 读 `agent_id` + `agent_transcript_path`，更新 state status=idle + transcript_path
- 仅 tester + reviewer 追踪（doc-maintainer/arbiter 不写）

**3. Stop hook inject 消息升级**
- e2e inject：读 state.subagents.tester，status=idle + id 非空 → 消息含 `tester_agent_id=<id>` + SendMessage 指令；否则保持 "spawn 新 tester"
- PASS reviewer inject：读 state.subagents.reviewer → 追加 `reviewer_agent_id=<id>`

**4. Builder prompt SendMessage 分支**
- e2e 续接：`tester_agent_id` 存在 → SendMessage 续接（只传失败用例）；报错/无响应 → fallback Agent(new)
- reviewer 续接：`reviewer_agent_id` 存在 → SendMessage 复查 🟡 findings；失败 → fallback 新 spawn

## V4.2 e2e_pending phase（2026-06-24）

e2e inject 前写 `phase: "e2e_pending"`，L1 闸静默后续 Stop 直到 e2e 完成或代码变动。修复 tester 运行期间 stop hook 反复触发（10+ 次）的 bug。

## V4.1 bare 模式 reviewer-as-gate 对齐 + e2e 默认 bare（2026-06-23）

bare 模式从 V2.x 事后咨询升级到与 worktree 一致的 reviewer-as-gate 前置门禁。

**1. 统一 PASS 路径**
- 新建 `loop-commit.sh` 替代 `worktree-commit-only.sh`，用 `project_root` 统一 bare/worktree 两种模式的 commit 操作
- stop hook PASS 分支删除 worktree/bare 双路径分歧（-150 行），统一走 commit → phase=passed_pending_review → reviewer_pending 段
- V2.x bare 路径（reviewer-params.json 生成 + rm state）整段删除

**2. L1 闸 bare fallback**
- `worktree_path` 为空时 fallback 到 `PROJECT_ROOT` 做 dirty 检测，bare 模式也能自愈回 active

**3. merge-and-cleanup.sh 接受 bare**
- 去掉 bare hard reject（exit 3），bare 走 stash drop + rm state
- cleanup_phase 幂等保护仅用于 worktree（bare 两步均幂等且顺序无关）

**4. e2e → bare 默认**
- builder.md 新增规则：plan 含 `<!-- e2e-cases -->` 标签时传 `--no-worktree` 给 setup

**5. Fixture**
- 新增 `test-bare-reviewer-gate.sh`（16 断言）、`test-e2e-default-bare.sh`（10 断言）
- 更新 `test-bare-loop-merge.sh`（对齐 V3.0 行为）、`test-worktree-commit-only.sh`（适配 loop-commit.sh）

---

## V4.0 Reviewer 吸收 Judge — plan 完成度检查 + 判据层统一（2026-06-21）

Reviewer 成为唯一的独立 agent 判据层，Judge 作为独立组件废弃。

**1. Reviewer Phase 0: plan 完成度检查**
- Reviewer 新增 Phase 0（plan 完成度检查 + early exit），接受 plan_path 输入
- Plan 中用 `<!-- plan-checklist -->` 标签包裹执行任务列表+文件地图，Reviewer 逐步骤语义验证
- Phase 0 不过 → 🔴 打回 builder（不进 Phase 1 代码审查）；过了 → 进 Phase 1

**2. Judge 废弃**
- Stop hook PASS 分支删除 judge 调用（~90 行），judge trace backfill 删除
- Reward hacking Layer 2 正则检测下沉到 stop hook 机械层（不依赖 LLM）
- FAIL 分支 retry_transient 简化为机械关键词 grep（不调 run-judge-agent.sh）

**3. State 字段变更**
- 新增 `plan_path`：通用 plan 文件路径（替代 `e2e_plan_path`）
- `e2e_plan_path` 废弃（stop hook 读时 fallback）
- Judge 相关 6 字段废弃：last_judge_action / last_judge_confidence / last_judge_ts / consecutive_nudge_count / judge_active_model / judge_consecutive_failures
- reviewer_pending 段新增 plan_path 字段

**4. 流程变更**
- 旧：PASS_CMD → e2e → judge → phase=passed_pending_review → reviewer → merge
- 新：PASS_CMD → reward_hacking_regex → e2e → phase=passed_pending_review → reviewer(Phase 0 + Phase 1) → merge

---

## V3.8 E2E 行为验收 stage（2026-06-20）

在 PASS_CMD 全过后、judge 之前，加入独立 tester subagent 驱动的端到端行为验证阶段。

**1. 设计哲学升级：判据按独立性分层**
- 原则零从「机器判据驱动」升级为「独立判据驱动」——判据可信度的关键属性是独立性（定义者和执行者都独立于被审计者），不是机器 vs LLM
- 三层：纯机器判据（人定义+机器执行）→ 独立 agent 判据（独立定义+独立执行）→ 同会话 LLM 判据

**2. E2E 验收机制**
- Plan 中用 `<!-- e2e-cases -->` 标签包裹行为验收用例（自然语言步骤列表）
- State 新增 `e2e_plan_path`（plan 指针）和 `e2e_verified_head`（通过时的 HEAD）
- Stop hook PASS 路径：judge 之前检查 state，提取用例，注入验证请求消息（exit 2）
- Builder 收到消息后 spawn tester（e2e 模式），tester 驱动浏览器/CLI/API 逐条验证
- Tester 全 pass → builder 写 `e2e_verified_head` 到 state → 下轮 stop hook 跳过 e2e 走 judge

**3. Tester 双模式**
- 写测试模式（原有）：收到 `spec_view` + `interface_signatures` → 写 pytest 文件
- E2E 执行模式（新增）：收到 `e2e_cases` → 驱动 app 验证行为，报 `E2E_SUMMARY`
- 隔离约束：只看用例文本 + app 运行态，禁止读源码/transcript/diff

**4. 新增文件**
- `scripts/extract-e2e-cases.sh`：从 plan 提取 e2e-cases 标签内容
- 2 个 fixture（22 assertions）：extract 提取 + stop hook e2e 分支

## V3.7 并发 Session 隔离 + Tester 写路径修复（2026-06-17~18）

修复并发 session 越界 + tester 写主仓 + fixture 清理挂起三条实战 bug。

**1. owner_session_id 防并发越界**
- stop hook 首次定位 state 时写入 `owner_session_id`，后续校验匹配，不匹配 → stderr 警告 + exit 0 skip
- 消化 2 条并发 session case（stop hook 被非 owner session 截获 + merge 后 cleanup 非原子窗口）
- state schema 新增 `owner_session_id` 字段

**2. tester 写主仓根因修复**
- 根因：`locate-state.sh` 策略 5 只匹配 `phase=active`，tester 在 `passed_pending_review` 阶段 spawn 时找不到 state → SubagentStart 不写 lock → write-guard 无锁走 builder 宽松模式
- 修复：策略 5 扩展为 `active + passed_pending_review`；subagent-start-guard additionalContext 注入条件同步扩展
- write-guard 加诊断日志（lock-resolve / strict-mode / wt-empty），定位断裂点用

**3. fixture 清理挂起修复**
- harness cleanup 在 `rm -rf` 前加 `git worktree prune` + `worktree remove --force`，解决含 worktree 注册的临时目录清理卡住

**4. cross-session e2e fixture 扩展**
- harness `run_hook` 支持可选 session_id 参数
- 新增 Case F/G/H：owner_session_id 不匹配 skip / 匹配正常处理 / 首次绑定写入

## V3.6 Reviewer 轮次优化 + Plan 假设化（2026-06-15）

降低 reviewer 平均轮次 + 把 plan 从「权威」frame 转为「假设」frame。

**1. Reviewer 轮次优化**
- builder.md：reviewer 建议 = 假设，builder 采纳前必须独立推导失效场景
- reviewer.md：硬性约束 #4——步骤 1 后禁止开放式补读，只允许防误报定点 Read/grep
- builder.md：spawn reviewer 时新增 `review_focus` 必填字段（参数边界值 + 具体怀疑点），reviewer 优先逐项验证
- reviewer.md：报告表格拆「问题」和「建议修法（hypothesis）」两栏，分离高/低置信度输出
- reviewer.md：报 🔴/🟡 前必须 Read/grep 实际 file:line 确认断言（V3.5 已部分落地，V3.6 补全）

**2. Plan 假设化**
- setup-builder-loop.sh：删除 plan_file 启发式猜测——方案路径由 builder 对话上下文持有，不再由脚本从 mtime 最新文件推测
- state schema：移除 plan_file 字段（11 个 fixture 同步清理）
- builder.md：新增「文件地图校验」步骤——读方案后 grep 校验 plan 数字 + 扫改动函数 caller，就地修正不回 planner
- builder.md：diff_summary 中实施与方案不同的点必须写明决策理由（方案是假设不是契约）
- planner.md：Phase 依赖检查从用户追问环节移至 pre-write 自检

**3. Prompt 清理**
- builder.md：修复 5 处对已废弃 `builder-loop.local.md` 的引用（V3.4 遗留），统一改为 `locate-state.sh` + state.yml

**4. 战略规划**
- cc-loop-tracking.md 刷新至 CC v2.1.177：Agent tool 仍无 schema（schema-out 继续搁置）、EnterWorktree 已稳定
- 新增 §6 长期演化方向：meta-think 攻防产出——spec contract / reviewer 并列收敛 / dashboard 三方向 + meta-decision 必须机器判据约束

## V3.5-B Step 3.5 机械化检测 + doc-lint 修复（2026-06-14）

消化 ≥6 条同根因——step 3.5 doc 评估 4 次漏触发 + doc-lint 签名变更误判 + 黑名单漏词。

**1. builder.md step 3.5 结构化输出**
- 输出格式从自由文本改为强制两行并列（doc-A + doc-B），缺任何一行 = 违规
- 同模式参照步骤 5 四档并列，已验证有效

**2. doc-lint 双向过滤**
- 从 diff `+` 行提取 ADDED_SYMBOLS，过滤掉签名变更（两边都出现的符号不算删除）
- 修复 sed BRE `^\+` → `^+` 的正则错误

**3. 黑名单补词**
- 通用词黑名单加 `append|clear`（6-03 误判根因）

**4. fixture**
- Case 6：签名变更不误判；Case 7：append/clear 过滤

## V3.5 Subagent 来源身份层（2026-06-14）

解决 9 条同根因——subagent 写落点错 / hook 撞错 session / 非 builder-loop agent 误触发。

**1. Per-agent-type lock 文件**
- 锁文件从 `cc-subagent-{sid}.lock` 改为 `cc-subagent-{sid}-{agent_type}.lock`，并发 subagent 不再互覆盖
- 新建 `lock-utils.sh` 公共函数库（7 个函数 + 白名单常量），6 个 hook 脚本统一 source

**2. 白名单 + active state 双条件**
- SubagentStart 只给白名单内 agent（tester/doc-maintainer/arbiter/reviewer）且有 active state 时写锁
- workflow / Explore / general-purpose 等非 builder-loop agent 完全不写锁，不触发任何 guard

**3. 通用清锁**
- `tester-lock-clear.sh` → `subagent-lock-clear.sh`，所有 managed agent 结束时按 session_id + agent_type 精确清锁
- SubagentStop hook 去掉 matcher 限制（原 matcher=tester）
- 旧格式锁向后兼容（legacy fallback）

**4. 5 个 e2e fixture**
- 并发锁隔离、白名单过滤、各类型清锁、TTL 过期、旧锁兼容

## V3.3 孤儿 worktree 检测与复用（2026-05-25）

早停/abandon 后遗留的 worktree 不再丢失——setup 自动检测并提示复用。

**1. 孤儿 worktree 检测**
- setup 在 flock 之后、worktree 创建之前扫描 WT_BASE_DIR
- worktree 目录存在但 STATE_DIR 无对应 state → 报为孤儿（exit 6）
- stderr 输出每个孤儿的 branch / dirty 文件数 / ahead commit 数 / 最近 commit

**2. `--reuse-worktree <path>` 复用**
- 跳过 worktree 创建和 stash，复用已有 worktree
- 从目录名反推 slug，写新 state（iter=0），worktree_mode="reuse"
- last_iter_head 设为 worktree 当前 HEAD（stop hook L2B 正确静默直到有新改动）
- 必须传绝对路径（孤儿检测输出可直接复制）

**3. `--ignore-orphans` 跳过检测**
- 有孤儿但想新建 worktree 时使用，不报 exit 6

**4. 防御加固**
- `--reuse-worktree` + `--no-worktree` 互斥校验（exit 2）
- 相对路径拒绝 + 明确错误提示

**5. fixture**
- `test-orphan-worktree-reuse.sh`：8 个 case / 38 assertions（检测、复用、忽略、无效路径、dirty 保留、多孤儿、相对路径、互斥 flag）

---

## V3.2 跨越界隔离 + 测试框架 + prompt 瘦身（2026-05-23 ~ 2026-05-24）

三类 worktree 越界污染的系统性修复 + fixture 基础设施重构 + prompt/hook 审计瘦身。

**1. stop hook slug 精确绑定**
- setup 写 `.claude/builder-loop.local.md` 作为 session 指针（只存 slug）
- stop hook 读 local.md 精确定位 state，取代 locate-state 策略 5（CWD 猜测）
- 删除兜底激活（无 local.md = exit 0 放行，不自动启动 loop）
- locate-state.sh 保留策略 1-4，删除策略 5

**2. setup 默认干净 worktree**
- 默认不 stash 主仓 dirty（V3.0 setup 在 builder 写代码前跑，dirty 必来自其他任务）
- 新增 `--touched-files file1,file2` 选择性带入（中途接入 loop 时用）

**3. merge-and-cleanup 防御加固**
- worktree dirty check：未提交改动时 abort + 保留现场（防 worktree remove 丢代码）
- detached HEAD 防御：merge 前断言主仓在分支上 + merge 后验证分支 ref 前移

**4. harness.sh 测试框架**
- `fixtures/e2e/harness.sh`：共享 create_test_env / run_hook / assert 系列 / harness_report
- 41 个 fixture 全部迁移，代码量平均减 40%
- 下次改机制实现只需改 harness，不用批量修 fixture

**5. 设计哲学**
- 新增 `docs/design-philosophy.md`：6 条原则（机器判据 / 数据唯一家 / 显式授权 / 改输入不改输出 / 三次即架构缺陷 / 契约先于实现）

**6. prompt 瘦身审计**
- builder.md 290→271 行：删反例教学 / 5 问释义 / 老 state 兼容说明 / 错误示例 / worktree 动机解释，合并 3 处 worktree_path 为统一声明
- tester.md 109→73 行：删角色模式对比 / hook 实现解释 / fixture 特化段
- stop hook stderr：PASS 消息 14→5 行、仲裁消息 24→9 行、早停消息 14→4 行（纯机器字段，流程由 builder.md 驱动）
- reviewer.md / arbiter.md：删重复声明和心理说辞

**7. doc-lint 误报修复**
- `git rm --cached` 文件（仍在磁盘）不再被判为"已删除"：文件层 + 符号层双重过滤
- 新增 fixture Case 5 防回归

**8. tester A+D 重构**
- A 加厚输入：tester spawn 时新增 `mock_targets` / `data_contracts` / `error_types` 三个可选字段，减少 tester 猜测外部依赖的盲区
- D 改 tampering 判据：从"测试文件被改 ≥3"改成"测试被删 / 断言被弱化（assert 删除 / skip·xfail 添加）≥3"，builder 修测试不再误触发早停
- 修复 `early-stop-check.sh` pipefail bug：`grep | wc -l` 在 grep 未匹配时通过 pipefail 杀脚本，加 `|| true` 兜底
- 修复双重计数：删除的文件同时被信号 1（删除数）和信号 2（assert 行数）计算，改为信号 2 只扫修改文件（`--diff-filter=M`）
- 新增 `test-early-stop-tampering.sh` fixture 5 个 case 覆盖新判据

**8. 文档评估分流（doc-maintainer A/B 分类）**
- builder.md 步骤 3.5 拆 A/B 两类：A 类机械同步（签名/对外文件）走 doc-maintainer subagent，B 类设计文档（版本条目/哲学/架构/CHANGELOG）builder 亲自写

## V3.0 reviewer-as-gate 重构（2026-05-09）

把 hook 行为从「主动喊话 + 立即 merge」改成「挂牌子 + builder 主动拉取」。三件事同时落地：

**1. 拆 merge 时机（reviewer-as-gate）**
- 新建 `worktree-commit-only.sh`：PASS 后只在 worktree 内 commit、不 merge、不删 worktree。
- 新建 `merge-and-cleanup.sh`：reviewer 通过后由 builder 主动调，做 ff merge + 删 worktree + 删 state；幂等设计（state.cleanup_phase 字段记进度 ff_merged → worktree_removed → state_removed）。
- `merge-worktree-back.sh` 保留作 V2.x 立即合主线路径（arbiter 续路径 + bare 模式 + 兼容 fixture 仍用）。
- bare 模式（slug=`__main__`）行为保持不变（仍走 PASS-then-commit-then-event-审）。

**2. 文件按 slug 拆**
- `reviewer-params.json` 合并到 state.reviewer_pending 段（消除两份字段漂移源）。
- `reviewer-diff.txt` → `reviewer-diff-<slug>.txt`（按 slug 拆，跨 worktree 不撞）。
- review_reports/ 路径含 slug（同上）。

**3. Hook 加多层闸自动识别非目标场景静默**
- L1 phase 闸：`state.phase=passed_pending_review` → 静默（牌子挂着等审）；特例：worktree 出现 dirty/新 commit → phase 自愈回 active 重跑 PASS_CMD。
- L2A AskUserQuestion 闸：transcript 末尾是 pending AskUserQuestion → 静默（builder 等用户答）。
- L2B 无改动闸：worktree HEAD == `state.last_iter_head` 且 git status 空 → 静默（builder 在思考/讨论）。
- L3 pause 闸：`.claude/builder-loop/<slug>.pause` 文件存在 → 静默（builder 主动 pause）。

**4. State schema 演进**
- 新增字段：`phase`（active / passed_pending_review）、`last_iter_head`、`cleanup_phase`、`reviewer_pending` 段。
- `active` 字段保留作向后兼容，**V3.x 渐进下掉**（详见 [improvements.md](.claude/improvements.md) 「active 字段下掉计划」）。

**5. abandon-loop.sh 适配**
- 加 `--keep-worktree` flag（默认行为，作显式入口）。
- 识别 `phase=passed_pending_review` 状态并在输出中提示用户。

**6. 跨 session 隔离 + 同 session 多 worktree 不丢消息**
- 通过 cwd 推 slug + 文件按 slug 拆双保险，跨 session 串扰 / 同 session 多 worktree 反馈丢失两个症状自动消除。
- locate-state.sh 策略 2 注释加强：cwd 含 worktrees/<slug> 是 V3.0 主信号源。

**7. 8 个新 e2e fixture**
- test-cross-session-isolation.sh：双 worktree cwd 隔离
- test-multi-worktree-feedback.sh：同 session 串行多 worktree 反馈不丢
- test-askuserquestion-silence.sh：L2A 闸
- test-no-diff-silence.sh：L2B 闸
- test-pause-file.sh：L3 闸
- test-passed-pending-review-lifecycle.sh：phase 全生命周期（reviewer 通过 / 阻塞 / 非阻塞）
- test-merge-and-cleanup-idempotent.sh：cleanup_phase 幂等
- test-worktree-commit-only.sh：单点验证

**8. dotfiles 同步**
- `~/.claude/commands/builder.md` V3.0 改动已同步（dotfiles commit `3349374`）：硬规则按 phase 判定 + 步骤 2 加 V3.0 路径分支（state.reviewer_pending）+ 老 state 兼容指引 + pause 用法。

并入的 improvements 候选：跨 session 串扰 / 同 session 多 worktree 反馈丢失 / V3.0 reviewer-as-gate / WIP 节流 / AskUserQuestion 期间 hook 自激空转。

## V3.0.1 reviewer-timing-check.sh phase 字段跟进 hotfix（2026-05-09）

P0 紧急修复，V3.0 拆 active/phase 字段时遗漏的一处勾子。

**事故触发**：session f80932fb（Engineering_Delivery_Bot 项目），builder PASS 后 phase=passed_pending_review，reviewer spawn 时被 reviewer-timing-check.sh 拦死（hook 仍读 active 字段过时逻辑），导致永远无法触发审核流程。

**根因**：reviewer-timing-check.sh 还在读 `^active:` 判定是否拦截，V3.0 改成用 `phase` 字段后勾子未同步。

**修法**：
1. Hook 主判切到 phase 字段：仅 phase=active 时拦截，phase=passed_pending_review 时放行。
2. V2.x 向后兼容：缺 phase 字段时 fallback 检查 active=true 兜底。
3. Deny 时补充 stderr 一行标准化诊断信息，避免 CC 把「exit 2 + 仅 stdout JSON」渲染成「无 stderr 输出」误导排查。

**新 fixture**：`test-reviewer-timing-check-phase.sh` 5 case 断言（①phase=active 拦 ②phase=passed_pending_review 放 ③缺 phase + active=true 拦 ④缺 phase + active=false 放 ⑤非 reviewer 早退）已接入 loop.yml stage。

**dogfooding 限制**：install.sh 创建的软链 `~/.claude/scripts/reviewer-timing-check.sh` 绝对路径指向仓库主仓，不指 worktree。本次 fix 改的是 worktree 内的脚本，运行时 hook 仍走主仓旧版本——所以本仓库自身 PASS 后 spawn reviewer 实地撞了同一个 P0（CC 渲染成「No stderr output」）。这次 reviewer 走兜底自审通过；merge 到主仓后，下次 V3.0 流程的 reviewer spawn 才能用上新 hook。

**并入 improvements**：本次新立项条目「[V3.0 P0 缺口] reviewer-timing-check.sh 还读 active 字段，PASS 后 reviewer 永远 spawn 不出来」（含事故现场 + 根因 + CC 渲染坑）随本 hotfix 标 ✅ 已修复。是 improvements.md 同期 [技术债]「active 字段下掉计划」的活样本——下掉前必须把所有读 active 字段的点改完，本条是其中一个。

## V1.0 核心能力

- 多阶段 PASS_CMD + 智能早停
- tester 强隔离（hook 锁机制）
- 方案文件三视图过滤（builder/tester/shared）
- worktree 真隔离 + 三档合回
- rebase 冲突仲裁（arbiter subagent）
- reviewer → tester 触发
- 改动分级（L1 跳过 / L2 正常 / L3 先 tester）
- 任务回顾与知识沉淀
- Stop hook 兜底激活（loop.yml 存在 + 有改动 + 无状态文件 → 自动启动 loop）

## V1.5

- Stop hook 续接修复（exit 2 + stderr，取代无效的 JSON stdout）
- Worktree 前置（builder 进入后先 setup 再写代码，避免代码丢失）
- NDJSON trace（`.claude/loop-trace.jsonl`，每轮记录 iter/stage/result/duration）
- 一键 init（`loop-init.sh` 整合 probe + init-loop-config + git init）
- E2E 测试（全新仓库端到端验证）

## V1.6

- Worktree auto-commit（merge 前自动提交未 commit 改动，防数据丢失）
- Reviewer 时序硬门禁（PreToolUse hook 拦截 loop 活跃期的 reviewer spawn）
- Reviewer 参数预计算（stop hook PASS 后写 reviewer-params.json，消除 LLM diff 计算依赖）

## V1.7

- Reviewer 默认模型 sonnet（兼容 max / copilot 双路径，消除 haiku+xhigh 失败场景）+ Builder retry 错误分类（`effort/reasoning/not supported` 等 API 参数错误直接走兜底，不再盲重试）
- E2E 新增 `test-reviewer-compat.sh`（配置 lint + 可选 `--live` smoke）

## V1.8

- 多状态并行（state 文件从 `.claude/builder-loop.local.md` 迁移到 `.claude/builder-loop/state/<slug>.yml`；locate-state.sh 按 CWD 定位；单项目可并行多个 loop；migrate-state.sh 一键迁移旧版本）

## V1.8.1

- 僵尸 state 自愈 + EARLY_STOP 立即通知
  - Stop hook 遇到 `active != true` 的 state → 归档到 `.claude/builder-loop/legacy/<ts>-zombie_inactive.bak` 后放行
  - EARLY_STOP 路径从"改 active=false + exit 0"改为"归档 + exit 2 + stderr 注入"，builder 当场收到通知
  - 配合 V1.8 的 per-worktree state 隔离，彻底闭环"同 session 多任务僵尸串味"问题

## V1.8.2

- 兜底激活 HEAD 游标
  - Stop hook bootstrap 分支新增「已处理 HEAD 游标」（`.claude/builder-loop/last_processed_head`）
  - PASS / 异常 merge / EARLY_STOP 三处出口写入当前 HEAD，下次 Stop 时若 HEAD 未前进且无未提交改动则静默放行
  - 消除"推完 commit 后反复触发 NOOP 空转 bootstrap"的自激循环

## V1.8.3

- Stop hook flock 互斥 + auto-commit message 语义化 + PASS 分支 state 预读
  - Stop hook 按 per-slug 粒度加 `flock -n`，抢不到锁 `exit 0` 静默放行（防 CC 并发触发的 TOCTOU race）
  - `merge-worktree-back.sh` 的 auto-commit message 从 state 的 `task_description` 解析，构造 `chore(loop): [cr_id_skip] Auto-commit ${task}`
  - Hotfix：PASS 分支把 `start_head` 读取提前到 merge 调用之前（防 cleanup_worktree rm state 后 grep 报错）

## V1.9

- Judge agent — LLM 语义判据补 PASS_CMD 二值判据盲区
  - `run-judge-agent.sh`：hook 内嵌 Anthropic API 调用，输出 `{action, confidence, reason, downgraded, ...}` 单行 JSON
  - 凭证双路径：`ANTHROPIC_API_KEY` env → `~/.claude.json` OAuth → none（降级）
  - 模型三层 fallback：`loop.yml.judge.model` > `$ANTHROPIC_DEFAULT_HAIKU_MODEL` > `"claude-haiku-4-5"`
  - 三态判定：`continue_nudge` / `stop_done` / `retry_transient`
  - 防脱缰：iter 上限 + 连续 nudge 上限默认 2 + confidence 阈值 0.5 + API 超时 8s
  - 任何故障路径 → `downgraded=true` + 走原 PASS/FAIL，不阻断
  - 完全回退：`loop.yml.judge.enabled: false`

## V2.0

- PASS_CMD 跑 worktree（元问题修复）+ tester/doc-maintainer 流程加固
  - 元问题根因：`run-pass-cmd.sh` 死代码读旧路径 → PASS_CMD 永远跑主仓
  - state schema 重构：`project_root` = 干活的地方；新增 `main_repo_path` = 主仓
  - `run-pass-cmd.sh` 改三参签名 `<run_cwd> <iter> [<log_root>]`
  - `early-stop-check.sh` 修保护路径检测失效 bug
  - doc-maintainer audit checklist 落地 `docs/doc-maintainer-audit-checklist.md`

## V2.1

- Judge agent 长期共存方案 + sonnet→haiku 降级链
  - env file 自动加载：`judge-env.sh`（主 env 缺失时 source）
  - sonnet → haiku 降级链：默认 `primary_model=claude-sonnet-4-6`，连续失败 2 次切 haiku
  - 降级状态本 loop 内有效，PASS 后自动重置
  - 默认 timeout 8 → 15 秒

## V2.1.1

- `.gitignore` 自愈固化
  - `init-loop-config.sh` 新增 3 条 ignore 规则
  - `setup-builder-loop.sh` 每次启动跑 `ensure_gitignore_rules()` 幂等自愈

## V2.2

- Tester 跨目录写硬门禁 + 复盘强制分类闸门 + Bootstrap 空转修复
  - `tester-write-guard.sh` 新 PreToolUse hook，物理拦截 tester 写到 worktree 之外
  - 锁 schema 扩展：追加 `worktree_path` / `main_repo_path` / `slug`
  - 复盘改造为强制 4 桶分类（A1/A2/B/C）
  - Bootstrap 触发器砍 `HAS_RECENT_COMMIT`，只看 `HAS_DIFF`

## V2.2.1

- Bootstrap 纯文档白名单
  - 改动全命中 `\.md$|^docs/|\.txt$|^LICENSE$|\.gitignore$` → 静默放行

## V2.3

- 主仓 dirty 安全入 worktree + Reward Hacking 检测
  - dirty stash：setup pre-flight `git stash push -u` + worktree 内 apply
  - state 新增：`pre_loop_stash_ref` / `pre_loop_dirty_files` / `worktree_mode`
  - Layer 2 正则兜底检测 reward hacking（loop.yml / pyproject.toml 等配置 + 关键词双命中）
  - stop hook 三选项注入 + `loop.yml.judge.reward_hacking_detection: false` 可关

## V2.4

- locate-state.sh 策略 5 — 主仓 cwd 自动绑定唯一 active worktree
  - 策略 2/3/4 全 miss 后扫 active=true + worktree_path 存活 → 恰好 1 个候选时绑定
  - setup cwd 警告 + stop hook 诊断 stderr

## V2.5

- stop hook 可观测性 — debug log + diagnose 脚本 + setup 自检
  - `debug_log()` 函数 + 10 处 phase 插桩，NDJSON 格式，1 MB rotate
  - `diagnose-stop-hook.sh`：6 段 dry-run 排查
  - setup 末尾 hook 注册 + 软链自检

## V2.5.1

- stop hook observability hotfix
  - debug_log 路径分裂修复（子目录 cwd 时日志集中写到 PROJECT_ROOT）
  - pass_cmd_result.log_path 空格截断修复

## V2.6 Phase 1

- abandon-loop.sh 出口 + dotfiles A3 关键词识别
  - `abandon-loop.sh <state_file> <reason>`：归档 state + stash 还原 + trace event + worktree 保留
  - A3 关键词白名单：停下loop / 停掉loop / 停止loop / 中止loop / abandon loop
  - 后续 Phase：Phase 2 异步 baseline probe + Phase 3 严格差集归因（未实施）

## V2.7

- Max / Copilot 方案运行时识别
  - install.sh 新增 `detect_plan()` 函数，读 `ANTHROPIC_BASE_URL` env 识别方案
  - Max 方案（direct HTTPS）→ 5 个 hook（不注册 tester-write-guard.sh，CC 直连无需 Write/Edit 代理拦截）
  - Copilot 方案（localhost/127.0.0.1）→ 6 个 hook（含 tester-write-guard.sh）
  - diagnose-stop-hook.sh 同步 PLAN env 参数，banner 加"plan: X base_url=Y"，[1/6] hook 检测按方案过滤
  - 新增 e2e fixture `test-plan-detection.sh`（21 断言覆盖两方案 install + diagnose [1/6]+[2/6] verdict + --json plan 字段）

## V2.7.1

- install / uninstall 鲁棒性增强（A 批 cherry-pick 回 main）
  - `install.sh` `has_entry()` → `find_entry_status()` 三态返回（missing / match / stale），matcher 字面变化时先删旧再写新，避免同脚本多条 stale 条目并存
  - `uninstall.sh` 软链删除循环 + `bl_scripts` 列表都补 `reviewer-timing-check.sh`（pre-existing 漏项）
  - 新增 e2e fixture：`test-install-uninstall-roundtrip.sh`（装→卸语义还原 baseline）+ `test-install-matcher-update.sh`（matcher 改后重 install 识别 stale）
