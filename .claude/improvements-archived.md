# cc-builder-loop 已关闭改进项（Archived）

> 从 improvements.md 迁出的已完成/已被 V3.0 覆盖的条目。保留历史参考价值。

---

---

## ✅ 2026-05-09 V3.0 reviewer-as-gate 已落地，并入 5 条候选

V3.0 「reviewer-as-gate + 文件按 slug 拆 + 多层闸」一波重构（详见 `CHANGELOG.md` V3.0 段）已并入下列 improvements 候选，**视为关闭**：

1. **同 session 多 worktree 第二轮反馈静默丢失**（2026-05-08）→ V3.0 文件按 slug 拆 + cwd 推 slug 自然解决
2. **WIP 节流：单文件 Edit 后发文本就触发提前 auto-commit**（2026-05-08）→ V3.0 L2B 闸（worktree HEAD == last_iter_head + git status 空 → 静默）覆盖
3. **stop hook 跨 session 串扰：A 的 reviewer 触发塞给 B**（2026-05-08）→ V3.0 文件按 slug 拆 + builder cwd 自检自然不串
4. **reviewer 退化为事后建议而非合主线门禁（V3.0 框架级重构）**（2026-05-01）→ V3.0 拆 merge 时机直接落地
5. **AskUserQuestion 期间 stop hook 反复 bootstrap 兜底跑 NOOP loop**（2026-04-29）→ V3.0 L2A 闸（transcript 末是 pending AskUserQuestion → 静默）覆盖

8 个 e2e fixture 覆盖上述场景，全部 PASS。已接入项目 state 文件需重新 setup（不写迁移脚本，老 state 由 hook 检测缺 phase 字段时让 builder AskUserQuestion 决策）。

> **实施偏离 spec 备忘**：spec 写「老 state 缺字段 → AskUserQuestion 阻断让用户决策」，落地改为「stderr warning + 隐式自动升级」。理由：跨 1-2 个版本周期所有已接入项目自动升级，不打断用户工作流；隐式升级语义安全（老 state 经一次 hook 自动写 phase 字段）。

---

## ✅ 2026-05-09 [V3.0 P0 缺口] reviewer-timing-check.sh 还读 active 字段，PASS 后 reviewer 永远 spawn 不出来 — V3.0.1 hotfix 已修

> 已修（CHANGELOG V3.0.1 段，2026-05-09 同日）。修法：reviewer-timing-check.sh 从读 active 切到读 phase（仅 phase=active 拦），缺 phase 时 fallback active=true 兼容 V2.x；deny 时 stderr 写一行避免「No stderr output」误判；新 fixture `test-reviewer-timing-check-phase.sh` 5 case 14 断言接入 PASS_CMD。本仓自身 PASS 后 spawn reviewer 撞主仓旧版 hook 实地验证 P0 必要性（CC 渲染成「No stderr output」复现），走兜底自审通过。

- **触发上下文**：Engineering_Delivery_Bot 项目跑 V3.0 reviewer-as-gate（session f80932fb），builder PASS 后想 spawn reviewer subagent，PreToolUse hook（reviewer-timing-check.sh）直接拦下，CC 渲染成「PreToolUse:Agent hook error: No stderr output」。Builder 只能走兜底自审，整个 V3.0 设计的 reviewer 门禁被绕过。所有已接入 V3.0 的项目都会撞这一条。
- **根因**：`scripts/reviewer-timing-check.sh` L45-46 还在 grep `^active:` 字段做拦截判定，V3.0 加的 `phase` 字段它根本不看。V3.0 时序：
  1. PASS 之前：phase=active，active=true → hook 应该拦（防 reviewer 读旧代码），现有逻辑正确
  2. PASS 之后：stop hook 把 phase 改成 passed_pending_review，但 active 字段不动还是 true（V3.0 设计 active 只写不读做新决策）→ hook 应该放行让 reviewer spawn，**实际仍按 active=true 拦**，exit 2 + deny JSON
  3. builder 看到 hook 报 error 只能走兜底自审，reviewer-as-gate 完全失效
