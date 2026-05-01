# Stop Hook 兜底激活游标防空转

<!-- role:shared -->

## 背景 & 目标

**背景**：session `3d62eb57-dbc9` 里复现了一个稳定 bug —— 用户推了一个 commit（实验制品 `e69092a`），随后 30 分钟内每次对话都触发一次 NOOP loop 兜底激活，总共空转 16 次，每次还会抛出 reviewer spawn 提示，严重污染对话流水。

**根因**（`scripts/builder-loop-stop.sh:104`）：
```bash
HAS_RECENT_COMMIT="$(git log --since='30 minutes ago' --oneline)"
# line 106: 只要 HAS_DIFF 或 HAS_RECENT_COMMIT 非空就触发兜底
if [ -z "$HAS_DIFF" ] && [ -z "$HAS_RECENT_COMMIT" ]; then exit 0; fi
```
推完 commit 后 HAS_DIFF 为空但 HAS_RECENT_COMMIT 持续命中 30 分钟，bootstrap 反复触发。NOOP loop 结束后 Stop hook 又被 CC 调用，陷入自激循环。

**目标**：用「已处理 HEAD 游标」切掉自激路径，同一个 commit 只触发一次 bootstrap。

**成功标准**：
- 推任意类型 commit 后，**第一次** Stop 仍正常 bootstrap（可能 NOOP，可能真有活干）
- **第二次起** Stop 在 HEAD 未前进的前提下**静默放行**（exit 0，无 stderr 噪声）
- 新 commit 出现后游标失效，恢复 bootstrap 能力

## 预估改动级别

**L2**（实现改动）。修改 1 个 shell 脚本 + 新增 1 个 E2E 测试脚本 + 一行文档。不新增对外接口，不涉及跨模块耦合。

## 约束 & 边界

**不能碰**：
- `skills/builder-loop/scripts/setup-builder-loop.sh` 的 state 写入契约
- `locate-state.sh` 的 state 定位逻辑（本次只改 bootstrap 分支）
- `merge-worktree-back.sh` 内部的 rm state 行为

**必须兼容**：
- 既有 loop.yml 格式、state schema 不变
- 无游标文件时降级为旧行为（避免已部署环境升级炸）
- 多 slug 并行场景（游标是项目级共享，所有 slug 对同一主干 HEAD 判断）

<!-- /role -->

<!-- role:builder -->

## 技术选型

| 方案 | 描述 | 取舍 |
|------|------|------|
| **A（采纳）** | `HAS_RECENT_COMMIT` 保留 + 叠加游标：条件改为 `(HAS_DIFF 或 HAS_RECENT_COMMIT) AND HEAD != 游标` | 最小改动，保留原「断线重接」语义（新 commit 出现时仍会 bootstrap） |
| B | 去掉 `HAS_RECENT_COMMIT`，只看 HAS_DIFF + 游标 | 更激进但可能丢掉「session 刚 crash 且仅剩 commit」的合法恢复 |
| C | 复用 `loop-trace.jsonl` 读最后一条 PASS 的 HEAD | 不新增文件，但每次都要解析 JSONL 成本高 |

**游标路径**：`.claude/builder-loop/last_processed_head`（项目级单文件，存完整 git SHA）。`.claude/builder-loop/` 目录已由 V1.8 引入，无需额外 mkdir 逻辑验证。

## 方案设计

### 读游标（bootstrap guard 入口）

位置：`scripts/builder-loop-stop.sh` 第 102~108 行附近，在 `HAS_DIFF` / `HAS_RECENT_COMMIT` 检查之后、"推断 task_description" 之前，追加游标检查：

```bash
# 已处理 HEAD 游标检查：同一个 HEAD 不重复触发 bootstrap
CURSOR_FILE="${PROJECT_ROOT}/.claude/builder-loop/last_processed_head"
CURRENT_HEAD="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
if [ -f "$CURSOR_FILE" ] && [ -n "$CURRENT_HEAD" ]; then
  LAST_HEAD="$(cat "$CURSOR_FILE" 2>/dev/null | head -1 | tr -d '[:space:]')"
  if [ "$CURRENT_HEAD" = "$LAST_HEAD" ]; then
    # 当前 HEAD 已经被处理过，且没有未提交改动 → 跳过
    if [ -z "$HAS_DIFF" ]; then
      exit 0
    fi
  fi
fi
```

