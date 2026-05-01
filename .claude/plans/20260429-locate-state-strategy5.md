# V2.4 主仓 cwd 自动绑定 active worktree state（locate-state.sh 策略 5）

<!-- role:shared -->

## 背景 & 目标

### 现状
V1.8 把 state 文件迁到 `.claude/builder-loop/state/<slug>.yml` + `locate-state.sh` 按 cwd 定位。当用户 / `setup-builder-loop.sh` 创建 worktree 但 CC session cwd 仍在主仓时，`locate-state.sh` 4 个策略全部 miss：
- 策略 1（向上找 `.claude/loop.yml`）能找到主仓 `PROJECT_ROOT`
- 策略 2（cwd 在 `.claude/worktrees/<slug>/` 下）→ 不命中（主仓 cwd 不在 worktree 里）
- 策略 3（遍历 state 比对 `worktree_path`）→ cwd ≠ worktree path
- 策略 4（`__main__.yml` 兜底）→ worktree 模式不存在该文件

stop hook 拿到空 `STATE_FILE` → 走 bootstrap → 撞上「跨 session 污染守门」（已存在 `loop/` 前缀 worktree → 静默放行）→ **永远不跑 PASS_CMD**，loop 全程哑火。绕过靠用户手动 `cd <worktree>`，但这是 V1.8 多状态主推荐路径的核心盲区，复现于 V2.2 落地 session `1781a3be`。

### 目标
1. **根治**：`locate-state.sh` 加策略 5 — 主仓 cwd + 同项目仅 1 个 active worktree state → 自动绑定
2. **决策跳转兜底**：`setup-builder-loop.sh` 检测 cwd ≠ worktree → 末尾打醒目 stderr 警告，告知用户/builder 后续 `cd <worktree>` 才能让 stop hook 跟踪
3. **诊断回路**：stop hook locate 未命中时打 stderr 列出扫到的候选 state（active / inactive / 死 worktree），出问题时一眼定位
4. **多 active 防误绑**：≥2 个 active worktree state 时仍返回空（保 V1.8 多状态隔离精神），不冒错绑风险

### 成功标准
- 主仓 cwd + 1 active worktree → stop hook 跟上 PASS_CMD
- 主仓 cwd + ≥2 active worktree → stop hook 不绑（但有诊断 stderr 提示用户）
- e2e fixture 全部通过；接入到 `.claude/loop.yml` 的 PASS_CMD

## 预估改动级别
**L2**（实现改动）。改 3 个脚本（`locate-state.sh` / `builder-loop-stop.sh` / `setup-builder-loop.sh`）+ 新增 1 个 e2e fixture + 文档同步。无新接口、无 schema 变更，纯行为补丁。Builder 实际跑下来若发现策略 5 触及更多文件（如 `migrate-state.sh` 的 active 字段计数），可升 L3 再讨论。

## 验收标准
1. 跑 `bash skills/builder-loop/fixtures/e2e/test-locate-state-strategy5.sh` 全部 case PASS
2. 在主仓 cwd 下手动 `bash setup-builder-loop.sh "<task>"`（worktree.enabled=true）→ 末尾 stderr 含「⚠️ CC session cwd 仍在主仓 ... 请 cd 到 <worktree>」
3. 在主仓 cwd 下喂 `printf '{"cwd":"<repo>"}' | bash scripts/builder-loop-stop.sh` → 命中策略 5 + 跑 PASS_CMD（不进 bootstrap）
4. 改完 `.claude/loop.yml` 加 stage `v24_locate_strategy5` → 跑 builder loop 自验证（meta-test）
5. CLAUDE.md「已交付能力」加 V2.4 段；`.claude/improvements.md` 删除 `#5 V1.8 多状态并行 + worktree 启用时 stop hook 用主仓 cwd 找不到 state` 一段

<!-- /role -->

<!-- role:builder -->

## 约束 & 边界

