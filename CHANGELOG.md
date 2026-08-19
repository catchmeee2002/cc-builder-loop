# Changelog

## Unreleased

- Assurance engineering correction 现在先把绝对 decision pointer 应用到完整冻结 contract，再验证变更只落在
  Assurance facet；首个 Builder checkpoint 前的 correction 以冻结 `target_start_head` 作为候选绑定，避免
  相对 facet 指针和空 candidate HEAD 造成错误拒绝。Compact profile blackbox helper 同时自包含仓库根路径和
  `tests.*` 导入，冻结命令可直接从 candidate worktree 执行（#128、#144、#147、#160）。

- Assurance v4 新增 `automatic_nonsemantic` recovery policy：Core 只对绑定当前 action/problem/facet 的精确
  Assurance 增量执行单调修正，并让依赖 evidence 重新验收；Mission、Authority、验收、信任边界、命令
  替换、外部目标、降级、stale binding 与 Git 冲突继续 fail closed。失败/取消 root 可用只读
  `cost_ancestry` 累积 task telemetry，不继承 candidate、角色、Tester source、evidence 或 lease；第三次
  非语义 transition 的一次决定只授权当前及同 category 后续三次（#144、#160）。

- Assurance v4 增加同 Core `compact` profile：仅 revision-one、1–3 behavior、单 machine/blackbox runner、
  无 publication/dirty/external target 的任务可进入；Tester、proof、machine、blackbox、Reviewer 五 gate
  保持独立。任一 correction、replay、recomposition、replacement、renewal 或 eligibility drift 只把派生
  effective profile 升为 full；telemetry 同时公开 profile、expected stages 与互不重叠的实现、验证、编排、
  等待耗时（#128）。

- Builder-loop 现在从单一 runtime 常量公开正式 SemVer `0.1.0`；`version --json`、新旧 ledger、
  retrospective、复制目录和安装回读统一携带 version/commit/dirty/capture status。当前 runtime 只允许当前
  minor 做 active mutation，对上一 minor 仅保留安全终态完成；新 GitHub Issue 改用唯一
  `issue-capture:v2` 嵌套 runtime identity，历史 v1 与 resolution v1 保持只读语义（#147）。

- Machine/preflight failure signature 现在绑定实际 stdout 与 stderr，失败集合收敛不再被误计为三次同错。
  终态 retrospective report v2 同时把每组 Issue routes 绑定到唯一远端更新及 body read-back receipt；旧 v1
  Issue route 保持 `REQUIRED/legacy-unverified`，同步属于 Agent 执行义务，不伪装成用户产品决策（#182、#183）。

- 角色行为实验场新增了七个 CC 遗留风险 canary contract：六个仓库外确定性 fixture 分别覆盖正向结果、
  大 diff 完整审查、文档 ground truth、内容密度、生产者到消费者链路，以及 proof 实际调用点、公开失败
  语义和隔离依赖自包含；一个安全宿主探针复现既有进程
  持锁导致后续验证 timeout 并确认进程组回收。canary 只建立 fresh model 采样前提，结果不写 runtime
  ledger，也不替代交付 evidence；Planner canary 会冻结用户已选择 Builder-loop 实验的激活前置条件，
  并限定为只读行为判断，避免把路线选择问卷或完整 admission 工作误计为目标规划样本
  。正向结果 case 现分别覆盖 Planner、Tester、Reviewer，文档 ground-truth 补充真实 Builder agent，
  生产者到消费者 case 补充 Reviewer；后者冻结 active/forged 可见、archived 隐藏的业务语义，并真实
  改写上游过滤、确认完整链路由绿转红后恢复 fixture（#7、#23、#28、#48、#61、#68、#174）。

- Native Driver 的长程 Reviewer prompt 改为 v2 单事实投影：contract、evidence、publication 与文档扫描
  只在 payload 顶层出现一次，审查契约以 JSON Pointer + digest 引用，并用紧凑 JSON 降低重复上下文。
  Reviewer 同一 dispatch 三次断流且尾部 turn 确认无输出时，会先持久化 thread 前缀与 compaction intent，
  再通过稳定 `thread/compact/start` 完成一次可恢复 compaction 和同身份 generation 续接；不调用 deprecated
  rollback，部分输出、非尾部、能力缺失、前缀漂移或第二次耗尽均停止。Telemetry 同时按真实
  `dispatch_turn_bound` 到 completed/scheduled/exhausted 终点统计 Agent 时间和失败次数，不再把 exhausted
  retry 成本归入 idle/user wait（#198、#199）。

