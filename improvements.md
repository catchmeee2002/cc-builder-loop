# Builder-Loop 改进清单

## 2026-05-23 stop hook 输出串到其他 CC session——subagent 在主仓 CWD 触发 hook
- 触发上下文：#231 任务在 worktree 模式下工作。spawn tester subagent 时，subagent 继承主仓 CWD（/mnt/hongyu.liao_docker/generator）而非 worktree 路径。subagent 运行 pytest 触发 stop hook，hook 在主仓 CWD 下执行，无法定位 worktree 的 state 文件，输出被路由到另一个监听主仓的 CC session。
- 根因：CC Bash tool 的 `cd` 跨调用不持久——builder 执行 `cd /worktree/path` 后，下一次 Bash 调用 CWD 仍回到主仓。subagent 更是完全独立的进程，CWD 必定是主仓。stop hook 靠 CWD 判断属于哪个 worktree，CWD 错了就串台。
- 临时解法：用 pause 文件暂停 hook，手动在 worktree 里跑 PASS_CMD，跑完手动 merge。但 merge-and-cleanup.sh 在未 commit 的 worktree 上执行会丢失所有未提交改动（"Already up to date" + cleanup 删 worktree = 改动全丢），不得不在主仓重做全部代码改动。
- 二次损伤：merge-and-cleanup.sh 对 worktree 内未 commit 的改动没有任何防护——不检查 dirty state 就直接 merge + 删 worktree。应该在 merge 前检查 `git status --porcelain` 非空则 abort。
- 建议方向：
  1. **subagent spawn 时显式传 worktree_path**：tester/reviewer subagent prompt 里带 worktree 绝对路径，subagent 所有 Bash 命令前缀 `cd <worktree> &&`
  2. **merge-and-cleanup.sh 前置 dirty check**：merge 前检查 worktree `git status --porcelain`，非空则 abort + stderr 报"worktree 有未提交改动，请先 commit"
  3. **stop hook 不依赖 CWD 定位 state**：改用 state 文件里的 worktree_path 字段反向匹配，或唯一 active state 时直接绑定（策略 5 当前实现可能有 bug）
- 优先级：高（改动全丢 + 需要手动重做，且 worktree 模式下必然复现）

## 2026-05-22 merge-and-cleanup.sh 在 detached HEAD 下 merge 导致代码静默丢失
- 触发上下文：磁盘自检自动清理功能（7 文件 +926 行）在 worktree 中通过 reviewer，builder 调 merge-and-cleanup.sh 做 fast-forward merge。脚本输出 `Updating 39d344b..94bbdf1 Fast-forward` 看起来成功，但 exit code 1。builder 看到 fast-forward 输出就报了"合并成功"，没管 exit code。实际上 merge 在 detached HEAD 下执行，只移了 HEAD 指针没移 feature-main 分支。cleanup 阶段 `checkout feature-main` 直接跳回 39d344b，后续 blacklist 和 OTA 改动继续在旧基线上推进，自动清理代码从分支上消失。直到线上车辆报警才发现代码不在。
- reflog 证据：`HEAD@{4}: merge loop/1779419801-task: Fast-forward → 94bbdf1` → `HEAD@{3}: checkout: moving from 94bbdf1 to feature-main → 39d344b`。从 94bbdf1 是 hash 而非 branch name 可见当时是 detached HEAD。
- 根因：merge-and-cleanup.sh 没有前置检查「当前 HEAD 是否在目标分支上」。worktree checkout 到某个 commit 后可能进入 detached 状态，脚本盲目 `git merge` 不会报错但不移动 branch ref。exit code 1（可能来自 cleanup 阶段）被 builder 忽视——builder 只看 stdout 里的 "Fast-forward" 就判定成功。
- 影响：P0 生产事故——代码静默丢失，无告警无拦截，直到用户在线上发现功能不生效才暴露。
- 建议方向（两层防御）：
  1. **merge-and-cleanup.sh 前置断言**：merge 前 `git symbolic-ref HEAD` 确认在目标分支上，detached 状态直接 exit 非零 + stderr 报明确错误。merge 后 `git rev-parse <branch>` 确认分支指针确实移动了，没动 → exit + 报错。
  2. **builder 对 exit code 非零必须当失败处理**：脚本返回非零时不能光看 stdout 里有 "Fast-forward" 就报成功，必须标红告知用户 merge 失败并保留 worktree 供排查。
- 优先级：高（P0，代码丢失 + 生产事故）

