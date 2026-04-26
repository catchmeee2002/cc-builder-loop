# cc-builder-loop — Builder 自闭环迭代

> 把 builder 模式从「单次执行」升级为「机器判定的多轮自闭环」。
> 项目接入只需在项目根放 `.claude/loop.yml`，定义 `pass_cmd`（lint/test 等通过条件），
> builder 改完代码会自动跑 PASS_CMD，失败自动喂回让 builder 再改，直到 PASS 或达到上限。

## 1. 链接映射表

install.sh 创建以下软链，把仓库文件映射到 CC 运行时路径：

| 仓库路径 | 运行时路径 | 链接方式 | 用途 |
|----------|-----------|---------|------|
| `skills/builder-loop/` | `~/.claude/skills/builder-loop/` | `ln -sfn` 整目录 | CC 自动发现 SKILL.md |
| `scripts/builder-loop-stop.sh` | `~/.claude/scripts/builder-loop-stop.sh` | `ln -sf` 逐文件 | Stop hook 入口 |
| `scripts/tester-lock-write.sh` | `~/.claude/scripts/tester-lock-write.sh` | `ln -sf` 逐文件 | SubagentStart hook |
| `scripts/tester-lock-check.sh` | `~/.claude/scripts/tester-lock-check.sh` | `ln -sf` 逐文件 | PreToolUse hook |
| `scripts/tester-lock-clear.sh` | `~/.claude/scripts/tester-lock-clear.sh` | `ln -sf` 逐文件 | SubagentStop hook |
| `scripts/tester-write-guard.sh` | `~/.claude/scripts/tester-write-guard.sh` | `ln -sf` 逐文件 | PreToolUse hook（Write\|Edit\|MultiEdit）|
| `scripts/reviewer-timing-check.sh` | `~/.claude/scripts/reviewer-timing-check.sh` | `ln -sf` 逐文件 | PreToolUse hook（Agent） |
| `agents/tester.md` | `~/.claude/agents/tester.md` | `ln -sf` 逐文件 | tester subagent |
| `agents/arbiter.md` | `~/.claude/agents/arbiter.md` | `ln -sf` 逐文件 | 仲裁 subagent |
| *(install.sh)* | `~/.claude/settings.json` hooks 段 | python3 增量合并 | 6 个 hook 条目 |

**注册的 6 个 hook**：

| Hook 类型 | Matcher | 脚本 | 作用 |
|-----------|---------|------|------|
| Stop | 无（全局） | builder-loop-stop.sh | 每次 CC Stop 时检查是否需要继续循环 |
| SubagentStart | `tester` | tester-lock-write.sh | tester 启动时落隔离锁（V2.2 锁文件追加 worktree_path / main_repo_path / slug 字段）|
| SubagentStop | `tester` | tester-lock-clear.sh | tester 结束时清锁 |
| PreToolUse | `Read\|Grep\|Glob` | tester-lock-check.sh | 拦截 tester 对 source_dirs 的读操作 |
| PreToolUse | `Write\|Edit\|MultiEdit` | tester-write-guard.sh | V2.2：拦截 tester 把文件写到 worktree 之外（exit 2 + 精确诊断 stderr）|
| PreToolUse | `Agent` | reviewer-timing-check.sh | 拦截 loop 活跃期的 reviewer spawn |

## 2. 部署指南

```bash
# 安装（幂等，可重复跑）
cd /mnt/hongyu.liao_docker/cc-builder-loop
./install.sh

# 卸载
./uninstall.sh

# 验证
ls -la ~/.claude/skills/builder-loop/SKILL.md  # 应指向本仓库
```

**前置依赖**：
- `~/.claude/` 目录已存在（通常由 dotfiles 的 `stow claude` 创建）
- python3（hook 注册用）
- jq（可选，install.sh 实际用 python3 做 JSON 操作）

**新机器部署顺序**：先 `my-dotfiles/install.sh`（stow 创建 `~/.claude/`），后 `cc-builder-loop/install.sh`。

## 3. 与 dotfiles 的依赖关系

本仓库是**自包含**的，但运行时依赖 dotfiles 中的以下共享文件：

| dotfiles 文件 | 本仓库依赖方式 |
|---------------|---------------|
| `~/.claude/commands/builder.md` | builder 模式定义，含 loop.yml 检测 / setup 调用 / tester 触发等 loop 逻辑 |
| `~/.claude/commands/planner.md` | planner 模式定义，含 3 视图区块约定 |
| `~/.claude/agents/reviewer.md` | reviewer 定义，含 TESTER_HINT 输出格式 |

