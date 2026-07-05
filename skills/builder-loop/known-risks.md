# Builder-Loop Known Risks（开口项 / 待研究）

记录已识别但本期不解决的风险。每条说明：当前缓解措施 + 监测手段 + 后续可能的彻底解法。

---

## R1: Judge agent 被 builder 学得绕过（reward hacking）

**风险描述**：judge 用 LLM 判 "diff 是否为空 / 是否含求助关键词" 等启发式信号时，builder 可能学到捷径——比如"只要 diff 非空就行"或"避开求助关键词"，从而绕过 nudge 检查，反而隐藏真问题。

**当前缓解**：
- 注入文案带 `[builder-loop judge | ...]` 身份标识 + 末尾"本消息来自 builder-loop 自动判定 agent，非用户输入" → builder 至少有机会反驳，不会盲目接受
- 连续 nudge 上限（默认 2）→ 即使 nudge 被绕过，最多消耗 2 个 iter
- iter 上限硬闸不变 → 永远是最终防护

**监测**：
- `judge-trace.jsonl` 记录每次 nudge 后的 `outcome` 字段（自动规则：`nudge_was_correct` / `nudge_likely_false_positive`）
- 假阳性率持续 > 30% 时人工审计

**后续可能解法**：
- 定期变更 judge prompt（让 builder 难学规律）
- 引入对抗样本测试集
- v3 高级仲裁进程接入历史 transcript 做更复杂判据

**V2.3 已扩展防御**：reward hacking 检测扩展到 PASS_CMD 配置 diff（`loop.yml` / `pyproject.toml` / `pytest.ini` / `setup.cfg` / `conftest.py` / `tests*/...py` 命中 `--reruns` / `@pytest.mark.flaky` / `xfail` / `skip` / `-k "not X"` 关键词）→ 强制 `action=continue_nudge` + stop hook 注入三选项 stderr 让 builder AskUserQuestion 二次确认（quarantine / 修测试根因 / 保留 cmd）。Layer 1（LLM）+ Layer 2（本地正则兜底）双层判据。

**V2.3 后仍存在的盲区**：白名单关键词不全（如 `--ignore-glob='tests/flaky/*'` 等"看似无害的过滤"绕过命中）→ 持续观察假阴性率，下迭代加白名单文件路径机制 / 扩关键词清单。

---

## R2: Judge LLM 假阳性（误判已完成为未完成）

**风险描述**：LLM 判据本质是概率，可能把"用户确实满意的完成"误判为 continue_nudge，浪费 iter / 干扰 builder。

**当前缓解**：
- `confidence_threshold`（默认 0.5）→ 半信半疑的判定直接降级回 PASS_CMD 二值判据
- 连续 nudge 上限（默认 2）→ 误判最多消耗 2 个 iter
- 用户在 transcript 看到 `[builder-loop judge | ...]` 前缀可以人工干预

**监测**：
- `judge-trace.jsonl` 的 `outcome=stop_was_false_positive`（手工标，规则只能近似自动标）
- 用户反馈渠道：known-risks 里记录已知误判 case

**后续可能解法**：
- 模型升级（haiku → sonnet）
- prompt 工程迭代
- 人工评测集

---

## R3: 模型版本不可用 / 已下线

**风险描述**：默认硬编码 `claude-haiku-4-5`，但模型可能下线（4-7 暂停 / 4-5 已 EOL）；env 配置的 `ANTHROPIC_DEFAULT_HAIKU_MODEL` 可能被用户改成不存在的版本。

**当前缓解**：
- 三层 fallback：`loop.yml.judge.model` > `$ANTHROPIC_DEFAULT_HAIKU_MODEL` > `claude-haiku-4-5`
- API 4xx → 视为降级（telemetry 记 `http_4xx`）
- self-check 子命令可主动验证模型可用性（不调真实生成 API，仅 ping endpoint）

**监测**：
- `judge-trace.jsonl` 的 `downgrade_reason=http_4xx` 频次

**后续可能解法**：
- 维护一个"已知可用模型"白名单，硬编码兜底跟着 Anthropic 发布节奏滚动更新

---

## R6: V2.3 dirty stash 流程边界 case（reviewer V2.3 提出）

**R6.1**：`git status --porcelain` 的 `awk '{print $NF}'` 对**重命名文件**（`R old -> new` 格式）只取 `new` 路径，`old` 路径丢失 → `pre_loop_dirty_files` 列文件清单不全；merge auto-commit body 少行（语义上不影响 stash 内容，仅记录欠完整）。

**R6.2**：EARLY_STOP 还原路径 `git stash apply <hash>` 失败（用户主仓被中途修改 / 冲突）→ 当前仅 warn `STASH_RESTORED=conflict`，stash 副本不 drop。用户事后需手动处理。fixture 未覆盖此 case（不易构造）。

