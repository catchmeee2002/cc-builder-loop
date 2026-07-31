---
name: builder-loop-planner
description: 在 Codex Plan mode（/plan）中为代码或文档变更生成 builder-loop 可验证方案，固定行为验收、独立测试目标、角色写边界、机器验证命令与审查范围。进入 Plan mode 且后续可能使用 $builder 交付时自动使用；普通问答、只读分析或已给出可执行且已验证方案时不要使用。
---

# Builder Loop Planner

只产出方案，不实现代码。

## 维护门禁

Full Driver v4 重建期间，本 Skill 不接受业务规划。若被显式调用、自动发现或由历史 continuation marker
加载，不运行 `plan-preflight`、`plan-validate`，不生成 Builder-loop spec，也不输出
`BUILDER_HANDOFF_READY`；直接说明维护期统一使用 Codex 原生 Plan 后停止。

## 建立上下文

1. 若同一 session 紧邻上一轮是 `BUILDER_CONTINUATION_READY:<preparation-run-id>`，本轮直接进入
   Builder-loop Planner，不重复规划路线卡。首次调用前运行 `status --help`，再用 marker 的 run id
   查询 runtime；仅当 `status=COMPLETE`、`continuation.ready=true`、owner session、repo、target
   branch/HEAD 与当前上下文一致且链接未被消费时继续。依据 preparation ledger 的唯一链接读取原
   abandoned business plan 与问题 snapshot，以 preparation final HEAD 为新 `spec_head`，生成更高
   business revision，并以老一轮冻结计划（old frozen plan）保持原业务目标；任何 stale、replay、跨 session、跨 repo、未 finalized 或 target drift 都 fail
   closed，回到普通路线卡或要求显式 run id。不得解析 transcript 重建业务事实。
2. 读取适用的 `AGENTS.md`、项目约束和设计哲学。
3. 检查 Git 根目录、当前 `HEAD`、该 HEAD 是否存在 `.claude/loop.yml`、测试布局、可复制执行的
   验证命令、对外接口和 target checkout residue。
4. 修改或新建 Markdown 前，读取项目声明的适用文档政策；项目未声明时读取
   `${CODEX_HOME:-$HOME/.codex}/builder-loop/doc-policy.md`。政策不可读时停止文档方案，
   不自行发明替代规则。
5. 只在答案会实质改变方案时使用 `request_user_input`。不要用纯文本问题替代。
   选项卡面向用户只说可观察行为和成本，统一使用“老一轮、问题清单、新一轮、本轮解决、已在别处
   处理、不再处理”等白话；`turn`、`finding`、`supersession` 等内部字段只出现在技术方案和证据中。
6. 不按 diff 大小决定验证深度。按行为变化、接口契约、数据风险和用户可见影响定义证据。
7. 先按后果判断规划深度：局部可逆任务不强制比较方案；影响广、难回退或范式级任务扩大分析。
8. 只有高影响选择存在真实分叉时，完整读取
   [方案取舍与演进](references/design-decisions.md)。范式级选择必须展示取舍并交还用户决定。
9. 只有一条可信路径时说明其他方向被什么约束排除，不虚构备选方案，也不恢复固定问卷。
10. target dirty 默认不带入任务。只有任务明确依赖某个 dirty 文件时，使用选项卡取得 exact-path
   授权，再运行 `workspace-scan --repo <repo> --path <path>`；把返回的 path/state digest 原样写入
   `workspace-intake` marker。不得自行选择、复制、stash 或概括成目录/glob。

## 固定方案契约

在最终方案中包含 `plan-checklist`。对任何可执行行为变化，再包含
`unit-test-spec`；纯 Markdown 文档任务改用 `documentation-spec` 并写出独立一行
`预估改动级别：L1`。两种 spec 都冻结规划时 HEAD、revision 和精确写边界。L1 不得包含
`unit-test-spec/e2e-cases`；需要运行时行为验收时包含 `e2e-cases`。

使用以下结构，字段名保持不变：

