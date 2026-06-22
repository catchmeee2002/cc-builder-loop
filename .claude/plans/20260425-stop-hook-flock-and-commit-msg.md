# Stop Hook flock 互斥 + auto-commit message 语义化（V1.8.3）

<!-- role:shared -->

## 背景 & 目标

两件 **loop 工具本身的健壮性补强**，都有明确的根因。

### P0 — Stop hook 并发 race（修 grep bug）

本次 session（V1.8.2 loop）末尾复现一个日志异常：
```
[builder-loop] 🔄 iter 1: 正在跑 PASS_CMD...
grep: /mnt/hongyu.liao_docker/cc-builder-loop/.claude/builder-loop/state/1777047345-stop-hook.yml: No such file or directory
```

**根因**：CC 可能并发触发 Stop hook。
1. Hook A 读 state → 打印 `iter 1 正在跑 PASS_CMD` → 执行 `run-pass-cmd.sh`（**数十秒级**）
2. Hook B 并发启动 → 同样读 state → 同样进 run-pass-cmd
3. A 完成 PASS → L220 `rm -f state` → exit 2
4. B 的 run-pass-cmd 内部某处 grep state → `No such file`

这是经典 TOCTOU race，且在 V1.8.2 修游标之后依然存在（互相独立）。结果是良性的（merge 仍成功）但日志噪声。

### P1 — auto-commit message 语义化

V1.8.2 loop PASS 的 merge commit `3419d32` 的 message 是 `chore(loop): [cr_id_skip] Auto-commit iter 0`，丢失本次改动语义。根因：`merge-worktree-back.sh:138` 把 auto-commit message 硬编码为 `Auto-commit iter ${ITER_NUM}`。

状态文件里明明有 `task_description` 字段（setup 时 builder 传入），可以用来构造信息化的 message。

### 成功标准

- Stop hook 并发触发时，第二个 hook `flock -n` 抢锁失败立即 `exit 0` 静默放行，不再踩 race
- loop PASS 后的主干 commit message 变成 `chore(loop): [cr_id_skip] Auto-commit <task_description>`，保留本次任务关键词
- 不破坏现有 V1.8 / V1.8.1 / V1.8.2 行为
- flock 场景和 message 场景都有 E2E 覆盖

## 预估改动级别

**L2**（实现改动）。改动 2 个现有 shell 脚本 + 新增 1 个 E2E 测试 + CLAUDE.md 一行。flock / yaml 解析都是既有 shell 习惯用法，无新接口。

## 约束 & 边界

**不能碰**：
- `setup-builder-loop.sh` 的 state schema（`task_description: |` block scalar 格式保留，不为了方便解析改成单行；block scalar 兼容多行与特殊字符）
- `locate-state.sh` 的 state 定位逻辑
- V1.8.2 新增的游标逻辑与 V1.8.1 僵尸归档逻辑
- 现有 E2E 脚本（不改已有测试）

**必须兼容**：
- 锁文件缺失/损坏 → 降级为"无锁"行为（不崩）
- state 里 task_description 为空 / 含特殊字符 → 降级为旧 `Auto-commit iter N` message
- commit-msg hook 校验（格式 `type(scope): [cr_id_skip] Uppercase English opener ...`）：message 以 `Auto-commit` 大写英文开头 + 后接 task_description，符合现有门禁

<!-- /role -->

<!-- role:builder -->

## 技术选型

### P0 flock 方案对比

| 方案 | 机制 | 取舍 |
|------|------|------|
| **A（采纳）** | 按 slug 粒度加 `flock -n`，抢不到 exit 0 静默放行 | 语义清晰："另一 hook 在处理本 slug，我不插手"；per-slug 保留多并行 loop 能力 |
| B | 全局 project-level 锁 | 过度 serialize，浪费多 slug 并行能力 |
| C | 所有 state 读写加 `2>/dev/null \|\| exit 0` | 治标不治本，补丁要打遍 run-pass-cmd / early-stop-check / merge-worktree-back 等多脚本 |
| D | state 原子 mv 到 `inprogress.$$` | 需要 trap cleanup，异常退出残留 |

### P1 auto-commit message 模板

格式：`chore(loop): [cr_id_skip] Auto-commit ${task_description}`

- 前缀 `Auto-commit` 保证 commit-msg 校验通过（大写英文开头）
- task_description 保留原始中文/英文描述，不截断（git log 自己会显示）
- task_description 读取用 awk 解析 YAML block scalar（见下方方案设计）
- 降级：task_description 为空时回退到 `Auto-commit iter ${ITER_NUM}`（保持旧行为）

## 方案设计

### P0：flock 加锁

**位置**：`scripts/builder-loop-stop.sh` 在 STATE_FILE / PROJECT_ROOT 定位**之后**（约 L149 附近），在进入后续"状态文件不存在/非活跃"判断之前。

