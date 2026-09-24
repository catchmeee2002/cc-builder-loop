# Changelog — cc-builder-loop 已交付能力

> 本文件记 **CC 版**产品线（分支 `cc/main`）。Codex 版见 `codex/main` 分支。

## 角色被残留后台任务唤醒时不冒充续接，残留可见可收（#306 #283 #308 #304，2026-09-24）

- **唤醒不再产生 evidence（#306）**：builder 的 SendMessage 由 PreToolUse 记为 `resume_request`；已登记角色没有对应请求的 SubagentStart 判为唤醒（`role_wake`），这一轮的结论不登记、不计不合规、不计轮次。此前后台任务结束把已交卷的角色唤醒后，它重发的旧结论会被登记成当前候选 HEAD 上的 evidence（探针实测唤醒与续接的 SubagentStart 字段完全相同）。
- **超时转后台的角色命令可见并由 builder 回收（#283 #308）**：PostToolUse(Bash) 的 `backgroundTaskId` 记为 `role_background`，并当场提示角色；builder 用 TaskStop 停掉后记 `role_background_stopped`。未停的任务列在 `bl status`、Stop 拉回消息、finalize / abandon 输出与 retro 信号里，不拦 finalize。此前 `60395a4` 只拦显式的 `run_in_background`，前台命令超时被转后台会绕过它。
- **brief 给出 ledger 路径（#304）**：此前 reviewer 为核对 evidence 在整个文件系统里 `find`，在 NFS 上跑了近 3 小时，正是上面那种残留任务。
- **hook 接线**：PreToolUse matcher 追加 `SendMessage`，PostToolUse 扩为 `SubagentHandback|Bash|TaskStop`，总数仍 10 条；`bl doctor` 检查这些 matcher，旧安装要重新 `./install.sh`，否则每次续接都会被当成唤醒。
- **#307 关闭**：Bash 前台 timeout 上限 10 分钟、超时即转后台，而心跳每次 PreToolUse 续期，「单次调用超过 40 分钟租约」构造不出来。

## 目标分支漂移时不丢改动、复审有的放矢、可以按外部顺序暂停（#298 #280 #299 #282，2026-09-23）

- **tester 分支跟着 rebase（#298）**：目标分支改过 tester 改过的测试文件时，`bl rebase` 在候选之后把 tester 分支也 rebase 过去（输出 `tester_rebase.status`：`not_needed` / `rebased` / `conflict` / `deferred`）；零冲突时重叠文件内容变了，tester evidence 变为 stale，续接 tester 确认（brief 待办 `confirm_rebased_tests`）；冲突时 rebase 停在 tester worktree 里由 tester 解（待办 `resolve_rebase_conflict`）。只要 tester 分支还落后且有重叠，`bl integrate` 就报 `INTEGRATE_TESTER_BASE_STALE`，readiness 新增 `rebase` 动作。此前 tester 分支永远停在 run 起点，rebase 后 integrate 用旧基线的整文件覆盖候选，静默抹掉了目标分支对同一测试文件的 33 行改动。漂移不碰 tester 文件时 tester 分支照旧不动。
- **候选 rebase 冲突解完后可以续上**：冲突时记一条 intent 事件，`git rebase --continue` 之后再 `bl rebase` 按 intent 采纳新 HEAD。此前文档写的这条流程会报 `WORKTREE_HEAD_MISMATCH`（没有测试覆盖到解完冲突这一步）。候选里 tester 拥有的文件冲突时直接取目标分支一侧继续：它们只是 integrate 派生的副本，真正的合并交给 tester 分支，builder 也无权写它们。
- **reviewer 复审看漂入变更（#280）**：reviewer evidence 仍随候选 HEAD 作废（原则一不放宽）；brief 新增 `target_drift`（漂入的提交与路径、与候选改动相交的路径、patch 是否未变），审查清单加第 7 条，设计哲学原则一补一句「复审对象是漂入的变更与候选的交互；patch 未变不构成沿用依据」。此前两轮复审都只比了 patch 就沿用结论，漂入的提交没人看。
- **hold 在重验途中也能用（#299）**：本 run 的 gate 全过过一次就能 hold，不要求此刻全绿；授权锚点改为 max(gate 首次全过, 最近一次 release)，rebase 后重新全绿不再要求重问；hold 之后候选没再变就一直给 `held`（证据 stale 也一样）。finalize 仍只认四项 fresh pass。
- **run 内的判据参数只有一个家（#282）**：`contract revise` 从候选 HEAD 读 `.claude/loop.yml`，只改候选一份、checkpoint 后 revise 即可，finalize 不再因主仓那份报 `DIRTY_OVERLAP`；不跟踪 loop.yml 的项目行为不变。

## proof 逐条报告测试的鉴别力（#285，2026-09-23）

- **`counterexample.per_id`**：反例阶段为每条声明 test_id 记 `red` / `green` / `missing`；reviewer 的 brief 列出从未变红的那些，`agents/reviewer.md` 审查清单增加对应的固定项。此前鉴别力按**组**判定，组内有一条红就算证明成立，一条在被测设计下恒真的边界所对应的测试从未红过也从未被提出来，带着四道门禁的背书上线并造成生产缺陷（#285）。
- **刻意没做**：不要求每条 test_id 都必须在反例下红（会误伤一组里覆盖不同侧面的测试），proof 的通过条件一字未改；#285 评论里「验证层次低于用户入口」那一面按原则零判为认知问题，不进机制。

## 本仓测试套件并行执行（#284，2026-09-22）

- **machine 的 test stage 改为 `pytest -n 16`，超时 600s**：串行全量已涨到 1019s（303 个用例，低负载），超时一路从 600s 调到 900s 再到 1500s；并行后 310 个用例 64–133s，实测 4 次全部通过。依赖 pytest-xdist。
- **#284 的拆解结论**：私有 `PYTHONPYCACHEPREFIX` 的冷编译确实让测试慢约 25%（同一批 proof 相关测试 191s 对 144s）：fixture 设了 `PYTHONDONTWRITEBYTECODE=1`，加上前缀后每个子进程都要重编标准库。但在真实 run 里每个 proof 组只多约 0.5s，可以忽略；「不读工作树遗留字节码」是证据的前提（原则一），所以不动，大头在串行执行。

## 角色不起后台任务、不碰候选 worktree（#283 #234，2026-09-22）

- **角色的后台 Bash 被拒（#283）**：tester / reviewer 调用 Bash 时带 `run_in_background: true`，PreToolUse 直接拒绝（CC 2.1.278 探针确认 subagent 的 tool_input 带这个字段）。此前角色审查时在后台起全量测试，交卷后被自己的后台任务反复唤醒，挂成「半死不活」的 subagent（一个 session 里出现 4 次，最久 22 小时），retro 还把这些唤醒计成「被续接」；有一次 reviewer 用 `pkill` 按模式清理，可能误杀别的 session 的进程。两份 agent 定义也写明了这条约束。
- **tester 的命令集成后仍不能触及候选 worktree（#234）**：此前集成后 Bash 不受限，tester 会直接在候选里改代码生成 mutation patch，这正是 #275 陈旧 `.pyc` 的来源形态。现在路径判断覆盖原样、realpath 与 `..` 归一；读候选走 Read / Grep / Glob，取文件走 `git show <候选分支>:<路径>`。brief 的 `add_mutation_patch` 待办写明在临时目录里生成 patch 的办法，并给出候选分支名。

## finalize 前的用户授权 hold，mutation 归属指向 contract（#291 #277 #286，2026-09-22）