**路径约定**：所有脚本引用都通过 `~/.claude/` 前缀的运行时路径（如 `~/.claude/skills/builder-loop/scripts/xxx.sh`），不直接引用仓库路径。

**解耦方向**（未来可选，现状不变；改 loop 行为要跨两仓同步是当前痛点）：
- **C 契约化** — cc-builder-loop 声明对 dotfiles `builder.md` 的段落契约 + E2E 加 `check-prompt-sync.sh` 校验
- **D 片段注入** — cc-builder-loop 持有 loop 相关 prompt 片段，`install.sh` 注入 dotfiles `builder.md` 的锚点之间，代码 + prompt 同仓改原子

## 4. 目录结构

```
cc-builder-loop/
├── install.sh / uninstall.sh   # 部署/卸载
├── CLAUDE.md                   # 本文件
├── skills/builder-loop/        # CC skill（含 SKILL.md、scripts/、fixtures/e2e/、schema/）
├── scripts/                    # Stop hook + tester 隔离 hook + reviewer 时序 hook（5 个 .sh）
└── agents/                     # tester.md + arbiter.md
```

## 5. 已交付能力（V1.0~V2.2）

- 多阶段 PASS_CMD + 智能早停
- tester 强隔离（hook 锁机制）
- 方案文件三视图过滤（builder/tester/shared）
- worktree 真隔离 + 三档合回
- rebase 冲突仲裁（arbiter subagent）
- reviewer → tester 触发
- 改动分级（L1 跳过 / L2 正常 / L3 先 tester）
- 任务回顾与知识沉淀
- Stop hook 兜底激活（loop.yml 存在 + 有改动 + 无状态文件 → 自动启动 loop）
- **V1.5**: Stop hook 续接修复（exit 2 + stderr，取代无效的 JSON stdout）
- **V1.5**: Worktree 前置（builder 进入后先 setup 再写代码，避免代码丢失）
- **V1.5**: NDJSON trace（`.claude/loop-trace.jsonl`，每轮记录 iter/stage/result/duration）
- **V1.5**: 一键 init（`loop-init.sh` 整合 probe + init-loop-config + git init）
- **V1.5**: E2E 测试（全新仓库端到端验证）
- **V1.6**: Worktree auto-commit（merge 前自动提交未 commit 改动，防数据丢失）
- **V1.6**: Reviewer 时序硬门禁（PreToolUse hook 拦截 loop 活跃期的 reviewer spawn）
- **V1.6**: Reviewer 参数预计算（stop hook PASS 后写 reviewer-params.json，消除 LLM diff 计算依赖）
- **V1.7**: Reviewer 默认模型 sonnet（兼容 max / copilot 双路径，消除 haiku+xhigh 失败场景）+ Builder retry 错误分类（`effort/reasoning/not supported` 等 API 参数错误直接走兜底，不再盲重试）
- **V1.7**: E2E 新增 `test-reviewer-compat.sh`（配置 lint + 可选 `--live` smoke）
- **V1.8**: 多状态并行（state 文件从 `.claude/builder-loop.local.md` 迁移到 `.claude/builder-loop/state/<slug>.yml`；locate-state.sh 按 CWD 定位；单项目可并行多个 loop；migrate-state.sh 一键迁移旧版本）
- **V1.8.1**: 僵尸 state 自愈 + EARLY_STOP 立即通知
  - Stop hook 遇到 `active != true` 的 state → 归档到 `.claude/builder-loop/legacy/<ts>-zombie_inactive.bak` 后放行（原行为是保留僵尸，下次 builder 进场会误判为活跃 loop）
  - EARLY_STOP 路径从"改 active=false + exit 0"改为"归档 + exit 2 + stderr 注入"，builder 当场收到通知立即 AskUserQuestion（原行为需等到下轮 user prompt 才发现）
  - 配合 V1.8 的 per-worktree state 隔离，彻底闭环"同 session 多任务僵尸串味"问题（复现 session `81bdbe27`）
- **V1.8.2**: 兜底激活 HEAD 游标
  - Stop hook bootstrap 分支新增「已处理 HEAD 游标」（`.claude/builder-loop/last_processed_head`）：PASS / 异常 merge / EARLY_STOP 三处出口写入当前 HEAD，下次 Stop 时若 HEAD 未前进且无未提交改动则静默放行
  - 消除"推完 commit 后 30 分钟内每次对话反复触发 NOOP 空转 bootstrap"的自激循环（复现 session `3d62eb57`）
  - 降级保证：游标文件缺失/损坏/HEAD 读不到都自动退回旧行为