**SLUG 提取**：
```bash
# 锁文件按 slug 粒度（不同 slug 可并发；同 slug 互斥）
SLUG="__main__"
if [ -n "$STATE_FILE" ]; then
  SLUG="$(basename "$STATE_FILE" .yml 2>/dev/null || echo "__main__")"
fi
```

注意：bootstrap 分支（FOUND_LOOP_ONLY=true）走 setup-builder-loop.sh --no-worktree，固定生成 `__main__` slug 的 state；所以 bootstrap 之间也天然通过 `__main__` 锁互斥。

**加锁代码**：
```bash
LOCK_FILE="${PROJECT_ROOT}/.claude/builder-loop/stop-hook-${SLUG}.lock"
mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  # 另一 hook 正在处理本 slug，静默放行
  exit 0
fi
```

FD 200 是任意大数，避免和 shell 内置 FD 冲突。脚本结束时 FD 自动关闭释放锁。

**插入时机选择**：放在 bootstrap 分支**之后**（L149 后，"state file 仍不存在 → 放行"之前），这样 bootstrap 里的 setup 也受锁保护。

### P1：auto-commit message

**位置**：`skills/builder-loop/scripts/merge-worktree-back.sh` L138。

**解析 task_description**（YAML block scalar `|` 格式）：
```bash
TASK_DESC="$(awk '/^task_description: \|/{getline; sub(/^[[:space:]]+/, ""); print; exit}' "$STATE" 2>/dev/null || echo "")"
```

**message 构造**：
```bash
if [ -n "$TASK_DESC" ]; then
  MSG="chore(loop): [cr_id_skip] Auto-commit ${TASK_DESC}"
else
  MSG="chore(loop): [cr_id_skip] Auto-commit iter ${ITER_NUM}"
fi
# 用 -F 从 stdin 读避免 shell 注入
printf '%s\n' "$MSG" | git -C "$WORKTREE_PATH" commit -F - >&2 || {
  echo "ERROR auto-commit-failed"
  exit 3
}
```

用 `-F -` 而非 `-m "$MSG"`，防止 task_description 里含反引号、$ 之类特殊字符被 shell 二次解析。

### 兼容 commit-msg hook

用户全局 hook `guard-commit-msg.sh` 要求 `type(scope): [cr_id_skip] 大写英文首字母开头`。我们构造的 message：
- `chore(loop)` ✅ type=chore scope=loop
- `[cr_id_skip]` ✅
- `Auto-commit XXX` ✅ 大写英文首字母（Auto 的 A）

task_description 本身可以是中文或其他非英文，不影响门禁（门禁只检查首字母）。

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| flock 在 NFS/FUSE 上语义不保证 | cc-builder-loop 部署在本地 ext4/xfs，风险不存在。文档 CLAUDE.md V1.8.3 条目里标注「本地文件系统前提」 |
| awk 解析 block scalar 遇到 task_description 为空（如旧版 state 或手动清空） | 降级到旧 `Auto-commit iter N` message，不让 commit 失败 |
| bootstrap 分支获取锁失败 → 用户看不到提示 | bootstrap 本来就是"自动续接"，本次放行意味着另一 hook 在跑，无需用户介入；符合 exit 0 静默语义 |
| flock 抢到锁后异常退出，锁会保留？ | FD 关闭即释放（exec 200>... 机制），脚本异常退出（包括 set -e 触发）shell 会自动关 FD |
| task_description 含换行符（block scalar 能容纳） | awk 只取第一行（`exit`），后续换行被忽略；符合"一句话摘要" commit 惯例 |

## 文件地图

**存量文件**（改动点）：

| 文件 | 改动 |
|------|------|
| `scripts/builder-loop-stop.sh` | 在 L149（`state file 仍不存在 → 放行`）**之前**插入 SLUG 提取 + flock 加锁块 |
| `skills/builder-loop/scripts/merge-worktree-back.sh` | L132~141 范围：加 task_description 解析 + message 分支构造 + 改用 `git commit -F -` |
| `CLAUDE.md` | 「已交付能力」章节 V1.8.2 之后加 V1.8.3 条目 |

**新增文件**：

| 文件 | 内容 |
|------|------|
| `skills/builder-loop/fixtures/e2e/test-stop-hook-race-and-commit-msg.sh` | E2E 两个场景：①模拟并发 flock（一个后台 subshell 持锁 `sleep 10`，另一前台立即调 hook 期望 exit 0 静默）②state 含 task_description → 跑 merge-worktree-back.sh → `git log -1 --pretty=%B` 含 task_description 字符串 |

**无需改动**：
- setup-builder-loop.sh（state schema 保持 block scalar）
- 其他 hook 脚本（flock 只保护 Stop hook，其他 hook 场景不同）
- 现有 E2E 脚本

## 执行任务列表

1. **改 `scripts/builder-loop-stop.sh`**：
   - 在 L149 `# state file 仍不存在 → 放行` 之前新增代码块：提取 SLUG（basename STATE_FILE .yml，兜底 `__main__`）、构造锁文件路径、`exec 200>` 打开、`flock -n 200` 抢锁、抢不到 `exit 0` 静默

