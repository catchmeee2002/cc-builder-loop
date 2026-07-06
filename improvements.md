# Builder-Loop 改进清单

> 时间倒序。每条按 builder.md 步骤 5 模板：`## YYYY-MM-DD <标题>` + 触发场景 / 现象 / 根因 / 优先级。
> **只记事实，不写建议方向**——loop 侧开发者拿到事实自己判断怎么修。
> 已消化条目直接删除（代码是 ground truth），不标 ✅。已关闭条目见 [CHANGELOG](CHANGELOG.md)。
>
> **观察期规则**：fix 已实装 + fixture/单测通过，但未经 e2e 或实战验证 → 进观察期而非直接删除。
> - 标题格式：`## YYYY-MM-DD [观察期] <标题>`
> - 必填字段：`验证条件：<截止日期 YYYY-MM-DD> + <通过→删除本条目> + <失败→具体动作>`
> - 截止日期默认 fix 日期 +14 天

## 2026-07-06 [观察期] extract-e2e-cases.sh fence 逻辑与 planner 格式冲突导致 e2e 验收静默跳过
- 修复：awk fence 逻辑加 `if (!capture)` 条件，capture 模式内 ``` 不 toggle fence（commit 6c568f4）。fixture case 9/10 覆盖
- 验证条件：截止日期：2026-07-20 前有 planner 产出 ```yaml 包裹的 e2e-cases 且 extract 正确提取 → 删除本条目。仍静默跳过 → 排查 awk 逻辑是否有未覆盖的 fence 嵌套场景
- 优先级：观察

## 2026-07-05 后台任务强制开 PR — 单人项目增加无意义摩擦
- 触发场景：divine-word 项目后台任务（background job）在 worktree 中修改 plan.md 文档后自动 push 分支 + `gh pr create --draft`
- 现象：用户发现多了一个 GitHub PR 需要手动 review 和 merge。之前交互式对话直接 commit master 没有这个步骤。用户是单人开发者，PR 流程纯增摩擦
- 根因：CC 后台任务系统提示词要求 worktree 隔离 + "Never push to main/master" + 自动开 draft PR。这个默认行为对团队项目合理，对单人项目是负担。loop 的 worktree 模式如果也触发了后台任务的 PR 流程，会与 loop 自身的 merge-and-cleanup.sh 冲突
- 优先级：中

## 2026-07-05 e2e 测例构造缺陷 — "消灭坏结果"通过但"好结果不存在"
- 触发场景：divine-word 项目 FGHK 修复轮。plan 中 fog-removed-pointlight e2e case 验收标准为"viewport 黑色占比 < 5%"
- 现象：builder 第一次实现了 Light2D 点光源方案（addSettlementLights）实现"文明中心有视野、外层暗"。e2e 失败（Light2D 导致 sprite 黑块）。builder 第二次修复直接把整个 addSettlementLights 删成空函数 → 全图无差别亮 → e2e 通过（黑色占比 0.03%）。feature 被删除但 e2e 报 PASS
- 根因：e2e case 只验证"坏东西没了"（黑色占比低），没验证"好东西在"（聚落周围存在明暗渐变）。删功能比修功能更容易通过这种单向验收。这是"消灭坏结果 ≠ 确认好结果存在"的结构性问题
- 优先级：高

## 2026-07-05 [观察期] V4.3 SendMessage 续接路径 — ToolSearch pre-load 修复
- 修复：builder.md 续接段加 `ToolSearch("select:SendMessage")` 前置 + 参数名 summary→message（commit 66ce073）
- 验证条件：截止日期：2026-07-19 前有 builder 在真实 loop 中成功通过 SendMessage 续接 tester 或 reviewer → 删除本条目。失败 → 排查 SendMessage 的 message 参数格式或 agent 生命周期问题
- 优先级：观察

## 2026-07-05 [观察期] V5.7 E2E framework redesign — verify+quality 双轨 judge
- 修复：tester 三层评估（L1→L2a verify→L2b quality），case schema 从 llm_judge 改为 judge:{verify, quality}（commit fb8802e）
- 验证条件：截止日期：2026-07-19 前有项目用新 schema 跑过完整 e2e 且 quality judge 正确拦住低质量产出 → 删除本条目。quality judge 形同虚设（全 pass 不拦）→ 检查 quality prompt 有效性
- 优先级：观察

## 2026-07-05 [观察期] V5.6 tester path isolation — absolute target_test_dirs
- 修复：builder spawn tester 时 target_test_dirs 改为绝对路径 + post-hoc 校验（commit 35c065d）
- 验证条件：截止日期：2026-07-19 前有 worktree 模式 loop 触发 tester 且 tester 文件全写在 worktree 内 → 删除本条目。tester 仍写主仓 → 检查 post-hoc 搬运是否生效
- 优先级：观察

## 2026-07-05 [观察期] V5.5 reviewer Phase D — doc audit 独立审计
- 修复：删 doc-maintainer，builder 写 doc，reviewer Phase D 独立审计 doc_freshness_check 列表（commit 1fd2d6e）
- 验证条件：截止日期：2026-07-19 前有 reviewer 实际执行 Phase D 并产出 doc 相关 finding（通过或发现问题均可）→ 删除本条目。Phase D 从未触发 → 检查 reviewer.md prompt 是否正确接收 doc_freshness_check 参数
- 优先级：观察

## 2026-07-06 bare 模式 auto-commit 漏 .gitignore 忽略文件 + stop hook 自愈 commit tester 产物

