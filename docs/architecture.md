# Codex Builder Loop 架构

## 系统边界

Codex 原生 Plan mode 负责探索和追问；全局托管规则先让用户选择继续使用原生 Plan，或加载
Planner Skill 固定 builder-loop 方案契约。根线程作为 Builder，Codex subagent threads 承担
Tester 与 Reviewer。runtime CLI 只处理可确定验证的内容，不替模型做产品或架构判断。

```text
/plan ── request_user_input
   ├─ Codex 原生 Plan → proposed_plan
   └─ Builder-loop Planner
             │ validated plan
             ▼
        $builder ── optional exact dirty snapshot ── Builder worktree
    ├─ parallel_ready=true  ── Tester thread 与 Builder 并行
    └─ parallel_ready=false ─ exact public files → isolated publication HEAD/manifest
                                                   └→ Tester author baseline
        │ Tester author tests_ready → integrate tester commit
        ▼
clean candidate worktree verify → same-thread black-box pass → Reviewer(code/test/docs)
        │ all evidence points to candidate HEAD
        ▼
temporary final ref/worktree → hooks → tree check → target CAS → cleanup
```

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

计划契约只接受 `schema_version: 2`。计划保留 `plan-checklist`，并在非 L1 使用 `unit-test-spec`、在 L1 使用
`documentation-spec`；运行时验收可再增加 `e2e-cases`。非 L1 spec 必须包含规划时 HEAD、计划
版本、接口、测试上下文、角色写路径、行为边界/不变量和 mock 策略。串行计划还必须声明
`public_prerequisites`。其中每项必须是 Builder-owned 的精确普通文件路径，不能是 glob、目录或
symlink。具体字段以 validator 实现和 fixtures 为准。

非 L1 的 effective verification source 只能有一个。`spec_head` 存在 `.claude/loop.yml` 时，
该文件是唯一来源且计划必须省略 `test_context.runner`；不存在时，计划必须声明 runner。
`plan-validate --repo <repo>` 与 `start` 调用同一只读 preflight，统一核对目标分支、supersession、
effective runner、安全规则、ownership 和冻结依赖。前者不创建 ledger、run 目录或 worktree；
只有完成这些上下文检查才返回 `READY`，并公开 `effective_verification_source`。

`parallel_ready=true` 只用于 Tester 无需等待 Builder 产物即可依据冻结目标和公开契约写测的
计划。为 `false` 时，计划必须明确可独立冻结的最终公开契约文件，例如 schema、header 或接口
定义；后续实现必须落在其他 Builder-owned 文件，不能在同一 run 继续修改已发布文件。runtime
先自动 checkpoint Builder，再验证相对 `spec_head` 的最终 tree 只改变声明文件，从该 tree 合成
一个 parent 为 `spec_head` 的隔离 publication commit，并将 HEAD、tree、每个 blob 与 manifest
digest 写入 ledger。Tester 以 publication HEAD 为 author baseline，不接收 Builder branch HEAD、
candidate diff 或其他实现内容。中间 Builder 历史不会进入 Tester baseline；当前版本仍不宣称 Git
object database 具有操作系统级读 ACL。

`plan_revision=1` 表示首次契约。更高 revision 必须携带被替代 run id 与旧 plan digest；start 只在
旧 run 已 abandoned、digest 匹配且 revision 增加时接受，避免把原地放宽伪装成新证据。

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

## 角色协作

Builder 与 Tester 使用冻结基线协作：

- Builder 写业务代码和受影响文档，不写 Tester-owned 路径。
- `parallel_ready=true` 时，两者从 `spec_head` 并行；为 `false` 时，Tester 必须等
  `publish-prerequisites` 成功并从隔离 publication HEAD 启动。publication manifest 会绑定
  Tester author turn、integration 与 Reviewer prerequisite snapshot；发布路径随后不可变。
- Tester author 必须返回 `tests_ready`，runtime 将该 turn 与 Tester HEAD 持久化；即使测试 tree
  无变化，也必须显式完成 integration attestation。它只依据冻结计划、公开接口、计划声明的前置产物、
  测试支持文件和运行结果写测；不向它提供 candidate diff，并由 prompt 禁止读取其他 Builder
  实现。该读边界不是文件系统 ACL。