- Tester correction 以最近 machine PASS 划分窗口；前三次修正后，第四次会停在绑定当前 problem/action/
  Reviewer/runtime 的 architecture review。用户通过 Native `resume --reason` 只授权一次额外原 Tester
  correction，不重置历史计数；授权消费后再次失败仍停止，真实 machine PASS 才开启新窗口（#196、#197）。

- Assurance proof 的绝对 `uv` launcher 在固定 frozen/offline/no-env-file 前缀后允许 virtual workspace
  显式选择唯一 `--all-packages`。runtime 为每个 candidate、baseline 与 mutation 执行创建并清理
  proof-worktree 外的临时 `UV_PROJECT_ENVIRONMENT` 与 tracked worktree snapshot，`UV_PROJECT` 只指向
  runtime-owned snapshot，使 editable-install 的 `egg-info`/build metadata 不污染被审 worktree；两者
  执行后统一删除，既不借用 candidate `.venv`，也不放宽 ignored residue。workspace member 依赖继续由
  冻结 `pyproject.toml`、`uv.lock` 与 uv identity 绑定（#195）。

- 新增轻量 `dev-worktree` 生命周期：Codex-native 本仓开发统一进入父目录单一托管根，create/finish
  副作用由 Git common-dir manifest 与精确 intent 绑定；status/doctor 只读分类 orphan、missing、drift、
  residue 和占用状态，recover 只续接既有 intent。未知 worktree 不 adopt/delete，finish 不 force、prune、
  merge 或 push；worktree 只约束候选写入，宿主只读依赖与共享运行态分别保留身份和既有部署事务边界（#190）。

- Assurance v4 新增只读 admission 投影：`validate --repo` 与 `start` 会聚合检查 machine、blackbox 和
  deployment executable，裸命令只从 trusted system PATH 解析，绝对路径绑定 SHA-256，仓库相对命令延迟到
  candidate；宿主 blocker 在 run/Agent 副作用前以 `ASSURANCE_ADMISSION_BLOCKED` 返回。successor 同时把
  source diff 仅记为 exact carryover，新 revision 的 Builder/Tester manifest 从空集合开始，公开前置文件
  只有当前 Builder 产出或未漂移 carryover 才能 checkpoint、publication 和进入 Tester（#186、#191）。

- Native Driver 将保留独立状态码 503、`auth_unavailable` 与 `no auth available` 的等价错误表达归一为
  同 dispatch 可恢复失败，不再绑定具体 HTTP/status 文案；以 ledger 持久化的 30 秒、60 秒指数退避
  继续同一 role/thread/action；CLI 在等待期间输出剩余时间，跨进程恢复从 Core 当前 machine/proof
  failure、recomposition 与 open problem 等唯一事实重建完整 action，
  prompt 或 identity 摘要漂移继续 fail closed（#181、#188）。

- Assurance v4 的 Tester problem 可由当前 producer 明确声明 `producer_continuity=invalid`。普通测试
  修正仍回原 thread；身份失效则通过持久化 replacement intent 建立新 Tester source，首个 author turn
  才解决绑定问题。首 turn 前 no-rollout 可在同一事务内续换 bootstrap identity，第三次停止到架构审查，
  candidate/target/source/问题 snapshot 或竞争事务漂移均零副作用拒绝（#187、#189）。

- Assurance v4 新增显式 `resolve-candidate-residue` 事务：请求绑定 current candidate、open problem
  snapshot 及完整 ignored regular-file path/SHA-256 manifest，Core 在首个 unlink 前持久化 intent，支持
  identical replay，并拒绝普通 untracked、symlink、额外/变化内容和并发事务。active run 只关闭对应
  `builder_loop` problem；已原子 supersede 且 disposition 为 `handled_elsewhere` 的 source 也可仅为 terminal
  cleanup 执行，不恢复角色、候选或 evidence。Native App Server 子进程同时强制使用 worktree 外的私有
  `PYTHONPYCACHEPREFIX` 并在退出后删除 cache tree，使显式 `py_compile` 保持 Python 语义而不污染角色
  worktree（#185）。