- **V1.8.3**: Stop hook flock 互斥 + auto-commit message 语义化 + PASS 分支 state 预读
  - Stop hook 按 per-slug 粒度加 `flock -n`（`.claude/builder-loop/stop-hook-<slug>.lock`），抢不到锁 `exit 0` 静默放行（防 CC 并发触发的 TOCTOU race）
  - `merge-worktree-back.sh` 的 auto-commit message 从 state 的 `task_description`（YAML block scalar）解析，构造 `chore(loop): [cr_id_skip] Auto-commit ${task}`，不再固化为 `Auto-commit iter N` 丢失语义
  - **Hotfix**：PASS 分支把 `start_head` 读取提前到 `merge-worktree-back.sh` 调用**之前** — 因 `cleanup_worktree()` 会 `rm -f state`，原 merge **之后**再 grep state 的路径会抛 `No such file` 到用户屏幕，`set -e` 触发脚本非正常退出 → reviewer-params / exit 2 PASS 消息丢失（真正消除 session `d9ef1004` 复现的 `grep state: No such file` 报错）
  - 前提：flock 语义要求本地文件系统（ext4/xfs 等），NFS/FUSE 场景未验证
- **V1.9**: Judge agent — LLM 语义判据补 PASS_CMD 二值判据盲区
  - 新增 `skills/builder-loop/scripts/run-judge-agent.sh`：hook 内嵌 Anthropic API 调用，输出 `{action, confidence, reason, downgraded, ...}` 单行 JSON
  - 凭证双路径：`ANTHROPIC_API_KEY` env（Copilot CC 方案优先）→ `~/.claude.json` `oauthAccount.accessToken`（正版 Max CC 方案 fallback）→ none（降级）
  - 模型三层 fallback：`loop.yml.judge.model` > `$ANTHROPIC_DEFAULT_HAIKU_MODEL` > `"claude-haiku-4-5"`，dot/dash 命名自动规范化
  - 三态判定：`continue_nudge`（exit 2 + nudge 文案 + state.iter++）/ `stop_done`（走原 PASS）/ `retry_transient`（FAIL 分支识别 API 抖动）
  - 防脱缰：iter 上限不变 + 连续 nudge 上限默认 2 + confidence 阈值默认 0.5 + API 超时 8s
  - 注入文案统一前缀 `[builder-loop judge | iter=X/Y | judge=Z | conf=W]` + 末尾"非用户输入"声明（与用户输入肉眼可分；retrospective T7 教训）
  - State 字段扩展：新增 `last_judge_action / last_judge_confidence / last_judge_ts / consecutive_nudge_count`（旧 state 缺字段视为初始值，由 upsert 自动追加）
  - Telemetry：每次 judge 调用一行 jsonl 落 `.claude/builder-loop/judge-trace.jsonl`，下一轮 stop hook 自动后置补 `outcome` 标签（仅 continue_nudge 类）
  - 任何故障路径（API 超时 / 非 200 / JSON 解析失败 / confidence 偏低 / 凭证缺失）→ `downgraded=true` + 走原 PASS / FAIL 路径，**不阻断现有 PASS_CMD 流程**
  - 完全回退方法：`loop.yml.judge.enabled: false` 或卸载 `run-judge-agent.sh`（stop hook 检测脚本缺失自动走原路径）
  - loop.yml schema 新增 `judge:` 段（全部可选）；本仓 PASS_CMD 加 `judge_agent` / `judge_integration` 两个 e2e 阶段