**R6.3**：worktree add 失败时主仓 stash 已写入 → 已实现 `git stash apply <hash>` 还原回滚，但**未有 fixture 覆盖**（需精心构造 worktree add 失败但不影响 stash 的场景）。

**R6.4**：`merge-worktree-back.sh` 路径 B（rebase 后 ff 失败 / `ERROR ff-after-rebase-failed`）+ 路径 C（NEED_ARBITRATION）下，主仓 stash 副本不会自动 drop（V2.3 已加 `warn_stash_residual` stderr 提示）。用户需见到提示后手动 `git stash drop`。fixture 未覆盖。

**R6.5**：`reward_hacking_detection` Layer 2 关键词清单不全——`--ignore-glob` / `--collect-only` / `pytest.mark.skipif` 等"看似无害的过滤"未在清单。当前依赖 Layer 1 LLM 判据兜底；持续观察假阴性率，下迭代加白名单文件路径机制 / 扩关键词清单。

**R6.6**：grep 实现差异——CC 环境注入 ugrep wrapper（function 级覆盖），ugrep 不解析 `\NNN` ASCII escape；GNU grep 解析。V2.3.1 已把 reward hacking pattern 从 `\047` 改为字面字符类 `['"'"'"]` 跨实现兼容。**留一个潜在 risk**：用户环境若有别的 grep 实现（如 BSD grep on macOS）可能仍有边界。fixture 已用同 pattern 验证。
- 增加自动切换：4xx 时尝试用 sonnet 重试一次

---

## R4: judge-trace.jsonl 无限增长

**风险描述**：每次 stop hook 触发都写一行 jsonl，单个项目可能积累到 100MB+，影响 git status / IDE 性能。

**当前缓解**：
- 文件路径 `.claude/builder-loop/judge-trace.jsonl`，建议项目 .gitignore（同 loop-trace.jsonl 一起）
- 单文件不轮转

**监测**：
- 文件大小超过 10MB 时考虑分片

**后续可能解法**：
- 按月分片：`judge-trace-2026-04.jsonl`
- 引入轮转脚本（手工调用）

---

## R7: 与 CC 官方 `/loop` skill 叠用撞车

**风险描述**：CC v2.1.121 起 `/loop` 含 dynamic 自适应步频模式（`ScheduleWakeup` 工具）。用户若在已激活 cc-builder-loop 的项目上叠用 `/loop` 监督 builder，wake-up 时机不感知 builder-loop state → 可能撞 worktree 跑到一半 / PASS_CMD 中途 / state 锁未释放。

**当前缓解**（用户层）：CLAUDE.md 顶部 + `docs/cc-loop-tracking.md` §3 明示禁忌。

**监测**：暂无自动监测（v3 计划探测 wake-up context 主动 skip bootstrap）。

**详见**：`skills/builder-loop/docs/cc-loop-tracking.md` — 含官方版本快照表 + 借鉴清单 + 持续超越方向 + 复查节奏。

---

## R5: 正版 Max CC 方案的 OAuth token 不可读

**风险描述**（2026-04-26 落地时实测发现）：CC 把 OAuth access token 存在系统级位置（推测 keyring / DBus secret service），**不写到 `~/.claude.json` 的 `oauthAccount` 字段**。该字段只含 metadata（emailAddress / accountUuid 等）。这意味着原方案"oauth 路径凭证检测"在当前 CC 架构下**永远返回 none**，judge agent 在正版 Max CC 方案上无法直接工作。

**当前状态**：
- run-judge-agent.sh 的 oauth 路径检测代码保留（如果 CC 未来在 oauthAccount 加 accessToken 字段，自动启用）
- V2.1 起加了 env file 自动加载（见下方 V2.1 Workaround），主 OAuth 用户也能用 judge

**V2.1 Workaround**（推荐，2026-04-26 落地）：写 `~/.claude/skills/builder-loop/judge-env.sh`（不进 git）：

```bash
# 方案 A：copilot-proxy 链路（已有 proxy 用户首选）
export ANTHROPIC_API_KEY=sk-666
export ANTHROPIC_BASE_URL=http://localhost:4142

# 方案 B：独立 sk-ant-key（无 proxy 用户）
# export ANTHROPIC_API_KEY=sk-ant-...
```

`run-judge-agent.sh` 启动时自动 source（仅主 env 未设时；已设主 env 的 Copilot 用户行为不变）。详见 `skills/builder-loop/judge-env.sh.example` 模板与 `CLAUDE.md` 7.3 排查手册。

