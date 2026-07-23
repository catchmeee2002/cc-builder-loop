# codex-builder-loop

面向 Codex CLI 的独立判据交付闭环。进入 Plan mode 后先选择规划方式：

```text
/plan <需求>
├─ Codex 原生 Plan
└─ Builder-loop Planner → $builder
```

全局托管规则会在每次进入 Plan mode 时通过选项卡询问。选择 Codex 原生 Plan 时不加载本项目
Skill；选择 Builder-loop Planner 时，Planner Skill 把行为、接口、测试目标、写入边界和验收方式
冻结成可校验计划。随后显式调用 `$builder`，由其协调 Builder、独立 Tester、机器验证和
Reviewer；全部门禁通过后，将候选结果收敛为一个语义提交。

## 核心行为

- Builder-loop Planner 在输出前运行带仓库上下文的确定性 plan validator；缺少规划 HEAD/revision、行为边界
  与不变量、mock 策略、角色写边界、串行公开前置产物或有效 checklist 的计划不能进入执行，
  实际生效的验证配置不合法时也不会返回 `READY`。
- 主仓 dirty 默认留在原处且不进入 run。任务确实依赖现有改动时，Planner 取得 exact-path 授权并用
  `workspace-scan` 冻结 state digest；`start` 合成不可变 snapshot 给 Builder，主仓和全局 stash
  全程不动。finalize 只在授权路径未漂移时消费 snapshot。
- `parallel_ready=true` 时 Builder 与 Tester 从同一个 `spec_head` 并行。为 `false` 时，Builder
  先发布计划声明的精确公开文件；runtime 从 `spec_head` 合成隔离 publication HEAD/manifest，
  将其设为 Tester 基线，并冻结这些文件后再接受 Tester author turn。
- Tester 首次写测不接收候选 diff，并按角色契约不得读取 Builder worktree；Builder 在 Tester
  提交后可以读测试，但不能修改。串行场景只向 Tester 暴露计划声明的公开前置产物和黑盒面，
  不提供实现 diff。当前读隔离依赖独立 thread 与角色契约，写隔离由 runtime 机械执行。
- Tester、Reviewer 在后续 iteration 续接原 agent thread，不以同名新 agent 冒充原上下文。
- 所有非 L1 run 都必须持久化 Tester author `tests_ready` 及其 integration，再由同一 thread 在
  candidate worktree 对集成 HEAD 完成 blackbox `pass`；可重放命令、数值 returncode、执行前后
  HEAD 与零 tracked/untracked/ignored residue 随 E2E evidence 绑定同一 candidate。
- `PASS / FAIL / FATAL` 来自真实命令退出状态；模型不能自述通过。显式退出码反转、动态 exit、
  内联 shell control flow 和已知 runner 控制文件弱化会在执行前被拒绝。
- 机器验证在 candidate 的临时干净 worktree 中执行，不允许验证命令改写候选现场。连续两次验证
  同一 candidate 失败会进入 no-progress，同一 failure fingerprint 跨三个 candidate 重现会要求
  架构复核；显式 resume 不重置迭代上限。
- Builder 负责同步项目文档，Reviewer 在同一次审查中执行文档审计；非 L1 Reviewer turn 的
  开始和结束都必须已经绑定当前 Tester integration、机器验证和 blackbox evidence。
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
- finalize 只更新本地目标分支，不自动 push、创建 PR 或合并远端分支。
- 正常修复循环会自动继续；测试目标或 ownership 变化、计划过期、迭代上限、Reviewer
  决策项、agent/target continuity 失败、目标同步 blocker 或 Git 冲突等安全停止会交还用户。

测试目标、ownership 或验收标准一旦需要修订，不能在现有 frozen run 内批准。保持原目标时
续接原 Tester/Reviewer thread；选择修订时先 abandon 当前 run 保留现场，再用 `/plan` 生成更高
`plan_revision` 且携带旧 run id/plan digest 的方案，并重新调用 `$builder`。runtime 会核对旧 run
确已 abandoned 且 revision 单调增加。达到迭代上限时遵循同一流程。

计划契约只接受 `schema_version: 2`；旧 v1 计划必须重新规划。纯 Markdown 文档任务可由 Planner
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

- Skills → `~/.agents/skills/`
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
blackbox command 依赖始终强制属于 affects。

## 开发验证

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/codex-builder-loop.py --help
bash scripts/verify-all.sh
```

该命令只运行确定性契约 fixtures，不包含真实 Codex child spawn、follow-up、hook continuation
和 sandbox live smoke；发布级实机验收条件见 [docs/known-issues.md](docs/known-issues.md)。

设计原则见 [docs/design-philosophy.md](docs/design-philosophy.md)，运行架构见 [docs/architecture.md](docs/architecture.md)，环境限制见 [docs/known-issues.md](docs/known-issues.md)。
