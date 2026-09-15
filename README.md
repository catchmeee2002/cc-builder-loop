# cc-builder-loop V8

Claude Code 的判据驱动交付闭环。模型说"做完了"不算数——由四类独立证据决定完成，每条证据绑定真实输入，输入一变证据即失效：

| gate | 谁产生 | 绑定的输入 |
|---|---|---|
| machine | runtime 在候选 worktree 跑 `.claude/loop.yml` 的 `pass_cmd` | contract digest、候选 HEAD、测试文件 blob |
| tester | 独立 tester subagent 写测试并给出 proof_spec | 测试文件 blob、mission digest |
| proof | runtime 证明测试有鉴别力（候选全绿 + baseline-red / mutation / reviewed-boundaries） | 候选 HEAD、测试文件、behaviors、proof_spec |
| reviewer | 独立 reviewer subagent 审查 | 候选 HEAD、contract digest、前三项证据的状态与 digest |

四项 fresh pass 后 `bl finalize` 用 `commit-tree` + `update-ref` CAS 把已审 tree 单提交写回目标分支。编排（spawn subagent、SendMessage 续接、AskUserQuestion）全部交给 Claude Code 原生能力；runtime 只做判据与 Git 事务。设计原则见 [docs/design-philosophy.md](docs/design-philosophy.md)。

## 使用

```text
/planner <需求>        → .claude/plans/<date>-<slug>.md（含 builder-loop contract）
/builder <plan 路径>    → bl start → 实现 → checkpoint → machine → tester → proof → reviewer → finalize
```

Builder 只需跟着 `bl status` 的 `next_action` 走；Stop hook 会在 run 未完成时把会话拉回来。需要用户决定的情况（迭代上限、同一失败三次、reviewer blocked、目标分支漂移冲突）都以 AskUserQuestion 呈现。

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
```

`.claude/builder-loop/` 加进 `.gitignore`。pass_cmd 在候选 worktree 内执行，必须能在新 checkout 里跑通。

## 开发

```bash
python3 -m pytest -p no:html -p no:cacheprovider -q tests
```

模块说明与失败路径见 [docs/architecture.md](docs/architecture.md)；仓库规则见 [CLAUDE.md](CLAUDE.md)；历史见 [CHANGELOG.md](CHANGELOG.md)。老版本保留在 `cc-old`（V7.4）与 `codex-new`（Codex Assurance v4）分支。
