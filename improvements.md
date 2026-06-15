# Builder-Loop 改进清单

> 时间倒序。每条按 builder.md 步骤 5 模板（触发上下文 / 建议方向 / 优先级）。
> 立项不等于本期实施——A 类候选清单，等独立任务挑出来落地。
> 已关闭条目见 [CHANGELOG V3.2](CHANGELOG.md#v32-跨越界隔离--测试框架2026-05-23)

## 2026-06-11 worktree pytest-html 插件冲突导致 PASS_CMD 失败被误判 test_tampering

- 触发场景：Engineering_Delivery_Bot 项目 worktree 内跑 PASS_CMD（stage=test），pytest 启动阶段 pytest-html 插件 import `py.xml` 失败（`ModuleNotFoundError: No module named 'py.xml'; 'py' is not a package`），所有测试未执行。loop 将此判为 `suspected_test_tampering` 触发早停。
- 现象：实际测试代码无任何问题（`-p no:html` 后 10/10 pass），但 loop 无法区分"插件环境崩溃"和"测试被篡改"。早停后 worktree 归档到 legacy/，需手动合并。
- 根因：worktree 继承主仓 Python 环境的 site-packages，但 pytest-html 依赖的 `py` 包与 worktree 内某个同名模块/包冲突。PASS_CMD 没有加 `-p no:html` 之类的插件隔离。test_tampering 检测逻辑把"pytest 启动失败"等同于"测试被篡改"。
- 优先级：中（环境特定，但误判导致整个 loop 流程作废）

## 2026-06-08 reviewer subagent prompt 硬约束三件套：必须 Write 报告 + 必须打 REVIEW_SUMMARY + 工作节奏限制

- 触发上下文：BOT 项目 OTA 分流 DZ9278 二审 spawn reviewer 后，agent 在中途说「现在读 worktree 里的关键源文件进行交叉验证」就 status=completed 返回，但**没有 Write 报告到 report_path、没有打 REVIEW_SUMMARY 行**。builder 检测到 report 文件不存在 → 走重试路径（按 3b）→ 重新 spawn reviewer。重试 prompt 里 builder 主动加了三段硬约束「必须 Write report_path / 末尾必须 REVIEW_SUMMARY / 工作节奏建议防无限交叉验证」，重试 agent 一次过出完整报告 + 落盘。说明：reviewer subagent 自己的 SKILL prompt 没有把这三件设为前置必做条件，靠 builder 每次重试 prompt 临时加。这种 trace 跑偏不出报告的失败模式可能反复出现。
- 建议方向：
  1. **改 reviewer.md / reviewer subagent 系统 prompt 顶部加三段硬约束**（不是 builder 调用时传 prompt 加，是 SKILL 本体加）：
     - 强制：报告必须 Write 到入参 `report_path`（不止 INLINE 在最终消息）
     - 强制：最终消息末尾必须有 `REVIEW_SUMMARY: 🔴X 🟡Y 🔵Z | 结论:... | 报告:<path|INLINE>` 一行
     - 节奏限制：读 diff 后选关键文件直接 Read 一次完成判断，**禁止以「再交叉验证一下」为由反复 Read 同一文件**；交叉验证靠 grep 单次确认而非反复阅读
  2. **builder.md 步骤 3a 兜底分类增加**：检测到 reviewer 返回了 `status=completed` 但**报告未落盘 + 无 REVIEW_SUMMARY** → 直接走 3b 重试（不当 success 处理），重试 prompt 自动注入三段硬约束（builder 不用临时拼）
  3. **reviewer subagent 完成消息模板**：要求最后一段必须以 `REVIEW_SUMMARY:` 起始的整行作为收尾标志；缺失则 builder 视为"未完成"
- 优先级：中（一次失败成本就是一次重试 + builder 额外 prompt 工程时间；高频任务里频率不低，加硬约束收益清楚）

## 2026-06-08 planner SKILL「文件地图」段加硬约束——不允许估算数字

- 触发上下文：BOT 项目 OTA 分流 DZ9278 plan 写「**11 个函数**改造」，进 builder 实施后 grep 实际是 **12 个函数 / 14 处 client.post 调用**。原因是 planner 写文件地图时 grep 不严谨——只数了 service/ota_service.py 顶层公开函数没数 `search_devices_and_versions` 内部三处 client.post + `poll_upgrade_progress`。Builder 阶段补 grep 后才发现，差点漏改。更严重的是：plan §4「不动调用方」表也没列 `util/upgrade_poller.py` —— 这是 `poll_upgrade_progress` 的唯一 caller，builder 没漏改但 reviewer 一审抓 🔴 漏传 `_car_hint`。根因是 planner 在写文件地图时只看了 vehicle_agent 的几个 tool，没 grep 公开函数的所有 caller。
- 建议方向：
  1. **planner SKILL.md 加硬约束**：方案文件「文件地图」段写"N 个函数 / N 处调用 / N 个 caller" **必须** 是 grep 结果而非估算。具体可加一句「凡写数字 N 必须背后是一条 grep 命令；不允许"约 X" / "大概 X" / "至少 X"；不确定就实际跑 grep 拼数字」
  2. **planner SKILL.md「文件地图」补充扫描步骤**：除了改动文件，必须 grep 每个公开函数的所有 caller，列入「调用方影响面」子表。本次 `poll_upgrade_progress` 漏列 caller `upgrade_poller.start_polling` 就是这种漏扫
  3. **builder「准计划」阶段交叉校验**：builder 拿到 plan 后第一动作 = 对 plan 里的每个"N 处"用 grep 反查，对不上直接回 planner 修；这是机械化校验，1 分钟内完成。可加到 builder.md「读方案文件时按角色视图过滤」之后
- 优先级：中（每次 plan 写得不精确，builder 都得补 grep + 可能漏改 caller → reviewer 抓 🔴 → 多一轮回合；落地后 plan 质量稳定提升）

## 2026-06-06 复杂 prompt 改造时 reviewer 多轮（2-4 轮）是必要而非冗余，不要因为多轮怀疑流程

- 触发上下文：generator 项目 P7 Phase D（outline_prompt_with_spine 加 N vs M' 三分支 + opening_pacing 反互斥）跑了 4 轮 reviewer。R1 抓 🔴 互斥死循环、R2 抓 🟡 M' = 0/负数兜底、R3 抓 🟡 op_n clamp→0 时 opening_hint 残留「前 0 章...」、R4 收口 🔵。每轮 reviewer 都挖出**前轮修法引入的新边界 / 历史漏的边界**——不是消耗。复杂 prompt 改造（多个条件分支 + 与下游约束耦合 + 极端配置组合）的边界穷举单轮人脑/单轮 reviewer 都难覆盖，多轮反馈是「逐层暴露盲区」的正常路径。
- 建议方向：
  1. **builder.md 步骤 5 回顾段加注释**：明确「prompt 改造类（不仅是 prompt 文案微调，是带条件分支 / 与外部上下文耦合的实质性 prompt 重写）reviewer 3-4 轮属正常，不要在第 2 轮怀疑流程/降低判据/早合主线」
  2. **改动级别判定**：在「改动级别机械检测」表加一行——若 diff 涉及「prompt 多条件分支 + 与同 prompt 其他段（如 opening_hint）耦合」→ 标 L2.5 提示 reviewer 走多轮
  3. **轻量替代**：reviewer-fallback.md 加一句「PASS_CMD 通过 + 4 轮 reviewer 收口 + 全集 1300+ 测试绿 = 健康路径，不要焦虑」
- 优先级：低（流程本身没问题，是 builder 心智模型问题；不修也能正常工作，只是 builder 信心可能波动）

## 2026-06-06 builder spawn reviewer 前必须主动列「边界穷举矩阵」让 reviewer 不靠运气抓边界

- 触发上下文：generator P7 Phase D 4 轮 reviewer——每轮 🟡 都是「边界配置」抓的（chapters_count=1 / first_n_chapters=0 / first_n_chapters=负数 / op_n ≥ chapters_count）。builder 在 Round 1/2 spawn reviewer 时没主动列边界穷举清单，reviewer 抓到 🟡 全靠它自己想边界场景。如果 builder 在 spawn prompt 里写明「请验证以下边界：op_n=0 / op_n=负数 / op_n>=chapters_count / chapters_count=1 / opening_pacing=空 dict」，reviewer 可以一轮命中两到三个边界 🟡，少 1-2 轮回合。
- 建议方向：
  1. **builder.md「触发 Reviewer Subagent」段加默认 prompt 字段「边界穷举」**：明示 builder spawn 前要列改动函数所有参数的极端值（0 / 负数 / None / 空 dict / 远大于 / 边界相等）穷举矩阵；reviewer 收到后逐项 verify。这与同日「重点关注 N 项」立项条目互补——「重点关注」是 builder 心里有数的逻辑怀疑点，「边界穷举」是参数边界机械化清单
  2. **builder「准计划」阶段引导**：写「边界穷举」时强制 builder 在 Edit 完代码后、commit 前**先 grep 调用方拿到所有参数 dict** + **列每个参数的极端值** + **手动跑 1-2 个边界用例**，把「自己心里没穷举的边界」结构化外化
  3. **配套 reviewer.md**：reviewer prompt 拿到「边界穷举」段后强制 Read 函数实现 + 逐参数模拟极端值看 prompt 输出是否合理（不只是看代码是否处理，而是看渲染产物是否自洽）
- 优先级：中（与「重点关注 N 项」立项条目同等级，两者搭配可显著降低 reviewer 轮次）

## 2026-06-06 builder spawn reviewer 时应在 prompt 默认明列「重点关注 N 项」让 reviewer 命中预怀疑盲区

- 触发上下文：Engineering_Delivery_Bot script_ota 下载字节进度量化任务（A2 watcher）一轮 reviewer。builder 在 spawn reviewer 的 prompt 里手工写了「重点关注 4：watcher 起点 vs phase 默认是否冲突造成进度条倒退」，reviewer 才命中 🔴。如果没写这条，watcher 跑 5–50% 段 / phase_progress["pkg_download"]=40 这种「双方默认值不一致」的隐患很可能漏审，等用户实际用刷包再撞才发现。spawn 同期还跟着写了重点关注 1-6 共 6 项，最后 reviewer 命中的 🔴/🟡/🔵 几乎全部命中我预先怀疑的点
- 建议方向：
  1. **builder.md「完成后触发 Reviewer Subagent」段加默认 prompt 字段「重点关注（builder 心里有数的 N 项）」**：明示 builder spawn 前要列 1-5 个具体怀疑点（不是泛泛的"测覆盖"或"代码质量"，而是「函数 X 与函数 Y 的状态字段是否对齐」「修了 A 是否影响 B」这种点对点怀疑）；reviewer 收到后逐项 verify
  2. **reviewer.md agent prompt 加节「prompt 含『重点关注』时优先逐项验证后再做自由发挥审查」**：避免 reviewer 拿到重点关注列表后还按通用模板全扫，浪费 token
  3. **配套 builder「准计划」阶段引导**：写「重点关注」时强制 builder 自问「我现在最担心的 3 个出错点是什么」，把这种「自己心里的怀疑」结构化外化，不让 reviewer 靠运气
- 优先级：中（每次 spawn reviewer 都能用上，对 reviewer 命中率提升显著；但需要改 SKILL prompt + agent prompt 两处）

## 2026-06-06 reviewer 建议前应自检「这个修复会不会引入反向副作用」防一轮 well-meaning 二轮翻车

- 触发上下文：同上任务。一轮 reviewer 看到 watcher `stop()` 只 set event 不 join，提 🔵 建议「加 join 防 completed 后还多推一帧字节%」。builder 按建议加了 `self._thread.join(timeout=self.interval + 5)`。二轮同一个 reviewer 自己又抓出 🟡：「这个 join 在 `_wrapped_cb` 同步调用链里执行，extracting 关键词命中时会阻塞 stdout 读取循环最多 15s」。也就是 reviewer 一轮提的"看似稳妥的修复"自己二轮就发现引入了新的同步阻塞副作用——这条要是上线就是一次回归
- 建议方向：
  1. **reviewer.md agent prompt 加自检步骤**：每条 🟡 / 🔵 建议在最终输出前自问「我建议的这个修复是否会引入：① 新的同步阻塞 / ② 死锁 / ③ 资源泄漏 / ④ 跨模块语义破坏？如果会，是 demote 到「仅观察」还是给完整修法（含副作用规避）」。三档输出：「建议+修法+副作用清单」「仅观察」「彻底拒绝原方案」
  2. **builder.md 步骤 3a 决策表加分支**：reviewer 给 🟡 / 🔵 时 builder 必须复核「建议的修复会不会引入新副作用」，引入即视为「拒绝原方案 + 反馈给 reviewer 让它出更稳的修法」而非闷头改
  3. 长期：reviewer 二轮发现自己一轮提的建议引入副作用时，自动追加一条 meta 反思「我下次类似建议前应先做副作用检查」喂回本 improvements
- 优先级：中（reviewer 的"修建议"质量直接决定 builder 的工作量和回归风险）

## 2026-06-06 setup-builder-loop 启发式抓 plan_file 可能抓到无关 plan，支持 --no-plan / --plan=<path> 显式参数

- 触发上下文：同上任务。setup-builder-loop.sh 自动给本次任务关联了 `.claude/plans/20260601-single-vehicle-online-query.md` 作为 `plan_file`，但本次任务是 script_ota 下载进度量化，跟 single-vehicle-online-query 八竿子打不着。setup 大概是按 `.claude/plans/` 最新文件抓的，没做任务描述匹配度判断。后果是 step 3a+ TESTER_HINT 处理时 builder 纠结要不要 spawn tester——按规则 spawn 要走 `split-plan-by-role.sh <plan_file> tester` 但 plan_file 内容跟任务无关，tester 会拿到错的 spec_view。最终 builder 跳过 spawn tester 自补，违反了硬规则但没办法
- 建议方向：
  1. **setup-builder-loop.sh 加 `--no-plan` 参数**：显式跳过 plan_file 关联，state.plan_file 写空字符串，后续流程不走 split-plan
  2. **setup-builder-loop.sh 加 `--plan=<path>` 参数**：显式指定 plan_file 路径，绕过启发式
  3. **启发式抓 plan 时做任务描述 vs plan 标题的关键词低匹配度警告**：当任务描述跟 plan 文件名 / 第一行标题完全无共同 keyword 时 stderr 打 WARNING 「⚠️ 启发式抓到的 plan 与任务描述匹配度低，建议 `--no-plan` 或 `--plan=<correct_path>` 显式指定」
  4. **builder.md「读方案文件时按角色视图过滤」段补一条**：若 builder 发现 plan_file 与任务描述无关，应在 state.plan_file 字段写空字符串自我修正（不能默默用错的 plan 跑 tester）
- 优先级：中（每次 setup 时如果 .claude/plans/ 里有不相关的最新文件就会踩；BOT 这类老项目 plans/ 累计了多个历史 plan，命中概率高）

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

## 2026-06-04 worktree 模式下 CC session cwd 留在主仓 → stop hook 错位 + 第 2 轮 no_progress 早停

- 触发上下文：Personal_Assistant_Bot 这次 session-key refactor 启 builder-loop worktree 后，setup 脚本明明在 stderr 警告「CC session cwd 仍在主仓 /...Personal_Assistant_Bot...stop hook 触发时不能直接定位本 worktree state」，但提示要求用户/builder 主动 `cd <worktree>`，builder 实际只在 Bash 里 cd，**CC session 自身的 cwd 没变**。结果：第 1 轮 PASS_CMD 失败注入回来 builder 修复，第 2 轮 stop hook 触发时检测 `last_iter_head == HEAD == 24dcbf6`（builder 在 worktree 改了文件但没 commit，主仓 HEAD 不动），判定为「无进展」直接归档 state 到 `legacy/20260603-220049-early_stop_no_progress.bak`。后续整个 reviewer / commit / merge 全靠手动补，loop 流程白搭一半
- 建议方向：
  1. **no_progress 早停判据放宽**：当前看 `last_iter_head == HEAD`，但 worktree 模式下"是否有进展"应该看 worktree 内有没有 dirty 改动（`git -C <worktree_path> status --porcelain` 非空）而不是 HEAD。判据改成 `HEAD 未变 AND worktree 全 clean` 才算无进展
  2. **setup 完直接打印一个 `cd <worktree>` 提示 + 自动写入 .claude/builder-loop.local.md 让 stop hook 优先用 state 文件里的 worktree_path 而不是 cwd**（实际上 state 已经有 worktree_path 字段；stop hook 是不是没用？）
  3. **stop hook 行为兜底**：能从 builder-loop.local.md 或 state/*.yml 找出唯一 active worktree 时自动绑定（V2.4 策略 5 据说已实现，但本次没生效——值得排查 hook 是不是认了 owner_cwd 没去查 state）
- 优先级：高（worktree 模式属于推荐路径，no_progress 早停直接让 loop 不可用）

## 2026-06-03 builder 接受 reviewer 建议前没自己推导反向 case 导致反复返工

- 触发上下文：M2 batch C 跑了 3 轮 reviewer。**第 1 轮**：reviewer 建议「death_chapter 改成取最后一条死亡 delta（last-write-wins）」——理由听起来合理（与 fold_entity_state 同语义、一致性好）。builder 直接采纳改了。**第 2 轮**：reviewer 自己发现这改法在 ch3 真死 + ch4 死后 realm 变化 + ch5 重复写死亡的场景下会让 ch4 漏检（chapter>5 过滤掉了），打 🟡 让 builder 改回 first-seen。**第 3 轮**：终于改对。**根因**：builder 接到 reviewer 建议时把它当「直接采纳的指令」，没自己推导「这建议在哪些反向 case 下会失效？」就改了。reviewer 自己也是事后才发现 — 说明双方都没在第 1 轮就跑完所有 case 的脑内推演。
- 建议方向：
  1. **builder.md 步骤 3a 加 boilerplate**：「接到 reviewer 建议时，先在心里跑至少 3 个反向 case（建议提的现象 + 现象的对立面 + 边界值），确认每个 case 在新写法下结论都对，再动手改」
  2. **reviewer 报告模板加自检栏**：reviewer 给出修改建议时，自己先列「我推荐这个修法因为它在场景 A/B/C 下都对」。这把推演责任前置给 reviewer，builder 接到的就是已过推演的建议
  3. 极端方案：reviewer 给的建议 + builder 实施前先 spawn 一轮独立 judger 跑反向 case 推演——但这会让 loop 每轮多 1-2 分钟，性价比不高，先用 1+2 软规则
- 优先级：高（M2 batch C 多跑 2 轮 reviewer = 多耗 10+ 分钟 + 2 次 stop hook + 用户分心；同类模式下次还会再来）

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
- 优先级：高（每次 worktree 模式都会踩）

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
- **优先级**：中（不破坏功能但会导致 stop hook 误绑 + git status 噪音；手动清理可绕过）

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
