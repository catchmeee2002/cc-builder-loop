---
name: full-driver-v4-experiment
description: 运行 Builder-loop Full Driver v4 实验执行引擎。由 Codex 原生 Agent 编排 Assurance Core v4，覆盖本地 Git 代码或 L1 文档交付、dirty intake、Tester、proof、机器验证、黑盒、Reviewer、恢复与 finalize。仅由用户显式调用 $full-driver-v4-experiment，或由已授权的 $builder 实验 handoff 加载；普通实施请求不得直接或隐式调用。
---

# Full Driver v4 Experiment

这是实验执行引擎。Core 只保存 Mission、Authority、Assurance、Execution、证据与 Git 事务事实；
Agent spawn、same-thread follow-up、结构化结果解析和持续循环由本 Skill 使用 Codex 原生能力执行。
不要持久化“下一步做什么”，每轮都重新调用 `driver-next` 派生动作。

## 启动与输入

1. 仅处理单一、本地 Git 交付；远程部署、外部设备和多仓原子事务不在本实验范围。
2. 读取项目 `AGENTS.md`、文档政策和设计哲学。直接显式调用时生成四事实面 contract；由 `$builder`
   handoff 加载时只接受 Planner 已验证并由 Builder 重验的唯一 contract，不重新解释或扩写。随后运行
   `codex-builder-loop assurance --experimental-v4 validate/start`。
   初始 `execution.agents` 保持为空；只有原生 spawn 返回真实 identity 后，才通过
   `prepare-tester` / `prepare-reviewer` 写入，不能预填或猜测 agent/thread id。Full Driver contract
   必须设置 `execution.driver_enforced=true`，使缺失 `action_id` 也无法绕过当前 Driver action。
3. `mission.delivery_kind=documentation` 是 L1：只保留 Reviewer/doc-review，不创建 Tester、machine
   或 blackbox 假证据。
   其他代码交付默认冻结 `tester`、`proof`、`machine`、`blackbox`、`reviewer` 五个独立 gate；只有
   用户明确改变 assurance 强度时才可增减。
4. Authority 中已授权 dirty intake 由 Core 校验 state digest，并复制到隔离 candidate 的
   `dirty_snapshot`；目标 worktree 保持原样。已授权 dirty 不暂停用户。
5. `mission.delivery_kind=preparation` 可修改 exact `protected_support_paths`，经 Reviewer 与 finalize
   形成 protected preparation。后续 business contract 使用 `execution.continuation` 单次消费；同一
   continuation token 的重复或二次 replay 必须由 Core 拒绝，不能复制旧 evidence 或 transcript。

## 角色隔离与连续性

### Tester 初次 spawn

每个身份只允许一次初始 `spawn_agent(agent_type="tester", fork_turns="none")`，一个 run 只 spawn 一次 Tester。
最小 brief 只包含 `run_id`、冻结 `contract`、Tester worktree、spec head 或隔离 publication manifest，
以及 author/blackbox 阶段目标；不得夹带父线程讨论、用户倾向、Builder 辩护或候选信息。

### Reviewer 初次 spawn

到达 `reviewer_final` 后只允许一次初始 `spawn_agent(agent_type="reviewer", fork_turns="none")`，一个 run 只 spawn 一次 Reviewer。
最小 brief 包含冻结 contract、candidate、完整 diff、验证证据和文档政策路径；
不得夹带父线程讨论、用户倾向或 Builder 辩护。Reviewer 必须看到候选信息；候选与完整 diff 是必需审查输入。

### 后续 turn

Tester 后续阶段必须用 `followup_task` follow-up 同一个 Tester thread；禁止 spawn 新 Tester，也不得
清空或重置上下文或角色历史。Reviewer finding 后必须用 `followup_task` follow-up 同一 Reviewer thread；
不新建 Reviewer，也不得清空或重置上下文或角色历史。只有 Core 明确允许 identity replacement
时才建立新身份，并保留旧 identity 与失效证据。

## 实现纪律

- 故障修复首次 edit 前，用现有证据确认观察到的失败、支持的机制、最小充分的根因修复，以及能区分
  修复前后的回归判据。证据不足时继续诊断或停止，不靠猜测堆兜底，也不建立第二份诊断状态。
- 每个独立实现单元完成后做廉价局部自检；跨模块变化在阶段边界再自检。自检不写 ledger，也不替代
  machine、Tester、proof、blackbox 或 Reviewer 正式门禁。

## 原生持续循环

持续调用 `assurance --experimental-v4 driver-next`，校验返回的 `action_id` 后执行恰好一个动作，再
重新派生；所有对应的 mutation CLI 都传入 `--action-id <action_id>`，由 runtime 拒绝过时或错配的
dispatch。直到 `finalize` 或明确的决策边界。动作面如下，不能跳 gate：