- **CC 渲染坑**：hook 退出码 2 + stdout JSON 是 deny 的标准协议，但 CC 把没 stderr 的 deny 渲染成「hook error: No stderr output」，让人误以为脚本崩了。这是次要观察，不一定要 fix CC，但建议 hook 在 deny 时也往 stderr 写一行明确原因，避免误判
- **建议方向**：
  1. **改 reviewer-timing-check.sh L45-46**：改读 phase 字段，仅在 `phase=active` 时拦（passed_pending_review、空 phase 都放行）
  2. **老 state 兼容**：phase 字段为空（V2.x 老 state）时 fallback 看 active=true 兼容老语义，stderr warning 提示需重新 setup（跟 stop hook L477-481 同款隐式升级套路）
  3. **deny 时 stderr 写一行**：避免「No stderr output」误判，例如 `echo "[builder-loop] reviewer-timing-check: blocked (phase=active)" >&2`
  4. **fixture**：构造「phase=passed_pending_review + active=true」的 state 喂给 hook（带 reviewer subagent_type 的 PreToolUse stdin），断言 exit 0；再构造「phase=active + active=true」断言 exit 2
  5. **同步推「active 字段下掉计划」**（improvements.md L89 那条 [技术债]）：本条是它的活样本，下掉前必须先把所有读 active 的点改完
- **优先级**：高（P0 — 直接卡死 V3.0 主链路 reviewer-as-gate；所有已接入 V3.0 项目活样本必撞；事故已经发生在 Engineering_Delivery_Bot）
- **复现 / 验证**：在已接入 V3.0 的项目跑 loop 到 PASS → 让 builder spawn reviewer subagent → 必看到 PreToolUse hook deny + 渲染成「No stderr output」；改完后同样路径 spawn 应放行

---


---

## 2026-05-08 同 session 串行多 worktree：第二轮 worktree 的 stop hook 反馈完全静默丢失

- **触发上下文**：BOT 项目同一 CC session 内做 B-1「hmi_flash 漏斗埋点」任务，连续两轮 setup-builder-loop.sh：
  - 第一轮 worktree `1778223247-b-1-hmi-flash` 跑 PASS → auto-commit `337dd65` → reviewer 启动 → 用户继续追问发现 stats 脚本还有缺口 → 没退出 builder 模式直接 setup 第二轮 worktree `1778223431-b-1-hmi-flash-stat`
  - 第二轮 worktree 内改完 service/vehicle_ws.py + scripts/hmi_flash_stats.py，PoC 跑通后发文本「等 Stop hook 跑 PASS_CMD」
  - **实际**：stop hook 跑了 PASS_CMD（HEAD 推进到 `519e66e`）+ merge-worktree-back.sh ff merge + 写 reviewer-params.json（含正确 changed_files / report_path / diff_file）+ 清理 worktree（worktree list 里只剩主仓和两个老旧 worktree，第二轮 1778223431-... 已不存在）—— **但 stderr 注入的「请继续 spawn reviewer」反馈消息完全没传到 CC 对话**
  - 用户主动追问"reviewer 和 pass_cmd 呢"才让我意识到 hook 实际跑完了；手动 Read reviewer-params.json + spawn reviewer 才接续上
- **根因（推测）**：CC session cwd 始终在主仓（setup 输出已警告过），同时 git worktree list 里除当前轮还有两个老 worktree（`1776923599-llm`、`1776930498-hmi`）—— 触发 V2.4 多 worktree 绑定策略：「策略 5 仅在唯一 active worktree 时自动绑定；多 active 必须显式 cd 到对应 worktree 才能让 stop hook 跟踪」。但实际 stop hook 仍能找到本轮 state 跑 PASS_CMD + auto-commit（因为 state 文件名带 timestamp），只是**反馈注入这一步**因为多 worktree 状态而被跳过。这与 05-08 早些时候那条「跨 session 串扰」的根因可能同源（hook 不识别 origin session/worktree），但表现是反向的：**该送的没送**而不是送错对象。
- **建议方向**：
  1. **state 文件加 origin_cc_session_id 字段**：与跨 session 那条建议合并，让 hook 能精确匹配「当前 stop 事件的 session 是不是这条 state 的 origin」决定是否注入
  2. **多 worktree 兜底反馈**：即便 V2.4 策略 5 不自动绑定，PASS_CMD 跑完 + auto-commit + 写 reviewer-params.json 后**至少要打一行可见日志或在 hook stdout 里告诉当前 session "PASS at iter N (MERGED)"**——现在是完全静默
  3. **builder.md 提示**：在「检查 loop.yml」段加一条警告：「同 session 内连续 setup 多个 worktree 时，第二轮 stop hook 反馈可能丢失。建议第一轮收尾后退出 builder 模式 / 主动 cd 进新 worktree / 改用新 CC session」
  4. **诊断脚本**：`diagnose-stop-hook.sh` 加一项[7/N]：检测当前 session 是否处于"多 worktree 但 cwd 在主仓"状态，若是则警告
  5. **e2e fixture**：构造同 session 内连续 setup 两个 worktree 的场景，断言第二轮 PASS 后 stop hook 也能注入消息（否则 fixture 红）
