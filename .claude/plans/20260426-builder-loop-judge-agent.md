# Builder-Loop Judge Agent — 用 LLM 语义判据补 PASS_CMD 二值判据的盲区

<!-- role:shared -->

## 背景 & 目标

### 现状盲区

当前 stop hook 的核心判据是 PASS_CMD（lint/test）二值通过 + iter 上限 + EARLY_STOP 信号。这套机器判据看不见以下场景：

| 盲区 | PASS_CMD 视角 | 实际状态 |
|------|--------------|----------|
| Builder 声称完成但未真改文件 | 测试还过 → PASS ✅ | 假完成 |
| Builder 回复"请告知 X 后我再继续" | 测试还过 → PASS ✅ | 卡住等用户 |
| Builder 给方案没动手 | 测试还过 → PASS ✅ | 偷懒 |
| API 抖动 / 回复中断 | PASS_CMD 跑不到 | 应该 retry |
| 编译就挂、PASS_CMD 自己 crash | 不可判 | 应该 retry/escalate |

### 目标

引入"判定 agent"（judge agent），在 stop hook 既有判据栈之上叠加一道**语义判定**，识别上述盲区并产出 3 态结果（continue_nudge / stop_done / retry_transient）路由 stop hook 出口。

**前置硬约束**：
- 判定 agent 任何故障路径（API 超时 / 非 200 / JSON 解析失败 / confidence < 0.5）→ **无差别降级回 PASS_CMD 二值判据**，绝不让 judge 成为新故障源
- iter 上限不变，永远是最终硬闸
- 所有判定调用 + 输入摘要 + 输出 + 是否降级，全部落 telemetry（用户消化担忧的关键）

### 成功标准

1. 判定 agent 在 **正版 CC（OAuth）+ Copilot CC（env BASE_URL+API_KEY）** 双路径下都能正常工作
2. 判定 LLM 模型号支持 loop.yml 配置 + env fallback + 默认值三层覆盖（haiku 4.5/4.6/4.7 都能切）
3. 任何降级路径都不阻断既有 PASS_CMD 流程（行为兼容现状）
4. telemetry 覆盖率 100%（每次 judge 调用必写一行 jsonl）
5. e2e 测试覆盖：双路径凭证 / 6 个降级场景 / 3 个状态路由 / telemetry 落盘

---

## 预估改动级别

**L3（新接口/模块）**：新增 `run-judge-agent.sh` 脚本 + 新 telemetry 文件 + state schema 扩展（4 字段）+ loop.yml schema 加 `judge` 段。Builder 根据实际 diff 确认或修正。

理由：虽然主要改动局限在 stop hook 内部，但引入了**新外部 API 依赖（Anthropic API）**和**新状态字段**，对 builder-loop 接入项目而言是新接口。

---

## 约束 & 边界

### 不能碰

1. **PASS_CMD 失败路径的现有行为**（V1.7 错误分类、extract-error.sh 反馈、early-stop-check）—— judge 只在 PASS_CMD 通过后叠加判定，FAIL 分支只在"识别 retry_transient"这一个点小幅介入，且降级路径回到原行为
2. **iter 上限语义** —— max_iterations 永远是最终硬闸，judge 不能续接突破
3. **现有 4 个 hook 注册**（Stop / SubagentStart / SubagentStop / 2 个 PreToolUse）—— 不增不减
4. **arbiter agent**（git rebase 冲突仲裁）—— 与本方案的 judge 完全无关，命名严格区分
5. **既有 state.yml 字段** —— 只追加，不改名/不改语义
6. **commit-msg hook 兼容**（V1.8.3 已加 `[cr_id_skip]`）—— 不影响

### 必须兼容

1. **正版 CC + Copilot CC 双路径凭证**（参考 `/mnt/hongyu.liao_docker/CC/CLAUDE.md`）
2. **模型号变化**（haiku 4.5 → 4.6 → 4.7）—— 三层配置 fallback
3. **未配置 judge 段的项目**（loop.yml 没写 `judge:`）—— 默认行为 = 现状（不调 judge）或 enabled 默认 true 但凭证缺失自动降级（**最终决策见技术选型**）
4. **state.yml 旧版本**（缺 last_judge / consecutive_nudge_count 字段）—— 缺字段视为初始值
5. **flock 互斥** + **HEAD 游标** + **僵尸 state 自愈** 等 V1.8.x 既有机制

### 保留为开口项（不在本方案解决）

- **Reward hacking**（builder 学得绕过 judge 检查）—— 仅做缓解（身份标识 + 连续 nudge 上限），实证留给后续观测
- **judge prompt 质量回归**（如何评估"假完成识别准确率"）—— 等 telemetry 攒够样本后再做评估集
- **v3 独立仲裁进程**（高级 daemon）—— 本方案积累的 judge-trace.jsonl 是其训练 / 评测数据来源

---

## 验收标准

### 功能验收

1. `bash skills/builder-loop/scripts/run-judge-agent.sh --self-check` 在两套配置下都返回 `OK`：
   - 正版 CC：`~/.claude.json` 存在 OAuth credentials → `OK (path=oauth, model=claude-haiku-4-5)`
   - Copilot CC：env 设 `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY` → `OK (path=env, model=<env>)`
