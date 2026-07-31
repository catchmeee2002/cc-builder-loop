# codex-builder-loop

## v4 实验开放期

[保质期: Full Driver v4 完成跨项目真实验收, owner: cc-builder-loop, 正向归宿: CHANGELOG.md]

Plan 环节现在提供「Codex 原生 Plan」与「Builder-loop 实验」一次选择。实验路线由 Planner 生成并
验证 Assurance v4 contract，紧邻的原生“实施计划”动作自动进入 `$builder`，再桥接 Full Driver v4。
legacy `start` 仍默认返回 `BUILDER_MAINTENANCE_DISABLED`，安装器也不注册 Builder lifecycle hooks；
v2/v3 ledger 保留原位，只继续支持诊断、恢复、finalize 与安全 cleanup。

该入口明确标为实验功能。Full Driver v4 仍需完成历史高频 Revision 回放、至少两个非本仓项目五次
真实交付且零非语义 Revision，才可去掉实验标识；用户不需要直接学习 v4 CLI 或 Core 命令。

维护分支已新增新旧隔离的 Assurance Core v4 实验实现：四个 contract facet 分别绑定 digest，
candidate/evidence、独立角色来源、机器验证、target rematerialization 和 finalize CAS 由 Core 负责；
Full Driver 只消费 Core 的 missing/stale gate 来调度原生 Agent。该实现仅通过带
`--experimental-v4` 的内部 `assurance` namespace 和显式 `$full-driver-v4-experiment` Skill 使用；
安装器会部署该内部 Skill且禁止其被普通请求隐式发现；面向用户的唯一入口仍是 Plan 选项和 `$builder`。

面向 Codex CLI 的独立判据交付闭环。进入 Plan mode 后先选择规划方式：

```text
/plan <需求>
├─ Codex 原生 Plan
└─ Builder-loop 实验 Planner → 原生“实施计划” → Full Driver v4
                              └─ $builder 手工回退
```

全局托管规则会在每次进入 Plan mode 时通过选项卡询问。选择 Codex 原生 Plan 时不加载本项目
Skill；选择 Builder-loop 实验时，Planner Skill 冻结并验证 v4 四事实面 contract，验证通过后输出
一次性就绪标记。用户选择 Codex 原生“实施计划”后，下一轮 Default mode 自动进入 Builder；也可
显式调用 `$builder`。Builder 只做授权桥接，Tester、proof、机器验证、blackbox、Reviewer 和 Git
收尾统一由 Full Driver v4 实验闭环处理。

## 核心行为

- Builder-loop Planner 在输出前运行带仓库上下文的确定性 plan validator；缺少规划 HEAD/revision、行为边界
  与不变量、mock 策略、角色写边界、串行公开前置产物或有效 checklist 的计划不能进入执行，
  实际生效的验证配置不合法时也不会返回 `READY`。
- `READY` 后的 `BUILDER_HANDOFF_READY` 位于冻结方案之外，只对同 session 紧邻的原生“实施计划”
  动作有效；标记缺失、过期、方案变化或 Codex 原生 Plan 不会隐式启动 builder-loop。标记不进入
  plan digest 或 ledger，`$builder` 继续作为兼容入口。
- 详细规划前可用只读 `plan-preflight --path <exact-path>` 检查预期写入。当前 machine runner/control
  重叠继续要求仓库外 bootstrap；若只命中 abandoned business run 的旧 support path，则先做独立
  preparation run。它完成后输出 `BUILDER_CONTINUATION_READY:<run-id>`，同 session 紧邻的下一次
  Plan mode 从 ledger 与 Git 恢复原 business revision；仍须重新验证计划和取得普通实施授权。
- 主仓 dirty 默认留在原处且不进入 run。任务确实依赖现有改动时，Planner 取得 exact-path 授权并用
  `workspace-scan` 冻结 state digest；`start` 合成不可变 snapshot 给 Builder，主仓和全局 stash
  全程不动。finalize 只在授权路径未漂移时消费 snapshot。