- Successor start 现在把 source path/blob 只保存在 immutable carryover，本 revision 的
  `builder_files/tester_files` 从空集合开始；Authority 新增的 public prerequisite 可等待新 Builder 实际
  创建和 checkpoint 后再发布，start 不再把历史 Builder manifest 投影成本 revision 的角色产出。

- Assurance v4 `start` 允许 revision contract 用既有 `mission.supersedes.candidate_head` 精确绑定一个被
  runtime-preparation Builder checkpoint 拒绝的 clean candidate。Core 只消费 source ledger 中唯一 open
  problem、completed 未消费 dispatch artifact、冻结 runtime support/authority 与未漂移 Git/target 事实，
  target ledger 落盘后才封存 source；普通 checkpointed successor 路径保持不变，且不新增 recovery command
  或旁路 candidate 状态（#184）。

- Native Driver 将三次 transport retry 耗尽持久化为不可改写的 `exhausted` dispatch generation。用户
  显式提供 `native-driver resume --reason` 后，Core 仅在 runtime identity、role/thread、action 与 prompt
  digest 均未漂移时建立同线程的新 generation；旧 attempt/turn/failure 保留，runtime 升级必须改走
  successor run，不允许在 active run 内热切换执行版本。

- Assurance v4 新增由冻结 runtime HEAD 绑定的 self-hosted support manifest。`validate --repo`、`start`
  和 Builder checkpoint 会在普通 run 修改自身 proof writer 前 fail closed，并要求 exact protected
  preparation 排除被修改 gate、保留独立 machine/blackbox/Reviewer；candidate 无法通过改写 manifest
  热切换 writer 或回填旧 observation。后续 business run 继续单次消费 finalized continuation，并在新
  runtime 上重建角色与 evidence（#180）。

- Legacy 与 Assurance v4 `prove-tests` 现在先完成全部 candidate group readiness，再决定是否执行
  baseline-red/mutation；多组失败保留首个 group/result 兼容字段并附有序 `failures`，单组与成功反例
  语义不变。Tester/Reviewer 角色契约同步要求 bound-call-site patch、公开异常语义、独立 proof 模块
  自包含和完整聚合归因；Native Driver 恢复 completed proof dispatch 时直接消费精确匹配的已持久化
  failure，不重入 proof gate 或重复记录失败。恢复中的 proof diagnosis 从 current failure 续接原 spec，
  初始 proof 的合法 problem 分支无需伪造 spec 即可进入 ownership 路由；DriverPort 从唯一持久化 spec
  与严格绑定的执行事实派生 schema-compatible group evidence。缺失 residue、group 身份漂移或 UV project
  binding 冲突会使 proof 失效重跑；reviewed-boundaries 仅在 current machine evidence 存在后公开。

- Native Driver 将结构化 turn failure 与 raw App Server stdio disconnect 归一为唯一 transport
  classification，只对精确匹配当前 role、thread、action 和 dispatch 的事故执行同事务有界重试，避免
  同一次断流形成相互冲突的 terminal failure。Driver action preparation 同时收敛到 Core-side 单一
  capability 契约；blackbox-only contract 只惰性登记 Tester identity，保持 Tester source/worktree 与
  author/tester evidence gate 缺省，Tester 和 Reviewer 均以冻结的 assurance required gates 为准
  （#178、#179）。

- Planner 现在在冻结 Authority 前从仓库已有版本化 manifest 的直接 path/blob/digest 引用检查角色契约
  依赖闭包，避免 instruction source 已授权但 behavior-lab manifest 遗漏；原生选择卡同时先描述用户可观察
  的行为、成本与退路，内部 revision/agent 术语只作为白话后的补充（#132、#148）。

- Legacy parity corpus 现在从冻结 source commit/tree 机械展开为 51 个 fixture、252 个 case 和 824 个
  assertion call，每个 case 显式标记 covered、rescue 或 retired，并绑定到真实存在的 unittest method。
  Assurance v4 同时恢复 Git-object 文档引用扫描：定义迁移、删除或重命名后残留的 qualified Markdown
  pointer 会在进入 Tester/Reviewer 前 fail closed，symbol-only 提及交给 Reviewer 做语义判断；旧 v4
  ledger 缺少 scan contract version 时保持兼容（#82）。

