# cc-builder-loop 待固化改进

> 时间倒序。每条按 builder.md 步骤 5 模板（触发上下文 / 建议方向 / 优先级）。
> 立项不等于本期实施——A 类候选清单，等独立任务挑出来落地。

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

## 2026-05-09 [V3.0 P0 缺口] reviewer-timing-check.sh 还读 active 字段，PASS 后 reviewer 永远 spawn 不出来

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

## 2026-05-09 [A2] spec 偏离实施时强制标注 — DEVIATION_FROM_SPEC 协议

- **触发上下文**：V3.0 主期 spec 写「老 state 缺 phase 字段 → AskUserQuestion 阻断让用户决策」，实施时改成「stderr warning + 隐式自动升级」（更平滑），reviewer 不知道偏离动机当 🔴 报。Builder 自主回复采纳/拒绝时多消耗一轮上下文反驳。频次：每次 builder 实施时遇到 spec 设计过激进 / 与现实冲突的场景都可能踩。
- **建议方向**（机制化代替"靠 builder 自我习惯"）：
  1. **builder.md 加 prompt**：「实施偏离 spec 时必须在 commit message body 加一段 `DEVIATION_FROM_SPEC: <reason>`」，落地 `~/.claude/commands/builder.md`（dotfiles 改动）
  2. **reviewer.md 加 prompt**：「评审前 scan commit message body 找 `DEVIATION_FROM_SPEC:` 标记；命中条目视为有意识决定，不当 bug 报；如不认同偏离 reason 单独提"建议回退到 spec"而非 🔴 阻塞」
  3. **fixture**：构造一个 commit message 含 DEVIATION_FROM_SPEC 标记的场景，断言 reviewer 输出不含该条目的 🔴
  4. **plan.md / spec 文件可选**：加「实施偏离记录」段，让方案 review 阶段就能识别"哪些 spec 项实施时大概率会偏离"
- **优先级**：中（机制比 builder 自我纪律更稳；每次 V3.x / V4.x 大改造都该用）
- **复现**：构造一个 spec 与现实有冲突的小任务（如 spec 写"严格阻断"实施改"warning 兜底"），看 reviewer 是否还会当 🔴 报

---

## 2026-05-09 [A2] planner 方案模板强制「老路径调用方清单」段

- **触发上下文**：V3.0 拆 `merge-worktree-back.sh` 为 commit-only + merge-and-cleanup 时，漏 grep 全仓调用方，结果 `run-apply-arbitration.sh` 仍调老脚本，arbiter 续路径绕过 reviewer-as-gate（reviewer 反馈 🟡 抓到）。Builder 改造大架构时需主动 grep 全仓"老路径调用方"清单——但当前没有机制强制做这件事，只能靠 builder 自觉。
- **建议方向**（机制化代替"靠 builder 自我习惯"）：
  1. **planner.md 方案模板加段**：「老路径调用方清单」必填——任何架构改造（拆脚本、改接口、废弃文件）方案在「文件地图」段后加新段，列出所有调用方（用 `grep -l <旧路径> -r` 输出截图证据），逐个标注「迁移 / 兼容 / 已知技术债」，落地 `~/.claude/commands/planner.md`（dotfiles 改动）
  2. **builder.md 加 prompt**：「读到方案文件含「老路径调用方清单」段时，进 builder 模式后第一动作是逐项验证迁移 / 兼容 / 标债，不能跳过任何一条」
  3. **fixture**：构造一个方案文件无该段的场景，断言 builder 启动时 stderr warning「方案缺老路径调用方清单」（不阻断但显眼提示）
- **优先级**：中（架构改造频次低但单次漏改成本高，比 V3.0 arbiter 缺口立项条目更上一层 — 那条是结果，这条是预防机制）
- **复现**：开新方案做架构改造任务，看是否有这一段；没有则规划 / 实施期容易漏调用方

---

## 2026-05-09 [A2] schema 字段变更强制「老 state 兼容 fixture」类别

