# cc-builder-loop 待固化改进

> 时间倒序。每条按 builder.md 步骤 5 模板（触发上下文 / 建议方向 / 优先级）。
> 立项不等于本期实施——A 类候选清单，等独立任务挑出来落地。

## 2026-04-29 locate-state.sh 策略 3 grep-sed 管道缺 `|| true` + set -e 缺失（pre-existing）

- **触发上下文**：V2.4 reviewer 反馈（🟡）— `locate-state.sh:24` 用 `set -uo pipefail` 缺 `-e`（与同项目其他脚本不一致，外部静默契约下吞错）；策略 3 的 `wt="$(grep -E '^worktree_path:' "$sf" 2>/dev/null | head -1 | sed -E '...')"`（L83）grep 未命中时 + pipefail 让子 shell 退出非 0，外层 `wt=...` 命令替换实际接受空字符串后续 `[ -z "$wt" ] && continue` 兜过去，但这种写法依赖 `set -e` 缺失才不杀脚本，写法脆弱（迁移到 `set -euo pipefail` 时会暴露）。本期按"bug fix 不带周边清理"原则不改。
- **建议方向**：
  1. 统一 `set -euo pipefail`，所有 grep / head / sed 管道末尾补 `|| true`（locate-state.sh / 其他遗留脚本一并扫一遍）
  2. e2e fixture：构造「worktree_path 字段缺失」的 state 文件验证策略 3 跳过该 state 不报错
  3. 顺路检查策略 4 / 5 的 grep 是否同样脆弱（V2.4 策略 5 已显式 `|| true`，但策略 3-4 未审）
- **优先级**：低（pre-existing 多版本未触发实质 bug；脆弱性属于代码风格而非功能正确性）

## 2026-04-29 V2.4 落地 session stop hook 未触发跑 PASS_CMD（loop 哑火活样本）

- **触发上下文**：本仓自身 V2.4 实施 session（slug=`1777457315-v2-4-locate-state-sh`）— builder reply 结束后等了 ~9 min，state 文件仍在（active=true、iter=0 未变），`.claude/loop-trace.jsonl` 最后一行是 2026-04-27 的（昨天的 PASS），今天没新条目；`.claude/loop-runs/` 也没新 iter 日志。证据指向 stop hook 这次根本没跑 PASS_CMD（而不是跑了但走早退 path —— 那会留 trace / 写 lock / 至少 stderr 有日志）。讽刺：本期就是修「主仓 cwd 时 stop hook 找不到 state」的盲区，而修复的 loop 自己没起来。
- **可能原因（待独立 task 定位）**：
  1. CC stop hook 注册条目本身丢了 / 被覆盖（settings.json 改动？install.sh 某次半执行？）
  2. flock 文件残留 → stop hook 抢锁失败静默 exit 0（`.claude/builder-loop/stop-hook-<slug>.lock` 检查）
  3. CC 主进程内部 stop 事件传播挂掉（CC 升级 / 重启 / hook timeout 调度问题）
  4. CC stop hook 触发条件变化（比如本会话有未应答 AskUserQuestion / 进入 dynamic loop / 后台 agent 未结束阻塞 stop 事件）
  5. cwd 跨主仓 / worktree 时 hook 注册脚本路径解析挂掉（`~/.claude/scripts/builder-loop-stop.sh` 软链断 / 主仓 .git 目录因 worktree branch 切换跨链路）
- **建议方向**：
  1. 加一个轻量自检脚本 `bash skills/builder-loop/scripts/diagnose-stop-hook.sh`：列 settings.json hooks 段 / 软链状态 / state 目录 / lock 目录 / 最近 trace；一键看为什么没触发
  2. setup-builder-loop.sh 末尾追加一次 hook 自检（hooks 段含 builder-loop-stop.sh + 软链有效），缺则 stderr 醒目报警提示用户重跑 install.sh
  3. stop hook 顶部加 debug log（`.claude/builder-loop/stop-hook-debug.log` 滚动，记每次触发的 ts / cwd / locate 结果 / 早退原因），出问题时直接 tail 看
  4. e2e fixture：构造「stop hook 软链断」场景验证 setup 自检能识别
- **优先级**：高（loop 触发本身是机制最底层契约，触发不到所有上层修复都白搭；本任务 9 分钟空等是直接证据）

## 2026-04-29 worktree PASS merge 后清理时丢失 untracked 白名单外文件，核心产出消失

