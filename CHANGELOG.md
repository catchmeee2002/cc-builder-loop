# Changelog — cc-builder-loop 已交付能力

> 从 CLAUDE.md §5 外移。记录各版本交付的能力与关键实现细节。

## V1.0 核心能力

- 多阶段 PASS_CMD + 智能早停
- tester 强隔离（hook 锁机制）
- 方案文件三视图过滤（builder/tester/shared）
- worktree 真隔离 + 三档合回
- rebase 冲突仲裁（arbiter subagent）
- reviewer → tester 触发
- 改动分级（L1 跳过 / L2 正常 / L3 先 tester）
- 任务回顾与知识沉淀
- Stop hook 兜底激活（loop.yml 存在 + 有改动 + 无状态文件 → 自动启动 loop）

## V1.5

- Stop hook 续接修复（exit 2 + stderr，取代无效的 JSON stdout）
- Worktree 前置（builder 进入后先 setup 再写代码，避免代码丢失）
- NDJSON trace（`.claude/loop-trace.jsonl`，每轮记录 iter/stage/result/duration）
- 一键 init（`loop-init.sh` 整合 probe + init-loop-config + git init）
- E2E 测试（全新仓库端到端验证）

## V1.6

- Worktree auto-commit（merge 前自动提交未 commit 改动，防数据丢失）
- Reviewer 时序硬门禁（PreToolUse hook 拦截 loop 活跃期的 reviewer spawn）
- Reviewer 参数预计算（stop hook PASS 后写 reviewer-params.json，消除 LLM diff 计算依赖）

## V1.7

- Reviewer 默认模型 sonnet（兼容 max / copilot 双路径，消除 haiku+xhigh 失败场景）+ Builder retry 错误分类（`effort/reasoning/not supported` 等 API 参数错误直接走兜底，不再盲重试）
- E2E 新增 `test-reviewer-compat.sh`（配置 lint + 可选 `--live` smoke）

## V1.8

- 多状态并行（state 文件从 `.claude/builder-loop.local.md` 迁移到 `.claude/builder-loop/state/<slug>.yml`；locate-state.sh 按 CWD 定位；单项目可并行多个 loop；migrate-state.sh 一键迁移旧版本）

## V1.8.1

- 僵尸 state 自愈 + EARLY_STOP 立即通知
  - Stop hook 遇到 `active != true` 的 state → 归档到 `.claude/builder-loop/legacy/<ts>-zombie_inactive.bak` 后放行
  - EARLY_STOP 路径从"改 active=false + exit 0"改为"归档 + exit 2 + stderr 注入"，builder 当场收到通知
  - 配合 V1.8 的 per-worktree state 隔离，彻底闭环"同 session 多任务僵尸串味"问题

## V1.8.2

- 兜底激活 HEAD 游标
  - Stop hook bootstrap 分支新增「已处理 HEAD 游标」（`.claude/builder-loop/last_processed_head`）
  - PASS / 异常 merge / EARLY_STOP 三处出口写入当前 HEAD，下次 Stop 时若 HEAD 未前进且无未提交改动则静默放行
  - 消除"推完 commit 后反复触发 NOOP 空转 bootstrap"的自激循环

## V1.8.3

- Stop hook flock 互斥 + auto-commit message 语义化 + PASS 分支 state 预读
  - Stop hook 按 per-slug 粒度加 `flock -n`，抢不到锁 `exit 0` 静默放行（防 CC 并发触发的 TOCTOU race）
  - `merge-worktree-back.sh` 的 auto-commit message 从 state 的 `task_description` 解析，构造 `chore(loop): [cr_id_skip] Auto-commit ${task}`
  - Hotfix：PASS 分支把 `start_head` 读取提前到 merge 调用之前（防 cleanup_worktree rm state 后 grep 报错）

## V1.9

- Judge agent — LLM 语义判据补 PASS_CMD 二值判据盲区
  - `run-judge-agent.sh`：hook 内嵌 Anthropic API 调用，输出 `{action, confidence, reason, downgraded, ...}` 单行 JSON
  - 凭证双路径：`ANTHROPIC_API_KEY` env → `~/.claude.json` OAuth → none（降级）
  - 模型三层 fallback：`loop.yml.judge.model` > `$ANTHROPIC_DEFAULT_HAIKU_MODEL` > `"claude-haiku-4-5"`
  - 三态判定：`continue_nudge` / `stop_done` / `retry_transient`
  - 防脱缰：iter 上限 + 连续 nudge 上限默认 2 + confidence 阈值 0.5 + API 超时 8s
  - 任何故障路径 → `downgraded=true` + 走原 PASS/FAIL，不阻断
  - 完全回退：`loop.yml.judge.enabled: false`

## V2.0