- **触发上下文**：V3.0 加 `phase` / `last_iter_head` / `cleanup_phase` 三个新字段，hook L1 闸初版漏写「state 缺 phase 字段」的处理（PHASE_FIELD 为空 fall-through，reviewer 反馈 🔴 抓到）。每次 schema 字段变更都该考虑「老数据缺该字段时走什么分支」——但当前没机制强制。
- **建议方向**（机制化代替"靠 builder 自我习惯"）：
  1. **fixture 框架扩展**：新增 `test-state-schema-old-data-compat.sh` 总入口 — 读 SKILL.md「状态文件 schema」段所有字段名，逐字段构造「state 缺该字段」的场景跑 hook，断言结果是「降级 / warning + 自动升级 / 显式 abort」三选一明确（不允许默默 fall-through）
  2. **builder.md 加 prompt**：「改 setup-builder-loop.sh / merge-* / hook 等读 state 字段的脚本前，先看 schema 字段是否有新增；新增字段必须先在 `test-state-schema-old-data-compat.sh` 加对应断言才能 commit」
  3. **CI hook**（可选）：commit-msg / pre-push hook 检测 SKILL.md 「状态文件 schema」段字段数量变化，强制要求对应 fixture 也增加断言数量（非严格匹配但量级一致）
  4. 与 [A2] 上面「fixture 验证 SKILL.md schema 与代码一致」条目同源 — 可合并实施
- **优先级**：中（每次 schema 演进都该跑；本期 V3.0 是个活样本）
- **复现**：往 SKILL.md schema 段加一个新字段不更新 fixture，看是否有报警

---

## 2026-05-09 reviewer 长 diff 评审 prompt 加约束 + fixture 验证 SKILL.md schema 与代码一致

- **触发上下文**：V3.0 reviewer-as-gate 主期 reviewer 反馈（🔵 2 条误读 + 🔴 1 条 schema 漂移）。
  1. **reviewer 误读**：reviewer 在 2787 行 diff 里给了 2 条误读建议——「commit message 没含 slug」（实际已用 task_description 构造）+「merge-and-cleanup.sh 用 exec 调 arbiter trap 失效」（实际没有 exec 调用）。reviewer 凭片段推测整文件，没 Read 完整代码段。
  2. **schema 漂移**：SKILL.md 里 reviewer_files 注释写 YAML list 形式（如 `[<files>]`），但代码生成的 state 里实际是 comma-separated string `"a.py,b.py"`。文档与代码漂移，下游解析逻辑会撞类型不一致。
- **建议方向**：
  1. **reviewer.md prompt 加约束**（dotfiles 改动）：「凡涉及具体代码位置（如 X 文件 L<n> 用 exec / hardcode 字符串等）的论断，必须先 Read 该位置完整段（≥10 行上下文）验证再下结论；不允许凭 diff 片段推测」。落地路径：`~/.claude/agents/reviewer.md`。
  2. **fixture 验证 SKILL.md schema 与代码一致**：新增 `test-skill-md-schema-consistency.sh` — 跑 setup-builder-loop.sh 生成实际 state 文件，按 SKILL.md「状态文件 schema」段示例字段 grep state 文件验证类型一致（YAML list vs string、字段名拼写、字段必备性）。每次 SKILL.md schema 变更后跑一次。
  3. **可扩展**：fixture 框架支持「schema 字段对照表」机制，未来加新字段时附带 schema-consistency 测试断言条目。
- **优先级**：中（不修不出严重事故，但 reviewer 误读会浪费 builder 反驳成本，schema 漂移会让下游解析撞 bug。频次中等：每次新加 schema 字段或 reviewer 处理 >2000 行 diff 时都可能触发）
- **复现 / 验证**：grep 全仓 SKILL.md 里 state schema 字段示例与代码生成的字段对照；reviewer 长 diff 评审任务找一次 fixture 测 prompt 约束生效

---

## 2026-05-09 [V3.0 缺口] arbiter 续路径迁移到 reviewer-as-gate

- **触发上下文**：V3.0 reviewer-as-gate 落地 reviewer 反馈（🟡）。`run-apply-arbitration.sh` 在 rebase 冲突由 arbiter 解决后调 `merge-worktree-back.sh`（V2.x「立即合」路径），commit 直接 ff 进主线，**跳过 reviewer gate**。冲突解决场景下 reviewer 看不到合并后的代码，无法发挥门禁作用。本期保留这条 V2.x 路径是为了不破坏 `test-conflict.sh` 等 fixture，但属于 V3.0 落地的已知缺口。
- **建议方向**：
  1. **首选**：`run-apply-arbitration.sh` 改调 `merge-and-cleanup.sh`（V3.0 拆 merge 路径，先 commit-only 再等 reviewer 才 ff merge）。Arbiter 解决冲突后 worktree 内仍有干净 commit，进 phase=passed_pending_review 等审。
  2. `merge-worktree-back.sh` 退化为纯 bare 模式入口（worktree 模式分支删除）。
  3. fixture 适配：`test-conflict.sh` 把"arbiter 完成立即合主线"断言改成"arbiter 完成进 passed_pending_review、reviewer 通过才合主线"。
  4. 跨 arbiter 重试场景验证：arbiter 第二次解决冲突 → 仍走新 gate 路径不重复合主线。
