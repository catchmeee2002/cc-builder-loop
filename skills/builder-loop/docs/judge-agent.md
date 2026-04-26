# Judge Agent (V1.9+ / V2.1 升级)

LLM 语义判据，用于补 PASS_CMD 二值判据看不见的盲区（假完成 / 求助 / 偷懒 / 网络中断）。

## V2.1 升级速览

V2.1 重点解决两件事：
1. **正版 Max CC 用户也能用 judge**：`run-judge-agent.sh` 顶部加 env file 自动加载（仅主 env 缺失时 source `~/.claude/skills/builder-loop/judge-env.sh`），主会话保持 OAuth 干净，judge 独立从文件读 copilot-proxy 凭证
2. **优先 sonnet + 失败软着陆 haiku**：默认 `primary_model=claude-sonnet-4-6`（copilot-proxy 唯一可用 sonnet），连续 2 次失败（timeout/5xx/parse_error，不含 401/429）后自动切 `fallback_model=claude-haiku-4-5` 并立即 retry；fallback 也失败回 PASS_CMD 二值。状态在 state.judge_active_model + judge_consecutive_failures 字段，loop PASS 自动重置

V1.9 配置完全兼容（`model:` 字段自动等价 primary_model）。配置示例见 `skills/builder-loop/judge-env.sh.example`。

## 1. 整体架构

```
                  ┌─────────────────────────────────────────┐
                  │  builder-loop-stop.sh (主入口)          │
                  └────────────────┬────────────────────────┘
                                   │
                       ┌───────────▼────────────┐
                       │  PASS_CMD 二值判据      │
                       │  (run-pass-cmd.sh)      │
                       └───────┬────────────┬───┘
                       PASS    │            │  FAIL
                               ▼            ▼
                    ┌──────────────────┐  ┌───────────────────────┐
                    │ run-judge-agent  │  │ run-judge-agent       │
                    │ (PASS 主导)      │  │ (FAIL 仅识 retry)     │
                    └─────┬────────────┘  └─────────┬─────────────┘
                          │                         │
                          ▼                         ▼
              ┌──────────────────────────────────────────────┐
              │ 状态机路由（4 action × 2 PASS/FAIL）          │
              │ + 任何降级 → 退回原行为                       │
              └──────────────┬───────────────────────────────┘
                             ▼
                  写 telemetry.jsonl → exit 0/2
```

## 2. 决策状态机

### PASS_CMD = PASS

| judge.action | downgraded? | 行为 |
|--------------|-------------|------|
| `stop_done` | false | exit 2 + 原 PASS 文案 + reviewer-params（merge worktree） |
| `continue_nudge` | false + 未达 nudge 上限 | exit 2 + nudge 文案，state.iter++，consecutive_nudge_count++ |
| `continue_nudge` | false + 已达 nudge 上限 | 强制走 stop_done（防脱缰） |
| `retry_transient` | false | PASS 时视为 stop_done（retry 在 PASS 后无意义） |
| `*` | true | 走原 PASS 路径（与 V1.8 行为完全一致） |

### PASS_CMD = FAIL

| judge.action | downgraded? | 行为 |
|--------------|-------------|------|
| `retry_transient` | false | exit 2 + retry 文案，state.iter++（不更新 last_error_hash） |
| `*` | true 或 其他 action | 走原 FAIL 路径（extract-error + early-stop + 喂错误） |

## 3. 凭证检测（双路径兼容）

```
检测优先级：env > oauth > none
  ├─ ANTHROPIC_API_KEY 已设 → "env"（Copilot CC 方案 / 自定义代理）
  ├─ ~/.claude.json 有 oauthAccount.accessToken → "oauth"（正版 Max CC 方案）
  └─ 都没有 → "none"（触发 missing_credentials 降级）
```

- env 路径：`x-api-key: $ANTHROPIC_API_KEY`，URL = `${ANTHROPIC_BASE_URL:-https://api.anthropic.com}/v1/messages`
- oauth 路径：`Authorization: Bearer <token>` + `anthropic-beta: oauth-2025-04-20`

> **为什么 env 优先于 oauth**：Copilot CC 方案会同时存在 ~/.claude.json，但需要走 `ANTHROPIC_BASE_URL` 指向 copilot-proxy（绕过 anthropic.com 鉴权失败）。env 优先才能命中 copilot 链路。

> ⚠️ **正版 Max CC 用户特别说明**（2026-04-26 实测发现）：CC 自己的 OAuth access token 不在 `~/.claude.json` 的 `oauthAccount` 字段公开（该字段只含 metadata），oauth 路径在当前 CC 架构下**永远返回 none**。如果你用 Max 订阅，judge agent 会自动降级回 V1.8 二值判据（无功能损失）。
>
> **Workaround**：从 https://console.anthropic.com 申请独立 API key（不影响 Max 订阅），`export ANTHROPIC_API_KEY=sk-ant-...` 后 judge agent 自动启用。详见 `known-risks.md` R5。

## 4. 模型选择三层 fallback

```
loop.yml.judge.model               (用户最显式声明，最高优先级)
$ANTHROPIC_DEFAULT_HAIKU_MODEL     (env 默认；Copilot 方案会设)
"claude-haiku-4-5"                 (硬编码兜底)
```

**命名规范化**：dot 写法（`claude-haiku-4.5`）自动转 dash（`claude-haiku-4-5`）。

## 5. 降级矩阵

任何故障路径都通过 `downgraded=true` 表达，**不阻断 PASS_CMD 流程**：