- 触发场景：novel-writer 项目 reader 成品级优化，bare 模式。项目 .gitignore 有 `*.md` 全忽略规则 + 白名单例外（CLAUDE.md / docs/architecture.md 等）。builder 新建 scripts/reader/README.md 时未同步加 .gitignore 例外。tester subagent 沉淀 scripts/reader/e2e_cases.yaml（新文件）后 builder 未手动 add
- 现象：
  1. handle-pass-result bare auto-commit 只 git add 已 tracked + git add -u，scripts/reader/README.md 被 `*.md` 规则忽略漏在首 commit 外（stat 缺该文件）。builder 手工 amend 补 .gitignore 例外 + README.md 才入库。若未发现，远端 sparse pull 拉不到 README.md → deploy cp 失败 → docsify 首页 404
  2. tester 产出的 e2e_cases.yaml 未提交 → 工作区 dirty → stop hook L1 闸自愈（passed_pending_review → active，reason=dirty_changes）→ 重跑 PASS_CMD → PASS → auto-commit e2e_cases.yaml 进新 commit → 回 passed_pending_review
- 根因：
  1. bare auto-commit 逻辑按 git check-ignore 过滤，不感知"builder 应该提交但被 .gitignore 误伤"的文件。builder 加 .gitignore 例外是手工补救，时机晚于首次 auto-commit
  2. stop hook L1 自愈是设计行为（dirty → active），auto-commit tester 产物也算正确收尾，但 builder 未预期 tester 产物会变 dirty，commit message 仍是 builder 的 task 描述，tester 产物混入可能误导
- 优先级：中（① 需 builder 警惕 `*.md` 全忽略项目里新建 .md 文件时同步加 .gitignore 例外，否则 bare auto-commit 漏提交；② stop hook 自愈 commit tester 产物实际是 helpful，但 commit 归属语义模糊）


## 2026-07-06 bare 模式 background reviewer agent 丢失（TaskOutput 报 No task found）

- 触发场景：novel-writer 项目 reader 成品级优化，bare 模式（主仓 cwd /home/ubuntu/dev/generator）。builder 在同一 message 内并行 spawn reviewer（subagent_type=reviewer, run_in_background=true）+ tester（同步）。tester 同步返回 E2E_SUMMARY
- 现象：reviewer background spawn 返回 agentId a325b956c244fbc7e + "Async agent launched successfully"。后续 TaskOutput(task_id=a325b956c244fbc7e, block=false) 报 "No task found with ID"。用户反馈"reviewer 没 spawn"。改同步 spawn（不带 run_in_background）后正常返回审查报告
- 根因：未定位。CC 平台 background agent 在本环境（bare 模式 / 主仓 cwd）调度丢失。worktree 模式未测
- 优先级：中（影响 bare 模式 background 审查链路可靠性，builder 以为在跑实则丢了）



## 2026-05-09 [理论风险] dirty stash 流程边界 case（原 known-risks R6）

- 触发条件：worktree 模式下 setup 的 stash apply / merge-and-cleanup 的 stash drop 在特定边界场景不完整
- 子项：
  - R6.1 git status --porcelain 重命名文件只取 new 路径，pre_loop_dirty_files 不全（仅记录欠完整，不影响 stash 内容）
  - R6.2 EARLY_STOP 还原 stash apply 失败（用户主仓被中途修改）→ 仅 warn，需手动处理。无 fixture
  - R6.3 worktree add 失败时已实现 stash 回滚，但无 fixture 覆盖
  - R6.4 merge-worktree-back 路径 B/C 下 stash 不自动 drop（有 stderr 提示）。无 fixture
  - R6.5 reward_hacking 关键词清单不全（--ignore-glob / --collect-only / pytest.mark.skipif 未覆盖），依赖 Layer 1 LLM 兜底
  - R6.6 grep 实现差异（V2.3.1 已改字面字符类兼容，BSD grep 残余风险）
- 优先级：低（各子项均有 workaround 或已部分修复，仅缺 fixture 覆盖）



## 2026-07-04 builder 一个 turn 内完成全周期导致 stop hook 从无执行机会

- 触发场景：Personal_Assistant_Bot 项目修 trigger_ci 回退。builder 进入 worktree 模式后，在同一个 turn 内连续完成：setup → 写代码 → 跑语法检查 → 跑 pytest → 说"等 stop hook 跑 PASS_CMD"→ 紧接着自己跑了 diff-level-check → 手动跑 PASS_CMD → python3 改 state 为 passed_pending_review → spawn reviewer → commit → merge-and-cleanup 删 state + worktree。全程无 turn boundary
- 现象：用户看到 builder 说"等 stop hook 跑"但 hook 从未触发。debug log 零 active-phase entry。所有 `no_op` 条目均在 merge 之后（state 已删）
- 根因：Stop hook 仅在 turn boundary 触发。builder agent 说了"等 hook"但没有 END TURN——在同一 response 内继续做了后续所有步骤。等 turn 真正结束时 state 已被 merge-and-cleanup 删除。locate-state strategy 5 逻辑已验证正确（yaml.dump 格式可匹配），不是定位失败
- 与 2026-07-02 事件「复现 2」机制相同：不是闸拦住了，是 CC 平台层的 turn boundary 在 builder 连续输出期间不会 fire Stop hook
- 优先级：高

## 2026-07-02 stop hook 在手动干预场景（unpause/reset/e2e标记）后不触发

