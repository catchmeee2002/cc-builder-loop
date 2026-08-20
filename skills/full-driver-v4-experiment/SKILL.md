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
   `codex-builder-loop assurance --experimental-v4 validate --repo <repo>` 与 `start`。
   初始 `execution.agents` 保持为空；只有原生 spawn 返回真实 identity 后，才通过
   `prepare-tester` / `prepare-reviewer` 写入，不能预填或猜测 agent/thread id。Full Driver contract
   必须设置 `execution.driver_enforced=true`，使缺失 `action_id` 也无法绕过当前 Driver action。
3. `mission.delivery_kind=documentation` 是 L1：只保留 Reviewer/doc-review，不创建 Tester、machine
   或 blackbox 假证据。
   其他代码交付默认冻结 `tester`、`proof`、`machine`、`blackbox`、`reviewer` 五个独立 gate；只有
   用户明确改变 assurance 强度时才可增减，并默认设置 `reviewer_preflight:true`。纯 L1 保持 false。
4. Authority 中已授权 dirty intake 由 Core 校验 state digest，并复制到隔离 candidate 的
   `dirty_snapshot`；目标 worktree 保持原样。已授权 dirty 不暂停用户。
5. `mission.delivery_kind=preparation` 可修改 exact `protected_support_paths`，经 Reviewer 与 finalize
   形成 protected preparation。后续 business contract 使用 `execution.continuation` 单次消费；同一
   continuation token 的重复或二次 replay 必须由 Core 拒绝，不能复制旧 evidence 或 transcript。
   self-hosted `runtime_support` 冲突时只接受 Core 返回的 exact affected paths，移除 affected gate 并保留
   independent gates；不得 resume 当前 run、热切换 evidence writer 或补造旧 proof observation。

## 角色隔离与连续性

### Tester 初次 spawn

每个身份只允许一次初始 `spawn_agent(agent_type="tester", fork_turns="none")`，一个 run 只 spawn 一次 Tester。
最小 brief 只包含 `run_id`、冻结 `contract`、Tester worktree、spec head 或隔离 publication manifest，
以及 author/blackbox 阶段目标；不得夹带父线程讨论、用户倾向、Builder 辩护或候选信息。

### Reviewer 初次 spawn

到达 `reviewer_preflight` 或 `reviewer_final` 后只允许一次初始
`spawn_agent(agent_type="reviewer", fork_turns="none")`，一个 run 只 spawn 一次 Reviewer。最小 brief
包含冻结 contract、candidate、完整 diff、当前阶段可用验证证据和文档政策路径；
不得夹带父线程讨论、用户倾向或 Builder 辩护。Reviewer 必须看到候选信息、当前
`doc_reference_scan` 及其 semantic checks；候选与完整 diff 是必需审查输入。

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
- Full Driver fallback 每次角色 turn 先用 `begin-dispatch` 绑定当前 `action_id`、role/thread、
  prompt/output digest，并显式传 `--driver-runtime-kind full_driver_skill`；真实 turn 开始后再
  `bind-dispatch-turn`，结果用 `complete-dispatch` 落盘并以
  `consume-dispatch --consumer-source full_driver_skill` 消费。Native transport 字段只由 Native Driver
  owner 绑定，不能由 fallback 冒充。
- `checkpoint_builder`：Builder 工作完成后先提交 candidate，再调用 `checkpoint-builder`，让 Core
  计算并绑定 candidate HEAD、builder files 和 evidence invalidation；Skill 不手写 Execution 清单。
- `scan_doc_references`：每次 candidate identity 变化后、进入 Tester 或 Reviewer 前调用 Core
  `scan-doc-references`。Core 只读取 `target_start_head..candidate_head` 的 Git objects，机械检查
  Python、JavaScript/TypeScript、Go、Rust 与 Shell 定义迁移、删除或重命名后残留的
  `old_path::symbol` Markdown 指针。硬 finding 回 `builder_fix`；symbol-only 提及作为 semantic check
  交给 Reviewer；scanner error 停到用户决定。不得用 live dirty 文件、grep 摘要或 Agent 自述替代该
  ledger 事实。
- `parallel_ready:true`：先用只允许 identity bootstrap、禁止读写仓库的最小 prompt spawn `tester`
  custom agent；使用工具返回的真实 identity 调用 `prepare-tester`，再 follow-up 同一 thread 执行
  `phase=author`。Tester 以 spec HEAD 为独立基线。
