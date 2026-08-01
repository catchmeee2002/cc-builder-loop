# Codex Builder Loop 架构

## v4 分层边界

legacy v2/v3 新 run 与无实验标志的 v4 CLI 保持关闭；面向用户的 Plan/Builder 实验入口已经开放。
新实现位于独立 `assurance_v4` package，不导入或原地改写 legacy `core.py`：

```text
          Full Driver v4 behavior contract
                         │
              Native Driver (default)
                  │              │
         Codex App Server    Full Driver Skill fallback
                  └──── driver advice ────┐
                                         ▼
                                Assurance Core v4
 Mission ─ Authority ─ Assurance Policy ─ Execution Manifest
                         │
 candidate/evidence binding ─ machine ─ role source ─ target CAS
```

四个事实面独立计算 canonical digest。Mission 只包含目标、行为、接口、验收场景和信任边界；
Authority 只定义 target、角色写范围和 dirty intake；Assurance Policy 定义所需判据及机器命令；
Execution Manifest 记录实际 candidate、文件、命令和 Agent identity。Execution 在授权内变化只使依赖
它的 evidence stale；只有 Mission 变化才提升 semantic revision，Authority 扩大和 Assurance 降级
分别要求用户授权。

Core ledger 保存 `active/finalizing/finalized/abandoned`、facet/evidence digest、candidate、target 与
可恢复事务，不保存“下一步让谁做”、correction budget 或普通 Agent 循环。Native Driver 在启动外部
role turn 前只额外持久化一个 dispatch intent，绑定 action/thread/prompt/output digest 和 turn/result；
它是跨进程副作用的恢复事实，不是调度状态，消费后立即清空。`driver.py` 每轮从 readiness 重新决定
Builder、Tester、机器、候选部署、黑盒或 Reviewer 动作；
同一失败签名三次只触发架构复核，不自动创建新 Mission。无冲突 target drift 通过 candidate
rematerialization 后局部重验，Git 冲突、授权扩大和覆盖风险才停止。

Full Driver contract 固定 `execution.driver_enforced=true`。该模式下 mutation 即使省略 `action_id` 也
必须即时匹配当前派生 action；携带 ID 时还要匹配同一 ledger 版本，拒绝错序调用和 stale replay。
启用 Driver enforcement 后，通用 Execution facet 更新只允许 no-op；candidate、Builder checkpoint、
Tester source 和角色 identity 必须分别通过专用事务写入，不能用通用更新入口伪造执行进度。
Assurance Core 的独立契约测试与手工底层诊断默认不启用 Driver enforcement，二者不共享隐式开关。

dirty intake 的自动 snapshot commit 只证明输入已隔离，不证明实现完成；ledger 单独记录
`builder_checkpointed` 执行事实，Driver 在首次 Builder checkpoint 前不会推进 Tester。串行 Tester 的
publication 在 target rematerialization 时基于新 target 重新生成，exact prerequisite blob/manifest
保持不变，Tester source 改以新 publication HEAD 为 base，避免恢复后退回旧 target 基线。

proof evidence 不能由 Agent 直接提交摘要。Tester 只提供符合公共 `codex-test-proof` schema 的 spec；
Core 在隔离 worktree 先证明 candidate tests 通过，再执行 baseline-red 或仅触及 Builder-owned 普通
实现文件的 mutation，并要求冻结测试产生 assertion failure。只有每个 Mission behavior 恰好覆盖一次
才记录 proof；ledger 同时保存规范化 spec、逐组执行结果、executable identity、log digest 与 artifact
位置，任意 report digest 不能替代真实执行。

Native Driver 使用 App Server 稳定 stdio thread/turn 接口并在启动前检查当前协议 schema；不启用
`experimentalApi` 字段，也不新增 API Key。Builder、Tester、Reviewer 的唯一 role policy 仍来自
`agents/*.toml`，App Server `outputSchema` 把终态收敛为公共 v4 JSON。Core 机械核对 thread identity、
Tester source HEAD/blob manifest、blackbox worktree/HEAD/逐命令结果和 Reviewer candidate。Native
Driver 能直接观察并恢复真实 thread/turn 生命周期，但不把协调器观察宣称为密码学或平台级防伪。
Tester author evidence 在 source 集成后由 Driver 绑定 Core 产生的 integrated candidate HEAD，同时保留
source HEAD/manifest provenance；同一 source/candidate 的 `integrate-tester` replay 是幂等 no-op。
proof 使用与 Tester-owned 路径一致的 canonical test id，Native wire 只在唯一可证明映射时补全模块或
文件前缀，最终 source binding 和真实执行分类仍由 Core 判定。

机器命令的 `run_before_full_suite` 只改变同一 machine gate 内的执行顺序：本地交付关键测试先跑，
失败后不启动后续昂贵命令。所有命令默认只接受 returncode 0；计划可用 `expected_returncodes` 冻结
其他合法结果，runtime 始终把实际 returncode 当作观察值，而不是让 Tester 在 evidence 中重定义成功。

可选 deployment contract 只支持当前 run 的单个已授权外部目标。Core 从 candidate HEAD 创建独立
deployment worktree，在其中运行项目提供的 probe/build/deploy/restore wrapper，绑定制品 SHA256、
部署前状态、部署后状态和恢复状态；target checkout 不切换。Tester blackbox 结果先暂存，Driver 无论
结果或 transport failure 都优先恢复，只有 probe 证明回到 baseline 后才登记 evidence。恢复失败保留
worktree、ledger 和外部状态事实并返回用户决定。跨 revision 环境复用和多仓部署仍不支持。
可恢复的 App Server transport failure 在同一 role thread 和 dispatch 上最多尝试三次，attempt 与 turn id
持久化进 ledger；第三次失败后 Core 返回 `NEEDS_USER`，Driver 不重置上限或另建 thread 绕过连续性。
App Server turn 使用 host-level `danger-full-access`，因为部分本地环境无法创建 bwrap namespace；这不
扩大产品信任声明，角色只读/写范围仍由独立 worktree、Git manifest、ownership 和 Core mutation gate
机械执行，与既有“不提供 filesystem ACL”的边界一致。