- **触发上下文**：novel-writer 项目 meta-analysis skill 实现任务（commit `db9f7bc` / `7632807`）。项目 `.gitignore` 排除 `.claude/commands/*`（白名单仅 `.claude/agents/`），但 `.claude/commands/meta-analysis.md`（新建 slash command）和 `.claude/commands/analyze-exp.md`（追加 Step 5）是本任务的核心产出。Builder 在 worktree 内创建/修改这两个文件，loop iter 1 PASS_CMD 通过后 worktree 自动 merge + 清理 → **untracked 改动随 worktree 目录被删除一并丢失**。主仓只看到 git tracked 的 meta-analyzer.md / CLAUDE.md / cli/app.py 已 merge，但无关键 skill 产出。Builder 必须根据上下文记忆在主仓重建这两个文件（本次靠对话上下文有完整内容，但若上下文压缩或会话断开则数据永久丢失）。
- **建议方向**：
  1. setup-builder-loop 时识别项目 `.gitignore` 中被排除但物理存在于 worktree 的非空文件 → 在 builder-loop.local.md 或 state yml 中记录这些路径
  2. merge-worktree-back.sh 在清理 worktree 前，先 `cp -r` 这些 untracked 路径到主仓（非 git 同步，纯文件系统拷贝）
  3. 或更稳：让 setup-builder-loop 加 `untracked_sync_paths:` 配置项（loop.yml），用户显式声明哪些路径需要从 worktree 复制回主仓（白名单制，避免误同步 venv 等）
  4. 兜底：worktree 清理前先 `find <worktree> -newer <start_head_time> -not -path '.git/*'` 列出所有新建/改动的 untracked 文件，警告用户「以下文件不在 git 中，merge 不同步，请手动同步」
- **优先级**：中（高发面：任何项目 `.gitignore` 排除核心文件目录的场景都会踩；本次靠记忆兜底但不可持续）

## 2026-04-29 reviewer-params.json changed_files 含 loop hook 一并 commit 的无关 untracked 累积，reviewer 焦点被噪音稀释