- `parallel_ready:false`：先 `publish-prerequisites`，再 spawn Tester 并以真实 identity 调用
  `prepare-tester`；Tester 只能从隔离
  publication HEAD 读取 exact file/blob manifest。publication path 集合冻结；每个 generation 的 manifest
  与 blob 不可变。后续发现 prerequisite 需要修复时只能经 Core 重组事务生成下一 generation，并让 Tester
  source 与全部受影响 evidence 对新 candidate 重新绑定。
- `tester_author`：首次 spawn Tester；后续 correction 使用同一 thread 续接。收到
  `TESTER_RESULT` 与 `PROBLEM_REPORT` JSON 后先校验身份和 schema；`tests_ready` 才调用
  `integrate-tester` 并记录 tester evidence。
- `prove-tests` / `tester_proof`：由同一 Tester thread 对冻结 behaviors 产出 proof spec；主线程把
  spec 和真实 Tester identity 交给实验 namespace 的 `prove-tests --action-id <action_id>`。Core 按
  公共 `schema/codex-test-proof.schema.json` 先运行全部 candidate group；任一失败时在首个
  group/result 兼容字段外附有序 `failures`，且不运行 baseline-red/mutation。全部 candidate 通过后才
  执行既有反例语义，只有每个 behavior 恰好一次且反例为真实 assertion failure 时才记录 proof evidence。命令非零后先读回
  `status.proof_failure`：只有它仍为 current、绑定同一 action/code 时，才把本次 Core failure 视为已
  持久化并继续调用 `driver-next`；否则保持原错误并停止。Native completed dispatch 在恢复时若已精确绑定
  同一 action/spec/Tester 的 current failure，直接消费该持久化事务，不再次进入 proof gate 或追加 event。
- `tester_proof_diagnose`：续接原 Tester thread，只把 Driver 返回的 current `proof_failure` 交给
  `phase=proof_diagnose`。本 turn 只归因、不改文件、不自行重跑：仅 proof execution input 需修正时，
  返回 `result=tests_ready` 和 digest 已变化的 replacement `proof_spec`，主线程以同一 diagnosis
  `action_id` 调用 `prove-tests`，不得调用 `integrate-tester` 或新增 problem；candidate、Tester source 或
  contract 需要变化时才返回非空 problem，owner 仅为 builder、tester 或 plan，并调用
  `record-problems --action-id <action_id>`。`proof_failure_decision` 直接停止到用户，不把环境、身份或
  完整性错误猜成角色修复。
- `verify_preflight`：在 Tester integration 后运行所有 `run_before_full_suite:true` 的 focused machine
  commands。结果绑定当前 candidate；最终 `verify_machine` 只能复用同 candidate、同 dependency 的真实
  command result，不能把 preflight 名义升级为整套 machine PASS。
- `verify_machine`：调用 Core `verify-machine`，保留真实 argv、runner identity、returncode 与
  observed HEAD；`run_before_full_suite:true` 的本地关键测试先执行，实际 returncode 必须命中计划冻结的
  `expected_returncodes`，失败不能被 shell 包装成 PASS。
- `tester_machine_diagnose`：续接原 Tester thread，只读 current `machine_failure` 归因，不改文件、不重跑
  machine。问题按 builder、tester、plan、current_project、builder_loop 或 external_platform 拆分后
  `record-problems`；同 signature 三次才进入 architecture review。
- `prepare_deployment`：仅当计划冻结了已授权真实环境时调用 Core。Core 从 candidate HEAD 创建隔离部署
  worktree，运行项目提供的查询、构建和部署命令，绑定制品 SHA256 与环境状态；不得切换 target
  checkout。若当前 probe 已证明授权目标承载相同制品，Core 会跳过重复 deploy；这不复用旧 blackbox
  结果。
- `tester_blackbox`：在 tester author 与 prove-tests、machine 都 current 后，续接同一 Tester
  thread，在 candidate worktree 的 integrated HEAD 执行结构化 blackbox；逐命令实际 returncode 必须
  命中冻结的 `expected_returncodes`。新 observation contract 还必须逐 case 回传相同 surface、实际
  execution ids、外部 target 和三个判据维度；缺 case、错表面或代理 evidence 都不能登记 PASS。
  部署型 run 先调用 `stage-blackbox` 暂存结果，不直接登记 PASS。
- `restore_deployment` / `complete_blackbox`：部署型 run 无论 Tester 通过、失败或控制面中断，都先执行
  项目恢复命令并由 probe 证明回到部署前状态；恢复成功后才把暂存结果绑定制品、环境和恢复事实，登记
  blackbox evidence。跳过 deploy 的事务只重新 probe 并确认环境未漂移，不执行会改变既有环境的恢复
  命令。计划授权 lease 时，`complete_blackbox` 可在当前 probe 与 lease 一致后登记 evidence 并继续
  Reviewer；finalize、abandon 前仍由 Driver 恢复。恢复失败或复用状态漂移只返回用户决定。
