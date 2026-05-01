# V2.5 stop hook 可观测性 — debug log + diagnose 脚本 + setup 自检

<!-- role:shared -->

## 背景 & 目标

### 现状
A 簇 (stop hook 稳定性) 有两个反向极端复现：
- **c1**（improvements.md L46）V2.4 落地 session：stop hook 该跑没跑（loop 哑火），9 分钟空等无任何 forensic 数据
- **c5**（improvements.md L29，含 2026-04-29 23:31 bullet 5 新发现）generator session a9a1ceef：stop hook 不该跑反复跑 NOOP（4 分钟内 3 次 + commit 后还多 1 次），无诊断手段

**关键事实**：CLAUDE.md §7.1 排查指引让用户 `tail -20 /tmp/builder-loop-stop-debug.log`，但 `grep -r 'debug.log' scripts/ skills/builder-loop/scripts/` 零命中——**文档承诺的诊断路径在代码里不存在**。c1 / c5 复盘时没数据可看，所以两轮排查都靠 trace.jsonl + state file 旁路推理，都没定位到根因（c5 bullet 5 显式承认"需先审 stop hook 实际检测代码确认到底看哪些字段触发 bootstrap"）。

### 目标
**阶段 1：可观测性建设**（不改触发逻辑）
1. **debug log 写入**：stop hook 顶部 + 关键决策点 + exit 处写 NDJSON 行到 `<P>/.claude/builder-loop/stop-hook-debug.log`，对齐 CLAUDE.md §7.1 承诺
2. **diagnose-stop-hook.sh**：一键排查脚本，dry-run 不改状态，列 hook 注册 / 软链状态 / state 目录 / lock 目录 / 最近 trace + debug log 摘要 / git worktree list
3. **setup-builder-loop.sh 末尾自检**：检测 settings.json 含 builder-loop-stop.sh hook 注册 + 关键软链有效 → 缺则 stderr 醒目警告

**阶段 2 推后**：c5 节流（dirty 指纹 / AskUserQuestion 静默）/ c1 触发链路修复——等阶段 1 真实数据让根因显形再决定治法（可能根因与现有假设不同）。

### 成功标准
- 装好 V2.5 后再现 c5 自激场景 → debug log 能直接看出每次 bootstrap 触发是因为 HAS_DIFF / HEAD 推进 / 哪个具体条件
- 模拟 hook 注册丢失（手动从 settings.json 删 builder-loop-stop.sh 条目）→ 下次 setup 末尾醒目警告
- diagnose-stop-hook.sh 在 generator session a9a1ceef 类型问题上能 ≤30 秒给出 root cause 候选

## 预估改动级别
**L2**（实现改动）。改 2 个脚本（builder-loop-stop.sh 加 debug log / setup-builder-loop.sh 加自检）+ 新增 1 个脚本（diagnose-stop-hook.sh）+ CLAUDE.md §7.1 路径修正 + 1 个 e2e fixture。无新接口、无 schema 变更。

## 验收标准
1. 跑 `bash skills/builder-loop/fixtures/e2e/test-stop-hook-debug-log.sh` 全部 case PASS
2. 在装了 V2.5 的项目里手动喂 stop hook 输入 → `<P>/.claude/builder-loop/stop-hook-debug.log` 有新条目
3. 手动从 settings.json 删除 builder-loop-stop.sh hook 条目 → 下次 setup 末尾 stderr 含 `⚠️ builder-loop-stop.sh hook 未在 settings.json 注册`
4. `bash skills/builder-loop/scripts/diagnose-stop-hook.sh` 在主仓 cwd 跑 → stdout 输出 6 段诊断（hook 注册 / 软链 / state / lock / trace / worktree）
5. CLAUDE.md §7.1 排查指引路径从 `/tmp/builder-loop-stop-debug.log` 改为 `<P>/.claude/builder-loop/stop-hook-debug.log`
6. CLAUDE.md §5 加 V2.5 段；improvements.md c1 / c5 不删（阶段 2 还没做），但更新备注「阶段 1 可观测性已落地（V2.5），等真实数据再修触发链路」

<!-- /role -->

