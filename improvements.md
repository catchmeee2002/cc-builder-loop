# Builder-Loop 改进清单

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