- **触发上下文**：同上 meta-analysis 任务。Loop PASS auto-commit 时 hook 把所有 `git status` 中的 untracked 文件一并 `git add` 进 commit `db9f7bc` —— 包括上一轮 exp-015 实验产物 `novels/exp-015-blood-v12/export/*` 共 14 个文件。`reviewer-params.json` 的 `changed_files` 字段直接基于这次 commit 生成，把 14 个无关文件喂给 reviewer。Builder 必须在 reviewer prompt 里手动列「需剔除：novels/exp-015-blood-v12/export/* 全部，与本任务无关」。如果 builder 没意识到要剔除，reviewer 会把无关 export 文件当本任务产物去审。
- **建议方向**：
  1. auto-commit 阶段细化 `git add` 策略：只 add 本任务相关路径（基于 plan_file 路径列表 + start_head 后文件系统差异 ∩ 当前用户主动改动）
  2. 或给 reviewer-params.json 加 `task_scope_paths:` 字段（来自 plan_file 提及路径或用户配置），reviewer 优先看这些路径，其余 changed_files 标 `[累积无关]` 提示忽略
  3. 兜底：setup-builder-loop 时拍快照 `untracked_baseline.txt`（worktree 创建瞬间的 untracked 列表），auto-commit 时 diff 这个 baseline，仅 commit 本次新增/改动的 untracked 不 commit 历史遗留的
- **优先级**：低（可在 reviewer prompt 手动剔除；但 hook 端如果能根除噪音，reviewer 焦点更准）

## 2026-04-28 reviewer subagent 输入只看 git diff，docs/*.md 被 .gitignore 排除时 reviewer 看不到文档质量

- **触发上下文**：4-28 Engineering_Delivery_Bot 项目 event loop 工具箱任务，本次同时改了代码（`util/vid_metrics.py` / `service/vehicle_ws.py` 等）和两份关键文档（`docs/hmi-flash-file-push-improvement.md` 加 Case 4 + 归因修正、`docs/ssh-eventloop-blocking.md` 加第三回方法论沉淀）。但项目 `.gitignore` 排除所有 `*.md` → reviewer 只拿到 git diff 跟踪的代码改动，**完全看不到这两份文档**。即便 docs 内容质量直接影响下次排查（方法论沉淀 / 归因记录），reviewer 无法审。本次靠 builder 在 spec_shared / prompt 里手动塞引用，但 reviewer 没读 docs 文件本身，质量审查存在死角。
- **建议方向**：
  1. setup-builder-loop 时扫 `.gitignore`，识别 `*.md` 排除规则 → 提示用户「项目 docs/*.md 在 git 之外，reviewer 不会自动看；如需 reviewer 审 docs 请在 spawn 时加 `extra_paths`」
  2. spawn reviewer 的接口加 `extra_paths: list[str]` 字段，Builder 可显式传入文档路径，reviewer 收到后 Read 这些文件参与评审
  3. stop hook 的 reviewer-params.json 可包含 `untracked_paths`（`git ls-files --others --exclude-standard` 的子集），reviewer 主动 Read，不依赖 builder 手动传
  4. 长期：reviewer.md 增加一段 prompt「如果 changed_files 中没有 docs/*.md 但任务包含文档 case，主动用 Read 探查 docs/ 目录最近 git stat 之外的 .md 改动」
- **优先级**：中（dimage docs 不进 git 是该项目特例，但其他项目可能也有"重要 .md 不进 git"的场景；本次手动绕过 OK，但碰上更复杂任务时 reviewer 会直接漏审）

## 2026-04-28 bare 模式 + 主仓 dirty 未 commit 时 stop hook reviewer-params 算法不准

- **触发上下文**：V2.3 落地 session 实测——bare 模式跑 loop（用户主仓有大量 untracked + modified 但 builder 直接在主仓改），PASS 后 stop hook 算 reviewer-params 走的是 `git diff start_head..HEAD --name-only` 取 changed_files。但 bare 模式下主仓 HEAD **从头到尾没动**（commit 由 reviewer 通过后才做）→ `changed_files=[]` + `diff_file` 空 → reviewer 看不到任何改动。本次 builder 手动用 `git diff HEAD` 拿 working tree diff fallback 才让 reviewer 工作。
- **建议方向**：
  1. **stop hook PASS 路径**（bare 分支）改算 reviewer-params：检测 `worktree_path` 空且主仓 working tree dirty → 用 `git diff HEAD` + `git ls-files --others --exclude-standard --exclude=<setup 自管理路径>` 计算 changed_files；diff_file 写 `git diff HEAD` 全文
  2. worktree 模式（worktree_path 非空）保持原行为不变
  3. e2e fixture：`test-bare-loop-reviewer-params.sh` —— bare 模式 + 主仓 dirty + setup → PASS_CMD 跑通 → 断言 reviewer-params.json changed_files 含主仓 dirty 文件清单 + diff_file 含 `git diff HEAD` 内容
  4. 配套 builder.md 更新「步骤 2 获取 diff + spawn reviewer」一句话明确：bare 模式 reviewer-params 已自动覆盖 working tree diff，无需手动 fallback
- **优先级**：中（bare 模式不是默认路径，但 bootstrap 用 bare + 用户手动 `--no-worktree` 都会撞；本期靠手动 fallback 兜过；不修的话每次 bare PASS 都得手动构造 diff）

## 2026-04-27 install.sh has_entry() 仅比脚本名不比 matcher

- **触发上下文**：V2.2 收尾时整理「改动同步 checklist」（CLAUDE.md §3 末尾）发现：`install.sh` L82 的 `has_entry(arr, cmd_name)` 只检查脚本名是否在 settings.json 任一条目，**不比对 matcher 字段**。后果：把 hook matcher 从 `Read|Grep|Glob` 改成 `Read|Grep|Glob|WebFetch` 后重跑 install.sh，`has_entry` 看到脚本名已存在直接跳过 → settings.json 仍是旧 matcher → 新增的 WebFetch 永远不被拦截。
- **建议方向**：
  1. `install.sh` `has_entry(arr, cmd_name, matcher)` 加 matcher 参数：脚本名匹配且 matcher 也匹配才视为已存在；matcher 不同视为"需更新"（先删旧条目再 append 新条目）
  2. e2e fixture：`test-install-matcher-update.sh` —— install 一次（matcher=A）→ 改 matcher=B → 再 install → 断言 settings.json 该条目 matcher=B
- **优先级**：中（V2.2 没改 matcher，未触发；未来改 matcher 时会静默失效）

## 2026-04-26 uninstall.sh bl_scripts 列表漏 reviewer-timing-check.sh

- **触发上下文**：V2.2 reviewer 审查发现（pre-existing 老 bug，本期未修按"bug fix 不带周边清理"原则留作 A2 候选）。`uninstall.sh` L49 的 `bl_scripts = ["builder-loop-stop.sh", "tester-lock-write.sh", "tester-lock-check.sh", "tester-lock-clear.sh", "tester-write-guard.sh"]` 列表漏 `reviewer-timing-check.sh`，uninstall 后 settings.json 里该 hook 条目残留，下次 install 重复合并造成 hook 执行多次。
- **建议方向**：
  1. `uninstall.sh` L49 的 `bl_scripts` 加 `"reviewer-timing-check.sh"` 一项
  2. 加 e2e fixture：`test-install-uninstall-roundtrip.sh`——install 后 uninstall 应让 settings.json 完全等于 install 前的状态（diff 必须为空）
- **优先级**：中（uninstall 不彻底导致冗余执行，但不影响功能正确性）