现有 Full Driver Skill 保留为协议不兼容时的 run 前回退和 parity oracle。ledger 一旦冻结
`driver_runtime.kind=native`，Skill 不得接管；旧 ledger 缺少该字段时继续由原承载恢复，Native Driver
不自动 adopt。

内部确定性入口为 `codex-builder-loop assurance --experimental-v4 ...`，Native 编排入口为
`codex-builder-loop native-driver start|resume|status`，但用户不直接调用。Plan 选项中的实验 Planner
冻结并验证 v4 contract，`$builder` 只消费已验证 handoff 并启动 Native Driver；内部 Full Driver
Skill 仍禁止被普通请求隐式调用。首版覆盖本地单仓 Git 代码、L1 文档交付，以及由项目 wrapper
定义的单 run 候选部署事务；跨 revision 环境复用、通用远程部署编排和多仓事务保持 unsupported。本地 protected preparation
通过 finalized HEAD、exact support manifest 和单次 continuation 消费衔接 business run。消费前先在
preparation ledger 持久化 intent，business ledger 落盘后再提交 consumed marker；进程中断时重试
同一 start 只收敛该 intent，不创建第二份状态。当前用户入口明确标为实验；只有完成历史回放和跨项目
真实验收后才移除实验标识并视为受支持的公共 Full Driver。

## Legacy v3 系统边界

Codex 原生 Plan mode 负责探索和追问；全局托管规则先让用户选择继续使用原生 Plan，或加载
Planner Skill 固定 builder-loop 方案契约。根线程作为 Builder，Codex subagent threads 承担
Tester 与 Reviewer。runtime CLI 只处理可确定验证的内容，不替模型做产品或架构判断。

```text
/plan ── request_user_input
   ├─ Codex 原生 Plan → proposed_plan
   └─ Builder-loop Planner
             │ validated plan + BUILDER_HANDOFF_READY
             ▼
 原生“实施计划” / $builder ─ optional exact dirty snapshot ─ Builder worktree
    ├─ parallel_ready=true  ── Tester thread 与 Builder 并行
    └─ parallel_ready=false ─ exact public files → isolated publication HEAD/manifest
                                                   └→ Tester author baseline
        │ Tester author tests_ready → integrate tester commit
        ▼
全 strong v3: test-effectiveness proof → clean candidate verify ─┐
其他 v3/v2: clean candidate verify → proof（仅适用 v3）──────────┴→ same-thread black-box → Reviewer
        │ all evidence points to candidate HEAD
        ▼
temporary final ref/worktree → hooks → tree check → target CAS → cleanup
```

就绪标记只在带仓库上下文的 plan validator 返回 `READY` 后出现，并位于冻结方案之外。它只授权同一
session 紧邻下一轮、已切回 Default mode 的 Codex 原生“实施计划”动作；消息插入、方案修订、session
变化或 Codex 原生 Plan 都使该路径失效。Builder Skill 因此允许隐式发现，但会在计划物化和任何
runtime 调用前核对完整条件；`$builder` 保留为不依赖标记的手工入口。该交接不写 Hook、计划摘要或
ledger，run 启动后仍只以 ledger 为执行事实源。

## Workspace intake

Target dirty 默认不进入 run，也不要求全局清理。Planner 只有在任务明确依赖某个 dirty 文件时才取得
exact-path 授权，并用只读 `workspace-scan` 冻结 index/worktree state digest。`start` 从
`spec_head` 和这些普通文件合成不可变 snapshot commit，Builder 从 snapshot 起步；target checkout
的字节与 index 状态不变，因此 abandon 不需要 stash 恢复，也不污染全局 stash。

Finalize 把 snapshot state 与最终 tree 一起纳入持久化 intent。只有授权路径仍处于
captured/final 可证明状态时，runtime 才允许覆盖；任何第三种内容都是 `TARGET_INTAKE_DRIFT`。
CAS 或 checkout 中断后，snapshot、final commit 和 intent 足以识别 expected、captured、partial
和 final 状态并幂等完成。无关 untracked/ignored residue 继续保留。

## 计划和并行门槛

新计划契约只接受 `schema_version: 3`。计划保留 `plan-checklist`，并在非 L1 使用
`unit-test-spec`、在 L1 使用 `documentation-spec`；运行时验收可再增加 `e2e-cases`。非 L1
spec 必须包含规划时 HEAD、计划版本、接口、测试上下文、角色写路径、行为边界/不变量、逐行为测试
鉴别最低强度和 mock 策略。串行计划还必须声明
`public_prerequisites`。其中每项必须是 Builder-owned 的精确普通文件路径，不能是 glob、目录或
symlink。具体字段以 validator 实现和 fixtures 为准。

当新 structured CLI/API 在 planning HEAD 尚无可供独立 Tester 读取的 wire contract 时，计划把
最终 machine-readable contract 同时列为 `interfaces` 文件输入和 `public_prerequisites`。runtime
通过 `interface_input_paths` 将其 blob 绑定到隔离 publication；Tester 只从该契约取得 exit、
status/code、字段层级与场景前提，发布后 Builder 不再修改它，也不以兼容投影迎合错误断言。

非 L1 的 effective verification source 只能有一个。`spec_head` 存在 `.claude/loop.yml` 时，
该文件是唯一来源且计划必须省略 `test_context.runner`；不存在时，计划必须声明 runner。
`plan-validate --repo <repo>` 与 `start` 调用同一只读 preflight，统一核对目标分支、supersession、
effective runner、安全规则、ownership 和冻结依赖。前者不创建 ledger、run 目录或 worktree；
只有完成这些上下文检查才返回 `READY`，并公开 `effective_verification_source`。

