# cc-builder-loop V8

Claude Code 的判据驱动交付闭环。模型说"做完了"不算数——由四类独立证据决定完成，每条证据绑定真实输入，输入一变证据即失效：

| gate | 谁产生 | 绑定的输入 |
|---|---|---|
| machine | runtime 在候选 worktree 跑 `.claude/loop.yml` 的 `pass_cmd` | contract digest、候选 HEAD |
| tester | 独立 tester subagent 在 run 起点的冻结基线上**盲写测试**（看不到实现，与 builder 并行） | tester 分支上的测试文件 blob、mission digest |
| proof | runtime 证明测试有鉴别力：候选逐用例全绿 + baseline-red / mutation / reviewed-boundaries；命令来自冻结的 `proof_runner`，结果读 junit | 候选 HEAD、测试文件、behaviors、proof_spec、assurance digest |
| reviewer | 独立 reviewer subagent 审查，每条问题标明该谁修 | 候选 HEAD、contract digest、前三项证据的状态与 digest |

tester 看不到实现是刻意的：看着实现写的测试只能证明"实现等于实现"。四项 fresh pass 后 `bl finalize` 用 `commit-tree` + `update-ref` CAS 把已审 tree 单提交写回目标分支；之后还要过一道**复盘闸门**——runtime 从 ledger 派生确定性信号，逐条给出去向（立项 / 不是事故）写进 ledger，session 才解绑。编排（spawn subagent、SendMessage 续接、AskUserQuestion）全部交给 Claude Code 原生能力；runtime 只做判据与 Git 事务。设计原则见 [docs/design-philosophy.md](docs/design-philosophy.md)。

## 使用

```text
/planner <需求>        → .claude/plans/<date>-<slug>.md（含 builder-loop contract）
/builder <plan 路径>    → bl start →（tester 后台盲写 ∥ builder 实现）→ integrate → machine
                          → tester 补 mutation patch → proof → reviewer → finalize → 复盘
```

Builder 只需跟着 `bl status` 的 `next_action` 走；Stop hook 会在 run 未完成时把会话拉回来，等用户或等后台 subagent 时放行。迭代上限与"同一失败三次"是真的停止点，续跑需要一次被记录的用户决定（`bl resume`）；reviewer 要求改契约、目标分支漂移冲突同样以 AskUserQuestion 交还用户。

## 安装

```bash
git clone <repo> && cd cc-builder-loop && ./install.sh
export PATH="$HOME/.claude/bin:$PATH"
```

安装器把 `agents/`、`skills/{builder,planner,builder-loop}`、`bin/bl` 软链进 `~/.claude`，在 `settings.json` 注册 8 条 hook（Stop / SubagentStart / SubagentStop / PreToolUse×3 / PostToolUse / UserPromptSubmit），并清理旧版本留下的断链与 hook。幂等；`./uninstall.sh` 逆操作。不触碰 `~/.agents`（可与 Codex 版共存）。安装后新开 Claude Code session。

## 项目接入

项目根放 `.claude/loop.yml`（或对 Claude 说「配置 loop」走向导）：

```yaml
pass_cmd:
  - stage: test
    cmd: python3 -m pytest -q tests
    timeout: 300
max_iterations: 5
proof_runner:                 # 可选；proof 用的测试命令前缀，缺省 python3 -m pytest
  framework: pytest
  cmd: "{main_repo}/.venv/bin/python -m pytest"
```

`.claude/builder-loop/` 加进 `.gitignore`。pass_cmd 与 proof 都在 worktree 内执行，必须能在新 checkout 里跑通；venv 在主仓就用 `{main_repo}` 引过去。

## 开发

```bash
python3 -m pytest -p no:html -p no:cacheprovider -q tests
```

模块说明与失败路径见 [docs/architecture.md](docs/architecture.md)；仓库规则见 [CLAUDE.md](CLAUDE.md)；历史见 [CHANGELOG.md](CHANGELOG.md)。本仓承载两条并行产品线：本页是 CC 版（`cc/main`）；Codex 版在 `codex-new` 分支。CC 版上一代 V7.4 见 tag `v7.4`。