- `builder_implement` / `builder_fix`：只在 candidate worktree 和 Builder ownership 内实现。普通
  修复、fixture 修正和实现缺陷不修改 Mission。
- `checkpoint_builder`：Builder 工作完成后先提交 candidate，再调用 `checkpoint-builder`，让 Core
  计算并绑定 candidate HEAD、builder files 和 evidence invalidation；Skill 不手写 Execution 清单。
- `parallel_ready:true`：先用只允许 identity bootstrap、禁止读写仓库的最小 prompt spawn `tester`
  custom agent；使用工具返回的真实 identity 调用 `prepare-tester`，再 follow-up 同一 thread 执行
  `phase=author`。Tester 以 spec HEAD 为独立基线。
- `parallel_ready:false`：先 `publish-prerequisites`，再 spawn Tester 并以真实 identity 调用
  `prepare-tester`；Tester 只能从隔离
  publication HEAD 读取 exact file/blob manifest。publication 的 manifest 与 blob 是串行 Tester
  的唯一新增公开输入，发布后不可漂移。
- `tester_author`：首次 spawn Tester；后续 correction 使用同一 thread 续接。收到
  `TESTER_RESULT` 与 `PROBLEM_REPORT` JSON 后先校验身份和 schema；`tests_ready` 才调用
  `integrate-tester` 并记录 tester evidence。
- `prove-tests` / `tester_proof`：由同一 Tester thread 对冻结 behaviors 产出 proof spec；主线程把
  spec 和真实 Tester identity 交给实验 namespace 的 `prove-tests --action-id <action_id>`。Core 按
  公共 `schema/codex-test-proof.schema.json` 隔离运行 candidate 与 baseline-red/mutation，只有每个
  behavior 恰好一次且反例为真实 assertion failure 时才记录 proof evidence。命令非零后先读回
  `status.proof_failure`：只有它仍为 current、绑定同一 action/code 时，才把本次 Core failure 视为已
  持久化并继续调用 `driver-next`；否则保持原错误并停止。
- `tester_proof_diagnose`：续接原 Tester thread，只把 Driver 返回的 current `proof_failure` 交给
  `phase=proof_diagnose`。本 turn 只归因、不改文件；必须返回非空 problem，owner 仅为 builder、tester
  或 plan，随后调用 `record-problems --action-id <action_id>`。`proof_failure_decision` 直接停止到用户，
  不把环境、身份或完整性错误猜成角色修复。
- `verify_machine`：调用 Core `verify-machine`，保留真实 argv、runner identity、returncode 与
  observed HEAD；`run_before_full_suite:true` 的本地关键测试先执行，实际 returncode 必须命中计划冻结的
  `expected_returncodes`，失败不能被 shell 包装成 PASS。
- `prepare_deployment`：仅当计划冻结了已授权真实环境时调用 Core。Core 从 candidate HEAD 创建隔离部署
  worktree，运行项目提供的查询、构建和部署命令，绑定制品 SHA256 与环境状态；不得切换 target
  checkout。若当前 probe 已证明授权目标承载相同制品，Core 会跳过重复 deploy；这不复用旧 blackbox
  结果。
- `tester_blackbox`：在 tester author 与 prove-tests、machine 都 current 后，续接同一 Tester
  thread，在 candidate worktree 的 integrated HEAD 执行结构化 blackbox；逐命令实际 returncode 必须
  命中冻结的 `expected_returncodes`。部署型 run 先调用 `stage-blackbox` 暂存结果，不直接登记 PASS。
- `restore_deployment` / `complete_blackbox`：部署型 run 无论 Tester 通过、失败或控制面中断，都先执行
  项目恢复命令并由 probe 证明回到部署前状态；恢复成功后才把暂存结果绑定制品、环境和恢复事实，登记
  blackbox evidence。跳过 deploy 的事务只重新 probe 并确认环境未漂移，不执行会改变既有环境的恢复
  命令。计划授权 lease 时，`complete_blackbox` 可在当前 probe 与 lease 一致后登记 evidence 并继续
  Reviewer；finalize、abandon 前仍由 Driver 恢复。恢复失败或复用状态漂移只返回用户决定。
- Mission Revision 使用 Core `revise-mission --transition` 原子绑定上一 revision 和 ledger 派生
  `pressure_digest`。新 run 的 `mission.supersedes` 只携带
  candidate snapshot 和 environment lease；Tester/Reviewer 必须创建新 thread 并重建全部 evidence。
  supersession 还必须在任何 worktree/ref/intent mutation 前校验 transition pressure decision 与完整
  `prior_problem_dispositions`；included problem 只继承问题意图和 owner，不继承 producer identity。
  lease 转移或制品不一致恢复由 `complete-supersede-transfer`、`restore-superseded-environment` 自动收敛。