2. `cat .claude/builder-loop/judge-trace.jsonl | wc -l` 与 stop hook 触发次数一致（每次必有一行）
3. e2e 测试 `test-judge-agent.sh` + `test-judge-integration.sh` 全部通过
4. 安装后 `bash uninstall.sh && bash install.sh` 幂等，不会重复注册任何 hook

### 行为验收（手工 / e2e）

| 场景 | 预期行为 |
|------|----------|
| PASS_CMD 通过 + diff 非空 + builder 正常完成 | judge → stop_done → 走原 PASS 路径（reviewer + cleanup） |
| PASS_CMD 通过 + diff 为空 + builder 声称完成 | judge → continue_nudge → exit 2 + nudge 文案，state.consecutive_nudge_count++ |
| PASS_CMD 通过 + builder 求助语 ("请告诉我 X") | judge → stop_done（builder 已自然停下，judge 同意） |
| PASS_CMD 通过 + judge API 超时 | 降级 → 走原 PASS 路径，telemetry 标记 `downgraded=true reason=timeout` |
| PASS_CMD 通过 + judge 返回 confidence=0.3 | 降级 → 走原 PASS 路径，telemetry 标记 `downgraded=true reason=low_confidence` |
| 连续 nudge 已 = 2 次 + judge 又返回 nudge | 强制 stop_done（防脱缰） |
| PASS_CMD 失败 + judge 识别为 retry_transient | exit 2 + retry 文案（不喂错误日志） |
| 凭证两路径都失效（env 缺 + ~/.claude.json 缺） | 降级 → 走原 PASS_CMD 路径，telemetry 标记 `downgraded=true reason=missing_credentials` |

### 文档验收

- [ ] `CLAUDE.md` 加 V1.9 已交付能力 + 已知问题排查手册
- [ ] `skills/builder-loop/README.md` 加 V1.9 版本条目
- [ ] `skills/builder-loop/SKILL.md` 加「## 判定 agent（V1.9+）」章节
- [ ] `skills/builder-loop/docs/judge-agent.md` 完整架构文档（同 arbiter-flow.md 同级）
- [ ] `skills/builder-loop/known-risks.md` 列出 R1（reward hacking）/ R2（LLM 假阳性）/ R3（模型版本不可用）

---

## judge agent 对外契约（接口签名）

### 调用形式

```bash
bash skills/builder-loop/scripts/run-judge-agent.sh \
    --state-file <path> \
    --project-root <path> \
    --transcript-path <path> \
    --pass-cmd-status <PASS|FAIL> \
    [--pass-cmd-stage <name>] \
    [--pass-cmd-log <path>]
```

### 输出

stdout 一行 JSON（始终输出，包括降级场景）：

```json
{
  "action": "continue_nudge | stop_done | retry_transient",
  "confidence": 0.0,
  "reason": "<one-line>",
  "downgraded": false,
  "downgrade_reason": "",
  "model_used": "claude-haiku-4-5",
  "credential_path": "env | oauth | none",
  "elapsed_ms": 0
}
```

stderr：人类可读 debug 信息（仅当 verbose）。

exit code：始终 0（脚本本身不失败 / 失败也通过 downgraded 字段表达）。

### 降级矩阵

| 原因 | downgrade_reason | downgrade 后 action |
|------|------------------|---------------------|
| API 超时（默认 8s） | `timeout` | `stop_done`（PASS）/ 沿用原 FAIL 行为 |
| API 非 200 | `http_<code>` | 同上 |
| JSON 解析失败 | `parse_error` | 同上 |
| confidence < 阈值 | `low_confidence` | 同上 |
| 凭证缺失 | `missing_credentials` | 同上 |
| consecutive_nudge_count >= max | `max_nudge_reached` | 强制 `stop_done` |
| judge 段 disabled | `disabled` | 同上 |

### Judge 决策状态机（3 态）

```
PASS_CMD=PASS:
  judge.action=stop_done       → exit 2 + 原 PASS 文案 + reviewer-params
  judge.action=continue_nudge  → exit 2 + nudge 文案 + state.iter++ + state.consecutive_nudge_count++
  judge.action=retry_transient → 视为 stop_done（PASS 时 retry 无意义）
  downgraded=true              → 视为 stop_done（PASS 时降级 = 走原 PASS 路径）

PASS_CMD=FAIL:
  judge.action=retry_transient → exit 2 + retry 文案（不喂错误日志，让 builder 重做同一任务）
  judge.action=continue_nudge  → 视为 retry_transient（FAIL 时 nudge 无意义）
  judge.action=stop_done       → 视为 retry_transient（FAIL 时 LLM 不能强制 stop）
  downgraded=true              → 走原 FAIL 路径（extract-error → early-stop → exit 2 喂错误）
```

PASS 时 LLM 是主导，FAIL 时 LLM 仅识别"是不是网络抖动"这一种特殊状态，其他全部走原路径。

<!-- /role -->

<!-- role:builder -->

## 技术选型

### 维度 1：判定 agent 跑在哪里