- **V2.0**: PASS_CMD 跑 worktree（元问题修复）+ tester/doc-maintainer 流程加固
  - **元问题根因**：V1.7 起 `run-pass-cmd.sh` L22 `STATE_FILE="${PROJECT_ROOT}/.claude/builder-loop.local.md"` 是 V1.7 之前的旧文件路径，V1.8 把 state 迁到 `.claude/builder-loop/state/<slug>.yml` 后这段成死代码；后果是 `RUN_CWD = PROJECT_ROOT = 主仓`，PASS_CMD 永远跑主仓 loop.yml + 主仓代码——"在 worktree 改 loop.yml 加 stage" 本轮看不到（V1.9 落地时被发现）
  - **state schema 重构**：`project_root` 字段语义改为"干活的地方"（worktree 模式 = worktree path / bare = 主仓），新增 `main_repo_path` 字段固定为主仓。下游 5 个脚本（setup / stop hook / merge-worktree-back / run-apply-arbitration / early-stop-check）全链路改造；老 V1.x state 缺 `main_repo_path` 时按"project_root 等于主仓"旧语义兜底
  - **run-pass-cmd.sh** 删除 V1.7 死代码，改为接收 `<run_cwd> <iter> [<log_root>]`；LOOP_YML 从 RUN_CWD 读、日志归档主仓；worktree 内 loop.yml 缺失（用户首次未 commit）→ fallback 主仓 + stderr 警告
  - **early-stop-check.sh** 顺修 V1.x 既有 bug：原"state path 向上 2 层"只到 `.claude/builder-loop` 子目录，git diff `-- "$test_dirs"` 永远 0 命中——保护路径作弊检测实质失效。改为读 state.project_root（V2.0 = worktree）能看见 builder 真实改的文件
  - **M5 merge-worktree-back.sh case 默认分支显式错误**：unknown action 不再静默 `exit 0 + rm state` 丢 state，改为 `exit 2 + stderr 完整输出`，防 V1.9.1 修过的 grep 静默退出回归再次踩坑
  - **M4 tester subagent prompt 加 4 条硬约束**：bare loop fixture slug=__main__ / V2.0 state schema 写 main_repo_path / worktree 启用前先 commit loop.yml / bash grep+head+sed 必须 || true 收尾（防止 `set -euo pipefail` 静默退出）
  - **M2 doc-maintainer audit checklist** 落地 `skills/builder-loop/docs/doc-maintainer-audit-checklist.md`：要求 maintainer 必跑 6 步黑盒 audit（fixture 表格交叉对账 / 版本号 / SKILL schema / 链接映射 / hook 注册 / 已修问题 fix 状态）+ 4 项分类勾选 + 历史欠账反查。Builder spawn doc-maintainer 时**必须把本文件路径附进 prompt**，杜绝引导式 prompt 漏判 V1.5–V1.9 累计 9 次 fixture 表格欠账的老问题
  - 配套两个新 e2e fixture：`test-pass-cmd-runs-worktree.sh`（17 case）+ `test-bare-loop-merge.sh`（10 case）
- **V2.1**: Judge agent 长期共存方案 + sonnet→haiku 降级链
  - **背景**：V1.9 凭证检测 `env > oauth > none`，正版 Max CC 用户主会话走 OAuth 不 export `ANTHROPIC_API_KEY`，OAuth token 又不在 `~/.claude.json` 公开字段（known-risks R5）→ judge 一直降级回 V1.8 二值判据
  - **env file 自动加载**：`run-judge-agent.sh` 顶部新增"主 env 缺失时 source 全局 `~/.claude/skills/builder-loop/judge-env.sh`"；`set -a` 模式让用户写裸赋值即可。**主 env 已设的用户不被覆盖**（向后兼容 Copilot env 方案）。新增 `loop.yml.judge.credentials_file` 字段允许项目级覆盖（phase 1 加载，覆盖默认全局路径）
  - **sonnet → haiku 降级链**：默认 `primary_model=claude-sonnet-4-6`（copilot-proxy 唯一支持的 sonnet ID），失败 `fallback_after_failures=2` 次后自动切 `fallback_model=claude-haiku-4-5`。失败定义：API timeout / HTTP 5xx / JSON parse_error；不计数：401/403（凭证）/ 429（rate_limit）/ low_confidence（模型判断能力问题）。**降级状态本 loop 内有效**（state.judge_active_model + judge_consecutive_failures 字段，loop PASS 删 state 自动重置；下个 loop 重新从 sonnet 试）
  - **默认 timeout 8 → 15 秒**：sonnet 单次 ~5.8s，留余量防偶发慢响应误降级
  - **完全向后兼容**：V1.9 配置 `model: <id>` 仍工作（自动等价 primary_model）；`fallback_model: ""` 留空 = 禁用降级链回 V1.9 行为；`enabled: false` 仍可整段关闭
  - 配套两个新 e2e fixture：`test-judge-env-file-load.sh`（12 case，5 个 A 段：主 env 优先 / 文件不存在 / 语法错降级 / loop.yml 项目级覆盖）+ `test-judge-model-fallback.sh`（28 case，11 个 B 段：成功路径 / 失败计数 / 切 fallback / 切 fallback 后再失败 / 401/429 不计数 / parse_error 计数 / 旧 state 兼容 / 改 primary_model 立即生效）
  - 用户配置示例：`skills/builder-loop/judge-env.sh.example` 模板（含 copilot-proxy + 独立 sk-ant-key 两条路径说明）