- **`bl hold` / `bl hold --release`（#291 #277，公共 CLI 新增）**：gate 全过后按用户决定暂缓 finalize，例如多会话按顺序发版时要等前面的批次发完。只在 `next_action=finalize` 时可用，并要求 gate 全过之后有 AskUserQuestion 的回答；held 期间 Stop 放行、不计 stall，`finalize` 报 `HOLD_ACTIVE`。状态从 `hold` / `hold_release` 事件派生，不新增 ledger 字段。此前三次现场都靠挂问题或手改 ledger 的 `waiting_for_user` 绕过；stall 逃生只统计一次没被打断的 Stop 链，等待期间有用户交互就会反复清零（`bl status` 是纯读的，issue 中这条归因不成立）。stall 逃生的提示也改为指向 `bl hold`。
- **mutation patch 被 contract 层面原因拒绝时归属改为 contract（#286）**：路径因 `protected` / `outside_authority` / `control_file` 被拒时，交卷与 `bl proof` 入口的 `PROOF_SPEC_INVALID` 都带 `suggested_owner=contract`，并提示 tester 改交 `insufficient_spec`（入口已拦下这类 patch，③ 段走不到）。修复了 `a7cbeea` 把归属检查提前到交卷后带来的回归：tester 无路可走，原地重交 3 次后被记为 fail。planner skill 写明，要锁住现役行为的文件列进 `builder_write` 并由 reviewer 看住零改动，而不是列 protected。

## 删除类与文本类 behavior 的写法规则（#267 #273，2026-09-22）

- **删除类（#267）**：planner skill 写明「旧测试文件被删除」不写成 behavior。它不是可观察行为，proof 构造不出反例；由 tester 的删除、integrate 与 machine 全量通过保证。此前这类 behavior 在 baseline-red 与 mutation 下都无法证明。
- **文本类（#273）**：planner skill 要求在 `then` 里冻结字面锚句及其位置；`agents/tester.md` 硬约束第 6 条规定，断言前对原文与锚句做同样的归一化（去强调标记与反引号、合并空白），没有锚句就交 `insufficient_spec`。此前 contract 只写意思，tester 猜关键词，实现换个说法或 `**` 把连续文字隔开，测试就会红。

## 角色结果登记的两条既有行为补上测试（#259，2026-09-22）

- `hooks.py` 已交付的「同一轮 A→B→A 时第三次的 A 照常登记」与「登记成功后不合规计数清零」此前没有用例守住，现在各有测试，handback 与 stop 兜底两条来源都覆盖，并用 mutation 证明测试有鉴别力。runtime 行为不变。

## machine 预算只计失败，基线提示读最新且不把超时当红（#276 #270 #272，2026-09-21）

- **迭代预算口径（#276）**：`MAX_ITERATIONS` 与 `remaining_iterations` 改为统计窗口内 machine 失败的次数。此前按运行次数计，integrate / rebase / checkpoint 逼出来的、已经通过的重验也占预算，并行开发下一个只失败 1 次的 run 就会触顶。`machine_iter` 仍然按运行计，只用于日志编号。
- **基线提示读收尾时的 ledger（#270）**：此前用排队前加载的快照，读不到排队期间 preflight 写入的新结论，会给出「起点上同样失败，与你无关」的假提示。
- **超时不算基线红（#272）**：`baseline_red` 改为从 preflight event 的 `stages[]` 派生，超时的 stage 另记为 `baseline_timed_out`，preflight 结果新增 `INCONCLUSIVE`。旧 event 同样按新规则判定。builder skill 写明门禁运行期间不要在本机另起重负载（兼 #23）。

## proof 输入在交卷时预检，执行不读工作树字节码（#274 #275 #265，2026-09-21）

- **mutation patch 交卷预检（#274，并入 #271）**：tester 交卷时对 ledger 实收的 patch 查路径归属（任何一轮）、目标存在与 `git apply --check`（首次 integrate 之后，基于交卷时的候选 head、临时 index）。此前这些只在 proof ③ 段查，坏 patch 要多跑一整轮 integrate → machine → proof；现场的损坏发生在 JSON 转录，tester 本地自验的那份始终是好的。③ 段的检查保留为最终判据。
- **私有字节码缓存（#275）**：machine / preflight / proof 的子进程使用本次调用独占的 `PYTHONPYCACHEPREFIX`。此前候选 worktree 里 run 外手工 apply/revert 留下的陈旧 `.pyc`（变异保持字节数且同秒完成）会被当成新鲜的执行，造成假红，也可能造成假绿。
- **语言与 runner 不匹配当场打回（#265）**：`framework=pytest` 时非 `.py` 的 test_id 在交卷时报 `PROOF_SPEC_INVALID`（`suggested_owner=contract`）。Go framework 未做。

## 判据只绑真实输入，受限恢复要有出口（#258 #249 #260 #266 #269 #227，2026-09-21）

- **`evidence_neutral_paths`（#258，公共契约新增）**：loop.yml 可声明判据读不到的路径（如 `docs/**`），由 start 冻结进 assurance 面并计入 digest，改动需 `contract revise --authorize`。machine / proof 的候选侧投影随之从「候选 HEAD」换成「候选全树剔除中性路径后的 `(mode, path, blob)` digest」，带 mode 所以纯 chmod 仍算变化。缺省为空时投影键名与取值逐字节不变，升级不让在跑的 run 失效。**reviewer 不豁免**——原则一要求它始终面对完整 integrated HEAD。此前只改一行 `.md` 会让 machine / proof / reviewer 全部 stale，实测重跑约 15 分钟。
- **machine 失败的 tester 归属与出口（#249）**：`tester_files_mentioned` 改由 `contract.path_owner` 裁决（范围是候选树 ∪ tester 分支），不再只统计 tester 本 run 改动过的文件——因契约变更而失效的**既有**测试此前永远匹配不上。readiness 相应新增与 proof 同构的出口：machine fail 且日志出现归 tester 的路径且 tester 尚未答复 → `resume_tester`。此前这条失败没有归属也没有出口，只能靠 `MISSION_REVISION` 绕过。
- **reviewer pass 时 owner=tester 的 finding（#249）**：不派发（派发会引出复审循环），但由 `finalize` 在返回里以 `unaddressed_findings` 列出，不再悄悄消失。
- **pass 后的复审可见（#269）**：reviewer evidence 为 pass 但该角色又被续接时，readiness 给 `awaiting_reviewer` 而不是 `finalize`。对称于 tester 早有的 `elif tester_run`；此前 builder 否决上一轮结论后的复审在 readiness 与 Stop hook 里完全不可见。
- **角色事实补漏（#260 #266）**：`fix_proof` 待办写明重交必须带覆盖全部 behavior 的完整 proof_spec；tester brief 告知交卷后测试会被 machine 全量跑、失败会回到它手上。`agents/tester.md` 的反例失败形态措辞同步为「call 阶段失败」。
- **`git rm` 过的路径不再让 checkpoint 失败（#227）**：已暂存的删除不再作为 pathspec 传给 `git add -A`。

## mutation 反例分类改用 junit 结构位（#255，2026-09-21）

- **判据**：`classify_counterexample` 不再检查失败信息的文本前缀，改为「声明 id 里至少有一个 `<failure>`（call 阶段失败）且全场没有 `<error>`（setup / collection 出错）」。此前要求全部 failure 的 message 都以 `AssertionError` / `assert ` / `Failed:` 开头，同组混有一条 `ValueError` 就整组判 `error`，单独红在 `KeyError` 上同样被判「测试没约束该行为」——该现象出现 3 次，每次让 run 多跑一整轮 tester → integrate → machine → proof。
- **常量 `ASSERTION_PREFIXES` 退役**：#114（扫 stdout 把 `RuntimeError` 当断言失败）与 #255 是同一判据两个方向的误判，文本判定整体换成 junit 的结构位（原则四）。
- **保持不变**：`<error>` 仍查全场而非只查声明 id（collection 错误可能挂在别的 node 上）；`rc != 1` 仍判 `error`；generic 框架仍只看退出码；baseline-red 与 mutation 仍共用同一个分类器。
- **已披露的代价**：「patch 破坏得太狠、测试在函数体内 import 失败」会从 `error` 变成算作反例成立，由 `TEST_MUTATION_INVALID` 的 patch 范围约束与 reviewer 兜底；挽回这点精度只能回到文本判定。

## planner 追问改为下限加可选并声明取舍（2026-09-20）

