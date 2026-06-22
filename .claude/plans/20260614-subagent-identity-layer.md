# V3.5-A: Subagent 来源身份层

<!-- role:shared -->

## 背景 & 目标

消化 improvements.md 中 9 条同根因 TODO（06-02 ×3 / 05-31 / 05-29 / 05-28 / 05-26 / 05-10 / 05-09），共同特征 = subagent / hook / session 之间缺「来源身份」判定层，导致：
- subagent 写文件落到主仓而非 worktree（被误放行或未被 guard 拦截）
- 并发 subagent 互相覆盖同一个 lock 文件
- 非 builder-loop subagent（workflow / Explore）触发 builder-loop hook
- Stop hook / SubagentStop 错位（锁残留 / 误删）

**成功标准**：9 条同根因对应场景全部有 fixture 覆盖或机械化防御，improvements.md 对应条目可标 ✅。

## 验收标准

1. 新建的 e2e fixture 全部 PASS（`run-fixture.sh test-subagent-identity-*.sh`）
2. 现有 48 个 fixture 无回归（`run-fixture.sh` 全量跑）
3. 真实项目 dogfood 一轮：worktree 模式下 tester + doc-maintainer 各跑一次，锁文件正确创建/清理
4. workflow / Explore subagent 不产生锁文件

<!-- /role -->

<!-- role:builder -->

## 预估改动级别

L3（新接口/模块）——新建 lock-utils.sh 公共函数库 + 改 lock 文件命名规则 + 改 hook 注册。

## 约束 & 边界

- **不等 CC 新接口**：纯 fallback 路径，用现有 SubagentStart stdin 的 session_id + subagent_type + cwd 三字段
- **不改 CC 源码**：所有改动基于 hook / skill / agent 扩展机制
- **向后兼容**：SubagentStart 写新格式锁的同时，PreToolUse guard 也要能处理旧格式锁（过渡期）
- **active 字段不碰**：V3.0 技术债，本次不清理

## 技术选型

| 方案 | 描述 | 结论 |
|------|------|------|
| **A: 按 agent_type 分文件** | 锁文件 `cc-subagent-{sid}-{agent_type}.lock`，每种 agent 独立锁 | ✅ 采纳。简单够用，同类型不会并发 |
| B: 锁目录 + 清单文件 | `/tmp/cc-subagent-{sid}/` 目录，每个 agent 子文件 | ❌ 排除。mkdir/rmdir 原子性复杂，收益小 |

**白名单策略**：正向白名单 `[tester, doc-maintainer, arbiter, reviewer]` + active state 双条件。不在白名单 或 无 active state → 不写锁，完全不干预。

## 方案设计

### 核心改动

1. **新建 `scripts/lock-utils.sh`**——公共函数库，提供：
   - `BL_AGENT_WHITELIST="tester doc-maintainer arbiter reviewer"` 常量
   - `bl_is_managed_agent(agent_type)` → 0/1
   - `bl_lock_path(session_id, agent_type)` → 锁文件绝对路径
   - `bl_find_active_locks(session_id)` → 当前 session 所有活锁列表
   - `bl_read_lock_field(lock_file, field_name)` → 字段值
   - `bl_is_sync_agent(agent_type)` → 0/1（tester/doc-maintainer/arbiter = sync, reviewer = background）
   - `bl_cleanup_stale_locks(session_id, ttl_sec)` → 清理过期锁

2. **改 `scripts/subagent-start-guard.sh`**：
   - source lock-utils.sh
   - 白名单检查：`bl_is_managed_agent` + locate-state.sh 找 active state（双条件）
   - 锁文件路径改用 `bl_lock_path(session_id, agent_type)` → `cc-subagent-{sid}-{agent_type}.lock`
   - 写锁内容不变（agent_type / session_id / project_root / worktree_path / slug / start_ts / ttl_min / source_dirs_abs）

3. **改 `scripts/tester-lock-clear.sh`** → 重命名为 **`scripts/subagent-lock-clear.sh`**：
   - source lock-utils.sh
   - 从 stdin 读 session_id + subagent_type（SubagentStop 提供）
   - 精确删除 `bl_lock_path(session_id, subagent_type)` 对应的锁文件
   - 非白名单 agent_type → 直接 exit 0

4. **改 `scripts/worktree-write-guard.sh`**：
   - source lock-utils.sh
   - 用 `bl_find_active_locks(session_id)` 替代单文件 `cc-subagent-{sid}.lock` 查找
   - 遍历活锁，任一 sync agent 锁存在 → 进入 SUBAGENT STRICT MODE
   - 锁内字段读取改用 `bl_read_lock_field`

5. **改 `scripts/tester-lock-check.sh`**：
   - source lock-utils.sh
   - 用 `bl_find_active_locks(session_id)` 查找，grep agent_type=tester 的锁
   - 只有 tester 锁存在时才启用 source_dirs 读隔离

6. **改 `install.sh`**：
   - SubagentStop 注册：脚本名从 `tester-lock-clear.sh` 改为 `subagent-lock-clear.sh`，matcher 保持 None（无限制）
   - 新增 deprecated 条目：`("SubagentStop", "tester-lock-clear.sh")`
   - 新增链接映射：`scripts/lock-utils.sh` → `~/.claude/scripts/lock-utils.sh`
   - echo 输出更新脚本名

7. **改 `uninstall.sh`**：
   - bl_scripts 列表加 `subagent-lock-clear.sh` + `lock-utils.sh`，去掉 `tester-lock-clear.sh`

