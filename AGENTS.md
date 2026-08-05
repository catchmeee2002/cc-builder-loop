# Project Map

- `skills/`：Planner、Builder 与 GitHub Issue 记录的 Codex Skills；保持精简，详细契约引用 schema
  或脚本帮助。
- `agents/`：Native Driver 共用的 Builder、Tester 与 Reviewer role 配置。
- `runtime/`：legacy v2/v3 的确定性计划、workspace snapshot、Git 收尾、验证、ownership、evidence
  和诊断实现；`runtime/codex_builder_loop/assurance_v4/` 是 v4 Core 与 Driver 派生层，
  `runtime/codex_builder_loop/native_driver/` 是 App Server transport 和 Full Driver 原生协调器。
- `scripts/codex-builder-loop.py`：runtime 的稳定 CLI 入口；`codex-builder-loop-config.py` 负责
  安装/卸载时跨 hooks 与 AGENTS 的事务更新。
- `hooks/`：只记录 agent 身份并做完成门禁，不承担循环编排。
- `policies/`：安装时部署的默认文档审计政策。
- `experiments/agent-behavior/`：离线角色行为场景、指令变体与机械评分；不进入交付 ledger。
- `experiments/issue-triage/`：按需或由本地 cron 运行的只读 GitHub Issue 分流实验；不进入 runtime、
  ledger 或交付门禁。
- `experiments/assurance-v4-replay/`：历史高频恢复场景的离线分类回放；不读取或迁移旧 ledger。
- `schema/`：runtime ledger 的唯一结构定义。
- `tests/`：不依赖真实模型的契约 fixtures。
- `docs/`：设计哲学、架构和已知环境问题。