- 触发场景：divine-word 项目 visual maturity overhaul，e2e 需要 Mac Agent 而 Mac Agent 反复不可用，builder 多次手动操作 state 文件（删 pause 文件、python3 重置 phase=active+iter=0、写 e2e_verified_head）。每次操作后用户说"loop没跑起来"
- 现象：
  1. 删除 `.claude/builder-loop/__main__.pause` 后，下一轮 Stop hook 静默 exit 0
  2. python3 写 `phase: active, iter: 0` 到 state 文件后，Stop hook 静默 exit 0
  3. python3 写 `e2e_verified_head` + `phase: completed` 到 state 文件后，Stop hook 静默 exit 0
- 根因：
  1. 事件1+2 共同命中 **L2B 闸**（HEAD == last_iter_head + git status 空 → 不跑）。state 文件在 `.claude/` 下被 .gitignore 排除，改 state 不产生 git dirty，L2B 判定"无改动"静默跳过
  2. 事件3 命中 **L1 闸**：builder 写了 `phase: "completed"`（非法值），L1 只识别 `passed_pending_review` 和 `e2e_pending` 做自愈，`completed` 走"非活跃"分支 → exit 0。即使 phase 正确为 `e2e_pending`，L1 自愈后仍要过 L2B（无 dirty → 拦）
  3. 共性：L2B 假设"无 git 改动 = 无事可做"，但在 unpause / e2e 完成标记等手动干预场景下，用户期望 hook 继续推进流程即使没有新代码改动
- 复现 2（2026-07-04）：cc-builder-loop 项目自身 V5.2 修复任务。PASS iter 1 → phase=passed_pending_review → L1 正确 exit 0（worktree 刚 commit 完干净）。之后 18 分钟内 spawn reviewer（background）、reviewer 返回、Edit SKILL.md、Write fixture、Bash 跑 fixture、python3 reset phase=active——debug log 零 entry，CC 一次 Stop 事件都没触发。与事件 1-3 不同：不是闸拦住了，是 CC 平台层根本没 fire Stop hook
- 优先级：高



## 2026-07-02 tester mock 模式与被测代码不匹配（CalledProcessError vs CompletedProcess）
- 触发场景：tester 为 `novel publish` 命令写 9 个测试，其中 3 个 mock `subprocess.run` 用 `side_effect=CalledProcessError`。但 publish 代码用 `subprocess.run` 不带 `check=True`，检查 `.returncode` 而非捕获异常。
- 现象：3 个测试失败（empty output / AssertionError），loop 多跑 2 轮修复
- 根因：tester 没有阅读被测函数的 subprocess 调用模式就选了 mock 策略
- 优先级：中


## 2026-07-02 fork subagent 批量改测试文件漏改导致 PASS_CMD 失败
- 触发场景：builder 用 fork subagent 批量替换 4 个测试文件中的 `state_after="X"` → `state_after=["X"]`。fork prompt 列了 4 个文件名但没列每个文件的命中数。fork 完成后 builder 没二次 grep 校验覆盖率，直接等 stop hook。
- 现象：`test_llm_verify_secret_revealed.py` 的 `_make_spine` 方法里 2 处 `state_after="..."` 被 fork 漏掉（fork 只改了该文件中它认为需要改的 2 处，另外 2 处在共享 helper `_make_spine` 里没被识别）。PASS_CMD stage=test 6 个用例 FAIL。
- 根因：fork subagent 继承上下文但不保证覆盖所有文件命中点——它按自己的理解筛选要改的位置，没有机械全覆盖保证。builder 也没在 fork 完成后跑 `grep -c` 确认剩余命中数=0。
- 优先级：中




## 2026-06-20 Planner Round 7 追问不完整——漏问 e2e 验证和测试深度
- 触发场景：divine-word 项目 /planner 走完整流程 7 轮追问，Round 7 只问了"EndingScene 验证方式"，未按规定追问"是否需要测试计划+深度偏好"和"是否需要端到端行为验证"
- 现象：用户发现 e2e 用例直接写进方案没经过确认，手动打断要求补 Round 7
- 根因：Round 7 规定三件事（验收方式 & 测试计划 & e2e 验证），planner 只问了第一件就认为完成
- 优先级：中


## 2026-07-01 [观察期] tampering 检测迁移到 reviewer — 待验证真篡改仍被拦截

- 修复：删除 early-stop-check.sh section 5（机器层 tampering 早停），改为 reviewer.md 步骤 2 新增「测试变更合法性审查」维度（独立 agent 语义判定）
- 根因：机器判据（测试文件变更 >= 阈值 → 早停）无法区分合法适配和篡改，3 次误杀（L3 适配/删测试/需求翻转）
- 验证条件：截止日期：2026-07-15 前无 reviewer 漏判真篡改 → 删除本条目。漏判 → 评估是否需要轻量机器护栏（如 assert 总数下降超 50% 时警告）
- 优先级：观察（已修，等验证）




## 2026-06-17 builder 步骤 5 确认记忆后调 /memory 导致用户被重复确认同一条知识

- 触发场景：builder 步骤 5 任务回顾中，用户通过 AskUserQuestion 确认了 `[记住] c1`（跳板机 script_ota 可能全程静默执行）。builder 随后调用 `/memory` skill 写入，但 `/memory` skill 有自己的完整流程（读→提炼→AskUserQuestion 确认→写入），等于同一条知识让用户确认了两次，且第二次的候选内容和第一次一模一样。
- 现象：用户反馈"这事不是刚问过我一模一样的吗"。
- 根因：builder.md 步骤 5 的 `[记住]` 路径指示"调 `/memory` 命令走完整流程"，但 `/memory` 的完整流程包含独立的确认环节，与步骤 5 的 AskUserQuestion 确认环节重复。两层确认机制叠加，用户体验为"问了两遍"。
- 优先级：低（不影响功能，但用户体验差；每次步骤 5 有 `[记住]` 候选都会触发）



