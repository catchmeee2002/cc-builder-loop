---
name: builder
description: 显式执行已接受的 builder-loop 方案，在隔离 worktree 中协调 builder、独立 tester 与只读 reviewer，依次完成角色边界检查、测试集成、机器验证、黑盒复验、代码与文档审计及安全收尾。仅当用户明确输入 $builder 时使用；不要隐式触发，也不要用于只读分析或普通代码修改。
---

# Builder

把主线程作为唯一协调者和业务实现写入者。不要复制 runtime 状态；以 run ledger 为唯一来源。

## 准备运行

1. 确认用户显式调用了 `$builder`，并取得同一聊天中已接受的 Plan mode 最终方案。
2. 读取适用的 `AGENTS.md`、设计哲学和项目文档政策；项目未声明政策时读取
   `${CODEX_HOME:-$HOME/.codex}/builder-loop/doc-policy.md`。政策不可读时停止，不自行发明
   替代规则。
3. 在第一次使用每个子命令前分别运行其 `--help`：
   `plan-validate`、`start`、`role-check`、`publish-prerequisites`、`integrate-tests`、`verify`、`status`、
   `record-evidence`、`finalize`、`abandon`。当前帮助优先于本文件中的调用示例。
4. 对每次 runtime 调用只解析 stdout 最后一行 JSON。要求存在 `status` 与 `message`；
   非 JSON、缺字段或命令异常一律视为 `FATAL`，不要根据前面的日志猜测结果。
5. 生成一个只含小写字母、数字和连字符的唯一 `run_id`，在启动 run 的目标 worktree 根创建
   `.builder-loop/codex/inbox/`，将接受的方案原样物化为 `<run_id>.md`，并在顶部加入：

```text
[保质期: run 完成, owner: builder-loop, 正向归宿: .builder-loop/codex/runs/<run_id>/ledger.json]
```

6. 先用 `plan-validate --repo <repo> --plan <path>` 重验物化文件，并确认返回的
   `effective_verification_source` 符合 `spec_head`：有 `.claude/loop.yml` 时必须是该文件，
   否则必须是 `plan:test_context.runner`；L1 必须是 `none`。仅在 `status=READY` 时继续。
7. 从 SessionStart developer context 取得 `session_id`。执行
   `start --repo <repo> --run <run_id> --session-id <session_id> --plan <path>`。不得省略
   session id，也不要自行增加当前 `--help` 未声明的参数。要求 `status=READY`，保存返回的
   `ledger_path`。
8. 立即在 commentary 输出 `BUILDER_LOOP_RUN_ID:<run_id>`。后续从 ledger 读取 builder
   与 tester worktree、分支和证据 HEAD，不在 prompt 或旁路文件缓存副本。
9. 从 ledger 读取 `plan.level`。只有 runtime 明确返回 `L1` 时才走下方纯文档
   分支；不得由 Builder 自行把可执行行为变更降级为 L1。

## L1 纯文档分支

1. 从 frozen `documentation-spec.ownership.builder_write` 读取精确文档边界，只在 builder
   worktree 实施已接受的文档方案，小步提交；不得自行扩成所有 Markdown。
2. 执行 `role-check --run <run> --role builder`。只接受 `status=READY`。
3. 不 spawn tester，不执行 tester role-check、`integrate-tests`、机器 `verify` 或
   黑盒复验，也不记录伪造的 verified/e2e evidence。
4. 直接进入「审查与收尾」，向 reviewer 明确传入
   `verification_mode=L1-documentation-only`。

## 独立写测与实现

`plan.level=L1` 时跳过本节。其他计划执行：

1. 从 ledger 读取 `parallel_ready`、tester worktree 和冻结测试目标。
2. `parallel_ready=true` 时立即 spawn 一次 `tester` custom agent，并在其运行期间实现
   `ownership.builder_write` 内的代码。
3. `parallel_ready=false` 时，从 frozen `test_context.public_prerequisites` 读取公开前置产物，
   先在 builder worktree 只产出这些精确普通文件，不提前写其他实现；执行
   `publish-prerequisites --run <run>`，只接受 `READY` 或同一 publication 的幂等 `NOOP`。runtime
   会自动 checkpoint、验证最终 diff、合成 parent 为 `spec_head` 的隔离 publication HEAD，并
   返回 head/tree/manifest/files。随后才 spawn Tester，只传 publication 元数据、tester worktree、
   冻结接口和黑盒入口，不传 Builder HEAD、candidate diff 或其他实现。已发布文件在本 run
   不可再改；后续实现必须落到其他 Builder-owned 文件。
