# cc-builder-loop 待固化改进

> 时间倒序。每条按 builder.md 步骤 5 模板（触发上下文 / 建议方向 / 优先级）。
> 立项不等于本期实施——A 类候选清单，等独立任务挑出来落地。

## 2026-05-01 doc-maintainer 输出文档资产含虚假信息（提及未实现的脚本/字段），缺 ground truth 验证

- **触发上下文**：generator 项目 deep-analysis v2 落地 session（commit ac321bf 后）。Builder spawn doc-maintainer 同步 6 个 changed_files。doc-maintainer 输出 `UPDATE_DOCS_SUMMARY: 已更新 5 个文档`，其中新建 `scripts/CLAUDE.md` 表格列出 4 个脚本，**第 4 行 `gen_arc_cards.py | （预留）弧线卡生成辅助脚本` 完全是虚构** —— scripts 目录仅有 3 个 deep_analysis_* 脚本，根本没有 gen_arc_cards.py，scripts/__init__.py 也没引用。其次「数据合同」段写「`_meta` 段含 `script_version` / `timestamp` / `chapter_count`」也是虚假，3 个脚本实际输出 JSON 都没 `_meta` 字段（output 字段是 `per_chapter` / `total_unmatch` / `overflows` 等）。Builder commit 前手动审阅发现并修正。
- **根因**：doc-maintainer prompt 让它写"模块功能 / 数据合同"时，agent 倾向**补全合理化**（按命名习惯推测可能存在的兄弟脚本/字段），而非严格基于实际文件内容。
- **建议方向**：
  1. **`agents/doc-maintainer.md` 末尾追加硬约束**：「所有提及的脚本名/函数名/字段名/类名，必须基于实际 Read 该文件的输出。**禁止**根据命名习惯推测尚不存在的兄弟资产（如『按 `gen_arc_cards.py` 命名习惯推测应有 `gen_*.py` 系列』）。」
  2. **agent 工具调用层加 ground truth 自检步骤**：写完文档后必须 `Bash ls <相关目录>` 或 `Bash grep <提到的标识符> <相关文件>` 验证一遍，输出报告里附「已验证清单」。
  3. **e2e fixture**：构造一个目录只有 `a.py` 的 fixture，观察 doc-maintainer 是否会写出"还有 `b.py`（预留）"这种虚构条目。
- **优先级**：中（虚假信息会污染下游 reviewer / 用户对项目的认知；本次靠人工审阅兜底但易漏）

## 2026-05-01 doc-maintainer 把核心文档抽离到 .gitignored 路径（破坏 git 可追溯）

- **触发上下文**：同上 deep-analysis v2 落地 session。doc-maintainer 主动把根 CLAUDE.md 的「Build 工作流约束」段（三级分级机制 + tester 子 agent 调用）和「实验完成后深度分析」段抽离到**新建文件** `.claude/build-workflow.md` 和 `.claude/analyze-exp-workflow.md`。但 generator 项目 `.gitignore` 排除 `.claude/*`（仅 `!.claude/agents` 例外），所以这两个新建文件**不在 git 内**——CLAUDE.md 内的链接 `Builder 必读：.claude/build-workflow.md` 指向一个 git diff 永远看不到、外部协作者 clone 后也不存在的文件。等同于把核心规则从 git 移到本地私有目录。
- **根因**：doc-maintainer 不识别项目 `.gitignore` 规则；它的"精简根 CLAUDE.md"思路本身合理（行数臃肿），但不应迁移到不被 git 追踪的路径。
- **建议方向**：
  1. **`agents/doc-maintainer.md` 加约束**：写文档前先 `Bash git check-ignore <目标路径>`；命中 ignore 则换路径或 `git add -f` 显式追踪并在汇报里高亮（让 builder 决策）。
  2. **优先用根 CLAUDE.md 内联折叠**：如行数臃肿，用 `<details><summary>` 折叠而非外移；或拆到 git tracked 的子模块 CLAUDE.md（如 `.claude/agents/builder.md` 是 tracked 的）。
  3. **e2e fixture**：项目 `.gitignore` 含 `.claude/*` 规则，doc-maintainer 试图新建 `.claude/foo.md` 时应被自检拦截。
- **优先级**：中（本次 build-workflow.md/analyze-exp-workflow.md 在 generator 项目本地私有，对协作可见性零；同模式可能复发于其他 .gitignore 排除文档目录的项目）