- `parallel_ready=true` 时 Builder 与 Tester 从同一个 `spec_head` 并行。为 `false` 时，Builder
  先发布计划声明的精确公开文件；runtime 从 `spec_head` 合成隔离 publication HEAD/manifest，
  将其设为 Tester 基线，并冻结这些文件后再接受 Tester author turn。
- Tester 首次写测不接收候选 diff，并按角色契约不得读取 Builder worktree；Builder 在 Tester
  提交后可以读测试，但不能修改。串行场景只向 Tester 暴露计划声明的公开前置产物和黑盒面，
  不提供实现 diff。Tester、Reviewer 首次创建都显式使用 `fork_turns="none"`，只接收完成冻结角色
  所需的最小 brief，从而切断父线程 conversation fork；本项目不提供 filesystem ACL，也不提供
  platform attestation 或 context manifest。Git/artifact 基线与写隔离继续由 runtime 机械执行。
- Tester、Reviewer 在后续 iteration 续接原 agent thread，不重新 spawn、清空角色历史或以同名
  新 agent 冒充原上下文。
- 所有非 L1 run 都必须持久化 Tester author `tests_ready` 及其 integration，再由同一 thread 在
  candidate worktree 对集成 HEAD 完成 blackbox `pass`。新 run 使用 schema v2 report 保存每条真实
  execution、存在冻结 case 时的逐例结果、逐维度 observation 与执行前后 HEAD；Tester 另行证明零 residue。即使没有
  冻结 case，也必须至少有一条 `method=command` 的 accepted execution。其他 method 的执行错误保留
  reason，但不计入结论或 dependency scope；全部
  accepted command 的可解析仓库依赖取并集，只有真实解析失败才回退全 tree。既有 active run 继续
  单 command v1，不用合成命令替换真实 provenance。unittest 只有显式 `.py` target 与 discover
  start directory 可以窄化；默认或仅 options 的 discovery 绑定当前目录，bare、dotted 和无 `.py`
  的 slash target 因无法区分 module/package 而 fail closed 到全 tree，不能猜成同名文件。
- `PASS / FAIL / FATAL` 来自真实命令退出状态；模型不能自述通过。显式退出码反转、动态 exit、
  内联 shell control flow 和已知 runner 控制文件弱化会在执行前被拒绝。
- Python Tester 文件的 skip/xfail/flaky/rerun 检测按真实 pytest、unittest、subprocess import binding
  解析 module/from-import alias 与 module-level `pytestmark`；局部定义、参数、赋值和容器造成的
  shadowing 不按同名字符串误判。非 Python 文件继续使用文本门禁，现有常量断言、吞异常和删除测试
  检测保持不变。
- 机器验证在 candidate 的临时干净 worktree 中执行，不允许验证命令改写候选现场。连续两次验证
  同一 candidate 失败会进入 no-progress，同一 failure fingerprint 跨三个 candidate 重现会要求
  架构复核；显式 resume 不重置迭代上限。
- Builder 负责同步项目文档，Reviewer 在同一次审查中执行文档审计；非 L1 Reviewer turn 的
  开始和结束都必须已经绑定当前 Tester integration、机器验证和 blackbox evidence。
- Reviewer custom-agent 是 `pass/findings/blocked` 终态的唯一来源；Builder 的 initial/follow-up
  brief 只传输入和目的，不得重定义终态、增加别名或要求未声明值。非法或缺失终态保持 fail-closed，
  findings 修复后续接原 Reviewer thread。
- Reviewer 通过前目标分支不移动。finalize 在临时 ref/worktree 执行目标仓库 hooks，确认最终
  tree 与已审 candidate 一致后先冻结唯一 final commit；目标 checkout 存在风险路径时保留该
  commit 等待处理，安全后才用 expected-old compare-and-swap 更新目标分支。
- `status` 分开公开 delivery gates、final commit staging 和真实 finalize readiness。tracked/index
  改动始终阻塞目标同步；无关 `.env`、cache、日志等 untracked/ignored 文件会被保留且不阻塞，
  只有与最终 tree 更新路径冲突或 Git 无法证明安全时才要求用户处理。