```markdown
<!-- unit-test-spec -->
schema_version: 3
spec_head: "<planning-time HEAD>"
plan_revision: 1
parallel_ready: true
interfaces:
  - "<public interface or black-box entry>"
test_context:
  target_test_dirs: ["tests"]
  support_paths: ["<test-only support paths>"]
  public_prerequisites: []
  # 仅当 spec_head 不存在 .claude/loop.yml 时保留下一行；存在时整行省略。
  runner: "<one deterministic verification command>"
ownership:
  builder_write: ["src/**", "<affected documentation paths when needed>"]
  tester_write: ["tests/**"]
# 可选；省略时 machine/blackbox 都按全 tree 失效。
evidence_scopes:
  machine:
    affects: ["src/**"]
    exempt: ["README.md"]
  blackbox:
    affects: ["src/**"]
    exempt: ["README.md"]
behaviors:
  - id: <kebab-case-id>
    what: "<observable behavior>"
    boundaries: ["<boundary>"]
    invariants: ["<must remain true>"]
test_effectiveness:
  requirements:
    - behavior_id: <same-kebab-case-id>
      minimum: strong
mock_strategy: {}
<!-- /unit-test-spec -->

<!-- plan-checklist -->
- [ ] <implementation outcome tied to one behavior>
- [ ] <machine verification evidence>
- [ ] <tester, reviewer, and documentation evidence target the integrated HEAD>
<!-- /plan-checklist -->
```

更高 `plan_revision` 还必须在 spec 之外加入：

```markdown
<!-- prior-problems -->
schema_version: 1
snapshot_sha256: "<老一轮 abandon 返回的摘要>"
items:
  - problem_id: "<老一轮问题 id>"
    handling: include
    plan_refs: ["behavior:<当前 behavior id>"]
<!-- /prior-problems -->
```

每个老问题恰好选择一次：`include` 引用真实 behavior/checklist；`handled_elsewhere` 写稳定
`reference`；`discard` 只在用户明确决定后写非空 `reason`。即使老清单为空也保留 marker。

受保护验证支持需要先准备时，先调用只读
`plan-preflight --repo <repo> [--run <abandoned-run>] --path <exact-path>...`。当前 runner/control
重叠只接受 `VERIFICATION_BOOTSTRAP_REQUIRED`；只有旧 run support-only 重叠可创建独立 revision 1
准备计划，并增加 `verification-preparation` marker，逐字使用 preflight 返回的旧 run id、plan
digest、problem snapshot、非空 problem ids 和 exact eligible paths。准备计划不得 supersede 业务
run，且当前 machine runner/control/support 仍不得进入 Builder ownership。

准备 run finalized 后的业务 revision 增加最小 marker：

```markdown
<!-- continuation-from -->
schema_version: 1
preparation_run_id: "<finalized preparation run id>"
<!-- /continuation-from -->
```

该 revision 正常 supersede 原 business run，不 supersede preparation run；preflight 链接的 problem
ids 必须在 `prior-problems` 中 `handled_elsewhere`，reference 包含 preparation final commit，其余
问题仍逐条处理。续接不绕过本 Skill 的 plan validation 或普通实施授权。

需要运行时行为验收时，使用唯一规范格式：

```markdown
<!-- e2e-cases -->
schema_version: 1
cases:
  - id: <unique-kebab-case-id>
    covers: [<behavior-id>]
    input: "<actual trigger>"
    level: full
    hard_rules:
      response_contains: ["<mechanical signal>"]
    verify:
      must: ["<positive observable>"]
      must_not: ["<negative observable>"]
    quality:
      criteria: ["<semantic quality criterion>"]
<!-- /e2e-cases -->
```

`fast` case 必须有非空 `hard_rules`，且省略 `verify/quality`；`full` case 必须同时提供
`verify.must`、`verify.must_not` 和 `quality.criteria`，可省略 `hard_rules`。不要在计划或 prompt 中
另存 fast/full 的结果状态映射；runtime 在 `prepare-follow-up --purpose blackbox` 时从这份冻结
cases 派生唯一 `blackbox_report_contract`。

任务明确接入 planning-time dirty 文件时，在 spec 前增加：

```markdown
<!-- workspace-intake -->
schema_version: 1
files:
  - path: "src/exact-file.py"
    state_sha256: "<workspace-scan 返回的 64 位摘要>"
<!-- /workspace-intake -->
```

纯文档任务使用：

```markdown
预估改动级别：L1

<!-- documentation-spec -->
schema_version: 3
spec_head: "<planning-time HEAD>"
plan_revision: 1
ownership:
  builder_write: ["README.md", "docs/<affected>.md"]
<!-- /documentation-spec -->

<!-- plan-checklist -->
- [ ] <documentation outcome>
- [ ] <Reviewer content and document-policy audit on final HEAD>
<!-- /plan-checklist -->
```

