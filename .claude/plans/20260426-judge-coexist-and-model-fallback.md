# V2.1 — Judge Agent 长期共存方案 + 模型降级链

<!-- role:shared -->

## 背景 & 目标

**背景**：V1.9 的 judge agent 凭证检测优先级为 `ANTHROPIC_API_KEY env > ~/.claude.json oauthAccount.accessToken > none`。正版 Max CC 用户主会话走 OAuth，CC 进程不 export `ANTHROPIC_API_KEY`，子进程（stop hook → judge agent）继承 env 也读不到；同时 OAuth token 不在公开字段，第二条路径也走不通 → judge 一直降级回 PASS_CMD 二值判据。

需要一个**长期共存方案**让正版 Max 用户也能用 judge agent，同时**优先用 sonnet 提升判定质量**，sonnet 失败时**自动降级 haiku** 保稳。

**目标**：
1. judge agent 自动加载用户配置的 env file，主 CC env 完全干净，主会话保持 Max OAuth
2. 默认模型 sonnet（claude-sonnet-4-6 是 copilot-proxy 唯一支持的 sonnet ID），失败 N 次切 haiku
3. 失败计数本 loop 内有效（state 字段，loop PASS 自动重置）
4. 完全向后兼容 V1.9（已用 env 的 copilot 用户不受影响）

**非目标**：
- 不做"跨 loop 持久化降级"（用户选了"本 loop state 字段"路径）
- 不改 stop hook 的 judge 调用契约（仍是 stdout JSON）
- 不引入新依赖

## 预估改动级别

**L2 + L3 混合**：
- 改 `run-judge-agent.sh` 内部逻辑（L2）
- 新增 state schema 字段 + loop.yml.judge 字段（L3，但是新增不是改签名）
- 完全向后兼容（缺新字段时退回 V1.9 行为）

按 L3 走流程：先 spawn tester 还是直接进 loop？方案约定**直接进 loop**，因为：
- judge agent 已经有 V1.9 单元测试 + 集成测试套件，新功能在它们基础上扩展（mock 容易复用）
- env file 加载逻辑独立可测、降级链逻辑可状态机驱动测试
- builder 自己写 fixture 比 spawn tester 黑盒写效率高（tester 是 reviewer hint 后的补救）

## 约束 & 边界

- **不破坏 V1.9 行为**：已设 `ANTHROPIC_API_KEY` env 的用户、已写 `loop.yml.judge.model` 的项目，本次升级后行为不变
- **不写主 CC env**：env file 仅在 `run-judge-agent.sh` 子进程内 source，主 CC 进程 env 不动
- **降级状态隔离**：state 字段，loop PASS 删 state 后自动重置；不引入跨 loop 共享文件
- **不漏写 state**：active_model / failures_count 必须在调用前后稳定写回 state，否则下次 judge 调用读到错误的活跃模型
- **stop hook 契约不变**：run-judge-agent.sh 输出仍是单行 JSON，新增字段（如 `failure_classified` / `fallback_triggered` / `active_model_after`）只用于诊断/telemetry，不改判定路由

## 技术选型

### 模型降级方案（用户已拍板）

| 选项 | 优点 | 缺点 | 选择 |
|------|------|------|------|
| A. 单模型固定 | 最简，无状态 | sonnet 抖动直接降级到 PASS_CMD 二值 | ❌ |
| **B. primary + fallback 两级链**（推荐） | 失败软着陆 haiku 不丢能力，本 loop 内不断重试浪费 | 多一组 state 字段 | ✅ |
| C. N 级链（sonnet → haiku → opus → ...） | 灵活 | 配置复杂、状态字段多 | ❌（YAGNI） |

### 降级触发条件（用户已拍板）

| 失败类型 | 计数 | 理由 |
|---------|------|------|
| API timeout | ✅ 计数 | 后端不可达 / 模型响应过慢 |
| HTTP 5xx / 502 / 503 | ✅ 计数 | 后端真实失败 |
| JSON parse_error | ✅ 计数 | 模型返回 markdown 包裹 / 拒答 |
| HTTP 401 / 403 | ❌ 不计数 → 降级原 PASS 路径 | 凭证问题，换 model 没用 |
| HTTP 429 rate_limit | ❌ 不计数（特殊处理） | 等一会再试更稳，不是模型问题 |
| low_confidence | ❌ 不计数（用户排除） | 模型判断能力问题，haiku 可能更不准 |