受保护验证支持采用关联双 Run，而不是让一个 run 修改自己的判据。详细规划前，独立只读
`plan-preflight` 将 exact intended paths 分成无冲突、旧 abandoned run 的 support-only 冲突和当前
machine runner/control 冲突；全过程不创建 ledger、worktree、ref 或文件。support-only 场景的
revision 1 preparation run 在自身 plan/ledger 冻结旧 business run、plan digest、problem snapshot、
problem ids 与 exact paths，并由普通独立验证完成；旧 ledger 保持只读。

preparation finalized 后，`status/finalize` 从 preparation ledger、原 business ledger 与 Git final
HEAD 派生单一 continuation object。后续 business revision 的 `continuation-from` 只引用 preparation
run，仍由正常 supersession 指向原 business run，且关联问题必须引用 preparation commit 为
`handled_elsewhere`。同 session 紧邻 marker 只负责把下一次 Plan mode 路由给 Planner；runtime 继续
核对 repo、target、session、finalized、spec HEAD 与 replay，并要求新的 plan validation 和实现授权。
这条链不持久化 mission、第二份计划、transcript 索引或 evidence 副本。

新计划 identity 为 `canonical-v2`：CRLF/CR 统一为 LF，唯一严格受管的首行生命周期 header 被移除，
末尾换行规范为一个；其余 Markdown 字节全部参与摘要。`plan-validate` 同时返回 canonical identity 与
raw source digest，start 在 ledger 另存 raw source/frozen-file digest。冻结文件随后同时核对 canonical
identity 和 frozen raw digest。既有 ledger 缺少这些字段时保持 `raw-v1`，不事后重算。

`parallel_ready=true` 只用于 Tester 无需等待 Builder 产物即可依据冻结目标和公开契约写测的
计划。为 `false` 时，计划必须明确可独立冻结的最终公开契约文件，例如 schema、header 或接口
定义；后续实现必须落在其他 Builder-owned 文件，不能在同一 run 继续修改已发布文件。runtime
先自动 checkpoint Builder，再验证相对 `spec_head` 的最终 tree 只改变声明文件，从该 tree 合成
一个 parent 为 `spec_head` 的隔离 publication commit，并将 HEAD、tree、每个 blob 与 manifest
digest 写入 ledger。Tester 以 publication HEAD 为 author baseline，不接收 Builder branch HEAD、
candidate diff 或其他实现内容。中间 Builder 历史不会进入 Tester baseline；当前版本仍不宣称 Git
object database 具有操作系统级读 ACL。

validator 会从 `interfaces` 文本中提取与 Builder ownership 重叠的仓库路径。路径意味着 Tester
author 依赖文件内容：并行计划必须改写为不含实现路径的真实黑盒入口，或转为串行计划并逐项发布
全部 exact files；glob 和遗漏 publication 均 fail closed。新 run 在 ledger 冻结该规则版本和
规范化路径，既有 active run 保留旧语义，避免升级时追溯改变已经启动的事务。

publication 改变 initial Tester author baseline 后，runtime 先持久化 ledger 中的 publication 与
`tester_integration.base_head`，再把同一干净 Tester HEAD 同步到 session locator 的 start
attestation，确认成功后才返回 `READY`。若响应或 locator 写入中断，`publish-prerequisites` 的
幂等重试只在 Tester 仍停在该干净 publication baseline、且尚未绑定 Tester identity 时修复派生
locator；Tester 已前进而 start identity 尚不可证明时保持 fail closed。Hook 与 journal fold 不读取
后续 live Git 状态来倒填捕获时事实。

v3 的 `e2e-cases` 只有一个 `schema_version=1 + cases` 格式。完整 case 继续只存在冻结计划；ledger
只保存 case ids 和规范摘要。`prepare-follow-up --purpose blackbox` 每次从 frozen cases 派生 report
版本、schema 路径及 mechanical/verify/quality 的 `required|not_applicable`，不持久化第二份 case
缓存。fast 只要求机械规则；full 的 verify/quality 必须通过，mechanical 是否适用由 hard_rules
决定。

新 run 冻结 blackbox report v2，唯一结构定义是 `schema/codex-blackbox-report.schema.json`。报告保存
全部真实 executions；无论是否存在冻结 case 都至少需要一条 `method=command` 的 accepted execution，
其必须零退出、未超时；存在冻结 case 时还必须与逐例结果共同覆盖声明 case。其他 method 携带 reason，但不计入结论。
Schema 与 dependency-free runtime parser
共同拒绝只含空白的 worktree、command、reason 和 observation。逐例三个维度各使用
`{status, observation}`，runtime 校验适用性并机械派生 outcome；
禁止用合成 aggregate command 或旁路 `commands` 数组代替 provenance。既有 active ledger 缺少版本
字段时继续 v1 单 command 结构，保持 run、agent 和 evidence identity。

`plan_revision=1` 表示首次契约。更高 revision 必须携带被替代 run id 与旧 plan digest；start 只在
旧 run 已 abandoned、digest 匹配且 revision 增加时接受，避免把原地放宽伪装成新证据。abandon
同时把本轮逐条问题和继承问题封成唯一 snapshot；更高 revision 的 `prior-problems` 必须绑定该摘要，
并让每个 problem id 恰好选择 `include`、`handled_elsewhere` 或 `discard`。`include` 项进入新 ledger，
若再次 abandon 继续保留；旧 ledger 没有 snapshot 时先一次性补录，不能把缺少历史记录解释为空清单。

唯一例外是显式 `预估改动级别：L1` 的纯 Markdown 文档任务。它用
`documentation-spec` 冻结 planning-time HEAD、revision 和精确 `builder_write`，不包含
`unit-test-spec/e2e-cases`。runtime 还会机械拒绝非 `.md` 改动，并跳过 Tester、机器验证和
E2E gate；Reviewer 的方案审查与 Phase D 文档审计仍然必需。

## 方案取舍与演进边界

Planner 先按后果决定分析深度。局部可逆任务直接形成最小方案；影响广、难回退且存在真实分叉时，
才按需读取方案取舍参考并把选择冻结进计划；范式级选择必须交还用户。主 Skill 只保存触发条件，
比较维度和演进检查下沉到单一参考文件，避免所有任务承担常驻提示成本。