<!-- role:builder -->

## 约束 & 边界

### 不能碰
- **stop hook 触发条件 / bootstrap 兜底逻辑**：本期不改任何触发判定（HAS_DIFF / DOC_PATTERN / FOUND_LOOP_ONLY / 跨 session 守门），只加观测点
- **state schema**：不新增 / 修改 state.yml 字段
- **flock 互斥语义**：debug log 写入要兼容 flock 抢锁失败的早退路径（hook 在 L149 抢锁失败时也要能记一行）
- **c5 / c1 触发链路修复**：推后到阶段 2

### 必须兼容
- **现有 trace.jsonl / loop-trace.jsonl**：debug log 是新增独立文件，不动现有 trace
- **跨 session 守门 / flock 互斥**：debug log 写入失败（权限 / 磁盘满）→ 静默继续，不阻断 hook 主流程
- **NFS / FUSE 文件系统**：debug log 用追加写（`>>`），不依赖 flock 文件锁

## 技术选型

### 方案 A：完整可观测性（debug log + diagnose + setup 自检）★推荐
**做法**：三件套都做，对齐 CLAUDE.md §7.1 文档承诺；debug log 路径从 `/tmp/...` 改到项目本地 `<P>/.claude/builder-loop/stop-hook-debug.log`（多项目隔离 + 可被 .gitignore 排除）。
- 优点：c1 / c5 复盘有数据可看；setup 自检防 c1 根因 1（hook 注册丢失）；diagnose 一键脚本是排查通用入口
- 缺点：每次 stop hook 触发要写 N 行 IO（NDJSON 平均 200 字节，每次 stop hook 写 5-10 行 = 1-2 KB），高频 stop 项目 debug log 增长快——靠滚动控制（默认 1MB rotate，保留 5 个）

### 方案 B：仅 diagnose 脚本（不改 stop hook）★rejected
**做法**：只做 diagnose-stop-hook.sh，stop hook 不写 debug log。
- 优点：零改动 stop hook 主流程，零回归风险
- 缺点：diagnose 拍快照只能看当前状态（hook 注册 / state / trace），看不到「过去 N 次 stop hook 触发的实际决策路径」——c1 / c5 类「reproduce 困难 / 间歇发生」问题仍无 forensic

### 方案 C：仅 debug log（不做 diagnose / setup 自检）★rejected
**做法**：只写 debug log，用户排查时手动 `tail` 看。
- 优点：最小改动
- 缺点：raw NDJSON 用户读起来累；setup 自检缺失意味着 c1 根因 1（hook 丢注册）发生时用户无任何提示，必须靠诊断脚本主动跑才知道

**选 A 理由**：c1 + c5 都是 stop hook 行为间歇性问题，debug log 的 forensic 价值最高；diagnose / setup 自检是补强各自的预防 / 主动排查环节，三件套是最小完整集。

## 方案设计

### 1. debug log 写入（builder-loop-stop.sh）

#### log 路径
- **路径**：`<PROJECT_ROOT>/.claude/builder-loop/stop-hook-debug.log`
- **大小限**：默认 1 MB，超出 → rotate（`mv stop-hook-debug.log stop-hook-debug.log.1`，老的 `.1 → .2 → ... → .5`，超过 .5 删除）
- **格式**：每行一条 NDJSON

```jsonc
{"ts":"2026-04-30T12:34:56.789Z","session":"abc12345","cwd":"/path","slug":"<slug>","phase":"<phase>","details":{...}}
```