- **优先级**：中（不修不会出严重事故，但 reviewer-as-gate 的核心防御在冲突场景下被绕过；rebase 冲突频次中等）
- **复现**：本仓 `test-conflict.sh` 跑通即可复现 — arbiter 解决冲突后看 commit 是否在主仓 HEAD 上（跳过了 reviewer gate）

---

## 2026-05-09 [技术债] active 字段下掉计划

- **触发上下文**：V3.0 reviewer-as-gate 引入 `phase` 字段作为 hook 主判信号源，但向后兼容保留了 `active: true / false` 字段。新写代码只读 phase、不读 active。`active` 字段成为只写不读的冗余字段，是技术债。
- **建议方向**：
  1. **V3.x 某版本（建议 V3.2 或 V3.3）统一移除 active 字段**：
     - grep 全仓 `'^active:'` / `state\.active` 等模式找所有引用点
     - 已知引用（截至 V3.0）：`builder-loop-stop.sh` L388 zombie 检测、`abandon-loop.sh` L106 active check、`locate-state.sh` L118 V2.4 策略 5、`merge-worktree-back.sh` 老 state 兼容、`setup-builder-loop.sh` L337 写状态、各 fixture 的 state 字段
     - 全部改用 `phase != ""`（非空即活跃）/ `phase != "active"`（特定状态）等表达
  2. **下掉前置条件**：
     - 所有已接入项目 state 文件至少经历过一次 setup-builder-loop.sh（即都含 phase 字段）—— 这要求至少跨 1-2 个版本周期
     - 没有外部脚本 / 用户工作流依赖 active 字段（grep public docs / SKILL.md / commands/builder.md 检查）
  3. **下掉时机**：在 V3.x 某次重构窗口顺手做（不专门开版本只改这件事）；下掉前发 announcement 给已接入项目用户
  4. **fixture**：增加 `test-active-field-deprecated.sh` 验证 hook 在 state 仅含 phase 字段（无 active 字段）时仍正常工作
- **优先级**：低（技术债不阻塞功能；V3.0 引入时已规划）
- **复现 / 验证**：grep 全仓 `\bactive\b` 看引用点是否还在；hook 读 phase 决策是否正确

---

## 2026-05-08 loop PASS 后 auto-commit 时序与 builder.md 步骤 4 描述不一致

- **触发上下文**：generator 项目修 deep-analysis 元问题。loop 跑完 iter 1 PASS（MERGED），主仓 git log 已多出 `8a90f72 chore(loop): Auto-commit ...`；builder 此时 spawn reviewer，reviewer 给出 🟡3 → builder 改 follow-up code → 想走 builder.md 步骤 4「自动 commit」时发现主仓已 clean。这次因为还有 follow-up dirty 改动，commit 没出错，但 builder 在 spawn reviewer 之前一度尝试改 worktree 内的 tests/CLAUDE.md（cwd 已被自动切回主仓）才意识到 worktree 已被 merge。
- **根因**：builder.md 步骤 4「自动 commit（Reviewer 通过后）」的描述顺序是 reviewer→commit，但 loop 实际行为是 loop_pass→auto_commit→reviewer→（可能的 follow-up commit）。文档与实际时序的 mismatch 让 builder 容易：① 看到主仓 clean 怀疑是不是 stash 出错 ② 在 reviewer 反馈的 follow-up 改动后，混淆"重新 commit"还是"follow-up commit"
- **建议方向**：
  1. **builder.md 步骤 4 重写**：明确"loop active 路径"和"非 loop 路径"两条时序：
     - loop 路径：loop_pass→**auto-commit 已发生**→reviewer→follow-up 改动用 follow-up commit（不 amend，按 `[cr_id_skip] Apply reviewer feedback for ...` 风格命名）
     - 非 loop 路径：reviewer 通过后 builder 主动 commit
  2. **stop hook 输出补一句指向**：当前 stop hook 输出含 `start_head=3fe1ac5 reviewer_params=...`，建议加 `auto_commit_sha=8a90f72` 让 builder 直接看到 auto-commit 已落地，不需要再 git log 确认
  3. **reviewer-params.json 加字段**：`auto_commit_sha` + `auto_commit_msg`，便于 reviewer 在报告中引用具体 commit；同时让 builder 在 follow-up commit 时复用相同 task 描述前缀
  4. **builder.md 4.5 改动汇总要含两个 commit**：当 loop+follow-up 都发生时，明确列 [auto-commit-sha] + [follow-up-sha]，避免 builder 误以为只有一次 commit