## 2026-05-19 step 3.5 删代码时文档过时引用未清理——#209 废弃 snapshot 后 3 个文档残留旧引用
- 触发上下文：#209 删除 `_derive_chapter_snapshot` + `save_chapter_snapshot` + `get_previous_snapshot` 等方法（-239 行），merge 后 builder 输出 `📄 doc: skip（内部重构删死代码，无新接口/能力）`。用户追问"所有文档更新了吗"后发现 `novel_writer/CLAUDE.md`（不可逆属性段提 snapshot_prompt）、`docs/architecture.md`（mermaid 图 + 目录树 + 注入矩阵共 6 处）、`tests/CLAUDE.md`（2 处测试描述）均包含已废弃的 snapshot 引用。
- builder 当时的判断过程：checklist 4 条逐项看——① SKILL.md 行为没变 ② 没新增对外文件 ③ CLAUDE.md 没有新能力要加 ④ 没新 TODO。全部不命中→skip。但 checklist 缺少"删除功能时检查现有文档是否有过时引用"这个维度。
- 根因：step 3.5 checklist 面向"新增"设计（新脚本/新文件/新能力/新 TODO），没有"删除/废弃"维度。删代码时文档里的旧引用不触发任何 checklist 条目。这跟 2026-05-17 的"接口变了但判断为不改对外接口"不同——这次是 checklist 本身有盲区。
- 累计犯次数：5 次（前 4 次 + 本次，但本次根因不同——不是偷懒 skip 而是 checklist 缺维度）
- 建议方向：step 3.5 checklist 加第 5 条——「删除/废弃功能/方法时，grep 文档目录（CLAUDE.md / architecture.md / tests/CLAUDE.md）检查是否有过时引用」。或者更根本的：merge-and-cleanup.sh 在 diff 含 `-def ` 行（删除函数）时自动 grep docs/ 检查同名引用残留。
- 优先级：高（checklist 结构性盲区，非行为问题）

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

## 2026-05-13 step 3.5 doc-maintainer checklist 自评无强制力，builder 可偷懒 skip
- 触发上下文：#150 V1/V2 数据协议大一统（L3，12 个源文件改动），builder 在 step 3.5 直接输出 `📄 doc: skip`，但实际命中了 checklist 第 1/3 条（接口签名变了 + CLAUDE.md 模块描述过时）。被用户发现后补 doc-maintainer
- 根因：checklist 是自评，builder 在长流程末尾（代码→测试→loop×2→reviewer→merge）注意力在"收尾"而非"检查"；skip 成本太低（一句话理由即可）；merge 在 doc 检查之前发生（流程无门禁）
- 建议方向：
  1. 把 checklist 改成结构化自检：builder 必须输出每条的 ✅/❌ 判定理由（类似 HR-8 五问），跳过任一条 → reviewer 标 🔴
  2. skip 理由必须覆盖 4 条 checklist 各为什么不命中，不是一句话能糊弄的
  3. merge 应在 doc 检查完成后才执行（doc 作为 merge 前置门禁）
- 优先级：高
- 同类事件：2026-05-11 builder 步骤 3.5 doc-maintainer 漏触发（同一根因，第二次犯）

## 2026-05-13 step 3.5.5 plan.md "恰好一行" 约束对 P0 专项太浅
- 触发上下文：#150 P0 专项内部有 3 张带状态列（✅/⚠️/❌）的表格 + 8 条问题清单 + 4 个 Phase 进度。builder 只在标题加了 `✅` 一个字，内部表格的 ⚠️/❌ 全没更新。被用户发现后补更新
- 根因：step 3.5.5 指令是"输出恰好一行"，对小改动够用但对 P0 专项（含内部状态表格）太浅。builder 没有被要求 Read plan.md 内容逐项检查
- 建议方向：区分任务规模——小改动（单条 #N）→ 一行标记；P0 专项或含内部状态表格的条目 → Read 对应段落 → 列出哪些状态列需更新 → 逐项 Edit
- 优先级：中

