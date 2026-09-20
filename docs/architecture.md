# builder-loop 架构

## 边界

```
Claude Code 原生                        builder-loop runtime（bl）
─────────────────                       ─────────────────────────
主 session（/builder） ──bl start────▶ contract 冻结、候选 + tester 两个 worktree、ledger、session 绑定
   ├─ Agent(tester, 后台) ─SubagentStart▶ 登记 agent_id；只注入 tester worktree / behaviors / 写边界
   │      │  在冻结基线上盲写测试（PreToolUse 拦它读候选）
   │      └──SubagentHandback / SubagentStop▶ 提交 tester worktree → 校验 proof_spec 结构 → tester evidence
   │ Write/Edit 候选 ──────checkpoint──▶ 按角色写边界校验 + 提交（唯一进入候选的途径）
   │                 ──────integrate───▶ tester 的测试按路径叠进候选（此后 tester 可读实现）
   │                 ──────machine─────▶ pass_cmd 在候选内执行 → machine evidence
   │  SendMessage(tester) 补 mutation patch ─▶ Start 再次触发（新一轮），交卷后 proof_spec 更新
   │                 ──────proof───────▶ 冻结的 proof_runner + junit 逐用例判定 → proof evidence
   ├─ Agent(reviewer) ────SubagentStart▶ 记下审查起点的候选 HEAD；注入 diff 范围与前置 evidence
   │      └──SubagentHandback / SubagentStop▶ reviewer evidence（findings 带 owner）
   ├─ Stop hook ◀─────────────────────── 未终态 → exit 2 + next_action；等用户 / 等在跑的 agent → 放行
   │                 ──────finalize────▶ commit-tree + update-ref CAS → 删两个 worktree → terminal
   └─ Stop hook ◀─────────────────────── 终态未复盘 → exit 2
                     ──────retro record▶ 复盘入 ledger → session 解绑
```

runtime 不保存"下一步让谁做"，也不保存"角色是否在跑"：`readiness()` 每次从 evidence、git 与事件流派生。ledger 唯一写入者是 `bl`（`ledger.mutate` flock + seq+1 + 原子写）；绕过 checkpoint 的提交会在下一次 `assert_identity` 时被拒。runtime 的内部 git 操作统一 `-c core.hooksPath=/dev/null`——checkpoint / integrate / rebase 是事务记录不是交付提交；只有 `finalize --run-commit-hook` 那一次跑目标仓库的 hooks。

## 为什么 tester 看不到实现

proof 只能证明"测试能抓住偏离当前实现"，证明不了"当前实现符合 behavior"：看着实现写的测试会把实现的 bug 一起抄进断言，mutation 照样通过。独立性必须来自信息隔离（原则一）。做法：

- tester 有自己的 worktree，分支从 run 起点（`tester.base`，不可变）长出，**永不 rebase**；contract 是它唯一的输入，所以 behaviors 带 `boundaries` / `invariants`，接口签名写进 `interfaces`。
- 两段式：盲写阶段新接口写不出 mutation patch（看不到要破坏什么），`patch` 可缺省；首次 integrate 后读隔离解除，readiness 给 `resume_tester` 让同一 agent 补 patch。是否已 integrate 由 `candidate.checkpoints` 里有没有 `role=integrate` 派生。
- integrate 用**路径叠加**而不是 merge：`git checkout <tester.head> -- <存在的文件>` + `git rm <被删的文件>` + 一次普通提交。`git rebase` 会丢 merge commit 并重放 tester 的提交，之后再合同一文件必然 add/add 冲突；叠加让候选历史保持线性、重复执行幂等。一律用 ledger 里的 SHA 而非分支名（tester 的 handback 登记可能正在提交）。
- 测试文件集合 = `git diff --name-status tester.base..tester.head`（含删除）；"候选是否需要 integrate" = 这些路径在两边的 blob 是否一致。两者都不落盘。

## 模块