- machine/blackbox evidence 可按计划冻结的 affects/exempt scope 绑定真实输入 digest；scope 外改动
  只推进 accepted HEAD 并保留 observed HEAD，Reviewer/doc-review 仍对任意 candidate 变化失效。
- `doctor` 只读报告 ledger、worktree、intake、evidence、progress 和 finalize 状态；`recover` 只重放
  已持久化事务，`cleanup` 只删除 terminal run 中 clean 且未漂移的 ledger-owned worktree。未知
  orphan 永不自动 adopt 或删除。
- start 冻结实际运行的 adapter 类型、commit 和 dirty 状态。高信号交付完成后，Builder 将业务项目、
  builder-loop 与外部平台事故按单一 owner 分流；跨边界因果链拆成两个原子事故，经用户授权才写
  项目问题文档或 GitHub issue。工程问题处理后，剩余隐含知识委托 `$memory-review`，不再执行旧版
  五问打分或直接写 memory。
- `$file-github-issue` 在创建时冻结事故快照，在关闭时追加最终根因、人类决策和验收证据，再由当前
  Agent 直接调用 `gh` 创建、补充或关闭；用户的动作指令就是授权，不增加第二次确认，也不引入
  Issue CLI。结案语料可供按需离线影子评测，但影子结果不写回 GitHub。
- finalize 只更新本地目标分支，不自动 push、创建 PR 或合并远端分支。
- 正常修复循环会自动继续；测试目标或 ownership 变化、计划过期、迭代上限、Reviewer
  决策项、agent/target continuity 失败、目标同步 blocker 或 Git 冲突等安全停止会交还用户。

测试目标、ownership 或验收标准一旦需要修订，不能在现有 frozen run 内批准。保持原目标时
续接原 Tester/Reviewer thread；选择修订时先 abandon 当前 run 保留现场，再用 `/plan` 生成更高
`plan_revision` 且携带旧 run id/plan digest 的方案。runtime 会核对旧 run 确已 abandoned 且
revision 单调增加。新方案验证后可使用原生“实施计划”动作交接，或显式调用 `$builder`。达到
迭代上限时遵循同一流程。

计划契约只接受 `schema_version: 3`；旧 v1/v2 计划必须重新规划。`plan-validate` 返回
canonical-v2 identity：只规范换行、唯一受管生命周期 header 和末尾换行，同时单独公开 raw source
digest；start 另存 frozen-file digest。纯 Markdown 文档任务可由 Planner
标记为 L1，并用 `documentation-spec` 冻结规划 HEAD、revision
和精确文档写边界：Builder 只改授权的 `.md`，不启动 Tester，也不要求或伪造机器/E2E 证据；
同一个 Reviewer 仍执行方案、内容与文档政策审计。其他任务必须包含 `unit-test-spec`，继续走
独立 Tester 和机器验证。

## 安装

```bash
cd /path/to/cc-builder-loop-codex
./install.sh
```

确保 `~/.local/bin` 在 `PATH` 中。安装后新开 Codex session，使全局 instructions 和 Skills 重新
发现；随后用 `/hooks` 审查并信任 builder-loop hook。若存在非空
`${CODEX_HOME:-$HOME/.codex}/AGENTS.override.md`，它会遮蔽 `AGENTS.md`，安装器因此拒绝继续；先
合并或移除 override，再重新安装。

安装器只配置 Codex。以下路径中的 Skills、custom agents、CLI、hook 脚本和默认文档政策均为
指向当前 checkout 的符号链接；`hooks.json` 与 `AGENTS.md` 只合并托管注册：

- Skills（Planner、Builder、GitHub Issue 记录、显式 Full Driver v4 实验）→ `~/.agents/skills/`
- custom agents → `${CODEX_HOME:-$HOME/.codex}/agents/`
- CLI → `~/.local/bin/codex-builder-loop`
- lifecycle hook 注册 → `${CODEX_HOME:-$HOME/.codex}/hooks.json`
- Planner 自动加载规则 → `${CODEX_HOME:-$HOME/.codex}/AGENTS.md` 的托管区块
- 默认文档政策 → `${CODEX_HOME:-$HOME/.codex}/builder-loop/doc-policy.md`