## 2026-06-11 worktree pytest-html 插件冲突导致 PASS_CMD 失败被误判 test_tampering

- 触发场景：Engineering_Delivery_Bot 项目 worktree 内跑 PASS_CMD（stage=test），pytest 启动阶段 pytest-html 插件 import `py.xml` 失败（`ModuleNotFoundError: No module named 'py.xml'; 'py' is not a package`），所有测试未执行。loop 将此判为 `suspected_test_tampering` 触发早停。
- 现象：实际测试代码无任何问题（`-p no:html` 后 10/10 pass），但 loop 无法区分"插件环境崩溃"和"测试被篡改"。早停后 worktree 归档到 legacy/，需手动合并。
- 根因：worktree 继承主仓 Python 环境的 site-packages，但 pytest-html 依赖的 `py` 包与 worktree 内某个同名模块/包冲突。PASS_CMD 没有加 `-p no:html` 之类的插件隔离。test_tampering 检测逻辑把"pytest 启动失败"等同于"测试被篡改"。
- 优先级：中（环境特定，但误判导致整个 loop 流程作废）



## 2026-06-06 复杂 prompt 改造时 reviewer 多轮（2-4 轮）是必要而非冗余，不要因为多轮怀疑流程

- 触发上下文：generator 项目 P7 Phase D（outline_prompt_with_spine 加 N vs M' 三分支 + opening_pacing 反互斥）跑了 4 轮 reviewer。R1 抓 🔴 互斥死循环、R2 抓 🟡 M' = 0/负数兜底、R3 抓 🟡 op_n clamp→0 时 opening_hint 残留「前 0 章...」、R4 收口 🔵。每轮 reviewer 都挖出**前轮修法引入的新边界 / 历史漏的边界**——不是消耗。复杂 prompt 改造（多个条件分支 + 与下游约束耦合 + 极端配置组合）的边界穷举单轮人脑/单轮 reviewer 都难覆盖，多轮反馈是「逐层暴露盲区」的正常路径。
- 建议方向：
  1. **builder.md 步骤 5 回顾段加注释**：明确「prompt 改造类（不仅是 prompt 文案微调，是带条件分支 / 与外部上下文耦合的实质性 prompt 重写）reviewer 3-4 轮属正常，不要在第 2 轮怀疑流程/降低判据/早合主线」
  2. **改动级别判定**：在「改动级别机械检测」表加一行——若 diff 涉及「prompt 多条件分支 + 与同 prompt 其他段（如 opening_hint）耦合」→ 标 L2.5 提示 reviewer 走多轮
  3. **轻量替代**：reviewer-fallback.md 加一句「PASS_CMD 通过 + 4 轮 reviewer 收口 + 全集 1300+ 测试绿 = 健康路径，不要焦虑」
- 优先级：低（流程本身没问题，是 builder 心智模型问题；不修也能正常工作，只是 builder 信心可能波动）



## 2026-06-05 tester 应主动用 `@pytest.mark.xfail(strict=False)` 当缺陷探针，让黑盒发现"实现 ↔ 契约不符"既明示又不阻塞 loop

- 触发上下文：Engineering_Delivery_Bot 这次自检告警按约车人路由 + script_ota 完成艾特通知改造，spawn tester 黑盒补测时，tester 发现 `get_current_booker_name(vid)` 的接口签名注释里写"不抛异常"，但实现里 `query_car_schedules` 抛 `ConnectionError` 会直接向外传播——典型的"实现层兜底缺失"。tester 没有 fallback 到"跳过"或"标 fail"两条死胡同，而是主动写了一个会抛异常的探针 case + `@pytest.mark.xfail(strict=False, reason="疑似缺陷：未捕获网络异常...修复后变 pass")`。这次操作让：① loop PASS_CMD 继续绿（xfailed 不阻塞）；② builder 拿到「契约和实现有 gap」的明确信号；③ builder 加最外层 `try/except` wrapper 后，这条 case 自动从 xfailed 变 xpassed，作为永久回归保护
- 建议方向：
  1. **tester SKILL.md / agents/tester.md 加一节「契约与实现 gap 处理」**：明示 tester 黑盒下若发现"签名/docstring 承诺 X 但 mock 抛异常实测实现做不到"时的推荐姿势是 `xfail(strict=False, reason="...")` + 探针 case，而不是删 case 或标 fail。模板示例：「当接口签名注释要求『任何输入返 None 不抛』，但内部依赖（如 HTTP 客户端）会抛 ConnectionError 且实现外层未 try/except 时，写 `with patch(...side_effect=ConnectionError("...")): assert fn() is None` 并加 xfail strict=False 标注」
  2. **tester 返回摘要中显式列 xfailed case 列表**：让 builder 在 3a 决策时立刻看到「tester 发现可疑契约 gap」清单，配合「采纳→修实现，自动 xpass；拒绝→保留 xfail 作为已知边界」两条路径
  3. **配套 builder.md 步骤 3a 加分支**：tester 返回 xfailed > 0 时，builder 必须逐条响应「采纳 / 拒绝并记理由」，禁止跳过
- 优先级：中（黑盒测试场景每次 spawn tester 都可能触发；当前 SKILL 没明示，tester 是否会自发想到这招完全靠模型自由发挥）