# Commands

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/codex-builder-loop.py --help
python3 scripts/codex-builder-loop.py native-driver --help
bash scripts/verify-all.sh
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/builder-loop-planner
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/builder
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/file-github-issue
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/full-driver-v4-experiment
python3 -m unittest discover -s experiments/issue-triage/tests -p 'test_*.py'
python3 experiments/assurance-v4-replay/runner.py
```

# Workflows

- 先定义或修改 CLI/schema 契约，再改 runtime、Skills 和 fixtures。
- v4 的 Mission、Authority、Assurance、Execution 分别计算 digest；Core 只记录交付与外部 turn 恢复
  事实，`driver.py` 才能决定下一步角色动作。dispatch intent 只绑定正在发生的单一副作用，不能缓存
  后续动作、correction loop 或第二份 evidence。
- Native Driver 默认承载 Full Driver；App Server capability 失败只能在创建 run 前回退现有 Full
  Driver Skill。run 创建后按 `driver_runtime.kind` 单写，禁止接管或双控制器 mutation。
- run 创建后的未处理 FATAL 必须先经 `record-driver-failure` 写入 ledger，再恢复 finalize/deployment
  副作用；环境安全后才进入 `failed`。failed 不 resume、不 supersede、不 abandon，cleanup 只接受与
  冻结 failure observation 一致的 clean worktree。
- runtime ledger 是执行事实唯一来源；Skills、hooks 和 agents 不直接改 JSON。
- Assurance v4 的已授权 plan decision 先用 `validate-decision` 绑定同 session、problem、action、facet
  digest 和唯一完整 replacement contract，再以相同 binding 调用 `update-facet` 或 `revise-mission`，
  原子关闭 problem 并 resume 同一 active run；普通执行信息变化不得先 abandon 或自动升级为新 run。确需 successor 时，source 必须在新
  contract 验证和 `start` 持久化 target 前保持 active，由 `start` 创建 target 后封为 superseded。
  `abandon` 只表示用户取消且没有 successor；abandoned、superseded、failed、finalized source 不恢复 continuity。
  legacy v2/v3 保持既有 abandoned-source revision 契约。
- 候选变化后按冻结 scope digest 处理 machine/blackbox evidence；未声明 scope 时全 tree 失效。
  Reviewer 与 doc-review 对任何 candidate 变化都失效。
- target dirty 默认隔离；只有用户显式授权的 exact path/state digest 才能经 workspace snapshot 进入
  Builder。不得手工 stash、复制或清理 target 来绕过 intake contract。
- Tester 与 Reviewer 必须续接原 thread；续接失败时停止并保留现场。
- Tester author/integration 与 blackbox 是两个独立 gate；blackbox details 必须绑定 candidate
  worktree、命令、returncode 和前后 HEAD。
- 串行 Tester publication 是独立 gate：路径集合冻结，每个 generation 的 HEAD/blob/manifest 不可变；
  prerequisite 修复只能经 recomposition 生成下一 generation，并让 Tester/evidence 对新 candidate 重绑。
- `run_before_full_suite` command 可形成 focused preflight；可选 Reviewer preflight 只做早期语义审计，
  不满足最终 gate。最终 Reviewer 仍须在 Tester/proof/machine/blackbox 已绑定 candidate 后启动。
- 外部环境只允许 ledger 中一个 run 持有 lease；同 run Revision 或显式 supersedes 转移必须重新 probe，
  不继承旧角色/evidence。finalize、abandon 和 cleanup 前必须释放 lease 并确认恢复。
- finalize intent 写入后冻结 run mutation；恢复时重新核对全部 gate，再同步 target 和 cleanup。
- no-progress/architecture-review 只能在用户确认后用 `resume --reason` 解除；不得重置 attempt 上限。
- 诊断先用只读 `doctor`；`recover` 只重放 persisted intent，`cleanup` 只处理未漂移的 terminal
  worktree，未知 orphan 不 adopt、不删除。
- 交付后工程事故只能归属 current project、builder-loop 或 external platform；跨边界因果链必须拆成
  两个原子事故。先经用户授权写对应问题容器，再把不可工程固化的剩余知识委托 `$memory-review`；
  memory 不得替代 issue、代码、测试、契约或项目文档。
- Builder 写代码和文档，Tester 写计划允许的测试，Reviewer 只读审查。
- 修复按 ownership 路由；测试实现问题回到原 Tester，目标、ownership 或验收标准变化按上述版本化
  lifecycle 处理，不用无条件 abandon 覆盖 Assurance v4。
- target drift 与 publication refresh 共用持久化 recomposition intent；Builder/Tester 冲突回原 thread
  的隔离 staging worktree，target 再推进则从最新 HEAD 重启。未知 residue、判据/授权变化才停止。
- machine/preflight failure 先持久化结构化结果与 signature，再续接原 Tester thread 只读归因；不得把
  Agent 自述或重跑日志直接变成 evidence。
- open problem owner 必须穷举：builder/tester 回原角色，plan/external_platform/builder_loop/
  current_project 进入各自 NEEDS_USER decision；后四类不得默认回落到 builder_fix。
- Tester/Reviewer 报出需要修复或决定的问题后，先经 `record-problems` 逐条写入 ledger；Assurance v4
  successor 用 source ledger 的逐条 disposition 连续交接，legacy v2/v3 继续由 abandon 封存问题清单并由
  更高 revision 的 `prior-problems` 逐条处理。旧 ledger 无清单时先显式补录。
- runtime 不做跨 Mission 智能 merge；只允许上述单 Mission、ownership 受限的冲突修复与重组事务。
- retrospective 的完整 `required_block` 留作结构化审计，Stop hook 优先校验精简
  `required_user_block`；READY 不绕过 active run，NEEDS_USER 可在精简块已展示后保留 active 现场等用户。

# Common Pitfalls

- `.codex/` 和 `.agents/` 在默认 workspace sandbox 中是只读保护路径，不存运行时状态。
- 不把 Codex 最终文本或进程 exit 0 当作 subagent 成功；必须看到真实 child thread 事件。
- 不解析 transcript 定位 run；使用 session id 查询 ledger。
- 不允许 `pass_cmd: []`、恒真/反向验证命令、可被角色改写的 runner 控制文件或未执行 stage
  产生 PASS。
- 独立 Tester 源码经 thread、ownership、Git/source manifest、integration 和 Reviewer 审查后属于
  proof 的可信输入；外部 supervisor 防环境、输出和空壳误判，不是任意恶意 Python 的安全沙箱。
- repository runner wrapper 必须在 `spec_head` 已存在且为仓库内普通文件；拒绝 symlink、仓库外
  target 和 PATH override。
- 不新增第二份计划、测试目标或 evidence 缓存。
- 事故记录只写触发场景、现场过程、现象、已确认事实、根因状态和复现条件；不写建议或方案，并从
  ledger.runtime_identity 标明 Claude Code/Codex adapter 及实际 commit。
- 迁移或删除成熟能力时更新 legacy parity corpus，逐项标明 covered、rescue 或 retired；不能只
  用新实现的自洽 fixture 证明迁移成功。

# Collaboration

- 保留用户已有 dirty worktree；所有开发在任务分支/worktree 完成。
- 自动提交前运行全部 fixtures 和 Skill validator。
- Markdown 只记录稳定契约、架构和用户说明；运行快照写结构化 evidence。

# References & Skills

- 设计判断：[docs/design-philosophy.md](docs/design-philosophy.md)
- 运行架构：[docs/architecture.md](docs/architecture.md)
- Planner：`$builder-loop-planner`
- 执行：`$builder`
- 记录 GitHub Issue：`$file-github-issue`
- 离线 Issue 分流实验：[experiments/issue-triage/README.md](experiments/issue-triage/README.md)