- 已授权且 Core `update-facet` 或 `revise-mission --transition` 能安全表达的 plan decision，必须先用
  `validate-decision` 绑定同 session、problem、action id、base facet digest 和唯一完整 replacement
  contract，再把相同 action/session/facet digest binding 传给 mutation，在同一 active run 原子收敛并
  resume；普通执行信息变化不触发 abandon 或新 run。Mission
  Revision 绑定上一 revision 和 ledger 派生 `pressure_digest`。
- 只有现有事务不能保持语义、授权或事务安全时才交给 Planner 形成 Assurance v4 successor。source 在
  successor contract 验证和 `start` 持久化 target 前必须保持 active，Driver 不得先调用 `abandon`；
  `start` 创建 target 后才把 source 封为 superseded。新 run 的 `mission.supersedes` 只携带 candidate
  snapshot 和 environment lease；Tester/Reviewer 必须创建新 thread 并重建全部 evidence。
- supersession 必须在任何 worktree/ref/intent mutation 前校验 source active、transition pressure decision
  与完整 `prior_problem_dispositions`；included problem 只继承问题意图和 owner，不继承 producer identity。
  lease 转移或制品不一致恢复由 `complete-supersede-transfer`、`restore-superseded-environment` 自动收敛。
  abandoned、superseded、failed 或 finalized source 都是 terminal，其 continuity 不可恢复，也不重新激活或
  rescue；必须在 target ledger/ref/worktree mutation 前拒绝。`abandon` 只用于用户明确取消且没有
  successor。legacy v2/v3 的 abandoned-source revision 行为不由本 v4 路由改写。
- `reviewer_preflight`：配置开启时，在 Tester integration 与 focused preflight 后用同一 Reviewer thread
  做早期只读代码/测试/文档语义审计。PASS 只记录 `reviewer_preflight` evidence，不满足最终 gate；finding
  回原 owner 修复并让失效事实重取。
- `reviewer_final`：只有 Tester、proof、machine、blackbox 等全部 reviewer prerequisites 齐全且
  current，且当前 candidate 的 `doc_reference_scan` 无 scanner error 或 broken qualified pointer 后，
  才用只允许 identity bootstrap 的最小 prompt spawn Reviewer，调用 `prepare-reviewer`
  绑定真实 identity，再 follow-up 同一 thread 开始审查。Reviewer 只使用成熟终态
  `REVIEW_RESULT: pass`、`REVIEW_RESULT: findings` 或 `REVIEW_RESULT: blocked`，并继续返回
  `REVIEW_HEAD` 和 `PROBLEM_REPORT` 唯一终态；主线程只把这些结构字段规范成 v4 evidence report，
  不修改 Reviewer 公共协议。finding 修复后必须 same-thread 续接；不得用
  fresh Reviewer 把旧 finding 洗掉。需要 replacement 时先调用 `prepare-reviewer --replace`，保留旧
  identity 并使 review evidence stale。
- `tester_fix`：结构化问题 owner=tester 且未声明 `producer_continuity=invalid` 时回到 Tester 同一
  thread；普通测试修正或 fixture 修正不修改 Mission。
- `replace_tester`：只有当前 Tester producer 自己报告 owner=tester 且
  `producer_continuity=invalid` 时启用。先调用 `begin-tester-replacement` 持久化唯一 intent，再建立并
  `bind-tester-replacement` 新 identity，最后 `complete-tester-replacement` 切换 source；新 Tester
  首个 `tester_author` turn 由 `bind-dispatch-turn` 原子解决对应问题。首 turn 前 App Server 返回
  `no rollout found` 时，以同一 intent 调 `begin-tester-replacement --renew-bootstrap`，最多三次；已
  有 turn/source/evidence 后不得续换。candidate、target、旧 source、pending dispatch 或其他 intent
  漂移时零副作用停止。
- `builder_fix`：结构化问题 owner=builder 时回到 Builder，在原 candidate 修复并 checkpoint。
- `recompose_candidate`：恢复或推进已持久化的 target/publication candidate 重组事务。所有 Git 副作用前
  intent 已落盘；target 再前进时从最新 target 重启，最终以 candidate/Tester source/target CAS 提交。
- `builder_recompose_fix` / `tester_recompose_fix`：分别续接原 Builder/Tester thread，在各自 staging
  worktree 和 ownership 内解决冲突并提交，再回 `recompose-candidate`；Reviewer 不参与修复。需要改变
  Mission、Authority、Tester 判据或产品选择时记录 owner=plan problem。