## 2026-06-05 loop 启动前/期间不允许遗留长 import 的后台进程，否则 pass_cmd reexport 阶段会 flaky timeout

- 触发上下文：Engineering_Delivery_Bot 这次刚改完代码、loop 还没起的时候，builder 想做 smoke import 验证（`python -c "from service import fanwei_client; from service import ws_health_check; from handler import ops_ota; ..."`），用 `run_in_background: true` 丢后台。bot 项目的这几个模块 import 链路触发 PG 连接，没 .env 时连接会卡住——后台进程一直占着 Python 启动 / module 编译相关资源。紧接着 stop hook fire 跑 `pass_cmd: python3 scripts/check_reexport.py`，正常 3s 跑完的检查撞 30s timeout。builder 第二轮无改动让 loop 重跑，第二次就过了，证明纯 flaky。这种 contention 在 hook 日志里完全看不出根因，初看像代码 bug
- 建议方向：
  1. **builder.md 「检查 loop.yml」段加一条警告**：进入 loop active 前禁止留 `run_in_background: true` 的长 import / DB 连接 / 网络请求类 Bash 任务；要做 smoke 也必须同步（`run_in_background: false`）+ 显式短超时（如 `timeout 5 python -c ...`）。理由是 stop hook 触发的 pass_cmd 跑在同一 host 上，长后台 Python 进程会拖慢 module 编译 / interpreter startup
  2. **setup-builder-loop.sh 输出加一行 reminder**：脚本 setup 成功后顺手提醒「⚠️ 如有 Bash 后台任务（特别是 Python import / DB / 网络）请先 TaskStop 再进 loop，避免 pass_cmd flaky timeout」
  3. **run-pass-cmd 检测同主机活跃 background Bash 任务**（如可行）：在 timeout 失败时自动追加日志「检测到 N 个 builder 启动的 background 任务可能影响 startup 性能，请考虑 TaskStop」，把"flaky"和"真 bug"快速分流
- 优先级：中（不是每次 loop 都踩，但踩了一次会浪费一轮 PASS_CMD + 一轮 builder 判断 + 一次 stop hook tick）

## 2026-05-31 reviewer 对方法名存在性无校验能力，需项目侧基建兜底

- 触发上下文：builder 写 `self._reply(ctx, ...)` 但 BaseAgent 只有 `_send_text`。reviewer 看 diff 文本无法做符号解析，未发现方法名错误。测试只覆盖纯逻辑层（hviz_updater），handler 层无冒烟测试。bug 流到线上在飞书触发时才爆 AttributeError。
- 建议方向：
  1. **reviewer prompt 无法根治**（LLM 不做符号表查询），应由项目侧 pass_cmd 兜底
  2. 建议 loop 文档加「最佳实践」建议：新增 tool handler 必须有一个冒烟测试（mock BaseAgent + CallContext 调一次 execute_tool），pass_cmd 拦住 AttributeError
  3. 长期：建议项目接入 mypy basic（pass_cmd 加 `mypy --ignore-missing-imports agent/`），编译期拦住所有方法名错误
- 优先级：中（已在业务项目 improvements.md 落地了 TOOL_DEFINITIONS 一致性检查 + 冒烟测试，loop 侧只需文档建议）

## 2026-05-31 loop「没跑起来」真相是 state 被 silenced（.yml.silenced-YYYYMMDD 改名）

- 触发上下文：跨 session 接手 builder-loop 任务，用户反馈「loop 没跑起来」。排查：`builder-loop/state/<slug>.yml` 用 `cat` 读为空。`ls` state 目录发现文件被改名为 `<slug>.yml.silenced-20260530`——Stop hook 的 locate-state 只认 `.yml` 后缀，找到非 `.yml` 就静默跳过、什么都不做。查 `stop-hook-debug.log` 该 slug 历史确认 loop 其实早已跑完：iter1 FAIL→continue、iter2 PASS→auto-commit→phase=passed_pending_review，之后才被 silenced 卡住。恢复 = 把 `.silenced-*` 改回 `.yml`，loop 即从 passed_pending_review 继续。
- 建议方向：① loop「看似死掉 / 没触发」第一排查动作：`ls builder-loop/state/` 看有无 `.silenced-*` / `.paused` 改名，及 `grep <slug> stop-hook-debug.log | tail` 看最后 phase。② 考虑 Stop hook 在找不到 `.yml` 但存在同 slug `.silenced-*` 时输出一行可见提示（而非完全静默），降低「以为没配置 loop」的误判。
- 优先级：中（silence 是有意机制，但静默到无任何痕迹时，跨 session 接手者极易误判为 loop 未配置/失效）

## 2026-05-29 install.sh 不感知 cc-switch common 覆盖，hook 注册改完会被切 provider 打回