- Assurance v4 的 blackbox acceptance case 现在逐条冻结真实观察面、实际 execution ids、必需判据维度
  和可选外部目标；Core 在 stage 与最终 evidence 使用同一校验，拒绝用 API、fixture、内部状态或其他
  表面的代理证据满足声明。旧 active v4 ledger 保留 command-only 兼容，deployment restore、同 run
  environment lease 与 cross-run transfer 仍重新取得当前 mission 的 case evidence（#76、#151、#154）。

- Assurance v4 将 target drift 与已发布 prerequisite 修复统一为 crash-safe candidate recomposition：
  publication 采用冻结路径、不可变 generation，Builder/Tester 冲突回原 thread 的 ownership staging，
  target 再推进可重启并以最终 CAS 收敛；status 提前列出同 target contenders。新增 focused machine
  preflight、同 Reviewer thread 的非最终 preflight、持久化 machine failure 归因，以及从 ledger event
  派生的时间分类、重组统计、warning 和前三大耗时。plan problem 使用结构化 `decision_request`，
  Planner 只向用户展示 delta，但 `validate-decision` 仍绑定同 session/action/facet digest 和唯一完整
  replacement contract；Builder 原子更新原 run 后 resume，不再为可表达变化重建整轮计划
  （#110、#111、#130、#137、#139、#143、#145、#153、#155）。

- Assurance v4 将 run 创建后的 Native Driver FATAL 持久化为可恢复 `driver_failure`，在 finalize intent
  或 deployment/lease 安全收敛后进入 `finalized` 或新 `failed` 终态；failed 保留 candidate observation，
  禁止 resume/supersede/abandon，并仅允许未漂移 clean cleanup。problem owner 改为六类穷举路由，
  `builder_loop` 与 `current_project` 不再默认派回 Builder。terminal retrospective 保留完整审计块，
  Stop hook 改校验精简 `required_user_block`，只向用户展示计数、digest 与真正待决项（#167、#170、#171）。

- Assurance v4 修正 contract-change lifecycle：授权决定优先由 `update-facet` 或 `revise-mission` 在同一
  active run 收敛；确需 successor 时，source 保持 active 直到 `start` 持久化 target，再原子封为
  superseded，并连续携带 candidate、lineage 与逐条 problem disposition，重新绑定角色和 evidence。
  `abandon` 只表示没有 successor 的用户取消，terminal source 在任何 target mutation 前拒绝且不恢复；
  legacy v2/v3 的 abandoned-source revision 行为保持不变（#169）。

- Assurance v4 现在把可信 `prove-tests` 失败持久化为 candidate、Tester source、spec、结构化错误与
  artifact 绑定的 `proof_failure`，不再让 Native Driver 因 `TEST_PROOF_CANDIDATE_FAILED` 或
  `TEST_PROOF_COUNTEREXAMPLE_INVALID` 直接退出并无限重放 completed dispatch。可在冻结 run 内修正的
  失败续接原 Tester thread 做只读归因，再按 builder/tester/plan problem 路由；环境、完整性与未知失败
  明确停到 `NEEDS_USER`。成功与失败的 exact replay 均不重复执行 proof，失败不生成 PASS evidence，
  telemetry 与三次同类失败门禁继续从 ledger event 派生（#166）。

- Assurance v4 新增完整 Revision continuity：计划可显式保留唯一 owner 的 environment lease，同 run
  Mission Revision 绑定上一 mission digest 并继续使用未漂移环境；跨 run `supersedes` 携带精确 candidate
  snapshot，按 source intent、target receipt、source seal 转移 lease。制品变化时先恢复旧环境再部署，
  Tester/Reviewer identity 与全部 evidence 始终重建；finalize、abandon 前强制恢复（#154）。

