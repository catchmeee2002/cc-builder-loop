# bare 模式 V3.0 reviewer-as-gate 升级 + e2e 默认 bare

<!-- role:shared -->

## 背景 & 目标

bare 模式 PASS 后仍走 V2.x 老路径（reviewer 事后咨询、state 立刻删除），缺失 V3.0 reviewer-as-gate 的前置门禁和反馈闭环。同时需要 e2e 行为测试的项目无法使用 worktree 模式（活进程绑定主仓 data/.env/log），需要在 plan 指定 e2e 时自动选择 bare 模式。

**成功标准**：bare 模式行为完全对齐 worktree V3.0（commit → phase=passed\_pending\_review → reviewer gate → cleanup），全套 fixture 覆盖，现有 worktree fixture 不回归。

## 预估改动级别

L2（实现改动）——修改 3 个脚本 + 1 个 prompt 文件的 bare/worktree 分支逻辑，新建 1 个脚本 + 2 个 fixture，删除 1 个脚本，更新 1 个文档。

## 约束 & 边界

- V2.x bare 路径（reviewer-params.json + rm state）**整段删除**
- 已接入项目 loop.yml 可以改（挨个改完）
- worktree 模式现有行为不能动
- merge-worktree-back.sh 保留作为 arbiter 路径入口（已知技术债，不在本次范围）
- bare 的 `__main__` slug 约定不变
- 不为假设性需求预留扩展点

## 技术选型

| 方案 | 描述 | 结论 |
|------|------|------|
| A. 统一 loop-commit.sh | 新建替代 worktree-commit-only.sh，`git -C project_root add -A && commit`，两种模式通用 | **推荐**：project\_root 已指向正确目录，脚本内零分支 |
| B. 分开 worktree-commit + bare-commit | 两个脚本各管各的 | 排除：逻辑重复，两份维护 |
| C. stop hook 内联 commit | 不用外部脚本 | 排除：stop hook 已 700+ 行 |

<!-- /role -->

<!-- role:builder -->

## 方案设计

### 1. 新建 loop-commit.sh（替代 worktree-commit-only.sh）

- 入参：`$STATE_FILE`
- 读 state 取 `project_root`、`task_description`、`pre_loop_dirty_files`
- `git -C "$PROJECT_ROOT" add -A && git commit -m "..."`
- stdout 输出 new HEAD short sha（供 stop hook 写 `last_iter_head`）
- 不区分 bare/worktree——`project_root` 在 bare 时=主仓，worktree 时=worktree path

### 2. builder-loop-stop.sh PASS 分支统一

- 删除 L556-709 的 worktree/bare 双路径分歧
- 统一为：调 `loop-commit.sh` → 写 `phase=passed_pending_review` → 写 `reviewer_pending` 段（含 pass\_start\_head / reviewer\_files / diff\_file / report\_path） → 写 `reviewer-diff-<slug>.txt`
- V2.x bare 路径（reviewer-params.json 生成 + `rm "$STATE_FILE"`）整段删除
- diff 计算统一用 `git -C "$PROJECT_ROOT" diff "$START_HEAD..HEAD"`（commit 后 HEAD 已移动，bare 下不再为空——修掉了现有 bare diff 为空 bug）

### 3. builder-loop-stop.sh L1 闸 bare fallback

- L304-331 当前：`WT_PATH_GATE` 从 `worktree_path` 取值，为空时 `WT_HAS_CHANGES=0`，gate 空转 exit 0
- 修改：`worktree_path` 为空时 fallback 到 `PROJECT_ROOT`
- dirty/new commits 检测逻辑不变，只是检测目标从 worktree 变为主仓

### 4. merge-and-cleanup.sh bare 分支

- 去掉 L38-42 的 bare hard reject（`exit 3`）
- `worktree_path` 为空时：
  - 跳过 ff merge（commit 已在主仓）
  - 跳过 worktree remove（无 worktree）
  - 保留：stash drop（`pre_loop_stash_ref` 非空时）+ rm state + 清理 reviewer 产物
- `cleanup_phase` 状态机照用（幂等保护）

### 5. builder.md reviewer dispatch 统一

- 删除 `reviewer_params=<path>` JSON 消费路径
- 统一为：读 state 的 `reviewer_pending` 段，走 reviewer gate 流程（与现有 worktree 路径一致）
- 新增规则：plan 含 `<!-- e2e-cases -->` 标签时，builder 传 `--no-worktree` 给 setup

### 6. SKILL.md 更新

- L95-99：bare 模式描述对齐 V3.0（删"保留 V2.x 行为"）
- L124-127：`worktree_mode` 字段说明更新

### 删除清单

- `skills/builder-loop/scripts/worktree-commit-only.sh`（被 loop-commit.sh 替代）
- stop hook L650-708 V2.x bare 路径代码
- builder.md 的 `reviewer_params` JSON 消费逻辑
- `.claude/reviewer-params.json` 相关 .gitignore 条目（如有）

### bare 模式特有差异（已评估，由调用方处理）

| 差异 | 处理方 | 方式 |
|------|--------|------|
| commit 直接在 main 分支 | 调用方（用户）| bare 模式天然代价，无隔离分支 |
| abandon 后 commit 留在 main | abandon-loop.sh | 输出加提示"commits on main, use git revert to undo" |
| dirty 范围可能包含无关文件 | owner\_session\_id | stop hook 已有跨 session 拦截 |
| 多轮 commit 堆在 main | reviewer-diff | `start_head..HEAD` 取完整 diff，不受影响 |

<!-- /role -->

## 风险 & 应对

