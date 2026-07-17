# Project Map

- `skills/`：Planner 与 Builder 的 Codex Skills；保持精简，详细契约引用 schema 或脚本帮助。
- `agents/`：Tester 与 Reviewer custom-agent 配置。
- `runtime/`：确定性计划、Git、验证、ownership 和 evidence 实现。
- `scripts/codex-builder-loop.py`：runtime 的稳定 CLI 入口；`codex-builder-loop-config.py` 负责
  安装/卸载时跨 hooks 与 AGENTS 的事务更新。
- `hooks/`：只记录 agent 身份并做完成门禁，不承担循环编排。
- `policies/`：安装时部署的默认文档审计政策。
- `schema/`：runtime ledger 的唯一结构定义。
- `tests/`：不依赖真实模型的契约 fixtures。
- `docs/`：设计哲学、架构和已知环境问题。

# Commands

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/codex-builder-loop.py --help
bash scripts/verify-all.sh
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/builder-loop-planner
python3 "${CODEX_HOME:-$HOME/.codex}/skills/.system/skill-creator/scripts/quick_validate.py" skills/builder
```

# Workflows

- 先定义或修改 CLI/schema 契约，再改 runtime、Skills 和 fixtures。
- runtime ledger 是执行事实唯一来源；Skills、hooks 和 agents 不直接改 JSON。
- 任何候选 HEAD 变化都会使旧 verify、E2E、review 和 doc-review evidence 失效。
- Tester 与 Reviewer 必须续接原 thread；续接失败时停止并保留现场。
- Tester author/integration 与 blackbox 是两个独立 gate；blackbox details 必须绑定 candidate
  worktree、命令、returncode 和前后 HEAD。
- 串行 Tester publication 是独立 gate：精确普通文件经隔离 publication HEAD/manifest 成为
  Tester baseline，发布路径随后不可变；不得把 Builder HEAD 或 candidate diff 直接交给 Tester。
- Reviewer turn 必须在所需 Tester integration、机器验证和 blackbox evidence 已绑定 candidate 后
  启动；过早 review 不能靠事后补证据变成有效审查。
- finalize intent 写入后冻结 run mutation；恢复时重新核对全部 gate，再同步 target 和 cleanup。
- Builder 写代码和文档，Tester 写计划允许的测试，Reviewer 只读审查。
- 修复按 ownership 路由；测试实现问题回到原 Tester，契约变化进入 abandon/new plan。
- Git 冲突只安全停止；v1 不自动仲裁。

# Common Pitfalls

- `.codex/` 和 `.agents/` 在默认 workspace sandbox 中是只读保护路径，不存运行时状态。
- 不把 Codex 最终文本或进程 exit 0 当作 subagent 成功；必须看到真实 child thread 事件。
- 不解析 transcript 定位 run；使用 session id 查询 ledger。
- 不允许 `pass_cmd: []`、恒真/反向验证命令、可被角色改写的 runner 控制文件或未执行 stage
  产生 PASS。
- repository runner wrapper 必须在 `spec_head` 已存在且为仓库内普通文件；拒绝 symlink、仓库外
  target 和 PATH override。
- 不新增第二份计划、测试目标或 evidence 缓存。

# Collaboration

- 保留用户已有 dirty worktree；所有开发在任务分支/worktree 完成。
- 自动提交前运行全部 fixtures 和 Skill validator。
- Markdown 只记录稳定契约、架构和用户说明；运行快照写结构化 evidence。

# References & Skills

- 设计判断：[docs/design-philosophy.md](docs/design-philosophy.md)
- 运行架构：[docs/architecture.md](docs/architecture.md)
- Planner：`$builder-loop-planner`
- 执行：`$builder`