计划是方案选择和演进约束的唯一事实来源。Reviewer 依据冻结计划执行最小充分设计审查，只把新增
模块、公共接口、依赖和扩展点映射到已接受行为、明确演进压力和必要交付基础设施，不另建一份设计判断；
无法映射且产生具体成本或偏离时才形成 finding。真实模型
对这些规则的服从率属于角色行为试验，不写入 runtime ledger，也不冒充确定性交付证据。

Builder 对故障修复只常驻四个轻量问题：失败、机制、因果源头的最小改动和前后可区分判据。机制
不清或同类失败重现时才加载根因参考。每个实现单元完成后做局部 diff、ownership 和最小检查自检；
这两项都是角色纪律，不新增状态或 gate。

`experiments/agent-behavior/` 保存版本化场景、按角色匹配的指令来源和离线准备/评分器。prepare
临时解析摘要绑定的指令正文并与场景一起输出，角色不匹配时拒绝；它不调用模型、不联网，默认只写
stdout，真实响应和结果只能进入忽略目录或仓库外。未来原生评测平台证明等价后，只迁移
场景和评分语义并删除本地 runner。

## 角色协作

Builder 与 Tester 使用冻结基线协作：

- Builder 写业务代码和受影响文档，不写 Tester-owned 路径。
- Tester 与 Reviewer 的 initial spawn 显式使用 `fork_turns="none"`，task brief 只包含各角色需要的
  冻结输入；这只提供 conversation-fork isolation。Git/artifact isolation 由 runtime 的 worktree、
  manifest、ownership 与 evidence 证明；当前不提供 filesystem ACL，也不提供 platform attestation 或 context
  manifest，ledger 不伪造对应字段。后续 turn 通过 `prepare-follow-up` 续接同一 agent/thread，
  不再次清空上下文。
- `parallel_ready=true` 时，两者从 `spec_head` 并行；为 `false` 时，Tester 必须等
  `publish-prerequisites` 成功并从隔离 publication HEAD 启动。publication manifest 会绑定
  Tester author turn、integration 与 Reviewer prerequisite snapshot；发布路径随后不可变。
- Tester author 必须返回 `tests_ready`，runtime 将该 turn 与 Tester HEAD 持久化；即使测试 tree
  无变化，也必须显式完成 integration attestation。它只依据冻结计划、公开接口、计划声明的前置产物、
  测试支持文件和运行结果写测；不向它提供 candidate diff，并由 prompt 禁止读取其他 Builder
  实现。该读边界不是文件系统 ACL。
- Tester-owned 源码在独立 author thread 中提交，经 ownership gate、Git/source manifest、integration
  和 Reviewer 测试完整性审查后进入可信输入边界。Tester 不得主动篡改测试采集器或伪造框架事件；
  runtime 不以同解释器权限隔离任意恶意 Tester 代码，也不把 Git 可见性或 agent sandbox 描述成
  操作系统级安全边界。
- v3 Tester author 同时返回一次性测试鉴别 JSON。冻结计划的测试鉴别要求全部为 `strong` 时，runtime
  在 Tester integration 后、机器验证前执行基线先红或受控变异，以便在昂贵判据前淘汰无鉴别力测试；
  只要含 `reviewed-boundaries`，仍先完成机器验证，再执行边界和不变量映射并由 Reviewer 审核理由。
  证明不会写回测试目标，也不经 shell 执行命令；无论顺序如何，机器验证仍是独立必需门禁。
- Tester commit 集成后，Builder 可以读取测试并修复实现，但 ownership gate 阻止其修改测试。
- 所有非 L1 run 都必须由原 Tester thread 在 candidate worktree 对集成 HEAD 完成 blackbox
  `pass`。candidate worktree 必须没有 tracked、untracked 或 ignored residue；v2 evidence 同时记录
  worktree、全部真实 execution、case observations 和执行前后 HEAD。仅看到 agent 文本、Builder
  HEAD 或合成命令不构成
  blackbox 证据。日志、截图和缓存应写到 candidate 外的临时 artifact 目录；若工具仍在 candidate
  产生文件，Tester 必须清理并复核 residue 为空后才能返回 pass。
- 测试实现错误在目标不变时由原 Tester thread 修正。测试目标、ownership 或验收标准需要变化
  时，不在 frozen run 内批准：先 abandon 保留现场，再通过 `/plan` 生成更高 revision 的新方案；
  验证通过后使用原生“实施计划”动作交接，或由 `$builder` 启动新 run。
- Reviewer 在机器验证和黑盒验收后启动；非 L1 v3 还必须先完成测试鉴别证明。runtime 会在 Reviewer turn
  开始和完成时分别冻结 prerequisite snapshot；非 L1 只有 Tester integration、publication attestation
  （串行时）、`verified_head`、`e2e_verified_head`，以及 v3 适用时的 `test_effectiveness_head` 均绑定当前 candidate
  才接受 review evidence。finding 按 ownership 路由：已授权路径内的实现/文档
  由 Builder 修，测试实现由原 Tester author correction 修；需要新增写路径时进入 contract
  修订。目标不变的修复必须重新验证并 follow-up 同一
  Reviewer；Reviewer 因可补齐的前置缺口 blocked 时也保持该 thread。需要修改冻结契约时转入新 run，
  continuity 无效时安全停止，均不得用 fresh Reviewer 替代原身份。
- Reviewer custom-agent 配置是终态 `pass/findings/blocked` 的唯一来源。协调器 brief 不复制或扩展
  这组值；adapter 收到缺失或非法结果时保持连续性失败，不把未知值映射成 finding 或 pass。
- Reviewer `findings/blocked` 与 Tester `fail/target_change_required/blocked` 还必须返回结构化
  `PROBLEM_REPORT`。协调器原样调用 `record-problems`，runtime 绑定真实 role turn；报告未登记时
  follow-up 与 abandon 均失败。Hook 仍只解析最终结果标记，不读取或重建问题内容。