## 2026-05-12 suspected_test_tampering 早停在 L3 改动中是误判
- 触发上下文：novel-writer Story Spine L3 架构改动（~25 文件 ~1800 行），删除了 MemoryContext 的 facts_context/constants/previous_snapshot/memory_context 四个旧字段，改了 draft_prompt/chapter_plan_prompt 等函数签名。测试中引用旧字段/旧签名的 fixture 必须适配，属于 L3 正常操作。但 stop hook 检测到测试文件被修改后判定 suspected_test_tampering 并早停，导致 loop 在 iter=1 就归档。
- 根因：stop hook 的 test_tampering 检测逻辑没有区分「改测试以绕过 PASS_CMD」和「L3 数据合同变更后适配测试 fixture」。后者是方案文件明确规定的 Phase E（测试修复），builder 流程中 tester subagent 也会改测试。
- 建议方向：test_tampering 检测应增加豁免条件——当方案文件标注 L3 且 changed_files 同时包含源码和测试时，视为合法适配不触发早停。或者更简单：只在「仅改测试不改源码」时才判定 tampering（如果源码也改了大量文件，测试跟着改是正常的）
- 优先级：高
- 事故现场：
  - legacy state bak: `/mnt/hongyu.liao_docker/generator/.claude/builder-loop/legacy/20260512-160925-early_stop_suspected_test_tampering.bak`
  - legacy info: `/mnt/hongyu.liao_docker/generator/.claude/builder-loop/legacy/20260512-160925-early_stop_suspected_test_tampering.info`
  - worktree 保留（含全部 dirty 改动）: `/mnt/hongyu.liao_docker/generator/.claude/worktrees/1778572012-story-spine`
  - worktree branch: `loop/1778572012-story-spine`（HEAD=1134bf1，无 commit，全部改动 unstaged）
  - stash ref: `53a45be350c03fa1b6a272ec59e3ae444d41c95c`（主仓 dirty 已 restore）
  - slug: `1778572012-story-spine`，iter=1 即早停，plan_file 指向 `.claude/plans/20260512-story-spine-implementation.md`
  - 实际失败测试 12/933：根因全部是 L3 签名变更后测试 fixture 未适配（draft_prompt 删 constants_md 参数 3 个 + MemoryContext 删 facts_context/memory_context 字段 4 个 + workshop STORY_SPINE 替代 CONSTANTS_LOCK 5 个）

## 2026-05-11 tester prompt 需标注源码的 subprocess 调用方式，否则 mock 目标全偏
- 触发上下文：Personal_Assistant_Bot 项目健康巡检 Agent 任务中，tester 写的 5 个测试文件全部 mock `subprocess.run`，但源码实际用 `subprocess.check_output`；同样 mock 不存在的 `_make_client` 而非 `httpx.Client`。reviewer 两轮才抓出（第一轮通过核心代码，第二轮发现测试 mock 失效），浪费两个 loop 迭代
- 建议方向：builder spawn tester 时在 interface_signatures 里显式标注每个模块用的 subprocess 调用方式（check_output / run / Popen）和 HTTP 客户端库（httpx.Client / requests.Session 等），让 tester 不用猜 mock 目标
- 优先级：中

## 2026-05-11 builder 步骤 3.5 doc-maintainer 漏触发两处文档更新
- 触发上下文：#127/#128/#129 三个 deep-analysis 工具快修合入后，builder 输出 `📄 doc: skip`，但实际有两处文档需要更新：①scripts/CLAUDE.md（outline_overflow 行为从只查 characters 变成查 characters + factions，命中触发条件第一条「脚本行为变了」，属 builder 误判）；②CHANGELOG.md（exp-024 代码合入后应追加条目，但 doc-maintainer 触发 checklist 四条均不覆盖 CHANGELOG，属流程缺陷）。最终由用户在交接环节发现，builder 手动补更新

## 2026-05-10 tester subagent 写文件泄漏到主仓而非 worktree
- 触发上下文：#69 编造大设定防御任务中，tester 被传了 worktree_path 但仍有文件（test_extraction.py）写到主仓路径，导致 merge-and-cleanup.sh 执行时主仓有未提交改动阻塞 fast-forward（exit 3）。手动 stash 后重试才成功
- 建议方向：tester prompt 里强化 worktree 路径前缀要求；或在 PreToolUse hook（Write/Edit matcher）校验目标路径必须以 worktree_path 开头，命中则拦截并提示修正
- 优先级：中

## 2026-05-10 reviewer subagent 对大文件读有限窗口导致误判
- 触发上下文：#69 第三轮 reviewer 声称 engine.py 里没有 registry_gate 调用（实际在 1858 行），原因是它只读了 1836-1855 行的窗口。这类 false positive 会浪费一轮 loop 迭代
- 建议方向：reviewer prompt 补充指引「对大文件（>500行）声称'代码不存在'前，grep 确认」；或 reviewer 工具链增加全文 grep 能力
- 优先级：低