- PASS_CMD 跑 worktree（元问题修复）+ tester/doc-maintainer 流程加固
  - 元问题根因：`run-pass-cmd.sh` 死代码读旧路径 → PASS_CMD 永远跑主仓
  - state schema 重构：`project_root` = 干活的地方；新增 `main_repo_path` = 主仓
  - `run-pass-cmd.sh` 改三参签名 `<run_cwd> <iter> [<log_root>]`
  - `early-stop-check.sh` 修保护路径检测失效 bug
  - doc-maintainer audit checklist 落地 `docs/doc-maintainer-audit-checklist.md`

## V2.1

- Judge agent 长期共存方案 + sonnet→haiku 降级链
  - env file 自动加载：`judge-env.sh`（主 env 缺失时 source）
  - sonnet → haiku 降级链：默认 `primary_model=claude-sonnet-4-6`，连续失败 2 次切 haiku
  - 降级状态本 loop 内有效，PASS 后自动重置
  - 默认 timeout 8 → 15 秒

## V2.1.1

- `.gitignore` 自愈固化
  - `init-loop-config.sh` 新增 3 条 ignore 规则
  - `setup-builder-loop.sh` 每次启动跑 `ensure_gitignore_rules()` 幂等自愈

## V2.2

- Tester 跨目录写硬门禁 + 复盘强制分类闸门 + Bootstrap 空转修复
  - `tester-write-guard.sh` 新 PreToolUse hook，物理拦截 tester 写到 worktree 之外
  - 锁 schema 扩展：追加 `worktree_path` / `main_repo_path` / `slug`
  - 复盘改造为强制 4 桶分类（A1/A2/B/C）
  - Bootstrap 触发器砍 `HAS_RECENT_COMMIT`，只看 `HAS_DIFF`

## V2.2.1

- Bootstrap 纯文档白名单
  - 改动全命中 `\.md$|^docs/|\.txt$|^LICENSE$|\.gitignore$` → 静默放行

## V2.3

- 主仓 dirty 安全入 worktree + Reward Hacking 检测
  - dirty stash：setup pre-flight `git stash push -u` + worktree 内 apply
  - state 新增：`pre_loop_stash_ref` / `pre_loop_dirty_files` / `worktree_mode`
  - Layer 2 正则兜底检测 reward hacking（loop.yml / pyproject.toml 等配置 + 关键词双命中）
  - stop hook 三选项注入 + `loop.yml.judge.reward_hacking_detection: false` 可关

## V2.4

- locate-state.sh 策略 5 — 主仓 cwd 自动绑定唯一 active worktree
  - 策略 2/3/4 全 miss 后扫 active=true + worktree_path 存活 → 恰好 1 个候选时绑定
  - setup cwd 警告 + stop hook 诊断 stderr

## V2.5

- stop hook 可观测性 — debug log + diagnose 脚本 + setup 自检
  - `debug_log()` 函数 + 10 处 phase 插桩，NDJSON 格式，1 MB rotate
  - `diagnose-stop-hook.sh`：6 段 dry-run 排查
  - setup 末尾 hook 注册 + 软链自检

## V2.5.1

- stop hook observability hotfix
  - debug_log 路径分裂修复（子目录 cwd 时日志集中写到 PROJECT_ROOT）
  - pass_cmd_result.log_path 空格截断修复

## V2.6 Phase 1

- abandon-loop.sh 出口 + dotfiles A3 关键词识别
  - `abandon-loop.sh <state_file> <reason>`：归档 state + stash 还原 + trace event + worktree 保留
  - A3 关键词白名单：停下loop / 停掉loop / 停止loop / 中止loop / abandon loop
  - 后续 Phase：Phase 2 异步 baseline probe + Phase 3 严格差集归因（未实施）

## V2.7

- Max / Copilot 方案运行时识别
  - install.sh 新增 `detect_plan()` 函数，读 `ANTHROPIC_BASE_URL` env 识别方案
  - Max 方案（direct HTTPS）→ 5 个 hook（不注册 tester-write-guard.sh，CC 直连无需 Write/Edit 代理拦截）
  - Copilot 方案（localhost/127.0.0.1）→ 6 个 hook（含 tester-write-guard.sh）
  - diagnose-stop-hook.sh 同步 PLAN env 参数，banner 加"plan: X base_url=Y"，[1/6] hook 检测按方案过滤
  - 新增 e2e fixture `test-plan-detection.sh`（21 断言覆盖两方案 install + diagnose [1/6]+[2/6] verdict + --json plan 字段）

## V2.7.1

- install / uninstall 鲁棒性增强（A 批 cherry-pick 回 main）
  - `install.sh` `has_entry()` → `find_entry_status()` 三态返回（missing / match / stale），matcher 字面变化时先删旧再写新，避免同脚本多条 stale 条目并存
  - `uninstall.sh` 软链删除循环 + `bl_scripts` 列表都补 `reviewer-timing-check.sh`（pre-existing 漏项）
  - 新增 e2e fixture：`test-install-uninstall-roundtrip.sh`（装→卸语义还原 baseline）+ `test-install-matcher-update.sh`（matcher 改后重 install 识别 stale）