- **优先级**：中（不修不会出错事故，但 builder 文档与 loop 实际行为有 gap，每次跨 reviewer 修改都会让 builder 心智负担一次。频次：高——所有 L2/L3 + reviewer 给 🟡 建议时都触发）

---

## 2026-05-08 builder 接到「加一行/改一参」类小任务时，setup loop 前应先 grep 确认前提

- **触发上下文**：BOT 项目 hmi 推送 9 分钟后 WS 自身 abort 排查。Builder 在用户聊天里基于"跳板机日志看到 ConnectionClosedError"分析得出根因 = "WS 没开 ping 心跳"，给用户讲了"加一行 `ping_interval=30` 就好"。用户「开搞」→ builder 立刻 setup-builder-loop.sh 创建 worktree → cd 进 worktree 准备 Edit `vehicle-jumpbox/client.py` → grep 该文件后**当场发现 `ping_interval=30, ping_timeout=10` 早就在 initial commit 里**！前提推翻。Builder 立刻停手 + 重新分析 → 修正根因为 "ping_timeout=10 太短"，AskUserQuestion 让用户重新拍板。所幸 worktree 还没动 Edit，没浪费实质工作；但已经创建了 worktree + 分支 + state，归档/清理需要 abandon-loop 走流程，对工作流是可见噪音。
- **根因**：builder.md 「前置 loop 检查」只要求"读方案文件 → setup loop"，没要求 setup 之前先验证"用户讲述的根因/前提与代码现状一致"。当用户的描述简短又笃定（如"加一行参数就好"）时，builder 容易直接跟进 setup，没做"前提核验"步骤。代码文件是确定性产物，5 秒一个 grep 就能避免错误 setup。
- **建议方向**：
  1. **builder.md 步骤 1 加自检**：「先计划，后动手」段加一句：当任务描述涉及"在 X 文件加/改 Y 参数/函数/导入"等可直接验证的具体改动点时，**setup loop 之前**先 `grep` 一下确认 X 当前状态是否真如预期；前提与现状不一致 → 不 setup loop，反过来 AskUserQuestion 给用户对齐根因
  2. **小改动豁免**：单文件 ≤10 行的小改动，可考虑 builder 模式默认在主仓直接改（按 HARD RULE 现有豁免规则），减少 setup loop 后才发现前提推翻的成本
  3. **abandon-loop 友好**：当 setup 后才发现"无需此 loop"时，提供一个轻量 `cancel-fresh-loop.sh`，识别 worktree 内零改动 + 状态文件 ≤5 分钟前创建 → 直接 git worktree remove + 删 state，不需要走完整 abandon 归档（abandon 是面向"已经做了大半再弃"的语义）
  4. **fixture**：构造"用户假前提" → builder 接收任务但 setup 前 grep 发现前提错"场景，断言 builder 的行为是 AskUserQuestion 而非继续 setup
- **优先级**：中（不修不会出严重事故，但"假前提任务"是常见模式：用户基于不完整信息描述任务，builder 跟进创建 worktree 后才发现要重新对齐根因。频次中等）

---

## 2026-05-08 reviewer 对非 git 改动 + 文件存在性验证不充分

