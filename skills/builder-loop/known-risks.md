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
- 增加自动切换：4xx 时尝试用 sonnet 重试一次

---

## R5: 正版 Max CC 方案的 OAuth token 不可读

**风险描述**（2026-04-26 落地时实测发现）：CC 把 OAuth access token 存在系统级位置（推测 keyring / DBus secret service），**不写到 `~/.claude.json` 的 `oauthAccount` 字段**。该字段只含 metadata（emailAddress / accountUuid 等）。这意味着原方案"oauth 路径凭证检测"在当前 CC 架构下**永远返回 none**，judge agent 在正版 Max CC 方案上无法直接工作。

**当前状态**：
- run-judge-agent.sh 的 oauth 路径检测代码保留（如果 CC 未来在 oauthAccount 加 accessToken 字段，自动启用）
- 实际行为：正版 Max CC 用户跑 judge → missing_credentials 降级 → 行为等价 V1.8（无功能损失，只是没拿到 judge 的盲区识别）

**Workaround**：用户可以从 https://console.anthropic.com 申请独立 API key（不影响 Max 订阅，只会少量计费走 console），导出 `ANTHROPIC_API_KEY=sk-ant-...`，judge agent 自动走 env 路径生效。

**长期解法**（需要 CC 主动开放）：
- CC 开放 `~/.claude/credentials/access_token` 文件接口（非 keyring）
- CC 提供 `claude internal-token` CLI 命令导出当前 OAuth token
- 或方案 v3 的独立仲裁进程通过 CC 的 IPC 复用同一对话上下文

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
