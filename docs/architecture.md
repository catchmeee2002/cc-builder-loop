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
        $builder ── Builder worktree
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
candidate diff 或其他实现内容。中间 Builder 历史不会进入 Tester baseline；v1 仍不宣称 Git
object database 具有操作系统级读 ACL。

`plan_revision=1` 表示首次契约。更高 revision 必须携带被替代 run id 与旧 plan digest；start 只在
旧 run 已 abandoned、digest 匹配且 revision 增加时接受，避免把原地放宽伪装成新证据。

唯一例外是显式 `预估改动级别：L1` 的纯 Markdown 文档任务。它用
`documentation-spec` 冻结 planning-time HEAD、revision 和精确 `builder_write`，不包含
`unit-test-spec/e2e-cases`。runtime 还会机械拒绝非 `.md` 改动，并跳过 Tester、机器验证和
E2E gate；Reviewer 的方案审查与 Phase D 文档审计仍然必需。

## 角色协作

Builder 与 Tester 使用冻结基线协作：

- Builder 写业务代码和受影响文档，不写 Tester-owned 路径。
- `parallel_ready=true` 时，两者从 `spec_head` 并行；为 `false` 时，Tester 必须等
  `publish-prerequisites` 成功并从隔离 publication HEAD 启动。publication manifest 会绑定
  Tester author turn、integration 与 Reviewer prerequisite snapshot；发布路径随后不可变。
- Tester author 必须返回 `tests_ready`，runtime 将该 turn 与 Tester HEAD 持久化；即使测试 tree
  无变化，也必须显式完成 integration attestation。它只依据冻结计划、公开接口、计划声明的前置产物、
  测试支持文件和运行结果写测；不向它提供 candidate diff，并由 prompt 禁止读取其他 Builder
  实现。该读边界不是 v1 的文件系统 ACL。
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
agent/turn 身份、候选 HEAD、证据 HEAD、Git 结果和事件，不保存模型推理或复制测试目标。

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
- 每次机器验证都在 candidate commit 派生的临时干净 worktree 中执行，并增加 ledger 中的
  attempt；验证命令改写 worktree 或 HEAD 时不产生有效 evidence。达到 `max_iterations` 后，
  当前 frozen run 不再继续 verify；每个 attempt 使用独立日志目录，历史 evidence 不被后续重跑
  覆盖。abandon 保留现场，修订方案进入新 run。
- repository runner entry、Makefile、pytest/ruff 配置和 package manifest 等可静态识别的控制面
  会进入 runtime 保护路径；显式反转、动态 exit 和内联 control flow 被拒绝。无法静态说明的复杂
  逻辑应下沉到计划声明的受保护 wrapper。wrapper 必须在 `spec_head` 已存在、位于仓库内且为
  普通文件；symlink、仓库外 target 和 PATH override 均拒绝。

## Evidence 与失效

机器验证、Tester blackbox、Reviewer 和文档审查都记录对应的 commit HEAD。所有非 L1 run
必须先取得 Tester author `tests_ready` 并完成 integration，再由同一 thread 在 candidate
worktree 对集成 HEAD 产出 blackbox `pass` evidence。E2E/review/doc-review 只能通过
`record-evidence` 写入，且必须携带 ledger 中已完成 agent turn 的 id；E2E 还必须携带可重放
details。只有全部必需 evidence 指向当前候选 HEAD，Tester commits 与 dirty tree 已
完整集成、worktree 无越界修改、目标分支仍在起始 HEAD 时，finalize 才能运行。

候选 HEAD、计划 digest 或 Tester-owned tree 任一变化都会使旧 evidence 失效。Skills 和 hooks 只能通过 runtime 的公开命令记录 evidence，不能直接编辑 ledger。

## Git 事务

Builder 与 Tester 内部 commits 保留到审查完成。finalize 从已审 candidate 创建临时 ref 和
worktree，在其中生成 squash commit，并使用目标仓库 Git 身份执行 commit hooks。hook 拒绝提交、
产生额外 commit 或令最终 tree 偏离 candidate 时，目标分支不移动，run 保留现场并停止。

只有临时提交的 parent、tree 和 evidence 全部复核通过，runtime 才用 expected-old
`git update-ref` compare-and-swap 更新目标分支，再同步干净 target worktree。CAS 前先把 candidate、
final commit、expected-old 和 target branch 写成 finalize intent；若进程在 ref 更新后退出，下一次
finalize 会重新核对全部 delivery gates，识别 final ref、补齐 worktree 同步并进入 cleanup。intent
存在期间拒绝新的 agent turn、验证、集成和 evidence 写入，避免审查失效后仍移动目标。并发移动使 CAS 失败并保留 staged
final commit，不会覆盖其他 ref 变化。目标仓库 commit hooks
已在临时 final worktree 完整执行，内部 Builder/Tester checkpoint 与 integration commit 则显式
禁用 hooks 和 GPG signing，因为它们不会进入最终历史。
更新后再次核对 ref/tree/clean worktree，再清理临时 ref/worktree 与角色 worktree。若目标分支已移动、
删除或 CAS/target worktree 同步失败，则保留
现场，不自动 rebase、解决冲突或覆盖目标分支。内部 checkpoint 使用隔离身份且跳过目标 hooks。

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
- 「显式授权，默认拒绝」要求修复不能跨 ownership、runner 控制文件不能由被测角色改写；由此
  findings 按 Builder/Tester/contract 路由，无法静态解释的验证逻辑停止而非猜测放行。

v1 不自动 push、开 PR、解决冲突或恢复丢失的 subagent context。
