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
   │                 ──────finalize────▶ commit-tree + update-ref CAS → terminal（worktree 留给 bl cleanup）
   └─ Stop hook ◀─────────────────────── 终态未复盘 → exit 2
                     ──────retro record▶ 复盘入 ledger → session 解绑
```

runtime 不保存"下一步让谁做"，也不保存"角色是否在跑"：`readiness()` 每次从 evidence、git 与事件流派生。ledger 唯一写入者是 `bl`（`ledger.mutate` flock + seq+1 + 原子写）；绕过 checkpoint 的提交会在下一次 `assert_identity` 时被拒。runtime 的内部 git 操作统一 `-c core.hooksPath=/dev/null`——checkpoint / integrate / rebase 是事务记录不是交付提交；只有 `finalize --run-commit-hook` 那一次跑目标仓库的 hooks。

## 为什么 tester 看不到实现

proof 只能证明"测试能抓住偏离当前实现"，证明不了"当前实现符合 behavior"：看着实现写的测试会把实现的 bug 一起抄进断言，mutation 照样通过。独立性必须来自信息隔离（原则一）。做法：

- tester 有自己的 worktree，分支从 run 起点（`tester.base`）长出；只有目标分支改过 tester 的测试文件时才 rebase（见下文 rebase 一段）；contract 是它唯一的输入，所以 behaviors 带 `boundaries` / `invariants`，接口签名写进 `interfaces`。
- 两段式：盲写阶段新接口写不出 mutation patch（看不到要破坏什么），`patch_file` 可缺省；首次 integrate 后读隔离解除，readiness 给 `resume_tester` 让同一 agent 补 patch。是否已 integrate 由 `candidate.checkpoints` 里有没有 `role=integrate` 派生。
- integrate 用**路径叠加**而不是 merge：`git checkout <tester.head> -- <存在的文件>` + `git rm <被删的文件>` + 一次普通提交。`git rebase` 会丢 merge commit 并重放 tester 的提交，之后再合同一文件必然 add/add 冲突；叠加让候选历史保持线性、重复执行幂等。一律用 ledger 里的 SHA 而非分支名（tester 的 handback 登记可能正在提交）。
- 测试文件集合 = `git diff --name-status tester.base..tester.head`（含删除）；"候选是否需要 integrate" = 这些路径在两边的 blob 是否一致。两者都不落盘。

## 模块

| 模块 | 职责 |
|---|---|
| `config` | `.claude/loop.yml` → `pass_cmd[] / max_iterations / proof_runner / worktree.root`；PyYAML 缺席时用内置子集解析。`start` 读目标分支 HEAD 里已提交的那份（#411：与 `contract revise` 读候选 HEAD 对称，冻结的判据参数就是随交付合入的那份），目标提交没有跟踪 loop.yml 时才退回工作区；两者不一致不报错但 start 输出 `loop_config_source.worktree_differs`，ledger 记 `repo.loop_config_source`；`{main_repo}` 只有 `expand_main_repo()` 一处展开实现，machine 与 proof 都调它（#391，原则二） |
| `contract` | 标签提取、校验（允许键来自 `FIELD_TABLE`，未知字段入口拒绝）、三面 digest、`classify_change`、glob 匹配；**`write_rejection()` 是路径归属与保护的唯一判定入口**（checkpoint / PreToolUse / mutation patch 共用） |
| `ledger` | schema @2 校验、mutate、events、session 指针（读后回核 owner）、`peek`（不校验 schema，给 doctor / runs / abandon 处理旧版 ledger） |
| `worktree` | 每个 run 的 `builder/` 与 `tester/` 两个 worktree（创建失败回滚）、临时 worktree（支持叠加与删除）、身份 / clean 断言。依赖准备（#383）：`worktree.link_paths` 声明的路径在建 worktree 与 preflight 快照时从 `worktree.link_source`（默认仓库根）软链接过去，只建链接、不复制不安装；`residue` / `assert_clean` / `worktree_fingerprint` 的 `ignore` 参数把这些链接排除在「现场改动」之外（链接由 runtime 建，不是写入者的改动），`prepare_links` 从 ledger 的 `loop_config` 读声明，run 内不重读 loop.yml |
| `run` | start / status / checkpoint（分角色、`--dry-run`）/ integrate / resume / hold（`--reason` / `--release`）/ abandon / contract validate\|revise。仓库解析两个入口：`resolve_repo_root()` 保留用户选的 linked worktree（start / validate / runs / doctor / cleanup），`run_roots()` 在定位已有 run 时依次试选仓与主仓 |
| `machine` | pass_cmd 三态、超时、信号终止（无效观察）、执行后 worktree 变更检测、失败签名、`tester_files_mentioned`、基线预跑 `preflight`；stage `cmd` 与 `ready_cmd` 执行前都经 `expand_main_repo` 展开 `{main_repo}`（与 proof 同源） |
| `process` | 门禁命令的执行边界：一次调用独占一个进程组，超时或直接进程被信号带走时先整组回收再返回（#363） |
| `evidence` | tester 文件与 integrate 需求的 git 派生、投影、`state()`、`role_running()` / `gate_running()`、blockers（授权窗口）、`readiness()` |
| `proof` | spec 结构校验、`build_argv`、junit 解析与 id 映射、候选 / 反例判定、四步执行、失败签名 |
| `finalize` | 前置检查、commit-tree（可选 `--run-commit-hook`）、intent、CAS、所有 on-target worktree 的 checkout 同步、恢复、rebase |
| `retro` | 确定性信号派生（只读 ledger）、复盘记录校验、cleanup |
| `hooks` | 六个 handler、结果标记解析、tester 读隔离与角色写边界、心跳续租；注入的上下文 = `brief.render()` |
| `brief` | 角色视角事实的**唯一来源**：写边界、候选可读性、待办；结构化 + 文本两种形态，`bl brief` 与 SubagentStart 同源；reviewer 另带文档引用线索（`doc_reference_hints`，现算调 `doc-lint.sh`，≤4 秒，失败降级为 `error`，不落盘） |
| `doctor` | 只读诊断；`missing_hooks()` 按 `HOOK_SPEC` 逐项核对 settings.json，doctor 与 `bl start` 共用 |
| `hookspec` | `HOOK_SPEC`：要注册哪些 hook 的唯一定义，install.sh 按它注册（#309） |

`hooks/bl-hook.sh` 先用纯 bash 从 stdin 抠 `session_id` 并查 session 指针，没有绑定就直接退出、不起 python——PreToolUse 挂在 Read / Bash 上，无绑定 session 的开销必须接近零。

## 写边界

`write_rejection(authority, role, path)` 依次判：`protected_paths` → 归属（**tester_write 优先**：两边 glob 都命中归 tester，构造上不相交，`builder_write:["**"]` 也碰不到测试）→ 角色不符 → 控制面规则。控制面冻结的是**规则**（`authority.control_basenames`：pytest.ini、pyproject.toml、conftest.py、Makefile、package.json、go.mod、Cargo.toml、BUILD、WORKSPACE、loop.yml 等）而不是文件列表：新建的 conftest.py 同样能劫持测试收集。规则只拦「builder 靠 glob 顺带命中」的情形——在写边界里字面点名即视为计划授权；tester_write 内的归 tester。`bl contract validate --check-repo` 在规划期列出会被保护的命中项，让用户中断发生在规划期。

`builder_write` 的条目**不得落在 `tester_write` 之内**（`validate_contract` 直接拒）：否则 checkpoint 判它归 tester、而 builder 以为自己有授权，两个角色都不敢动（#240）。要让测试跟着实现变，在 behavior 里写明，由 tester 改。注入给 tester 的 brief 也只列它自己的写边界，不复述 builder 的。

builder 与 tester 各看各的 worktree，未提交改动互不可见；删除类任务的旧测试由 tester 依据 contract 清理，两个角色不会互相整单拒绝。

## evidence 投影（dependency_digest 的输入）

| kind | 投影 | 何时 stale |
|---|---|---|
| machine | 三面 digest、候选输入 | 候选输入或 contract 变化 |
| tester | tester 分支上测试文件的 `{path, blob}`（删除记 null）、mission digest | tester 再次提交、mission 变 |
| proof | 候选输入、tester 文件 blob、behavior ids、proof_spec digest、assurance digest、proof 判定规则标识 | 候选输入或测试变化、补了 patch、proof_runner 变化、判定规则升级 |
| reviewer | 候选 **HEAD**、三面 digest、machine / tester / proof 各自 `{status, dependency_digest, state}` | 候选变化、任一前置证据变化或失效 |

「候选输入」由 `evidence.candidate_facet()` 给出，是 machine / proof 真正读得到的候选内容，不是碰巧承载它的 HEAD（原则一）：

- loop.yml 没声明 `evidence_neutral_paths` 时，它就是 `{"candidate_head": <head>}`——键名与取值都与该机制引入之前逐字节相同，升级 runtime 不会让在跑的 run 失效。
- 声明了就换成 `{"candidate_inputs": <digest>}`，digest 取候选全树剔除中性路径后的 `(mode, path, blob)` 列表。带 mode，所以纯 `chmod` 也算输入变化。结果按 `(repo, head, 中性集合)` 缓存——git 对象不可变，而一轮 readiness 要算四次投影。
- reviewer 不参与这套剔除：原则一要求「Reviewer 和文档审查始终面对完整 integrated HEAD」，所以只改文档的 checkpoint 仍让 reviewer stale，只是不再连累 machine 与 proof。
- 中性路径是项目事实（这个仓库的判据读什么），只在 loop.yml 声明，由 start 冻结进 assurance 面计入 digest，改它需要一次 `contract revise --authorize`；planner 写了也以 loop.yml 为准。

rebase 改的是 `repo.target_start_head` 与候选 HEAD。漂移路径（`tester.base..target_start_head`）与 tester 文件**不相交**时 `tester.base` 不动，tester evidence 不因漂移而失效，漂移进来的文件也不会被算成测试文件。**相交**时（#298）tester 的测试是相对过期基线定义的：此时按路径叠加会用旧基线的整文件抹掉目标分支的改动，所以 `bl rebase` 在候选之后把 tester 分支也 rebase 到 `target_start_head`，更新 `tester.base` / `tester.head`（输出 `tester_rebase.status`：`not_needed` / `rebased` / `conflict` / `deferred`）。零冲突时重叠文件内容变了，tester evidence 自然 stale，续接 tester 确认；冲突时 rebase 停在 tester worktree 里由 tester 解；tester 在跑或 worktree 不干净则推迟。只要还相交，integrate 就报 `INTEGRATE_TESTER_BASE_STALE`，readiness 给 `rebase` 或 `resume_tester`。

候选里的测试文件只是 integrate 从 tester 分支派生的副本（原则二），所以候选 rebase 停在冲突上时，tester 拥有的路径直接取目标分支一侧并继续，只把 builder 的冲突留给 builder；那些文件真正的合并由 tester 分支的 rebase 完成，integrate 再叠回来。builder 本来就不能写这些路径。

两处 rebase 停在冲突上时都记一条 intent 事件（`candidate_rebase` / `tester_rebase`，含 `from_head` 与 `onto`）。冲突解完、`git rebase --continue` 之后 HEAD 已经前进：只有事件对得上（`from_head` 就是 ledger 的 HEAD）且结果落在 `onto` 之上，才采纳为新 HEAD（原则七），否则照常 `WORKTREE_HEAD_MISMATCH`。tester 的 checkpoint（含交卷登记）先做这一步，所以 tester 解完冲突可以直接交卷。

reviewer evidence 照常随候选 HEAD 作废（原则一）；brief 的 `target_drift` 从上次审过的 `reviewed_head` 派生漂入的提交与路径、与候选改动相交的路径、patch 是否未变（`git patch-id --stable`），让复审直接看漂入变更与候选的交互。

## proof

命令来自冻结的 `assurance.proof_runner`（loop.yml 声明）：环境依赖因此进入证据输入，tester 不再把机器侧 venv 的绝对路径藏进 argv。`shlex` 拆分、不过 shell，只展开 `{main_repo}`。tester 的 proof_spec 每组只有 `kind / behavior_ids[1] / test_ids / timeout / patch_file? / reviewed_boundaries?`。mutation 组的 patch 以文件交：tester 用 `git diff > <临时目录>/x.patch` 生成，结果行里只给绝对路径 `patch_file`，runtime 交卷时读取原始字节，换成 ledger 里的 `patch`（ledger 只存字节）。只给 inline `patch` 正文 → `PROOF_SPEC_INVALID`：模型转述长 patch 会丢空白上下文行、字符走样，patch 因此一律走文件传递原始字节（原则四）。`patch_file` 须是绝对路径、UTF-8、≤256 KiB，realpath 不能落在候选 / tester worktree / 主仓之内；test id 以 `-` 开头拒绝，文件部分必须是 tester 拥有且存在于 tester 分支的路径；framework=pytest 时文件部分必须是 `.py`，否则 `PROOF_SPEC_INVALID`（`suggested_owner=contract`，runner 由 loop.yml 冻结，tester 修不了）；同一分支再用 `ast` 静态解析该文件，核对 `file::func` / `file::Class::method` 的符号真实存在，参数化 id 只看第一个 `[` 之前（参数部分本身可能含 `::`，#310），不存在时报 `PROOF_SPEC_INVALID` 并列出 `details.test_ids_missing`（#397）。源文件读不到或解析失败、有 `from x import *`、类声明了基类时找不到的方法、以及导入名 / 赋值别名下的成员，一律不判缺失——这些符号的子符号不在本文件里，那是别的失败，留给 proof 的 `missing` 兜底。该 framework 下允许哪些 kind 只由 `proof.kinds_allowed` 给出（generic 只有 `mutation`，另加按 proof 下限放行的 `reviewed-boundaries`），`check_structure` 与 tester brief 都引用它，不各抄一份（#380 #265，原则二）。

pytest 框架下结论取自 junit xml：候选阶段要求 rc==0 且每个声明 id 都匹配到用例并全部 passed（skipped / xfail / 未执行都不算）；反例阶段要求 rc==1、全场无 `<error>`、且**声明的每个 test_id 匹配到的全部用例都是 `<failure>`**——green / mixed（参数实例只红一部分）/ missing 都不算证明。`test_ids` 是证据名单，不是该 behavior 的全部相关测试名单：只检查现役行为的守卫测试不列入，仍由 machine 全量运行（#370，原则一）。判据只取 junit 的结构位，不看失败信息的文本：`<error>` 是 setup / teardown / collection 出错，测试没跑起来，没有鉴别力；`<failure>` 是 call 阶段失败，测试跑到了且被破坏的实现让它红了，`KeyError`、解包错与 `AssertionError` 同等算数（原则四；#114 与 #255 是同一判据两个方向的误判）。`<error>` 查全场而非只查声明 id——collection 错误可能挂在别的 node 上。代价是「patch 破坏得太狠、测试在函数体内 import 失败」会算作反例成立，由 `TEST_MUTATION_INVALID` 的 patch 约束与 reviewer 兜底。id 映射用 classname 后缀匹配（monorepo 子目录自带 ini 时 rootdir 会变），不带 `[` 的 id 匹配全部参数实例。generic 框架只看退出码，只能做 mutation，且没有逐用例结果——强证明组因此只许一个证据 test_id，否则无法逐条证明（`PROOF_SPEC_INVALID`）。generic 没有 junit 可读，分不清「测试红了」和「runner 没跑起来」，因此在**打 patch 之前**先在同一个 mutation 现场跑一次对照：非 0 就报 `TEST_PROOF_RUNNER_FAILED`（`phase=control`，`suggested_owner=contract`），不产出任何 `assertion-failure`——命令起不来是零观察，不是证据（#375，原则一）。对照只比较同一现场两次运行的成败，不解读输出文本（原则四）；它证明的是「命令能跑起来且被 patch 打翻」，不是「断言抓住了破坏」，generic 的强证明只到这一档。对照与正式运行共用候选 worktree 的依赖：`proof_runner.link_paths` 声明的路径（如 `node_modules`）在建 baseline / mutation 现场时从候选软链过去，两处输入因此同源（#376）；声明的路径在候选里不存在时同样报 `TEST_PROOF_RUNNER_FAILED`（`phase=link`）。链接不复制、不安装、不清理链接目标，依赖目录归声明方负责；链接路径本身不算「现场被改动」。

每个 behavior 的 proof 下限写在 contract（缺省 strong）；`reviewed-boundaries` 必须在该 behavior 上显式放行。反例阶段另记 `counterexample.per_id`：每条声明 id 是 `red`（它匹配到的全部用例都在 call 阶段失败）/ `mixed`（只有部分参数实例失败）/ `green`（跑了没红）/ `missing`（没跑到）。**只有全部声明 id 都是 red 才 PASS**，其余一律 FAIL 并列出未全红 id（#370）。判定规则标识 `proof_rule=every-declared-case-red@1` 进入 proof 的输入投影，旧规则下形成的 PASS 因此自动 stale 并重跑，不能冒充新规则结论（原则一）。

mutation patch 分两处检查，检查对象都是 ledger 实收的那份字节（`normalized_patch` 是两处共用的唯一规范化）：

| 时机 | 检查 | 失败 |
|---|---|---|
| tester 交卷（任何一轮）与 proof 入口：`check_structure` | 路径 `write_rejection(…, "builder", path)` | `PROOF_SPEC_INVALID`，当场打回；原因是 `protected` / `outside_authority` / `control_file` 时 details 带 `suggested_owner=contract` 并提示 tester 改交 `insufficient_spec`（contract 没把行为所在文件交给 builder，tester 改 patch 解决不了，#286）|
| tester 交卷且首次 integrate 之后：`validate_spec` 的预检 | 目标是交卷时候选 head 上的普通文件；临时 `GIT_INDEX_FILE` 上 `git apply --cached --check`，不碰候选 worktree | `PROOF_SPEC_INVALID`，当场打回 |
| proof ③ 段 | 上述全部 + 在临时 worktree 真实 apply、改动路径与声明一致、执行前后 diff 不变 | `TEST_MUTATION_INVALID`，owner=tester |

proof 入口不做可应用性预检：交卷后 builder 又改了实现导致对不上，要落到 ③ 段按 tester 失败记账，而不是以 spec 无效挡在门外。

machine / preflight / proof 的子进程都拿到本次调用独占、结束即删的 `PYTHONPYCACHEPREFIX`（run 目录 `tmp/` 下），不读工作树里的 `__pycache__`：那里的字节码不在任何 digest 里，保持字节数的变异在同一秒内 apply/revert 后会被 CPython 当成新鲜的（原则一）。每次调用新建，因为 proof 的临时 worktree 路径跨调用复用；工作树里的缓存不删（原则三）。

**进程边界**（#363）：stage 与 proof runner 一律经 `process.run()` 以 `start_new_session=True` 启动，一次调用独占一个进程组。runtime 超时，或直接进程被信号带走时，先 `killpg` 整组再返回——判定与清理在同一次调用里闭环，残留的孙进程不会再与下一次 machine 抢 CPU。清理范围只有本次调用自己建出的进程组：不按进程名、命令行或其他 run 的 pid 查杀；进程正常退出时不额外清理（有意后台化的子孙归命令自己）。子孙主动 `setsid` 逃出进程组不受此约束，这与既有敌手模型一致。

**仓库身份**（#364）：`--repo` 给的路径就是输入身份。`resolve_repo_root()` 只取该路径的 `git rev-parse --show-toplevel`，共用 `.git` 的 linked worktree 不再被静默改写回主仓——start / contract validate --check-repo / runs / doctor / cleanup 因此读的是用户选的那条产品线的分支与 `loop.yml`。定位**已有 run** 时用 `run_roots()`：先试选仓自己的 runs 目录，再试它的主仓（runtime 建的 candidate / tester worktree 属于主仓的 run），以实际存在的 ledger 为准，`RUN_NOT_FOUND` 的 details 列出搜索过的根。读不到的根（EACCES）不等价于「这个 run 不存在」（#404，原则一/三）：它被跳过并作为事实报出，`RUN_NOT_FOUND` 的 details 与 brief 的 `unreadable_roots` 各带一份；`list_runs` 对不可读的 run 目录同样只标记 `unreadable`、不中断整个列表。`--repo` 既可写在子命令前也可写在子命令后，两处指向同一个参数（#413）。

## readiness → next_action

terminal →（无 retrospective `retro`，否则 `done`）；有 blocker → `needs_user`；tester 需要干活且没在跑 → `spawn_tester` / `resume_tester`（**先于** builder 的 `checkpoint`：tester 后台并行，越早放出去越好）；builder 无 checkpoint → `checkpoint`；tester 在跑 → `awaiting_tester`；需要 integrate → `integrate`；machine 未过：machine 门禁正在执行 → `awaiting_gate`，fail 且失败日志里出现归 tester 的路径且 tester 尚未答复 → `resume_tester`，否则 `machine`；proof 未过：tester 在跑 → `awaiting_tester`，proof 门禁正在执行 → `awaiting_gate`，缺 mutation patch 且 tester 在首次 integrate 后还没答复过 → `resume_tester`，已交付的 patch 打不到当前候选（`stale_patch_groups`，只在这里问）且 tester 在最近一次候选变化后还没答复过 → `resume_tester`（#305 #398），fail 且 owner=tester 且 tester 尚未答复 → `resume_tester`，否则 `proof`；reviewer 未过：在跑 → `awaiting_reviewer`，fail 且有 owner=tester 的 blocking/major 且 tester 尚未答复 → `resume_tester`，否则 spawn / resume；reviewer 已过但**又被续接在跑** → `awaiting_reviewer`；全过 → `finalize`。门禁在跑是 machine / proof 分支自己的输入条件：下一步取决于它的新结论，所以不按旧结论催（#279）；与门禁结论无关的动作（integrate、awaiting_tester、reviewer 阶段）照常给出；只有 preflight 在跑时照常给 `machine` / `proof`——它们会自动排队等 preflight。

「tester 尚未答复」用事件时间戳比较（ISO 微秒，UTC，字典序即时间序）：答复过而失败依旧时回到 `proof`，让同签名计数生效，避免无限续接。

machine 的 `resume_tester` 出口与 proof 的那条同构：builder 改不了 `tests/**`，归 tester 的 machine 失败若没有出口，就只能靠 `MISSION_REVISION` 绕过（#249）。日志里哪些路径归 tester 由 `contract.path_owner` 裁决，范围是候选树 ∪ tester 分支——不是「tester 本 run 改过的文件」，否则因契约变更而失效的**既有**测试永远匹配不上。

reviewer 已 pass 之后那条兜底对称于 tester 的 `elif tester_run`（它同样不看 tester 自己的 state）：builder 否决上一轮结论、续接同一 reviewer 复审时，没有这条就直接落到 `finalize`，复审在 readiness 与 Stop hook 里完全不可见（#269）。

reviewer 判 pass 时给出的 `owner=tester` 的 finding 不派发（派发会引出复审循环），但会由 `finalize` 在返回里以 `unaddressed_findings` 列出，不让它悄悄消失。其中的 minor 同时以可选待办 `consider_minor_findings` 出现在 tester brief 里（#290）：readiness 不变，是否续接 tester 去改由 builder 决定。

**续接与唤醒**（#306）：角色残留的后台任务结束时，CC 会把已交卷的角色再唤醒一轮；唤醒与 SendMessage 续接的 SubagentStart 字段完全相同（CC 2.1.280 探针）。能区分两者的只有 builder 的 PreToolUse(SendMessage)：它记一条 `resume_request`，一条请求只对应一次开轮（请求之后又出现开轮、唤醒或结论就作废）。没有请求的 SubagentStart 记 `role_wake`；最近一次开轮事件是 `role_wake` 的轮次里，SubagentStop 与 handback 都静默——重发的旧结论不登记、不计不合规，也不计轮次（retro 另有 `S-role-wake`）。否则旧结论会被登记成唤醒时刻候选 HEAD 上的 evidence（原则七）。

**角色是否在跑**不落盘：最近一条生命周期事件是 `role_start`（或要求重发的 `role_malformed`）且 40 分钟租约未过期（`role_wake` 不算在跑）。该 agent 的每次 PreToolUse 续租，只 touch `run_dir/heartbeat-<role>`，不写 ledger。只用于 `awaiting_*`（抑制 Stop 回拉），不给任何 CLI 加闸；agent 失联则租约到期后回到 resume。

**门禁是否在跑**同样不落盘：machine / proof / preflight 执行期间各持有 `run_dir/gate-<holder>.lock` 的 flock（文件里写 pid，本进程自己持有不算）。进程死掉锁自动释放，没有残留状态要清理。同一门禁重复启动 → `GATE_BUSY`；machine / proof 遇到 preflight 在跑会排队等它（两套全量测试并发会把彼此挤成假超时），排队期间自己的锁已持有，所以 Stop 看得到它在等。

**hold**（#291 #299 #368）：多会话按顺序发版时，合入时机由外部条件决定，不由 gate 决定。`bl hold --reason` 要求当前 mission revision 下 gate 全过过一次（每个 revision 首次全过时记一条 `gates_passed{revision}` 事件；旧事件没有 revision 视作 1），不要求此刻全绿——rebase 之后、重验之前集成方要的正是「先别重验」；hold 不削弱判据，finalize 仍只认四项 fresh pass。hold 不再要求用户回答：等待外部协作放行属于正常工程活动，不升级为用户重规划；`--reason` 仍必填，builder 必须在当轮回复里说明已暂缓、在等什么。集成方、跨会话消息与任务通知都不记 `user_input`，不能用来解除 blocker 或修订 contract。release 与 rebase 都不重置锚点，同一 mission revision 内重新全过后可以再 hold；只有 mission 修订才要在新目标下重新全过。成功后只要候选在 hold 之后没再变，readiness 就给 `held`（证据 stale 也一样；builder 自己 checkpoint 了说明工作已恢复，照常给真实动作，重新全绿后再表现为 held），Stop 放行、`finalize` 报 `HOLD_ACTIVE`。hold 从 `hold` / `hold_release` 事件派生，不落 ledger 字段（原则五），并作为 `S-hold` 进入复盘。`bl hold --release` 不需要授权，只是回到正常流程，目标分支前进了照常 `TARGET_DRIFT` → rebase。

**blocker** 按最近一次用户授权以来的窗口计算：`MAX_ITERATIONS`（窗口内 machine **失败**次数达到上限；integrate / rebase / checkpoint 逼出来且通过的重验不计，原则十针对的是反复撞墙）、`NO_PROGRESS`（machine 同签名 3 次）、`PROOF_STALL`（proof 同签名 3 次）、`REVIEW_CONTRACT`（reviewer 提了 owner=contract 的 blocking/major）、`WAITING_FOR_USER`。前三个激活时 `bl machine` / `bl proof` 直接 exit 3 不执行——上限是真的停止点。`bl resume --reason` 记一条 authorization，但要求 blocker 之后存在 `user_input` 事件：模型不能自己给自己授权。该事件**只由 PostToolUse(AskUserQuestion) 写入**——后台 subagent 结束时唤醒主 session 的任务通知也会触发 UserPromptSubmit，那不是真人。

## hook 接线

| event | matcher | 逻辑 |
|---|---|---|
| SessionStart | — | 纯 bash、不起 python（每个会话都会触发）：往 `CLAUDE_ENV_FILE` 写 `export PATH=<本仓 bin>:$PATH`。CC 把它注入之后的每条 Bash，主会话与 subagent 都生效，SKILL 与 runtime 提示里的裸 `bl` 因此可直接用 |
| Stop | — | 终态已复盘 → 解绑放行；waiting → 放行；`awaiting_*` 与 `held` → 放行且不计 stall；其余（含终态未复盘）→ exit 2 + next_action；`stop_hook_active` 且 seq 未变连续 3 次 → 放行并记 `stall_escape` |
| SubagentStart | tester\|reviewer | 登记 agent_id（保留 turn；换了 agent_id 记 `role_replaced`）、记 `role_start{candidate_head}`（reviewer 另带 `candidate_fingerprint`：候选的 HEAD / index / status / dirty 内容摘要，取不到记 null，#312）、注入上下文（= `brief.render()`；tester 集成前后内容不同）。已登记的 agent_id 而前面没有对应的 `resume_request` → 是残留后台任务把它唤醒（#306）：只记 `role_wake`，不开新一轮、不注入 |
| SubagentStop | tester\|reviewer | 只认登记的 agent_id。本轮已有结论或已记 fail → 放行；本轮有 handback 尝试（没开 handback 的环境不会有）→ 不解析 `last_assistant_message`（调用方收不到，常是收尾句），只有不合规 handback 时 exit 2 要它重新 handback；本轮没有 handback → **兜底**解析 `last_assistant_message`，登记规则与 handback 相同（`via:stop`），不合规 → exit 2（与 handback 共用计数，第 3 次记 fail） |
| PreToolUse | AskUserQuestion / EnterWorktree\|SubagentHandback | 写 waiting / deny。SubagentHandback：登记角色（非唤醒轮次、本轮未记 fail）交卷**投递前**做与登记相同的校验（结果行、结构、patch_file、patch 可应用性；tester 的 test_ids 查 worktree 工作树，因为登记时才提交），不合规 → deny + `role_malformed{via:handback}`，被拒的报告不会送达 builder（#303）；到上限记 fail 并放行，该轮的 PostToolUse 不再登记。只校验不登记：deny 之后工具不执行 |
| PreToolUse | Read\|Grep\|Glob\|Write\|Edit\|MultiEdit\|NotebookEdit\|Bash\|SendMessage\|TaskStop | 主会话的 SendMessage，`to` 是已登记角色的 agent_id → 记 `resume_request`（续接的持久化 intent，#306）。主会话的 TaskStop，task_id 是记录在案、未停的角色任务 → 当场记 `role_background_stopped`：任务早已自己结束时 CC 报 No task found、PostToolUse 不来，只有这里看得到 builder 的意图（#308）。其余仅对登记的角色：续租；角色的 Bash 带 `run_in_background: true` 一律拒（后台进程活过交卷，结束时的任务通知会把角色反复唤醒，#283；只看这个结构化字段，不解析 `&` / `nohup`）；tester 的 Bash 命令串触及候选 worktree 路径（原样、realpath 或 `..` 归一后）集成前后都拒，集成后读候选走 Read / Grep / Glob 或 `git show <候选分支>:<路径>`（#234）；reviewer 写一律拒；tester 写只许自己 worktree 的 tester_write；集成前 tester 读 / 搜候选拒（先 realpath，Grep/Glob 必须显式 path，Bash 命令串含候选路径或分支名拒——尽力而为） |
| PostToolUse | AskUserQuestion | 清 waiting、记 `user_input`（授权续跑的唯一依据） |
| PostToolUse | SubagentHandback\|Bash\|TaskStop | Bash：登记角色的前台命令超时被 CC 转到后台（`tool_response.backgroundTaskId`，PreToolUse 看不到，#283）→ 记 `role_background{task_id}`，并用 additionalContext 告诉角色（CC 2.1.280 实测能送达 subagent）；TaskStop：主会话停掉的正是记录在案、PreToolUse 还没登记过的角色任务 → 记 `role_background_stopped`（按剩余列表去重，不会记两次）。未停的任务出现在 `bl status` / Stop 拉回消息 / finalize 与 abandon 的输出 / retro 信号 `S-role-background`，不拦 finalize（#308）。SubagentHandback：开了 handback 的环境里角色结果的登记点。只认登记的 agent_id；从 `tool_input.message` 取最后一条结果行；不合规 → exit 2 要它重新 handback（≤2 次）→ 第 3 次记 fail；与本轮最后一条结论 payload 相同（`payload_sha256`；tester 还要求 worktree 无未提交改动）→ 放行不重记；登记成功后不合规计数清零；tester：提交 tester worktree + spec 结构校验 + evidence + proof_spec；reviewer：起点 HEAD ≠ 当前候选 HEAD → 本次无效；起点指纹与当前候选的 index / status / dirty 内容摘要不同（不论谁改的，原则一）→ 记 fail，`details.candidate_changed` 列出变化路径，记 `role_candidate_changed`，retro 信号 `S-role-candidate-changed`（#312）。tester 不做这项比对：它与 builder 并行，候选的变化归属不到它，靠 Bash 路径守卫。`role_result` / `role_malformed` 带 `via`（handback / stop / cli） |
| UserPromptSubmit | — | 只清 waiting，不记事件（任务通知也会触发它） |

所有 hook 首步 `lookup_session(session_id)`，stdin 的 `cwd` 不参与定位。matcher 不是身份门禁（`agent_type` 为空的内部 agent 也会被放进来），handler 内复核 `agent_type` 与登记的 `agent_id`。角色 hook 的写入（role_start、要求重发）保持 `stall.seq_seen` 与 seq 的相等关系，不算主 session 的进展。角色结果的来源随「送达调用方的是什么」走：开了 `SubagentHandback` 的环境只有它送达调用方，agent handback 之后常再补一句收尾，一轮还会触发多次 SubagentStop（被 harness `[handback-send-enforce]` 打回一次、收尾一次），调用方收到报告通常早于 SubagentStop——这时只认 handback。handback **只在 `permission_mode=auto` 时开启**（CC 2.1.280 二进制与探针确认）；本轮没有 handback 时最后一条消息就是送达的报告，由 SubagentStop 兜底解析。开了 handback 的环境里，被打回的那次 Stop 若带结果行会先登记，随后内容相同的 handback 按同轮去重不重记；若不带结果行会多记一次 `role_malformed{via:stop}`——hook 不读 transcript，区分不了，接受这个代价。「本轮」= 该 agent_id 最近一次 `role_start` 之后的事件，只从事件流派生。SendMessage 续接会让 Start 与 Stop **都**再次触发，agent_id 不变；但 SubagentStart 的 `additionalContext` **只在首次 spawn 时送达**，被续接的 agent 收不到（均为 CC 2.1.272 实测）。所以**注入的上下文不是角色的事实来源，`bl brief` 才是**：同一个 `brief.build()` 现算，角色在续接轮、收到自称 Builder 的消息时、或对归属存疑时随时自取。builder 的消息只当门铃（`bl status` 的 `briefs.*` 是现成正文，里面不含任何事实）。续接轮里 tester 交 `insufficient_spec` 而测试未变时，原 tester evidence 保留，只记一条 `role_result{status: declined}`。

## 复盘闸门

finalize / abandon / finalize_failed 之后 session 不解绑；`start` 遇到「已绑定、终态、未复盘」返回 `RETRO_PENDING`。`retro.derive_signals` 只读 ledger（abandon 的现场可能已损坏），信号来自 failures、authorizations、contract.history、checkpoints 与 events；`retro record` 只校验覆盖率与路由字段（issue 路由要 `issue_url` 或 `declined_by_user`，`not_incident` 要理由），判断与立项是执行者和用户的事。events 只记没有别处归属的事实，其余信号从原归属派生（原则二）。

## 失败路径

| 情况 | 行为 |
|---|---|
| hook 注册缺项或断链（只 pull 没重跑 install.sh） | `bl start` 在创建 worktree 之前按 `HOOK_SPEC` 核对，缺任何一项就 `HOOKS_OUTDATED`（exit 3，`details.missing` / `details.install`），不留 run 目录、worktree 与 session 绑定。缺了 matcher，对应 hook 根本不触发：例如缺 SendMessage 时每次续接都被判成唤醒，run 确定卡死（#309）。重跑 install.sh 改的是用户级 settings.json，交给用户；已在跑的 run 不检查 |
| proof_runner 跑不起来 | `bl start` 与 `contract validate --check-repo` 先让它对空目录收集一次（健康时退出码 5；`--version` 不加载插件，不算数），不过就拒绝启动（`PROOF_RUNNER_UNAVAILABLE`）——run 还不存在，改 loop.yml 不需要 revise/授权。冒烟自身超过 120s 单独报「冒烟超时」而不是「起不来」：多半是 `uv run --with-requirements` 首次下载依赖（#394） |
| machine FAIL 的 stage 在基线上也红 | 跑过 `bl preflight` 则 `failure.baseline_red=true`，附上该 stage 的预跑日志 `failure.baseline_log`，提示失败原因未必相同、由 builder 对照两份日志判断（预跑只观察到 stage 粒度的红，推不出「与候选无关」；不比对日志文本，#300）；没跑过则提示可以补跑。判定取 machine 收尾时 ledger 里最新一次匹配的 preflight（排队期间写入的也算），并从其 `stages[]` 派生：超时的 stage 不算红，记为 `baseline_timed_out`（超时是没观察到结果，常见于资源争抢），preflight 结果此时为 `INCONCLUSIVE` |
| stage 声明了 `ready_cmd` 而探针没过（#377 #383） | 探针与 `cmd` 走同一条 `bash -c` 执行路径，只认它自己的退出码，不解析日志文本（原则四）。preflight：该 stage 不跑 `cmd`，记为 `unready`（`baseline_unready`），不进 `baseline_red`，结果 `INCONCLUSIVE`；`machine_commands` 的配对 digest 计入 `ready_cmd`，探针改了不沿用旧预跑结论。machine：`MACHINE_STAGE_UNREADY`（negative，exit 1）——不写 evidence、不追加 failures、不占 `machine_iter`，只记 `machine_stage_unready` event；装好环境后直接重跑，删掉 `ready_cmd` 即恢复旧行为。与信号终止同构：环境没就绪时 `cmd` 没跑，没有判据结果可归因给候选 |
| preflight 建不出快照 worktree（仓库父目录不可写等，#381） | 不抛硬错：结果 `INCONCLUSIVE`，输出 `snapshot_error`，不产生 `baseline_red` |
| machine FAIL | evidence fail + failures 追加；输出 `repeat_count` / `remaining_iterations` / `tester_files_mentioned` / 完整日志 `failure.log`；诊断只看完整日志，失败输出不再附截断副本（#365）。测试写错由 builder SendMessage 给 tester |
| machine 同一输入上显式重试（#278） | `bl machine --retry --reason R`：先要求当前是 fresh machine FAIL、原因为非空，并先记 `machine_retry` event（原因、上一失败签名与 stage、当时候选 HEAD、machine `dependency_digest`），再照常执行；retro 信号 `S-machine-retry`。不重置计数、不改旧 evidence、不绕过上限/无进展 blocker；runtime 不按路径判断失败是否与候选无关 |
| machine 收尾检测到候选工作树变化（tracked 修改、未忽略的 untracked 文件、HEAD 变化，#378） | `MACHINE_WORKTREE_CHANGED`（negative，exit 1）：写入者不可辨（builder、`pass_cmd` 或其他），因此不构成候选失败——不写 evidence、不追加 failures、不占 `machine_iter`，只记一条 `machine_worktree_changed` event（paths、head_at_start、head_after、candidate_head、iter、dependency_digest）。runtime 不清理现场、不 reset、不自动重试；处理掉改动或 checkpoint 吸收后直接重跑。优先级低于 `MACHINE_INPUT_CHANGED` 与 `MACHINE_STAGE_SIGNALED`，高于普通 PASS / FAIL / timeout。只检测收尾残留与 HEAD 差异，不宣称发现执行期间已还原的瞬时改动 |
| machine 执行期间输入投影变化（builder 并发 checkpoint / integrate、contract revise 改了 facets） | `MACHINE_INPUT_CHANGED`（negative，exit 1）：本次观察作废，不写 evidence、不追加 failures、不占 `machine_iter`，只记一条 `machine_input_changed` event，`changed_inputs` 列出变了的投影键。判据是 `evidence.input_changes`（起跑与收尾两份 ledger 的投影比较），而非 git HEAD——没进 ledger 的工作树改动走下一条 |
| machine stage 的直接进程被信号终止（负 returncode，#297） | `MACHINE_STAGE_SIGNALED`（negative，exit 1）：runtime 观察不到谁发的信号（外部会话、命令自身或崩溃），因此不构成候选失败——不写 evidence、不追加 failures、不占 `machine_iter`，只记一条 `machine_stage_signaled` event（stage、returncode、signal、candidate_head、dependency_digest）。日志保留；输入不变时直接重跑，信号作废本身不会生成 PASS。与输入投影变化同时发生时以 `MACHINE_INPUT_CHANGED` 为准 |
| preflight stage 的直接进程被信号终止 | 该 stage 不算基线红（基线红只认正的 returncode），结果 `INCONCLUSIVE`，输出 `baseline_signaled`；超时的 `baseline_timed_out` 同理 |
| proof 执行期间输入投影变化（checkpoint / integrate / tester 交卷换了 proof_spec / contract revise） | `PROOF_INPUT_CHANGED`（negative，exit 1）：与 machine 同一判据，同样不写 evidence、不追加 failures，记 `proof_input_changed` event（含 `attempt`），重跑的 attempt 不变 |
| contract 声明的 `assurance.machine_stages` 在 loop.yml 里缺失 | `MACHINE_STAGE_MISSING`（fatal，exit 2），`bl start` / `contract revise` 经 `freeze_assurance` 拒绝，`validate --check-repo` 列为 problem；run 还没创建，无现场代价。loop.yml 未纳入版本控制，可能被别的 session 改写，这是唯一挡住「stage 静默丢失」的地方 |
| `bl start` 时主仓停在目标分支且有未提交的 tracked 改动 | 候选从目标分支 HEAD 建立，这些改动不在基线里：start 输出 `target_uncommitted`（最多 50 条，只报告不拦截、不落盘） |
| proof runner 起不来 | 候选阶段 rc≠0、一条 junit 记录都没有且不是 runtime 组超时 → `TEST_PROOF_RUNNER_FAILED`，`suggested_owner=contract`（两个角色都改不了，要改 loop.yml）；generic 的 `phase=control`（未打 patch 的对照非 0）与声明了 `link_paths` 时的 `phase=link`（候选里缺声明路径）同样报这个码 |
| proof 组超时 | candidate / baseline / mutation / control 任一阶段被 runtime 按组的 `timeout` 终止 → `TEST_PROOF_GROUP_TIMEOUT`，`suggested_owner=tester`，details 带 `phase` / `group` / `behavior` / `timeout` / `returncode` / 完整日志 `log`；超时不进入红/绿与鉴别力判定（#320）。runner 自己返回 124 不算组超时 |
| proof 失败 | `TEST_PROOF_CANDIDATE_FAILED` / `TEST_PROOF_NOT_EXECUTED` / `TEST_BASELINE_RED_NOT_PROVEN` / `TEST_MUTATION_SURVIVED` / `TEST_MUTATION_INVALID` / `TEST_MUTATION_PATCH_MISSING` / `PROOF_WORKTREE_MUTATED`；`suggested_owner` 指向 builder 或 tester；`TEST_PROOF_CANDIDATE_FAILED` 发生时 machine evidence 对当前候选是 fresh pass → 归 tester 并带 `isolation_hint`（全量过、单跑失败，多半是测试隔离，#313；只看 machine 状态，不解析日志）。失败输出只给完整日志 `log`，不再附 `tail`（#365） |
| reviewer 未过 | finding 的 owner 决定去向：builder 修 / 回 tester / `REVIEW_CONTRACT` 交还用户 |
| 目标分支前进 | finalize → TARGET_DRIFT；`bl rebase` 先动候选，漂移触及 tester 文件时再动 tester 分支；成功后 evidence 自然 stale，tester evidence 只在重叠文件内容变了时 stale |
| 目标分支与上游不一致（#385） | 基线仍是本地 `refs/heads/<target>`（finalize 的 CAS 目标、rebase 的 onto、checkout 同步的起点都指向它），上游只参与准入：`baseline.require_target_baseline` 在 `start` / `rebase` / `finalize` 三处判定，落后（`behind`）或分叉（`diverged`）时拒绝，错误码 `TARGET_NOT_IN_SYNC`（start 用 fatal，另两处 negative，details 带 `relation` / `local_head` / `remote_head` / `upstream_ref`）。runtime 不执行 fetch：上游 ref 从未 fetch 过时报 `unverified`、无 remote 时报 `local_only`，两者都放行并带 `warning`。`ahead`（本地在上游之上）放行。`bl start --allow-stale-target` 只承认开工基线，不改变 finalize 的写回条件；`recover()` 有意不校验——它在完成/回滚一次已写下 intent 的本地 ref 事务，引入外部条件会把半截状态卡死 |
| tester 分支落后且漂移触及它的文件 | integrate → `INTEGRATE_TESTER_BASE_STALE`（negative，details.paths），不提交；readiness 给 `rebase`（tester 冲突时给 `resume_tester`） |
| on-target worktree（任一 checkout 了目标分支的 worktree，含主仓）dirty 与变更路径重叠 | finalize → DIRTY_OVERLAP（details 按 worktree 列重叠路径），不写回 |
| finalize CAS 后中断 | ledger 留 `finalize_intent`；再次 finalize 按目标分支当前 HEAD 恢复 |
| commit hook 改写 tree | `--run-commit-hook` 模式下 FINAL_COMMIT_TREE_MISMATCH，目标分支不动 |
| abandon | 终态 abandoned，worktree 与分支保留；复盘后 `bl cleanup` 回收 clean、HEAD 未漂移且调用者不在其中的 |
| 旧版 ledger（@1）| hook 对它静默；`bl runs` / `doctor` 能列出，`bl abandon --run` 能结束它 |

## 演进方向

演进待办统一跟踪在 GitHub Issues（#367）；评估后明确不做的 Codex 版特性及理由见 issue #233。