- **触发上下文**：BOT 项目精简两个运维文档（删 `docs/hmi-flash-file-push-improvement.md` + 精简 `docs/ssh-eventloop-blocking.md` 563→140 行）。两个文件都不在 git（项目 .gitignore 排除所有 .md），git diff 看不到这部分改动。reviewer 收到 changed_files=[CLAUDE.md, agent/CLAUDE.md] 但任务主旨是 docs 清理。Builder 在 prompt 里详细告知"⚠️ 这次主要内容（hmi 删 + ssh-eventloop 精简）不在 diff 里。请直接 Read 主仓副本评估其完整性，并 grep 确认 hmi 文档已不存在"。reviewer 接受了，主动 Read 主仓 + grep 验证，覆盖性结论扎实。但同一份报告里 reviewer 又对 CLAUDE.md 导航重构后 `docs/agent-architecture.md` 条目消失发出 🟡 警告——**没主动 ls 验证该文件是否还存在**就下结论"建议确认文件已删除"。实际该文件早已被删除（内容并入 agent/CLAUDE.md），新版括号注是合理内化。Builder 拒绝采纳该 🟡，给出"reviewer 漏验证存在性"的反驳理由。
- **根因**：reviewer SKILL prompt 当前对"non-git 改动"和"引用文件路径验证"两件事的处理策略不对称：① non-git 改动这次因 builder 显式提示 + 提供主仓直读路径，reviewer 做到了 ② 但对引用文件路径的存在性验证（"agent-architecture.md 是否还存在"），reviewer 默认不主动 ls/grep 而是凭印象下结论。差异本质：①需要 builder 主动告知，②应该是 reviewer 自检默认动作。
- **建议方向**：
  1. **reviewer SKILL prompt 加自检条款**：「凡涉及"某文件不存在/被删除/缺失"的论断，先 `ls <path>` 或 `git log -- <path>` 验证再下结论；不允许凭印象给警告」
  2. **non-git 改动的 builder→reviewer 协议**：固化为 reviewer-params.json 里加 `non_git_paths` 字段（builder 显式列出本轮改动的非 git 路径），reviewer SKILL 自动将这些路径加入"必须直读评估"清单。当前靠 prompt 自由文本提示，下次 builder 漏写就会有盲点
  3. **fixture**：构造一份 reviewer-params 含 `non_git_paths=["docs/foo.md"]`，断言 reviewer 输出包含对该路径的直读评估
  4. **CLAUDE.md / SKILL.md 约束**：reviewer 报告里所有"建议确认 X"的句式必须先自我验证；如果验证后 X 实际不存在/不影响，应改为"已 ls 验证 X 不存在，原条目已合理内化"而不是"建议确认"
- **优先级**：中（不修不会出严重事故，但 reviewer 报告会出现"凭印象的伪问题"，让 builder 每次都要花时间反驳验证。频次：跨 git/非 git 改动 + 用户重构类任务必现）

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

## 2026-05-08 install.sh / diagnose-stop-hook.sh 不分 max / copilot 方案 → max 用户 fixture 永远报「少一条 hook」

- **触发上下文**：cc-builder-loop A 批 session（slug=`1778210210-a-install-sh-has-e`）。loop iter 1 PASS_CMD 在 stage `v25_stop_hook_observability` 挂掉 → A 批被 abandon。根因：fixture A4 段调 diagnose-stop-hook.sh 时输出 `[1/6] settings.json hook 注册 ❌ fail`，因为 ~/.claude/settings.json 缺 `tester-write-guard.sh` 这条 hook。但用户告知「edit/write 写入相关的 hook，在 max 方案里本来就不需要注册，只注册在 copilot 方案的配置里」—— 即 max 方案下 settings.json 缺这条是**正确状态**。当前 install.sh L103-110 的 `registrations` 列表写死 6 条无脑全注册；diagnose-stop-hook.sh 也写死 6 条期望，没有方案差异判别 → max 用户运行 fixture 必然撞「[1/6] verdict=fail」。
- **建议方向**：
  1. **install.sh 加方案识别**：检测 ENV `BUILDER_LOOP_PLAN`（值 max / copilot，默认 copilot）或读 settings.json 是否含 max OAuth 字段自动判定；max 方案下 `registrations` 表跳过 tester-write-guard.sh
  2. **diagnose-stop-hook.sh 同步识别**：[1/6] 检查根据方案过滤期望列表，max 方案下少 tester-write-guard 视为 ok
  3. **uninstall.sh 不动**：删一个不存在的 hook 本来就 no-op，已是该语义
  4. **方案识别结果持久化**：写到 `<P>/.claude/builder-loop/plan` 文件或 state schema 加 `plan: max | copilot` 字段
  5. **e2e fixture**：构造「max 方案 settings.json 缺 tester-write-guard」场景，断言 install.sh 跳过该条注册 + diagnose [1/6] verdict=ok
