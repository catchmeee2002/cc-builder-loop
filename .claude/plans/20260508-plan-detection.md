# c2: install.sh / diagnose 加 max/copilot 方案识别

> 来源：`.claude/improvements.md` 2026-05-08 高优条目。A 批 session 暴露：install.sh 无脑注册 6 条 hook，diagnose 无脑要求齐 6 条；max 方案下 `tester-write-guard.sh` 不该被注册也不该被检查。

<!-- role:shared -->

## 背景 & 目标

**背景**：A 批 PASS_CMD 卡在 `v25_stop_hook_observability` fixture 的 A4 段，根因之一就是这条——install.sh 把 `tester-write-guard.sh` 当所有人必装的 hook，但 max 方案下 CC 用 OAuth 直连，编辑/写入拦截不需要走代理这层 hook，settings.json 缺它是**正确状态**。

**目标**：让 install.sh / diagnose-stop-hook.sh 都根据 `ANTHROPIC_BASE_URL` 判方案 ——

- BASE_URL 含 `localhost` / `127.0.0.1` → copilot 方案（注册并要求 6 条）
- 其他（含空、`api.anthropic.com` 等） → max 方案（注册并要求 5 条，跳过 tester-write-guard.sh）

跑时显式打印「检测方案=X（BASE_URL=...）」让用户可见。

**预估改动级别**：L2（改两个脚本顶部 + 注册表加方案过滤字段 + 新增 1 个 e2e fixture，不动 hook 行为不动状态机）。Builder 根据实际 diff 确认或修正。

## 验收标准

- 真实环境（用户当前 max env）跑 `bash install.sh`：输出含「检测方案=max」，settings.json 仍只 5 条 hook（不主动加 tester-write-guard）。
- 真实环境跑 `bash diagnose-stop-hook.sh /tmp/sometestrepo`：[1/6] verdict=ok（5 条都在），不再报「少 tester-write-guard」。
- 模拟 copilot env（`ANTHROPIC_BASE_URL=http://127.0.0.1:4141`）跑 install/diagnose：行为跟之前完全一样（注册并要求 6 条）。
- 新 fixture `test-plan-detection.sh` 通过。
- V2.5 fixture `test-stop-hook-debug-log.sh` 在 max env 下自然 PASS（c2 修完后，diagnose [1/6] verdict=ok → fixture A4 段 set -e 不触发）。

<!-- /role -->

<!-- role:builder -->

## 约束 & 边界

**不能碰**：

- install.sh 5.1-5.6 备份/自检/原子替换段（已加固，不动）。
- registrations 表里 6 条 hook 的 type / matcher / cmd_name 现值。
- diagnose-stop-hook.sh [2/6]~[6/6] 段（只改 [1/6]）。
- ANTHROPIC_BASE_URL 这个 env 的语义（只读不改不伪造）。
- A 批 worktree 改动（已 abandon 保留在 branch loop/1778210210-...，本期 c2 跟它解耦）。

**必须兼容**：

- copilot 用户当前行为（默认注册并要求 6 条）。
- 老 ~/.claude/settings.json（无论现在含 5 条还是 6 条都能正确升级）。
- e2e fixture 跑环境（CI 一般不设 BASE_URL，会被判 max → 这是合理的，CI 跑 fixture 时模拟两种 env 切换）。

**性能**：detect_plan 是 1 次 bash case 语句，开销可忽略。

## 技术选型

### 方案识别机制

| 选项 | 选/弃 |
|------|------|
| **运行时读 `ANTHROPIC_BASE_URL`，case 匹配 localhost/127.0.0.1** | ✅ 选定 — 最简、可见、跨 shell 一致（用户 dotfiles 写死 export，shell 启动一致 source） |
| 单文件 `~/.claude/builder-loop-plan` 持久化缓存 | ❌ 弃 — 多一份漂移源 |
| ENV `BUILDER_LOOP_PLAN=max` 用户手设 | ❌ 弃 — 不友好 |
| `--plan` flag 命令行覆盖 | ❌ 弃（确认不加，需求场景下不必要） |

### detect_plan 函数是否抽 helper

| 选项 | 选/弃 |
|------|------|
| **install.sh / diagnose 各内联一份（3 行 case）** | ✅ 选定 — install.sh 装到 ~/.claude/scripts/ 是入口工具，依赖 skills/builder-loop/scripts/ helper 会循环 |
| 抽 `skills/builder-loop/scripts/detect-plan.sh` 让二者 source | ❌ 弃 — 依赖关系颠倒 |