- **追问结构**：V7.4 的固定 4~7 轮表格在 V8 重写时被压成 5 个方向，当时没有逐项交代去向。现在分「下限维度」（目标与 behaviors、trust_boundaries、写边界，任何档位不可砍）和「可选维度」（接口、方案对比、风险与退路、判据强度，按任务取舍；任务新增或修改接口时接口升为下限）。
- **取舍声明**：第一轮提问前在正文末尾用 2~4 行声明档位、要问与跳过的可选维度及依据（引用本任务具体事实），不新增问题；方案文件「背景与目标」留一行「追问范围」。此前 planner 砍哪些维度是用户看不见的黑盒。
- **V7.4 七问去向**：目标与 GWT 场景、约束与边界 → 等价保留（下限）；方案对比、风险与退路、接口 → 可选维度；演进路径 → 退役（原则五：对未知未来不预置抽象，也没有 gate 消费它）；验收方式与 e2e → 由 tester / proof 两道 gate 替代（原则一、九）；「必须用 AskUserQuestion」→ 归全局配置与 CC 原生提问，不在 skill 里再抄一份（原则二）。

## 门禁绑定整份输入投影与 start 暴露冻结缺口（#254 #256 #264，2026-09-20）

- **machine / proof 共用 `evidence.input_changes`（#254）**：门禁起跑与收尾的输入投影不一致就作废本次观察，不再只比 `candidate.head`。proof 新增 `PROOF_INPUT_CHANGED`；machine 的 `MACHINE_INPUT_CHANGED` 覆盖 contract revise，两者的 details / event 追加 `changed_inputs`。此前 proof 跑的过程中 checkpoint / tester 交卷，旧输入上的结论会被记到新输入上。
- **`assurance.machine_stages`（#256）**：contract 可声明方案依赖的 machine stage 名，`bl start` / `contract revise` 校验 loop.yml 都有，缺了 `MACHINE_STAGE_MISSING`；`validate --check-repo` 同样列出。
- **`bl start` 输出 `target_uncommitted`（#264）**：主仓停在目标分支时未提交的 tracked 路径，只报告。

## planner 按任务把文档容器列进写边界（#263，2026-09-20）

- planner SKILL 新增一条写边界规则：任务改变对外行为 / 契约 / 导航，或本身要探查未知的平台行为 / 外部约束时，把 CLAUDE.md、README、`docs/` 下受影响的文件字面列进 `builder_write`（列入只是授权，改不改仍按 doc-policy 判断）；纯内部重构不列。此前 CLAUDE.md 常被漏列，run 内补文档只能打断用户授权并重跑全部 gate。

## 没开 handback 的环境由 SubagentStop 兜底登记（2026-09-20）

- **handback 按环境开关，不由版本号决定**：上一版假设「CC ≥ 2.1.273 就有 SubagentHandback」，实测不成立（业务机 2.1.273、用户机都有；开发机 2.1.278 前台 / 后台、`-p` / 交互都没有）。上一版在没开 handback 的环境里永远登记不了角色结果。
- **SubagentStop 兜底**：本轮有 handback 尝试就只认 handback（上一版行为不变）；本轮没有 handback 时，SubagentStop 解析 `last_assistant_message` 登记，事件 `via: stop`，规则与 handback 共用（同轮去重、不合规打回、计数清零）。
- **`bl doctor` 去掉 CC 版本检查**：版本号判断不了 handback 是否开启，保留会误报。brief 与 agents 的交卷说明改为两种环境都成立的写法。

## 角色结果改由 SubagentHandback 登记（#257 #250，2026-09-19）

- **结论来源换成 handback**：CC 2.1.273 起 subagent 只有经 `SubagentHandback({message})` 交出的内容才送达调用方，最后写的纯文本不送达；一轮还会触发多次 SubagentStop。原来在 SubagentStop 解析 `last_assistant_message`，于是收尾句「已交付。」被判 malformed、同一份报告被记两条 verdict（#257）。现在新增 `PostToolUse[SubagentHandback]` hook 作为角色结果的唯一登记点；SubagentStop 不再解析，只在本轮 handback 不合规又没补交时打回。同轮相同 payload 不重复登记。`role_result` / `role_malformed` 事件新增 `via`，`role_result` 新增 `payload_sha256`。
- **登记早于 Builder 收到报告（#250）**：handback 当场登记，ledger 不再滞后于 Builder 看到的报告，不会再因此白跑 proof。
- **不再兼容 < 2.1.273 的 CC**：`bl doctor` 新增 `claude_version`，过旧列为 problem。hook 由 9 条变为 10 条。

## install 去重判据与 machine 观察期间输入变化（#246 #253，2026-09-19）

- **install.sh 按 `HOOK_MARKER` 去重（#246）**：识别本版 hook 改为引用 `doctor.HOOK_MARKER`（`bl-hook.sh`），与 doctor 同一个判据。此前按命令串是否含 `builder-loop` 判断，仓库路径不含该子串时每跑一次就多注册一整套 hook。`builder-loop` 子串条件保留，仅作 V7 及更早版本 hook 的退役清理。
- **machine 执行期间候选 HEAD 前进不再记成失败（#253）**：ledger 的 `candidate.head` 在 stage 运行期间变了（builder 并发 checkpoint），本次观察对应不到确定输入，返回 `MACHINE_INPUT_CHANGED` 并作废：不写 evidence、不计 failures、不占 `machine_iter`，retro 的 `S-machine-failures` 不再把它算作失败。pass_cmd 自己 commit 造成的变化仍是 `worktree_mutated` FAIL。

## 文档同步收口（2026-09-19）

- **reviewer brief 带文档引用线索**：`doc_reference_hints{hits,error}` 每次现算（候选 worktree 内以 `.` 为根调 `doc-lint.sh`，基准 `target_start_head`，≤4 秒），文本形态标明「启发式、可能误报」。不进 machine、不写 ledger；算不出来降级为 `error`，brief 照常返回。
- **builder SKILL**：文档同步提示从 §4 汇报移到 §2（finalize 之前）；复盘第 5 步先按 doc-policy 判断项目文档缺口（走 issue），只关乎 AI 协作的才建议 `/memory`。

## install.sh 幂等重跑不再堆积 settings.json 备份（#238，2026-09-18）

- 写入顺序改为"算新内容 → 与旧 `settings.json` 逐字节比较 → 不同才备份并写入"，比较对象是与写盘同一套序列化产出的最终文本。内容不变的重跑不再生成 `settings.json.bak.*`。

## V8.2.1 修复自举 dogfood 与另一台机器上报的问题（2026-09-18）

- **`bl` 进会话 PATH（#245）**：新增 SessionStart hook（纯 bash），往 `CLAUDE_ENV_FILE` 写 `export PATH=<本仓 bin>:$PATH`。CC 把它注入之后的每条 Bash，主会话和 subagent 都生效（2.1.272 实测）。SKILL 与 runtime 提示里约 70 处裸 `bl` 因此不用逐处改写。此前每个新会话的第一条 `bl` 都是 command not found；dogfood 里 reviewer 找不到 `bl`，自己搜出并用了候选 worktree 里的 `bin/bl`——那正是被审查的代码。hook 由 8 条变为 9 条。
- **结果标记解析失败会说明原因（#244）**：此前没写标记、JSON 解析失败、解析结果不是对象，三种情况都报「缺少结果标记」，也不回显原文。反斜杠（`\d`、Windows 路径）、字面 TAB、全角冒号这几种写法肉眼看不出毛病，却都会被这样误判；角色以为自己漏了标记，就会原样再发一次。现在分开报告，回显出错位置并说明正确写法，原因写入 ledger 的 `role_malformed` 事件。

## V8.2 角色查账本、门禁预检、门禁在跑（2026-09-18）

来由：V8.1 的首个真实业务 run 走到了 finalize，但多花了约 2 轮 tester 续接、2 次用户授权、3 次 Stop 空转，产出 #240–#243。