- Assurance v4 命令现在把实际 returncode 与计划冻结的 `expected_returncodes` 分开；本地关键测试可在
  同一 machine gate 内先于昂贵全量命令执行并失败即停。新增单 run 候选部署事务：从 candidate HEAD
  创建隔离 deployment worktree，绑定项目制品 SHA256、授权环境和部署前后 probe，Tester 结果先暂存，
  环境恢复成功后才登记 blackbox evidence；Native transport 中断或重试耗尽同样先进入恢复。目标
  checkout 不为部署切换。当前 probe 若确认目标已承载同一制品，则跳过重复 deploy，重新执行当前
  Revision 的 blackbox，并在环境无漂移后释放事务（#146、#151、#152、#154）。

- 新增 Native Driver v1，使用本地 Codex App Server 的版本化 stdio thread/turn 接口承载 Full Driver
  v4。Builder、Tester、Reviewer identity 和单一 crash-safe dispatch intent 由 Assurance Core ledger
  绑定，Agent 结果经统一 schema、turn id 和 artifact digest 后才能进入现有 evidence/problem 事务；
  可恢复的 transport failure 在同一 thread/dispatch 上最多自动尝试三次，耗尽后 fail closed；每轮仍由
  `driver-next` 重新派生动作。`$builder` 保持原入口并默认启动 Native Driver，只有 run 创建
  前 capability preflight 不兼容时才回退现有 Full Driver Skill；旧 run 不自动迁移，native-owned run
  禁止中途换控制器。Tester integration replay 现在幂等，author evidence 绑定集成后的 candidate，proof
  使用可回溯 Tester-owned source 的 canonical test id。安装器新增 Builder role 配置，不增加用户命令
  或 API Key。

- 将 Plan 环节的 Builder-loop 路线以实验功能重新开放：托管 AGENTS 使用一次原生选项卡选择路线，
  Planner 直接生成并验证 Assurance v4 contract，紧邻的原生 `Implement the plan.` 自动加载 `$builder`；
  Builder 只校验授权并桥接 Full Driver v4，不恢复 legacy v2/v3 `start` 或第二套角色循环。内部 Full
  Driver Skill 继续禁止普通请求隐式调用，公共支持状态仍等待跨项目真实观察。
- 新增隔离的 Assurance Core v4 实验 namespace、四事实面 schema/ledger、局部 evidence invalidation、
  dirty intake、并行/串行 Tester publication、proof、结构化问题路由、独立来源绑定、隔离机器验证、
  target rematerialization、protected preparation/单次 continuation、持久化 finalize intent 和目标 CAS；
  Full Driver Skill 使用原生 custom-agent spawn/same-thread follow-up，按 Core readiness 持续派生下一动作，
  mutation 以派生 `action_id` 拒绝过时 dispatch，dirty snapshot 与 Builder checkpoint 分开记账，且不把
  角色循环写入 ledger。Driver enforcement 启用后，通用 Execution facet 更新被锁定，candidate、
  checkpoint 与角色来源只允许通过对应专用事务写入。安装器部署禁止隐式调用的显式实验 Skill，公共 `$builder` 与 Plan 高保证入口
  仍保持关闭。离线回放 corpus 冻结 Issue #160 的 26-run 样本、#158 R1–R8 和产品变化/
  Git 冲突控制场景；公共 Builder-loop 继续处于维护关闭状态。
- 进入 Full Driver v4 重建维护期：安装配置移除了 Builder lifecycle hooks 和 Plan 路线选择，
  Planner/Builder Skill 关闭隐式加载并在显式进入时 fail closed；legacy `start` 默认在仓库锁、计划
  读取和任何交付副作用前返回 `BUILDER_MAINTENANCE_DISABLED`。v2/v3 ledger 及其诊断、安全恢复、
  finalize/cleanup 能力被保留，既有确定性 fixture 通过仓库内部显式环境开关继续回归。
- 新增受保护 verification support 的关联双 Run 续接契约：只读 `plan-preflight` 在详细规划前
  区分当前 machine runner/control 冲突与 abandoned business run 的旧 support-only 冲突；后者由
  独立 preparation run 交付，再通过 ledger 与 Git 派生的 `BUILDER_CONTINUATION_READY` 交回同
  session Planner。后续 business revision 使用 `continuation-from` 续接原 supersession/problem
  链，并对 repository、target、session、finalized HEAD、问题映射与单次消费 fail closed（#158）。