满足以下约束：

- 让 `spec_head` 等于规划时 Git `HEAD`。
- `workspace-intake` 只能列用户明确授权、属于 `builder_write` 的 exact regular file 或 tracked
  deletion。ignored、symlink、目录、Tester-owned、support/runner/control 路径不得接入；marker
  省略表示全部 target dirty 留在原处且不进入 Builder。
- `evidence_scopes` 省略时保持全 tree fail-closed。使用时，machine 与 blackbox 必须分别把每个
  `builder_write` pattern 恰好放入 affects 或 exempt；Tester、support、runner、publication 和
  blackbox 实际命令依赖由 runtime 强制加入 affects。只有能证明不影响该 gate 的路径才能 exempt，
  Reviewer/doc-review 永不跨 HEAD 复用。
- 始终让 builder 与 tester 写路径互斥；只有 Tester 能完全依据 `spec_head`、冻结目标和既有公开
  黑盒面写测时才设置 `parallel_ready: true`。
- 设置 `parallel_ready: false` 时，在方案正文和 checklist 中明确 Builder 必须先提交的公开前置
  产物、对应路径及 Tester 可见的公开入口，并把同一事实写入非空
  `test_context.public_prerequisites`。每项必须是 Builder-owned 的精确普通文件路径，不得使用 glob、
  目录或 symlink；文件必须是可独立冻结的最终公开契约产物，发布后同一 run 不再修改，后续实现
  写入其他 Builder-owned 文件。为 `true` 时该字段必须为空。Tester 只接收 runtime 合成的隔离
  publication HEAD/manifest、冻结契约和黑盒入口，不接收 Builder HEAD、candidate diff 或其他
  实现文件。
- 新 structured CLI/API 的 wire shape 若在 `spec_head` 不存在且 Tester 写测依赖精确的 exit code、
  status/code、字段层级或生命周期场景，必须把一份最终 machine-readable contract 作为串行公开
  前置产物。该 exact Builder-owned 文件同时列入 `interfaces` 和 `public_prerequisites`，以便
  `interface_input_paths` 机械绑定 publication；契约应冻结可验证 examples 与场景前提，Tester 不得
  从错误文本、候选 diff 或实现细节猜测输出。发布后实现和测试只引用该契约，不增加迎合错误断言的
  兼容字段。
- 让 tester 独占 `target_test_dirs`；不要把这些路径放进 `builder_write`。
- 把测试目标写成输入、输出、边界和不变量，不写实现方式。
- 每个 behavior 使用唯一 kebab-case id，并提供非空 `boundaries` 与 `invariants`；始终提供映射类型
  `mock_strategy`，即使当前为空。
- `test_effectiveness.requirements` 必须让每个 behavior id 恰好出现一次。缺陷修复、新行为、
  公共接口、协议或 schema 变化使用 `minimum: strong`；纯重构或冻结行为已经正确且无法形成
  有意义反例时才用 `minimum: reviewed-boundaries`。后者仍须由 Tester 映射正向、反向、边界和
  不变量测试，并由 Reviewer 审核理由；不能自动把 strong 降级。
- 测试有效性证明把独立 Tester thread 产出、经 ownership、Git/source manifest、integration 和
  Reviewer 审查绑定的 Tester-owned 源码视为可信输入。runtime 仍须阻断 hostile PATH、runner
  篡改、输出伪造、空壳执行和基础设施错误，但当前契约不承诺隔离任意恶意 Python 测试代码；
  不得把同进程 reporter 或外部 supervisor 描述成操作系统级安全边界。
- 只声明一个验证来源：`spec_head` 存在 `.claude/loop.yml` 时省略 `test_context.runner`，并按其中
  实际命令把 repository wrapper 列入 `support_paths`；不存在时必须给出一个可直接运行的
  `test_context.runner`。不要使用恒真或仅打印命令。
- 把测试工具支持文件列入 `support_paths`，并保持其不属于 `builder_write`；Tester 需要修改时
  再显式列入 `tester_write`，不要借此扩大 Tester 的业务源码写权限。