- **V2.1.1**: `.gitignore` 自愈固化（K1 教训预防）
  - **背景**：worktree 模式下 `merge-worktree-back.sh` 的 `git add -A` 会把 PASS_CMD 跑过程中生成的运行时文件（`judge-trace.jsonl` / `loop-trace.jsonl` / `reviewer-params.json` / lock）也 commit 进 branch，主仓同路径 untracked 顶住 ff merge → stop hook 报 `MERGE_LAST="ERROR ff-after-rebase-failed"`（V2.1 落地时刚踩过一次）
  - **修复**：(1) `init-loop-config.sh` 接入向导新增 3 条规则（顶层 `.claude/loop-trace.jsonl` / `.claude/reviewer-params.json` / `.claude/reviewer-diff.txt`），原有 `.claude/builder-loop/` + `.claude/loop-runs/` 不变；(2) `setup-builder-loop.sh` 每次启动 loop 时跑 `ensure_gitignore_rules()` 幂等自愈，存量项目（接入时漏配的）自动追加，stderr 输出 `🛡️ .gitignore 自愈追加：<rule>`
  - **fixture 健壮性顺修**：`test-nudge-max-reads-worktree.sh` 的 `bash setup ... | head -5` 改用临时日志文件解耦（防 head 关 pipe 触发 SIGPIPE 让 setup 中途死，setup 输出量随版本会增长）
  - **根因 follow-up（未修）**：`merge-worktree-back.sh` 的 `git add -A` 仍是过度收集；本期靠 `.gitignore` 兜底，独立任务再做精确化
- **V2.2**: Tester 跨目录写硬门禁 + 复盘强制分类闸门 + Bootstrap 空转修复
  - **背景**：session `283ee3b2` 暴露 3 个独立 bug。①tester subagent 虽然 cwd=worktree、prompt 明示 worktree_path，仍把 5 处工具调用写到主仓绝对路径，下游 ff-merge 撞 untracked。②同 session 复盘把"tester 跨目录写"这种**显然能用 hook 防住**的 A2 类机制缺口判成 B 类落 memory，违反 builder.md L177 自身判据。③同 session 阶段 0 闭环后，stop hook 因 `HAS_RECENT_COMMIT` 触发器太激进连续两次 NOOP 兜底激活、输出 reviewer 流程提示空转
  - **议题 1 防御**：(a) `scripts/tester-write-guard.sh` 新 PreToolUse hook（matcher=`Write|Edit|MultiEdit`），物理拦截 tester subagent 把文件写到 worktree 之外。识别 tester 复用 V1.1 既有锁文件 `${ISOLATION_LOCK_DIR:-/tmp}/cc-subagent-${session_id}.lock`，新读 `worktree_path` 字段决定是否拦截。(b) `tester-lock-write.sh` 锁 schema 扩展：追加 `worktree_path` / `main_repo_path` / `slug` 三字段。(c) `agents/tester.md` 输入字段表新增 `worktree_path`，硬性约束第 6 条「路径根硬约束」+ 步骤 4 自检追加路径根校验项 + 禁 Bash cp/mv/ln 搬运
  - **议题 1 路径白名单**：严格只允 `${worktree_path}/*`（含尾斜杠防 `/wt` 与 `/wt2` 误匹配），realpath 解析后再 prefix 比较防 path traversal 绕过。bare loop（`worktree_path` 为空）+ V1.x 老锁（无字段）→ 放行所有 Write/Edit
  - **议题 1 拒绝语义**：exit 2 + stderr 含「尝试写入路径 / 解析后 abspath / 允许根 worktree_path / 主仓 main_repo_path / 改用建议」5 行精确诊断，让 tester 看到错误后能直接拼出正确路径自动重试
  - **议题 1 spawn 契约**：`~/.claude/commands/builder.md` L119/L122 spawn tester 段强制传 `worktree_path`（loop 活跃 = state.worktree_path / loop 已结束 = ""）
  - **议题 2 复盘改造**：`builder.md` 步骤 5 重写为「① 列全部候选 → ② 强制 4 桶分类（A1/A2/B/C 各列号或'无'+理由）→ ③ 仅 B/C 走 5 问 → ④ 提审强制 [A1]/[A2]/[mem] 前缀 → ⑤ 落盘」。空桶必须显式写"无"+一句话理由，4 桶并列输出。钉 3 条反例锚点（跨目录写 → A2 / .gitignore 自愈 → 已交付不立项 / stop hook 平台契约 → B 不是 A）
  - **议题 3 bootstrap 触发器收敛**：`scripts/builder-loop-stop.sh` L173-179 砍 `HAS_RECENT_COMMIT` 作为触发条件，bootstrap 兜底**只看** `HAS_DIFF`（未提交工作树改动）。`HAS_RECENT_COMMIT` 变量保留供 task_desc fallback 推断
  - **议题 3 取舍**：用户/builder 手动 commit 后工作树干净 → bootstrap 静默放行（不再被无意义 NOOP 触发的 reviewer 提示困扰）；损失场景：用户在主仓直接改代码 + commit + 关 CC（不经 loop）→ 失去自动补 PASS_CMD 兜底，需手动 `setup-builder-loop.sh "<task>"` 起 loop（详见 §7.7）
  - **install.sh / uninstall.sh** 同步追加 `tester-write-guard.sh` 软链 + hook 注册条目（registrations 列表 +1 条；uninstall.sh `bl_scripts` 列表 +1 项）
  - 配套新 e2e fixture：`test-tester-write-guard.sh`（13 case，A1-A9 覆盖 拒绝主仓 / 放行 worktree / 无锁 / bare loop 老锁兼容 / 等于 worktree 根 / 前缀部分匹配 / path traversal / 非 tester subagent 放行）