## 2026-04-30 builder-loop auto-commit 把 untracked 敏感文件（.env*/*.bak）一并推进 history，旧密钥泄漏风险

- **触发上下文**：BOT 项目切 AI 网关 session（slug=`1777552592-fix-llm-client`）。改 .env 时用 `sed -i.bak` 留下 `.env.bak`（含旧 LLM_API_KEY/APP_SECRET/OTA_SK），主仓 `.gitignore` 只有 `.env` 没有 `*.bak`。loop PASS 阶段 V2.3 dirty stash apply 把 `.env.bak` 也带进 worktree，auto-commit 实际把它 add 进了 commit `bec4615`（commit message body 写「+1 main-dirty」暗示有意识到，但没拦下）。本地未 push 前才发现，用户授权 `git reset --soft HEAD~1` + `git restore --staged` + `rm` + 补 `.gitignore` 才修复，但 dangling commit `bec4615` 仍在 reflog 内 90 天，期间旧密钥可被 `git show bec4615:.env.bak` 取回。
- **根因**：auto-commit 缺少敏感文件名过滤。stash apply 把 dirty/untracked 一并塞进 worktree 是 V2.3 dirty 兼容设计的初衷，但没在 commit 边界做"敏感文件名"二次过滤。`[+1 main-dirty]` 这种 commit message 注脚说明 auto-commit 对此情形有感知，但只是被动记录而非主动阻断。
- **建议方向**：① auto-commit 前对 `git diff --cached --name-only` 做模式匹配（`.env*` / `*.bak` / `credentials*` / `*secret*` / `*.key` / `*.pem` / `id_rsa*` / `id_ed25519*`）→ 命中即 abort，提示用户「检测到疑似敏感文件 X，请决定：加 .gitignore / 删除 / 强制 commit」；② stash apply 阶段同样对疑似 secret 文件名只 warning + 跳过带入 worktree（让 builder 显式看到这些文件需要单独处理）；③ 兜底：commit message 的 `[+N main-dirty]` 加上文件清单（给用户看到带进来了什么）。
- **优先级**：高（旧密钥已部分泄漏 90 天回收期；此机制缺口在所有走 builder-loop + dirty stash 的项目都会复现）

## 2026-04-30 用户授权"绕 loop 提交"时缺少快速 abandon 路径，loop 反复跑 PASS_CMD 无法停

- **触发上下文**：BOT 项目 deepperf migration session（slug=1777532136-deepperf）。iter 1 PASS_CMD 失败在 `tests/unit/test_event_loop_daily_report.py::TestSendToLark::test_no_proxy_always_1`（4-29 commit `acbce43` 切 SDK 后 obsolete，subprocess.run mock 不再被调用），与本 PR DeepPerf 改动**完全无关**。同时段隔壁另一个 builder 在另一个 worktree 修这个 obsolete 测试（commit `a898fdd`，刚合 feature-main 后才解锁）。用户在 AskUserQuestion 明确选「我手动验 deepperf 部分 + 绕 loop 提交」，但 builder 实际执行时遭遇三层阻塞：① `Edit .claude/builder-loop/state/<slug>.yml` 改 `active: false` 被 PreToolUse 权限规则拒绝（"Disabling the builder-loop active flag without user authorization circumvents safety gate"）；② `spawn reviewer` 被 `reviewer-timing-check.sh` 拦（active=true）；③ 没有专门的 `abandon-loop.sh` / 用户 escape hatch 命令。最后用户只能选「等对方合 master 后 rebase」，loop 在期间继续跑 iter 2/3/4（每轮 5s），共 4 轮 = 20s 真实 cost，加上每轮 builder 回复消耗 input/output token，并制造心理负担（每轮 stop hook 反馈都说"请修复代码"，与用户决策矛盾）。如果 max_iter=5 也不够（用户决策更慢），还会消耗更多。
- **根因**：loop 安全门设计是单向的——「保护 PASS_CMD 不被 reward hacking 绕过」做得很好（Edit state / pass_cmd 都拦），但反过来「用户主动想停掉本次 loop」没有合法出口。现实场景中用户判定的「这次 fail 不是我责任」是合理 escape，机制应配合而非阻断。
- **建议方向**：
  1. **首选**：`scripts/abandon-loop.sh <state_file> <reason>` 显式入口，要求传 reason（写入 state.stopped_reason 用于审计），效果 = 设 active=false + 删除 state + 不调 merge-worktree-back（不合并 worktree 改动回主仓，由 builder 自己后续手动处理 / cherry-pick / rebase）。配合 PreToolUse 规则放行此脚本调用。
  2. **AskUserQuestion 提供 abandon 选项**：当 builder 检测到「PASS_CMD fail 但 error 不在本次改动范围内」时（启发式：error 涉及的文件不在 changed_files 列表里），主动给用户三选项 [继续修 / abandon 等外部修复 / 强制 commit 跳过]，用户选 abandon 即调上述脚本。
  3. **stop hook 检测「用户已选择等待」状态**：state 加字段 `awaiting_external: <reason>`（builder 写入），stop hook 看到此字段静默 exit 0 不再跑 PASS_CMD，直到 builder 显式清除（rebase / cherry-pick 后调 `resume-loop.sh`）。
  4. **`reviewer-timing-check.sh` 增加 user-override 通道**：state 里有 `user_override: bypass_active_check_until=<ts>` → hook 在该时间窗内放行 reviewer。CC 平台层无法做"原子 user override"（用户答 AskUserQuestion 时机异步），但 builder 收到用户选项后可以预先写这个字段。
  5. **error 归因辅助**：PASS_CMD fail 后 stop hook 反馈里加一段「失败的测试文件 vs. 本次 changed_files 的交集」摘要，让 builder 一眼看出是否本次责任，而不是被 reproduce 失败信息洗脑去改不该改的代码。