### registrations 表过滤逻辑

把元组从 3 字段（type, cmd, matcher）扩到 4 字段，加 `plan_filter`：

```python
registrations = [
    ("Stop",           "builder-loop-stop.sh",      None,                    ""),
    ("SubagentStart",  "tester-lock-write.sh",      "tester",                ""),
    ("SubagentStop",   "tester-lock-clear.sh",      "tester",                ""),
    ("PreToolUse",     "tester-lock-check.sh",      "Read|Grep|Glob",        ""),
    ("PreToolUse",     "tester-write-guard.sh",     "Write|Edit|MultiEdit",  "copilot"),
    ("PreToolUse",     "reviewer-timing-check.sh",  "Agent",                 ""),
]
```

`""` = 通用，所有方案都装；`"copilot"` = 仅 copilot 方案装。

主循环加分支：

```python
for hook_type, cmd_name, matcher, plan_filter in registrations:
    if plan_filter and plan_filter != PLAN:
        skipped += 1
        continue
    # 现有 has_entry 逻辑...
```

输出末尾加 `skipped` 计数。

> 注：A 批 worktree 里 has_entry 已改成 find_entry_status 三态。本期 c2 在主仓 main 上做（不依赖 A 批），主循环仍是 has_entry 二态写法。A 批 cherry-pick 时手动合并这两块（c2 加 plan_filter 字段；A 批改 has_entry → find_entry_status）。

### diagnose [1/6] 期望列表过滤

```bash
PLAN="$(detect_plan)"
# expected 列表内联在 [1/6] 的 python3 段，按 PLAN 过滤
```

输出顶部加一行 `检测方案=$PLAN（ANTHROPIC_BASE_URL=$BASE_URL_DISPLAY）`。

### e2e fixture 设计

`skills/builder-loop/fixtures/e2e/test-plan-detection.sh`：

```
1. mktemp HOME + 写 baseline settings.json（空 hooks）
2. unset ANTHROPIC_BASE_URL → bash install.sh → 断言 5 条 hook + 输出含「检测方案=max」
3. uninstall → 还原
4. ANTHROPIC_BASE_URL=http://127.0.0.1:4141 → bash install.sh → 断言 6 条 hook + 输出含「检测方案=copilot」
5. 各跑 diagnose 一次断言 [1/6] verdict=ok
6. 清理
```

## 方案设计

见技术选型每段，已写明实现细节。

核心两段代码（install.sh 顶部 + diagnose 顶部）：

```bash
detect_plan() {
  local url="${ANTHROPIC_BASE_URL:-}"
  case "$url" in
    *localhost*|*127.0.0.1*) echo "copilot" ;;
    *)                       echo "max" ;;
  esac
}
PLAN="$(detect_plan)"
echo "✓ 检测方案=${PLAN}（ANTHROPIC_BASE_URL='${ANTHROPIC_BASE_URL:-(unset)}'）"
```

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| 老 ~/.claude/settings.json 已含 tester-write-guard（之前手装），新 install.sh 跑 max 方案不会主动删 | 本期 c2 仅「max 方案下不主动加」；删除联动留给 A 批 cherry-pick 后（A 批 has_entry 三态返回支持 stale 分支删旧）。本期主循环遇到 plan_filter 不命中就跳过，不动旧条目。如果用户当前 settings.json 真有 stale tester-write-guard，install.sh 会跳过它（不重复加）+ diagnose 仍会按 max 期望列表 [1/6] verdict=ok（settings.json 多了一条无害） |
| ANTHROPIC_BASE_URL 设了但是 hostname.lan 这种自定义主机名（用户跑 copilot 但不用 localhost） | 算 max → install 不注册 tester-write-guard → 用户的 copilot 代理拦不住 Write/Edit。但这是边缘场景；通过 install.sh 显式打印 BASE_URL 让用户当场看出，可手动 export 为 127.0.0.1 转发再重跑 |
| diagnose 修了 [1/6] 之后 V2.5 fixture 仍因 set -e bug 在 verdict=ok 但其他段 verdict=warn 时假阳性 | 看完 diagnose 整体退出码逻辑：max env 下 5 条都装齐 → [1/6] ok；其他 5 段也 ok（[5/6] 可能 warn 因为 trace 空，但 warn 退出码=1 也会触发 set -e）。**等等，确实有这个风险**——A4 段 fixture set -e 触发条件不是 verdict=fail，而是 diagnose 退出码 != 0。需在本期顺手验证：max env 下 fixture 跑 diagnose 退出码是几。如果 diagnose 退出码 != 0（即使 verdict=ok），fixture 仍会挂。**应对**：跑通 V2.5 fixture 是验收标准之一，c2 改完手动验证；如果发现 diagnose 退出码非 0 即使 verdict=ok，要么改 diagnose 退出码语义（warn 算成功？），要么把 c1（fixture 容错）一起做了。预判 c1 顺带做更稳 |
| install.sh 改坏 settings.json | 5.1-5.6 段的备份 / 写前自检 / 写后自检 / 原子替换 + .bak 都还在，跑挂可手动 cp 还原 |