| 选项 | 实现 | 决策 |
|------|------|------|
| **A1. Hook 内嵌 API 调用**（采纳） | stop hook 调 `run-judge-agent.sh`，脚本内 curl Anthropic API | ★ |
| A2. Builder 自评 | 在 builder.md 加"回复前 spawn 判定 subagent" | 否 — 裁判员当运动员 |
| A3. CC 后置 subagent | Stop 写标记，下轮启动先 spawn 仲裁 | 否 — CC 没"next-turn 前置 hook"机制 |

### 维度 2：判定输入范围

| 选项 | 内容 | 决策 |
|------|------|------|
| 最小 | builder 最后一条回复 | 否 |
| **中等**（采纳） | + last user prompt + 本轮 git diff stat | ★ |
| 最大 | + PASS_CMD stdout + state 全字段 + iter 历史 | 否 — token 浪费收益小 |

### 维度 3：与 PASS_CMD 关系

| 选项 | 决策 |
|------|------|
| C1. 替代 PASS_CMD | 否 — 不靠谱 |
| **C2. PASS_CMD 后叠加**（采纳） | ★ 解决"假完成"主战场 |
| C3. PASS_CMD 失败时兜底 | 否 — 覆盖太窄 |
| C4. 并行投票 | 否 — token 翻倍 |

PASS_CMD 失败路径仅在"识别 retry_transient"一个点接入，主要是为了 retrospective T2"故障路径优先"。

### 维度 4：API 凭证读取（双路径）

```
判定脚本启动 → 凭证检测函数 detect_credentials()：

if [ -n "$ANTHROPIC_API_KEY" ] && [ -n "$ANTHROPIC_BASE_URL" ]; then
  echo "env"          # Copilot 路径（ANTHROPIC_BASE_URL=http://localhost:4142 + 占位 key）
elif [ -n "$ANTHROPIC_API_KEY" ]; then
  echo "env"          # 仅有 key，BASE_URL 默认 https://api.anthropic.com
elif [ -f "$HOME/.claude.json" ] && python3 -c "import json; j=json.load(open(...)); assert j.get('oauthAccount')"; then
  echo "oauth"        # 正版 CC OAuth 路径
else
  echo "none"         # 触发降级
fi
```

OAuth 路径调用：`Authorization: Bearer <token>`，token 从 `~/.claude.json` 的 `oauthAccount.accessToken` 字段读取（如果该字段不存在则视为 none，路径未公开，做防御性处理）。

env 路径调用：`x-api-key: $ANTHROPIC_API_KEY`，URL = `${ANTHROPIC_BASE_URL:-https://api.anthropic.com}/v1/messages`。

> **凭证检测顺序的小坑**：copilot 方案设了 `ANTHROPIC_API_KEY=sk-666...777`（占位），env 路径优先级**必须**高于 oauth 路径——否则 copilot 链路会误走 oauth + 默认 anthropic.com 域名（绕过 copilot-proxy 鉴权失败）。

### 维度 5：模型选择三层 fallback

```
loop.yml.judge.model           # 用户最显式声明，最高优先级
$ANTHROPIC_DEFAULT_HAIKU_MODEL  # env 默认（copilot 方案会设）
"claude-haiku-4-5"              # 硬编码兜底（始终是最近发布的 haiku 版本号）
```

> 模型 ID 命名约定：API 用 `-` 分隔（`claude-haiku-4-5`），CC 配置文件经常写成 `.`（`claude-haiku-4.5`）。判定脚本从 env 读到的可能是任意一种，调 API 前要做规范化（替换 `.` 为 `-`）。

## 方案设计

### 总体架构

```
                  ┌─────────────────────────────────────────┐
                  │  builder-loop-stop.sh (主入口，已存在)  │
                  └────────────────┬────────────────────────┘
                                   │
                       ┌───────────▼────────────┐
                       │  PASS_CMD 二值判据      │
                       │  (run-pass-cmd.sh，不动) │
                       └───────┬────────────┬───┘
                       PASS    │            │  FAIL
                               ▼            ▼
                    ┌──────────────────┐  ┌───────────────────────┐
                    │ run-judge-agent  │  │ extract-error +        │
                    │ (PASS 主导)      │  │ early-stop-check       │
                    └─────┬────────────┘  │ (FAIL 主导，不动)      │
                          │               └─────────┬─────────────┘
                          │                         │
                          │            ┌────────────▼──────────┐
                          │            │ run-judge-agent        │
                          │            │ (FAIL 仅识 retry_*)    │
                          │            └────────────┬──────────┘
                          │                         │
                          ▼                         ▼
              ┌──────────────────────────────────────────────┐
              │ 判定状态机（PASS/FAIL × 3 action 路由）       │
              │ + 任何降级 → 退回原行为                       │
              └────────────┬─────────────────────────────────┘
                           ▼
                   写 telemetry → exit 0 / exit 2
```

### run-judge-agent.sh 主流程