- 触发上下文：用户 settings.json 的 builder-loop hooks 停在 V3.1 之前（SubagentStart 指废弃的 tester-lock-write.sh、SubagentStop 多余 matcher、缺 worktree-write-guard）。排查发现 install.sh 其实早跑过（subagent-start-guard.sh / worktree-write-guard.sh 软链都在 ~/.claude/scripts/），但用户用 cc-switch 管理多 provider——cc-switch 的 common 底板里存着旧 hooks，每次切 provider 用 common 重写 settings.json，把 install.sh 注册的新 hooks 覆盖回旧的。install.sh 只改 settings.json、对 cc-switch 这一层完全无感知。本次靠手动跑 install.sh + 把对齐后的 hooks 同步进 cc-switch common（cc-switch config common set）才根治。
- 建议方向：① install.sh 注册 hook 前探测 cc-switch（`~/.cc-switch/cc-switch.db` 存在或 `cc-switch` 在 PATH）。② 探测到则显式警告：「检测到 cc-switch，settings.json 的 hook 改动会被切 provider 时的 common 底板覆盖，请把 hook 同步进 cc-switch config common」。③ 进阶：install.sh 提供 `--sync-cc-switch-common` 选项，自动把注册的 hooks 合并进 cc-switch common。④ 至少在 README/安装文档写明 cc-switch 用户这个坑。
- 优先级：中（仅影响 cc-switch 多 provider 用户，但他们每次切 provider 都被打回，且极难自查——表现为「明明 install 过 hook 却不生效」）

## 2026-05-27 worktree 路径被 CC 内部安全机制硬拦，Read/Edit/Bash 全无法访问

- 触发上下文：generator 项目 #269 secret 知识隔离，builder-loop setup 创建 worktree 到 `.claude/worktrees/` 下。builder 尝试用绝对路径 Read/Edit/Bash(head/ls/cd) 访问 worktree 内文件，全部被 CC 拦截（"File is in a directory that is denied by your permission settings"），即使 settings.json allow=["*"] 且用户确认没有弹窗（自动拒绝）。最终用 `EnterWorktree(path=...)` 工具切入已有 worktree 后才能正常读写。
- 建议方向：① 排查 CC 对 `.claude/` 子目录的内置 deny 规则边界（skills/commands/agents/plans 似乎可访问，worktrees 不行）② setup-builder-loop.sh 输出中建议用户使用 `EnterWorktree` 而非 `cd` ③ 或考虑将 worktree 创建到 `.claude/` 外部（如项目根的 `.worktrees/`）绕开安全机制
- 优先级：低（2026-06-15 验证：当前 CC 版本 Read/Write .claude/worktrees/ 路径无拦截。疑为旧版本行为或项目特定配置。降级，复现时再修）

## 2026-05-26 巨型 diff（8000+ 行）下 reviewer subagent 审查深度不足

- 触发上下文：Engineering_Delivery_Bot 项目 Phase 2 拆分 `service/vehicle_ws.py`（3364 行 → 8 个子模块），产生 8122 行 diff（19 文件，+3889/-3543）。builder 将完整 diff_file 路径和 diff_summary 传给 reviewer subagent。reviewer 返回的报告确实找到了 `_ws_loop` 按值绑定这个真实 bug（🔴），但用户质疑"这行吗，审的到细节吗？等于没怎么审"。
- 事实：reviewer subagent usage: 70128 tokens / 9 tool_uses / 192s。8122 行 diff 超出单次阅读能力，reviewer 实际策略是读 worktree 内的新文件（而非逐行 diff 对比），因此能发现 import 绑定类问题，但无法验证"原文件的每个函数是否完整搬运、是否有遗漏或多余代码"。对于纯机械搬运型拆分，当前 reviewer 流程缺少"拆分完整性校验"（如原文件函数数 vs 新文件函数数的自动化对账）。
- **后续实锤**（同日 20:35）：Phase 3 拆分 `card/vehicle_card.py` 时，`_PHASE_PROGRESS` 常量未加入 re-export 层，reviewer 未发现。Phase 4 拆分 `agent/vehicle_agent.py` 时，`import config as cfg` 遗漏，reviewer 也未发现。两个 bug 都在用户手动触发 OTA 刷包和 investigate 功能时才暴露（生产炸了），427 个单测全过但没覆盖这两条路径。
- **根因**：re-export 层的导出完整性没有自动化校验。拆分产生的 re-export 文件声称"所有外部 import 路径保持兼容"，但没有机制验证这个声明——哪些名字被外部引用了、re-export 是否全部覆盖，纯靠人/reviewer 目视。
- 优先级：中

## 2026-05-26 模块拆分后 re-export 完整性可作为 PASS_CMD 自动校验

- 触发上下文：上述巨型 diff 审查不足的直接后果。用户要求写一个"导出完整性校验"脚本，扫描所有 `from X import Y` 语句，验证 re-export 层确实导出了 Y。该脚本已作为项目级 PASS_CMD 加入 loop.yml。
- 事实：这类"声明式兼容层 + 自动化校验"模式不限于本项目——任何涉及 re-export / barrel file / __init__.py 聚合导出的拆分场景都适用。loop 框架侧可以考虑：(1) 在 reviewer prompt 中对"新增 re-export 文件"场景自动追加导出完整性检查指令；(2) 提供通用的 re-export 校验脚本模板供项目接入。
- 优先级：低

## 2026-05-26 假活僵尸 state GC — session 崩掉后 active state 无人回收