**V1.9 老 Workaround**（V2.1 起改 env file 路径更清洁）：用户可以从 https://console.anthropic.com 申请独立 API key（不影响 Max 订阅，只会少量计费走 console），主进程 `export ANTHROPIC_API_KEY=sk-ant-...` 即可。**缺点**：会污染主 CC env 让其也走 sk-ant-key 而非 OAuth；V2.1 的 env file 方案没有这个副作用。

**长期解法**（需要 CC 主动开放）：
- CC 开放 `~/.claude/credentials/access_token` 文件接口（非 keyring）
- CC 提供 `claude internal-token` CLI 命令导出当前 OAuth token
- 或方案 v3 的独立仲裁进程通过 CC 的 IPC 复用同一对话上下文

---

## R8: E2E llm_judge 对视觉质量判断力不足（2026-07-05 divine-word 事件）

**事件描述**：divine-word 项目两次"前端视觉大迭代"（commit `3143d50`、`7067801`），planner 选了"需要 e2e 测试"，plan 文件写入 `<!-- e2e-cases -->` 含 8 条用例（如 `farm-military-visible: llm_judge "地图上是否可见农田tile和军营/旗帜sprite？"`）。builder-loop 信号链设计上完整。但最终产出：fog-of-war 巨大黑洞覆盖 60%+ 屏幕、所有建筑是 `fillRect` 纯色矩形而非 sprite。视觉效果是 programmer-art 级别，用户端到端 playtest 才发现。

**根因（两层）**：

1. **llm_judge 确认偏差**：tester agent 截图后调 LLM 问"地图上是否可见农田tile"，LLM 看到绿色 `fillRect` 方块→判"可见，有绿色区域代表农田"→ PASS。llm_judge **分不清 fillRect 占位实现和真正的 sprite**，因为 prompt 里没有 negative example（"如果看到大片均匀颜色矩形 = FAIL"）或精度要求（"必须是离散 16x16 像素 tile"）。同一个 LLM 又当 builder（写 fillRect）又当 judge（看 fillRect 判通过），天然确认偏差。

2. **e2e 静默跳过无告警**：`handle-pass-result.sh` line 52 `if [ -n "$E2E_PLAN_PATH" ]`——plan_path 为空时整个 e2e 块被跳过，直接走后续 commit 流程，**不输出任何 warning**。无法回溯 e2e 是否真正执行（state 已清理）。如果 plan_path 未正确注册（手动 setup、loop 已 active 跳过重注册等场景），e2e 验证被整体静默吞没。

**已修复（V5.7）**：
- `llm_judge` → `judge: {verify, quality}` 双轨判定。quality 用评价式 prompt 对抗数据源模式
- handle-pass-result.sh E2E_PLAN_PATH 空/plan 不存在 → stderr warning + JSON `e2e_skipped`
- tester 审计落盘：`.claude/e2e-audit/{timestamp}.yaml`
- planner Round 7 加 quality 写法规范 + 自检

**残余风险**：LLM 评价式 prompt 是否真能对抗数据源模式，需实际项目验证。待观察期结束后评估效果。

---

## R9: tester e2e 视觉验收的感知模式缺陷（2026-07-05 divine-word 事件，R8 追加）

**事实（续 R8 同一事件，深一层分析）**：

1. **tester 当前感知模式**：tester e2e 验收截图时，工作方式是"数据源模式"——从截图中提取目标物是否存在（"有绿色区域→农田可见→PASS"）。它没有进入"视觉作品模式"——评整体画面质量、第一印象、面积占比、是否像一个成品游戏。

2. **同一缺陷在独立 agent 身上复现**：同一个项目中，一个完全没看过代码的 agent 被要求端到端玩游戏并评估，看了12张截图，每张都有占屏幕60%的黑洞，没有报出来。该 agent 事后自述根因："把截图当数据源读（提取文字和数值），不是当视觉作品评"。这说明问题不是 tester 特有的，是 LLM agent 处理截图时的通用感知模式。

3. **确认式提问强化数据源模式**：当前 llm_judge 的提问方式是 `"地图上是否可见农田tile？"`——这是确认式的，引导 agent 找目标物并确认存在。agent 在这种提问下不会主动评估"画面整体是否合格"。

4. **上下文泄漏强化确认偏差**：tester 收到 e2e_cases 时，同时收到 plan 文件内容和实现描述。它知道 builder 刚实现了 fog-of-war 和农田渲染，所以在截图中"找"这些实现的证据，而不是"评"截图的视觉质量。找到绿色区域→确认存在→PASS。如果 tester 不知道实现了什么、只拿到截图和设计文档的视觉目标，它更可能报出"60%是黑的"和"建筑是纯色方块"。