```
1. 解析参数 (state-file / transcript-path / pass-cmd-status / ...)
2. 读 loop.yml judge 段（enabled / model / confidence_threshold / max_consecutive_nudges / api_timeout_sec / system_prompt_path）
   judge.enabled=false 或不存在 judge 段 → 输出 disabled JSON 退出
3. 凭证检测 detect_credentials() → env / oauth / none
   none → 输出 missing_credentials downgrade JSON 退出
4. 模型选择 resolve_model() → loop.yml.judge.model || ANTHROPIC_DEFAULT_HAIKU_MODEL || "claude-haiku-4-5"
5. 构建 input：
   - last_assistant_text：从 transcript-path 反向扫第一条 role=assistant && type=text 的 message，
     用 createdAt 严格大于 hook 启动时刻的基准戳（防 retrospective T4 时序坑）
   - last_user_text：同样反向扫 role=user 的最近一条
   - diff_stat：git -C $PROJECT_ROOT diff --stat $start_head..HEAD（前 30 行）
   - pass_cmd_status：参数透传
   - iter / consecutive_nudge_count：从 state-file 读
6. 读 system prompt：skills/builder-loop/prompts/judge-system.md
7. 调 API：
   - timeout: 8s（loop.yml 可改）
   - max_tokens: 256
   - stream: false
   - format: 强制 JSON 输出（response_format 或在 prompt 里要求严格 JSON）
8. 解析响应：
   - 200 + 合法 JSON + action 在白名单 → 正常路径
   - 否则 → downgrade
9. confidence 检查：
   - confidence < threshold（默认 0.5）→ downgrade reason=low_confidence
10. 写 telemetry：.claude/builder-loop/judge-trace.jsonl
11. stdout 一行 JSON
```

### builder-loop-stop.sh 集成点

**PASS 分支（`scripts/builder-loop-stop.sh:254-307` 之间）**：

在 `PASS_START_HEAD_PREREAD="..."` 这行之后，**`merge-worktree-back.sh` 调用之前**插入 judge 调用：

```bash
# ---- V1.9: judge agent 调用（PASS_CMD 通过后） ----
JUDGE_RESULT="$(bash "${SKILL_DIR}/run-judge-agent.sh" \
    --state-file "$STATE_FILE" \
    --project-root "$PROJECT_ROOT" \
    --transcript-path "$TRANSCRIPT_PATH" \
    --pass-cmd-status "PASS" 2>/dev/null || echo '{"action":"stop_done","downgraded":true,"downgrade_reason":"script_error"}')"

JUDGE_ACTION="$(echo "$JUDGE_RESULT" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('action','stop_done'))" 2>/dev/null || echo "stop_done")"
JUDGE_DOWNGRADED="$(echo "$JUDGE_RESULT" | python3 -c "..." 2>/dev/null || echo "true")"

# 路由：
case "$JUDGE_ACTION" in
  continue_nudge)
    if [ "$JUDGE_DOWNGRADED" = "true" ]; then
      # 视为 stop_done，走原 PASS 路径
      :
    else
      # 检查 consecutive_nudge_count
      CUR_NUDGE="$(grep -E '^consecutive_nudge_count:' "$STATE_FILE" | head -1 | awk '{print $2}')"
      CUR_NUDGE="${CUR_NUDGE:-0}"
      MAX_NUDGE="$(read from loop.yml judge.max_consecutive_nudges, default 2)"
      if [ "$CUR_NUDGE" -ge "$MAX_NUDGE" ]; then
        # 强制 stop_done
        echo "[builder-loop judge | iter=${NEXT_ITER} | force=stop_done] consecutive_nudge_count=${CUR_NUDGE} >= max=${MAX_NUDGE}" >&2
        # fall through 到 PASS 路径
      else
        # 写回 state（iter++ + consecutive_nudge_count++ + last_judge）
        # exit 2 + nudge 文案
        cat >&2 <<NUDGE_MSG
[builder-loop judge | iter=${NEXT_ITER}/${MAX_ITER} | judge=continue_nudge | conf=${JUDGE_CONF}]
原因：${JUDGE_REASON}
请确认：是确实完成了无需更多改动，还是漏了什么？

(PASS_CMD 状态：通过)
本消息来自 builder-loop 自动判定 agent，非用户输入。如果你认为判定错误，请在回复中说明理由继续操作。
NUDGE_MSG
        exit 2
      fi
    fi
    ;;
  retry_transient)
    if [ "$JUDGE_DOWNGRADED" = "true" ]; then
      :  # 视为 stop_done
    else
      # PASS 时收到 retry 视为 stop_done（异常情况，记 telemetry）
      :
    fi
    ;;
  stop_done|*)
    :  # 走原 PASS 路径
    ;;
esac

# 走原 PASS 路径（merge-worktree-back.sh + reviewer-params + exit 2 PASS 文案）
PASS_START_HEAD_PREREAD="..."  # 已存在
...
```

**FAIL 分支（`scripts/builder-loop-stop.sh:383-440` 之间）**：

在 `early-stop-check.sh` 调用之前插入 judge：