### 不能碰
- `state/<slug>.yml` schema：不新增字段、不改字段语义（V2.0 / V2.3 字段维持原样）
- bare loop（`worktree_path` 空）行为：策略 4 兜底 `__main__.yml` 完全保留
- `locate-state.sh` 的「静默错误」契约（hook 频繁调用，不向 stderr 喷）：策略 5 命中走静默；不命中也静默；诊断 stderr 由 stop hook 自己出
- V1.x 老 state 兼容：缺 `active` 字段视为非 active（保守不绑）

### 必须兼容
- 现有策略 1-4 路径：策略 5 只在策略 2/3 全部 miss + 策略 4 文件不存在 时才介入
- V1.8 多状态隔离精神：≥2 个 active worktree → 仍返回空（绑错的代价 > 漏绑）
- bootstrap 兜底激活：locate 命中策略 5 后直接走正常 state 路径，不再走 bootstrap（一致性）

## 技术选型

### 方案 A：locate-state.sh 加策略 5（自动绑定唯一 active）★推荐
**做法**：策略 4 之前插入策略 5 — 扫 `STATE_DIR/*.yml`，过滤「`active: true` AND `worktree_path` 非空 AND 该目录存在」→ 仅 1 个候选时输出该 state 路径 exit 0；2+ 候选返回空 exit 1 让 stop hook 走诊断分支。
- 优点：根治问题、零侵入 schema、与 V1.8 多状态隔离精神兼容（多 active 不绑）
- 缺点：「自动绑」对维护者是隐式行为，需文档清晰

### 方案 B：setup 强制改 CC session cwd（rejected）
让 setup 输出 `cd <worktree>` 让用户/builder 执行，依赖人手切。问题：CC session cwd 不能由子进程改；user/builder 容易忘；治标不治本。

### 方案 C：state 字段记 owner_cwd 反向定位（rejected）
state 已存 `owner_cwd`，让 hook 反向「我是不是这个 cwd」。问题：仍需扫描全部 state、判定逻辑复杂、跨 session 改 cwd 后 owner_cwd 失准。

**选 A 理由**：方案 A 三条建议方向都覆盖（locate 策略 5 + setup 提示 + hook 诊断），影响面最小，能自验证（fixture 多 case 覆盖边界）。

## 方案设计

### locate-state.sh 策略 5 算法
```
# 在策略 4（兜底 __main__.yml）之前插入
if [ -d "$STATE_DIR" ]; then
  active_candidates=()
  for sf in "$STATE_DIR"/*.yml; do
    [ -e "$sf" ] || continue
    active="$(grep -E '^active:' "$sf" | head -1 | awk '{print $2}')"
    [ "$active" != "true" ] && continue
    wt="$(grep -E '^worktree_path:' "$sf" | head -1 | sed -E 's/...//')"
    [ -z "$wt" ] && continue          # bare loop（策略 4 接管）
    [ ! -d "$wt" ] && continue        # 死 worktree（孤儿 state，不绑）
    active_candidates+=("$sf")
  done
  if [ "${#active_candidates[@]}" -eq 1 ]; then
    echo "${active_candidates[0]}"
    exit 0
  fi
  # 0 个或 ≥2 个 → 静默 fallthrough 到策略 4
fi
```

**触发前提**：策略 1 已锁定 `PROJECT_ROOT`、策略 2 不命中（cwd 不在 worktrees 子目录）、策略 3 不命中（无 worktree_path 等于 cwd 的 state）、策略 4 之前。

### stop hook 诊断 stderr
locate 返回空（`STATE_FILE=""`）后，进入 bootstrap 分支前补一段：
```
if [ -z "$STATE_FILE" ] && [ -d "${PROJECT_ROOT}/.claude/builder-loop/state" ]; then
  ACTIVE_LIST="$(扫描 + 过滤 active=true 的 worktree state，列 wt path 与状态)"
  if [ -n "$ACTIVE_LIST" ]; then
    echo "[builder-loop] ⚠️  cwd=${CWD} 未匹配任何 state，但发现以下 active worktree state：" >&2
    echo "$ACTIVE_LIST" >&2
    echo "[builder-loop]    若要让 stop hook 跟踪，请 cd 到对应 worktree" >&2
  fi
fi
```
**注意**：`PROJECT_ROOT` 此处仍未赋值（state 未命中走的是 `FOUND_LOOP_ONLY=true` 路径）。需先在 locate 后做一次「向上找 loop.yml」拿到 root（与 stop hook L118-127 复用同样逻辑），或调用 `find_project_root` 工具函数（locate-state.sh 已内置可借鉴）。