- **`bl brief --role tester|reviewer`：角色事实的唯一来源（#240 #243）**。写边界、候选可读性、contract 版本、**等你做的事**全部从 ledger 与 git 现算，不落盘。SubagentStart 注入的上下文就是它的文本形态（同一个 `brief.build()`），角色在续接轮、收到自称 Builder 的消息时、对归属存疑时随时自取。builder 经 SendMessage 发的消息降级为门铃：`bl status` 的 `briefs.*` 不再含任何事实。此前 tester 拿到的是一次性文字快照——续接时不送达、与 runtime 的判定各说各话、消息又无从验真。
- **写边界重叠在规划期就拒（#240）**：`builder_write` 的条目落在 `tester_write` 之内 → `CONTRACT_INVALID`。此前 validate 接受，而 checkpoint 判它归 tester、注入给 tester 的上下文说它归 builder，两个角色都不敢动。给 tester 的 brief 也不再复述 builder 的写边界。
- **门禁在跑时 Stop 不再催（#242）**：machine / proof / preflight 执行期间各持 `run_dir/gate-<holder>.lock` 的 flock，`next_action` 正是在跑的那个门禁时改判 `awaiting_gate` 并放行。重复启动同一门禁 → `GATE_BUSY`。进程死掉锁自动释放。此前后台跑 machine 时 Stop 反复要求"运行 bl machine"，只能靠无进展放行脱身。
- **判据本身跑不起来的问题提前到规划期（#241）**：`bl start` 与 `contract validate --check-repo` 先让 proof_runner 完整启动一次（对空目录收集），不过就拒绝启动（`PROOF_RUNNER_UNAVAILABLE`）——此时 run 还不存在，改 loop.yml 不需要 revise 与授权。`bl doctor` 也报这项。
- **`bl preflight`**：在 run 起点的临时 worktree 上跑一遍 pass_cmd，记 event（不动 evidence、不计 machine_iter），之后 machine 失败会标出 `baseline_red` —— 这一段在起点上同样失败，与候选无关。由 builder 在 start 后用后台 Bash 启动，与写实现并行。
- 快检最初用的是 `<cmd> --version`，交付前在本仓自己身上就发现漏了：pytest 的 `--version` 不加载插件，本机装坏的 pytest-html 照样返回 0。改为对空目录 `--collect-only`、要求退出码 5（完整启动且没收集到用例）。本仓 `loop.yml` 同时补上与 pass_cmd 一致的 `proof_runner`。
- **proof runner 起不来不再甩给 builder（#241）**：候选阶段 rc≠0 且一条 junit 记录都没有 → `TEST_PROOF_RUNNER_FAILED`，`suggested_owner=contract`。
- 自举 dogfood（交付 #238）后补：`bl brief` 的重取命令改用已安装 runtime 的绝对路径——dogfood 里 reviewer 找不到裸 `bl`，搜出并用了候选 worktree 里的 `bin/bl`；builder SKILL 去掉当前 CC 已不存在的 `Agent(run_in_background)` 参数（subagent 默认在后台跑）。

## V8.1 tester 独立性、判据加固、复盘闸门（2026-09-17）

来由：对 codex-new 做了全量 parity 审计并逐条评估（取舍与理由见 #233），加上首次真实 dogfood 暴露的 #226–#232。

- **tester 从冻结基线盲写测试，与 builder 并行**。每个 run 两个 worktree（`<run_id>/builder`、`<run_id>/tester`），tester 分支从 run 起点长出、永不 rebase；SubagentStart 不再注入候选路径，PreToolUse 在首次 integrate 前拒绝 tester 读 / 搜 / 命令触及候选（realpath 后判断）。两段式：盲写时 mutation patch 可缺省，集成后 readiness 引导续接同一 tester 补 patch。
- **`bl integrate`**：tester 的测试按路径叠进候选（checkout + rm + 一次普通提交），不用 merge——rebase 会丢 merge commit 并重放 tester 提交，之后必然 add/add 冲突。测试文件集合与"是否需要 integrate"都从 git 派生，不落盘。
- **proof 重做（#229）**：测试命令由 loop.yml `proof_runner{framework, cmd}` 声明并冻结进 assurance 面，tester 只给 test_ids；pytest 下按 junit 逐用例判定（候选：每个声明 id 出现且 passed；反例：rc==1、无 `<error>`、`<failure>` 的 message 是断言类），替换原来的 stdout 正则；`{main_repo}` 占位符；generic 框架只看退出码、只许 mutation。每个 behavior 的 proof 下限写在 contract，`reviewed-boundaries` 需显式放行。
- **写边界**：单一判定入口 `contract.write_rejection`；tester_write 优先（构造上不相交）；控制面文件按 basename 规则保护，builder 靠 glob 顺带命中不算授权，需字面点名。checkpoint 分角色看各自 worktree，加 `--dry-run`（#227）。
- **contract 增量字段**：behavior 的 `boundaries` / `invariants` / `proof`，mission 的 `mock_strategy`；`bl contract validate --check-repo` 在规划期对照仓库。
- **上限是真的停止点（#231）**：blocker 按最近一次用户授权以来的窗口计算；激活时 `bl machine` / `bl proof` 直接 exit 3；`bl resume --reason` 要求 blocker 之后有过用户输入事件，模型不能自授权。
- **内部 git 操作不跑目标仓库 hooks（#232）**：统一 `-c core.hooksPath=/dev/null`，仅 `finalize --run-commit-hook` 例外。
- **Stop hook 不再在等待 subagent 时反复拉回（#228）**：`awaiting_tester` / `awaiting_reviewer` 放行且不计 stall；"角色在跑"由事件流 + 40 分钟心跳租约派生，不落盘。
- **reviewer**：finding 带 `owner`（builder / tester / contract）并据此路由；审查期间候选变化则本次结论无效。machine 失败输出 `tester_files_mentioned`，测试写错可回到 tester。
- **复盘硬闸门（#226）**：终态后 session 不解绑；`bl retro signals` 从 ledger 派生确定性信号，`bl retro record` 校验覆盖率后才解绑；未复盘时 Stop 拦住、`bl start` 返回 `RETRO_PENDING`。`bl cleanup` 回收已复盘的 abandoned run。
- ledger 升 `@2`：`tester`、`events[]`（只记无别处归属的事实）、`authorizations`、`runtime_identity`、`retrospective`；`peek` 让 `bl runs` / `doctor` / `abandon` 能处理 `@1`。时间戳改微秒精度。
- `hooks/bl-hook.sh` 加纯 bash 快速路径：session 未绑定 run 时不起 python（PreToolUse 现在挂在 Read / Bash 等高频工具上）。
- 首次真实会话 E2E（真 skill + 真 agent + 真 hook）抓到并修复的三处：续接的 subagent 收不到 SubagentStart 的 additionalContext → 集成后的事实改由 builder 消息传递，`bl status` 输出 `briefs.resume_tester`；后台 subagent 的任务通知也会触发 UserPromptSubmit → `bl resume` 的用户输入依据只认 AskUserQuestion 的回答；续接轮 tester 交 `insufficient_spec` 且测试未变时保留原 evidence，记 `declined`。
- #230：`contract validate|revise`、`evidence show`、`retro *` 接受写在子命令之后的 `--session` / `--run`；`init-loop-config.sh` 在 `.claude/` 整目录被忽略时不再写无效的否定规则。

## V8.0 Claude Code 原生重写（2026-09-16）

从 V7.4（tag `v7.4`）重写。编排交给 Claude Code 原生能力，runtime 只做判据与 Git 事务。