| 风险 | 影响 | 应对 |
|------|------|------|
| stop hook 改坏 | 所有项目 loop 挂 | fixture 覆盖 bare+worktree 两种模式的 PASS→gate→cleanup 全流程 |
| 已接入项目回归 | 升级后 bare loop 行为变化 | 挨个更新已接入项目 + 跑一次 setup 验证；现有 worktree fixture 全跑不回归 |
| bare abandon 后 commit 留在 main | 用户可能不知道如何回滚 | abandon-loop.sh 输出加 revert 提示 |

<!-- plan-checklist -->

## 文件地图

| 文件 | 改动 |
|------|------|
| `skills/builder-loop/scripts/loop-commit.sh` | **新建**：统一 commit 脚本 |
| `skills/builder-loop/scripts/worktree-commit-only.sh` | **删除** |
| `scripts/builder-loop-stop.sh` | **改**：PASS 分支统一 + L1 闸 bare fallback + 删 V2.x 路径 |
| `skills/builder-loop/scripts/merge-and-cleanup.sh` | **改**：去掉 bare hard reject，加 bare 分支 |
| `~/.claude/commands/builder.md`（dotfiles） | **改**：统一 reviewer dispatch + e2e → --no-worktree |
| `skills/builder-loop/SKILL.md` | **改**：bare 模式描述对齐 V3.0 |
| `skills/builder-loop/fixtures/e2e/test-bare-reviewer-gate.sh` | **新建** |
| `skills/builder-loop/fixtures/e2e/test-e2e-default-bare.sh` | **新建** |

## 执行任务列表

### Phase 1: 核心脚本（无外部依赖）

1. 新建 `skills/builder-loop/scripts/loop-commit.sh`：读 state → `git -C project_root add -A && commit` → stdout 输出 new HEAD
2. 改 `scripts/builder-loop-stop.sh` PASS 分支：删 worktree\_path 非空判断，统一调 loop-commit.sh → 写 phase + reviewer\_pending → 写 diff 文件。删 V2.x bare 路径（L650-708）
3. 改 `scripts/builder-loop-stop.sh` L1 闸：`WT_PATH_GATE` 为空时 fallback 到 `PROJECT_ROOT`
4. 改 `skills/builder-loop/scripts/merge-and-cleanup.sh`：去掉 bare hard reject（L38-42），worktree\_path 空时跳过 merge+remove，只做 stash drop + rm state

### Phase 2: 集成 & Prompt（消费 Phase 1 的统一 PASS 路径）

5. 改 `builder.md`：删 `reviewer_params` JSON 消费逻辑，统一为 `reviewer_pending` 段消费；加"plan 含 `<!-- e2e-cases -->` → 传 `--no-worktree`"规则
6. 更新 `SKILL.md`：bare 模式行为描述对齐 V3.0
7. 删 `skills/builder-loop/scripts/worktree-commit-only.sh`

### Phase 3: Fixture（消费 Phase 1+2 的统一行为）

8. 新建 `test-bare-reviewer-gate.sh`：bare setup → PASS → commit → phase=passed\_pending\_review → L1 self-heal → reviewer pass → merge-and-cleanup bare 分支 → state 已删
9. 新建 `test-e2e-default-bare.sh`：plan 含 e2e-cases 标签 → setup 使用 --no-worktree → state.worktree\_mode=bare
10. 跑现有 worktree fixture 全套确认不回归

### Phase 4: 已接入项目同步

11. 挨个检查已接入项目 loop.yml + builder 行为，确认升级后兼容

<!-- /plan-checklist -->

<!-- role:tester -->

## 测试计划

**测试目标**：验证 bare 模式完整 reviewer-as-gate 生命周期 + worktree 模式不回归

**关键测试场景**：

1. **bare PASS → reviewer gate 主流程**：setup --no-worktree → PASS 通过 → loop-commit.sh 在主仓 commit → state.phase=passed\_pending\_review → reviewer\_pending 段完整 → reviewer-diff-\_\_main\_\_.txt 生成。断言：state 未被删除、reviewer-params.json 不再生成

2. **bare L1 自愈**：phase=passed\_pending\_review 下主仓出现 dirty → stop hook 自愈回 active → PASS 重跑。断言：phase 变回 active

3. **bare reviewer pass → cleanup**：merge-and-cleanup.sh 在 worktree\_path 空时不做 ff merge / worktree remove，只删 state + 清理 reviewer 产物。断言：state 文件已删、主仓 commit 保留

4. **bare abandon mid-review**：phase=passed\_pending\_review 时 abandon → state 归档 → 输出含"commits on main"提示。断言：legacy/ 有归档文件

5. **e2e plan → bare default**：plan 含 `<!-- e2e-cases -->` → setup 创建 bare state（slug=\_\_main\_\_，worktree\_mode=bare）。断言：state 字段正确

6. **worktree 模式不回归**：现有 worktree fixture 全跑一遍。断言：全部现有断言通过

**测试深度**：深度——每个场景覆盖正常路径 + 至少一个边界条件

<!-- /role -->

## 验收标准

1. bare 模式 PASS 后：commit 在主仓 → phase=passed\_pending\_review → reviewer 前置门禁 → cleanup 只删 state
2. L1 闸在 bare 模式下正常自愈（主仓 dirty → 回 active）
3. plan 含 `<!-- e2e-cases -->` 时 builder 自动传 `--no-worktree`
4. test-bare-reviewer-gate.sh 全部断言通过
5. test-e2e-default-bare.sh 全部断言通过
6. 现有 worktree fixture 全部不回归
7. SKILL.md bare 模式描述与实际行为一致
