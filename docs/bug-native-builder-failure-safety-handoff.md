# Native Builder Failure Safety Handoff

本文是 `cc-builder-loop` Native Builder 可靠性工作的交接入口，包含失败现场、路线边界和
`Planner + Codex 原生执行`普通计划。本文不是运行事实源；动态状态以 Assurance ledger、Git
worktree 和 `cc-builder-loop #198` 为准。

## 当前结论

Builder-loop 实验 run 没有交付代码。它在 Builder turn 达到 3600 秒总 deadline 后返回
`responseStreamTimeout`；自动续接时 protocol canary 连续超时，run 进入
`NATIVE_DRIVER_PROTOCOL_UNAVAILABLE/FATAL`。

这不是“新修复代码自举后崩溃”：执行该 run 的 runtime 仍是旧的
`e945d5431eaf85ec2fe9f30dc1e874954a97b68f`。候选中的 runtime/schema 修改没有 checkpoint，
不得手工采用、复制、提交或从 failed run 恢复。

当前路线不能把已接受的 Builder handoff 直接降级为 Native 执行。Native 路线必须重新进入
Planner，明确冻结一份普通计划，并从 clean development worktree 开始。

## 现场索引

[保质期: 2026-08-25, owner: Codex, 正向归宿: Assurance ledger、Git refs、cc-builder-loop #198]

- Builder run：`native-builder-failure-safety-20260825-102407`
- Failed ledger：
  `/mnt/hongyu.liao_docker/cc-builder-loop/.git/builder-loop-assurance-v4/runs/native-builder-failure-safety-20260825-102407/ledger.json`
- 未 checkpoint candidate：
  `/mnt/hongyu.liao_docker/cc-builder-loop/.git/builder-loop-assurance-v4/runs/native-builder-failure-safety-20260825-102407/candidate`
- Development worktree：
  `/mnt/hongyu.liao_docker/codex-worktrees/cc-builder-loop-1056d6ccccdb/native-builder-failure-safety-20260825-20260825-102407-78e088`
- Development worktree id：`native-builder-failure-safety-20260825-20260825-102407-78e088`
- Base/runtime commit：`e945d5431eaf85ec2fe9f30dc1e874954a97b68f`
- Candidate dirty paths：`runtime/codex_builder_loop/assurance_v4/core.py`、
  `runtime/codex_builder_loop/native_driver/app_server.py`、
  `runtime/codex_builder_loop/native_driver/coordinator.py`、
  `schema/assurance-v4-ledger.schema.json`
- Retrospective report digest：
  `143324187fedfd7d8a51af68ad46df227b1f975b4b1aef8e62c754c27519f774`
- Issue：`cc-builder-loop #198`，已追加现场并完成 retrospective receipt 回读

## 已确认事实

1. 首次 Builder turn 绑定后运行至 3600 秒总 deadline，返回 `responseStreamTimeout`。
2. candidate branch/worktree HEAD 没有前移，未产生 `builder_checkpointed`、machine、blackbox
   或 Reviewer evidence。
3. candidate worktree 保留未提交修改；由于 `checkpoint-builder` 要求 clean committed
   candidate，不能把它伪装成已交付候选。
4. 同一 run 的自动续接在建立新 generation 前，protocol canary 连续三次
   `NATIVE_APP_SERVER_TIMEOUT`，最终写入 `NATIVE_DRIVER_PROTOCOL_UNAVAILABLE/FATAL`。
5. 失败 run、candidate worktree 和开发 worktree 均未被手工清理或改写。
6. 失败事实已路由到既有 `#198`；不把这次失败当作代码修复成功或长程稳定性证据。

## 设计依据

本计划遵循 [设计哲学](design-philosophy.md)：

- **副作用先持久化**：Builder 可能已修改 candidate，断流后先记录 manifest，再决定恢复。
- **每个事实只有一个家**：ledger/schema 保存执行事实，Git 保存候选历史，Issue 保存事故现场；
  不在普通计划里复制 transcript 或动态状态。
- **Fail-closed 局部化**：clean candidate 可保留既有 retry；dirty candidate 不自动 commit、
  rollback、cleanup 或重放。
- **改输入条件，不堆输出特判**：先压缩失败边界和诊断输入，不增加 retry 次数掩盖长 turn。
- **连续性属于 thread 和事务身份**：Native route 不接管 failed Builder run，也不新建 thread
  冒充旧连续性。

## 普通版计划

执行路线是 `Planner + Codex 原生执行`。本计划不生成 Assurance v4 contract，不输出
`BUILDER_HANDOFF_READY`，不启动 Builder run，也不复用上面的 failed ledger。

### 目标

实现 Native Builder transport failure 的两项最小安全改进：

1. 保存有界、脱敏、可校验的 App Server stderr 和 transport/turn failure observation。
2. Builder 发生 candidate 副作用后，禁止同 action 盲目 retry，并保留精确 dirty manifest。

### 不做什么

- 不改变现有 timeout 数值。
- 不增加 retry 上限。
- 不把 Reviewer compaction 扩展到 Builder。
- 不实现 bounded Builder slice 或自动回滚。
- 不从 failed run 复制未 checkpoint candidate。
- 不宣称本计划完成后即可关闭 #198；真实长程 canary 仍是独立验收。

### 实施顺序

1. **先冻结契约和 schema**
   - 更新 `schema/assurance-v4-ledger.schema.json`，为 dispatch failure 增加可选的
     candidate manifest、manifest digest 和诊断 receipt 字段。
   - 保持旧 ledger 可读；缺少新字段时走 legacy observation。
   - 不新增第二份 transcript/cache 状态。

2. **补强 App Server transport 诊断**
   - 在 `runtime/codex_builder_loop/native_driver/app_server.py` 中使用受控 stderr drain，
     避免 `stderr=PIPE` 阻塞。
   - 保存有界、脱敏的 stderr 摘要、字节数和 SHA-256；凭证、Authorization header 和 token
     不得写入 artifact、ledger、diff 或终端。
   - 将 transport generation、structured turn error、cleanup state 和诊断 receipt 绑定到
     同一 failure event。
   - 保持现有 initialize/request/idle/total timeout 和 protocol canary 语义不变。

3. **加入 Builder side-effect retry gate**
   - 在 `runtime/codex_builder_loop/native_driver/coordinator.py` 中，仅对
     `builder_implement`、`builder_fix`、`builder_recompose_fix` 检查 candidate 前后 manifest。
   - candidate 未变化时保持现有最多三次 retry/generation 行为。
   - candidate 有变化时不调用 `retry-dispatch`，通过既有
     `record-driver-failure`/failed lifecycle 保存现场并阻止重放。
   - 不自动 commit、stash、rollback、删除 dirty 文件或创建新 Builder thread。
   - Reviewer no-output tail compaction 路径保持原样。

4. **补测试与 blackbox**
   - `tests/test_native_driver_v1.py` 覆盖 stderr drain、上限/脱敏、clean retry、
     dirty Builder retry block 和 Reviewer compaction parity。
   - `tests/test_assurance_v4_contract.py` 覆盖 schema、manifest digest、legacy ledger
     兼容、failure replay 幂等和 dirty cleanup block。
   - 新增 `tests/helpers/native_builder_failure_blackbox.py`，通过公共 Native Driver CLI
     模拟“Builder 已写文件后 transport 断流”，验证不重复副作用、不产生 PASS。

5. **文档与验证**
   - `docs/architecture.md` 只记录稳定的 failure observation/retry boundary。
   - `CHANGELOG.md` 记录过去式改动。
   - 不修改 `docs/design-philosophy.md`；原则没有变化。
   - 运行 focused tests、blackbox、schema/compile 检查和完整
     `bash scripts/verify-all.sh`。

### 验收标准

- stderr drain 不因输出量造成 App Server 或 Driver 阻塞。
- 脱敏后的 diagnostic receipt 可与 generation/cleanup/failure digest 对齐。
- clean Builder failure 保持现有 retry 上限和连续性绑定。
- dirty Builder failure 不再重放同一 action，且 ledger 记录 candidate path/content manifest。
- Reviewer 既有 compaction 测试全部保持通过。
- 旧 ledger 可读取，新旧 failure replay 的 digest 冲突会 fail closed。
- 失败或宿主压力只形成明确 failure/NEEDS_USER，不被转换为 PASS。

## 新 session 交接步骤

1. 先检查宿主 load、D-state、磁盘等待和当前 Git worktree；没有稳定 admission 不启动长 turn。
2. 读取本文件、`docs/design-philosophy.md`、`docs/architecture.md`、`doc-policy.md` 和
   当前 `AGENTS.md`。
3. 使用 `dev-worktree status --id
   native-builder-failure-safety-20260825-20260825-102407-78e088`。
4. 只使用 clean development worktree：
   `/mnt/hongyu.liao_docker/codex-worktrees/cc-builder-loop-1056d6ccccdb/native-builder-failure-safety-20260825-20260825-102407-78e088`
5. 不 resume、supersede、abandon 或 cleanup
   `native-builder-failure-safety-20260825-102407`；不搜索或采用其 dirty candidate。
6. 重新进入 Plan，选择 `Planner + Codex 原生执行`，使用本节普通计划生成普通实施计划。
7. 按“契约/schema → runtime → tests → docs → 全量验证”顺序实施。
8. 只有 clean commit、focused/full tests、blackbox、Reviewer/doc review 和 diff 检查都绑定
   同一 candidate 后，才允许提交或合并。