- `builder_loop_problem_decision` / `current_project_problem_decision`：对应 owner 的 open problem 保持
  active candidate 并立即停止全部 Agent dispatch，等待用户选择 abandon 或仓库外救援；不得默认派回
  Builder，也不得把 run 伪造为 failed。
- `external_problem_decision`：`owner=external_platform` 的 open problem 必须停止 dispatch，通过
  `request_user_input` 取得继续授权；新的外部 probe 成功后调用
  `resolve-external-problem --problem-key <key> --reason <reason>`。不得派 `builder_fix`、重复
  `record-problems`、修改 candidate，或把恢复决定当成 PASS evidence；下一步由 Driver 重跑原 gate。
- `rematerialize_target`：兼容别名；新 Driver 统一调用 `recompose-candidate`。
- `recover_finalize`：只重放已经持久化的 finalize intent。
- `complete_driver_failure`：run 创建后的未处理 FATAL 已由 `record-driver-failure` 冻结 action、dispatch
  与 candidate observation。优先恢复 finalize intent，其次恢复 deployment/lease；只有环境安全后才进入
  failed。恢复失败保持 recovering/NEEDS_USER，不得 abandon、切换控制器或继续 Agent dispatch。
- `architecture_review`：相同 failure signature 连续三次形成 no-progress 才暂停普通重试。
- `finalize`：readiness 全绿后调用 Core 原子收尾，不自行移动 target ref、stash 或删除未知 worktree。

每个 Tester/Reviewer 的非通过结果都必须携带唯一、非空、符合
`schema/codex-problem-report.schema.json` 的 `PROBLEM_REPORT`。主线程调用 `record-problems` 原样登记，
按 owner 穷举路由：普通 `tester` → `tester_fix`，当前 Tester producer 明确
`producer_continuity=invalid` → `replace_tester`，`builder` → `builder_fix`，`plan` → 产品/契约决定，
`external_platform` → 上述授权恢复流程，`builder_loop` / `current_project` → 专属 NEEDS_USER decision。
交付后事故归属只允许 `current_project` 或 `builder_loop`（外部平台另行记录），不能混成一个问题。

Tester continuity 或 Reviewer continuity 丢失时，优先 same-thread 续接。Tester transport identity
丢失但 producer 仍可信时沿用既有 `prepare-tester --replace` 边界；Tester 已失去独立 author 资格时只走
上述 `replace_tester` 持久事务。dirty 或漂移则保留现场并停止。不可恢复的 Agent 连续性不自动改变
Mission。

## 终态事故与记忆

到达 `FINALIZED`、`NEEDS_USER`、`FATAL`、continuity failure 或 abandon 后，统一读取
[交付后事故归属](../builder/references/post-delivery-retrospective.md)，先调用
`assurance retrospective-status --repo <repo> --session-id <session>` 取得所有 terminal root 的 canonical
snapshot，再逐项完成分流并通过 `assurance record-retrospective` 原子记录。Full Driver Skill 调
`consume-dispatch` 时显式传 `--consumer-source full_driver_skill`；operator recovery 传
`--consumer-source operator_recovery`。完成工程问题分流后，才以
`builder-loop delegated` 模式调用 `$memory-review`；不得复制旧版五问或把 memory 当成 Issue、代码、
测试、契约和项目文档的替代品。最终消息必须逐字包含 status 返回的 `required_user_block`；完整
`required_block` 只保留为结构化审计事实。复盘不重新打开 gate，不改写 finalized target，也不覆盖
非成功终态。

## 唯一用户中断边界

从 `checkpoint_builder`、L1、dirty snapshot、parallel Tester、普通修复和全部 evidence gate，直到
`recover_finalize`，都由循环继续处理，不形成用户中断。

仅以下情况输出最终标记：Mission 或验收目标必须改变；Authority 必须扩大；出现产品取舍；Git 冲突
或可能覆盖用户 dirty；同签名三次 no-progress 后需要设计决定；Tester/Reviewer 连续性不可恢复。
用户授权决定后优先通过同一 active run 的既有事务恢复；只有确需 successor 时才按上述
active-to-superseded 交接。这些边界之外不得因 revision、普通修复或工具轮次结束打断用户。

成功且终态复盘完成后最后输出独立行：

`FULL_DRIVER_V4_RESULT: finalized`

上述 Mission、Authority、产品、Git、no-progress 或连续性边界需要用户决定时，保留全部现场并输出：

`FULL_DRIVER_V4_RESULT: needs_user`