```bash
# ---- V1.9: judge agent 仅识别 retry_transient ----
JUDGE_RESULT="$(bash "${SKILL_DIR}/run-judge-agent.sh" \
    --state-file "$STATE_FILE" \
    --project-root "$PROJECT_ROOT" \
    --transcript-path "$TRANSCRIPT_PATH" \
    --pass-cmd-status "FAIL" \
    --pass-cmd-stage "$STAGE" \
    --pass-cmd-log "$LOG_PATH" 2>/dev/null || echo '{"action":"continue_strict","downgraded":true}')"

if [ "$(extract action)" = "retry_transient" ] && [ "$(extract downgraded)" = "false" ]; then
  cat >&2 <<RETRY_MSG
[builder-loop judge | iter=${NEXT_ITER} | judge=retry_transient | conf=${JUDGE_CONF}]
原因：${JUDGE_REASON}（疑似上轮 API 中断/网络抖动）
请重新执行同一任务，不要重做已经完成的部分。

本消息来自 builder-loop 自动判定 agent，非用户输入。
RETRY_MSG
  # 不调 extract-error.sh，state.iter++ 但不更新 last_error_hash
  exit 2
fi

# 走原 FAIL 路径（早停检查 + 错误注入）...
```

### state.yml 字段扩展

新增字段（追加在 created_at 之后，旧 state 缺字段视为初始值）：

```yaml
last_judge_action: ""              # 上次 judge 的 action（首次为空）
last_judge_confidence: 0.0
last_judge_ts: ""                  # ISO 时间戳
consecutive_nudge_count: 0         # 连续 continue_nudge 计数（连续 stop_done 时清零）
```

### loop.yml schema 扩展

新增 `judge` 段（全部可选，整段不写 = 用默认值）：

```yaml
judge:
  enabled:
    type: boolean
    default: true
    description: 是否启用 judge agent。false 时所有调用直接降级。
  model:
    type: string
    default: ""  # 空字符串 = 走 env / 默认值 fallback
    description: |
      LLM 模型 ID。三层 fallback：本字段 > $ANTHROPIC_DEFAULT_HAIKU_MODEL > "claude-haiku-4-5"
      命名规范：用 dash 分隔（claude-haiku-4-5），自动规范化 dot 写法。
  confidence_threshold:
    type: float
    default: 0.5
    description: 置信度阈值。低于此值降级回 PASS_CMD 二值判据。
  max_consecutive_nudges:
    type: integer
    default: 2
    description: 连续 continue_nudge 上限，超过强制 stop_done。
  api_timeout_sec:
    type: integer
    default: 8
    description: 单次 API 调用超时秒数。超时降级。
  system_prompt_path:
    type: string
    default: ""  # 空 = 用 skills/builder-loop/prompts/judge-system.md 默认
    description: 自定义 judge system prompt 路径（相对项目根）
```

### prompts/judge-system.md 模板（核心 prompt）

```
你是 builder-loop 的判定 agent。任务：基于 builder 最后一条回复 + 用户最后一条 prompt + 本轮 git diff stat，判定下一步动作。

输入字段：
- pass_cmd_status: PASS | FAIL
- last_assistant_text: builder 最后一条回复
- last_user_text: 用户最后一条 prompt
- diff_stat: git diff --stat 输出
- iter / max_iter / consecutive_nudge_count

判据：
- PASS_CMD = PASS 时：
  - builder 声称完成 + diff 非空 + diff 触及 last_user_text 提到的目标 → stop_done (高置信)
  - builder 声称完成 + diff 为空（或仅注释/未跟踪文件） → continue_nudge
  - builder 在求助/等用户决策（关键词："请告诉我"、"need (you|user)"、"无法继续"） → stop_done（builder 已自然停下）
  - builder 给方案没动手 → continue_nudge
- PASS_CMD = FAIL 时：
  - builder 回复异常截断（明显未完整收尾） → retry_transient
  - 其他 → 输出 continue_strict 让上游走原 FAIL 路径（不会被使用，仅占位）

输出严格 JSON：
{
  "action": "continue_nudge | stop_done | retry_transient | continue_strict",
  "confidence": 0.0~1.0,
  "reason": "<一句话原因，<= 80 字>"
}

不要输出任何 JSON 之外的内容。
```

### telemetry 文件 schema（`.claude/builder-loop/judge-trace.jsonl`）

每行一个 JSON 对象：