**限频**：仅当扫描后 ACTIVE_LIST 非空时打印（避免在新接入项目 / 无 loop 项目刷屏）。

### setup-builder-loop.sh 末尾警告
在末尾 `echo "提示：下次 Stop hook ..."` **之前**加：
```
if [ -n "$WORKTREE_PATH" ] && [ "$OWNER_CWD" = "$PROJECT_ROOT" ]; then
  cat >&2 <<WARN
⚠️  CC session cwd 仍在主仓：${OWNER_CWD}
   stop hook 触发时不能直接定位本 worktree state（除非命中策略 5 / 唯一 active）。
   建议下一步：
     1. 若本 session 还要继续：cd ${WORKTREE_PATH}
     2. 若并发多 worktree：在新 CC session 用 --cwd ${WORKTREE_PATH} 启动
WARN
fi
```
`OWNER_CWD` 已在 L334 取过（`OWNER_CWD="$(pwd -P)"`），直接复用。

## 文件地图

### 改动文件
| 路径 | 改动点 | 估行数 |
|------|--------|--------|
| `skills/builder-loop/scripts/locate-state.sh` | L99 之前插入策略 5（约 20 行 bash） | +20 |
| `scripts/builder-loop-stop.sh` | L86-87 之间插入诊断 stderr 段（约 25 行） | +25 |
| `skills/builder-loop/scripts/setup-builder-loop.sh` | L370 之前插入 cwd 不一致警告（约 12 行） | +12 |
| `CLAUDE.md` | 「已交付能力」加 V2.4 一段；§7 加 7.10 已知问题（cwd 未追时的诊断说明） | +25 |
| `.claude/improvements.md` | 删除 `#5 2026-04-26 V1.8 多状态并行 + worktree 启用时 stop hook 用主仓 cwd 找不到 state` 全段 | -8 |
| `.claude/loop.yml` | PASS_CMD 加 `v24_locate_strategy5` stage | +1 |

### 新增文件
| 路径 | 用途 |
|------|------|
| `skills/builder-loop/fixtures/e2e/test-locate-state-strategy5.sh` | E2E 4 case 验证策略 5 + setup 警告 |

## 执行任务列表

按依赖顺序执行：

1. **改 `locate-state.sh`**：在 L99（`MAIN_STATE=...`）之前插入策略 5 代码块；保留文件头注释「静默错误」契约；新策略号文档化（更新文件头 L11-15 的策略列表 → 新增 5）
2. **改 `setup-builder-loop.sh`**：在 L370 `echo "提示：下次 Stop hook ..."` 之前插入 cwd 不一致警告（条件判断 `$WORKTREE_PATH` 非空 AND `$OWNER_CWD` = `$PROJECT_ROOT`）
3. **改 `scripts/builder-loop-stop.sh`**：在 L86 `STATE_FILE=...` 后、L88 `if [ -n "$STATE_FILE" ]` 前插入诊断段；扫 STATE_DIR；仅在有 active worktree state 时打 stderr
4. **新增 `test-locate-state-strategy5.sh`**：参照 `test-stop-hook-cursor.sh` 模板写 4 case（详见 tester 视图）
5. **改 `.claude/loop.yml`**：PASS_CMD 末尾加一行 `- { stage: "v24_locate_strategy5", cmd: "bash skills/builder-loop/fixtures/e2e/test-locate-state-strategy5.sh", timeout: 30 }`
6. **改 `CLAUDE.md`**：V2.3 段下补 V2.4 段（约束：参照 V2.3 风格、≤30 行、含「背景 / 修复 / 边界 / 完全向后兼容」四要素）；§7.9 后加 §7.10（cwd 未追时的诊断说明）
7. **改 `.claude/improvements.md`**：删除 `#5 2026-04-26` 整段（落地后从待办池移除）
8. **本机自验证**：手动喂 stop hook 三种 cwd（主仓 / worktree 子目录 / 主仓 + 多 active）→ 看行为符合预期
9. **跑 builder loop**：触发 V2.4 stage 自闭环回归（meta：本期改动用 builder-loop 自己自验证）