- Tester commit 集成后，Builder 可以读取测试并修复实现，但 ownership gate 阻止其修改测试。
- 所有非 L1 run 都必须由原 Tester thread 在 candidate worktree 对集成 HEAD 完成 blackbox
  `pass`。candidate worktree 必须没有 tracked、untracked 或 ignored residue；evidence 同时记录
  worktree、执行命令、returncode 和执行前后 HEAD。仅看到 agent 文本或 Builder HEAD 不构成
  blackbox 证据。日志、截图和缓存应写到 candidate 外的临时 artifact 目录；若工具仍在 candidate
  产生文件，Tester 必须清理并复核 residue 为空后才能返回 pass。
- 测试实现错误在目标不变时由原 Tester thread 修正。测试目标、ownership 或验收标准需要变化
  时，不在 frozen run 内批准：先 abandon 保留现场，再通过 `/plan` 生成更高 revision 的新方案
  并由 `$builder` 启动新 run。
- Reviewer 在机器验证和黑盒验收后启动。runtime 会在 Reviewer turn 开始和完成时分别冻结
  prerequisite snapshot；非 L1 只有 Tester integration、publication attestation（串行时）、
  `verified_head` 与 `e2e_verified_head` 均绑定当前 candidate 才接受 review evidence。finding 按 ownership 路由：已授权路径内的实现/文档
  由 Builder 修，测试实现由原 Tester author correction 修；需要新增写路径时进入 contract
  修订。目标不变的修复必须重新验证并 follow-up 同一
  Reviewer；需要修改冻结契约时转入新 run。
- Tester worktree 出现未提交改动时视为尚未集成，finalize 保留现场并停止。

## Runtime 与 ledger

稳定入口是 `codex-builder-loop` CLI。运行状态位于启动 run 的目标 worktree 下
`.builder-loop/codex/runs/`，不写 Codex 的受保护配置目录；Hook 从同一 Git repository 的 target、
Builder 或 Tester worktree 出发都会发现这一个状态家。ledger 只记录计划摘要、worktree/branch、
agent/turn 身份、候选、workspace snapshot、evidence provenance、Git 结果和事件，不保存模型推理
或复制测试目标。

Hook 使用 Codex 提供的 `session_id` 找到唯一 active run：

- SessionStart 只注入当前 session 身份和使用提示。
- `SubagentStart` 属于 subagent/thread 创建事件，不保证在同一 thread 的 follow-up turn 再次触发；
  `SubagentStop` 才是逐 turn 事件。首次 turn 由二者直接记录。后续 turn 在根线程发送
  `followup_task` 前，必须先经 runtime `prepare-follow-up` 写入唯一 pending turn、冻结 dispatch
  边界事实并清除该角色旧 evidence；`SubagentStop` 用同一 agent id 和新的 turn id 原子认领该
  pending turn。若未来原生 surface 为 follow-up 提供 start 事件，同一 pending turn也可先由 start
  认领，再按普通 terminal event 完成。
- 每个 role 同时只允许一个 running 或 pending turn；terminal event 必须匹配当前 turn 或唯一
  pending follow-up，已完成 turn 不能重放。Tester author correction 在 prepare 时立即使旧
  integration attestation 失效；Reviewer follow-up 的 prerequisite start snapshot 也在 prepare
  时冻结，不能用完成时快照倒填。
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
- repository runner entry、Makefile、pytest/ruff 配置和 package manifest 等可静态识别的控制面
  会进入 runtime 保护路径；显式反转、动态 exit 和内联 control flow 被拒绝。无法静态说明的复杂
  逻辑应下沉到计划声明的受保护 wrapper。wrapper 必须在 `spec_head` 已存在、位于仓库内且为
  普通文件；symlink、仓库外 target 和 PATH override 均拒绝。

## Evidence 与失效