关键点：
- 游标文件缺失 → 走原逻辑（兼容首次升级）
- `git rev-parse HEAD` 失败（极罕见）→ 走原逻辑
- **有 HAS_DIFF 时游标不生效**：用户本地仍在改代码，必须正常 bootstrap

### 写游标（清理 state 时）

在 `builder-loop-stop.sh` 所有会「结束本轮 loop」的出口处写入当前 HEAD：

| 出口位置（按语义锚点定位，行号仅参考） | 现有行为 | 新增 |
|---------|---------|------|
| PASS 分支 MERGED\|NOOP（锚点：`rm -f "$STATE_FILE"` 之前，约 L220） | `rm -f "$STATE_FILE"` + 写 reviewer-params.json | 之前先写游标 |
| merge 未知结果异常分支（锚点：`rm -f "$STATE_FILE"` 之前，约 L325） | `rm -f "$STATE_FILE"` | 之前先写游标 |
| EARLY_STOP 分支（锚点：`archive_to_legacy "$STATE_FILE" "early_stop_${REASON}"` 之前，约 L346） | `archive_to_legacy` 归档 state + exit 2（V1.8.1 改造后） | 之前先写游标 |
| 僵尸 state 归档分支（L161）| 被动归档 `active != true` 的历史残留 | **不写游标**（避免把旧 HEAD 写进游标误伤后续合法 bootstrap） |

写入形式（封装成函数避免重复）：

```bash
write_processed_cursor() {
  local proj_root="$1"
  local head_sha
  head_sha="$(git -C "$proj_root" rev-parse HEAD 2>/dev/null || echo "")"
  if [ -n "$head_sha" ]; then
    mkdir -p "${proj_root}/.claude/builder-loop" 2>/dev/null || true
    echo "$head_sha" > "${proj_root}/.claude/builder-loop/last_processed_head" 2>/dev/null || true
  fi
}
```

三处出口调用 `write_processed_cursor "$PROJECT_ROOT"`。

### 降级保证

- 游标文件写失败（磁盘只读、权限问题）→ 静默忽略，下次仍按旧逻辑走（bug 不会恶化）
- 游标内容损坏（非 SHA 格式）→ `!=` 判断自然为 true，降级为旧逻辑
- 用户手动 `git reset --hard` 回到老 commit 使 HEAD 倒退 → 游标 `!=` 当前 HEAD，仍会触发 bootstrap（可接受的代价，不做 ancestor 检查增加复杂度）

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| 游标写时机错了（提前或过晚）→ 真该 bootstrap 的场景被跳 | 只在"本轮 loop 正式结束"的 3 个出口写，不在 bootstrap 入口写 |
| 多个 session 并发触发 Stop hook 同时写游标 | echo 单行原子写入，最坏情况后者覆盖前者，不影响正确性 |
| 未来 V1.9+ 引入跨分支 loop，游标单文件不够 | 本版本标记"单文件"为 V1.8.1 简化实现，后续若需可升级为 per-branch |

## 文件地图

**存量文件**（改动点）：

| 文件 | 改动 |
|------|------|
| `scripts/builder-loop-stop.sh` | ① L102~108 附近插入游标读检查；② L202 前插 `write_processed_cursor`；③ L307 前插 `write_processed_cursor`；④ L325~332 的 early_stop 分支 python heredoc 之后插 `write_processed_cursor`；⑤ 文件顶部添加 `write_processed_cursor` 函数定义（放在 "解析 stdin" 之前） |
| `CLAUDE.md` | "已交付能力" 段加 V1.8.1 一句话 |
| `skills/builder-loop/README.md` | 若已有版本历史小节则追加 V1.8.1 条目；否则跳过 |

**新增文件**：

| 文件 | 内容 |
|------|------|
| `skills/builder-loop/fixtures/e2e/test-stop-hook-cursor.sh` | E2E：构造「推 commit 后反复 Stop 不应再触发 bootstrap」场景，参考 `test-new-repo-loop.sh` 的框架 |

**无存量文件**：本版本所有功能都是对 stop hook 的增量补丁，不涉及新模块。

## 执行任务列表

