---
name: builder-loop
description: "builder-loop V8 机制说明与接入向导：判据驱动的交付闭环（machine / tester / proof / reviewer 四道 gate + finalize CAS）。用户说『配置 loop』『接入 loop』『init loop』时进入接入向导生成 .claude/loop.yml；排障看 `bl doctor`。"
---

# builder-loop V8

模型改完代码不算完成；完成由四类独立证据决定，每条证据绑定产生它的真实输入（candidate HEAD、测试文件 blob、contract digest），输入一变证据即失效。编排（spawn / 续接 / 问用户）交给 Claude Code 原生能力，runtime 只管判据与 Git 事务。设计原则见仓库 `docs/design-philosophy.md`。

## 一次 run 的形状

```
/planner → plan.md（含 contract）
/builder → bl start（候选 worktree + ledger，绑定 session）
   实现 → bl checkpoint → bl machine（pass_cmd）
   → tester subagent（写测试 + proof_spec，hook 记账）→ bl machine（含新测试）
   → bl proof（候选全绿 + baseline-red / mutation / reviewed-boundaries）
   → reviewer subagent（hook 记账；findings → 修 → 重验 → SendMessage 复审）
   → bl finalize（commit-tree + update-ref CAS 写回目标分支，删 worktree）
```

Stop hook 在 run 未终态时阻止会话结束并给出 `next_action`；`waiting_for_user`（AskUserQuestion 挂起）时放行；连续 3 次 Stop 之间 ledger 无进展也放行并提示。

## CLI（`bl`，装在 `~/.claude/bin`）

| 命令 | 作用 | 退出码 |
|---|---|---|
| `bl start --plan P --session S [--target B]` | 冻结 contract + loop.yml，建候选 worktree | 0 / 2 配置错 / 3 session 已有 run |
| `bl status` | readiness（四项状态 + next_action + blockers）、dirty、agents | 0 |
| `bl checkpoint --role builder\|tester [-m]` | 按写边界提交候选改动；越界 → 拒绝并列路径 | 0 / 1 |
| `bl machine` | 在候选 worktree 顺序跑 pass_cmd | 0 PASS / 1 FAIL / 2 FATAL / 3 blocker |
| `bl proof [--spec-file]` | 证明测试有鉴别力 | 0 / 1 / 3 |
| `bl finalize [-m] [--run-commit-hook]` | CAS 写回目标分支 | 0 / 1（未就绪 / DRIFT / dirty 重叠） |
| `bl rebase` | 目标分支前进后把候选 rebase 上去 | 0 / 1 冲突 |
| `bl contract validate --plan P` / `revise --plan P [--authorize]` | 校验 / 同 run 改 contract | 0 / 3 需授权 |
| `bl abandon --reason R` | 终止，保留 worktree 供查看 | 0 |
| `bl evidence show` / `bl runs` / `bl doctor` | 诊断 | 0 |

定位 run：`--session <id>`（hook 与 /builder 用）或 `--run <run_id>`；仓库只有一个活跃 run 时可省略。

所有 evidence、agent 身份、intent 都在 `.claude/builder-loop/runs/<run_id>/ledger.json`，唯一写入者是 `bl`。tester / reviewer 的结论由 SubagentStop hook 从 `last_assistant_message` 的 `BUILDER_LOOP_RESULT:` 行解析后写入，只认 SubagentStart 登记过的 agent_id。

## 项目配置 `.claude/loop.yml`

```yaml
pass_cmd:                      # 顺序执行，任一非 0 即 FAIL；至少 1 个
  - stage: lint
    cmd: ruff check src
    timeout: 60
  - stage: test
    cmd: python3 -m pytest -q tests
    timeout: 300
max_iterations: 5              # machine 尝试上限，到达即交还用户
worktree:
  root: ../builder-loop-worktrees/<repo>   # 可选，默认仓库同级目录
```

pass_cmd 在候选 worktree 内执行（需 clean、HEAD 与 ledger 一致），命令必须能在一个新 checkout 里跑通（editable install 指向主仓的 venv 会让测试跑到主仓代码——用 `PYTHONPATH=.` 或 `uv run` 之类避免）。`.claude/builder-loop/` 需在 `.gitignore`。

## 接入向导（用户说「配置 loop」时）

1. `bash <skill 目录>/scripts/probe-project-stack.sh <项目根>` → language / test_framework / lint_tools / dirs / recommended_pass_cmd
2. AskUserQuestion：通过条件（推荐全套 / 只测试 / 自定）、上限轮数（3 / 5 / 10）
3. `echo '<choice JSON>' | bash <skill 目录>/scripts/init-loop-config.sh <项目根>`（choice：`pass_cmd`[{stage,cmd,timeout}] / `max_iterations` / `layout`）
4. smoke：在项目根手动跑一遍 pass_cmd 里的命令确认能过
5. 汇报配置路径与 stage 数

## 排障

`bl doctor` 报 hook 注册、断链、孤儿 session 指针；`bl status --run <id>` 看 evidence 状态；日志在 `.claude/builder-loop/runs/<id>/logs/`。机制问题到 cc-builder-loop 仓开 issue（`readlink ~/.claude/skills/builder-loop` 上两层取 remote）。