| 模块 | 职责 |
|---|---|
| `config` | `.claude/loop.yml` → `pass_cmd[] / max_iterations / proof_runner / worktree.root`；PyYAML 缺席时用内置子集解析 |
| `contract` | 标签提取、校验、三面 digest、`classify_change`、glob 匹配；**`write_rejection()` 是路径归属与保护的唯一判定入口**（checkpoint / PreToolUse / mutation patch 共用） |
| `ledger` | schema @2 校验、mutate、events、session 指针（读后回核 owner）、`peek`（不校验 schema，给 doctor / runs / abandon 处理旧版 ledger） |
| `worktree` | 每个 run 的 `builder/` 与 `tester/` 两个 worktree（创建失败回滚）、临时 worktree（支持叠加与删除）、身份 / clean 断言 |
| `run` | start / status / checkpoint（分角色、`--dry-run`）/ integrate / resume / abandon / contract validate\|revise |
| `machine` | pass_cmd 三态、超时、执行后 worktree 变更检测、失败签名、`tester_files_mentioned`、基线预跑 `preflight` |
| `evidence` | tester 文件与 integrate 需求的 git 派生、投影、`state()`、`role_running()` / `gate_running()`、blockers（授权窗口）、`readiness()` |
| `proof` | spec 结构校验、`build_argv`、junit 解析与 id 映射、候选 / 反例判定、四步执行、失败签名 |
| `finalize` | 前置检查、commit-tree（可选 `--run-commit-hook`）、intent、CAS、checkout 同步、恢复、rebase |
| `retro` | 确定性信号派生（只读 ledger）、复盘记录校验、cleanup |
| `hooks` | 六个 handler、结果标记解析、tester 读隔离与角色写边界、心跳续租；注入的上下文 = `brief.render()` |
| `brief` | 角色视角事实的**唯一来源**：写边界、候选可读性、待办；结构化 + 文本两种形态，`bl brief` 与 SubagentStart 同源；reviewer 另带文档引用线索（`doc_reference_hints`，现算调 `doc-lint.sh`，≤4 秒，失败降级为 `error`，不落盘） |
| `doctor` | 只读诊断 |

`hooks/bl-hook.sh` 先用纯 bash 从 stdin 抠 `session_id` 并查 session 指针，没有绑定就直接退出、不起 python——PreToolUse 挂在 Read / Bash 上，无绑定 session 的开销必须接近零。

## 写边界

`write_rejection(authority, role, path)` 依次判：`protected_paths` → 归属（**tester_write 优先**：两边 glob 都命中归 tester，构造上不相交，`builder_write:["**"]` 也碰不到测试）→ 角色不符 → 控制面规则。控制面冻结的是**规则**（`authority.control_basenames`：pytest.ini、pyproject.toml、conftest.py、Makefile、package.json、go.mod、Cargo.toml、BUILD、WORKSPACE、loop.yml 等）而不是文件列表：新建的 conftest.py 同样能劫持测试收集。规则只拦「builder 靠 glob 顺带命中」的情形——在写边界里字面点名即视为计划授权；tester_write 内的归 tester。`bl contract validate --check-repo` 在规划期列出会被保护的命中项，让用户中断发生在规划期。

`builder_write` 的条目**不得落在 `tester_write` 之内**（`validate_contract` 直接拒）：否则 checkpoint 判它归 tester、而 builder 以为自己有授权，两个角色都不敢动（#240）。要让测试跟着实现变，在 behavior 里写明，由 tester 改。注入给 tester 的 brief 也只列它自己的写边界，不复述 builder 的。

builder 与 tester 各看各的 worktree，未提交改动互不可见；删除类任务的旧测试由 tester 依据 contract 清理，两个角色不会互相整单拒绝。

## evidence 投影（dependency_digest 的输入）

| kind | 投影 | 何时 stale |
|---|---|---|
| machine | 三面 digest、候选 HEAD | 任何 checkpoint / integrate / rebase、contract 变化 |
| tester | tester 分支上测试文件的 `{path, blob}`（删除记 null）、mission digest | tester 再次提交、mission 变 |
| proof | 候选 HEAD、tester 文件 blob、behavior ids、proof_spec digest、assurance digest | 候选或测试变化、补了 patch、proof_runner 变化 |
| reviewer | 候选 HEAD、三面 digest、machine / tester / proof 各自 `{status, dependency_digest, state}` | 候选变化、任一前置证据变化或失效 |

rebase 改的是 `repo.target_start_head` 与候选 HEAD；`tester.base` 不动，所以 tester evidence 不因目标分支漂移而失效，漂移进来的文件也不会被算成测试文件。

## proof

命令来自冻结的 `assurance.proof_runner`（loop.yml 声明）：环境依赖因此进入证据输入，tester 不再把机器侧 venv 的绝对路径藏进 argv。`shlex` 拆分、不过 shell，只展开 `{main_repo}`。tester 的 proof_spec 每组只有 `kind / behavior_ids[1] / test_ids / timeout / patch? / reviewed_boundaries?`；test id 以 `-` 开头拒绝，文件部分必须是 tester 拥有且存在于 tester 分支的路径。