- 新增 schema v2 blackbox report，逐条保存 command/method-error execution、逐例结果和维度
  observations；新 run 冻结 v2，legacy active run 保留单 command v1。`prepare-follow-up` 现在从
  frozen cases 派生适用性，evidence digest/scope 覆盖完整 report 和全部 accepted commands。v2 现在
  无条件要求至少一条 accepted execution，并精确合并可解析 command scope；Schema/runtime 同步拒绝
  report 的纯空白关键字段。unittest dependency resolver 仅窄化显式 `.py` 与 discover directory，
  并将默认 discovery 或歧义 module/package target fail closed 到全 tree，停止猜测同名 `.py`。
- 计划 identity 升级为 canonical-v2，并分别审计 raw source/frozen digest；legacy ledger 保持
  raw-v1。稳定 CLI 在 runtime import 前禁写 bytecode，Tester role-check 改用 Python AST/token
  区分真实 skip/xfail/rerun、module-level `pytestmark` 与 comments/string fixtures，并解析
  pytest/unittest/subprocess alias 与局部 shadowing。Reviewer brief 不再重定义 custom-agent 的
  pass/findings/blocked 终态
  （#98、#105、#106、#117、#124、#132、#134）。
- Tester 与 Reviewer initial spawn 现在显式使用 `fork_turns="none"` 和最小冻结 brief，后续阶段仍续接
  原 thread；文档同步区分 conversation、Git/artifact、filesystem 与平台 attestation 边界。Reviewer
  因可补齐前置缺口 blocked 时只允许原 thread 复审，不再建议 fresh Reviewer（#88、#125）。
- 将 Builder-loop Planner 验证成功后的 Codex 原生“实施计划”动作视为一次明确授权：方案外的
  `BUILDER_HANDOFF_READY` 只对同 session 紧邻 Default turn 生效，Builder Skill 允许严格受限的
  隐式发现；普通实施请求、Codex 原生 Plan、过期或缺失标记仍 fail closed，`$builder` 保留为手工
  回退。交接不新增 Hook、runtime 状态、计划摘要或 ledger 字段。
- 冻结测试证明的可信输入边界：独立 Tester 源码经 thread、ownership、Git/source manifest、
  integration 和 Reviewer 审查后视为可信；runtime 继续防 hostile 环境、runner、输出伪造、空壳
  证明和基础设施错误，但不把同解释器 reporter 或外部 supervisor 描述成任意恶意 Python 的
  操作系统级安全沙箱。
- 将新计划契约升级为 schema v3：逐行为冻结测试鉴别最低强度，并把端到端用例统一为
  `schema_version=1 + cases`。新 start 拒绝 v2 计划；既有 v2 ledger 仍按原门禁续接。
- 新增版本化 `schema/codex-test-proof.schema.json` 与 `prove-tests` 隔离门禁；schema 提供
  unittest 基线先红、pytest 受控变异正例及成功 evidence 定义，门禁以基线先红、受控变异或
  Reviewer 审核的边界映射证明测试能鉴别冻结行为。证据绑定 candidate、Tester source HEAD、
  Tester manifest、命令、变异和日志。证明
  命令改为非内联测试运行器或冻结的受保护仓库脚本白名单，未知解释器与命令分发器默认拒绝；实际
  executable 固定为受信任绝对路径并记录身份，proof 子进程使用可信系统 PATH，避免冻结 wrapper
  内部二次劫持。反例只有完整测试 id 精确映射到真实断言失败时才成立；schema、parser 与 evidence
  对 runtime 合法 run id 和 Draft 2020-12 integer 语义保持一致。allowlisted runner 还会把声明 id
  绑定到 Tester source blob，拒绝仓库外测试目标；candidate 必须逐项执行并通过声明测试，pytest
  与 unittest 的逐项状态和真实异常类型改由独立监督进程采集，测试进程不持有最终结果写入权；原始
  stdout/stderr 只作日志，不能由 skip/xfail、无关通过测试、captured stdout 或结束后追加文本冒充
  证明。
- 结构化端到端证据改为逐例校验机械、功能、质量和汇总结果，并在 Tester integration、机器验证、
  测试鉴别证明全部绑定候选后才允许 blackbox 与 Reviewer。
- Builder 增加按需根因修复参考和每个实现单元的廉价分段自检；新增离线角色行为实验场，只准备和
  评分版本化场景，不调用模型、不联网、不写交付 ledger 或提交实验结果。