- `reviewer_final`：只有 Tester、proof、machine、blackbox 等全部 reviewer prerequisites 齐全且
  current 后，才用只允许 identity bootstrap 的最小 prompt spawn Reviewer，调用 `prepare-reviewer`
  绑定真实 identity，再 follow-up 同一 thread 开始审查。Reviewer 只使用成熟终态
  `REVIEW_RESULT: pass`、`REVIEW_RESULT: findings` 或 `REVIEW_RESULT: blocked`，并继续返回
  `REVIEW_HEAD` 和 `PROBLEM_REPORT` 唯一终态；主线程只把这些结构字段规范成 v4 evidence report，
  不修改 Reviewer 公共协议。finding 修复后必须 same-thread 续接；不得用
  fresh Reviewer 把旧 finding 洗掉。需要 replacement 时先调用 `prepare-reviewer --replace`，保留旧
  identity 并使 review evidence stale。
- `tester_fix`：结构化问题 owner=tester 时回到 Tester 同一 thread；普通测试修正或 fixture 修正
  不修改 Mission。
- `builder_fix`：结构化问题 owner=builder 时回到 Builder，在原 candidate 修复并 checkpoint。
- `external_problem_decision`：`owner=external_platform` 的 open problem 必须停止 dispatch，通过
  `request_user_input` 取得继续授权；新的外部 probe 成功后调用
  `resolve-external-problem --problem-key <key> --reason <reason>`。不得派 `builder_fix`、重复
  `record-problems`、修改 candidate，或把恢复决定当成 PASS evidence；下一步由 Driver 重跑原 gate。
- `rematerialize_target`：target drift 无冲突时调用 Core 重物化并重验受影响 evidence，不请求用户。
- `recover_finalize`：只重放已经持久化的 finalize intent。
- `architecture_review`：相同 failure signature 连续三次形成 no-progress 才暂停普通重试。
- `finalize`：readiness 全绿后调用 Core 原子收尾，不自行移动 target ref、stash 或删除未知 worktree。

每个 Tester/Reviewer 的非通过结果都必须携带唯一、非空、符合
`schema/codex-problem-report.schema.json` 的 `PROBLEM_REPORT`。主线程调用 `record-problems` 原样登记，
按 owner 路由：`tester` → `tester_fix`，`builder` → `builder_fix`，`plan` → 产品/契约决定，
`external_platform` → 上述授权恢复流程。
交付后事故归属只允许 `current_project` 或 `builder_loop`（外部平台另行记录），不能混成一个问题。

Tester continuity 或 Reviewer continuity 丢失时，优先 same-thread 续接。Tester 只有旧 source clean、
branch/worktree 未漂移时才允许 `prepare-tester --replace`；dirty 或漂移则保留现场并停止。不可恢复的
Agent 连续性不自动改变 Mission。

## 终态事故与记忆

到达 `FINALIZED`、`NEEDS_USER`、`FATAL`、continuity failure 或 abandon 后，统一读取
[交付后事故归属](../builder/references/post-delivery-retrospective.md)，先调用
`assurance retrospective-status --repo <repo> --session-id <session>` 取得所有 terminal root 的 canonical
snapshot，再逐项完成分流并通过 `assurance record-retrospective` 原子记录。Full Driver Skill 调
`consume-dispatch` 时显式传 `--consumer-source full_driver_skill`；operator recovery 传
`--consumer-source operator_recovery`。完成工程问题分流后，才以
`builder-loop delegated` 模式调用 `$memory-review`；不得复制旧版五问或把 memory 当成 Issue、代码、
测试、契约和项目文档的替代品。最终消息必须逐字包含 status 返回的 `required_block`；复盘不重新
打开 gate，不改写 finalized target，也不覆盖非成功终态。

## 唯一用户中断边界

从 `checkpoint_builder`、L1、dirty snapshot、parallel Tester、普通修复和全部 evidence gate，直到
`recover_finalize`，都由循环继续处理，不形成用户中断。

仅以下情况输出最终标记：Mission 或验收目标必须改变；Authority 必须扩大；出现产品取舍；Git 冲突
或可能覆盖用户 dirty；同签名三次 no-progress 后需要设计决定；Tester/Reviewer 连续性不可恢复。
这些边界之外不得因 revision、普通修复或工具轮次结束打断用户。

成功且终态复盘完成后最后输出独立行：

`FULL_DRIVER_V4_RESULT: finalized`

上述 Mission、Authority、产品、Git、no-progress 或连续性边界需要用户决定时，保留全部现场并输出：

`FULL_DRIVER_V4_RESULT: needs_user`