ledger v2 为每类 evidence 记录 `observed_head`、`accepted_head`、输入 digest、scope 和 provenance。
所有非 L1 run
必须先取得 Tester author `tests_ready` 并完成 integration，再由同一 thread 在 candidate
worktree 对集成 HEAD 产出 blackbox `pass` evidence。E2E/review/doc-review 只能通过
`record-evidence` 写入，且必须携带 ledger 中已完成 agent turn 的 id；E2E 还必须携带可重放
details。只有全部必需 evidence 接受当前候选 HEAD，Tester commits 与 dirty tree 已
完整集成、worktree 无越界修改、目标分支仍满足 continuity 时，finalize 才能冻结最终提交。
目标 checkout 是否可同步是后续独立 gate，不再冒充 evidence readiness。

计划可把每个 Builder-owned pattern 对 machine/blackbox 分为 `affects` 或 `exempt`；Tester、runner、
support、publication 和实际 blackbox command 依赖强制属于 affects。候选变化后 runtime 重新计算
scope digest：相同才推进 `accepted_head` 并保留原 `observed_head`，不同则失效。未声明 scope 或
依赖不可静态说明时退化为全 tree。Reviewer/doc-review 对任意候选变化都失效。Skills 和 hooks
只能通过 runtime 公开命令记录 evidence，不能直接编辑 ledger。

## Diagnostics 与恢复

`doctor` 是只读事实汇总：列出 ledger/schema、owned/missing/orphan worktree、branch/head/residue、
workspace intake、evidence provenance、progress stop、finalize 与 cleanup 状态。它不修复、不 adopt
也不删除。`recover` 只重放已经持久化的 final/cleanup 事务；`cleanup` 只处理 terminal run 中
ledger-owned、clean 且 HEAD 未漂移的 worktree。未知 orphan 只报告人工检查入口。

新 start 写 ledger v2。读取 v1 时先在内存规范化；首次受锁写操作原子写回 v2 并追加 migration
event。旧 run 没有 evidence scope，迁移后按全 tree 语义继续，run/session/agent/turn、candidate、
intent 和 worktree 身份保持不变。

start 还把实际执行 adapter 的 `runtime_identity` 冻结进 ledger，包括 Codex/Claude Code 类型、
adapter commit、checkout dirty 状态和捕获状态。事故记录只能引用这份 planning-time/runtime-time
事实；旧 ledger 没有该字段时明确标为 `legacy-unavailable`，不得用任务结束时的当前 checkout
反推历史版本。

## 交付后事故与知识复盘

finalize 完成后，Builder 只在出现多轮失败、冲突/recovery、Tester correction、Reviewer finding、
角色或 evidence 独立性异常、用户纠正的重要前提，或计划外工程缺陷时加载按需 retrospective
reference。复盘不重新打开 delivery gate，也不写 runtime ledger。

每个工程事故最终只能归属 `current_project`、`builder_loop` 或 `external_platform`。同一因果链
跨越业务仓库与 builder-loop 时强制拆成两个原子事故；两条可以相互引用，但复现、责任和关闭条件
彼此独立。提交前先只读搜索重复项，再通过 `request_user_input` 请求创建 issue、追加已有 issue、
写入项目声明的问题文档或跳过。问题记录只包含触发场景、现场过程、现象、已确认事实、根因状态和
复现条件，不包含建议或设计方向，并必须注明实际运行的是 Claude Code 版还是 Codex 版。

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
  candidate worktree、命令、退出码和前后 HEAD，而不是只信一行自然语言；同一原则也要求串行
  Tester 只看到冻结公开文件，因此 runtime 发布 exact-file isolation HEAD/manifest，而不是一般
  Builder HEAD 或 candidate diff。
- 「显式授权，默认隔离」要求未知 dirty 留在原处、精确授权输入冻结成 snapshot；由此只有真实
  覆盖风险才停止，而不是把 target 全局干净当成交付前提。
- 「契约与成熟行为先于实现」要求迁移维护逐项 parity corpus；由此删除旧 fixture 前必须明确
  covered、rescue 或 retired，不能只用新 runtime 测试证明自身自洽。
- 「每个事实只有一个家」要求工程事故按单一 owner 落盘；由此跨业务/loop 边界的因果链必须拆分，
  adapter 版本必须由 start 冻结，剩余隐含知识才交给原生 memory skill。

runtime 不自动 push、开 PR、解决冲突、adopt 未知 orphan 或恢复丢失的 subagent context。