| downgrade_reason | 触发条件 |
|------------------|----------|
| `disabled` | loop.yml.judge.enabled=false |
| `missing_credentials` | env 和 oauth 都无效 |
| `missing_args` | 必填参数缺失 |
| `missing_prompt` | system prompt 文件不存在 |
| `timeout` | curl 超过 api_timeout_sec（默认 8s） |
| `http_<code>` | API 返回非 200 |
| `no_oauth_token` | oauth 路径但读 token 失败 |
| `parse_error` | API 返回非合法 JSON / action 不在白名单 |
| `low_confidence` | confidence < confidence_threshold（默认 0.5） |

降级时输出的 action：
- PASS_CMD=PASS → `stop_done`（走原 PASS 路径）
- PASS_CMD=FAIL → `continue_strict`（走原 FAIL 路径）

## 6. 防 LLM 判据脱缰

| 防护机制 | 默认值 | 配置项 |
|----------|--------|--------|
| iter 上限硬闸 | 5 | `max_iterations`（已有，不变） |
| 连续 nudge 上限 | 2 | `judge.max_consecutive_nudges` |
| 置信度阈值 | 0.5 | `judge.confidence_threshold` |
| API 超时 | 8s | `judge.api_timeout_sec` |
| 总开关 | true | `judge.enabled` |

`consecutive_nudge_count` 在 state.yml 中持久化；stop_done 时通过 cleanup（rm state）自然清零；FAIL 路径不动。

## 7. 注入文案（与用户输入可区分）

所有 judge 触发的 stderr 注入都带统一前缀：

```
[builder-loop judge | iter=X/Y | judge=ACTION | conf=Z]
原因：<reason>
<具体提示>

(PASS_CMD 状态：通过|失败)
本消息来自 builder-loop 自动判定 agent，非用户输入。如果你认为判定错误，请在回复中说明理由继续操作。
```

末尾"非用户输入"声明给 builder 一个反驳通气口（缓解 reward hacking）。

## 8. Telemetry（`.claude/builder-loop/judge-trace.jsonl`）

每次 judge 调用写一行 JSON：

```json
{
  "ts": "2026-04-26T14:30:00Z",
  "slug": "feat-x",
  "iter": 3,
  "input": {
    "pass_cmd_status": "PASS",
    "pass_cmd_stage": "",
    "diff_stat_summary": "0 files",
    "last_assistant_snippet": "...(前 200 字)",
    "last_user_snippet": "...(前 100 字)"
  },
  "judge": {
    "action": "continue_nudge",
    "confidence": 0.87,
    "reason": "diff is empty",
    "model_used": "claude-haiku-4-5",
    "credential_path": "env",
    "elapsed_ms": 4523
  },
  "downgraded": false,
  "downgrade_reason": "",
  "consecutive_nudge_count_after": 1,
  "outcome": null
}
```

`outcome` 由下一轮 stop hook 自动后置补标（仅 continue_nudge 类）：
- 上轮 nudge + 本轮 diff 非空 → `nudge_was_correct`
- 上轮 nudge + 本轮 diff 仍空 → `nudge_likely_false_positive`

`stop_done` / `retry_transient` 类需要更复杂判据，留给人工标或 v3 高级仲裁进程。

## 9. self-check 子命令

```bash
bash skills/builder-loop/scripts/run-judge-agent.sh --self-check
```

输出当前凭证状态、模型选择、loop.yml 路径，**不调真实 API**。用于安装后或排查时快速验证 judge agent 配置可用。

预期输出（env 路径示例）：
```
[judge self-check]
  credentials:    env
    ANTHROPIC_API_KEY: sk-66666666... (len=32)
    ANTHROPIC_BASE_URL: http://localhost:4142
  env haiku model: claude-haiku-4.5
  resolved model:  claude-haiku-4-5
OK
```

## 10. 排查手册

### 10.1 judge 不调用（telemetry 没新行）

可能原因：
1. **scripts 软链未更新**：检查 `~/.claude/skills/builder-loop/scripts/run-judge-agent.sh` 是否存在
2. **stop hook 旧版**：检查 `~/.claude/scripts/builder-loop-stop.sh` 是否含 V1.9 改动（grep `run-judge-agent.sh`）
3. **PASS_CMD 失败**：FAIL 路径只在 retry_transient 时才会留下 telemetry，其他 FAIL 不调 judge

### 10.2 全部判定都被降级

```bash
# 看降级原因分布
cat .claude/builder-loop/judge-trace.jsonl | python3 -c "
import json, sys
from collections import Counter
c = Counter()
for line in sys.stdin:
    try:
        obj = json.loads(line)
        if obj.get('downgraded'):
            c[obj.get('downgrade_reason', '?')] += 1
    except: pass
print(c.most_common(10))
"
```

常见原因：
- `missing_credentials` → 检查 `--self-check` 输出
- `timeout` → 检查 ANTHROPIC_BASE_URL 网络可达
- `http_401` / `http_403` → token 失效或 endpoint 不接受当前凭证类型
- `parse_error` → 模型可能返回 markdown 包裹 JSON 或拒答；查 model_used 字段考虑换模型

### 10.3 nudge 太多（builder 被反复打扰）

调高阈值：
```yaml
judge:
  confidence_threshold: 0.7    # 默认 0.5
  max_consecutive_nudges: 1    # 默认 2
```

或关掉 judge：
```yaml
judge:
  enabled: false
```

### 10.4 完全回退到 V1.8 行为

`loop.yml.judge.enabled: false`；或卸载 `run-judge-agent.sh`（stop hook 检测到脚本缺失会自动走原路径）。

## 11. 已知风险（开口项）

详见 `skills/builder-loop/known-risks.md`：
- R1: Reward hacking（builder 学得绕过 nudge 检查）
- R2: LLM 假阳性（confidence 阈值 + 上限缓解）
- R3: 模型版本不可用（三层 fallback）
- R4: judge-trace.jsonl 无限增长