```json
{
  "ts": "2026-04-26T14:30:00Z",
  "slug": "feat-x",
  "iter": 3,
  "input": {
    "pass_cmd_status": "PASS",
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

`outcome` 字段后置补：在下一轮 judge 调用前，由 stop hook 根据本轮发生的事自动标注上一次的 outcome（自动规则）：

| 上轮 action | 本轮观察 | outcome 自动标注 |
|-------------|----------|-----------------|
| continue_nudge | 本轮 diff 非空且 PASS | `nudge_was_correct`（builder 接受 nudge 后真的改了东西） |
| continue_nudge | 本轮仍 diff 为空 | `nudge_likely_false_positive`（builder 没听 nudge，可能 nudge 错了） |
| stop_done | 用户在下次 prompt 里说"没做完" | `stop_was_false_positive`（手工标，规则只能近似） |
| retry_transient | 本轮 PASS 或 builder 复盘提到"上次中断" | `retry_was_correct` |

后置补 outcome 的逻辑放在 stop hook 进入 judge 调用之前的初始化段，约 10 行 python3。

## 文件地图

### 存量文件（需要改动）

| 路径 | 改动点 |
|------|--------|
| `scripts/builder-loop-stop.sh` | PASS 分支约 L254-307 之间 + FAIL 分支约 L386-413 之间各插入 judge 调用 + 状态机路由；初始化段加 outcome 补标 |
| `skills/builder-loop/schema/loop.schema.yml` | 末尾追加 `judge:` 段（约 30 行 yaml） |
| `skills/builder-loop/SKILL.md` | 末尾追加「## 判定 agent（V1.9+）」章节 |
| `skills/builder-loop/README.md` | 「版本交付历史」部分追加 V1.9 条目 |
| `CLAUDE.md` | 「5. 已交付能力」追加 V1.9 + 「7. 已知问题」追加 7.3 节 |
| `agents/arbiter.md` | **不动**（与 judge 完全无关） |
| `~/.claude/commands/builder.md` | **不动**（注入文案已自带身份标识，builder 自然识别） |
| `install.sh` / `uninstall.sh` | **不动**（无新 hook） |

### 新增文件

| 路径 | 内容 |
|------|------|
| `skills/builder-loop/scripts/run-judge-agent.sh` | 核心调用脚本（约 250 行 bash + python3 内嵌） |
| `skills/builder-loop/prompts/judge-system.md` | judge system prompt 模板 |
| `skills/builder-loop/docs/judge-agent.md` | 详细架构文档（同 arbiter-flow.md 格式） |
| `skills/builder-loop/known-risks.md` | 项目级已知风险开口项（R1/R2/R3） |
| `skills/builder-loop/fixtures/e2e/test-judge-agent.sh` | judge 单元测试（mock API + 9 个 case） |
| `skills/builder-loop/fixtures/e2e/test-judge-integration.sh` | 端到端集成测试（完整 stop hook 流） |

### 不动的目录

- `skills/builder-loop/scripts/` 下其他脚本（locate-state / merge-worktree-back / extract-error 等）
- `agents/` 下 tester.md / arbiter.md
- `scripts/` 下其他 hook 脚本（tester-lock-* / reviewer-timing-check）

## 执行任务列表

按依赖顺序，每步明确"改哪个文件 / 做什么"：

### 阶段 A：基础设施（无依赖，并行可做）

1. **写 schema**：`skills/builder-loop/schema/loop.schema.yml` 末尾追加 `judge:` 段（参考"loop.yml schema 扩展"小节）
2. **写 prompt**：新建 `skills/builder-loop/prompts/judge-system.md`，内容见"prompts/judge-system.md 模板"小节
3. **写 known-risks.md**：新建 `skills/builder-loop/known-risks.md`，列 R1（reward hacking）/ R2（LLM 假阳性）/ R3（模型版本不可用）三条

### 阶段 B：核心脚本

4. **写 run-judge-agent.sh**：新建 `skills/builder-loop/scripts/run-judge-agent.sh`，实现见"run-judge-agent.sh 主流程"小节。关键子函数：
   - `parse_args` — 解析命名参数
   - `read_loop_judge_config` — 读 loop.yml judge 段
   - `detect_credentials` — env / oauth / none 三态检测
   - `resolve_model` — 三层 fallback + dash 规范化
   - `extract_last_assistant_text` — 反向扫 transcript jsonl + createdAt 校验
   - `build_user_message` — 拼装 input
   - `call_anthropic_api` — curl + 8s timeout + 重定向 stderr
   - `parse_response` — JSON 校验 + action 白名单
   - `write_telemetry` — 落 jsonl
   - `output_result` — stdout JSON + 退出
   
   调用约定：所有失败路径都通过 `output_downgrade_result` 输出降级 JSON（exit 0），不让 set -e 触发非零退出。

5. **写 self-check 子命令**：脚本支持 `--self-check`，输出凭证状态 + 模型选择 + API 连通性（不调真实 API，仅 ping endpoint）

### 阶段 C：stop hook 集成

6. **改 builder-loop-stop.sh — PASS 分支**：在 `PASS_START_HEAD_PREREAD="..."` 那行**之后**、`MERGE_OUT="$(bash ...merge-worktree-back.sh...)"` **之前**插入 judge 调用与路由（参考"PASS 分支"小节）。注入文案统一前缀 `[builder-loop judge | iter=X/Y | judge=Z | conf=W]`。

7. **改 builder-loop-stop.sh — FAIL 分支**：在 `ESTOP="$(bash "${SKILL_DIR}/early-stop-check.sh" ...)"` **之前**插入 retry_transient 检测（参考"FAIL 分支"小节）。注入文案同上前缀。

8. **改 builder-loop-stop.sh — outcome 补标**：在 `# ---- 2. 取当前 iter ----` **之前**加一段 outcome 补标逻辑：读上一轮 judge-trace.jsonl 末尾的 `outcome:null` 记录 → 根据当前 state / diff 自动标注 → 改写该行。约 30 行 python3。

9. **改 builder-loop-stop.sh — state.yml 写入**：在已有的 `re.sub(r'^iter:.*$', ...)` python3 段同时追加写 `last_judge_action / last_judge_confidence / last_judge_ts / consecutive_nudge_count` 4 个字段。stop_done 时 consecutive_nudge_count 清零。

### 阶段 D：测试

10. **写 test-judge-agent.sh**：mock 测试，详见"测试计划"。
11. **写 test-judge-integration.sh**：端到端测试，用 fixture 项目 + 真实 stop hook 跑一遍。

### 阶段 E：文档