- **优先级**：高（本次直接挡住 A 批 PASS_CMD；max 方案用户每次跑该 fixture 都会撞）

## 2026-05-08 V2.5 fixture A4 段 set -e + 命令替换吞退出码 → 测试静默 exit 看似 hang

- **触发上下文**：同上 A 批 session。stop hook 反馈日志末尾停在 `--- A4: diagnose-stop-hook.sh 6 段 + 严格 dry-run ---` 后没任何 assert 行，看似 hang。手动加 90s/120s timeout 仍只输出到 A4 段就停。最后 bash -x trace 才发现：fixture `test-stop-hook-debug-log.sh` 第 233 行 `A4_OUT="$(bash "$DIAG_SCRIPT" "$TMP" 2>&1)"` 在顶部 `set -euo pipefail` 下，当 diagnose 因 [1/6] verdict=fail 返回非 0 时，`$()` 命令替换退出码传到外层赋值，set -e 直接杀 fixture，下一行 `A4_EC=$?` 来不及执行。fixture 设计本意是仅检查输出含 `[1/6]~[6/6]` 字面（与 verdict ok 无关），但 set -e 让它走不到 assert 行就 silent exit。A1/A2/A3 段类似调用都已有 `|| A?_EC=$?` 容错，A4 漏写。
- **建议方向**：
  1. **A4 段补容错**：第 233 行末尾加 `|| A4_EC=$?`，跟 A1/A2/A3 写法对齐
  2. **全仓同模式扫描**：grep 找 `set -euo pipefail` + `_OUT="$(... 2>&1)"` + 下一行 `_EC=$?` 的写法，统一容错
  3. **fixture 反向断言**：A4 段加一条 `assert "A4 退出码可捕获（set -e 不抢）" "[ -n \"${A4_EC+set}\" ]"`，显式让以后改 fixture 的人意识到这种容错的必要性
  4. **memory 锚点**：项目记忆已有 `bash_command_substitution_or_true_swallows_exitcode.md`（讲 `|| true` 让 ec 永远 0），这次是反例 —— 不加 `|| ec=$?` 让 set -e 杀外层
- **优先级**：中（不修这条，下次 settings.json 再跟 install.sh 漂移就会复现「PASS_CMD 假阳性 hang」假象，徒增排查成本）

## 2026-05-07 worktree merge 后 cwd 切回主仓但 builder file state cache 仍在 worktree 路径

- **触发上下文**：generator 项目 exp-016 followup loop iter 1 PASS + MERGED 后，builder 想在主仓改 `novel_writer/engine.py` 应用 reviewer 反馈。Edit 调用直接报 `File has not been read yet`——CC 主 agent 的 file state cache 仍记录的是 worktree 路径下的版本，loop merge-worktree-back.sh 把改动 ff merge 到主仓 + cleanup worktree 后，主仓 engine.py 是新版（含 builder 改动），但主 agent 不知道——必须重新 Read 主仓的 engine.py 才能 Edit。worktree 已被物理删除（`ls .claude/worktrees/<slug>` not found），但 cache 还在记忆它。
- **根因**：CC harness 的 file state cache 按"绝对路径"索引，loop merge 触发的 worktree 删除 + cwd 切换是 hook 层的副作用，主 agent 不感知；builder 直觉上"已经改过的文件"包含 worktree 路径下的版本，但 merge 后那些 worktree 路径下的文件已不存在。
- **建议方向**：① merge-worktree-back.sh 在 stop hook 注入消息里显式提示「worktree 已删除，主仓 cwd 切回主仓；如需对 merge 后代码做后续 Edit，必须先重新 Read 主仓文件路径」② 或者更轻量：SKILL.md/builder.md 加一节「loop PASS 后 reviewer 反馈如何修」明确告知"先 Read 主仓文件再 Edit"③ 不一定值得 V3.0 大重构，但 reviewer 反馈修复路径会反复踩这个坑（每次 reviewer 给 🟡 都要 Edit 修，每次都首先撞 cache 失效），频次不低。
- **优先级**：低中（频次中等，但损失只是一次额外的 Read 调用，不是数据丢失级）

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