4. spawn Tester 时传入 `phase=author`、`run_id`、`plan_path`、tester worktree、完整
   `unit-test-spec`、`e2e-cases`、`parallel_ready` 及允许查看的公开前置产物。保存返回的
   tester agent/thread id；一个 run 只 spawn 一次 Tester。
   首次 spawn 由 `SubagentStart` 绑定 thread；此后每次调用 `followup_task` 前必须先调用
   `prepare-follow-up --run <run> --role <role> --agent-id <id> --purpose <purpose>`。Tester 写测或
   澄清用 `purpose=author`，黑盒复验用 `purpose=blackbox`，Reviewer 复审用
   `purpose=review`。只有 `READY` 或同一 pending turn 的幂等 `NOOP` 才能发送 follow-up；不得
   直接修改 ledger，也不得在 prepare 失败后继续发送。
5. Builder 在 `ownership.builder_write` 内实现代码，并按已读取的文档政策判断、同步计划授权的
   受影响文档；首次 Reviewer 前完成。要求并行计划的 Tester 从 `spec_head`、串行计划的 Tester
   从 publication HEAD 出发并核对 manifest；两者都只在 `ownership.tester_write` 内独立写测并
   提交。Builder 不读取未提交的 Tester 测试来反向迎合
   断言，也不修改 `tester_write`、runner、测试配置或 runtime 自动保护的验证控制文件。
6. 等待 Tester 返回；只有最后一行 `TESTER_RESULT: tests_ready` 且包含 tester commit/head、
   changed paths 和最小执行证据时才继续。
7. Tester 报 `target_change_required` 时冻结当前 run，不修改 plan 或测试目标。主线程使用
   `request_user_input` 提供三类选择：保持冻结目标并先以 `purpose=author` prepare、再 follow-up
   同一 Tester 澄清；abandon；或
   修订契约。选择修订时先 `abandon --run <run>` 保留现场，再要求用户 `/plan` 生成更高
   `plan_revision` 的新方案并重新调用 `$builder`；新 run 不冒充旧 Tester thread。
8. 对 builder 与 tester 分别执行 `role-check --run <run> --role <role>`。仅
   `status=READY` 可继续；`NEEDS_USER` 必须按上一步请求用户，禁止自行放宽 ownership
   或跳过 reward-hacking 告警。
9. 执行 `integrate-tests --run <run>`。只接受 `READY` 或明确的 `NOOP`；冲突或失败时
   保留 worktree 和 ledger，按 `message` 处理，不手工伪造已集成状态。

## 验证与同线程黑盒复验

`plan.level=L1` 时跳过本节。其他计划执行：

1. 执行 `verify --run <run>`。runtime 必须在当前 candidate commit 派生的临时干净 worktree
   中运行机器判据；Builder 不在验证 worktree 中修代码或保留运行产物。
2. `status=FAIL` 时读取返回的 `stage` 与 `log_path`，按 ownership 路由：Builder-owned
   实现/文档错误由 Builder 修；冻结目标不变的 Tester-owned 测试或测试支持实现错误，先以
   `purpose=author` prepare，再 follow-up 同一 Tester 进入 author correction，返回新的
   `tests_ready` 后重新 role-check 和
   `integrate-tests`；需要改变测试目标、ownership 或验收标准时 abandon/new plan。不得让
   Builder 越界修改测试或受保护 runner 控制文件。修复后再重跑。
   返回 `iteration_limit_reached=true` 时停止当前 frozen run，不继续调用 verify。使用选项卡让
   用户选择 abandon，或先 abandon 保留现场、再 `/plan` 提升 `plan_revision` 并重新调用
   `$builder`；不得在同一 run 内重置计数或批准修订。
3. `status=FATAL` 时停止；不要把“判据未执行”当作测试失败，更不要返回 PASS。
4. `status=PASS` 后先执行
   `prepare-follow-up --run <run> --role tester --agent-id <tester_id> --purpose blackbox`，再
   follow-up 同一个 tester thread，传入 `phase=blackbox`、集成 HEAD、
   `candidate_worktree` 的绝对路径、runner 和公开黑盒入口。所有非 L1 run 都必须执行本步骤；
   禁止 spawn 新 Tester。
5. 要求 Tester 先在 `candidate_worktree` 执行 `git rev-parse HEAD` 并核对 integrated HEAD，
   再只从公开接口、CLI/API/UI、运行时输出和测试结果做黑盒验收；不读取实现 diff、不改业务
   源码、不改既定测试目标。日志、截图、缓存优先写到 candidate 外的临时目录；结束前清理本 turn
   产生的 tracked/untracked/ignored residue，并同时核对普通 status 与 ignored files 均为空。
   返回实际 command、数值 returncode、执行前后 HEAD、worktree 路径和 `candidate_dirty=false`。
6. Tester 报 FAIL 且可重放公开行为确为产品实现错误时由 Builder 修实现，重新 `role-check`
   和 `verify`，再以 `purpose=blackbox` prepare 并 follow-up 同一 Tester；若失败来自 Tester 的
   执行方法或测试支持实现，则仍以 `purpose=blackbox` prepare 后续接同一 Tester，不让 Builder
   修改 tester-owned 内容。
