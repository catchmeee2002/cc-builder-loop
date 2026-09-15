# cc-builder-loop V8 — 判据驱动的交付闭环（Claude Code 原生）

模型改完代码不算完成；完成由四类独立证据决定：machine（pass_cmd）/ tester（独立 subagent 写测试）/ proof（证明测试有鉴别力）/ reviewer（独立 subagent 审查）。每条证据绑定产生它的真实输入 digest，输入一变证据即失效。编排交给 Claude Code 原生能力（subagent / SendMessage / hooks），本仓 runtime 只管判据与 Git 事务。

## 设计哲学

所有设计决策的判据见 [`docs/design-philosophy.md`](docs/design-philosophy.md)（唯一来源，此处不复制）。动逻辑前先对照：新增状态是否服务判据 / 证据 / Git 事务（原则五）；是否在堆输出特判而不是改输入条件（原则四）。

## Project Map

| 路径 | 职责 |
|---|---|
| `runtime/builder_loop/` | Python runtime（stdlib only）。`ledger` 单写者；`contract` 三面 digest；`evidence` 投影与 readiness；`machine` / `proof` / `finalize` 三个判据事务；`hooks` 六个 CC hook handler；`cli` 入口 |
| `hooks/bl-hook.sh` | 唯一 hook 入口，stdin 透传给 `python3 -m builder_loop hook <event>` |
| `bin/bl` | CLI 入口（install 软链到 `~/.claude/bin/bl`） |
| `agents/{tester,reviewer}.md` | 角色 subagent 定义；运行时上下文由 SubagentStart hook 注入，文件本身只写硬约束 |
| `skills/builder` `skills/planner` `skills/builder-loop` | `/builder` `/planner` 入口与机制说明 + 接入向导 |
| `schema/` | contract / proof-spec / agent-result / ledger 的 JSON schema（文档级，runtime 用手写校验） |
| `tests/` | pytest；`conftest.py` 提供临时 git 仓、CLI、hook 三个 fixture |
| `docs/architecture.md` | 工程推导：模块边界、evidence 投影、hook 接线、失败路径 |

## Commands

```bash
python3 -m pytest -p no:html -p no:cacheprovider -q tests   # 全量测试（本机 pytest-html 插件损坏，必须 -p no:html）
./install.sh                                                # 软链 + 注册 8 条 hook（幂等，会清旧版断链）
bl doctor                                                   # hook / 软链 / 孤儿 session 诊断
```

## Workflows

- 改 runtime 逻辑 → 对应 `tests/test_*.py` 加 case；evidence 投影或 readiness 规则变化必须同步 `docs/architecture.md` 的表。
- 改 hook 输入字段假设 → 先用真实会话探针确认（临时 hook 把 stdin 落盘），CC 版本不同字段会变。
- 改 agent 输出契约（`BUILDER_LOOP_RESULT`）→ 同时改 `hooks.py` 的 `*_RESULT_FORMAT`、`agents/*.md`、`schema/agent-result.schema.json`。
- 本仓自身用 V8 交付（`.claude/loop.yml` 已配）；dogfood 需新开 session 让 skills 重新发现。

## Common Pitfalls

- hook stdin 的 `cwd` 跟随 Bash 最后 `cd` 的目录，不能用它定位仓库——一律走 session 绑定。
- git init 模板带 commit-msg 门禁：fixture 仓库要 `rm -rf .git/hooks` 或用合规 message。
- 候选 worktree 在仓库同级目录；pass_cmd 里的相对路径以 worktree 为根。
- `$(...)` 会吃掉 patch 末尾换行；runtime 已容忍，但手工构造 patch 时注意。

## Collaboration

commit 格式 `type(scope): [cr_id_skip] Desc`；finalize 默认绕过 commit hook（commit-tree），message 由调用方给。

## References

- 用户入口与安装：[`README.md`](README.md)
- 版本历史：[`CHANGELOG.md`](CHANGELOG.md)
- 老版本：`cc-old` 分支（V7.4 bash/prompt 版）、`codex-new` 分支（Codex Assurance v4）