#### 写入点（关键决策路径插桩）
| phase | 时机 | details |
|-------|------|---------|
| `entry` | hook 入口（解析 stdin 后立即） | `{cwd, transcript_path}` |
| `locate_result` | locate-state.sh 调用后 | `{state_file, found_loop_only, project_root, run_cwd}` |
| `flock_acquire` | flock 抢锁结果 | `{lock_file, acquired:true/false}` |
| `bootstrap_check` | bootstrap 段触发条件判定 | `{found_loop_only, has_diff, changed_files_count, doc_only, has_recent_commit, decision:"trigger"/"skip_doc_only"/"skip_no_diff"/"skip_existing_worktree"}` |
| `pass_cmd_start` | 跑 PASS_CMD 前 | `{iter, run_cwd}` |
| `pass_cmd_result` | PASS_CMD 跑完 | `{result:"PASS"/"FAIL", last_line, last_stage, log_path}` |
| `merge_result` | merge-worktree-back.sh 后 | `{merge_action, merge_last_line}` |
| `judge_result` | run-judge-agent.sh 后（有 judge 时） | `{action, downgraded, downgrade_reason, conf, model_used}` |
| `early_stop` | early-stop-check.sh STOP 时 | `{reason, iter}` |
| `exit` | 脚本退出前 | `{code:0/2, reason:"pass"/"fail_continue"/"early_stop"/"need_arbitration"/"silent_passthrough"/...}` |

#### 写入实现（bash 函数）

```bash
debug_log() {
  local phase="$1" details="$2"
  local log_dir log_file
  log_dir="${PROJECT_ROOT:-.}/.claude/builder-loop"
  log_file="${log_dir}/stop-hook-debug.log"
  mkdir -p "$log_dir" 2>/dev/null || return 0
  # rotate（lazy check 避免每次 stat IO）
  if [ "${DEBUG_LOG_ROTATE_CHECKED:-0}" -eq 0 ] && [ -f "$log_file" ]; then
    local sz
    sz="$(stat -c%s "$log_file" 2>/dev/null || stat -f%z "$log_file" 2>/dev/null || echo 0)"
    if [ "${sz:-0}" -gt "${BUILDER_LOOP_DEBUG_LOG_MAX_BYTES:-1048576}" ]; then
      for i in 5 4 3 2 1; do
        [ -f "${log_file}.$i" ] && mv "${log_file}.$i" "${log_file}.$((i+1))" 2>/dev/null || true
      done
      [ -f "${log_file}.6" ] && rm -f "${log_file}.6" 2>/dev/null || true
      mv "$log_file" "${log_file}.1" 2>/dev/null || true
    fi
    DEBUG_LOG_ROTATE_CHECKED=1
  fi
  # 用 python3 拼 JSON 防引号 / 特殊字符破格式
  TS="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)" \
  PHASE="$phase" DETAILS="$details" CWD="$CWD" SESSION="${HOOK_SESSION_ID:-}" SLUG="${SLUG:-}" \
    python3 -c "
import os, json, sys
try:
    details = json.loads(os.environ.get('DETAILS') or '{}')
except Exception:
    details = {'raw': os.environ.get('DETAILS', '')}
line = {
    'ts': os.environ.get('TS',''),
    'session': os.environ.get('SESSION','')[:8],
    'cwd': os.environ.get('CWD',''),
    'slug': os.environ.get('SLUG',''),
    'phase': os.environ.get('PHASE',''),
    'details': details,
}
print(json.dumps(line, ensure_ascii=False))
" >> "$log_file" 2>/dev/null || true
}
```

注意细节：
- **IO 失败容忍**：`mkdir -p ... || return 0` + 写入 `... || true`，任何失败不阻断 hook
- **flock 之前也写**：`entry` / `locate_result` 必须在 `exec 200>"$LOCK_FILE"` 之前完成，不依赖 flock 成功
- **flock 失败的 exit 路径**：在 `flock -n 200` 失败 exit 0 之前补一行 `flock_acquire` + `exit silent_passthrough`

