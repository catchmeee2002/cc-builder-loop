---
name: builder-loop
description: "builder-loop 机制说明与接入向导：判据驱动的交付闭环（machine / tester / proof / reviewer 四道 gate + finalize CAS + 复盘闸门）。用户说『配置 loop』『接入 loop』『init loop』时进入接入向导生成 .claude/loop.yml；排障看 `bl doctor`。"
---

# builder-loop

模型改完代码不算完成；完成由四类独立证据决定，每条证据绑定产生它的真实输入（候选 HEAD、测试文件 blob、contract digest），输入一变证据即失效。编排（spawn / 续接 / 问用户）交给 Claude Code 原生能力，runtime 只管判据与 Git 事务。设计原则见仓库 `docs/design-philosophy.md`。

## 一次 run 的形状

```
/planner → plan.md（含 contract）
/builder → bl start（候选 worktree + tester worktree + ledger，绑定 session）
   ├─ tester（后台）：在冻结基线上只看 contract 盲写测试 ─┐   两边并行，
   └─ builder：在候选 worktree 写实现 → bl checkpoint ──────┤   tester 看不到实现
   bl integrate（把 tester 的测试按路径叠进候选）◀──────────┘
   → bl machine（pass_cmd）
   → 续接 tester 补 mutation patch（此时实现已可读）
   → bl proof（候选逐用例全绿 + baseline-red / mutation / reviewed-boundaries）
   → reviewer（findings 带 owner：builder / tester / contract）
   → bl finalize（commit-tree + update-ref CAS 写回目标分支，删两个 worktree）
   → bl retro signals → bl retro record（复盘硬闸门，之后 session 才解绑）
```

Stop hook：run 未终态 → 拦住并给出 `next_action`；等待用户（AskUserQuestion 挂起）或等待在跑的 subagent / 门禁（`awaiting_*`）→ 放行；终态但未复盘 → 拦住；连续 3 次 Stop 之间 ledger 无进展 → 放行并提示。

## CLI（`bl`，装在 `~/.claude/bin`）

| 命令 | 作用 | 退出码 |
|---|---|---|
| `bl start --plan P --session S [--target B]` | 冻结 contract + loop.yml，建候选与 tester worktree | 0 / 2 配置错 / 3 已有 run 或上个 run 未复盘 |
| `bl status` | readiness（四项状态 + next_action + blockers）、dirty、agents、谁在跑 | 0 |
| `bl checkpoint --role builder\|tester [--dry-run] [-m]` | 按角色提交各自 worktree 的改动；越界拒绝并列路径；`--dry-run` 只分类 | 0 / 1 |
| `bl integrate` | tester 分支的测试按路径叠进候选（幂等） | 0 / 1 |
| `bl machine` | 在候选 worktree 顺序跑 pass_cmd | 0 PASS / 1 FAIL / 2 FATAL / 3 上限或无进展 |
| `bl proof` | 证明测试有鉴别力 | 0 / 1 / 3 |
| `bl preflight` | 在 run 起点跑一遍 pass_cmd，标出本来就红的 stage（后台跑，只记 event 不动判据） | 0 |
| `bl brief --role tester\|reviewer [--json]` | 角色视角的当前事实与待办；角色自取，是它的唯一来源 | 0 |
| `bl resume --reason R` | 用户授权后解除上限 / 无进展 / proof 反复失败（blocker 之后必须有过用户输入） | 0 / 1 / 3 |
| `bl finalize [-m] [--run-commit-hook]` | CAS 写回目标分支 | 0 / 1（未就绪 / DRIFT / dirty 重叠） |
| `bl rebase` | 目标分支前进后把候选 rebase 上去；漂移触及 tester 改过的文件时 tester 分支也跟着 rebase（`tester_rebase.status`，冲突交 tester 解） | 0 / 1 冲突（候选或 tester 任一） |
| `bl contract validate --plan P [--check-repo]` / `revise --plan P [--authorize]` | 校验 / 同 run 改 contract | 0 / 1 / 3 需授权 |
| `bl abandon --reason R` | 终止，保留 worktree；仍需复盘 | 0 |
| `bl retro signals` / `bl retro record --file F \| --no-incident` | 终态复盘 | 0 / 1 |
| `bl cleanup [--run R]` | 回收已复盘的 abandoned run 的 worktree（clean 且 HEAD 未漂移才删） | 0 |
| `bl evidence show` / `bl runs` / `bl doctor` | 诊断 | 0 |