- Tester worktree 出现未提交改动时视为尚未集成，finalize 保留现场并停止。

## Runtime 与 ledger

稳定入口是 `codex-builder-loop` CLI。运行状态位于启动 run 的目标 worktree 下
`.builder-loop/codex/runs/`，不写 Codex 的受保护配置目录；Hook 从同一 Git repository 的 target、
Builder 或 Tester worktree 出发都会发现这一个状态家。ledger 只记录计划摘要、worktree/branch、
agent/turn 身份、候选、workspace snapshot、逐条问题与跨 revision 处理决定、evidence provenance、
Git 结果和事件，不保存模型推理或复制测试目标。问题内容由
`schema/codex-problem-report.schema.json` 定义，计划处理由
`schema/codex-prior-problems.schema.json` 定义，不另建 Issue 缓存或 transcript 索引。

稳定 Python 入口在导入 runtime package 前设置当前解释器及子进程的 no-bytecode 条件，普通 CLI
调用因此不会向调用方 worktree 写入 runtime `__pycache__`。显式 `py_compile` 与绕过稳定入口的
任意 import 保持 Python 原语义；runtime 不以清理 residue 冒充预防。

Hook 使用 Codex 提供的 `session_id` 找到唯一 active run。`start` 会在当前用户的私有 runtime
目录创建 locator，保存 repo/run 绑定、是否仍接收事件，以及首次 Tester Start 的冻结 baseline
attestation；`prepare-follow-up` 在 spawn 前把同一 Tester thread 的 pending dispatch attestation
原子更新到 locator。Hook 因此只读私有 locator 并写 journal，不依赖事件 `cwd`、Git 扫描或 run
ledger I/O。locator 是可由 ledger 重建的派生索引，不记录执行结论，也不能覆盖 ledger 中的 owner
identity；终态 run 未排空 journal 时会先关闭新事件接收并保留现场。

Subagent 生命周期采用 write-ahead delivery：Hook 只校验原生身份与最终结果标记，并把 versioned
event envelope 原子写入同一 session 的 delivery journal。journal entry 是尚未折叠的交付意图，
不是第二份角色事实；runtime 在任何消费角色状态的 gate 前，按 event id 幂等折叠到 ledger 的
`agents`、`pending_agent_turns`、`completed_agent_turns` 与 `events`，成功后才删除 entry。这样 Hook
热路径不扫描 Git、不等待 run ledger 锁，也不因固定 subprocess timeout 丢掉已经完成的 turn。
多个 CLI 可以同时观察到尚未删除的同一 entry，进程也可能在 ledger 写入后、删除 entry 前退出；
fold 因此先在同一 run lock 内识别已经接受的 role/turn/event，再校验真正的新 Tester start
attestation。相同 start 或相同 terminal result 重放为 `NOOP`，不会被解释成未准备 follow-up；
不同 result、未准备的新 turn 或不同 agent 仍按原连续性契约拒绝。
locator、journal 和 ledger 的 session/repo/run binding 任一不一致时 fail closed；未知 entry 不
adopt、不删除，矛盾的合法来源事件进入结构化 continuity failure。

具体生命周期规则：

- SessionStart 只注入当前 session 身份和使用提示。
- `SubagentStart` 属于 subagent/thread 创建事件，不保证在同一 thread 的 follow-up turn 再次触发；
  `SubagentStop` 才是逐 turn 事件。首次 turn 由二者直接记录。后续 turn 在根线程发送
  `followup_task` 前，必须先经 runtime `prepare-follow-up` 写入唯一 pending turn、冻结 dispatch
  边界事实并清除该角色旧 evidence；`SubagentStop` 用同一 agent id 和新的 turn id 原子认领该
  pending turn。若未来原生 surface 为 follow-up 提供 start 事件，同一 pending turn也可先由 start
  认领，再按普通 terminal event 完成。
- 每个 role 同时只允许一个 running 或 pending turn；terminal event 必须匹配当前 turn 或唯一
  pending follow-up。已接受 turn 的同一事件可幂等重放但不能生成第二份事实；冲突重放继续失败。
  Tester author correction 在 prepare 时立即使旧
  integration attestation 失效；Reviewer follow-up 的 prerequisite start snapshot 也在 prepare
  时冻结，不能用完成时快照倒填。
- Hook 只有在 terminal envelope 已可靠写入 journal 后才允许角色完成；短暂 ledger contention
  只会延后 fold，不会降级成 warning 后放行。`status` 与 `doctor` 公开 locator、queued event 与
  blocked delivery 状态，使“角色仍在运行”和“完成事件未交付”可机械区分。
- Stop 仅在 run 仍为 ACTIVE 时返回 Codex 的 continuation block。Builder 通过
  `BUILDER_INPUT_REQUIRED:<run_id>` 明确进入用户等待；冲突、continuity failure、fatal、
  完成或歧义状态均允许停止并展示原因。
- Agent thread 一旦 closed 就不能由新 id 接管；目标分支移动也会进入稳定的
  `CONTINUITY_FAILURE`，直到用户放弃现场或启动新 run。
- 每次机器验证都在 candidate commit 派生的临时干净 worktree 中执行，并在 ledger v2 记录
  candidate、stage、returncode、日志摘要和 failure fingerprint；验证命令改写 worktree 或 HEAD
  时不产生有效 evidence。同一 candidate 连续失败两次进入 `no_progress`，同一 fingerprint 在三个
  不同 candidate 重现进入 `architecture_review_required`。显式 `resume` 记录用户理由但不重置
  attempt；达到 `max_iterations` 后，
  当前 frozen run 不再继续 verify；每个 attempt 使用独立日志目录，历史 evidence 不被后续重跑
  覆盖。abandon 保留现场，修订方案进入新 run。