checkout 必须保留在安装时的路径；移动或删除前先运行 `./uninstall.sh`，移动后再重新安装。
安装器不会修改 `~/.claude`，因此可以与 `main` 分支上的 Claude Code 版本共存。首次安装或
hook 内容变化后，需要再次检查并信任新 hash。

## 项目配置

Codex adapter 沿用 `.claude/loop.yml` 的文件路径，避免再维护第二份项目配置；runtime 只消费
`pass_cmd` 和 `max_iterations`，Claude Code 版本的其他字段在本分支不生效：

```yaml
pass_cmd:
  - stage: lint
    cmd: ruff check .
    timeout: 60
  - stage: test
    cmd: pytest -q
    timeout: 300
max_iterations: 5
```

非 L1 计划的验证来源必须唯一：若 `spec_head` 存在 `.claude/loop.yml`，计划必须省略
`test_context.runner`；若不存在，则计划必须提供它。`plan-validate --repo <repo>` 与 `start` 共享
同一套只读 preflight，并在 READY JSON 的 `effective_verification_source` 中返回实际来源。

现有 `pass_cmd` 只有满足 Codex runner 安全契约时才能直接复用：至少一个真实 stage；不得恒真、
反转退出码、覆盖 PATH 或包含无法静态解释的内联控制流。repository wrapper 必须在规划时
`spec_head` 已存在，是仓库内普通文件而非 symlink，并写入 `test_context.support_paths`；引用
`~/.claude/...` 等仓库外脚本的旧配置需要先迁为仓库内受保护 wrapper。Makefile、pytest/ruff
配置、package manifest 等已识别控制文件自动进入只读保护面。L1 文档任务不读取也不需要
runner。

`max_iterations` 限制单个 run 的机器验证尝试次数；到达上限后 abandon 会保留角色 worktree，
修订方案必须进入新的 run。最终 squash commit 使用目标仓库配置的 Git 身份，并在临时
ref/worktree 执行该仓库的 commit hooks；最终 tree 复核通过后才以 expected-old CAS 更新目标分支。
内部 Builder/Tester checkpoint 不进入目标分支。

计划只有在用户明确接入 planning-time dirty 文件时才增加：

```markdown
<!-- workspace-intake -->
schema_version: 1
files:
  - path: "src/exact-file.py"
    state_sha256: "<workspace-scan 输出>"
<!-- /workspace-intake -->
```

`unit-test-spec.evidence_scopes` 可把每个 Builder-owned pattern 分到 machine/blackbox 的
`affects` 或 `exempt`；省略即保持全 tree 失效。Tester、runner、support、publication 与实际
blackbox accepted execution 的全部 command 依赖始终强制属于 affects。

稳定 `scripts/codex-builder-loop.py` 入口会在导入 runtime 前禁止当前进程写 bytecode，并把该设置
传给正常子进程，避免只读 CLI 或测试 harness 在调用方 worktree 产生 `__pycache__`。显式
`py_compile` 和绕过稳定入口的任意 Python import 不在此承诺内，runtime 也不会删除既有 residue。

## 开发验证

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/codex-builder-loop.py --help
bash scripts/verify-all.sh
```

按需运行的 Issue 根因与注意力分流实验位于
[experiments/issue-triage/](experiments/issue-triage/README.md)。它不属于实时服务或交付 runtime；固定
样本、真实 Issue 只读 shadow、结案语料边界和运行命令均由该模块维护。

该命令只运行确定性契约 fixtures，不包含真实 Codex child spawn、follow-up、hook continuation
和 sandbox live smoke；发布级实机验收条件见 [docs/known-issues.md](docs/known-issues.md)。

设计原则见 [docs/design-philosophy.md](docs/design-philosophy.md)，运行架构见 [docs/architecture.md](docs/architecture.md)，环境限制见 [docs/known-issues.md](docs/known-issues.md)。