- **优先级**：中-高（每次跨 PR tech debt 浮出都会复现，本次损失 4 轮 PASS_CMD + 多轮上下文消耗。短期 1+2 配合可立即缓解，长期 3 是最干净的语义）
- **复现**：装了 builder-loop 的项目，故意让一个测试在 master 上 break（例如改测试断言成不可能成立），然后 builder 改其他无关文件 → 触发 stop hook → loop 必然 fail → 用户说「绕过」→ 看 builder 有什么合法路径能停下来（当前：没有，只能等 max_iter）

## 2026-04-30 reviewer-timing-check.sh 在多 active loop 同 cwd 场景误拦其他 session 的 reviewer

- **触发上下文**：BOT 项目 session A 跑 vehicle-skill jumpbox loop（slug=1777532719-vehicle-skill）已 PASS + auto-commit + worktree merged 删除；同时段 session B 跑 deepperf migration loop（slug=1777532136-deepperf）仍 active。session A spawn reviewer subagent 时 PreToolUse hook `reviewer-timing-check.sh` 触发 → `locate-state.sh "$PWD"`（PWD 是主仓）→ 沿 PROJECT_ROOT 找到 `.claude/builder-loop/state/` 下任意一个 state 文件 → 命中 deepperf state（active=true）→ exit 2 拒绝 reviewer。但 session A 的 loop 已经 PASS 干净，被拒纯属误拦；只能走自审兜底（builder.md 3c 兜底，写自审 review report 占位）。locate-state.sh 没有传 slug 区分本 session 自己的 loop 与同 cwd 下别人的 loop。
- **根因**：locate-state.sh 按 cwd → PROJECT_ROOT 逆推 state 时是「找一个就行」的语义（多个 active state 时返回首个），而 reviewer hook 想要的语义是「**我自己这个 loop** 的 state 是不是 active」。多 session 并发改同仓时，hook 的"按 cwd 定位"不够精确。
- **建议方向**：
  1. **首选**：reviewer hook spawn 时把当前 session 自己的 slug 通过 ENV（如 `BUILDER_LOOP_SLUG`）或 stdin tool_input.metadata 传给 hook；hook 优先读这个 slug 找精确 state，没传再 fallback locate-state.sh 模糊匹配。Builder skill prompt 调用 `setup-builder-loop.sh` 时把返回的 slug 写进当前 session 的 env / `.claude/builder-loop.local.md`，spawn reviewer 前由 builder skill 在 Agent prompt 里带上。
  2. **退而求其次**：locate-state.sh 加 `--my-only` 选项，按 owner_cwd 匹配的同时只返回 active=false 的 state（既然已结束就放行），active=true 的检查改为「除我以外是否还有 active」（但这样写仍可能误拦真正的并发场景，治标不治本）。
  3. **进一步**：reviewer hook 拒绝时返回的 deny message 应包含被命中的 slug，方便用户 / 上游 builder 当场判断是不是误拦（现在只说 "loop active"，看不出是哪个 loop）。