### 降级生命周期（用户已拍板）

**本 loop 内 state 字段** — `state.judge_consecutive_failures` + `state.judge_active_model`，loop PASS → state 删除 → 下个 loop 重新从 sonnet 开始。

### env file 路径（用户已拍板）

**全局** `~/.claude/skills/builder-loop/judge-env.sh`（不进 git，用户本地配）。

`loop.yml.judge.credentials_file` 字段允许项目级覆盖（默认值即全局路径）。

<!-- /role -->

<!-- role:builder -->

## 方案设计

### state schema 新增字段（V2.1）

```yaml
# 已有字段不变（参考 V1.9）：
last_judge_action: "continue_nudge"
last_judge_confidence: 0.8
last_judge_ts: "2026-04-26T..."
consecutive_nudge_count: 1

# V2.1 新增：
judge_active_model: "claude-sonnet-4-6"     # 当前活跃模型；降级后切 haiku
judge_consecutive_failures: 0                # 连续 sonnet 失败计数；达阈值切 fallback 后重置 0
```

**字段语义**：
- 缺失时 = 初始值（active_model = primary_model 配置 / failures = 0）
- 切到 fallback 后，judge_active_model 字段值改为 fallback_model 字符串
- judge_consecutive_failures 仅在 active_model = primary 时累加（fallback 已经是兜底，再失败 → downgraded=true 走原 PASS 路径，不再切第三档）

### loop.yml.judge schema 扩展

```yaml
judge:
  enabled: true                                            # 不变
  primary_model: "claude-sonnet-4-6"                       # 新：V2.1 默认（替代旧 model 字段）
  fallback_model: "claude-haiku-4-5"                       # 新：降级目标，可空（空则不降级，失败直接 downgraded）
  fallback_after_failures: 2                               # 新：连续失败阈值（默认 2）
  api_timeout_sec: 15                                      # 改：默认 8 → 15（sonnet 单次 5.8s 留出余量）
  confidence_threshold: 0.5                                # 不变
  credentials_file: "~/.claude/skills/builder-loop/judge-env.sh"  # 新：默认值即全局路径
  # V1.9 字段（兼容保留）：
  # model: <id>  →  自动等价于 primary_model（若同时设两个 → primary_model 优先）
  # max_consecutive_nudges: <n>  → 不变
  # system_prompt_path: <path>  → 不变
```

**模型 resolve 顺序**（V2.1）：
1. `state.judge_active_model`（运行时降级状态优先）
2. `loop.yml.judge.primary_model`（V2.1 配置）
3. `loop.yml.judge.model`（V1.9 兼容）
4. `$ANTHROPIC_DEFAULT_HAIKU_MODEL` env
5. `"claude-sonnet-4-6"`（V2.1 改 default）

### env file 加载（run-judge-agent.sh 顶部）

```bash
# V2.1: 凭证检测前 source 全局 env file（仅 env 缺失时）
# 优先级：env > judge-env.sh > oauth > none
JUDGE_ENV_FILE_DEFAULT="$HOME/.claude/skills/builder-loop/judge-env.sh"
JUDGE_ENV_FILE="${JUDGE_ENV_FILE_OVERRIDE:-$JUDGE_ENV_FILE_DEFAULT}"
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -f "$JUDGE_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; source "$JUDGE_ENV_FILE"; set +a
fi
```

`JUDGE_ENV_FILE_OVERRIDE` 由 `run-judge-agent.sh` 解析 `loop.yml.judge.credentials_file` 后通过 env 传入子段（避免 source 时 expand 项目相对路径出错）。

### 失败分类逻辑

`call_api()` 现有错误分类基础上加：

```bash
case "$err" in
  ERR_TIMEOUT|ERR_HTTP_5*)  CLASSIFIED_FAIL=1 ;;     # V2.1: 计数
  ERR_PARSE)                 CLASSIFIED_FAIL=1 ;;
  ERR_HTTP_401|ERR_HTTP_403) CLASSIFIED_FAIL=0 ;;     # 凭证问题不计数
  ERR_HTTP_429)              CLASSIFIED_FAIL=0 ;;     # rate_limit 不计数
  *)                         CLASSIFIED_FAIL=0 ;;
esac
```