### 退路
- 步骤 1-3 任一引入回归 → revert 该脚本，loop 仍按 V2.3 行为工作（用户手动 cd worktree）
- 步骤 4 fixture 跑不通 → 不阻断核心修复（locate 策略 5 本身的逻辑能用），fixture 加 `# TODO V2.4.1` 后续修

## 风险 & 应对

### R1：策略 5 把 V1.x 老 state（缺 active 字段）误绑
- **场景**：用户从 V1.7 升级，state 文件没 `active` 字段
- **应对**：策略 5 只接受 `active: true` 字面值；缺字段或值为空都视为非 active 不绑（grep 取空 → 字符串比对 != "true" → continue）

### R2：诊断 stderr 在「未接入 builder-loop 的项目目录但 cwd 偶然命中 .claude/loop.yml 路径」打印噪音
- **场景**：用户在某个有 `.claude/loop.yml` 但没启 loop 的项目下 cd → CC stop hook 触发 → 诊断 stderr 噪音
- **应对**：仅当 `STATE_DIR/*.yml` 存在 AND 至少 1 个 `active: true` 时打印（无 active state 时静默）

### R3：多 builder 并发跑同一项目（多 active worktree）→ 用户期望「最近创建那个」自动绑
- **场景**：用户跑 2 个 worktree loop，习惯后想 locate 自动选最新
- **应对**：本期不实现，保持「多 active 不绑」（用户已选「仍返回空，stderr 警告」选项）；若长期反馈强烈，下一期可加 `loop.yml.locate.tie_breaker: latest_mtime` 配置项

### R4：fixture 在 CI / 不同 git 版本下 worktree behavior 抖动
- **场景**：`git worktree add` 在不同 git 版本输出格式可能微差
- **应对**：fixture 不依赖 git worktree 命令本身，改用「手动 mkdir + 写 state yml + worktree_path 字段指 mkdir 出来的目录」模拟（参照 `test-stop-hook-cursor.sh` 风格）

### R5：策略 5 命中后 stop hook 走正常路径，但 worktree 内未 commit `.claude/loop.yml` → run-pass-cmd 走 fallback 主仓
- **场景**：用户 setup 后未先 commit loop.yml → worktree 内 loop.yml 缺失
- **应对**：已是 V2.0 已知行为（CLAUDE.md §7.4），fallback 主仓 + stderr 警告，不阻断；本期不重复处理

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标
验证 `locate-state.sh` 策略 5 + stop hook 诊断 stderr + setup-builder-loop.sh cwd 警告三处补丁的行为正确性。

### 接口签名（黑盒视角）

#### `locate-state.sh`
- **输入**：`bash locate-state.sh <cwd>`，stdin 不读
- **输出 stdout**：state 文件绝对路径（命中）或空（未命中）
- **输出 stderr**：始终空（locate 静默契约）
- **退出码**：命中 → 0；未命中 → 1

#### `builder-loop-stop.sh`
- **输入**：stdin = `{"cwd": "<path>", "transcript_path": "..."}` JSON
- **输出 stderr**：诊断信息 / 状态提示
- **退出码**：续接 → 2；放行 → 0

#### `setup-builder-loop.sh`
- **输入**：`bash setup-builder-loop.sh [--no-worktree|--no-stash] "<task>"`
- **输出 stdout**：状态报告
- **输出 stderr**：警告 / 提示信息（含本期新增 cwd 不一致警告）
- **退出码**：成功 → 0；冲突 → 4；其他失败 → 1/2/3/5