- runtime 改为 Python 包 `runtime/builder_loop`（stdlib only，约 2.7k 行），CLI `bl`；ledger 单写者，flock + seq。
- contract 三面（mission / authority / assurance）各算 canonical digest；`assurance.machine_commands` 由 start 从 loop.yml 冻结；mission 变需 revision+1，authority 扩大 / assurance 降级需 `--authorize`。
- 四道 gate：machine / tester / proof / reviewer。evidence 记按 kind 定制的输入投影 digest，stale 每次重算不落盘；readiness 派生 next_action，不存 phase。
- proof：候选全绿 → baseline-red / mutation（tester 提供 patch，只能改 builder_write 内已有文件，执行前后比对防篡改）/ reviewed-boundaries；group↔behavior 双射；同签名三次 PROOF_STALL。
- tester / reviewer 的 evidence 由 SubagentStop hook 按 CC 提供的 `agent_type` / `agent_id` 从 `last_assistant_message` 的 `BUILDER_LOOP_RESULT:` 行写入；只认 SubagentStart 登记过的 agent_id；不合规 exit 2 重发，三次记 fail。
- Stop hook 只挡门：run 未终态 exit 2 + next_action；AskUserQuestion 挂起（PreToolUse 写 / PostToolUse、UserPromptSubmit 清）放行；连续 3 次 Stop 之间 ledger seq 无变化放行并提示。不跑 pass_cmd、不解析 transcript。
- 强制 worktree（仓库同级 `../builder-loop-worktrees/<repo>/<run_id>`），删 bare 模式；checkpoint 按角色写边界拒绝越界（protected / tester_owned / builder_owned / outside_authority）。
- finalize：commit-tree 单亲提交 + 落盘 intent + `update-ref` expected-old CAS + 同步目标 checkout；`--run-commit-hook` 可在临时 worktree 跑 hook 并比对 tree；TARGET_DRIFT → `bl rebase`；DIRTY_OVERLAP 阻止写回；中断后沿 intent 恢复。
- install.sh 改 python3 主体：软链 agents / skills / bin，注册 8 条 hook，清理旧版断链与 hook，幂等；`bl doctor` 诊断。
- 退役：judge、arbiter、diff-level-check（L1/L2/L3）、doc_freshness 三层、复盘 5 问、locate-state 六策略、phase 状态机与 L1/L2A/L2B/L3 闸、pause、bare 模式、merge-worktree-back、migrate-state、diagnose-stop-hook、reviewer-timing-check、reward-hacking 关键词黑名单、e2e 沉淀 YAML、全部 bash fixture（判据语义移植为 pytest，35 例）。
- 保留：`doc-lint.sh` / `doc-reference-check.py`（可选 pass_cmd stage）、`probe-project-stack.sh` / `init-loop-config.sh`（接入向导，已裁掉 judge / worktree 旧字段）。
- 角色 evidence 依赖 Claude Code 2.1.272 起 hook stdin 的 `agent_type` 字段（自定义 agent 返回其 frontmatter `name`，matcher 可按它过滤，`last_assistant_message` 完整携带结果标记行）——2026-09-16 实测确认，这是 V8 能成立的硬前提，也是最低 CC 版本要求。
- 设计哲学文档采用 codex-new 的 11 条版，宿主指称改写为 Claude Code（thread → agent 会话，AGENTS.md → CLAUDE.md）。


## V7.4 文档失效源码指针机械锚点（2026-07-21）

**动机**：函数迁移后，嵌套 CLAUDE.md 中的 `old/path.py::symbol` 仍指向已不存在的位置；原
`doc_freshness_check` 只用 changed basename 扫三个顶层文档，Builder 与 Reviewer 都收不到信号。
这是用户第三次在交付后追问文档并发现漏更，按原则五属于架构缺口而非提示词疏忽。

**核心变更**：
- `diff-level-check.sh` 从 diff 提取 Python/JavaScript/Go/Rust/Shell 被移除的函数或类定义；同名定义
  出现在其他文件时标为 moved，否则标为 removed。签名只在原文件变化时不误判为迁移
- 提取与 Markdown 定位集中到 `doc-reference-check.py`；`diff-level-check.sh` 与 `doc-lint.sh` 共同
  消费这一唯一结果，避免两套“迁移是否使指针失效”的实现继续漂移。`doc-lint` 因此也能在配置为
  PASS_CMD stage 时直接阻断失效 qualified pointer
- detector 调用失败会写入 `machine_checks.doc_reference_scan_error`，Builder 必须停止、Reviewer
  必须 blocked；扫描失败不再退化成“没有文档影响”
- 文档扫描从三个硬编码文件扩展为项目维护型 Markdown；排除 `.git`、`.claude`、vendor/build
  产物、CHANGELOG 与 improvements 历史容器，避免要求改写历史记录
- 同一行同时包含旧完整路径与已迁移/删除符号时，输出
  `machine_checks.broken_symbol_references`；只有符号提及时进入 `semantic_checks`，继续由 Reviewer
  判断语义，不把模糊命中升级成机器硬结论
- Builder 步骤 3.5.5 必须修复每条失效指针；Reviewer Phase D 即使目标文档未进入 changed_files
  也会定点复核

**设计依据**：原则一要求可证明的指针失效进入机器判据，原则四要求增强审计输入而非继续增加
“多 grep 几个词”的输出约束；精确指针与模糊语义分层，避免 doc-policy 所反对的找补式更新。

## V7.3 PASS_CMD 假 PASS 堵死 + 第三参数正名 log_root（2026-07-16）

