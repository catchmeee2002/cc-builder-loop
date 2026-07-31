---
name: full-driver-v4-experiment
description: 显式运行 Builder-loop Full Driver v4 实验。由 Codex 原生 Agent 编排 Assurance Core v4，覆盖本地 Git 代码或 L1 文档交付、dirty intake、Tester、proof、机器验证、黑盒、Reviewer、恢复与 finalize。不得作为公共 `$builder` 或普通实施请求的隐式入口。
---

# Full Driver v4 Experiment

这是显式实验入口。Core 只保存 Mission、Authority、Assurance、Execution、证据与 Git 事务事实；
Agent spawn、same-thread follow-up、结构化结果解析和持续循环由本 Skill 使用 Codex 原生能力执行。
不要持久化“下一步做什么”，每轮都重新调用 `driver-next` 派生动作。

## 启动与输入

1. 仅处理单一、本地 Git 交付；远程部署、外部设备和多仓原子事务不在本实验范围。
2. 读取项目 `AGENTS.md`、文档政策和设计哲学，生成四事实面 contract，再运行
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
  behavior 恰好一次且反例为真实 assertion failure 时才记录 proof evidence。
- `verify_machine`：调用 Core `verify-machine`，保留真实 argv、runner identity、returncode 与
  observed HEAD；失败不能被 shell 包装成 PASS。
- `tester_blackbox`：在 tester author 与 prove-tests、machine 都 current 后，续接同一 Tester
  thread，在 candidate worktree 的 integrated HEAD 执行结构化 blackbox；校验逐命令 returncode、
  前后 HEAD、candidate dirty 与 case 覆盖后记录 evidence。
- `reviewer_final`：只有 Tester、proof、machine、blackbox 等全部 reviewer prerequisites 齐全且
  current 后，才用只允许 identity bootstrap 的最小 prompt spawn Reviewer，调用 `prepare-reviewer`
  绑定真实 identity，再 follow-up 同一 thread 开始审查。Reviewer 继续使用成熟的 `REVIEW_RESULT`、
  `REVIEW_HEAD` 和 `PROBLEM_REPORT` 唯一终态；主线程只把这些结构字段规范成 v4 evidence report，
  不修改 Reviewer 公共协议。finding 修复后必须 same-thread 续接；不得用
  fresh Reviewer 把旧 finding 洗掉。需要 replacement 时先调用 `prepare-reviewer --replace`，保留旧
  identity 并使 review evidence stale。
- `tester_fix`：结构化问题 owner=tester 时回到 Tester 同一 thread；普通测试修正或 fixture 修正
  不修改 Mission。
- `builder_fix`：结构化问题 owner=builder 时回到 Builder，在原 candidate 修复并 checkpoint。
- `rematerialize_target`：target drift 无冲突时调用 Core 重物化并重验受影响 evidence，不请求用户。
- `recover_finalize`：只重放已经持久化的 finalize intent。
- `architecture_review`：相同 failure signature 连续三次形成 no-progress 才暂停普通重试。
- `finalize`：readiness 全绿后调用 Core 原子收尾，不自行移动 target ref、stash 或删除未知 worktree。

每个 Tester/Reviewer 的非通过结果都必须携带唯一、非空、符合
`schema/codex-problem-report.schema.json` 的 `PROBLEM_REPORT`。主线程调用 `record-problems` 原样登记，
按 owner 路由：`tester` → `tester_fix`，`builder` → `builder_fix`，`plan` → 产品/契约决定。
交付后事故归属只允许 `current_project` 或 `builder_loop`（外部平台另行记录），不能混成一个问题。

Tester continuity 或 Reviewer continuity 丢失时，优先 same-thread 续接。Tester 只有旧 source clean、
branch/worktree 未漂移时才允许 `prepare-tester --replace`；dirty 或漂移则保留现场并停止。不可恢复的
Agent 连续性不自动改变 Mission。

## 唯一用户中断边界

从 `checkpoint_builder`、L1、dirty snapshot、parallel Tester、普通修复和全部 evidence gate，直到
`recover_finalize`，都由循环继续处理，不形成用户中断。

仅以下情况输出最终标记：Mission 或验收目标必须改变；Authority 必须扩大；出现产品取舍；Git 冲突
或可能覆盖用户 dirty；同签名三次 no-progress 后需要设计决定；Tester/Reviewer 连续性不可恢复。
这些边界之外不得因 revision、普通修复或工具轮次结束打断用户。

成功时最后输出独立行：

`FULL_DRIVER_V4_RESULT: finalized`

上述 Mission、Authority、产品、Git、no-progress 或连续性边界需要用户决定时，保留全部现场并输出：

`FULL_DRIVER_V4_RESULT: needs_user`