pytest 框架下结论取自 junit xml：候选阶段要求 rc==0 且每个声明 id 都匹配到用例并全部 passed（skipped / xfail / 未执行都不算）；反例阶段要求 rc==1、全场无 `<error>`、声明 id 里至少一个 `<failure>`。判据只取 junit 的结构位，不看失败信息的文本：`<error>` 是 setup / teardown / collection 出错，测试没跑起来，没有鉴别力；`<failure>` 是 call 阶段失败，测试跑到了且被破坏的实现让它红了，`KeyError`、解包错与 `AssertionError` 同等算数（原则四；#114 与 #255 是同一判据两个方向的误判）。`<error>` 查全场而非只查声明 id——collection 错误可能挂在别的 node 上。代价是「patch 破坏得太狠、测试在函数体内 import 失败」会算作反例成立，由 `TEST_MUTATION_INVALID` 的 patch 约束与 reviewer 兜底。id 映射用 classname 后缀匹配（monorepo 子目录自带 ini 时 rootdir 会变），不带 `[` 的 id 匹配全部参数实例。generic 框架只看退出码，只能做 mutation。

每个 behavior 的 proof 下限写在 contract（缺省 strong）；`reviewed-boundaries` 必须在该 behavior 上显式放行。mutation patch 的路径在执行时用 `write_rejection(…, "builder", path)` 校验，并要求是候选上已存在的普通文件；执行前后比对 diff 防篡改。builder 后来改了实现导致 patch 对不上 → `TEST_MUTATION_INVALID`，owner=tester。

## readiness → next_action

terminal →（无 retrospective `retro`，否则 `done`）；有 blocker → `needs_user`；tester 需要干活且没在跑 → `spawn_tester` / `resume_tester`（**先于** builder 的 `checkpoint`：tester 后台并行，越早放出去越好）；builder 无 checkpoint → `checkpoint`；tester 在跑 → `awaiting_tester`；需要 integrate → `integrate`；`machine`；proof：缺 mutation patch 且 tester 在首次 integrate 后还没答复过 → `resume_tester`，fail 且 owner=tester 且 tester 尚未答复 → `resume_tester`，否则 `proof`；reviewer：在跑 → `awaiting_reviewer`，fail 且有 owner=tester 的 blocking/major 且 tester 尚未答复 → `resume_tester`，否则 spawn / resume；全过 → `finalize`。最后一步：若 `next_action` 正是某个**已在执行**的门禁（machine / proof），改判 `awaiting_gate`。

「tester 尚未答复」用事件时间戳比较（ISO 微秒，UTC，字典序即时间序）：答复过而失败依旧时回到 `proof`，让同签名计数生效，避免无限续接。

**角色是否在跑**不落盘：最近一条生命周期事件是 `role_start`（或要求重发的 `role_malformed`）且 40 分钟租约未过期。该 agent 的每次 PreToolUse 续租，只 touch `run_dir/heartbeat-<role>`，不写 ledger。只用于 `awaiting_*`（抑制 Stop 回拉），不给任何 CLI 加闸；agent 失联则租约到期后回到 resume。

**门禁是否在跑**同样不落盘：machine / proof / preflight 执行期间各持有 `run_dir/gate-<holder>.lock` 的 flock（文件里写 pid，本进程自己持有不算）。进程死掉锁自动释放，没有残留状态要清理。同一门禁重复启动 → `GATE_BUSY`；machine / proof 遇到 preflight 在跑会排队等它（两套全量测试并发会把彼此挤成假超时），排队期间自己的锁已持有，所以 Stop 看得到它在等。

**blocker** 按最近一次用户授权以来的窗口计算：`MAX_ITERATIONS`、`NO_PROGRESS`（machine 同签名 3 次）、`PROOF_STALL`（proof 同签名 3 次）、`REVIEW_CONTRACT`（reviewer 提了 owner=contract 的 blocking/major）、`WAITING_FOR_USER`。前三个激活时 `bl machine` / `bl proof` 直接 exit 3 不执行——上限是真的停止点。`bl resume --reason` 记一条 authorization，但要求 blocker 之后存在 `user_input` 事件：模型不能自己给自己授权。该事件**只由 PostToolUse(AskUserQuestion) 写入**——后台 subagent 结束时唤醒主 session 的任务通知也会触发 UserPromptSubmit，那不是真人。

## hook 接线