#### CLAUDE.md §7.1 路径修正
原文：
```
2. 若有 `legacy/*.bak`，用 `tail -20 /tmp/builder-loop-stop-debug.log` 查 hook 退出原因
```
改为：
```
2. 用 `tail -50 <project_root>/.claude/builder-loop/stop-hook-debug.log` 查最近 hook 触发的关键决策点（V2.5 引入；NDJSON 格式）
```

### 2. diagnose-stop-hook.sh

`skills/builder-loop/scripts/diagnose-stop-hook.sh [project_root]`（默认 cwd 向上找 loop.yml 锚点）

输出 6 段（每段标题加标 [✅ ok] / [⚠️ warn] / [❌ fail]）：

```
=== diagnose-stop-hook v0.1 ===
project_root: /xxx
ts: 2026-04-30T12:34:56Z

[1/6] settings.json hook 注册 ............ [✅ ok / ❌ missing]
  - Stop hook (builder-loop-stop.sh): present / MISSING
  - SubagentStart hook (tester-lock-write.sh): present / MISSING
  - PreToolUse hooks (5 项 expected vs N 项 actual)
  - 列出 missing 项让用户重跑 install.sh

[2/6] 软链状态 ........................... [✅ ok / ❌ broken]
  - ~/.claude/scripts/builder-loop-stop.sh → /xxx/scripts/... (broken: dangling)
  - ~/.claude/skills/builder-loop/ → ...
  - 列出 broken 项让用户重跑 install.sh

[3/6] state 目录 .......................... [✅ ok]
  - <P>/.claude/builder-loop/state/ 下 N 个文件
  - 各 state 摘要：active / iter / worktree_path / 是否存活

[4/6] lock / cursor / stash 状态 ......... [✅ ok / ⚠️ stale]
  - stop-hook-*.lock 文件清单
  - last_processed_head 内容 vs 当前 HEAD
  - 是否有 builder-loop:auto:slug=... stash 残留

[5/6] 最近 trace + debug log 摘要 ........ [✅ ok / ⚠️ no-data]
  - loop-trace.jsonl 末 5 行（带 ts / iter / result / stage）
  - stop-hook-debug.log 末 10 行（带 phase / decision）
  - 若两者都空 → 提示「stop hook 从未在本仓触发，可能 hook 注册丢失（参 [1/6]）」

[6/6] git worktree list ................... [✅ ok / ⚠️ ghost]
  - 各 worktree path / branch / HEAD
  - 标识哪些是 loop/<slug> 前缀（builder-loop 创建）
  - 检测 worktree 在 git worktree list 但物理目录不存在 = ghost
```

实现细节：
- **dry-run 严格**：只读 `git -C <P> ...` / `cat` / `stat`，不写任何文件不调任何外部 API
- **退出码**：0 = 全 ok / 1 = 至少一段 warn / 2 = 至少一段 fail
- **支持 `--json` flag**：输出 JSON 形式让 CI / hook 消费

### 3. setup-builder-loop.sh 末尾自检

在现有 V2.4 cwd 警告段之后、`echo "提示：下次 Stop hook ..."` 之前插入：

```bash
# V2.5: hook 注册 + 软链自检（缺失时 stderr 醒目警告）
SETTINGS_JSON="${HOME}/.claude/settings.json"
HOOK_REG_OK=1
LINK_OK=1
if [ -f "$SETTINGS_JSON" ]; then
  if ! python3 -c "
import json, sys
try:
    cfg = json.load(open('$SETTINGS_JSON'))
    hooks = cfg.get('hooks', {})
    stop_hooks = hooks.get('Stop', [])
    for entry in stop_hooks:
        for h in entry.get('hooks', []):
            cmd = h.get('command', '')
            if 'builder-loop-stop.sh' in cmd:
                sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    HOOK_REG_OK=0
  fi
fi
[ ! -L "${HOME}/.claude/scripts/builder-loop-stop.sh" ] && LINK_OK=0
[ ! -e "${HOME}/.claude/scripts/builder-loop-stop.sh" ] && LINK_OK=0  # broken link
if [ "$HOOK_REG_OK" -eq 0 ] || [ "$LINK_OK" -eq 0 ]; then
  cat >&2 <<HOOK_WARN
⚠️  V2.5 自检：builder-loop hook 配置异常，stop hook 可能不会触发！
   ① settings.json Stop hook 注册：$([ "$HOOK_REG_OK" -eq 1 ] && echo "✅ ok" || echo "❌ missing")
   ② ~/.claude/scripts/builder-loop-stop.sh 软链：$([ "$LINK_OK" -eq 1 ] && echo "✅ ok" || echo "❌ broken/missing")
   修复：cd <cc-builder-loop 仓> && bash install.sh
   排查：bash ~/.claude/skills/builder-loop/scripts/diagnose-stop-hook.sh
HOOK_WARN
fi
```

## 文件地图

### 改动文件
| 路径 | 改动点 | 估行数 |
|------|--------|--------|
| `scripts/builder-loop-stop.sh` | 顶部加 `debug_log()` 函数 + 10 处 phase 插桩 | +60 |
| `skills/builder-loop/scripts/setup-builder-loop.sh` | 末尾加 hook 注册 + 软链自检（cwd 警告段之后） | +30 |
| `CLAUDE.md` | §5 加 V2.5 段；§7.1 路径修正 `/tmp/...log` → `<P>/.claude/builder-loop/stop-hook-debug.log` | +20 |
| `.claude/improvements.md` | c1 / c5 末尾加注「V2.5 阶段 1 可观测性已落地，阶段 2 触发链路修复待数据」 | +4 |
| `.claude/loop.yml` | 加 stage `v25_stop_hook_observability` | +1 |
| `skills/builder-loop/README.md` | fixture 表格加 V2.5 fixture 行 | +1 |

### 新增文件
| 路径 | 用途 |
|------|------|
| `skills/builder-loop/scripts/diagnose-stop-hook.sh` | 一键排查脚本（dry-run，6 段输出，支持 `--json`） |
| `skills/builder-loop/fixtures/e2e/test-stop-hook-debug-log.sh` | E2E（4 case，详见 tester 视图）|

## 执行任务列表

按依赖顺序执行：

1. **建 worktree**：standard `bash setup-builder-loop.sh "..."`，cd worktree
2. **改 `scripts/builder-loop-stop.sh`**：顶部 `set -euo pipefail` 后加 `debug_log()` 函数；按方案设计第 1 节 phase 表插 10 个调用点；保证 IO 失败 / flock 失败 / locate miss 路径都能写一行
3. **新增 `skills/builder-loop/scripts/diagnose-stop-hook.sh`**：实现 6 段输出 + `--json` flag，dry-run 严格只读
4. **改 `skills/builder-loop/scripts/setup-builder-loop.sh`**：末尾插 hook 注册 + 软链自检（cwd 警告段之后）
5. **改 `CLAUDE.md`**：§7.1 路径修正；§5 加 V2.5 段（≤25 行，含背景 / 三件套 / forensic 价值 / 完全向后兼容）
6. **改 `.claude/improvements.md`**：c1 / c5 末尾加「V2.5 阶段 1 已落地，阶段 2 待数据」一行
7. **改 `skills/builder-loop/README.md`**：补 V2.5 fixture 表格条目
8. **改 `.claude/loop.yml`**：加 `v25_stop_hook_observability` stage（60s timeout）
9. **新增 `skills/builder-loop/fixtures/e2e/test-stop-hook-debug-log.sh`**：4 case（见 tester 视图）
10. **本机自验证**：跑 fixture + 手动喂 stop hook 输入 + 跑 diagnose-stop-hook.sh + 故意删 settings.json 注册条目验证 setup 自检
11. **跑 builder loop**：触发 V2.5 stage 自闭环回归（meta：本期改动用 builder-loop 自验证）

### 退路
- 步骤 2 引入 stop hook 回归 → revert 该脚本，loop 仍按 V2.4 行为工作
- 步骤 3 diagnose 脚本运行失败 → 删该脚本，loop 主流程不影响
- 步骤 4 setup 自检报警过于敏感 → 阈值改宽 / 整段去掉

## 风险 & 应对

### R1：debug log 写入 IO 失败阻断 hook 主流程
- **场景**：磁盘满 / 权限错 / NFS 挂载点失联
- **应对**：所有 IO 写入末尾接 `|| true`，`mkdir -p ... || return 0`；fixture 加专门 case 模拟磁盘满（chmod 000 目录）→ hook 仍能正常 exit

### R2：debug log 高频 stop 项目增长太快
- **场景**：单 session 跑几小时几百次 stop hook → 几 MB log
- **应对**：默认 1 MB rotate + 保留 5 个 = 最多 5 MB；`BUILDER_LOOP_DEBUG_LOG_MAX_BYTES` env 可调

### R3：rotate 时机 race（多 hook 并发触发同时跑 rotate）
- **场景**：CC 并发 stop + flock 早退路径多 hook 同时进 rotate 检查
- **应对**：rotate 用 `mv` 原子（POSIX 保证），同名 dest mv 是原子替换；多并发 mv 最坏情况是某个 `.1` 被覆盖，老 log 数据丢一份不致命

### R4：CLAUDE.md §7.1 改路径让历史 session 排查文档对不上
- **场景**：用户从老笔记 / 文档复制 `tail /tmp/builder-loop-stop-debug.log` → 文件不存在
- **应对**：CLAUDE.md §7.1 显式标 V2.5 起改路径 + 留一行「老路径 `/tmp/builder-loop-stop-debug.log` 在 V2.4 及之前从未实际写入，是文档承诺与实现脱节，本次修正」

### R5：diagnose 脚本误报（hook 实际 ok 但脚本判 missing）
- **场景**：settings.json 用 jq 缩进 / 字段顺序不同导致 python3 解析判错
- **应对**：解析逻辑只看 `hooks.Stop[*].hooks[*].command` 末段是否含 `builder-loop-stop.sh`，不依赖具体格式；fixture 测多种缩进 / 顺序

### R6：本期不改触发逻辑导致 c5 自激空转还在持续犯
- **场景**：用户装了 V2.5 后仍遇到 generator session 那种自激空转，但只能事后看 debug log
- **应对**：本期是阶段 1 ack；CLAUDE.md V2.5 段明示「c5 触发链路修复推后，本期只让根因可见」；如果用户当下被 c5 困扰可以临时手动 commit 止血（已有解法）

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标
验证 V2.5 三件套（debug log / diagnose / setup 自检）的功能正确性 + IO 失败容忍 + 默认 rotate 行为。

### 接口签名（黑盒视角）

#### `builder-loop-stop.sh` debug log
- **触发**：每次 stop hook 触发都自动写
- **输出**：`<PROJECT_ROOT>/.claude/builder-loop/stop-hook-debug.log` 追加 NDJSON 行
- **格式**：每行 `{"ts":..., "session":..., "cwd":..., "slug":..., "phase":..., "details":{...}}`
- **写入失败**：静默继续不阻断 hook 主流程；exit code 与 V2.4 一致
- **rotate**：log 大小 > 1 MB 时下次写入前 rotate（`.1 → .2 → ... → .5`）

#### `diagnose-stop-hook.sh`
- **输入**：可选 `[project_root]` 默认 cwd 向上找 loop.yml；`--json` flag 切 JSON 输出
- **输出 stdout**：6 段诊断（人读）或单 JSON 对象（机读）
- **退出码**：0 全 ok / 1 至少一段 warn / 2 至少一段 fail
- **dry-run 严格**：执行前后 `<P>/` 任何文件不变（mtime / size / content）

#### `setup-builder-loop.sh` 自检
- **触发**：每次 setup 末尾（cwd 警告段之后）
- **输出 stderr**：检测到 hook 注册 missing / 软链 broken 时 `⚠️ V2.5 自检：...` + 修复指引；ok 时静默
- **不阻断 setup**：警告归警告，state file 仍正常创建

### 关键测试场景

#### Case A1：debug log 基础写入 + phase 顺序
**前置**：临时 git repo + `loop.yml`（worktree.enabled=false 走 bare 路径快） + 手动构造 active state

**操作**：`printf '{"cwd":"<TMP>"}' | bash scripts/builder-loop-stop.sh`

**断言**：
- `<TMP>/.claude/builder-loop/stop-hook-debug.log` 存在
- 含 phase=`entry`、`locate_result`、`flock_acquire`、`pass_cmd_start`、`pass_cmd_result`、`exit` 至少 6 行
- 每行是合法 JSON（python3 可解析）
- `entry` 在所有其他 phase 之前
- `exit` 在最后一行

#### Case A2：debug log 写入失败容忍（磁盘 / 权限）
**前置**：同 A1 + `chmod 000 <TMP>/.claude/builder-loop/`（debug log 目录无写权限）

**操作**：跑 stop hook（同 A1）

**断言**：
- hook exit code 与 chmod 之前同样 case 一致（不因 IO 错改变 exit code）
- stop hook stderr 没出现 `Permission denied` 等错误（IO 失败被静默）
- PASS_CMD 仍正常跑（log_path 由 PASS_CMD 自己管，不受影响）
- chmod 还原后下次跑能写入新行

#### Case A3：rotate 触发（log 超 1 MB）
**前置**：临时仓 + 手动 echo 1.5 MB 内容到 stop-hook-debug.log；设 `BUILDER_LOOP_DEBUG_LOG_MAX_BYTES=1048576` env

**操作**：跑一次 stop hook

**断言**：
- `stop-hook-debug.log.1` 存在 + 大小 ≈ 1.5 MB（旧内容）
- `stop-hook-debug.log` 是新文件 + 内容只含本轮 phase（< 5 KB）
- 重复 5 次让 `.5` 出现，第 6 次让 `.6` 不出现（保留 5 个）

#### Case A4：diagnose-stop-hook.sh 6 段输出 + 严格 dry-run
**前置**：临时仓 + 装好的 builder-loop 环境（settings.json + 软链 + state + lock 文件）

**操作**：
1. `find <TMP> -type f -newer ... > before.txt`（拍快照）
2. `bash diagnose-stop-hook.sh <TMP>` > diag.out
3. `find <TMP> -type f -newer ... > after.txt`

**断言**：
- diag.out 含 `[1/6]` 到 `[6/6]` 6 段
- exit code 0（全 ok 场景）
- before.txt = after.txt（dry-run 严格，无任何文件变化）
- 删 settings.json Stop 注册 → 重跑 → diag.out 含 `[1/6] ... ❌ missing` + exit 2

#### Case A5：setup 自检识别 hook 注册缺失
**前置**：临时仓 + `~/.claude/settings.json` 不含 builder-loop-stop.sh 注册

**操作**：跑 `bash setup-builder-loop.sh "<task>"` 2>err.log

**断言**：
- err.log 含 `⚠️  V2.5 自检`
- err.log 含 `① settings.json Stop hook 注册：❌ missing`
- err.log 含 `bash install.sh` 修复指引
- err.log 含 `bash ~/.claude/skills/builder-loop/scripts/diagnose-stop-hook.sh`
- setup exit code 0（自检不阻断）+ state 文件已正常创建

### 测试深度
中等（5 case，比预算多 1 case 因为 IO 失败 + rotate 都需要单独 case）。覆盖：基础写入 + IO 失败容忍 + rotate + diagnose 严格 dry-run + setup 自检。

### Fixture 风格约定
1. 严格参照 `test-stop-hook-cursor.sh` / `test-locate-state-strategy5.sh` 模板（assert 函数、PASS/FAIL 计数、`mktemp -d` + trap cleanup、git config `e2e@test.local`）
2. 每个 case 末尾打印 `--- Step N done ---`
3. 末尾汇总：`echo "PASS: $PASS  FAIL: $FAIL"` + `[ $FAIL -eq 0 ]` 决定 exit code
4. 临时 git commit message 必须 `chore(test): [cr_id_skip] Xxx` 格式
5. 不依赖外部网络 / API（PASS_CMD 用 `cmd: "true"` 让快速 PASS）
6. **`~/.claude/settings.json` 不能动**（A5 case 模拟用 fake HOME 临时目录或 chmod 暂时屏蔽）
7. Case A2 `chmod 000` 后用 trap 保证测试结束恢复（`chmod -R 755`）

### 边界条件需验证
- locate-state.sh miss + bootstrap 跑 → debug log 应记 phase=`bootstrap_check` + `decision: trigger`
- 跨 session 守门触发 exit 0 → debug log 应记 phase=`exit` + `reason: existing_loop_worktrees`
- flock 抢锁失败 exit 0 → debug log 应记 phase=`flock_acquire` + `acquired:false` + phase=`exit` + `reason: lock_held_by_other`
- bare loop slug=__main__ 触发 → debug log slug 字段 = `"__main__"`
- V1.x 老 state 缺 `main_repo_path` → debug log `locate_result.run_cwd` 仍正确

<!-- /role -->