- Tester 初次 author 不计入 correction 窗口。之后每个已完成的 `purpose=author` follow-up 都从
  ledger `agent_event` 派生计数；最近一次 machine PASS 或用户对 correction 架构停止执行的显式
  resume 开启新窗口。前三次 correction 可继续，未恢复 machine PASS 时第四次 prepare 进入
  `architecture_review_required` 且不创建 pending turn。status/doctor 从事件派生当前和累计事实，
  resume 保留历史与 machine attempt 上限，不维护第二份可漂移计数器。
- `record-problems` 以 run/source/source-id/key 生成稳定 id，同一报告幂等、冲突重放拒绝。
  abandon 在改变 phase 前检查全部必须报告的角色结果，并原子封存问题 snapshot；`status`/`doctor`
  公开缺报告来源、继承数量和 snapshot 摘要。`backfill-problems` 只允许对旧 abandoned ledger 追加
  一次清单，完全相同内容幂等，不能覆盖已有 snapshot、plan、evidence、worktree 或 phase。
- repository runner entry、Makefile、pytest/ruff 配置和 package manifest 等可静态识别的控制面
  会进入 runtime 保护路径；显式反转、动态 exit 和内联 control flow 被拒绝。无法静态说明的复杂
  逻辑应下沉到计划声明的受保护 wrapper。wrapper 必须在 `spec_head` 已存在、位于仓库内且为
  普通文件；symlink、仓库外 target 和 PATH override 均拒绝。

## Evidence 与失效

ledger v2 为每类 evidence 记录 `observed_head`、`accepted_head`、输入 digest、scope 和 provenance。
所有非 L1 v3 run 都先完成 Tester integration：测试鉴别要求全部为 `strong` 时依次执行测试鉴别证明、
机器验证、同 thread blackbox、Reviewer；只要含 `reviewed-boundaries`，保持机器验证、测试鉴别证明、
同 thread blackbox、Reviewer 的兼容顺序。v2 ledger 保持机器验证后直接进入既有后续门禁，不生成或
回填测试鉴别证据。测试鉴别证明绑定 candidate、Tester source HEAD、Tester-owned manifest、命令/变异和
日志摘要；candidate 或 Tester integration 变化时立即失效，不能跨 HEAD 复用。E2E/review/doc-review 只能通过
`record-evidence` 写入，且必须携带 ledger 中已完成 agent turn 的 id；E2E 还必须携带可重放
details。只有全部必需 evidence 接受当前候选 HEAD，Tester commits 与 dirty tree 已
完整集成、worktree 无越界修改、目标分支仍满足 continuity 时，finalize 才能冻结最终提交。
目标 checkout 是否可同步是后续独立 gate，不再冒充 evidence readiness。

`schema/codex-test-proof.schema.json` 是 `prove-tests` 输入和成功 evidence 的唯一结构来源，使用
JSON Schema Draft 2020-12，并在标准 examples 中提供 unittest 基线先红与 pytest 受控变异正例；
runtime parser 实现该契约，不维护第二份字段定义。`prove-tests --spec -` 接受
`schema_version=1` JSON。每个 group 都含仅一个元素的 `behavior_ids`、
`method`、直接执行的 `argv`、`test_ids` 和 1..600 秒 `timeout_seconds`，所有 group 必须
让每个冻结 behavior 恰好出现一次；多 behavior group、重复、缺失或未知 coverage 在任何 proof
执行前拒绝。JSON Schema 视为整数的有限 number（例如 `1.0`、`30.0`）由 parser
规范化为整数；布尔值、非整数、NaN 和 Infinity 拒绝。成功 evidence 的 `run_id` 与 runtime/ledger
共用同一事务身份规则，不生成别名。基线先红还含 `claimed_failure_kind`；`mutation` 另含 unified Git
`patch`；成功 mutation evidence 还内联保存 runtime 从隔离 worktree 生成的 canonical full-index
binary-capable `applied_diff`，并让其文本与 `applied_diff_sha256`、changed paths 和执行结果共同描述
唯一 mutation。该 diff 进入现有 ledger evidence，不依赖原始 Tester payload、临时 worktree 或
sidecar artifact。`reviewed-boundaries` 含 `reason`，且 `reviewed_boundaries` 必须分别提供
`positive_test_ids`、`negative_test_ids`、`boundary_test_ids`、`invariant_test_ids`
四个非空列表。`argv` 只接受明确支持的非内联测试运行器形态，或 `spec_head` 已存在、由
`test_context.support_paths` 声明且双方都不可改的仓库脚本；其他命令默认拒绝。allowlisted runner
必须显式选择全部声明 test id，每个 id 还必须解析到 Tester source HEAD 中 Tester-owned 的普通文件
与 blob；仓库外 discovery、父路径和 pytest 路径重定向拒绝。runtime 还拒绝
env/PATH/PYTHONPATH 字段、命令分发器、内联 shell/解释器控制流或跨 ownership 变异。裸
`python/pytest` 会规范化为当前 runtime 的绝对 Python 可执行文件，仓库脚本 launcher 也会在执行前
固定并记录路径、摘要和冻结 blob。proof 子进程不继承调用方 PATH，而使用 runtime Python 所在目录
加系统默认路径，因此 wrapper 的 shebang 和内部裸命令也不能二次解析到 hostile PATH。候选阶段必须
从独立监督进程取得声明测试的逐项状态。监督进程独占最终结果 FD，不把它或对应环境变量传给测试
进程；pytest 的临时插件和 unittest 的临时 runner 只写原始框架事件，监督进程核对事件唯一性与真实
退出码后才写最终结果。原始 stdout/stderr 只进入日志，不能授权 PASS 或断言失败。受保护 wrapper
必须保留 runtime 注入的原始事件 FD 与环境，使其中唯一一次受支持的 pytest 执行返回事件；缺失、
重复或与进程退出码不一致时 fail closed。wrapper 显式转发受支持的 unittest/pytest 命令时，runtime
从冻结脚本后的命令后缀识别框架，把嵌套可执行文件规范化为可信 Python，并继续执行同一套测试来源、
完整 id 与结构化采集校验；无法唯一识别、嵌套 dispatcher 或绝对外部可执行文件在 spec 阶段直接拒绝，
不回落到 `auto` 或文本 PASS。proof supervisor 与测试子进程从最小可信环境启动，只继承基础身份、
临时目录、locale 和必要系统字段；外部 shell function、`BASH_ENV`、动态加载器、Python/pytest 启动
注入和调用方 PATH 均不进入执行链。runtime 只注入本次 proof 的 channel、缓存和随机临时插件路径，
禁用用户 site 与第三方 pytest 插件自动加载，并把缓存写入候选外，避免宿主环境、测试打印或可导入的
持久结果模块冒充测试结论。
基线先红和受控变异必须解析为 `assertion-failure`。零测试、导入、语法、收集、
配置、用法、启动错误、超时或无法识别的 runner 输出都不能形成测试鉴别 evidence。unittest 只按
完整 `module.Class.method`、pytest 只按完整 node id 精确映射声明测试；短方法名、任意子串、同名歧义
和未声明断言失败均保持未映射。candidate 的每个声明 id 必须在结构化结果中唯一、完整且普通通过；
skip、xfail、xpass、未执行或额外测试不能借其他通过测试形成 strong proof。失败类型取框架提供的真实
异常类型；同一 id 的 setup/call/teardown 和 subTest 事件保留累计计数，skip 或非断言错误支配普通
通过与断言失败。captured stdout、结束摘要或 atexit 追加文本都不参与分类。

