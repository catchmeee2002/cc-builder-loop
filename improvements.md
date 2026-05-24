# Builder-Loop 改进清单

> 时间倒序。每条按 builder.md 步骤 5 模板（触发上下文 / 建议方向 / 优先级）。
> 立项不等于本期实施——A 类候选清单，等独立任务挑出来落地。
> 已关闭条目见 [CHANGELOG V3.2](CHANGELOG.md#v32-跨越界隔离--测试框架2026-05-23)

## 2026-05-23 prompt 瘦身审计：检查各流程提示词和 hook 输出是否过重

- **触发上下文**：本轮 V3.2 session 大规模改动后回顾。builder.md / SKILL.md / tester.md / reviewer.md / stop hook stderr 注入等多处提示词经过多版本迭代膨胀，可能存在冗余/过时/互相矛盾的指令。
- **建议方向**：逐文件审计提示词组成，按设计哲学 HR-4（prompt 只写"做什么"）清理动机/解释/心理说辞；合并重复指令；删除已被代码机制取代的 prompt 约束（如 doc-lint 已接管的文档检查提示）
- **优先级**：中（不紧急但影响 LLM 执行效率和一致性）

## 2026-05-23 tester 角色重构：A+D 模式（加厚输入 + builder 合法修测试）

- **触发上下文**：builder 几乎每天都在修 tester 写的测试。test_tampering 早停连续误判（L3 适配 + fixture 替换两次）。调研确认纯 spec 写可执行测试业界无成功案例，最小可行信息 = spec + 签名 + 类型 + mock 目标。
- **根因**：tester 黑盒隔离的输入不够（缺 mock 目标、数据结构、错误类型），产出的测试不可用，builder 被迫白盒修补，tampering 检测误判。
- **方案（A+D 组合）**：
  1. **A 加厚输入**：builder spawn tester 时增加 `mock_targets`（外部调用方式）、`data_contracts`（关键数据结构）、`error_types`（异常类型清单），保留黑盒精神但给够信息
  2. **D 改 tampering 判据**：从"测试文件被改 = 可疑"改成"测试被删 / 断言被弱化（去 assert / 加 skip/xfail）= 可疑"。builder 修测试是正常流程
- **涉及文件**：builder.md（spawn tester 段）、tester.md（agent prompt）、builder-loop-stop.sh（早停逻辑）
- **优先级**：高（每天踩 + 设计哲学原则 4「改输入条件」）

> 已关闭条目见 [CHANGELOG V3.2](CHANGELOG.md#v32-跨越界隔离--测试框架2026-05-23)

---

## 2026-05-19 setup-builder-loop.sh 在 worktree CWD 内调用时创建嵌套 worktree

- **触发上下文**：V3.1 worktree 隔离加固任务。loop 早停后在旧 worktree 的 CWD 下调 `setup-builder-loop.sh` 想重新进 loop。setup 把旧 worktree 当作"主仓"（因为它也有 `.claude/loop.yml`），在其下再建 `.claude/worktrees/<slug>/`，产生嵌套 worktree。嵌套的 worktree 从旧 worktree 的 HEAD 创建，不包含未提交的代码改动。
- **建议方向**：
  1. **setup 检测是否已在 worktree 内**：`git rev-parse --is-inside-work-tree` + `git worktree list` 检查当前 CWD 是否在某个 worktree 子路径内，如果是则报错 + stderr 提示「请 cd 到主仓再跑 setup」
  2. **或者 setup 自动追溯到主仓**：如果 CWD 在 worktree 内，沿 `main_repo_path`（from state）或 `.git` 文件的 `gitdir:` 追溯到真正的主仓，在那里创建 worktree
- **优先级**：中（低频但一旦踩到很混乱，需要手动清理嵌套 worktree + abandon）

---

---

## 2026-05-19 worktree dogfooding 限制：diagnose fixture 在 worktree 内永远 fail

- **触发上下文**：V3.1 修改 `diagnose-stop-hook.sh` 期望新 hook 名（subagent-start-guard.sh 等），但活跃系统 `~/.claude/settings.json` 仍注册旧 hook 名。`test-stop-hook-debug-log.sh` 的 A4 段跑 diagnose 检查活跃系统 → 必 fail。PASS_CMD stage `v25_stop_hook_observability` 因此永远过不了，直到 merge + install.sh。
- **建议方向**：
  1. **fixture A4 段改为 self-contained**：不查活跃系统 `~/.claude/`，改为在 temp HOME 下跑 install + diagnose（跟 test-plan-detection.sh 的做法一致）
  2. **或者 fixture A4 跳过活跃系统检查**：检测到 CWD 在 worktree 内时，A4 只验证 diagnose 脚本语法正确 + dry-run，不验证活跃系统 hook 状态
- **优先级**：中（每次改 hook 名/注册都会撞；workaround 是手动跑 PASS_CMD 确认只有这一条 fail）

---

## 2026-05-19 早停后重新 setup 丢失旧 worktree 未提交改动

- **触发上下文**：V3.1 worktree 隔离加固任务。loop 在 iter 1 早停（suspected_test_tampering），state 归档，旧 worktree 保留但无活跃 state。想重新进 loop，从主仓调 `setup-builder-loop.sh` → 创建新 worktree（从 main HEAD）。新 worktree 是干净的 main 副本，**旧 worktree 中的十几个未提交文件（新建脚本、修改的 install/uninstall/fixture）全部丢失**。被迫手动 `cp` 逐个文件从旧 worktree 到新 worktree。
- **根因**：setup 的 stash 机制只搬运"主仓 dirty"（setup 前主仓的未提交改动），不搬运旧 worktree 的改动。早停归档 state 后，旧 worktree 的改动跟新 loop 没有关联。
- **建议方向**：
  1. **setup 增加 `--reuse-worktree <path>` 参数**：检测到指定 worktree 存在且有未提交改动时，直接在该 worktree 上创建新 state（不新建 worktree），复用已有代码
  2. **或者早停后提供 "resume" 路径**：早停归档 state 时在 stderr 额外输出 `resume 命令: setup-builder-loop.sh --reuse-worktree <旧path>`，让 builder 一键复用
  3. **或者早停不归档 state，改为 pause**：早停后把 phase 设为 paused 而非归档到 legacy，stop hook 的 L3 闸（.pause 文件）静默不跑，但 state 和 worktree 关联保留。用户决定继续时删 pause 即可
- **优先级**：中-高（任何早停后想继续修的场景都会踩；手动 cp 十几个文件极易遗漏）

---

## 2026-05-17 step 3.5 doc skip 连续两次误判——同会话内 #181 和 #182 均跳过
- 触发上下文：同一个 CC 会话连续完成 #181（engine.py 加 `_build_milestone_block` + `_build_locked_values_block` 2 个新方法）和 #182（extraction.py 加 `_match_fact_against_secrets` + 改 `apply_extraction` 签名加 story_spine 参数 + prompt.py 3 处文案追加）。两次 merge 后 builder 均输出 `📄 doc: skip（内部实现扩展，不改对外接口）`。实际命中 checklist 第 3 条（CLAUDE.md 的"已交付能力"应加版本条目）——novel_writer/CLAUDE.md 的 extraction.py 模块描述 + 架构决策"章间连续性架构"段需要更新。用户发现后 builder 手动补了文档。
- builder 当时的判断过程（事实）：
  1. #181 merge 后，builder 的原话是 `📄 doc: skip（内部实现扩展，不改对外接口/文档变更）`——把"新增 2 个 private 方法"等同于"不改对外接口"，但 private 方法改变了 engine 的架构能力，CLAUDE.md 应记录
  2. #182 merge 后，builder 的原话是 `📄 doc: skip（prompt 文案 + 内部路由逻辑，不改对外接口）`——把"改了 apply_extraction 公开函数签名（加了 story_spine 参数）"忽略了，且 extraction.py 模块描述中没有 secret 路由能力
  3. 两次判断间隔 < 30 分钟，第二次没有因为第一次的模式而警觉
- 根因事实：与 2026-05-13 同一条目完全相同的根因——checklist 是自评、skip 成本低、merge 在 doc 之前。2026-05-13 记录该条目后未有任何代码层改动落地。
- 累计犯次数：4 次（2026-05-11 × 1 + 2026-05-13 × 1 + 2026-05-17 × 2）
- 建议方向：此条目已升级为必修。三个方向（按实施难度排序）：
  1. （最小改动）builder prompt step 3.5 改为**每条 checklist 逐项输出判定理由**，不允许一句话 skip。reviewer 检查 skip 理由是否覆盖 4 条
  2. （中等）merge-and-cleanup.sh 执行前插入 doc-lint：diff 中若含 `def ` 新增或签名变更（函数参数变化），且 CLAUDE.md 未在同一 diff 中更新 → 阻断 merge 并 stderr 提示
  3. （重构）取消 builder 自评——每次 merge 前强制 spawn doc-maintainer，由 doc-maintainer 自行判断是否需要更新（返回"无需更新"也是合法结果，但判断权不在 builder 手上）
- 优先级：高（4 次累犯，已证明纯 prompt 约束对此行为无效）

## 2026-05-13 step 3.5.5 plan.md "恰好一行" 约束对 P0 专项太浅
- 触发上下文：#150 P0 专项内部有 3 张带状态列（✅/⚠️/❌）的表格 + 8 条问题清单 + 4 个 Phase 进度。builder 只在标题加了 `✅` 一个字，内部表格的 ⚠️/❌ 全没更新。被用户发现后补更新
- 根因：step 3.5.5 指令是"输出恰好一行"，对小改动够用但对 P0 专项（含内部状态表格）太浅。builder 没有被要求 Read plan.md 内容逐项检查
- 建议方向：区分任务规模——小改动（单条 #N）→ 一行标记；P0 专项或含内部状态表格的条目 → Read 对应段落 → 列出哪些状态列需更新 → 逐项 Edit
- 优先级：中


## 2026-05-11 builder 步骤 3.5 doc-maintainer 漏触发两处文档更新
- 触发上下文：#127/#128/#129 三个 deep-analysis 工具快修合入后，builder 输出 `📄 doc: skip`，但实际有两处文档需要更新：①scripts/CLAUDE.md（outline_overflow 行为从只查 characters 变成查 characters + factions，命中触发条件第一条「脚本行为变了」，属 builder 误判）；②CHANGELOG.md（exp-024 代码合入后应追加条目，但 doc-maintainer 触发 checklist 四条均不覆盖 CHANGELOG，属流程缺陷）。最终由用户在交接环节发现，builder 手动补更新

## 2026-05-10 审查角色看 diff 默认看工作树（不是已提交 baseline）

- **触发上下文**：在小说生成器项目（generator）落 doc-policy v2 改造任务（commit `7e6df51`）。本次是纯文档改造，按分级判定属于 L1，跳过 loop 直接 spawn 审查角色。审查 subagent 跑了 `git diff main HEAD` 看本分支已提交 vs 主干 diff，**没看工作树改动**——本次改动尚未 commit 所以对它不可见。结果 4🔴 全报「文件不存在 / 段未挪走 / 忽略文件没改」，由文档维护 agent 用 ls + grep 独立验证后确认是误报，构建角色（builder）自主拒绝。多花一轮审查耗时 + 复述误判证据，差点被错误结论阻塞 commit。
- **建议方向**：
  1. **审查角色提示词（reviewer.md）头部加 baseline 优先级声明**：审查时必须按下面优先级看改动——① 工作树（`git status -s` 看 untracked + `git diff HEAD` 看 modified）→ ② 直接读新文件内容 → ③ 已提交的 baseline（`git diff <start>..HEAD`）只在 loop 通过 `reviewer_pending` 段明示 `start_head` 时才使用。无 `start_head` 默认假设工作树改动尚未 commit。
  2. **构建角色提示词（builder.md）步骤 3 spawn reviewer 时加显式 prompt 提示**：非 loop 场景（L1 跳 loop / 不在 loop 路径）spawn 时 prompt 必含一句「本次改动尚未 commit，请用 git status / 直接读文件看工作树，不要跑 `git diff main HEAD`」。
  3. **加 fixture 反例**：build 一个「未 commit 的纯文档改动 + 5 个文件（含 2 新建）」的 fixture，跑审查角色看是否漏报；漏报视为 fail。
- **优先级**：中（频次中等：L1 纯文档改动 + 不入 loop 任务每月数次；危害是审查 4🔴 阻塞误导用户，构建角色必须靠自检拒绝才能继续 commit。改 prompt 一处，成本低）

---

## 2026-05-10 构建角色 commit 前没主动查 gitignore，新建 markdown 险些丢

- **触发上下文**：在小说生成器项目（generator）落 doc-policy v2 改造任务。构建角色（builder）写完 4 个文件后跑 `git status` 才发现新建的 `CHANGELOG.md` / `docs/model-gateway.md` 没列出来——查 `.gitignore` 才知道项目策略是 `*.md` 默认排除，必须显式白名单豁免。如果不查 `.gitignore` 直接 commit，会把两个新文件落下，CLAUDE.md 改动指向"不存在"的导航条目，下次新会话加载时找不到。
- **建议方向**：
  1. **构建角色提示词（builder.md）步骤 4 加 commit 前自检**：commit 前用 `git status` 列出 untracked，逐项判断「该入 git 但被忽略 vs 本就不该入 git」；前者立刻 grep `.gitignore` 找拦截规则补白名单。
  2. **加新建文件 checklist**：任何新建非源码文件（`.md` / `.yml` / `.json` / `.toml`）后，先 `git status -s | grep ^??` 确认 git 看得到，看不到立刻处理。
  3. **改进步骤 4.5 改动汇总段格式**：要求构建角色显式列出「新建文件 N 个 / 修改文件 M 个 / 删除文件 K 个」，git status 不显示新建文件时自然暴露问题。
- **优先级**：低-中（频次低：项目策略 `*.md` 默认排除是 generator / cc-builder-loop 等少数项目特殊配置，多数项目默认 markdown 入 git。但一旦撞上后果是文档孤悬、新会话加载断链）

---

## 2026-05-10 tester subagent 写文件泄漏到主仓而非 worktree
- 触发上下文：#69 编造大设定防御任务中，tester 被传了 worktree_path 但仍有文件（test_extraction.py）写到主仓路径，导致 merge-and-cleanup.sh 执行时主仓有未提交改动阻塞 fast-forward（exit 3）。手动 stash 后重试才成功
- 建议方向：tester prompt 里强化 worktree 路径前缀要求；或在 PreToolUse hook（Write/Edit matcher）校验目标路径必须以 worktree_path 开头，命中则拦截并提示修正
- 优先级：中

## 2026-05-10 reviewer subagent 对大文件读有限窗口导致误判
- 触发上下文：#69 第三轮 reviewer 声称 engine.py 里没有 registry_gate 调用（实际在 1858 行），原因是它只读了 1836-1855 行的窗口。这类 false positive 会浪费一轮 loop 迭代
- 建议方向：reviewer prompt 补充指引「对大文件（>500行）声称'代码不存在'前，grep 确认」；或 reviewer 工具链增加全文 grep 能力
- 优先级：低

## 2026-05-09 spawn doc-maintainer 时 builder 没传 worktree_path，文档写到主仓

- **触发上下文**：generator 项目 exp-021 meta 推翻 deep 任务 worktree 模式。reviewer 通过后 builder 按步骤 3.5 spawn doc-maintainer 同步 novel_writer/CLAUDE.md / scripts/CLAUDE.md / tests/CLAUDE.md。doc-maintainer subagent 工作目录在主仓 cwd，prompt 里没强制指定写入根目录 → 文档落到主仓 working tree（CLAUDE.md 在 .gitignore 白名单进 git，但和 worktree 分支无关）。builder 发现后只能手动 `git diff > patch && git apply`（worktree 内）+ `git checkout`（主仓）搬移，再让 stop hook 自愈跑一轮 PASS_CMD + 再 spawn 一轮 reviewer 审文档，多花一轮 reviewer 时间。
- **建议方向**：
  1. **builder.md 步骤 3.5 模板补 worktree_path 必传字段**：当 V3.0 worktree 模式（builder-loop.local.md active=true）时，spawn doc-maintainer 的 prompt 必须含 `worktree_path: <path>` + 「所有 Edit/Write 用 worktree 绝对路径前缀」，跟 3a+ TESTER_HINT 走 tester 时的 worktree_path 处理保持一致。bare 模式可省略。
  2. **doc-maintainer agent 自身约定**：agent prompt 头部加「如调用方传了 worktree_path，所有目标文件路径必须以 worktree_path 开头；否则按 cwd 处理」，做防御性处理。
  3. **builder.md 加反例锚点**：步骤 3.5 末尾加一句「⛔ worktree 模式下不传 worktree_path → doc-maintainer 默认 cwd 写主仓 → 文档孤悬，需手动搬移 + 多走一轮 reviewer」
- **优先级**：中（worktree 模式 + 文档评估命中清单的任务每周都遇；builder 当下能手动补救但代价是多走一轮 reviewer 耗时；改 prompt 一处即可）

---

## 2026-05-09 [V3.0.1 衍生] hook 软链注册指向主仓，worktree 内改 hook 不会在自己身上 dogfood

- **触发上下文**：V3.0.1 reviewer-timing-check.sh hotfix 任务。worktree 内改完 hook + fixture（fixture 14/14 PASS 黑盒已验证），但 PASS 后主进程 spawn reviewer 仍撞主仓旧版 hook 拦死（CC 渲染成「No stderr output」）。原因：install.sh 创建的软链是 `~/.claude/scripts/<hook>.sh → /mnt/hongyu.liao_docker/cc-builder-loop/scripts/<hook>.sh` 绝对路径指主仓，不指 worktree。改任何 hook 类的任务都会撞，本次直接复现了事故现场 session f80932fb 的同款表现。
- **建议方向**：
  1. **install.sh 加 `--dev` flag（轻量方案）**：开发模式下软链改成跟随当前 worktree 的当前 cwd，跑 `./install.sh --dev` 就能把运行时 hook 切到 worktree 的脚本上自测；任务完成后跑 `./install.sh` 切回主仓。复杂度低、可控
  2. **builder.md 步骤 5 加提示（轻量方案）**：「改 hook 类脚本（reviewer-timing-check / tester-* / builder-loop-stop / 等）的任务完成后，提示用户 dogfooding 局限——只能跑 fixture 黑盒验证，自己 spawn reviewer 仍走旧版本，需要等 merge 进主线后下次任务才能用上新版」
  3. **fixture 框架补一类「hook 改动专用 fixture 标签」**：改 hook 的任务必须在 PASS_CMD 里加一条 fixture 黑盒覆盖新行为（不依赖运行时软链生效）
- **优先级**：中（频次中等：hook 改动每月几次；危害是 builder 容易被误导以为 fix 没生效。本次踩到才意识到，写进文档让下次少走弯路）

---

## 2026-05-09 [V3.0.1 衍生] merge-and-cleanup.sh ff merge 失败时不让 bystander dirty，错误信息没给 stash 建议

- **触发上下文**：V3.0.1 hotfix 任务结尾调 `merge-and-cleanup.sh` ff merge，主仓 `.claude/improvements.md` 有别的 cross-session（BOT 项目）写的立项条目 dirty，ff merge 撞「Your local changes to the following files would be overwritten by merge」exit 3。错误信息让 builder 误以为本次改动跟主仓有冲突，实际是无关 bystander dirty。手动「git stash push -- .claude/improvements.md → 重跑 merge-and-cleanup → git stash pop」三步绕过。
- **建议方向**：
  1. **merge-and-cleanup.sh 在 ff merge 失败时**自动诊断：grep 失败错误信息里被 overwrite 的文件清单，对照本次 PASS commit 是否动过这些文件——
     - 没动过 = bystander dirty → 输出 stash 建议命令（不自动 stash，让用户决定）
     - 动过 = 真冲突 → 走 arbiter（已有路径）
  2. **错误信息加 stash 模板**：「检测到主仓 N 个文件 dirty 阻挡 ff merge，本次 commit 未触及；建议：`git stash push -m bystander -- <files>` → 重跑本脚本 → `git stash pop`」
  3. **更激进**：脚本直接 `git stash push -m "merge-and-cleanup-bystander"` 让开 → ff merge → unstash；但有副作用（user 改一半的 dirty 被 stash 可能不爽），建议先走 1+2 提示而非自动
- **优先级**：中（频次中：跨 session 多任务并行时常见；当前手工绕过虽然可行但容易误判 + 多消耗一次排查）

---

## 2026-05-09 [V3.0.1 衍生] .gitignore 漏 reviewer-diff-*.txt 新格式（V3.0 文件按 slug 拆后未跟进）

- **触发上下文**：V3.0.1 hotfix merge 完后主仓 `git status` 显示 `.claude/reviewer-diff-1778322888-reviewer-timing-chec.txt` untracked。`.gitignore` 里有 V2.x 的 `.claude/reviewer-diff.txt`（单文件），V3.0 reviewer-as-gate 把该文件改成按 slug 拆 `.claude/reviewer-diff-<slug>.txt` 但 .gitignore 没跟进。所有已接入 V3.0 项目都会撞——每跑一次 loop 留一个 untracked diff 文件。
- **建议方向**：
  1. **.gitignore 加一行**：`.claude/reviewer-diff-*.txt`（保留 `.claude/reviewer-diff.txt` 兼容老数据，新加 `*` 模式覆盖 V3.0 格式）
  2. **fixture 验证**：在 V3.0 lifecycle fixture 末尾断言主仓 git status 不应残留 reviewer-diff-* untracked
  3. **顺手扫**：grep V3.0 改动里所有「按 slug 拆」的文件名（`reviewer-diff-<slug>.txt` / `review_reports/<project>_<slug>_*.md` 等），跟 .gitignore 对一遍
- **优先级**：低（不影响功能但污染 git status；接入 V3.0 项目都会踩，但只是噪音不是漏洞）

---

## 2026-05-09 doc-maintainer 在 worktree 改动会被 PASS auto-commit 抢跑

- **触发上下文**：BOT 项目「上电自检告警每日封顶限频」任务，PASS_CMD 通过后 stop hook 在 spawn doc-maintainer 之前已经把 worktree 改动 auto-commit（commit `b4b780d`）。doc-maintainer 之后在 worktree 内 Edit 了 `CLAUDE.md`（加一条核心约束条目），但这次 Edit 没有任何机制 commit；最终 `merge-and-cleanup.sh` fast-forward 主线时这一行 doc 改动**没合进来**。Builder 只能在主仓手动 Edit + commit 一次（`e341b31`）补救。
- **建议方向**：
  1. step 3.5 文档评估在 V3.0 reviewer-as-gate 路径下要明确"doc-maintainer 改完必须 builder 主动 amend 进上一个 PASS commit 或追一个 followup commit"，写进 `builder.md` step 3.5
  2. 或者：`merge-and-cleanup.sh` 合主线前先 `git status --porcelain` 检查 worktree 是否有 uncommitted 改动，有则 stage + amend 进最近 commit，否则 fast-forward 会丢
  3. 或者：doc-maintainer SKILL prompt 末尾加 "Edit/Write 完成后，自己用 `git add <files> && git commit --amend --no-edit` 把改动并入上一个 commit"
- **优先级**：中（doc 漏 commit 不影响功能、能事后人肉补；但 silent loss 让人很难发现，比如这次是兜底自审顺手 grep 才发现 main 上没有那一行）

## 2026-05-09 merge-and-cleanup.sh 末尾 cwd 被删导致 pwd 报错 exit 1

- **触发上下文**：BOT 项目同上任务，builder cwd 在 worktree 时调 `merge-and-cleanup.sh`，脚本删 worktree 后末尾的 shell 报 `pwd: error retrieving current directory: getcwd: cannot access parent directories: No such file or directory` 并 exit 1。Fast-forward 实际成功（commit / branch 删除均完成），但 exit 1 让调用者（Builder）一开始误判失败。
- **建议方向**：
  1. `merge-and-cleanup.sh` 末尾添加 `cd "${MAIN_REPO}" 2>/dev/null || cd /tmp` 让 cwd 跳出被删的 worktree 后再 exit
  2. 或者脚本入口先记录 `MAIN_REPO`，最后 `exec sh -c "cd '$MAIN_REPO' && exit 0"` 类似的兜底
- **优先级**：低（仅是返回码误导，合并本身正确）

## 2026-05-09 main-dirty stash apply 进 worktree 的语义需要在 setup 输出 / commit message 之外多一处显式提示

- **触发上下文**：BOT 项目同上任务，主仓有 3 个 dirty 文件 + 1 个 untracked，setup-builder-loop.sh 通过 stash apply 把它们带进了 worktree，最终 `merge-and-cleanup.sh` fast-forward 时一并 commit 进主线（commit message 后缀 `[+3 main-dirty]`）。Builder 看 commit 文件列表时一开始疑惑"为什么 fs_webhook/preview_card.py 也在里面"，需要回看 setup 时的输出 / commit message 后缀才能定位。这是设计行为不是 bug，但 AI / 人在排查时容易误判为 loop 机制把无关文件偷偷塞进了 commit。
- **建议方向**：
  1. `builder.md` 的"前置 loop 检查"段加一条 hint："若主仓有 dirty，setup 会把它们 stash apply 到 worktree，最终 fast-forward 合主线时这些 dirty 也会一并 commit。Builder 的 changed_files 报告应清楚区分 task-related vs main-dirty。"
  2. `setup-builder-loop.sh` 输出"⚠️ 主仓 dirty N 个，已 stash apply 到 worktree，将随本次任务一并 commit"加粗或加颜色
  3. Reviewer 主体范围应自动按 `pre_loop_dirty_files` state 字段去除 main-dirty 文件
- **优先级**：中（不影响功能但严重影响排查体验，AI 看 commit 容易误判）

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

## 2026-04-29 doc-maintainer 改主仓而非 worktree（与 V2.2 tester-write-guard 同模式漏洞）

- **触发上下文**：V2.4 落地 session 步骤 3.5 spawn doc-maintainer 同步评估 SKILL.md / README.md。Doc-maintainer 输出 `UPDATE_DOCS_SUMMARY: 已更新 1 个文档 | skills/builder-loop/README.md: 补 V2.4 fixture 表格条目`，但实际改的是**主仓** `skills/builder-loop/README.md`，不是 builder cwd 所在的 worktree（`/mnt/hongyu.liao_docker/cc-builder-loop/.claude/worktrees/1777457315-v2-4-locate-state-sh/skills/builder-loop/README.md`）。Builder 在 worktree 内 git status 看不到 README 改动；后续 ff merge 时主仓本地 README 又卡 merge（"local changes would be overwritten"），需手动 stash → merge → drop。Builder 还得自己在 worktree 内手动补一次同样的行才能进 commit。本次靠注意力发现，下次可能漏判。
- **建议方向**：
  1. 与 V2.2 `tester-write-guard.sh` 同模式扩展：把 `Write|Edit|MultiEdit` 跨目录写防护扩展到 doc-maintainer 子代理（matcher 改成识别 doc-maintainer subagent）；或加新 hook `doc-maintainer-write-guard.sh`
  2. `agents/doc-maintainer.md`（如果存在）prompt 字段表加 `worktree_path` 必填字段 + 步骤自检追加路径根校验项（同 V2.2 tester.md 加固方式）
  3. builder.md 步骤 3.5 spawn doc-maintainer 段强制传 `worktree_path`（loop 活跃 = state.worktree_path / loop 已结束 = ""）
  4. e2e fixture：`test-doc-maintainer-write-guard.sh` 模拟 doc-maintainer 试图写主仓 → exit 2 + 精确诊断 stderr
- **优先级**：中（doc 漏审风险跟 tester 同等级；本次手动补但易遗漏）

## 2026-04-29 locate-state.sh 策略 3 grep-sed 管道缺 `|| true` + set -e 缺失（pre-existing）

- **触发上下文**：V2.4 reviewer 反馈（🟡）— `locate-state.sh:24` 用 `set -uo pipefail` 缺 `-e`（与同项目其他脚本不一致，外部静默契约下吞错）；策略 3 的 `wt="$(grep -E '^worktree_path:' "$sf" 2>/dev/null | head -1 | sed -E '...')"`（L83）grep 未命中时 + pipefail 让子 shell 退出非 0，外层 `wt=...` 命令替换实际接受空字符串后续 `[ -z "$wt" ] && continue` 兜过去，但这种写法依赖 `set -e` 缺失才不杀脚本，写法脆弱（迁移到 `set -euo pipefail` 时会暴露）。本期按"bug fix 不带周边清理"原则不改。
- **建议方向**：
  1. 统一 `set -euo pipefail`，所有 grep / head / sed 管道末尾补 `|| true`（locate-state.sh / 其他遗留脚本一并扫一遍）
  2. e2e fixture：构造「worktree_path 字段缺失」的 state 文件验证策略 3 跳过该 state 不报错
  3. 顺路检查策略 4 / 5 的 grep 是否同样脆弱（V2.4 策略 5 已显式 `|| true`，但策略 3-4 未审）
- **优先级**：低（pre-existing 多版本未触发实质 bug；脆弱性属于代码风格而非功能正确性）

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