- **优先级**：中（同 session 多 worktree 不是常见模式，但一旦发生静默丢失会让用户以为 loop 卡住，浪费上下文/排查时间。频次中）

## 2026-05-08 worktree 内多步改动节流不足：单次 Edit 后发文本就触发 stop hook 提前 auto-commit

- **触发上下文**：B-1 第一轮 worktree（`1778223247-b-1-hmi-flash`）。计划改 3 个文件：handler/card_vehicle_ops.py（7 处日志）+ scripts/hmi_flash_stats.py（PATTERNS + 聚合 + 漏斗）+ service/vehicle_ws.py（pull_file 完成日志）。Edit 完第一个文件后发了一条文本「先 Edit 加 7 处阶段日志」+ 跑 syntax check + 没继续动手，然后 user 给了下一条系统提醒（task tools 提醒），CC 一回合自然结束 → stop hook 立即跑 PASS_CMD（lint/test 单文件改动当然 PASS）→ ff merge 主仓 → auto-commit `337dd65` → worktree 清理。结果：本来计划"3 文件一并交付"的任务被切成"1 文件提前交付"，第二轮要重新 setup-builder-loop.sh 接续做剩下 2 个文件。
- **根因**：builder 工作流没有"WIP 节流"概念。stop hook 触发条件是"CC 一回合自然结束"，不论改动是否完整。Builder prompt 里只在 doc-policy / debug 三禁这种宏观规则，没有「连续 Edit 之间不发文本以免触发 hook」的约束。普通用户写 Edit/Write 天然连续；但 CC 写代码自己习惯每改一个文件就发一段文字解释 + syntax check —— 这种"分阶段汇报"模式在 builder-loop 下变成提前 commit 的风险。
- **建议方向**：
  1. **builder.md 加 prompt**：「在 worktree 内改多个文件时，所有 Edit 调用之间不输出非工具文本；汇报留到所有 Edit 完成后一次性输出。验证（syntax check / PoC）也算工具调用，不会触发；但纯 print 文字会让 CC 一回合 stop」
  2. **stop hook 节流策略**：state 文件加 `min_iter_files_changed` 字段，PASS_CMD 通过但 git diff 改动文件数 < 该值时**不立即 merge**，等下一次 stop 再判定。默认 1（向后兼容），有方案时显式设置如 3。
  3. **打开 explicit "loop done" 信号**：让 builder 显式告诉 hook"我改完了"（发空 commit、touch 个 sentinel 文件、或其他低阻抗信号），hook 看到信号才 merge；没信号则只 PASS 不 merge。
  4. **e2e fixture**：构造"Edit 一个文件 + syntax ok + 中途发文本"场景，断言 stop hook 不立即 auto-commit（除非 builder 显式发完成信号）
- **优先级**：中（不修也能用，但每次"半成品提前 commit"都浪费一次 worktree 建立成本 + 让 git log 多两条 chore(loop) commit 噪音。频次：跨多文件改动的中型任务必现）

---

## 2026-05-08 stop hook 跨 session 串扰：A session 跑完 PASS 把 reviewer 触发指令注入到无关 B session