runtime 消费 active v3 evidence 时重新核对上述原子 coverage；mutation 还必须含有摘要匹配的
canonical `applied_diff`。升级前留下的歧义或不可回放 evidence 不再满足当前 gate，只能在冻结目标
不变时由同一 Tester thread 重新证明；runtime 不 backfill 缺失 diff，也不改写 finalized/abandoned
历史 ledger。v2 ledger 继续不要求或补写 test-effectiveness evidence。

上述监督进程用于把最终证据写入权、进程退出码和原始输出从普通测试结果判定中分离，防止误用、
环境污染和空壳证明；它不是对可信 Tester 源码的恶意代码沙箱。同权限 Python 测试可反射或
monkeypatch 同进程对象这一事实不被隐藏，安全边界是受审 Tester source identity，而不是 reporter
对象本身。若未来需要执行不可信测试代码，必须另行引入并验证操作系统级隔离，不能在当前 reporter
上继续叠加特判。

计划可把每个 Builder-owned pattern 对 machine/blackbox 分为 `affects` 或 `exempt`；Tester、runner、
support、publication 和所有 accepted blackbox command 依赖强制属于 affects。runtime 先拒绝零
accepted execution，再逐条解析并合并全部 `method=command` 的仓库路径；其他 method 不参与 scope，
只有 accepted command 确实无法静态解析时才回退全 tree。unittest resolver 只把显式 `.py` target
和 discover start directory 视为可证明的窄输入；默认或仅 options 的 discovery 绑定当前目录，
bare、dotted 与无 `.py` 的 slash target 不查询 candidate tree，也不猜同名文件，而是产生
`RUNNER_DEPENDENCY_UNRESOLVED`，使整份 report scope 回退全 tree。完整规范化 report digest 进入
blackbox input context。候选变化后 runtime 重新计算
scope digest：相同才推进 `accepted_head` 并保留原 `observed_head`，不同则失效。未声明 scope 或
依赖不可静态说明时退化为全 tree。Reviewer/doc-review 对任意候选变化都失效。Skills 和 hooks
只能通过 runtime 公开命令记录 evidence，不能直接编辑 ledger。

## Diagnostics 与恢复

`doctor` 是只读事实汇总：列出 ledger/schema、owned/missing/orphan worktree、branch/head/residue、
workspace intake、evidence provenance、progress stop、finalize 与 cleanup 状态。它不修复、不 adopt
也不删除。`recover` 只重放已经持久化的 final/cleanup 事务；`cleanup` 只处理 terminal run 中
ledger-owned、clean 且 HEAD 未漂移的 worktree。未知 orphan 只报告人工检查入口。

新 start 写 ledger v2，并在 plan 摘要中标明 contract schema v3、blackbox report v2 与
canonical-v2/raw digests。读取 ledger v1 时先在内存规范化；
首次受锁写操作原子写回 v2 并追加 migration event。既有 plan v2 ledger 缺少新字段时按历史语义
解释，可继续诊断、恢复、清理和完成原事务，但不能补写 v3 测试鉴别 gate；未启动的 v2 计划必须
重新规划。旧 run 没有 evidence scope，迁移后按全 tree 语义继续，run/session/agent/turn、
candidate、intent 和 worktree 身份保持不变。

start 还把实际执行 adapter 的 `runtime_identity` 冻结进 ledger，包括 Codex/Claude Code 类型、
adapter commit、checkout dirty 状态和捕获状态。事故记录只能引用这份 planning-time/runtime-time
事实；旧 ledger 没有该字段时明确标为 `legacy-unavailable`，不得用任务结束时的当前 checkout
反推历史版本。

## 离线 Issue 分流实验

`experiments/issue-triage/` 归本仓维护，但不属于 builder-loop runtime。它只在需要校准时读取固定
fixtures 或真实 GitHub Issue，通过隔离的 Responses API 请求生成根因、攻击和分流建议；结果进入
私有运行目录，不写 ledger，也不触发 Planner、Builder、标签、评论或修复动作。

Issue 创建快照与关闭结论分别承担实验输入和事后对照。历史评测必须绑定事故 `incident_head` 并截断
修复后材料，避免模型从当前代码或结案评论看到答案。详细样本契约、角色和运行入口只在
[实验模块文档](../experiments/issue-triage/README.md) 维护，runtime 架构不复制其字段和路线快照。

## 交付后事故与知识复盘

finalize 完成后，Builder 只在出现多轮失败、冲突/recovery、Tester correction、Reviewer finding、
角色或 evidence 独立性异常、用户纠正的重要前提，或计划外工程缺陷时加载按需 retrospective
reference。复盘读取最终 ledger 的问题清单和老一轮逐项处理决定，不再依赖已经离开上下文的旧角色
文本；复盘不重新打开 delivery gate，也不写 runtime ledger。