### 主控流程（伪码）

```
1. 加载 env file（若 env 缺失）
2. detect_credentials() → 路径 / 模型 resolve
3. read_state() → active_model / failures_count
4. call_api(active_model)
5. if 成功:
     write_state(active_model, failures_count=0)
     output_json(action=..., active_model_after=active_model)
6. if 失败 + classified:
     if active_model = primary_model:
       failures_count++
       if failures_count >= threshold and fallback_model 非空:
         active_model = fallback_model
         failures_count = 0  # 重置避免再降
         retry call_api(fallback_model) 一次
         if 成功 → write_state + output_json(active_model_after=fallback)
         if 失败 → downgrade
       else:
         write_state(failures_count)
         downgrade
     else:  # 已经是 fallback 还失败
       downgrade
7. if 失败 + 未 classified（如 401/429）:
     downgrade（不修改计数 / 不切模型）
```

`downgrade` = 输出 `{"action":"stop_done|continue_strict","downgraded":true,"downgrade_reason":...}`，stop hook 走原 PASS_CMD 二值路径。

### 文件地图

#### 改动

| 文件 | 改动 |
|------|------|
| `skills/builder-loop/scripts/run-judge-agent.sh` | env file 加载 / state 字段读写 / 失败分类 / 降级链 / fallback retry / 默认模型 / 默认 timeout |
| `skills/builder-loop/schema/loop.schema.yml` | judge 段新增 primary_model / fallback_model / fallback_after_failures / credentials_file 字段；api_timeout_sec 默认 8 → 15 |
| `skills/builder-loop/SKILL.md` | judge 配置说明 + 状态 schema 加 V2.1 字段 |
| `skills/builder-loop/docs/judge-agent.md` | 凭证矩阵新增 env-file 路径 + 降级链流程图 + sonnet 默认理由 |
| `skills/builder-loop/known-risks.md` | R5 OAuth 不可用 → 补 V2.1 workaround 已落地 |
| `CLAUDE.md` | 已交付能力加 V2.1 + 7.3 排查手册扩 |
| `skills/builder-loop/README.md` | 5.3 fixture 表格补 + 演进路径加 V2.1 |

#### 新增

| 文件 | 用途 |
|------|------|
| `skills/builder-loop/judge-env.sh.example` | 模板，用户复制后改路径用（含注释说明 sk-666 用法） |
| `skills/builder-loop/fixtures/e2e/test-judge-env-file-load.sh` | env file 加载行为测试 |
| `skills/builder-loop/fixtures/e2e/test-judge-model-fallback.sh` | sonnet 失败 → haiku 降级链测试 |
| `.claude/loop.yml` | PASS_CMD 加两个新 stage |

#### 不动

- `scripts/builder-loop-stop.sh`（V2.0 路径分流不变 + judge 调用契约不变）
- `skills/builder-loop/scripts/setup-builder-loop.sh`（state 写入字段时 V2.1 新字段为可选，由 run-judge-agent.sh 运行时 upsert）
- 其他所有 V2.0 改动

### 兼容性矩阵

| 用户 | V1.9 行为 | V2.1 升级后行为 |
|------|----------|---------------|
| Copilot CC（env 已设 sk-666 + base_url） | 走 env 路径，model = haiku（V1.9 默认） | 走 env 路径，model = sonnet（V2.1 默认）→ 失败链回 haiku |
| 正版 Max CC（无 env、无 env file） | missing credentials → 降级（走原 PASS） | 同 V1.9 行为（env file 不存在） |
| 正版 Max CC（有 env file） | 不读 env file → 降级 | source env file → env 路径，sonnet → haiku |
| 项目 loop.yml 写了旧 `model: <id>` 字段 | 用旧字段 | 兼容旧字段（=primary_model），但走 V2.1 降级链 |

## 风险 & 应对

### R1：sonnet 比 haiku 慢，PASS_CMD 跑完到 stop hook 总耗时变长

- 现状：haiku 4s，sonnet 5.8s，差 1.8s
- 影响：单 iter 总耗时 +1.8s，loop 多轮后累计变长（5 iter 约 +9s）
- 应对：可接受（用户拍板用 sonnet）；users 不满意可改 loop.yml.judge.primary_model 为 haiku 退回旧行为