12. **写 docs/judge-agent.md**：架构图 / 状态机 / 凭证 fallback / 降级矩阵 / telemetry 字段 / 排查手册。
13. **改 SKILL.md**：追加「## 判定 agent（V1.9+）」章节，简短说明 + 链接到 docs/judge-agent.md。
14. **改 README.md**：版本历史追加 V1.9 条目。
15. **改 CLAUDE.md**：「5. 已交付能力」追加 V1.9 项；「7. 已知问题」追加 7.3 节"judge agent 降级排查"。

### 阶段 F：联调

16. **smoke test**：`bash skills/builder-loop/scripts/run-judge-agent.sh --self-check` 在双方案下都返回 OK
17. **跑现有 e2e**：`fixtures/e2e/test-new-repo-loop.sh` + 其他既有测试全部通过（验证向后兼容）
18. **跑新 e2e**：`fixtures/e2e/test-judge-agent.sh` + `test-judge-integration.sh` 全过

## 风险 & 应对

| 风险 | 来源 | 应对 |
|------|------|------|
| **R1**: judge API 失败成为新故障源 | retrospective T2 | 任何异常路径降级回 PASS_CMD；telemetry 全覆盖；脚本始终 exit 0 |
| **R2**: builder 学得绕过 nudge（reward hacking） | LLM 判据本质 | 注入文案带身份标识让 builder 可反驳；连续 nudge 上限 2；known-risks.md 记开口项 |
| **R3**: 判据脱缰死循环 | LLM 误判 | iter 上限硬闸不动；连续 nudge 独立计数；max_nudge 触发后强制 stop_done |
| **R4**: 双方案凭证差异导致一边失败 | 正版/Copilot 路径不同 | env 路径**优先**于 OAuth；OAuth 字段缺失防御性处理；self-check 双路径都验证 |
| **R5**: 模型版本不可用（4.7 暂停 / 4.5 已下线） | API 模型号变化 | 三层 fallback（loop.yml > env > 硬编码）；API 4xx 视为降级 |
| **R6**: builder 最后一条回复定位错误 | retrospective T4 | 反向扫 transcript jsonl + createdAt 严格大于 hook 启动基准戳 |
| **R7**: 文案与用户输入混淆 | retrospective T7 | 统一前缀 `[builder-loop judge | ...]` + 末尾"本消息来自 builder-loop 自动判定 agent，非用户输入" |
| **R8**: 状态机污染（judge 状态与 loop 状态分离） | retrospective T1 | last_judge_* 字段加进现有 state.yml，不另起 file |
| **R9**: judge-trace.jsonl 无限增长 | telemetry | 单文件，无轮转；项目级使用建议 .gitignore；后续若超 10MB 加按 ts 分片 |
| **R10**: stop hook 总耗时增加（API 4-8s） | 引入 LLM | 仅 PASS 分支额外耗时；FAIL 分支大多直接降级（retry_transient 罕见）；可接受 |
| **R11**: prompt cache 失效 | hook 反复调相同 system prompt | system prompt 文件路径固定 + 内容稳定，依赖 Anthropic API 自动 cache |

### 退路（紧急回退）

- **完全停用 judge**：在所有项目 `.claude/loop.yml` 加 `judge: { enabled: false }` 即可。Stop hook 行为完全回到 V1.8.x。
- **回滚代码**：本次所有改动通过 `git revert <V1.9 commit>` 即可全部恢复，无 schema 破坏性变更（state 字段是追加，旧 state 仍兼容）。

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标

1. judge agent 在双路径凭证下都能正常调用（正版 OAuth + Copilot env）
2. 6 个降级场景全部正确触发降级 + 不阻断 PASS_CMD 流程
3. 3 态判定路由（PASS×3 + FAIL×3 = 6 组组合）行为符合预期
4. telemetry 落盘格式正确、覆盖率 100%、outcome 后置补标准确
5. 向后兼容：未配置 judge 段的项目行为完全等价于现状

### 关键测试场景

#### 单元测试（test-judge-agent.sh，mock API）

> 测试方法：用 `python3 -m http.server` 起 mock Anthropic API（端口 18999），脚本注入 `ANTHROPIC_BASE_URL=http://127.0.0.1:18999` + `ANTHROPIC_API_KEY=test`，每个 case 用一个不同的 mock 响应。

| # | 场景 | 输入 | 预期输出 | 预期 telemetry |
|---|------|------|----------|----------------|
| C1 | env 路径凭证 + PASS + diff 非空 + 正常 builder | mock 返回 `{action:stop_done, confidence:0.9}` | stdout: stop_done | 1 行，downgraded=false, credential_path=env |
| C2 | env 路径 + PASS + diff 为空 + builder 声称完成 | mock 返回 `{action:continue_nudge, confidence:0.87}` | stdout: continue_nudge | 1 行，action=continue_nudge |
| C3 | API 超时（mock sleep 10s，timeout=8s） | — | stdout: downgraded=true, downgrade_reason=timeout | 1 行，downgraded=true |
| C4 | API 返回 500 | mock 500 | downgraded=true, downgrade_reason=http_500 | downgraded=true |
| C5 | API 返回非法 JSON（"hello world"） | mock 200 + plain text | downgraded=true, downgrade_reason=parse_error | downgraded=true |
| C6 | API 返回 confidence=0.3 | mock 返回 `{action:continue_nudge, confidence:0.3}` | downgraded=true, downgrade_reason=low_confidence | downgraded=true |
| C7 | 凭证全缺（unset env + 临时改名 ~/.claude.json） | — | downgraded=true, downgrade_reason=missing_credentials | downgraded=true |
| C8 | judge.enabled=false（loop.yml 写死 false） | — | downgraded=true, downgrade_reason=disabled | downgraded=true |
| C9 | 模型 ID 含 dot（`claude-haiku-4.5`） | env 设 `claude-haiku-4.5` | API 调用收到 `claude-haiku-4-5`（mock 校验） | model_used=claude-haiku-4-5 |