- **触发上下文**：BOT 项目两个 CC session 并发。A session（slug=`1778223247-b-1-hmi-flash`）跑 builder-loop 改 `handler/card_vehicle_ops.py` 加 hmi_flash 漏斗埋点，14:55:40 PASS_CMD iter 1 通过 → `merge-worktree-back.sh` ff merge 主仓为 commit `337dd65` + 写 `.claude/reviewer-params.json`（含 changed_files / report_path / diff_file）+ stop hook 注入「请继续 spawn reviewer ...」反馈到 stdout。但 stop hook 注入是按"当前被 stop 的 CC 进程"分发的，不识别 origin session —— 这条反馈塞给了**完全无关的 B session（id=`42a64c02`，正在帮用户查飞书私聊聊天记录 + 统计 LLM tool_call 退化日志分布）**，B session 从头到尾零代码改动。B session 主理人识破后拒绝接管（理由：① 自己未改代码 spawn reviewer 没 diff 可审 ② A session 才是 commit 作者应自己跑 reviewer ③ 跨 session 抢 reviewer 与 `cross_session_collaboration_etiquette` 记忆冲突），但 hook 设计层面这条注入本不应到 B 手里。
- **根因**：`merge-worktree-back.sh` 写 `reviewer-params.json` + stop hook 注入消息时**没有 origin session_id 字段**，stop hook 分发逻辑也不做 session 匹配。CC harness 的 stop hook 触发条件是"任意 CC 进程 stop"，所以无论哪个 session 在这一刻刚好结束一轮，都会被 hook 抓到喂同一份反馈。该问题在单 session 项目下不可见；多 session 并发开发同一项目的场景（同一仓 `.claude/` 共享，state 文件全局唯一）会暴露。CLAUDE.md 项目记忆 [worktree_inherits_claude_dir] 和 [cross_session_collaboration_etiquette] 已经隐约触及该模式——这次是"reviewer 触发"环节也撞上了同一类问题。
- **建议方向**：
  1. **reviewer-params.json 加 origin_session_id 字段**：`merge-worktree-back.sh` / `run-pass-cmd.sh` 写状态时把当前 `CC_SESSION_ID`（或 `CLAUDE_SESSION_ID`，按 harness 暴露的环境变量为准）落进 JSON。
  2. **stop hook 注入前匹配 session**：`builder-loop-stop.sh` 读 reviewer-params 的 origin_session_id，与当前 stop 事件的 session_id 比较，不匹配则**不注入**（仍可保留状态文件，等正确 session 下次 stop 再触发）；或 fallback 行为是把消息改写成「检测到 A session（id=xxx）的 reviewer 等待 spawn，但你不是它。如有需要，请到对应 session 接续；本 session 忽略即可」让 B session 主理人不必每次都自己识破。
  3. **state schema 同步加字段**：所有 builder-loop 产出的状态文件（worktree state、reviewer-params、reviewer-diff、loop iteration log）都带 origin_session_id，便于事后审计 + 跨 session 防撞。
  4. **e2e fixture**：构造两个 CC session 并发场景，session A 跑 PASS_CMD 通过 → session B 同时也在做不相关查询任务并刚好 stop → 断言 stop hook 不会把 A 的 reviewer 触发消息塞给 B。
  5. **文档/SKILL.md 提示**：在 README 或 troubleshooting 加一节「多 session 并发开发同仓时的注意事项」，列出已知串扰点（reviewer 触发 / state 文件冲突 / worktree 互相可见但内容不同步）。
- **优先级**：中（不修不会有数据损坏，但会浪费 B session 上下文 + 在 reviewer-as-gate 的 V3.0 落地后变得**更危险**——届时 B session 若没识破而盲目跑 reviewer，可能误把 A 的 commit 给 ff merge / cleanup，破坏 A session 的工作流。频次随多 session 协作增加而上升）

---



## 2026-05-01 reviewer 退化为事后建议而非合主线门禁，PASS_CMD 通过即 ff merge 让 reviewer 阻塞级反馈难撤回（V3.0 框架级重构）