**退路**：install.sh 跑炸 → `cp ~/.claude/settings.json.bak.<ts> ~/.claude/settings.json` 还原；diagnose 跑炸 → 它本来就是 dry-run 工具，最差用户看不到诊断输出，不影响主流程。

## 文件地图

存量改动：

- `install.sh` 顶部（13 行后，前置检查段前）→ 加 detect_plan 函数 + PLAN 变量 + echo「检测方案=...」
- `install.sh` L103-110 registrations 表 → 元组加第 4 字段 plan_filter（仅 tester-write-guard 一行 = `"copilot"`，其他 5 条 = `""`）
- `install.sh` L112-120 主循环 → for 解包加 plan_filter，前置 if 判断跳过；skipped 计数 + print 输出加 skipped 字段
- `skills/builder-loop/scripts/diagnose-stop-hook.sh` 顶部 → 加 detect_plan + PLAN + echo「检测方案=...」
- `skills/builder-loop/scripts/diagnose-stop-hook.sh` [1/6] 段 → expected hook 列表读 PLAN env 过滤

新增：

- `skills/builder-loop/fixtures/e2e/test-plan-detection.sh`

不动：

- `uninstall.sh`（A 批已修，max 方案下删不存在 hook 是 no-op）
- 现有 e2e fixture 内容（V2.5 fixture 修完 c2 后自然 PASS）
- registrations 表 6 条 hook 的 type / matcher / cmd_name 现值
- A 批 worktree branch（独立任务，本期不动）

## 执行任务列表

按顺序：

1. **install.sh 加 detect_plan + PLAN 变量 + echo**（顶部 8 行）
2. **install.sh registrations 表加 plan_filter 字段**（5 行 `""` + 1 行 `"copilot"`）
3. **install.sh 主循环加 plan_filter 过滤 + skipped 计数 + print 输出**（5 行）
4. **diagnose-stop-hook.sh 顶部加 detect_plan + PLAN + echo**（同 1 的 8 行）
5. **diagnose-stop-hook.sh [1/6] 段 expected 列表加 plan 过滤**（python 段内 list comprehension）
6. **新建 `test-plan-detection.sh`**（mktemp HOME + 模拟 max/copilot env 各跑 install + diagnose 一遍 + 断言）
7. **跑新 fixture 验证**
8. **跑相邻 V2.5 fixture `test-stop-hook-debug-log.sh` 验证**（关键 — 这是 c2 解决的根本场景）
9. **跑真实 `bash install.sh`**（max env）应输出「检测方案=max」+ 不主动加 tester-write-guard + 现有 5 条 hook 仍 match

<!-- /role -->

## 测试计划

**测试目标**：max / copilot 两种 env 下 install.sh / diagnose-stop-hook.sh 行为正确。

**关键场景**：

1. **max install**：unset BASE_URL → install → 5 条 hook + 输出含「检测方案=max」+ skipped=1
2. **copilot install**：BASE_URL=localhost:4141 → install → 6 条 hook + 输出含「检测方案=copilot」+ skipped=0
3. **max diagnose**：[1/6] verdict=ok（5 条期望 + 实际 5 条）
4. **copilot diagnose**：[1/6] verdict=ok（6 条期望 + 实际 6 条）
5. **V2.5 fixture 联动**：在主仓真实 ~/.claude/ 跑 V2.5 fixture，max env 下应 PASS

**测试深度**：快速。新 fixture 1 个 .sh 黑盒 e2e。