| event | matcher | 逻辑 |
|---|---|---|
| SessionStart | — | 纯 bash、不起 python（每个会话都会触发）：往 `CLAUDE_ENV_FILE` 写 `export PATH=<本仓 bin>:$PATH`。CC 把它注入之后的每条 Bash，主会话与 subagent 都生效，SKILL 与 runtime 提示里的裸 `bl` 因此可直接用 |
| Stop | — | 终态已复盘 → 解绑放行；waiting → 放行；`awaiting_*` → 放行且不计 stall；其余（含终态未复盘）→ exit 2 + next_action；`stop_hook_active` 且 seq 未变连续 3 次 → 放行并记 `stall_escape` |
| SubagentStart | tester\|reviewer | 登记 agent_id（保留 turn；换了 agent_id 记 `role_replaced`）、记 `role_start{candidate_head}`、注入上下文（= `brief.render()`；tester 集成前后内容不同） |
| SubagentStop | tester\|reviewer | 只认登记的 agent_id。本轮已有结论或已记 fail → 放行；本轮有 handback 尝试（没开 handback 的环境不会有）→ 不解析 `last_assistant_message`（调用方收不到，常是收尾句），只有不合规 handback 时 exit 2 要它重新 handback；本轮没有 handback → **兜底**解析 `last_assistant_message`，登记规则与 handback 相同（`via:stop`），不合规 → exit 2（与 handback 共用计数，第 3 次记 fail） |
| PreToolUse | AskUserQuestion / EnterWorktree | 写 waiting / deny |
| PreToolUse | Read\|Grep\|Glob\|Write\|Edit\|MultiEdit\|NotebookEdit\|Bash | 仅对登记的角色：续租；reviewer 写一律拒；tester 写只许自己 worktree 的 tester_write；集成前 tester 读 / 搜候选拒（先 realpath，Grep/Glob 必须显式 path，Bash 命令串含候选路径或分支名拒——尽力而为） |
| PostToolUse | AskUserQuestion | 清 waiting、记 `user_input`（授权续跑的唯一依据） |
| PostToolUse | SubagentHandback | 开了 handback 的环境里角色结果的登记点。只认登记的 agent_id；从 `tool_input.message` 取最后一条结果行；不合规 → exit 2 要它重新 handback（≤2 次）→ 第 3 次记 fail；与本轮最后一条结论 payload 相同（`payload_sha256`；tester 还要求 worktree 无未提交改动）→ 放行不重记；登记成功后不合规计数清零；tester：提交 tester worktree + spec 结构校验 + evidence + proof_spec；reviewer：起点 HEAD ≠ 当前候选 HEAD → 本次无效。`role_result` / `role_malformed` 带 `via`（handback / stop / cli） |
| UserPromptSubmit | — | 只清 waiting，不记事件（任务通知也会触发它） |

所有 hook 首步 `lookup_session(session_id)`，stdin 的 `cwd` 不参与定位。matcher 不是身份门禁（`agent_type` 为空的内部 agent 也会被放进来），handler 内复核 `agent_type` 与登记的 `agent_id`。角色 hook 的写入（role_start、要求重发）保持 `stall.seq_seen` 与 seq 的相等关系，不算主 session 的进展。角色结果的来源随「送达调用方的是什么」走：开了 `SubagentHandback` 的环境只有它送达调用方，agent handback 之后常再补一句收尾，一轮还会触发多次 SubagentStop（被 harness `[handback-send-enforce]` 打回一次、收尾一次），调用方收到报告比 Stop 早 8–37 秒（#257 #250）——这时只认 handback。handback **按环境开关、不由 CC 版本号决定**（业务机 2.1.273 开、开发机 2.1.278 没开）；本轮没有 handback 时最后一条消息就是送达的报告，由 SubagentStop 兜底解析。开了 handback 的环境里，被打回的那次 Stop 若带结果行会先登记，随后内容相同的 handback 按同轮去重不重记；若不带结果行会多记一次 `role_malformed{via:stop}`——hook 不读 transcript，区分不了，接受这个代价。「本轮」= 该 agent_id 最近一次 `role_start` 之后的事件，只从事件流派生。SendMessage 续接会让 Start 与 Stop **都**再次触发，agent_id 不变；但 SubagentStart 的 `additionalContext` **只在首次 spawn 时送达**，被续接的 agent 收不到（均为 CC 2.1.272 实测）。所以**注入的上下文不是角色的事实来源，`bl brief` 才是**：同一个 `brief.build()` 现算，角色在续接轮、收到自称 Builder 的消息时、或对归属存疑时随时自取。builder 的消息只当门铃（`bl status` 的 `briefs.*` 是现成正文，里面不含任何事实）。续接轮里 tester 交 `insufficient_spec` 而测试未变时，原 tester evidence 保留，只记一条 `role_result{status: declined}`。

## 复盘闸门

finalize / abandon / finalize_failed 之后 session 不解绑；`start` 遇到「已绑定、终态、未复盘」返回 `RETRO_PENDING`。`retro.derive_signals` 只读 ledger（abandon 的现场可能已损坏），信号来自 failures、authorizations、contract.history、checkpoints 与 events；`retro record` 只校验覆盖率与路由字段（issue 路由要 `issue_url` 或 `declined_by_user`，`not_incident` 要理由），判断与立项是执行者和用户的事。events 只记没有别处归属的事实，其余信号从原归属派生（原则二）。