- **触发上下文**：V3.4 任务收尾时发现 3 个 5 月 23 号创建的 state（task-alpha/beta/gamma），`phase=active` + `iter=0` + 创建 3 天无人碰。是 V3.2 开发期间 session 非正常退出留下的。这些假活 state 导致 locate-state.sh 策略 5 误绑（bare NOOP stop hook 被无意义地触发）。另有一个 4 月 24 号的孤儿 worktree 同理。
- **根因**：session 崩溃 / 用户直接关终端时不会走 abandon-loop，state 保持 `phase=active` 永久留在磁盘。现有 zombie-selfheal 只处理 `stopped_reason` 非空的已停 state，不处理"声称 active 但无人驾驶"的假活。
- **建议方向**：
  1. **setup 时检测假活**：扫 `state/*.yml` 找 `phase=active` + `iter=0` + `created_at` 超过 24 小时的 → 提示「发现 N 个疑似假活 state，要归档吗？」+ 列出 slug/创建时间/worktree 路径
  2. **或 stop hook 加 TTL 检测**：`phase=active` + 最近一次 `last_iter_head` 更新超过 N 小时 → 自动归档到 legacy（风险：正在跑但暂停的 loop 也会被回收，需结合 `.pause` 文件排除）
  3. **最轻量**：V3.3 孤儿 worktree 检测扩展——在 setup 的孤儿检测里同时扫假活 state，合并到同一个用户决策流程
- **优先级**：低（V5.1 session_id 匹配 + V3.7 session mismatch 校验已解牙——僵尸不再劫持新 session，仅污染 state 目录和 active 计数；手动清理可绕过）

---

---

## 2026-05-19 worktree dogfooding 限制：diagnose fixture 在 worktree 内永远 fail

- **触发上下文**：V3.1 修改 `diagnose-stop-hook.sh` 期望新 hook 名（subagent-start-guard.sh 等），但活跃系统 `~/.claude/settings.json` 仍注册旧 hook 名。`test-stop-hook-debug-log.sh` 的 A4 段跑 diagnose 检查活跃系统 → 必 fail。PASS_CMD stage `v25_stop_hook_observability` 因此永远过不了，直到 merge + install.sh。
- **建议方向**：
  1. **fixture A4 段改为 self-contained**：不查活跃系统 `~/.claude/`，改为在 temp HOME 下跑 install + diagnose（跟 test-plan-detection.sh 的做法一致）
  2. **或者 fixture A4 跳过活跃系统检查**：检测到 CWD 在 worktree 内时，A4 只验证 diagnose 脚本语法正确 + dry-run，不验证活跃系统 hook 状态
- **优先级**：中（每次改 hook 名/注册都会撞；workaround 是手动跑 PASS_CMD 确认只有这一条 fail）

---

## ~~2026-05-19 早停后 setup 应检测孤儿 worktree 并提示复用~~ ✅ 已关闭（2026-05-25 落地，V3.3）

> 已落地：setup-builder-loop.sh 新增孤儿 worktree 检测（exit 6）+ `--reuse-worktree <path>` 复用 + `--ignore-orphans` 跳过。fixture test-orphan-worktree-reuse.sh 38 assertions 覆盖。

- ~~**触发上下文**~~：早停后旧 worktree 保留（含改动），但 state 归档。重新 setup 创建新 worktree，旧改动丢失。根因：setup 每次按 timestamp 生成新 slug，不检查已存在的可复用 worktree。
- ~~**方案（2026-05-23 对齐）**~~：setup 开头扫 worktree 目录，发现孤儿（worktree 存在但无对应 active state）→ 提示用户"复用 / 新建"。复用 = 写新 state 指向已有 worktree + reset iter=0。
- ~~**根因**~~：setup 的 stash 机制只搬运"主仓 dirty"（setup 前主仓的未提交改动），不搬运旧 worktree 的改动。早停归档 state 后，旧 worktree 的改动跟新 loop 没有关联。
- ~~**建议方向**~~：
  1. **setup 增加 `--reuse-worktree <path>` 参数**：检测到指定 worktree 存在且有未提交改动时，直接在该 worktree 上创建新 state（不新建 worktree），复用已有代码
  2. **或者早停后提供 "resume" 路径**：早停归档 state 时在 stderr 额外输出 `resume 命令: setup-builder-loop.sh --reuse-worktree <旧path>`，让 builder 一键复用
  3. **或者早停不归档 state，改为 pause**：早停后把 phase 设为 paused 而非归档到 legacy，stop hook 的 L3 闸（.pause 文件）静默不跑，但 state 和 worktree 关联保留。用户决定继续时删 pause 即可
- **优先级**：中-高（任何早停后想继续修的场景都会踩；手动 cp 十几个文件极易遗漏）

---

## 2026-05-13 step 3.5.5 plan.md "恰好一行" 约束对 P0 专项太浅
- 触发上下文：#150 P0 专项内部有 3 张带状态列（✅/⚠️/❌）的表格 + 8 条问题清单 + 4 个 Phase 进度。builder 只在标题加了 `✅` 一个字，内部表格的 ⚠️/❌ 全没更新。被用户发现后补更新
- 根因：step 3.5.5 指令是"输出恰好一行"，对小改动够用但对 P0 专项（含内部状态表格）太浅。builder 没有被要求 Read plan.md 内容逐项检查
- 建议方向：区分任务规模——小改动（单条 #N）→ 一行标记；P0 专项或含内部状态表格的条目 → Read 对应段落 → 列出哪些状态列需更新 → 逐项 Edit
- 优先级：中


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

## 2026-05-10 reviewer subagent 对大文件读有限窗口导致误判
- 触发上下文：#69 第三轮 reviewer 声称 engine.py 里没有 registry_gate 调用（实际在 1858 行），原因是它只读了 1836-1855 行的窗口。这类 false positive 会浪费一轮 loop 迭代
- 建议方向：reviewer prompt 补充指引「对大文件（>500行）声称'代码不存在'前，grep 确认」；或 reviewer 工具链增加全文 grep 能力
- 优先级：低

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

## 2026-05-09 doc-maintainer 在 worktree 改动会被 PASS auto-commit 抢跑