- 将旧版 Builder 末尾的五问打分与直接 memory 协议替换为交付后分层复盘：业务项目、builder-loop
  和外部平台事故按单一 owner 落盘，跨边界因果链强制拆分；问题记录只含客观现场并经用户授权。
  工程问题处理后，剩余隐含知识才以 `builder-loop delegated` 模式交给 `$memory-review`。
- 新 run 在 ledger 中冻结 `runtime_identity`，记录 Codex/Claude Code adapter、实际 commit、dirty
  状态和捕获状态；旧 ledger 明确标为 `legacy-unavailable`，事故版本不再依赖事后猜测。
- Assurance v4 强化首次跨项目真实 run 暴露的边界：Tester integration 拒绝嵌套 runner 与 proof channel
  移除；proof 支持绑定绝对 uv、冻结 project/lock 文件和唯一 pytest/unittest 事件；Builder dispatch
  先 checkpoint 后消费，problem replay 按 producer/key/candidate 幂等；旧 failed evidence 在依赖变化后
  自动 stale；v4 ledger 冻结 runtime identity；全部成功与失败终态统一进入事故复盘；用户批准的 plan
  problem 通过 `update-facet --resolve-plan-problem-key` 与 facet 更新原子收敛。
- 将未授权 dirty 的语义从“全局停止”修正为“默认隔离”：新增 exact-path `workspace-intake` 与
  `workspace-scan`，以不可变 Git snapshot 把用户授权输入交给 Builder，target 与全局 stash 在
  start/abandon 期间保持不变；finalize 只消费未漂移的 captured state。
- ledger 升级为 v2：evidence 记录 observed/accepted HEAD、真实输入 digest、scope 和 provenance；
  machine/blackbox 可在显式 exempt-only 变化后复用，Reviewer/doc-review 继续精确绑定 integrated
  HEAD。现有 v1 run 在首次受锁写入时原子迁移并保留全部身份与 Git 事务事实。
- 新增 deterministic progress stop、`doctor`、`recover`、`resume` 和 safe `cleanup`。同一
  candidate 双失败或同类失败跨三个 candidate 重现时交还用户；未知 orphan 永不自动接管或删除。
- 建立旧 CC 51 个 E2E 行为的结构化 parity corpus，冻结 28 个等价覆盖、8 个抢救/优化替代和
  15 个明确退役项，防止再次用 clean-sheet fixtures 代替迁移守恒证据。
- 将 delivery gate readiness、final commit staging 与目标 checkout 同步 readiness 拆开；dirty target
  现在会先冻结唯一 final intent，再以结构化 blocker 等待重试。tracked/index 改动继续 fail closed，
  无关 untracked/ignored 文件不再阻塞或被清理，路径碰撞和 Git dry-run 不确定性仍在 CAS 前停止。
- 将全局 Plan mode 入口由强制加载 Planner 改为每次通过 `request_user_input` 选择 Codex 原生
  Plan 或 Builder-loop Planner；只有后者生成带一次性交接标记、可由原生实施动作或 `$builder`
  执行的冻结契约。
- 将 `unit-test-spec` 与 `documentation-spec` 升级为 schema v2，并明确拒绝旧 v1 计划；非 L1
  计划在 `spec_head` 存在 `.claude/loop.yml` 时必须省略 `test_context.runner`，不存在时才由计划
  提供 runner，消除验证命令双来源。
- 为 `plan-validate` 增加仓库上下文和 `effective_verification_source`，与 `start` 共享只读
  preflight，在创建 run/worktree 前统一验证目标 HEAD、supersession、runner 安全、ownership
  与冻结依赖。
- 修复同一 Tester/Reviewer thread 的 follow-up 只有 `SubagentStop`、没有第二次
  `SubagentStart` 时 ledger 保留首次 turn 结果的问题。新增 `prepare-follow-up` dispatch 契约，
  在发送 follow-up 前失效旧 evidence、记录 pending turn，并由实际 terminal event 绑定新
  `turn_id`、结果、HEAD 与 Reviewer prerequisite snapshot。

## Codex-native 0.1.0 — 2026-07-19