1. **改 `scripts/builder-loop-stop.sh`**：
   - 在文件顶部 `set -euo pipefail` 之后、`SKILL_DIR=` 之前新增 `write_processed_cursor` 函数
   - L102~108 之间插入游标读检查逻辑
   - L202（PASS 分支 `rm -f "$STATE_FILE"` 之前）调 `write_processed_cursor "$PROJECT_ROOT"`
   - L307（异常 merge 结果 `rm -f "$STATE_FILE"` 之前）同上
   - L332 附近（early_stop，`open(sf, 'w').write(text)` 的 python heredoc 结束之后、`write_trace` 之前）同上
2. **新增 `skills/builder-loop/fixtures/e2e/test-stop-hook-cursor.sh`**：按下方测试计划构造场景，参考 `test-new-repo-loop.sh` 的 `assert / PASS / FAIL` 框架
3. **更新 `CLAUDE.md`**：在「已交付能力」章节 V1.8.1 之后加一行：
   > - **V1.8.2**: Stop hook 兜底激活游标（`.claude/builder-loop/last_processed_head`，同一 HEAD 不重复 bootstrap，消除推 commit 后 30 分钟内反复 NOOP 空转）
4. **本地跑通 E2E**：`bash skills/builder-loop/fixtures/e2e/test-stop-hook-cursor.sh` 退出码 0

## 验收标准

- [ ] 新 E2E 脚本本地运行退出码 0
- [ ] 手动验证：在一个接入 loop.yml 的项目里，推一个 NOOP 性质的 commit，连发 3 条纯对话消息，观察 stderr 仅第一条出现 bootstrap 提示，后续两条静默
- [ ] 原有 E2E 全部仍通过：`test-new-repo-loop.sh` / `test-isolation.sh` / `test-parallel-loop.sh` / `test-conflict.sh` 退出码 0
- [ ] `git diff` 只涉及 `scripts/builder-loop-stop.sh` + 新增 E2E 脚本 + CLAUDE.md，无其他文件误改

<!-- /role -->

<!-- role:tester -->

## 测试计划

**测试目标**：验证「同一 HEAD 不重复 bootstrap」这个行为契约，同时保证「HEAD 前进后仍能正常 bootstrap」和「有未提交改动时游标不阻塞」。

**测试深度**：快速（1 个 E2E 脚本覆盖 4 个关键场景即可，不需要 unit 级拆分）。

### 关键测试场景

场景清单（每条对应一个 assert 块）：

1. **首次 bootstrap**：
   - 准备：空仓库，`loop-init.sh` 初始化，推一个真实 commit（`touch foo.txt && git add -A && git commit -m ...`）
   - 动作：构造 Stop hook stdin JSON（`{"cwd": "$PROJ"}`），调 `builder-loop-stop.sh`
   - 断言：游标文件 `.claude/builder-loop/last_processed_head` 被创建，内容等于 `git rev-parse HEAD`

2. **相同 HEAD 不再触发**：
   - 前置：场景 1 完成
   - 动作：无任何代码改动，再次调 `builder-loop-stop.sh`
   - 断言：
     - 退出码 = 0（静默放行，不是 exit 2）
     - stderr **不包含**「兜底激活」字样
     - 没有新的 state 文件出现

3. **HEAD 前进后恢复触发**：
   - 前置：场景 2 完成（游标存在且等于旧 HEAD）
   - 动作：新增一个 commit（`echo x > bar.txt && git add -A && git commit -m ...`），再调 `builder-loop-stop.sh`
   - 断言：
     - stderr 包含「兜底激活」
     - 游标文件内容已更新为新 HEAD

4. **未提交改动时游标不生效**：
   - 前置：场景 3 完成
   - 动作：不 commit，直接修改一个文件制造 `HAS_DIFF`，再调 `builder-loop-stop.sh`
   - 断言：stderr 包含「兜底激活」（HAS_DIFF 优先级高于游标跳过）

### 边界条件思路

- **游标文件不存在**：首次升级场景，必须走原逻辑，不能因游标缺失就跳过 bootstrap
- **游标内容是空行 / 多余空白 / 非 SHA 字符串**：应当等价于"游标不匹配"，走正常 bootstrap（不崩溃）
- **仓库无任何 commit**（`git rev-parse HEAD` 失败）：不应因为 HEAD 读不到就误跳，降级为旧逻辑

### 验收口径

E2E 脚本退出码 0 即视为通过。脚本末尾打印 `✅ PASS=<n> FAIL=<m>` 汇总，`FAIL != 0` 时退出码 1。

<!-- /role -->