### R2：env file 误写 / 读取失败 / 语法错误

- 现状：source 失败会让脚本静默继续（set -uo pipefail 而非 set -e）
- 影响：env file 语法错 → ANTHROPIC_API_KEY 仍空 → 降级
- 应对：source 后立即 verify ANTHROPIC_API_KEY 是否生效；invalid → stderr 警告 + 走 oauth 路径继续检测（保 V1.9 行为）

### R3：state 字段污染下次 loop（loop 异常退出，state 残留）

- 现状：V1.8.1 + V1.8.3 已经处理过僵尸 state（active=false 归档 + flock）
- 应对：本次新增字段都是数值/字符串，残留不影响 setup 创建新 state（new state 不带这些字段，judge 读到缺失值→默认值）

### R4：fallback retry 让本轮 judge 总耗时翻倍

- 现状：sonnet 5.8s 失败 → fallback haiku 4s → 总 ~10s
- 影响：单次降级触发的 judge 调用慢一倍
- 应对：仅触发 1 次（达阈值时降级 + retry 1 次），后续 active_model 已是 haiku 不再 retry。可接受。

### R5：用户在 worktree 里改 loop.yml.judge.primary_model，本轮立即生效与否？

- V2.0 已经统一 PASS_CMD/judge 都从 RUN_CWD（worktree）读 loop.yml
- V2.1 只要继续遵循 V2.0 路径，worktree 内改 primary_model 立即生效 ✅

## 执行任务列表（builder 视角）

按以下顺序写代码：

1. **`skills/builder-loop/scripts/run-judge-agent.sh`** 改造：
   - 顶部加 `JUDGE_ENV_FILE` 加载（仅 env 缺失时 source）
   - `read_loop_yml_judge_config()` 增加 primary_model / fallback_model / fallback_after_failures / credentials_file 字段解析（保 model / max_consecutive_nudges 兼容）
   - 加 `read_state_judge_runtime()` 函数读 active_model / failures_count
   - 加 `write_state_judge_runtime(active_model, failures)` 函数 upsert（兼容缺字段）
   - 加 `classify_failure(err_code)` 函数返回 0/1
   - 主调用流程改造：active_model resolve → call_api → 分类 + state 写回 → 必要时 retry fallback
   - 默认值：primary_model = `claude-sonnet-4-6`，api_timeout_sec = 15
   - JSON 输出新增 `active_model_after` / `failure_classified` / `fallback_triggered` 字段（不影响 stop hook 路由）

2. **`skills/builder-loop/judge-env.sh.example`**：
   ```bash
   # judge agent 凭证配置 — 只在 ANTHROPIC_API_KEY env 缺失时 source
   export ANTHROPIC_API_KEY=sk-666     # copilot-proxy 占位 / 真实 sk-ant-key
   export ANTHROPIC_BASE_URL=http://localhost:4142   # copilot-proxy / 删此行走官方 API
   ```

3. **`skills/builder-loop/schema/loop.schema.yml`** 扩 judge 段。

4. **测试**：`test-judge-env-file-load.sh` + `test-judge-model-fallback.sh`，参考 V1.9 mock 套路（mock copilot-proxy）。

5. **文档**：CLAUDE.md / SKILL.md / README.md / known-risks.md / judge-agent.md。

6. **`.claude/loop.yml`** 加两个新 stage。

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标

验证 V2.1 V judge agent 三大新行为：
1. env file 加载（主 env 干净时 source / 主 env 已设时不覆盖 / 文件缺失时跟 V1.9 一致）
2. 模型降级链（sonnet 连续失败 → haiku，state 字段正确变更）
3. 默认值 + 向后兼容（V1.9 配置仍工作）

### 关键测试场景

#### A. env file 加载（test-judge-env-file-load.sh）

| Case | 主 env | judge-env.sh 存在？ | 期望行为 |
|------|--------|--------------------|---------|
| A1 | 干净 | 存在（含 sk-666 + base_url） | source 后凭证可用，self-check OK |
| A2 | 已设 sk-ant-real | 存在（含 sk-666） | 不 source，使用主 env 的 sk-ant-real |
| A3 | 干净 | 不存在 | 不 source，跟 V1.9 missing credentials 行为一致（self-check exit 1） |
| A4 | 干净 | 存在但语法错 | source 失败有 stderr 警告，凭证仍空，降级 |
| A5 | 干净 | 存在 + loop.yml.judge.credentials_file 指定别处 | 用 loop.yml 路径而非默认全局路径 |