7. Tester 提议改变测试目标、ownership 或验收标准时，执行上一节的 frozen-run 协议；即使
   用户选择修订，也不得在当前 run 更新测试。新方案必须进入新 run。
8. 只有同一 Tester thread 返回 `TESTER_RESULT: pass` 后，才对当前 integrated HEAD 执行
   `record-evidence --run <run> --kind e2e_verified --head <head> --agent-id <tester_id>
   --details '<json>'`。details 必须原样包含 Tester 返回的 `candidate_worktree`、`head_before`、
   `head_after`、`command` 和数值 `returncode=0`；runtime 会绑定 live candidate。只接受
   `READY` 或幂等 `NOOP`。

## 审查与收尾

1. 非 L1 计划在 Tester author `tests_ready`、机器验证和同 thread blackbox `pass` 均通过后，
   spawn 一次
   `reviewer` custom agent；L1 计划在 builder role-check 通过后 spawn。传入 plan、
   candidate/integrated HEAD、相对 `spec_head` 的完整 diff、与 plan level 匹配的验证
   证据、`verification_mode` 和文档政策路径。串行计划还传入 publication head/tree/manifest、
   exact files 及 Tester author manifest attestation。Reviewer turn 开始前再次确认这些 gate 已绑定
   当前 candidate；不能先启动 Reviewer 再补机器或 blackbox evidence。
2. 要求 reviewer 依次执行 Phase 0 方案完成度、代码缺陷、测试完整性与防篡改、Phase D
   文档政策审计。等待最后一行 `REVIEW_RESULT`。
3. Reviewer 有 actionable findings 且冻结契约不变时，按 ownership 路由：Builder-owned
   实现/文档由 Builder 修；Tester-owned 测试实现或测试支持由同一 Tester author correction
   修正、重新 `tests_ready` 并集成。随后执行
   `prepare-follow-up --run <run> --role reviewer --agent-id <reviewer_id> --purpose review`，再
   follow-up 同一 Reviewer thread 复审，不新建 reviewer。
   非 L1 必须重新 `role-check`、`verify` 和 tester blackbox；L1 只重新执行 builder
   role-check，不为通过门禁临时添加虚假测试。
   finding 需要改变测试目标、ownership 或验收标准时，先 abandon，再进入新 plan/new run。
4. reviewer 或 tester 要求用户决策时，先调用 `status --run <run>`，再使用
   `request_user_input`。在等待前独立一行输出 `BUILDER_INPUT_REQUIRED:<run_id>`。
   当前 surface 没有 `request_user_input` 时，保留该 marker，停止并明确要求切换到可使用
   选项卡提问的 Plan mode；不要降级成纯文本问卷。
5. reviewer 四个 phase 通过后，对同一 integrated HEAD 分别调用
   `record-evidence --run <run> --kind reviewed --head <head> --agent-id <reviewer_id>` 与
   `record-evidence --run <run> --kind doc_reviewed --head <head> --agent-id <reviewer_id>`。
   所有证据必须指向同一个 HEAD；只经 runtime 写入，不直接编辑 ledger。
6. 再调用 `status --run <run>`，确认没有 missing/stale gate 且
   `delivery_gates_ready=true`。从计划生成简洁的 Conventional Commit 消息；当
   `ready_to_stage_final=true` 或 `ready_to_finalize=true` 时调用
   `finalize --run <run> --message <message>`。runtime 先在临时 ref/worktree 执行目标仓库 hooks、
   核对 parent/tree 并持久化唯一 final intent，再在没有 `finalize_blockers` 时以 expected-old CAS
   更新目标分支。
7. `FINAL_COMMIT_STAGED_TARGET_BLOCKED` 表示已冻结 `staged_final_head`，不是 gate 失败。只向用户
   展示 `finalize_blockers` 中的风险路径，请其处理后重试同一 `finalize`，或由用户明确选择
   abandon；不要要求删除 `target_residue` 中未被列为 blocker 的 ignored/untracked 文件，也不要
   重跑 hooks、Tester、验证或 Reviewer。仅 `status=COMPLETE` 或幂等 `NOOP` 表示成功。
8. 其他 `CONFLICT`、`CONTINUITY_FAILURE`、`NEEDS_USER`、`FATAL` 或
   `FATAL_AMBIGUOUS` 时保留现场，并根据 `message` 请求用户决定。只有用户明确放弃时才调用
   `abandon --run <run>`。
9. 成功时最后一行输出 `BUILDER_RESULT: pass run_id=<run_id>`；已按用户决定放弃时输出
   `BUILDER_RESULT: abandoned run_id=<run_id>`；需要用户时输出
   `BUILDER_RESULT: needs_user run_id=<run_id>`。不要在 runtime 未完成时声称交付完成。