每个工程事故最终只能归属 `current_project`、`builder_loop` 或 `external_platform`。同一因果链
跨越业务仓库与 builder-loop 时强制拆成两个原子事故；两条可以相互引用，但复现、责任和关闭条件
彼此独立。提交前先只读搜索重复项；计划外 Issue、问题文档写入或跳过通过 `request_user_input`
决定，当前任务关联 Issue 的正常更新沿用已有授权。问题记录只包含触发场景、现场过程、现象、
已确认事实、根因状态和复现条件，不包含建议或设计方向，并必须注明实际运行的是 Claude Code 版还是 Codex 版。

工程问题完成分流后，Builder 才以 `builder-loop delegated` 模式主动加载 `$memory-review`，只交付
不能通过代码、测试、正式契约、项目文档或 issue 固化的剩余知识。旧版五问打分和直接写 memory
协议不再属于 Builder prompt；memory-review 的结果也不构成交付 evidence。

## Git 事务

Builder 与 Tester 内部 commits 保留到审查完成。finalize 从已审 candidate 创建临时 ref 和
worktree，在其中生成 squash commit，并使用目标仓库 Git 身份执行 commit hooks。hook 拒绝提交、
产生额外 commit 或令最终 tree 偏离 candidate 时，目标分支不移动，run 保留现场并停止。

临时提交的 parent、tree 和 evidence 全部复核通过后，runtime 立即把 candidate、final commit、
expected-old 和 target branch 写成 finalize intent。intent 是冻结交付物的唯一事实；目标 checkout
residue 是实时文件系统事实，由 preflight、status、首次 finalize 和 recovery 共用的计算入口动态
分类，不复制进 ledger。

未授权 tracked/index 改动始终是同步 blocker；workspace-intake 路径只有仍等于 planning-time
snapshot 或 final state 时才是可恢复输入。普通与 ignored untracked 路径只有在和最终 tree 的写入路径
相同、形成文件/目录前缀冲突，或 `git read-tree -n -u -m` 无法证明安全时才阻塞；无关 `.env`、
cache 和日志保留原状。存在 blocker 时 final intent 与 staging ref/worktree 保留，目标 ref 不移动；
用户处理风险路径后重试同一 finalize，不重跑 hooks 或生成第二个提交。intent 存在期间拒绝新的
agent turn、验证、集成和 evidence 写入，避免冻结后候选继续变化。

blocker 为空时 runtime 才用 expected-old `git update-ref` compare-and-swap 更新目标分支并同步
worktree。若进程在 ref 更新后退出，recovery 会重新核对全部 delivery gates，并识别 ref 尚未移动、
ref 已为 final 但 index 仍为 expected-old tree、ref/index 均已为 final tree 三种安全状态。并发移动、
无法解释的 index 或同步失败会保留现场，不自动 rebase、清理用户文件、解决冲突或覆盖目标分支。
更新后的 postcondition 要求 final ref 和 candidate index tree，但允许同步前已存在且不冲突的
untracked/ignored residue。目标仓库 hooks 只在临时 final worktree 执行一次；内部 Builder/Tester
checkpoint 与 integration commit 继续跳过 hooks 和 GPG signing。

## 设计推导

- 设计哲学「契约先于实现」要求计划在工作开始前可判定，因此 validator 必须拒绝缺行为边界、
  串行前置产物、文档写边界或 revision 链接的方案；由此得到 `unit-test-spec` 与
  `documentation-spec` 两类冻结契约。
- 「每个事实只有一个家」要求 target、Builder、Tester 三个 worktree 不各存一份状态；由此状态
  留在启动 run 的目标 worktree，其他 worktree 只通过 Git worktree discovery 找到同一 ledger。
- 同一原则要求机器验证只有一个 effective source；因此仓库 loop 配置与计划 runner 不能并存，
  Planner 校验和 start 也必须复用同一 preflight，而不是各自解释一份 runner。
- 「判据按独立性分层」要求 Tester pass 对实际 candidate 可追溯；由此 blackbox evidence 绑定
  candidate worktree、每条真实 execution、case observation 和前后 HEAD，而不是只信一行自然语言
  或伪造 aggregate command；同一原则也要求串行
  Tester 只看到冻结公开文件，因此 runtime 发布 exact-file isolation HEAD/manifest，而不是一般
  Builder HEAD 或 candidate diff。
- 同一原则要求先明确判据的可信输入：独立 Tester 源码经 thread、ownership、Git/source manifest、
  integration 和 Reviewer 绑定后视为可信；由此 runtime 负责环境与证据完整性，而不把同解释器
  reporter 包装成任意恶意 Python 的安全沙箱。
- 「显式授权，默认隔离」要求未知 dirty 留在原处、精确授权输入冻结成 snapshot；由此只有真实
  覆盖风险才停止，而不是把 target 全局干净当成交付前提。
- 「契约与成熟行为先于实现」要求迁移维护逐项 parity corpus；由此删除旧 fixture 前必须明确
  covered、rescue 或 retired，不能只用新 runtime 测试证明自身自洽。
- 「改输入条件，不堆输出特判」要求 Python role hygiene 在 AST/token 层区分可执行 skip/xfail/
  rerun、module-level `pytestmark` 与 comments/string fixtures，并按 import binding 解析 alias 与局部
  shadowing；稳定 CLI 则在
  runtime import 前禁写 bytecode，而不是事后清理 residue。Reviewer 终态同理由 custom-agent 单点
  定义，协调器只传输入，不再用 prompt 特判重写输出值。
- 「每个事实只有一个家」要求工程事故按单一 owner 落盘；由此跨业务/loop 边界的因果链必须拆分，
  adapter 版本必须由 start 冻结，剩余隐含知识才交给原生 memory skill。

runtime 不自动 push、开 PR、解决冲突、adopt 未知 orphan 或恢复丢失的 subagent context。