- 从 `main` 建立长期独立的 `codex-native` 分支；Claude Code 版本继续由 `main` 维护。
- 将用户入口固定为 `/plan <需求>` 后显式调用 `$builder`，安装器为 Plan mode 注入
  Planner Skill 选择规则，不改变普通 Codex 改码行为。
- 以根线程作为 Builder；`parallel_ready=true` 时 Tester 与 Builder 从同一 `spec_head` 并行，
  串行计划则将精确公开文件发布为隔离 HEAD/manifest 后再启动 Tester，并冻结发布路径。
- 将测试目标、ownership、验收标准和迭代上限的修订统一为新 run：旧 run 先 abandon 保留
  现场，再经 `/plan` 提升 `plan_revision` 并重新调用 `$builder`。
- 新增确定性 runtime CLI、ledger、ownership gate、机器验证、agent lifecycle 记录、证据
  HEAD 失效、冲突安全停止和单次 squash commit。
- 对外只保留 L1 纯 Markdown 特例；其他任务统一进入 `L2/L3` 非 L1 交付路径，不再维护两套
  L2/L3 流程。
- Builder 继续负责文档维护，Reviewer 在 Phase D 按文档政策审计；未拆分独立 Doc Reviewer。
- 新增 Codex Skills、Tester/Reviewer custom agents、官方 lifecycle hooks 与隔离安装/卸载流程。
- 随安装部署默认文档政策；项目可提供更具体政策覆盖，避免 Reviewer Phase D 依赖作者机器路径。
- 要求所有非 L1 run 取得 Tester author `tests_ready` 和同 thread blackbox `pass` 证据。
- 将 Tester author turn、空 tree integration 和已完成 turn 防重放写入 ledger；Tester/Reviewer 新
  turn 会使本角色旧 evidence 失效。
- 将 blackbox evidence 绑定 candidate worktree、实际命令、returncode 和执行前后 HEAD，并按
  ownership 将测试修复路由回原 Tester。
- 为 L1 增加冻结 planning-time HEAD、revision 与精确文档写边界的 `documentation-spec`；修订
  计划通过旧 run id/plan digest 验证 abandoned supersession。
- 拒绝反向/恒真 runner，并保护 Makefile、pytest/ruff 配置和 package manifest 等可识别验证
  控制文件；repository wrapper 还要求在 `spec_head` 已存在且为仓库内普通文件，并拒绝 PATH
  override、symlink 与仓库外 target。
- 将 `max_iterations` 落入 ledger 状态机；机器验证改在 candidate 的临时干净 worktree 执行，
  每次 attempt 保留独立日志；最终提交改在临时 ref/worktree 运行目标仓库 hooks、验 tree 后再以
  expected-old compare-and-swap 更新目标分支，并以冻结 run mutation、重验 gate 的持久化
  finalize intent 恢复 CAS 前后的进程中断。
- 将 Reviewer pass 绑定到 turn 开始与完成时的 Tester integration、机器验证和 blackbox snapshot，
  防止先审查后补证据；L1 仍只绑定干净候选与冻结文档路径。
- 让 lifecycle Hook 能从 target、Builder、Tester worktree 定位同一 ledger，并为并发 start 增加
  repository 级锁；损坏 ledger 不再被解释为“无 active run”。
- 将安装器配置更新改为跨 `hooks.json`/`AGENTS.md` 的事务，保留同 entry 的外部 handler，且不再
  覆盖固定 `.bak` 文件。
- 移除 Tester/Reviewer custom agent 的固定模型配置，使其继承根线程模型和推理强度。
- 明确安装产物为指向 checkout 的符号链接，并补充自定义 `CODEX_HOME` 路径和移动约束。
- 将全局 AGENTS 托管块缩为 `/plan` 与显式 `$builder` 两条触发规则；非空
  `AGENTS.override.md` 会遮蔽该块，因此安装时 fail closed。
- 删除本分支中旧的 Claude Code commands、Markdown agents、Stop-hook 编排脚本和旧 fixture；
  同时移除旧 `.claude` plan、review report、trace、settings 与本地状态快照；这些实现及其完整历史
  仍保留在 `main` 和 Git 历史中。
- 建立不依赖真实模型的契约测试，覆盖计划、并行 ownership、Tester dirty state、agent
  continuity、evidence HEAD、Stop gate、schema、安装器、Git 冲突与 finalize 清理。