2. **改 `skills/builder-loop/scripts/merge-worktree-back.sh`**：
   - L134 后读 TASK_DESC（awk 解析 `task_description: |` 下一行）
   - L138 改为条件构造 MSG（有 TASK_DESC 用完整 message，否则回退旧格式）
   - `git commit -m "..."` 改为 `printf | git commit -F -` 避免注入

3. **新增 `skills/builder-loop/fixtures/e2e/test-stop-hook-race-and-commit-msg.sh`**：按测试计划场景

4. **更新 `.claude/loop.yml`**：新增 stage `race_and_msg` 指向新 E2E

5. **更新 `CLAUDE.md`**：「已交付能力」V1.8.2 之后加：
   > - **V1.8.3**: Stop hook flock 互斥 + auto-commit message 语义化
   >   - 并发 Stop hook 用 per-slug `flock -n` 互斥，抢不到锁 exit 0 静默放行（修 V1.8 之前的 TOCTOU race，复现 session `d9ef1004` 末尾 `grep state: No such file`）
   >   - merge-worktree-back.sh auto-commit message 从 state 的 task_description 构造（`Auto-commit ${task}`），loop 主干 commit 不再丢失语义

6. **本地跑通 E2E**：`bash skills/builder-loop/fixtures/e2e/test-stop-hook-race-and-commit-msg.sh` 退出码 0；`bash skills/builder-loop/fixtures/e2e/test-stop-hook-cursor.sh`（V1.8.2 测试）仍 25/25 通过

## 验收标准

- [ ] 新 E2E 脚本本地退出码 0
- [ ] V1.8.2 测试脚本仍全绿（无回归）
- [ ] 其他原有 E2E 全部仍通过：`test-new-repo-loop.sh` / `test-isolation.sh` / `test-parallel-loop.sh` / `test-zombie-selfheal.sh` 退出码 0
- [ ] 手动验证：在 hongyu_Repo 或类似接入项目跑一次 loop，`git log -1 --pretty=%s main` 应为 `chore(loop): [cr_id_skip] Auto-commit <task_description>` 而非 `Auto-commit iter 0`
- [ ] `git diff` 只涉及方案声明的 4 个文件

<!-- /role -->

<!-- role:tester -->

## 测试计划

**测试目标**：
1. 验证 per-slug flock 互斥生效（并发触发时后者静默放行）
2. 验证 auto-commit message 能正确从 state 的 task_description 构造

**测试深度**：快速（1 个 E2E 脚本覆盖 2 组场景即可）。

### 关键测试场景

**场景组 A：flock 并发互斥**

1. 初始化临时仓库 + loop.yml（pass_cmd = `sleep 5` 让 PASS_CMD 慢下来）+ 种子 commit
2. 后台 subshell 持锁：`( flock 200; sleep 10 ) 200>${LOCK_FILE} &`
3. 前台立即调 Stop hook：`printf '{"cwd":"$TMP"}' | bash $HOOK_SCRIPT 2>$ERR`
4. 断言：
   - 前台 hook exit 0（静默放行）
   - stderr **不含** `iter` / `正在跑 PASS_CMD` 等流程关键字
   - stderr 长度 = 0 或非常小（纯静默）
5. 等待后台锁释放，确认再次调用可正常运行（锁文件不阻塞后续）

**场景组 B：auto-commit message 从 task_description 构造**

6. 临时仓库 + loop.yml + 启用 worktree
7. 调 setup-builder-loop.sh 带一个明确的 task_description（如 `"E2E test: auto-commit message propagation"`）
8. 在 worktree 里造一个改动（echo > file），**不 commit**
9. 调 merge-worktree-back.sh
10. 断言：
    - exit 0，输出含 `MERGED` 或 `NOOP`
    - 主干 git log 最新 commit message 含 `Auto-commit E2E test: auto-commit message propagation`
    - 不是旧的 `Auto-commit iter 0`

**场景组 C：task_description 为空时降级**

11. 手动写一个 task_description 为空的 state 文件
12. 造改动 + 调 merge-worktree-back.sh
13. 断言：commit message 回退到 `Auto-commit iter 0`（旧行为），不失败

### 边界条件思路

- **锁文件不存在**：首次调用时自动创建，不要因为 `exec 200>` 失败就崩（测试观察：脚本第一次跑应该能创建 lock 文件）
- **task_description 含 `"` 或反引号**：用 `-F -` 传给 git commit，shell 不会二次展开
- **task_description 含换行**：awk 只取第一行（`exit` 提前退出）
- **flock 工具不可用**（极罕见）：当前方案假定 Linux + util-linux flock 存在；测试环境检测 `command -v flock` 能找到即可

### 验收口径

脚本退出码 0 即通过，末尾打印 `✅ PASS=<n>  ❌ FAIL=<m>`。

<!-- /role -->