- **触发上下文**：cc-builder-loop V2.6 Phase 1（abandon-loop.sh 出口）落地 session。Loop iter 1 PASS → `merge-worktree-back.sh` 自动 auto-commit + ff merge + cleanup → 此时 commit `2934b15` 已在主线 → 才被 stop hook 通知 builder spawn reviewer 看 commit。Reviewer 反馈 `🔴 0 / 🟡 4 / 🔵 4`（虽然不到阻塞级但都是实质改动 — read_field 缺 strip / reason 换行污染 / 注释与实现不符 / SKILL+README V2.6 段缺失），builder 走新 commit `60c950a` 修。本次侥幸没 🔴 阻塞级；下次若 reviewer 发 🔴 阻塞级问题，已 merge 难撤回（要 git revert，破坏主线 history）。用户在收尾时质疑"理论上 reviewer 通过才能进 commit 环节吧"，明确了**当前流程让 reviewer 退化为事后建议而非合主线门禁**的设计缺陷。
- **根因**：V1.6 修 worktree 数据丢失 bug（git worktree remove --force 静默吞掉 uncommitted 改动）时把「worktree auto-commit + ff merge 主干 + cleanup worktree + drop state」绑成 `merge-worktree-back.sh` 一个原子动作，没有"留 worktree 等 reviewer"的中间态。Reviewer 又是 PASS_CMD 通过后由 stop hook 通知 builder spawn 的——时序天然在 merge 之后，**reviewer 永远只能看已合主线的代码**。
- **建议方向**（V3.0 框架级重构，**改造规模 ≈ V2.6 全部 Phase 之和**）：
  1. **拆原子动作**：`merge-worktree-back.sh` 拆为 `worktree-commit-only.sh`（PASS 后调，仅 worktree 内 git add+commit 不动主仓 / 不 ff merge / 不 cleanup）+ `merge-and-cleanup.sh`（reviewer 通过才调，做 ff merge + cleanup worktree + drop state）
  2. **state schema 加中间态字段**：`phase: active | passed_pending_review`（默认 active；worktree-commit-only 后 stop hook 写 passed_pending_review；reviewer 通过调 merge-and-cleanup 时直接 drop state 不留 phase=passed_merged 防僵尸）
  3. **builder.md 流程改造**：PASS 后强制 spawn reviewer；🔴 阻塞 → 用户决策（修复回 PASS_CMD 新一轮 iter / abandon-loop 放弃）；🟡 / 🔵 非阻塞 → builder 修 → worktree 新 commit → 重 spawn reviewer 复审；0 🔴 → 调 merge-and-cleanup 才合主线
  4. **reviewer-timing-check.sh 语义反转**：从「loop 活跃禁止 spawn reviewer」改为「state.phase=active 禁止 spawn / state.phase=passed_pending_review 必须 spawn」
  5. **locate-state.sh / stop hook 适配** passed_pending_review 态：stop hook 看到此态**不再跑 PASS_CMD**（已通过），仅识别 reviewer 反馈喂回；多 passed_pending_review 并存允许（多 worktree 等审）
  6. **abandon-loop.sh 兼容**：passed_pending_review 态下用户也能 abandon（reviewer 阻塞 + 用户不想修），本期 V2.6 Phase 1 abandon-loop.sh 已支持 active=true state，新增需识别 phase=passed_pending_review 也允许 abandon
  7. **e2e fixture**：`test-reviewer-as-gate.sh` 覆盖 PASS → reviewer 通过 → merge / PASS → 🟡 → builder 修 → 复审通过 → merge / PASS → 🔴 → 用户修走新 PASS_CMD 轮 / PASS → 🔴 → abandon / 多 passed_pending_review 并存
  8. **配置项可选保守模式**：`loop.yml.merge_gate: pass | reviewer`（默认 reviewer，激进项目可设 pass 退回 V2.6 行为）
- **代价**：状态机多一态 + worktree 保留期变长 + 多 worktree 并存概率提高 + reviewer-timing-check.sh 语义反转 + 跨脚本协议 breaking change（拆 merge-worktree-back.sh）。需迁移所有已接入项目的 state schema。
- **好处**：reviewer 真成合主线门禁 + 阻塞级反馈不再污染主线 + commit history 干净（只有 reviewer 通过的代码进主线）+ 与人类 PR review 文化对齐
- **优先级**：高（reviewer 退化为事后建议是当前架构核心缺陷；V3.0 主题级重构；V2.6 全部 3 个 Phase 收尾后启动）
- **复现**：装了 builder-loop 的项目，让 loop 跑 PASS_CMD 通过 → 看到 commit 已自动合主线 → spawn reviewer → 模拟 reviewer 报 🔴 → 此时 commit 已在主线难撤回。本 V2.6 Phase 1 reviewer 报 🟡 4 没到 🔴 是侥幸。



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