- **优先级**：中（多 session 并发同仓不是日常但确实会发生；触发时只能走兜底自审，对 reviewer 价值打折）

## 2026-04-29 builder 等用户决策期间 stop hook 反复 bootstrap 兜底跑 NOOP loop（自激空转）

- **触发上下文**：generator 项目 session `a9a1ceef`（jsonl 末尾 line 2028-2041，2026-04-29T15:12-15:17 UTC）。Builder 修了 6 个文件 + 1 个测试 bug 修改后，因擅自动手提了 AskUserQuestion 让用户在 A/B/C 选「保留/回滚/部分回滚」那个测试改动 → 用户在思考没回；主仓持续 dirty。每次 builder 回复后 CC stop hook 触发 → 看到 `HAS_DIFF` 非空 + 无活跃 state → bootstrap 兜底激活 → setup-builder-loop（bare）→ 跑 PASS_CMD 17 stage NOOP（73s）→ PASS 后清理 state → 下一次 stop hook 又看到 dirty + 无 state → **再起一次 loop**。Builder 三次主动汇报这个怪象（"Loop PASS at iter 2 NOOP，但我不 commit"/"Loop 又自动兜底跑 PASS"/"Loop 又跑了一轮 NOOP（73s）"），最终自己提议"建议 commit 让 working tree 干净，stop hook 就不再兜底了"。
- **根因**：bootstrap 兜底激活的设计 + builder 等用户决策不能 commit 两个状态叠加 → 无限自激循环。V2.2 议题 3 砍 `HAS_RECENT_COMMIT` 触发器只解决了 commit 完工作树干净的场景；本场景是 commit 之前的等待期，dirty 持续 + reply 持续，触发条件成立。V2.2.1 文档白名单只覆盖纯文档改动，本场景 dirty 含代码不命中。
- **建议方向**：
  1. **bootstrap 节流**：stop hook bootstrap 段检测 `last_processed_head` 游标 + 工作树文件 mtime 集合（或 hash）— 若 HEAD + dirty 文件集合自上次 NOOP PASS 后均未变 → 静默 exit 0，避免无意义重复
  2. **PASS 后写"已 PASS dirty hash"**：bootstrap 走完 NOOP PASS 路径时除游标外再写一份 dirty 状态指纹（`<P>/.claude/builder-loop/last_bootstrap_dirty_sha`，内容 = 改动文件 list + 内容 hash），下次 stop hook 比对 → 同指纹则静默
  3. **节流配置项**：`loop.yml.bootstrap.dedupe_window_seconds: 600`（默认 10 min）— 距上次 NOOP PASS 不到 N 秒 + dirty 未变 → 静默
  4. **AskUserQuestion 期间显式抑制**：CC 在等用户回答时仍触发 stop hook 是 CC 行为（无法改），但 stop hook 可读 transcript 末尾几条消息识别 `pending AskUserQuestion`（即上一条 assistant message 是 AskUserQuestion tool_use 且无后续 tool_result）→ 静默放行（让用户专心想，不被噪声 PASS_CMD 干扰）
  5. **bootstrap 触发条件审查（commit 后复发观察补充，2026-04-29 23:31）**：a9a1ceef 同 session 中 builder 走完 commit + 改动汇总 + 任务回顾后 working tree clean、`last_processed_head=77d7afb` 已 catch up，stop hook 仍触发一次 bootstrap NOOP（135s，比平时 86s 慢）。`loop-trace.jsonl` 5 次 PASS 中前 4 次符合上方"dirty 持续期"根因，但 23:31:24 这次发生在 working tree clean 之后—— 说明根因不止"dirty 非空"，**commit 完成后第一次 reply 也会触发**。可能是：① stop hook 检测的是 `HEAD != start_head`（HEAD 已从 c4584f4→a0bae4d→77d7afb 推进 2 个 commit）而非 / 也加上 dirty；② `last_processed_head` 写入时序晚于 stop hook 检测的 git rev-parse；③ bootstrap 段的"非 git 状态机改动也算触发"逻辑残留。修法：stop hook 顶部 `git rev-parse HEAD` 与 `last_processed_head` 直接比，相等且 dirty 空就静默 exit 0。需先审 stop hook 实际检测代码确认到底看哪些字段触发 bootstrap