- **触发上下文**：BOT 项目「上电自检告警每日封顶限频」任务，PASS_CMD 通过后 stop hook 在 spawn doc-maintainer 之前已经把 worktree 改动 auto-commit（commit `b4b780d`）。doc-maintainer 之后在 worktree 内 Edit 了 `CLAUDE.md`（加一条核心约束条目），但这次 Edit 没有任何机制 commit；最终 `merge-and-cleanup.sh` fast-forward 主线时这一行 doc 改动**没合进来**。Builder 只能在主仓手动 Edit + commit 一次（`e341b31`）补救。
- **建议方向**：
  1. step 3.5 文档评估在 V3.0 reviewer-as-gate 路径下要明确"doc-maintainer 改完必须 builder 主动 amend 进上一个 PASS commit 或追一个 followup commit"，写进 `builder.md` step 3.5
  2. 或者：`merge-and-cleanup.sh` 合主线前先 `git status --porcelain` 检查 worktree 是否有 uncommitted 改动，有则 stage + amend 进最近 commit，否则 fast-forward 会丢
  3. 或者：doc-maintainer SKILL prompt 末尾加 "Edit/Write 完成后，自己用 `git add <files> && git commit --amend --no-edit` 把改动并入上一个 commit"
- **优先级**：中（doc 漏 commit 不影响功能、能事后人肉补；但 silent loss 让人很难发现，比如这次是兜底自审顺手 grep 才发现 main 上没有那一行）

## 2026-05-09 main-dirty stash apply 进 worktree 的语义需要在 setup 输出 / commit message 之外多一处显式提示

- **触发上下文**：BOT 项目同上任务，主仓有 3 个 dirty 文件 + 1 个 untracked，setup-builder-loop.sh 通过 stash apply 把它们带进了 worktree，最终 `merge-and-cleanup.sh` fast-forward 时一并 commit 进主线（commit message 后缀 `[+3 main-dirty]`）。Builder 看 commit 文件列表时一开始疑惑"为什么 fs_webhook/preview_card.py 也在里面"，需要回看 setup 时的输出 / commit message 后缀才能定位。这是设计行为不是 bug，但 AI / 人在排查时容易误判为 loop 机制把无关文件偷偷塞进了 commit。
- **建议方向**：
  1. `builder.md` 的"前置 loop 检查"段加一条 hint："若主仓有 dirty，setup 会把它们 stash apply 到 worktree，最终 fast-forward 合主线时这些 dirty 也会一并 commit。Builder 的 changed_files 报告应清楚区分 task-related vs main-dirty。"
  2. `setup-builder-loop.sh` 输出"⚠️ 主仓 dirty N 个，已 stash apply 到 worktree，将随本次任务一并 commit"加粗或加颜色
  3. Reviewer 主体范围应自动按 `pre_loop_dirty_files` state 字段去除 main-dirty 文件
- **优先级**：中（不影响功能但严重影响排查体验，AI 看 commit 容易误判）

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

## 2026-04-30 reviewer-timing-check.sh 在多 active loop 同 cwd 场景误拦其他 session 的 reviewer

- **触发上下文**：BOT 项目 session A 跑 vehicle-skill jumpbox loop（slug=1777532719-vehicle-skill）已 PASS + auto-commit + worktree merged 删除；同时段 session B 跑 deepperf migration loop（slug=1777532136-deepperf）仍 active。session A spawn reviewer subagent 时 PreToolUse hook `reviewer-timing-check.sh` 触发 → `locate-state.sh "$PWD"`（PWD 是主仓）→ 沿 PROJECT_ROOT 找到 `.claude/builder-loop/state/` 下任意一个 state 文件 → 命中 deepperf state（active=true）→ exit 2 拒绝 reviewer。但 session A 的 loop 已经 PASS 干净，被拒纯属误拦；只能走自审兜底（builder.md 3c 兜底，写自审 review report 占位）。locate-state.sh 没有传 slug 区分本 session 自己的 loop 与同 cwd 下别人的 loop。
- **根因**：locate-state.sh 按 cwd → PROJECT_ROOT 逆推 state 时是「找一个就行」的语义（多个 active state 时返回首个），而 reviewer hook 想要的语义是「**我自己这个 loop** 的 state 是不是 active」。多 session 并发改同仓时，hook 的"按 cwd 定位"不够精确。
- **建议方向**：
  1. **首选**：reviewer hook spawn 时把当前 session 自己的 slug 通过 ENV（如 `BUILDER_LOOP_SLUG`）或 stdin tool_input.metadata 传给 hook；hook 优先读这个 slug 找精确 state，没传再 fallback locate-state.sh 模糊匹配。Builder skill prompt 调用 `setup-builder-loop.sh` 时把返回的 slug 写进当前 session 的 env / `.claude/builder-loop.local.md`，spawn reviewer 前由 builder skill 在 Agent prompt 里带上。
  2. **退而求其次**：locate-state.sh 加 `--my-only` 选项，按 owner_cwd 匹配的同时只返回 active=false 的 state（既然已结束就放行），active=true 的检查改为「除我以外是否还有 active」（但这样写仍可能误拦真正的并发场景，治标不治本）。
  3. **进一步**：reviewer hook 拒绝时返回的 deny message 应包含被命中的 slug，方便用户 / 上游 builder 当场判断是不是误拦（现在只说 "loop active"，看不出是哪个 loop）。
- **优先级**：中（多 session 并发同仓不是日常但确实会发生；触发时只能走兜底自审，对 reviewer 价值打折）

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