## 失败路径

| 情况 | 行为 |
|---|---|
| proof_runner 跑不起来 | `bl start` 与 `contract validate --check-repo` 先让它对空目录收集一次（健康时退出码 5；`--version` 不加载插件，不算数），不过就拒绝启动（`PROOF_RUNNER_UNAVAILABLE`）——run 还不存在，改 loop.yml 不需要 revise/授权 |
| machine FAIL 的 stage 在基线上也红 | 跑过 `bl preflight` 则 `failure.baseline_red=true` 并提示与候选无关；没跑过则提示可以补跑 |
| machine FAIL | evidence fail + failures 追加；输出 `repeat_count` / `remaining_iterations` / `tester_files_mentioned`；测试写错由 builder SendMessage 给 tester |
| pass_cmd 改了候选文件 | `failure.worktree_mutated`，视为 FAIL |
| machine 执行期间输入投影变化（builder 并发 checkpoint / integrate、contract revise 改了 facets） | `MACHINE_INPUT_CHANGED`（negative，exit 1）：本次观察作废，不写 evidence、不追加 failures、不占 `machine_iter`，只记一条 `machine_input_changed` event，`changed_inputs` 列出变了的投影键。判据是 `evidence.input_changes`（起跑与收尾两份 ledger 的投影比较），而非 git HEAD——pass_cmd 自己 commit 造成的 HEAD 变化仍走 `worktree_mutated` |
| proof 执行期间输入投影变化（checkpoint / integrate / tester 交卷换了 proof_spec / contract revise） | `PROOF_INPUT_CHANGED`（negative，exit 1）：与 machine 同一判据，同样不写 evidence、不追加 failures，记 `proof_input_changed` event（含 `attempt`），重跑的 attempt 不变 |
| contract 声明的 `assurance.machine_stages` 在 loop.yml 里缺失 | `MACHINE_STAGE_MISSING`（fatal，exit 2），`bl start` / `contract revise` 经 `freeze_assurance` 拒绝，`validate --check-repo` 列为 problem；run 还没创建，无现场代价。loop.yml 未纳入版本控制，可能被别的 session 改写，这是唯一挡住「stage 静默丢失」的地方 |
| `bl start` 时主仓停在目标分支且有未提交的 tracked 改动 | 候选从目标分支 HEAD 建立，这些改动不在基线里：start 输出 `target_uncommitted`（最多 50 条，只报告不拦截、不落盘） |
| proof runner 起不来 | 候选阶段 rc≠0 且一条 junit 记录都没有 → `TEST_PROOF_RUNNER_FAILED`，`suggested_owner=contract`（两个角色都改不了，要改 loop.yml）|
| proof 失败 | `TEST_PROOF_CANDIDATE_FAILED` / `TEST_PROOF_NOT_EXECUTED` / `TEST_BASELINE_RED_NOT_PROVEN` / `TEST_MUTATION_SURVIVED` / `TEST_MUTATION_INVALID` / `TEST_MUTATION_PATCH_MISSING` / `PROOF_WORKTREE_MUTATED`；`suggested_owner` 指向 builder 或 tester |
| reviewer 未过 | finding 的 owner 决定去向：builder 修 / 回 tester / `REVIEW_CONTRACT` 交还用户 |
| 目标分支前进 | finalize → TARGET_DRIFT；`bl rebase` 只动候选；成功后 evidence 自然 stale，tester evidence 不受影响 |
| 主仓 dirty 与变更路径重叠 | finalize → DIRTY_OVERLAP，不写回 |
| finalize CAS 后中断 | ledger 留 `finalize_intent`；再次 finalize 按目标分支当前 HEAD 恢复 |
| commit hook 改写 tree | `--run-commit-hook` 模式下 FINAL_COMMIT_TREE_MISMATCH，目标分支不动 |
| abandon | 终态 abandoned，worktree 与分支保留；复盘后 `bl cleanup` 回收 clean 且 HEAD 未漂移的 |
| 旧版 ledger（@1）| hook 对它静默；`bl runs` / `doctor` 能列出，`bl abandon --run` 能结束它 |

## 未做（演进方向）

machine evidence 的 affects / exempt scope（当前每次候选变化全量重跑）；`acceptance_cases` blackbox；reviewer 后台模式；hook 超时导致 tester 结果未入账时的补录入口。评估后明确不做的 Codex 版特性及理由见 issue #233。