- `make`、pytest、包管理器等 runner 的 Makefile、测试配置、package manifest 等控制文件由
  runtime 自动保护；不要把这些路径授权给 Builder 或 Tester。复杂组合逻辑放进已声明的受保护
  repository wrapper，不用 `!`、动态 exit、PATH override 或内联 shell control flow 反转退出码。
  wrapper 必须在规划时 `spec_head` 已存在、位于仓库内且是普通文件而非 symlink，并列入
  `support_paths`；当前版本不允许在被它验证的同一个 run 中创建或修改。缺少 wrapper 时先做独立、
  用户授权的准备提交，再基于新 HEAD 重新 `/plan`。
- 先按文档政策判断本次是否触及文档面；触及时把具体 README/docs 路径加入
  `ownership.builder_write` 和 checklist，使 Builder 能在首次审查前同步文档。未触及时不添加
  找补式文档任务。L1 `documentation-spec` 只能列精确 `.md` 文件，不能用目录或 glob 扩权。
- 让 checklist 同时覆盖功能完成、机器判据、黑盒验收、代码审查和文档审计。
- 对所有非 L1 计划，把 Tester author `tests_ready` 和同一 thread 针对 integrated HEAD 的
  blackbox `pass` 写入 checklist；不能只依赖机器测试或 Reviewer。
- 对 UI、CLI、API、跨进程或集成行为增加 `e2e-cases`；case id 唯一且 `covers` 只能引用冻结
  behavior。只允许 `tools_called`、`tools_not_called`、`min_tools`、`max_tools`、
  `max_steps`、`response_contains`、`response_not_contains` 作为机械规则，并保持正反断言
  无冲突。
- 测试目标、ownership 或验收标准一旦需要变化，不设计“当前 run 内批准”路径。保持原契约时
  续接原 thread；选择修订时先 abandon 当前 run 保留现场，再由 `/plan` 提升 `plan_revision`
  生成新方案；方案验证通过后可使用原生“实施计划”动作交接，`$builder` 保留为手工入口。
- `plan_revision=1` 不写 `supersedes`。从 abandoned run 修订时使用更高 revision，并在 spec 中
  增加 `supersedes.run_id` 与旧 ledger 的 `plan.sha256`；runtime 会验证旧 run 已 abandoned、摘要
  一致且 revision 单调增加。还必须读取 abandon 返回的问题清单，逐项生成唯一 `prior-problems`；
  旧 ledger 没有清单时先在 Default mode 用 `backfill-problems` 补录，不能把“没有记录”当成“没有问题”。

## 验证方案

1. 首次调用前运行 `codex-builder-loop plan-validate --help`；涉及验证支持写入时先运行
   `codex-builder-loop plan-preflight --help`，需要 intake 时再运行
   `codex-builder-loop workspace-scan --help`，以当前 CLI 为准。
2. 将完整方案通过 stdin 传给 `codex-builder-loop plan-validate --repo <git-root>`。
3. 只解析 stdout 最后一行 JSON，并要求至少包含 `status` 与 `message`。
4. `status=READY` 时核对 `effective_verification_source` 与 `spec_head` 的实际来源一致，并保存返回的
   canonical-v2 `plan_sha256`、`plan_source_sha256` 和 `plan_digest_kind`。最终方案正文必须与本次
   验证输入一致；Builder 后续只能增加或替换唯一受管生命周期 header，重验后的 `plan_sha256`
   必须保持相同，raw source digest 继续作为字节级审计事实。
5. `status=NEEDS_USER` 时使用 `request_user_input` 处理 JSON 指出的实质选择，更新方案后重验。
6. `status=FATAL` 或输出不是合法 JSON 时停止，不猜测成功。
7. 其他状态按 `message` 修正可修复的契约问题，再运行一次；不要绕过 validator。

最终回答保留完整计划 marker 内容。只有 `status=READY` 时，才在冻结方案正文之外、
`<proposed_plan>` 结束后追加以下独立行：

```text
BUILDER_HANDOFF_READY
```

`status` 不是 `READY` 时不得输出 `BUILDER_HANDOFF_READY`。
该行不进入 plan-validate 输入、计划 Markdown、plan digest 或 ledger。用户随后可使用 Codex 原生
“实施计划”动作进入 Default mode 并交接 Builder，`$builder` 继续作为手工入口。不要创建
或启动 builder-loop run，不写 ledger，也不要提前生成实现代码。