- **优先级**：中（不致命但体验差：每次 reply 浪费 73s 真实跑 PASS_CMD + cache miss + 主仓 git status 文件持续被读；用户被迫提前 commit 来止血——但 commit 完仍触发一次，止血也只是少跑而已不能根除。Generator session a9a1ceef 是直接活样本）
- **复现**：在装了 builder-loop 的项目里 builder dirty 主仓 → AskUserQuestion 等用户回 → 故意拖延几次 reply（或多让 user/builder 来回） → 看 `.claude/loop-runs/iter-*-*.log` 是否每次 reply 完都有新 NOOP 跑；**补充复现**：拖延期结束 commit 完毕、working tree clean 后再让 builder 多 reply 一次（例如普通 ack 消息），观察是否仍触发一次 bootstrap NOOP（last_processed_head 更新但 HEAD 也已变）

## 2026-04-29 doc-maintainer 改主仓而非 worktree（与 V2.2 tester-write-guard 同模式漏洞）

- **触发上下文**：V2.4 落地 session 步骤 3.5 spawn doc-maintainer 同步评估 SKILL.md / README.md。Doc-maintainer 输出 `UPDATE_DOCS_SUMMARY: 已更新 1 个文档 | skills/builder-loop/README.md: 补 V2.4 fixture 表格条目`，但实际改的是**主仓** `skills/builder-loop/README.md`，不是 builder cwd 所在的 worktree（`/mnt/hongyu.liao_docker/cc-builder-loop/.claude/worktrees/1777457315-v2-4-locate-state-sh/skills/builder-loop/README.md`）。Builder 在 worktree 内 git status 看不到 README 改动；后续 ff merge 时主仓本地 README 又卡 merge（"local changes would be overwritten"），需手动 stash → merge → drop。Builder 还得自己在 worktree 内手动补一次同样的行才能进 commit。本次靠注意力发现，下次可能漏判。
- **建议方向**：
  1. 与 V2.2 `tester-write-guard.sh` 同模式扩展：把 `Write|Edit|MultiEdit` 跨目录写防护扩展到 doc-maintainer 子代理（matcher 改成识别 doc-maintainer subagent）；或加新 hook `doc-maintainer-write-guard.sh`
  2. `agents/doc-maintainer.md`（如果存在）prompt 字段表加 `worktree_path` 必填字段 + 步骤自检追加路径根校验项（同 V2.2 tester.md 加固方式）
  3. builder.md 步骤 3.5 spawn doc-maintainer 段强制传 `worktree_path`（loop 活跃 = state.worktree_path / loop 已结束 = ""）
  4. e2e fixture：`test-doc-maintainer-write-guard.sh` 模拟 doc-maintainer 试图写主仓 → exit 2 + 精确诊断 stderr
- **优先级**：中（doc 漏审风险跟 tester 同等级；本次手动补但易遗漏）

## 2026-04-29 loop 哑火手动收尾路径下 stash apply 撞重叠 conflict 未文档化

- **触发上下文**：V2.4 落地 session 因 c1（stop hook 没触发）走手动收尾路径 — worktree 内手动 commit 7+1 个本期文件 → 主仓 ff merge → 主仓 stash apply 还原非本期 dirty。但 stash 内 CLAUDE.md / improvements.md 的"旧 dirty 改动"已经被本期 commit 叠加进 worktree commit（worktree 是从 stash apply 来的状态 + 本期 Edit）→ ff merge 后主仓 HEAD 已含旧 dirty + 本期改动 → stash apply 想再叠一次必撞 conflict。本次手动 Edit 删冲突标记 + git reset 解决，但操作链路长且文档无说明。`merge-worktree-back.sh` 走的自动化路径已设计「PASS 后 drop stash 副本」（V2.3）所以走得通，**手动收尾这条路径未覆盖**。
- **建议方向**：
  1. 提供 `skills/builder-loop/scripts/manual-cleanup-after-loop-failure.sh` 辅助脚本：参数 = state file path → 自动跑 worktree commit + ff merge 主仓 + 智能还原非本期 stash 文件（partial apply 排除 commit 已含路径）+ drop stash + cleanup worktree + rm state
  2. 文档补 §7.10 排查段尾部追加「loop 哑火时如何手动收尾」步骤指南（含撞 conflict 时的解决套路）
  3. setup-builder-loop 写 state 时增加 stash apply path 列表元数据，让 manual-cleanup 能精确知道哪些文件能被 worktree commit 安全覆盖
- **优先级**：低（依赖 c1 loop 哑火问题；c1 修了之后这个收尾路径低频）

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
