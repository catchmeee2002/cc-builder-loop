# cc-builder-loop — 判据驱动的交付闭环（Claude Code 原生）

模型改完代码不算完成；完成由四类独立证据决定：machine（pass_cmd）/ tester（独立 subagent 在冻结基线上盲写测试，与 builder 并行）/ proof（证明测试有鉴别力）/ reviewer（独立 subagent 审查）。每条证据绑定产生它的真实输入 digest，输入一变证据即失效；run 结束后还有一道复盘闸门。编排交给 Claude Code 原生能力（subagent / SendMessage / hooks），本仓 runtime 只管判据与 Git 事务。

## 设计哲学

所有设计决策的判据见 [`docs/design-philosophy.md`](docs/design-philosophy.md)（唯一来源，此处不复制）。动逻辑前先对照：新增状态是否服务判据 / 证据 / Git 事务（原则五）；是否在堆输出特判而不是改输入条件（原则四）。

## 产品线

本仓库承载两条**并行**的产品线：共享同一份设计哲学，不共享代码，互不合并。

| 产品线 | 宿主 | 分支 |
|---|---|---|
| CC 版（本文件描述的这一条） | Claude Code | `cc/main`（仓库默认分支，自 V7.4 线性演进） |
| Codex 版 | Codex | `codex/main` |

- 两条线都是在役产品，谁也不是谁的旧版或上游。
- 一条线的做法要进另一条线：先审计、再逐项评估，只取判据 / 证据 / 事务层面的东西，编排交给各自宿主的原生能力（原则九）——不做无脑移植。
- 两条线**不共用工作树**：各自的安装软链（CC 的 `~/.claude/*`、Codex 的 `~/.agents/skills` 与 `~/.codex/*`）指向各自的检出。共用一个工作树时，切到哪条线的分支，另一条线的安装就会悄悄加载错的代码。
- 命名一律带产品线前缀：分支 `cc/<主题>` / `codex/<主题>`（主干为 `cc/main` / `codex/main`）；tag `cc-vX.Y.Z` / `codex-vX.Y.Z`（无前缀的老 tag：`v7.x` 属 CC 版，`v0.x` 属 Codex 版）；issue 必带 `line:cc` 或 `line:codex` 标签。

## Project Map

| 路径 | 职责 |
|---|---|
| `runtime/builder_loop/` | Python runtime（stdlib only）。`ledger` 单写者 + events；`contract` 三面 digest 与**唯一的写边界判定** `write_rejection`；`brief` **角色事实的唯一来源**；`evidence` 投影 / readiness / 角色与门禁在跑的派生；`machine`（含 preflight）/ `proof` / `finalize` 三个判据事务；`run` 生命周期（含 integrate、resume、hold）；`retro` 复盘与 cleanup；`hooks` 六个 CC hook handler；`cli` 入口 |
| `hooks/bl-hook.sh` | 唯一 hook 入口；纯 bash 快速路径——session 没绑定 run 就不起 python |
| `bin/bl` | CLI 入口（install 软链到 `~/.claude/bin/bl`） |
| `agents/{tester,reviewer}.md` | 角色 subagent 定义；运行时上下文由 SubagentStart hook 注入，文件本身只写硬约束 |
| `skills/builder` `skills/planner` `skills/builder-loop` | `/builder` `/planner` 入口与机制说明 + 接入向导 |
| `schema/` | contract / proof-spec / agent-result / ledger 的 JSON schema（文档级，runtime 用手写校验） |
| `tests/` | pytest；`conftest.py` 提供临时 git 仓、CLI、hook 三个 fixture |
| `docs/architecture.md` | 工程推导：模块边界、evidence 投影、hook 接线、失败路径 |
| `docs/doc-policy.md` | 全局文档维护原则（install 软链到 `~/.claude/doc-policy.md`，builder / reviewer / planner 按它判断文档改动） |

## Commands

```bash
python3 -m pytest -p no:html -p no:cacheprovider -q -n 16 tests   # 全量测试，并行需 pytest-xdist（串行要 17 分钟）；本机 pytest-html 插件损坏，必须 -p no:html
./install.sh                                                # 软链 + 注册 10 条 hook（幂等，会清旧版断链）
bl doctor                                                   # hook / 软链 / 孤儿 session 诊断
```

## Workflows

- 改 runtime 逻辑 → 对应 `tests/test_*.py` 加 case；evidence 投影、readiness 规则、hook 接线变化必须同步 `docs/architecture.md` 的表。
- 新增 ledger 字段前先过原则五：能从 git / events / evidence 派生的不落盘（`integrated_head`、`running` 都是被这条否掉的）。
- 涉及路径归属或保护的判断一律调 `contract.write_rejection`，不要在调用点另写一份。
- 要让 tester / reviewer 知道的事实一律进 `brief.build()`，不在 prompt、hook 或 builder 的消息里另写一份——角色只信 `bl brief`（注入的上下文续接时不送达，SendMessage 的正文角色无从验真）。
- 改 hook 输入字段假设 → 先用真实会话探针确认（临时 hook 把 stdin 落盘），CC 版本不同字段会变。
- 改 agent 输出契约（`BUILDER_LOOP_RESULT`）→ 同时改 `hooks.py` 的 `*_RESULT_FORMAT`、`agents/*.md`、`schema/agent-result.schema.json`。
- 本仓自身用 V8 交付（`.claude/loop.yml` 已配）；dogfood 需新开 session 让 skills 重新发现。

## Common Pitfalls

- hook stdin 的 `cwd` 跟随 Bash 最后 `cd` 的目录，不能用它定位仓库——一律走 session 绑定。
- git init 模板带 commit-msg 门禁：fixture 仓库要 `rm -rf .git/hooks` 或用合规 message。
- worktree 在仓库同级目录；pass_cmd / proof_runner 里的相对路径以 worktree 为根，主仓的 venv 用 `{main_repo}` 引。
- hook matcher 不是身份门禁：`agent_type` 为空的内部 agent 也会被 `tester|reviewer` 放进来，handler 内必须复核。
- 改角色结果登记时两种环境都要覆盖：`SubagentHandback` 按环境开关、不由 CC 版本号决定——有它时只认 handback（最后一条消息常是收尾句），没有时 SubagentStop 解析最后一条消息兜底。
- readiness 靠事件时间戳的字符串比较判先后（微秒精度、UTC）；构造测试数据时别手写秒级时间戳。
- pytest 把 `test*` 开头的模块级函数都当用例收集：测试辅助函数别叫 `tester_xxx`。

## Collaboration

commit 格式 `type(scope): [cr_id_skip] Desc`；finalize 默认绕过 commit hook（commit-tree），message 由调用方给。机制缺陷用 `file-issue` skill 提到本仓；从 Codex 版吸收什么、不吸收什么及理由记在 issue #233。

## References

- 用户入口与安装：[`README.md`](README.md)
- 版本历史：[`CHANGELOG.md`](CHANGELOG.md)
- CC 版上一代（V7.4 bash/prompt 版）：tag `v7.4`
- Codex 版：`codex/main` 分支（Codex Assurance v4）