详见 `skills/builder-loop/README.md` 与 `skills/builder-loop/docs/judge-agent.md`。

## 6. 开发原则

- **不改 CC 源码**：所有功能基于 CC 的 hook / skill / agent 扩展机制实现
- **可破坏性升级**：升级允许不兼容已接入项目的 loop.yml，但必须手动更新所有已接入项目确保继续可用
- **[HARD RULE] Prompt 只写"做什么"**：写 builder.md / SKILL.md / agent prompt / commands/*.md 时只下达 imperative 指令（操作步骤、判据、出口、约束），禁止写动机/原因/反向出题/"防偷懒"等心理说辞。设计思路写到代码注释或 `docs/`，不进 prompt。

## 7. 已知问题 / 排查手册

### 7.1 Stop hook 未触发测试 — 僵尸 state 文件 bug（2026-04-24 定位，V1.8.1 修复）

**现象**：builder 回复"✅ loop 已活跃"，但 Stop hook 没跑测试，session 直接停下（复现 session `81bdbe27`）。

**根因**：Stop hook 每次都正确触发，但读到 `active=false` 的僵尸 state（来自前一个任务手动编辑或早停遗留）后正确地放行了——这是设计行为。真正的问题是**僵尸 state 本身的存在**：同一 CC session 里连续做多个任务时，builder 可能跳过前置 setup，看到旧状态文件就假设"已活跃"，结果消费的是无效的僵尸。

**修复**（V1.8.1）：Stop hook 现在遇到 `active != true` 的 state 从"放行保留"改为"归档到 `legacy/<ts>-zombie_inactive.bak` + 放行"，下次 builder 进场无法再读到僵尸；同时 EARLY_STOP 从"改字段 + exit 0"改为"归档 + exit 2 注入"，让 builder 当场收到通知。

**排查步骤**：
1. 用 `ls -la <project_root>/.claude/builder-loop/` 查看是否有 `state/` 或 `legacy/` 目录
2. 若有 `legacy/*.bak`，用 `tail -20 /tmp/builder-loop-stop-debug.log` 查 hook 退出原因
3. 若日志显示 `active=false` 或 `stopped_reason` 存在，证实是僵尸 → V1.8.1 修复已生效
4. 若日志显示其他退出原因（如 `no_project_root`），按日志的 `EXIT_REASON` 字段诊断

### 7.2 Commit-msg hook 拦截导致 auto-commit 失败

**现象**：loop 跑到 merge-worktree-back.sh 时失败，错误信息含 "commit message" 或 "cr_id_skip"。

**根因**：当项目启用了严格的 commit-msg hook（如 guard-commit-msg.sh）时，auto-commit message 的格式必须合规。V1.8 的 message `"chore(loop): auto-commit iter N"` 不含必要的 `[cr_id_skip]` 标记，被 hook 拦截。

**修复**（V1.8.1）：merge-worktree-back.sh 的 auto-commit message 改为 `"chore(loop): [cr_id_skip] Auto-commit iter N"`，兼容所有启用 msg hook 的项目。

**排查步骤**：检查 merge-worktree-back.sh 第 138 行的 commit message，应含 `[cr_id_skip]` 标记。

### 7.3 Judge agent 全部判定都被降级（V1.9+）

**现象**：开启 V1.9 后，`.claude/builder-loop/judge-trace.jsonl` 每行都是 `downgraded:true`，loop 行为退化为 V1.8（PASS_CMD 二值判据）。

**排查步骤**：

1. 跑 self-check：
   ```bash
   bash ~/.claude/skills/builder-loop/scripts/run-judge-agent.sh --self-check
   ```
   - 输出 `ERROR: missing credentials` → 检查 `ANTHROPIC_API_KEY` env 是否设置（Copilot 方案）
   - **正版 Max CC 用户**（V2.1+ 推荐）：CC 自己的 OAuth token 不在 `~/.claude.json` 公开字段，judge 走不通 oauth 路径（详见 `skills/builder-loop/known-risks.md` R5）。
     **V2.1 Workaround**：写 `~/.claude/skills/builder-loop/judge-env.sh`：
     ```bash
     # 方案 A：copilot-proxy 链路（已有 proxy 用户首选）
     export ANTHROPIC_API_KEY=sk-666
     export ANTHROPIC_BASE_URL=http://localhost:4142
     # 方案 B：独立 sk-ant-key（无 proxy 用户）
     # export ANTHROPIC_API_KEY=sk-ant-...
     ```
     模板见 `skills/builder-loop/judge-env.sh.example`。run-judge-agent.sh 启动时自动 source（仅主 env 未设时）。

2. 看降级原因分布：
   ```bash
   cat <project_root>/.claude/builder-loop/judge-trace.jsonl | python3 -c "
   import json, sys
   from collections import Counter
   c = Counter()
   for line in sys.stdin:
       try:
           obj = json.loads(line)
           if obj.get('downgraded'):
               c[obj.get('downgrade_reason', '?')] += 1
       except: pass
   print(c.most_common(10))
   "
   ```

3. 常见原因 → 处理：
   - `missing_credentials` → 见步骤 1
   - `timeout` → 检查 `ANTHROPIC_BASE_URL`（copilot-proxy 是否在跑 / 网络是否通）
   - `http_401` / `http_403` → token 失效，重新登录 / 重启 copilot-proxy
   - `parse_error` → 模型可能返回 markdown 包裹 JSON 或拒答；查 `model_used` 字段，可考虑在 loop.yml 改 `judge.model`
   - `low_confidence` → 默认阈值 0.5 可能偏严，调高到 0.7 或调低到 0.3 看效果

4. 完全回退到 V1.8 行为：在项目 `.claude/loop.yml` 加：
   ```yaml
   judge:
     enabled: false
   ```

**已知风险开口项**：详见 `skills/builder-loop/known-risks.md`（R1 reward hacking / R2 LLM 假阳性 / R3 模型版本不可用 / R4 jsonl 增长）。

### 7.4 worktree 内 loop.yml 不存在（V2.0+）

**现象**：stop hook 跑 PASS_CMD 时 stderr 出现 `[run-pass-cmd] ⚠️  <worktree>/.claude/loop.yml 不存在（可能 worktree 内 loop.yml 未 commit），fallback 到主仓 ...`。

**根因**：V2.0 起 PASS_CMD 在 worktree 内跑、loop.yml 也从 worktree 读（让 worktree 内改 loop.yml 立即生效）。git worktree add HEAD 只拷贝 git tracked 的文件——若 loop.yml 写完后还没 `git add + git commit`，worktree 内就看不到。

**处理**：
- 用户接入新仓库时序：写 `.claude/loop.yml` → `git add .claude/loop.yml && git commit -m "..."` → 再调 `setup-builder-loop.sh`
- 已发生时：fallback 主仓 loop.yml 仍能跑，**不阻断**；下次 setup 前补 commit 即可
- e2e fixture 在 setup 前必须 commit loop.yml，否则会触发本警告（不算失败但产生多余 stderr）

**fixture 已知例**：`test-stop-hook-race-and-commit-msg.sh::场景 D` 在 V2.0 升级时失败，fix 是 setup 之前显式 `git add .claude/loop.yml && git commit`。

### 7.5 PASS_CMD 跑了主仓而非 worktree（已修）

**现象**：在 worktree 内改 loop.yml 加 stage，本轮 PASS_CMD 没跑新 stage（`.claude/loop-runs/iter-N-<new-stage>.log` 不存在）。

**根因**（V1.7-V1.9.x）：`run-pass-cmd.sh` L22 死代码读旧 V1.7 之前路径 `.claude/builder-loop.local.md`，V1.8 已迁移到新路径 → 永远找不到 → `RUN_CWD = PROJECT_ROOT = 主仓` → PASS_CMD 跑主仓的 loop.yml。Worktree 改的 loop.yml 不生效要等到 PASS + merge 回主仓后下一轮才看到。

**修复**（V2.0）：state schema 增加 `main_repo_path` 字段，`project_root` 字段语义改为"干活的地方"；下游脚本全链路适配；run-pass-cmd.sh 删死代码改三参签名。

**自检**：项目根 `.claude/builder-loop/state/<slug>.yml` 应同时含 `project_root: <worktree>` + `main_repo_path: <主仓>` 字段。缺 `main_repo_path` 就是老 V1.x state——下次 setup 后会自动写新 schema。

### 7.6 sonnet 降级到 haiku 后再不切（V2.1+）

**现象**：本 loop 后续 judge 调用 `model_used` 一直是 `claude-haiku-4-5`，看 `judge-trace.jsonl` 发现一段时间前发生过 `fallback_also_failed` 或 `fallback_triggered`。

**根因**：V2.1 设计：sonnet 连续失败 `fallback_after_failures` 次（默认 2）后切 haiku，**本 loop 内不再切第三档**也不重新尝试 sonnet。state 字段 `judge_active_model` 持久化到 loop PASS。

**何时重置**：
- loop PASS（merge 后 state 删除）→ 下个 loop 自动重新从 sonnet 试
- 手动 rm `<P>/.claude/builder-loop/state/<slug>.yml` 后下次 setup 重新开始
- 手动编辑 state 删除 `judge_active_model` 字段（不推荐，可能引入不一致）

**判断 sonnet 是否真的不可用**（避免误判 haiku 替代生效）：
```bash
# 测试 sonnet 直连
curl -sS -X POST $ANTHROPIC_BASE_URL/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}' \
  --max-time 10
```
- 200 + content → sonnet 后端正常，judge 状态机本 loop 锁定 haiku 是预期行为
- 5xx / 401 / timeout → 后端真的有问题；haiku fallback 是合理的兜底

**完全禁用降级链**：在 `loop.yml.judge` 设 `fallback_model: ""`，sonnet 失败直接 downgrade 回 PASS_CMD 二值（不切 haiku）。

### 7.7 用户主仓直接 commit 后 loop 没自动验证 PASS_CMD（V2.2+ 行为变更）

**现象**：用户在主仓直接改代码 + commit + 关 CC（不经过 builder loop / 不调 setup-builder-loop.sh）。期望 stop hook 兜底激活帮跑 PASS_CMD 验证，但 loop 没起来。

**原因**（V2.2 议题 3 设计变更）：bootstrap 兜底激活原本看「未提交工作树改动 OR 30 分钟内有 commit」任一条件就触发，V2.1 及之前会针对用户的新 commit 自动跑 PASS_CMD。

V2.2 砍了 `HAS_RECENT_COMMIT` 触发器，bootstrap **只看**未提交工作树改动。原因：原行为造成「builder/用户手动 commit 收尾后连续两次 NOOP 兜底激活、输出 reviewer 流程提示空转」（复现 session `283ee3b2`）——多数场景下用户 commit 完就是真的"已收尾"，loop 不该再纠缠。

**处理**（V2.2 推荐）：
- 用户主仓直接改代码后，commit **之前**关 CC：下次开 CC 触发 stop hook 时仍会兜底（HAS_DIFF 非空）
- 用户主仓直接改代码 + commit + 关 CC：下次开 CC 后**手动**调
  ```bash
  bash ~/.claude/skills/builder-loop/scripts/setup-builder-loop.sh "<task description>"
  ```
  起 loop 跑 PASS_CMD 验证

**自检**：bootstrap 是否触发可看 stderr 是否含 `[builder-loop] ⚡ 兜底激活：检测到 loop.yml + 代码改动但无状态文件...`。