8. **改 `CLAUDE.md` 链接映射表 + hook 注册表**：同步新文件名

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| 旧格式锁残留（升级前写的 `cc-subagent-{sid}.lock`）导致 guard 逻辑错位 | `bl_find_active_locks` 同时 glob 新旧两种格式，旧格式按现有逻辑处理 |
| SubagentStop 的 stdin 中 subagent_type 可能为空（CC 行为不确定） | `subagent-lock-clear.sh` 对空 type 回退为 glob 删除 `cc-subagent-{sid}-*.lock` 全部 |
| `locate-state.sh` 每次 SubagentStart 都调一次，性能开销 | 已有机制（现行 subagent-start-guard.sh 已经调了），开销可接受 |
| 改 hook 注册后旧 `tester-lock-clear.sh` 条目残留 | install.sh deprecated 列表清理 |

## 文件地图

| 文件 | 操作 | 改动点 |
|------|------|--------|
| `scripts/lock-utils.sh` | **新建** | 公共函数库（7 个函数 + 1 个常量） |
| `scripts/subagent-start-guard.sh` | 改 | source lock-utils + 白名单双条件 + 新锁路径 |
| `scripts/tester-lock-clear.sh` | 删 | 被 subagent-lock-clear.sh 替代 |
| `scripts/subagent-lock-clear.sh` | **新建** | 通用清锁（按 session_id + agent_type 精确删） |
| `scripts/worktree-write-guard.sh` | 改 | source lock-utils + glob 多锁查找 |
| `scripts/tester-lock-check.sh` | 改 | source lock-utils + 只匹配 tester 锁 |
| `scripts/reviewer-timing-check.sh` | 改 | source lock-utils（可选，统一 bl_read_lock_field） |
| `install.sh` | 改 | SubagentStop 脚本名 + deprecated + 新链接 |
| `uninstall.sh` | 改 | bl_scripts 列表更新 |
| `CLAUDE.md` | 改 | 链接映射表 + hook 注册表 |
| `skills/builder-loop/SKILL.md` | 改 | lock 文件路径说明更新 |
| `fixtures/e2e/test-subagent-identity-concurrent-locks.sh` | **新建** | 并发 subagent 锁不互覆盖 |
| `fixtures/e2e/test-subagent-identity-whitelist.sh` | **新建** | workflow/Explore 不写锁 |
| `fixtures/e2e/test-subagent-identity-clear-all-types.sh` | **新建** | 各 agent_type 清锁正确 |
| `fixtures/e2e/test-subagent-identity-ttl-expiry.sh` | **新建** | TTL 过期锁自动失效 |
| `fixtures/e2e/test-subagent-identity-old-lock-compat.sh` | **新建** | 旧格式锁向后兼容 |

## 执行任务列表

### Phase 1: 公共函数库
1. 新建 `scripts/lock-utils.sh`，实现 7 个函数 + 白名单常量
2. 单元级验证：source 后调每个函数确认输出

### Phase 2: 写锁改造
3. 改 `scripts/subagent-start-guard.sh`：source lock-utils + 白名单双条件 + 新锁路径
4. 新建 `scripts/subagent-lock-clear.sh`：通用清锁逻辑
5. 删 `scripts/tester-lock-clear.sh`（git rm）

### Phase 3: 读锁改造
6. 改 `scripts/worktree-write-guard.sh`：glob 多锁 + bl_is_sync_agent 判定
7. 改 `scripts/tester-lock-check.sh`：只匹配 tester 锁
8. 改 `scripts/reviewer-timing-check.sh`：统一 source lock-utils（可选）

### Phase 4: 部署改造
9. 改 `install.sh`：脚本名 + deprecated + 新链接
10. 改 `uninstall.sh`：bl_scripts 列表
11. 改 `CLAUDE.md`：链接映射表 + hook 注册表
12. 改 `SKILL.md`：lock 文件路径说明

### Phase 5: 测试
13. 新建 5 个 e2e fixture（并发锁 / 白名单 / 清锁 / TTL / 旧锁兼容）
14. 全量 `run-fixture.sh` 无回归
15. 真实项目 dogfood

### Phase 6: 收尾
16. improvements.md 9 条同根因逐条标记状态
17. CHANGELOG.md 追加 V3.5 段

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标
验证 subagent 来源身份层在各种场景下正确工作：锁创建 / 锁隔离 / 锁清理 / 白名单过滤 / 向后兼容。

### 关键测试场景

1. **并发锁不互覆盖**（`test-subagent-identity-concurrent-locks.sh`）
   - 模拟 tester 和 doc-maintainer 同 session_id 先后写锁
   - 断言：两个锁文件独立存在，字段各自正确
   - 断言：清除 tester 锁后 doc-maintainer 锁仍在

2. **白名单过滤**（`test-subagent-identity-whitelist.sh`）
   - 模拟 workflow / Explore / general-purpose 类型触发 SubagentStart
   - 断言：不产生锁文件
   - 模拟无 active state 时 tester 触发 SubagentStart
   - 断言：不产生锁文件（双条件）

3. **各 agent_type 清锁**（`test-subagent-identity-clear-all-types.sh`）
   - 依次创建 tester / doc-maintainer / arbiter / reviewer 锁
   - 模拟各自 SubagentStop，断言对应锁被删、其他锁不受影响

4. **TTL 过期**（`test-subagent-identity-ttl-expiry.sh`）
   - 创建锁并将 start_ts 设为过去（超过 TTL）
   - 触发 worktree-write-guard → 断言过期锁被忽略/删除

5. **旧格式锁兼容**（`test-subagent-identity-old-lock-compat.sh`）
   - 手动创建旧格式 `cc-subagent-{sid}.lock`
   - 触发 worktree-write-guard → 断言按旧逻辑处理不报错

### 测试深度
深度（e2e fixture），覆盖所有 5 个关键场景。

<!-- /role -->