#### 集成测试（test-judge-integration.sh，真实 stop hook）

| # | 场景 | 验证点 |
|---|------|--------|
| I1 | 模拟 builder 完成 + PASS_CMD 通过 + judge 返回 stop_done | stop hook exit 2 + 输出含 PASS 文案 + reviewer-params.json 存在 |
| I2 | 同 I1 但 judge 返回 continue_nudge | exit 2 + nudge 文案 + state.consecutive_nudge_count=1 + state.iter++ |
| I3 | 连续 2 次 continue_nudge 后第 3 次 judge 又返回 nudge | 强制 stop_done（走原 PASS 路径），telemetry 记 max_nudge_reached |
| I4 | judge 降级（kill mock 服务）+ PASS_CMD 通过 | 走原 PASS 路径（与 V1.8 行为完全一致） |
| I5 | PASS_CMD 失败 + judge 返回 retry_transient | exit 2 + retry 文案 + state.iter++ + 不更新 last_error_hash |
| I6 | PASS_CMD 失败 + judge 降级 | 走原 FAIL 路径（extract-error + early-stop） |
| I7 | outcome 后置补标：上轮 nudge 后本轮 diff 非空 + PASS | 上轮 telemetry 行的 outcome=nudge_was_correct |
| I8 | outcome 后置补标：上轮 nudge 后本轮仍 diff 空 | outcome=nudge_likely_false_positive |
| I9 | judge.enabled=false → stop hook 行为完全 = V1.8 | 所有现有 e2e（test-new-repo-loop, test-zombie-selfheal 等）依然通过 |

#### 边界条件 / 已知坑（重点关注）

1. **transcript jsonl 反向扫的 createdAt 校验**：构造一个"旧 user message 的 createdAt 大于新 assistant message 的 createdAt"（retrospective 错误 11），验证 judge 仍然定位到正确的 last_assistant_text
2. **flock 互斥与 judge 并发**：模拟 CC 并发触发两次 stop hook，验证 judge 只调用 1 次（被 flock 拦在外面的实例 exit 0）
3. **PASS_START_HEAD 预读**（V1.8.3 hotfix 的关键路径）：judge 调用必须在 `cleanup_worktree` 之前完成，rm state 后再调 judge 会 grep state 报错。验证 judge 调用插入的位置正确
4. **僵尸 state 自愈**（V1.8.1）：state.active=false 时 judge **不应该**被调用，hook 直接归档放行
5. **bootstrap 兜底激活**（V1.8.2）：兜底激活后第一轮 PASS_CMD 时，state 是新建的 consecutive_nudge_count 缺字段。judge 应当视为 0，不报 missing_field 错误

### 测试深度

**深度模式**：所有 9 个单元 case + 9 个集成 case + 5 个边界 case 全部覆盖。预计测试代码行数 ~600 行 bash + 配套 fixture 数据。

测试落地目录：
- `skills/builder-loop/fixtures/e2e/test-judge-agent.sh`（单元，含 mock server 启动逻辑）
- `skills/builder-loop/fixtures/e2e/test-judge-integration.sh`（集成）
- `skills/builder-loop/fixtures/e2e/judge-fixtures/`（mock 响应 JSON / 测试 transcript jsonl）

### 不在测试范围内（开口项）

- **真实 API 准确率回归**：需要黄金样本集（10~20 个真实 builder transcript），本方案不构建，由后续 telemetry 攒数据后单独评估
- **多 worktree 并行下 judge 互不干扰**：当前 V1.8 multi-state 架构已保证，judge 调用走 per-state，复用既有保证
- **API 计费 / 成本压测**：每 PASS 一次约 $0.001（haiku），预估每天 < $0.1，不需要专项测试

<!-- /role -->

---

## 摘要

把 stop hook 的"是否需要续接"判据从纯 PASS_CMD 二值机器判据 + iter 上限 + EARLY_STOP 信号，叠加一道 LLM 语义判据（judge agent），识别 PASS_CMD 看不见的盲区（假完成 / 求助 / 偷懒 / 网络中断）。判定输出 3 态（continue_nudge / stop_done / retry_transient）路由 stop hook 出口；任何故障路径全降级回 PASS_CMD 二值判据；telemetry 100% 覆盖。技术选型 A1（Hook 内嵌 API 调用）+ B 中等输入 + C2 PASS_CMD 后叠加；凭证双路径兼容（正版 OAuth + Copilot env）；模型号三层 fallback。改动级别 L3。