定位 run：`--session <id>` 或 `--run <run_id>`（写在子命令前后都行）；仓库只有一个活跃 run 时可省略。

事实都在 `.claude/builder-loop/runs/<run_id>/ledger.json`，唯一写入者是 `bl`。tester / reviewer 的结论从送达调用方的报告里的 `BUILDER_LOOP_RESULT:` 行解析后写入：本轮有 SubagentHandback 就由 PostToolUse(SubagentHandback) hook 读 handback message，没有（该环境没开 handback）就由 SubagentStop hook 读 `last_assistant_message`，只认 SubagentStart 登记过的 agent_id。runtime 的内部提交不跑目标仓库的 git hooks；只有 `finalize --run-commit-hook` 那一次交付提交会跑。

## 项目配置 `.claude/loop.yml`

```yaml
pass_cmd:                      # 顺序执行，任一非 0 即 FAIL；至少 1 个
  - stage: lint
    cmd: ruff check src
    timeout: 60
  - stage: test
    cmd: python3 -m pytest -q tests
    timeout: 300
max_iterations: 5              # machine 失败上限（通过的重验不计）；到达即停，续跑需用户授权
proof_runner:                  # proof 用的测试命令前缀；runtime 在后面拼 test id
  framework: pytest            # pytest（读 junit 逐用例判定）| generic（只看退出码，只能做 mutation）
  cmd: "{main_repo}/.venv/bin/python -m pytest"   # 缺省 python3 -m pytest；{main_repo} 展开为主仓路径
evidence_neutral_paths:        # 可选，缺省为空 = 候选任何改动都让 machine / proof 失效
  - docs/**                    # 判据读不到的路径；只改它们不重跑 machine / proof（reviewer 照常复审）
worktree:
  root: ../builder-loop-worktrees/<repo>          # 可选，默认仓库同级目录
```

pass_cmd 与 proof 都在 worktree 内执行，命令必须能在一个新 checkout 里跑通：venv 在主仓就用 `{main_repo}` 引过去；依赖不在清单里就把 `uv run --with-requirements … python -m pytest` 之类写进 `proof_runner.cmd`。它会被冻结进 contract 的 assurance 面，run 中途改动需要用户授权。`.claude/builder-loop/` 需在 `.gitignore`。

`evidence_neutral_paths` 声明的是「这个项目的 pass_cmd 与 proof_runner 读不到哪些路径」。写宽了等于让判据失效，所以只写确实不被任何测试读取的路径——有测试读 `docs/` 下的文件（快照测试、文档 lint）就不能把它列进去。它同样冻结进 assurance 面并计入 digest，run 中途改动需要用户授权。不确定就别写：缺省行为是候选任何改动都重验。

## 接入向导（用户说「配置 loop」时）

1. `bash <skill 目录>/scripts/probe-project-stack.sh <项目根>` → language / test_framework / lint_tools / dirs / recommended_pass_cmd
2. AskUserQuestion：通过条件（推荐全套 / 只测试 / 自定）、上限轮数（3 / 5 / 10）、测试怎么跑（系统 python / 项目 venv / uv）
3. `echo '<choice JSON>' | bash <skill 目录>/scripts/init-loop-config.sh <项目根>`（choice：`pass_cmd`[{stage,cmd,timeout}] / `max_iterations` / `layout`），需要时手工补 `proof_runner`
4. smoke：在项目根手动跑一遍 pass_cmd 与 `proof_runner.cmd --collect-only` 确认能过
5. 汇报配置路径与 stage 数

## 排障

`bl doctor`：hook 注册、断链、孤儿 session 指针、已结束未复盘的 run、旧版 ledger。`bl status --run <id>` 看 evidence 与谁在跑；日志在 `.claude/builder-loop/runs/<id>/logs/`；hook 调用轨迹在 `~/.claude/builder-loop/hook-trace.jsonl`。机制问题用 `file-issue` skill 提到 cc-builder-loop 仓（`readlink ~/.claude/skills/builder-loop` 上两层取 remote）。