#### B. 模型降级链（test-judge-model-fallback.sh）

mock copilot-proxy 服务（python http.server）控制返回不同 status：
- `mock_mode=ok` → 200 + 合规 JSON
- `mock_mode=timeout` → 长 sleep
- `mock_mode=5xx` → 502
- `mock_mode=parse_err` → 200 + 非 JSON

| Case | 调用序列 | mock 模式 | 期望 state 字段变化 |
|------|---------|----------|-------------------|
| B1（连续 sonnet 成功） | 调 3 次 | 全 ok | active_model = sonnet, failures = 0 |
| B2（sonnet 1 失败） | 调 1 次 | 5xx | active_model = sonnet, failures = 1（未达阈值） |
| B3（sonnet 2 连续失败 → 切 haiku） | 调 2 次 | 全 5xx | 第 2 次后 active_model = haiku, failures = 0；输出 fallback_triggered=true |
| B4（sonnet 失败 → 切 haiku 后 haiku 也失败） | 调 3 次 | 全 5xx | 切 haiku 后再失败 → output downgraded=true（不再切第三档） |
| B5（sonnet 1 失败 + 1 成功 → 计数清零） | 调 2 次 | 5xx, ok | active_model = sonnet, failures = 0（成功后重置） |
| B6（401 不计数） | 调 2 次 | 全 401 | active_model = sonnet, failures = 0；output downgraded=true |
| B7（rate_limit 不计数） | 调 1 次 | 429 | active_model = sonnet, failures = 0；downgraded=true downgrade_reason=rate_limit |
| B8（parse_error 计数） | 调 2 次 | 全 parse_err | 第 2 次后切 haiku, failures=0 |
| B9（fallback_model 空时不切，直接降级） | loop.yml 留空 fallback_model + 调 2 次 | 全 5xx | 不切，failures=2，output downgraded=true |
| B10（缺 state 字段时按默认值） | 已存在的旧 state（无 V2.1 字段） + 调 1 次 | ok | 写入 V2.1 字段，active_model = primary, failures = 0 |
| B11（worktree 内改 primary_model 本轮生效） | worktree loop.yml 改 primary 为别名 | ok | API 调用用 worktree 配置的模型 |

#### C. 向后兼容（融合到上述 fixture）

| Case | 配置 | 期望 |
|------|------|------|
| C1 | loop.yml 仅含旧 `model: claude-haiku-4-5` 字段 | 等价 primary_model = haiku，无 fallback，失败直接降级 |
| C2 | loop.yml 同时含 `model` + `primary_model` | primary_model 优先 |

### 测试深度

**深度** — V2.1 新增凭证路径 + 降级状态机，必须 mock copilot-proxy 才能稳定复现各 status code。fixture 内嵌 mock 服务（与 V1.9 test-judge-agent.sh 同套路）。

期望覆盖率：上述 5 + 11 + 2 = 18 case，所有 case 含 mock 模式 / state 前后比对 / JSON 输出字段断言。

<!-- /role -->

## 验收标准

1. ✅ 主 CC（Max OAuth）启动时不需要任何 env 变更，judge agent 仍能调用 sonnet
2. ✅ `bash run-judge-agent.sh --self-check` 在配 env file 后输出 `credential_path=env`
3. ✅ sonnet 连续 2 次失败后，本 loop 内后续 judge 调用都用 haiku（state.judge_active_model 验证）
4. ✅ 同 loop 内 haiku 也失败 → JSON 输出 `downgraded=true`（不再切第三档）
5. ✅ loop PASS state 删除 → 下个 loop 重新从 sonnet 开始
6. ✅ V1.9 配置（仅 `model:` 字段）升级后行为不变
7. ✅ 18 个测试 case 全部通过 + 现有 14 个 V1.x/V2.0 fixture 不回归
8. ✅ Reviewer 通过（≤2 个 🟡，无 🔴）
9. ✅ 文档同步（doc-maintainer 6 步 audit 全 PASS）