**动机**：`run-pass-cmd.sh` 在 pass_cmd 解析失败时静默输出 `PASS` —— 一个 stage 都没跑却报通过，纯机器判据这层地基被无声绕过。触发链：builder.md 把第三参数写作 `<project_root>`，而 worktree 模式下 `state.project_root` = worktree_path，builder 照字面传 worktree → `log_root == run_cwd` → fallback 读主仓 loop.yml 的条件不成立 → worktree 内无 loop.yml（gitignored 不 checkout）→ python 抛 FileNotFoundError。放大器是 `while ... done < <(parse_pass_cmd)`：process substitution 的退出码父 shell 收不到，`set -euo pipefail` 也够不着它，while 读到空输入零次循环，直落末尾 `echo "PASS"`。违反原则一（判据按独立性分层）——地基能静默返回 PASS，其上的 reviewer / tester 层全建在假地基上，且没有任何一层校验 PASS 的真伪。同时违反原则二（每份数据一个家）：`project_root` 一名四指（state 字段 = worktree、stop hook 变量 = 主仓、本脚本第三参数 = log_root、handle-pass-result 第四参数 = 主仓）。详见 [GitHub issue #67](https://github.com/catchmeee2002/cc-builder-loop/issues/67)。

**核心变更**：
- **run-pass-cmd.sh**：新增 `FATAL <reason>`（exit 2）出口，与 `PASS`（exit 0）/ `FAIL <stage> <log>`（exit 1）三分。loop.yml 缺失 / yaml 解析失败 / pass_cmd 为空 / 零个 stage 真执行 —— 四种「判据没跑起来」一律 FATAL，不再降级成 PASS。解析改为先捕获 + 显式判退出码 + here-string 喂循环，替掉吞退出码的 process substitution。`PASS` 现在只在至少跑过一个 stage 且全通过时输出
- **参数正名**：第三参数在 builder.md / SKILL.md 中从 `<project_root>` 改为 `<main_repo>`，显式写明 worktree 模式取 `state.main_repo_path`、bare 模式取 `state.project_root`。`handle-pass-result.sh` 第四参数同步正名（它读的也是主仓 loop.yml）
- **builder-loop-stop.sh**：新增 FATAL 分支。原 FAIL 分支按 `FAIL <stage> <log>` 分词，会把 `FATAL <reason>` 的 reason 误当 stage，把 builder 引向「改代码」——而 FATAL 要改的是参数或 loop.yml 配置
- **CLAUDE.md**：§1 链接映射表补齐 `agents/reviewer.md` 与 `commands/*.md`（install.sh 通配全链，表里一直漏）；§3「与 dotfiles 的依赖关系」全表作废——builder.md / planner.md / reviewer.md 三个文件早已在本仓 `commands/` 与 `agents/` 下，不是 dotfiles 共享文件

**验证**：`test-run-pass-cmd-args.sh` 13 → 25 case 全 PASS。新增 Case 5（#67 原场景，正负配对：传 worktree → FATAL 且 stdout 绝不含 PASS；传主仓 → fallback 生效且 stage 真跑过）/ Case 6（pass_cmd 为空）/ Case 7（yaml 损坏）。**变异测试**：回退修复后 10 条断言变红，传错参数 / pass_cmd 空 / yaml 损坏三种情况旧代码均 `ec=0` —— 假 PASS 现场复现，证明新 case 有鉴别力而非恒过。

**已知残留**：本仓自身 `.claude/loop.yml` 的 `pass_cmd` 为 `cmd: "true"`（空判据），53 个 fixture 无自动执行入口；`test-pass-cmd-runs-worktree.sh` / `test-nudge-max-reads-worktree.sh` / `test-stop-hook-debug-log.sh` 在 main 上已有 15 条断言长期为红且无人发现（本次已用基线对比确认非本次引入）。与本 issue 同族——判据形同虚设，待立项。

## V7.2 arbiter 续路径接入 reviewer-as-gate（2026-07-15）

**动机**：V3.0 落地 reviewer-as-gate 时只迁移了正常 PASS 路径，arbiter 解决 rebase 冲突后仍走 V2.x 立即合语义（`merge-worktree-back.sh`）——冲突融合后的代码直接 ff 进主线，reviewer 从未看见。违反原则一（判据按独立性分层）：arbiter 是同会话 LLM，其输出必须过独立 agent 判据层才能进主线。详见 [GitHub issue #42](https://github.com/catchmeee2002/cc-builder-loop/issues/42)。

**核心变更**：
- **run-apply-arbitration.sh**：步骤 8 从「调 `merge-worktree-back.sh` 合回」改为「写 `phase=passed_pending_review` + `reviewer_pending` 段挂牌等审」，与正常 PASS 路径同出口。ff merge 改由 builder 在 reviewer 通过后调 `merge-and-cleanup.sh` 完成
- **diff baseline**：reviewer diff 基准从 `state.start_head` 改为 rebase 落点（rebase 前 `MAIN_BRANCH` 的 SHA）。start_head 是 rebase 前的旧 main，用它做基准会把其他 builder 合入主干的改动混进 reviewer 视野
- **退出码**：`MERGE_FAILED`（exit 3）移除——本脚本不再 merge，该出口不再可能
- **arbiter-flow.md**：删除 V3.0 缺口警告；APPLIED 决策改为「Read state 拿 reviewer_pending → 走 reviewer 流程 → 通过后 merge-and-cleanup.sh」

**验证**：`test-arbitration-apply.sh` 13/13 PASS，新增 9 条断言锚住新行为（phase 值 / reviewer_pending 段 / need_arbitration 清除 / **主线 HEAD 未移动** / worktree 存活 / state 存活）。`test-conflict.sh` 9/9 PASS 不受影响。

**已知残留**：`merge-worktree-back.sh` 生产路径已无调用者（bare 模式 V4.1 起走 `loop-commit.sh`），仅 2 个 fixture 直接测它，事实上已孤儿化。

## V7.1 subagent 模型硬编码为 claude-opus-4-6[1m]（2026-07-15）

tester.md / reviewer.md frontmatter `model` 从 `sonnet` 改为 `claude-opus-4-6[1m]`，钉死 Opus 4.6 1M context 版本。token 成本约 sonnet 的 5-10×，每轮 loop 自动触发一次 reviewer。fixture `test-reviewer-compat.sh` 断言同步更新。

## V7.0 unit-test-spec：planner → tester 测试目标结构化（2026-07-14）

**动机**：tester（写测试模式）的主输入 `spec_view` 是 plan 全文 blob——tester 必须从自然语言中自行提炼"测什么"，违反原则四（改输入条件，不改输出约束）和原则六（契约先于实现）。planner 的"测试计划"章节是可选且非结构化的。

**核心变更**：
- **planner.md**：plan 结构新增 `<!-- unit-test-spec -->` YAML 标签（L2/L3 必出），planner 从验收标准+方案设计自动推导后 AskUserQuestion 确认。旧"测试计划（可选）"替换为"单元测试规格（L2/L3 必出）"
- **tester.md**：主输入从 `spec_view`（plan 全文）改为 `unit_test_spec`（结构化 YAML）。删除 `mock_targets` / `data_contracts` / `error_types` 字段，mock 策略折入 `unit_test_spec.mock_strategy`。新增 `missing_cases` 字段接收 reviewer 增量
- **builder.md**：TESTER_HINT 路径从传 plan 全文改为提取 `<!-- unit-test-spec -->` 标签内容。L3 路径引用"参数同步骤 3a+ 格式"自动继承。plan 无标签时 warn 并跳过 tester spawn

**破坏性变更**：
- 旧 plan（无 `<!-- unit-test-spec -->` 标签）无法触发写测试模式 tester
- tester 不再接受 `spec_view` 字段

**设计决策**：
- `<!-- unit-test-spec -->` 与 `<!-- e2e-cases -->` 并列独立——前者是测试规格（tester 据此写代码），后者是行为用例（tester 据此跑验收），抽象层级不同
- reviewer TESTER_HINT 格式不改（`missing_cases` 保持 freeform string 数组）——reviewer 是独立 agent，不被 planner 格式约束

## V6.1 subagent 续接基建路径修复（2026-07-10）

**动机**：V4.3 设计的 SendMessage 续接路径从未生效——V5.0 退役 SubagentStop hook 后，`handle-pass-result.sh` 的 `status=="idle"` 守卫永远不满足（无人写 idle），导致 agent_id 始终输出空，每次都 fallback 开新 agent。

**核心变更**：
- **handle-pass-result.sh**：去掉 tester/reviewer 两处 `status=="idle"` 守卫，改为只检查 agent_id 存在
- **builder.md V5.5 回写**：从 reviewer-only 扩展为 reviewer+tester 统一规则
- **SKILL.md schema 示例**：reviewer status 从 `"idle"` 修正为 `"running"`
- **fixture**：新增 `test-handle-pass-agent-id.sh`（4 case：running 状态提取、e2e 路径提取、无 subagents 段、无 status 字段）

## V6.0 improvements.md → GitHub Issues 迁移（2026-07-06）

**动机**：用户跨 2 台服务器工作，业务侧 agent 无法直接写 cc-builder-loop 的 improvements.md。

**核心变更**：
- **improvements.md 退役**：50 条活跃/观察期条目迁移至 GitHub Issues（新建 #4~#53；#2~#3 为迁移前已创建的 issue，仅补标签），improvements.md 文件删除
- **diff-level-check.sh GitHub fallback**：本地 improvements.md 不存在时自动 `gh issue list` 查 GitHub remote，做 candidates 匹配。输出新增 `improvements_source` 字段（`local`/`github`/`none`）
- **setup-builder-loop.sh 观察期扫描 GitHub fallback**：同上逻辑，gh 不可用时 stderr 警告 + 跳过
- **builder.md step 5 `[loop 改进]` 路径**：从 `Edit improvements.md` 改为 `gh issue create --repo <repo>`。观察期/关闭操作走 `gh issue edit`/`gh issue close`
- **reviewer.md Phase D2 分支**：`improvements_source=github` 时降级为 advisory（GitHub 操作不在 changed_files 可见）
- **GitHub labels 体系**：active / observation / priority:high / priority:mid / priority:low / synced

**设计决策**：文件缺失 fallback 机制——脚本先查本地 improvements.md，存在则读（业务项目不变）；不存在则走 GitHub。cc-builder-loop 删文件即自动切换，业务项目无影响。

## V5.9 doc_freshness_check 三层 specific 检查指令（2026-07-06）

**动机**：V5.8.1 fence fix 后暴露 builder 步骤 3.5.5 用「未命中」shortcut 跳过 CHANGELOG/plan/improvements 更新。根因：机器层给 open-ended 文件列表，LLM 做 open-ended 判断（违反原则一+四）。

**核心变更**：
- **diff-level-check.sh 输出重构**：`doc_freshness_check` 从 `string[]` 改为 `{machine_checks, candidates, semantic_checks}` 三层结构
  - `machine_checks.changelog_needed`：diff 含 .sh/agents/*.md 改动 + CHANGELOG 不在 changed_files → boolean
  - `machine_checks.plan_version_stale`：plan.md 版本号 ≠ CHANGELOG 最新版本号 → boolean
  - `candidates.improvements_status`：活跃 improvements 条目标题匹配 changed file basename → string[]
  - `semantic_checks`：沿用交叉引用 + 附 specific question → `{file, question}[]`
- **builder.md step 3.5.5 适配**：machine_checks 项 builder 只执行不判断（消灭"未命中"shortcut）；candidates 逐条回答 specific 问题；semantic_checks Read + 回答
- **reviewer.md Phase D 适配**：D1 验证 machine_checks 落地（CHANGELOG/plan 是否在 changed_files）；D2 验证 candidates 处理；D3 验证 semantic_checks

**设计依据**：原则一（能机械覆盖的用机器判据兜底）+ 原则四（改输入条件让正确行为自然发生，不加输出约束）。

## V5.8.1 extract-e2e-cases fence 修复 + 观察期扫描防崩溃（2026-07-06）

**修复**：
- **extract-e2e-cases.sh fence 静默跳过**：V5.4 awk fence 逻辑无条件优先于 capture，planner 在 `<!-- e2e-cases -->` 标签内用 ` ```yaml ``` ` 包裹 YAML 时全部内容被跳过 → e2e 验收静默跳过。修法：`if (!capture) fence = !fence`，capture 模式内 ``` 只剥标记行不 toggle fence。fixture case 9/10 覆盖
- **setup-builder-loop.sh check_observation_expiry 崩溃**：V5.8 观察期扫描的 grep pipeline 在条目 deadline 格式不匹配时 exit 1 + pipefail + set -e 杀掉整个 setup。修法：`|| true`

## V5.7 E2E 框架重设计：verify + quality 双轨判定（2026-07-05）

**动机**：divine-word 事件——e2e 验收通过但产出是 programmer-art。根因：(1) llm_judge 确认式提问引导 tester 进入"数据源模式"；(2) 独立 agent 复现同样盲区——是 LLM 通用感知模式问题；(3) 框架无质量维度、无审计、静默跳过。

**核心变更**：
- **Case schema 重设计**：`llm_judge: string|null` → `judge: {verify, quality}`。verify = 功能验证（确认式），quality = 质量标准（评价式，给 tester 判断自由度）。每条 full case 必须同时含两个维度
- **Tester 评估流程三层化**：L1 hard_rules → L2a verify（确认式 prompt）→ L2b quality（评价式 prompt）。prompt 结构差异对抗 LLM 数据源模式
- **Planner Round 7 指导重写**：verify/quality 写法规范 + 自检（"功能正确但质量极差能拦住吗？"）
- **审计落盘**：tester 写 `{project_root}/.claude/e2e-audit/{timestamp}.yaml` 留痕，不随 state 清理
- **静默跳过修复**：handle-pass-result.sh E2E_PLAN_PATH 空/plan 文件不存在 → stderr warning + JSON `e2e_skipped` 字段

**设计依据**：原则四（case 结构 = 输入条件，引导正确认知模式）+ 原则一（quality 判定 = 独立语义层，给 tester 判断自由度而非穷举失败模式）。

## V5.6 tester 路径隔离 + e2e_verified_head ancestor 判定（2026-07-05）

**动机**：tester subagent 3 次写入主仓而非 worktree（07-04、07-02、04-29 doc-maintainer 同模式）。原则五"三次=架构缺陷"。根因：tester 的 session cwd = 主仓（CC 不支持设 subagent cwd），prompt 说"以 worktree_path/ 开头"是输出约束，对抗环境引导（Glob 探索返回主仓路径）赢面低。

**核心变更**：
- **路线 2（原则四，改输入条件）**：builder spawn tester 时 `target_test_dirs` 从相对路径改为绝对路径（已含 worktree 前缀）。tester 的 Glob/Write 自然跟随此路径，不再需要心理转换
- **路线 3（原则一，分层兜底）**：builder 在 tester 返回后 parse `CHANGED_TEST_FILES`，逐路径校验前缀，不匹配 → cp + rm 搬运
- **tester.md 约束简化**：硬约束 6 从"worktree_path 非空时 prepend"改为"路径必须在 target_test_dirs（绝对路径）之内"——消除心理转换需求

**实验验证**：
- 实验 A：fresh agent cwd = session primary working directory（固定），Bash cd 不影响 → 路线 1 不可行
- 实验 B：tester 步骤 2 Glob target_test_dirs 获取探索路径 → 传绝对路径即可引导写入位置
- 实验 C：CHANGED_TEST_FILES 行提供可审计的文件路径列表 → post-hoc 校验有数据源

**设计依据**：原则四（改输入条件不改输出约束）+ 原则五（三次=架构缺陷）+ 原则一（分层：路线 2 降频 + 路线 3 兜底消灭漏网）。

### e2e_verified_head 从精确 SHA 改为 ancestor + path filter

**动机**：e2e 通过后的无关 commit（doc 更新、reviewer 修复）让 HEAD 前进 → `e2e_verified_head == HEAD` 不匹配 → L1 自愈永远不触发 → 用户等 30 分钟死循环（两次复现：07-04 + 06-24）。根因：SHA 精确匹配是 identity 比较，e2e 需要的是 behavioral equivalence。

**核心变更**：L1 闸 e2e_pending 自愈从 `e2e_verified_head == HEAD` 改为：
1. `e2e_verified_head` is-ancestor-of HEAD（含精确相等）
2. `git diff --name-only` 中间 diff 全命中 safe patterns（`*.md`/`*.txt`/`docs/*`/`.claude/*`）
3. 有源码变更 → 不自愈（保留原始并发守卫功能）

**fixture**：`test-e2e-verified-head-ancestor.sh`（3 case × 2 assert = 6 assertions）覆盖 ancestor+doc / ancestor+source / exact-match 三种场景。

## V5.5 文档审计架构重构（2026-07-05）

**动机**：文档更新无独立判据层（代码有 PASS_CMD + reviewer 两层，文档只有 builder 自评）。90% loop 结束后追问"文档更新了吗"都能捡出遗漏。doc-maintainer 独立性为零（builder spawn + 指挥），是 writer 不是 auditor。

**核心变更**：
- 删 `agents/doc-maintainer.md`——builder 自己写所有文档（doc 视为特殊 code）
- reviewer.md 新增 Phase D：doc-policy compliance 独立审计（输入 doc_freshness_check 列表，审文档描述与代码行为一致性）
- 修 🔴 re-review 缺口：builder.md 🔴 路径补"修完后必须重跑 PASS_CMD + reviewer 再审"
- 修 V5.4 reviewer agent_id 回写遗漏：spawn 后写 state.subagents.reviewer

**设计依据**：原则一（独立性属于判断层不属于执行层）+ 原则四（删协调约束比加更简单）+ research 数据（单 agent 顺序链优于多 agent 协调，builder.md ~2.7K tokens 仍在指令高原区）。

## V5.4 Builder 接管 PASS_CMD 执行（2026-07-04）

**动机**：Stop hook 作为 PASS_CMD 唯一触发器被证实不可靠（CC 平台 Stop event 有时不 fire，三次复现）。原则五"三次=架构缺陷"触发重构。

**核心变更**：Stop hook PASS 路径（~230 行内联逻辑）提取为独立脚本 `handle-pass-result.sh`（e2e 检测 + reward hacking + commit + state 写入 + reviewer_pending）。Builder 可直接调用 `run-pass-cmd.sh` + `handle-pass-result.sh` 完成一次迭代，不依赖 Stop event。Stop hook 保留为 safety net（如果 fire，L1 闸看 phase=passed_pending_review → exit 0，不 double run）。

**新脚本**：`skills/builder-loop/scripts/handle-pass-result.sh`——stdout JSON 输出（type: pass/e2e_needed/reward_hack/commit_error），exit code 0/2/3/4。

**独立性分析**：PASS_CMD 的独立性属于测试套件本身（loop.yml 定义、机器执行），不属于触发机制。谁 invoke `pytest` 不影响判据独立性。

## V5.3 install.sh 幂等覆盖 + find_project_root worktree 追溯（2026-07-04）

- **install.sh 幂等覆盖**：hook 注册从"检测差异→条件更新"改为"无条件删旧+写新"。消灭 `find_entry_status` 部分比较导致的配置漂移（如只比脚本名不比 matcher）。deprecated 列表合并进统一清理逻辑
- **find_project_root worktree 追溯**：setup-builder-loop.sh 和 locate-state.sh 的 PROJECT_ROOT 锚定增加 `.git` 文件检测（worktree 的 `.git` 是文件不是目录）。检测到 worktree 后通过 `git rev-parse --git-common-dir` 追溯到主仓，解决 `--reuse-worktree` 从 worktree 内调用时 state 创建在 worktree 内的问题
- **doc_freshness_check 交叉引用**：diff-level-check.sh 从"plan.md 硬编码存在性探测"升级为扫描项目文档（CLAUDE.md/SKILL.md/README.md）与 changed files 的 basename 交叉引用。命中的文档输出到 doc_freshness_check 数组，步骤 3.5.5 强制 Read 检查过时性

## V5.2 e2e_pending dirty guard + locate-state || true（2026-07-04）

- **L1 闸 e2e_pending dirty guard**：e2e_pending 时 dirty_changes 不再触发自愈回 active，仅 new_commit 和 e2e_verified 自愈。解决 tester 写文件导致 stop hook 无限循环（e2e_pending → dirty heal → active → PASS → e2e_pending）。trade-off：builder 修完 failed e2e 的代码后需手动写 phase=active（注入消息已含提示）
- **locate-state.sh 策略 3 || true**：grep pipeline 末尾补 `|| true`，与策略 4/5/6 对齐，防 set -e 迁移时脚本中断
- **improvements.md 清理**：删 3 条已消化条目（uninstall.sh 漏项 V3.5-A 已修 / e2e_pending 循环本次修 / locate-state || true 本次修）

## V5.1 locate-state session_id 匹配 + merge-and-cleanup 一行修（2026-07-03）

### locate-state.sh session_id 匹配

CC session CWD 是会话级常量（主仓），Bash tool 的 `cd` 不改它。多 worktree 并发时 locate-state.sh 原有策略全靠 CWD 匹配，策略 5（唯一 active 兜底）在 ≥2 active 时被安全限制封死 → stop hook 找不到 state → loop 静默失效。两次实际复现（2026-06-04 PA_Bot、2026-06-30 pc-ipc-toolkit）。

修法：locate-state.sh 加可选第二参数 `session_id`。新增策略 1.5（按 `owner_session_id` 精确匹配）和策略 6（唯一未绑定 active state 首次绑定兜底）。stop hook 从 stdin 提取 session_id 后传入 locate-state.sh。首次 stop hook fire 写入 `owner_session_id` → 后续全走策略 1.5 直接命中，不再依赖 CWD。

### merge-and-cleanup.sh 两处一行修

- `read_field()` sed 加 `s/^'(.*)'$/\1/` 剥 yaml 单引号（python yaml.dump 输出 `cleanup_phase: ''` 导致 `ERROR unknown-cleanup-phase`）
- worktree remove 后加 `cd "$PROJECT_ROOT"` 兜底（CWD 被删 → `set -euo pipefail` 下 getcwd 失败 → 误导性 exit 1）

## V5.0 隔离范式变更——认身份隔离退役，隔离退地基（2026-07-02）

**设计范式变更**，非增量功能。

### 变更原因（因果链）

1. **subagent_type 失效**：CC 内核的 SubagentStart hook stdin 对自定义 agent（tester / reviewer / doc-maintainer / arbiter）从未在真实环境提供过 `subagent_type` 字段。2026-06-14 V3.5 落地 per-agent-type 锁机制时，所有测试用 fixture 灌假 stdin 全绿，但真实 session 从未写过一把 tester/reviewer 锁。2026-07-02 实测确认：内置 agent（general-purpose / Explore）同样读空——整套按身份识别的隔离机制**从落地起就在真实环境空转**。
2. **读隔离病根消失**：读隔离防的是「同一个大脑串味」——builder 写实现的意图流进测试。但 tester 是独立推理实例（独立上下文、独立模型 sonnet），与 builder 不共享思维链。共脑不存在，串味不成立，读隔离失去防御对象。
3. **写隔离退地基**：写边界的防御对象（agent 越界/篡改）与共不共脑无关，仍然存在。但强制手段（按 subagent_type 拦截的 hook）已失效。替代方案：subagent prompt 职责声明（软）+ merge 前 diff 审查（中）+ PASS_CMD 锚定 agent 改不到的测试集（硬）。内核 `isolation: worktree` 作演进预留。

### 删除的文件

| 文件 | 原用途 |
|------|--------|
| `scripts/subagent-start-guard.sh` | SubagentStart hook：按 subagent_type 写 per-type 锁 + 注入 worktree 边界上下文 |
| `scripts/lock-utils.sh` | subagent lock 公共函数库 |
| `scripts/tester-lock-check.sh` | PreToolUse hook：拦截 tester 对 source_dirs 的读操作 |
| `scripts/worktree-write-guard.sh` | PreToolUse hook：分级写路径防护 |
| `scripts/subagent-lock-clear.sh` | SubagentStop hook：按 agent_type 清锁 |
| `skills/builder-loop/scripts/split-plan-by-role.sh` | 方案文件按 role 标签过滤（读隔离载体） |

### 删除的 stop hook 闸

- **L2C**（fork subagent 锁存在 → 静默）：依赖 per-type 锁机制，随认身份 hook 一起退役

### 变更的 prompt / 文档

- **tester.md**：「禁止 Read 实现源码」→「测试断言锚定契约，可 Read 实现做交叉验证」；路径约束从硬约束降为职责声明
- **doc-maintainer.md**：加 `git check-ignore` 自检
- **builder.md / planner.md**：删 role 视图过滤，方案全文直传 subagent
- **design-philosophy.md**：原则一（独立 agent 判据独立性来自架构天然属性，不依赖外部隔离机制）；原则三（写边界退地基）
- **install.sh**：registrations 从 6 条缩为 2 条（Stop + PreToolUse:Agent）；deprecated 加 4 条认身份 hook
- **CLAUDE.md**：映射表、hook 表、闸顺序、目录结构同步更新

### 保留的 hook

| Hook | 脚本 | 保留原因 |
|------|------|---------|
| Stop | builder-loop-stop.sh | loop 核心闸（PASS_CMD 驱动），与认身份无关 |
| PreToolUse:Agent | reviewer-timing-check.sh | 时序控制（phase=active 时拦 reviewer），按 phase 字段判而非 agent 身份 |

### 设计哲学依据

- **原则四（改输入条件，不改输出约束）**：认身份 hook 全是末端输出约束（拦它别读、拦它别写），是原则四点名的脆点。新架构下 subagent 天然独立，这些约束的前提（共脑串味）消失，删除是正确路径。
- **原则五（三次就是架构缺陷）**：tester 写主仓 / e2e 无限循环 / 锁不写——三个 bug 同一个根（subagent_type 读空），证明 per-agent-type 锁是架构缺陷而非单点 bug。

## V4.10 fork-aware stop hook（2026-07-02）

> **已在 V5.0 中退役。** L2C 闸及相关锁机制（lock-utils.sh / SubagentStart 写锁 / SubagentStop 清锁）随认身份隔离整体删除。保留此条目作为历史记录。

stop hook 新增 L2C 闸：fork subagent 锁存在时静默 exit 0，等 fork 完成再判。解决 builder fork 后台改文件时 stop hook 提前触发导致 no_progress 误判的问题。`lock-utils.sh` 白名单加 `fork`，SubagentStart hook 自动为 fork 写锁，SubagentStop 自动清锁。

## V4.9 tampering 检测迁移到 reviewer 语义判定（2026-07-01）

删除 `early-stop-check.sh` 的机器层 `suspected_test_tampering` 早停（section 5），改为 `reviewer.md` 步骤 2 新增「测试变更合法性审查」维度。消除 3 次误杀（L3 改动适配测试、删已删函数测试、需求翻转场景）。`loop.schema.yml` 删除 `early_stop.protected_path_changes` 字段。设计哲学依据：原则一层错位——语义判定不应伪装成机器判据，reviewer 独立 agent 是正确的判定层。

## V4.8 E2E 沉淀 + 分级（2026-07-01）

**1. planner YAML 统一**：plan `<!-- e2e-cases -->` 标签格式从 markdown 自然语言统一为 YAML（id/input/hard_rules/llm_judge/level），消除 tester 解析转换成本。

**2. tester 沉淀步骤**：E2E 验收 all_pass 后，读 `e2e_cases_path`（从 loop.yml 配置）→ 去重 → 补全 hard_rules（从执行结果提取 actual tool_calls/steps）→ 自动标注 level → append 到项目回归集 YAML。输出 `E2E_SEDIMENT: N new cases appended`。

**3. level 分级过滤**：case 加 `level: fast|full` 字段。tester 根据 `e2e_level` 参数过滤 case（fast=只跑 L1 硬规则，full=全部）。PA Bot `e2e_agent_test.py` 同步支持 `--level` 参数。

**4. loop.yml 新字段**：`e2e_cases_path`（回归集路径）+ `e2e_level`（过滤级别，默认 full）。stop hook inject 消息传递两个字段给 tester。

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