### 关键测试场景

#### Case A1：主仓 cwd + 1 active worktree state → 策略 5 命中
**前置**：临时 git repo + `.claude/loop.yml`（worktree.enabled=true）+ 手动构造 1 个 state 文件 `state/foo-slug.yml`（`active: true`、`worktree_path: <tmp>/wt-foo`）+ `<tmp>/wt-foo` 目录存在。

**操作**：`bash locate-state.sh <repo-root>`

**断言**：
- exit code = 0
- stdout 输出 `state/foo-slug.yml` 绝对路径
- stderr 为空

#### Case A2：主仓 cwd + 2 active worktree state → 不绑 + stop hook stderr 含诊断
**前置**：在 A1 基础上再加 1 个 state 文件 `state/bar-slug.yml`（`active: true`、`worktree_path: <tmp>/wt-bar`）+ `<tmp>/wt-bar` 目录存在。

**操作**：
1. `bash locate-state.sh <repo-root>` → 验证 exit 1 + stdout 空 + stderr 空
2. `printf '{"cwd":"<repo-root>"}' | bash scripts/builder-loop-stop.sh 2>err.log` → 验证 stderr 含 `cwd=... 未匹配任何 state` 与两个 worktree path

**断言**：
- locate exit code = 1
- stop hook stderr 含本任务 worktree path 字符串（两个都列出）
- stop hook stderr 含「请 cd 到对应 worktree」提示

#### Case A3：主仓 cwd + 1 active state 但 worktree 目录已删 → 不绑（孤儿排除）
**前置**：构造 1 个 state（`active: true`、`worktree_path: <tmp>/wt-dead`）+ **不创建** `<tmp>/wt-dead` 目录。

**操作**：`bash locate-state.sh <repo-root>`

**断言**：
- exit code = 1（不绑 dead worktree）
- stdout 空

#### Case A4：setup 调用后立即跑 locate（端到端）→ 命中
**前置**：临时 git repo + `loop.yml`（worktree.enabled=true）+ 真跑 `bash setup-builder-loop.sh "test-task-slug"`。

**操作**：
1. 看 setup stderr 是否含 `⚠️  CC session cwd 仍在主仓` + worktree path（cwd=主仓，setup 把 worktree 创建在 `.claude/worktrees/<ts>-test-task-slug`）
2. `bash locate-state.sh <repo-root>` → 验证策略 5 命中 setup 创建的 state

**断言**：
- setup stderr 含 `⚠️` + `CC session cwd 仍在主仓` 文案
- setup stderr 含 worktree 绝对路径
- locate exit 0 + stdout = setup 创建的 state 路径

### 测试深度
中等（4 case），覆盖主路径 + 多 active 边界 + 死 worktree 边界 + 端到端集成 case。

### Fixture 风格约定
1. 严格参照 `test-stop-hook-cursor.sh` 模板（assert 函数、`PASS/FAIL` 计数、`mktemp -d` + trap cleanup、git config 用 `e2e@test.local`）
2. 每个 case 末尾打印 `--- Step N done ---`
3. 末尾汇总：`echo "PASS: $PASS  FAIL: $FAIL"` + `[ $FAIL -eq 0 ]` 决定 exit code
4. 临时 git commit message 必须 `chore(test): [cr_id_skip] Xxx` 格式（项目根 hook 校验）
5. 不依赖外部网络 / API（不触发 judge agent，PASS_CMD 用 `cmd: "true"`）
6. 临时仓库 `worktree_path` 字段写绝对路径（避免 cwd 漂移撞 mtime 排序）

### 边界条件需验证
- V1.x 老 state（缺 `active` 字段）→ 不参与策略 5 候选
- bare loop（`worktree_path` 空）→ 策略 4 兜底 `__main__.yml` 不受影响
- 一个 active + 一个 inactive → 仅候选 active，命中策略 5
- `STATE_DIR` 不存在 → fallthrough 到策略 4 失败，退出 1（不报错）

<!-- /role -->
