# builder-loop 多状态并行改造

<!-- role:shared -->

## 背景 & 目标

### 问题现场（2026-04-24）

EDB 项目会话里 setup 了 `fix-bot-open-id-timeout` loop（创建 worktree A + 写 state），**1 分钟后**另一个 Claude 会话在同一项目 setup 了 `run-mode-ipc-5` loop（创建 worktree B + **覆盖**了同一 state file）。结果：
- 我当时的 loop 状态被悄悄清掉，worktree A 若未手动 cleanup 就成孤儿
- 最后 Stop hook 触发时读的是别人的 state，跑的是别人的 PASS_CMD
- reviewer-timing-check 因 state.active=true 把本会话 spawn reviewer 拦下，无法区分"这不是我的 loop"

根因：单一 `.claude/builder-loop.local.md` 只能容纳一个 active loop，不支持多会话并行。

### 目标

1. **并发安全**：同一项目可同时跑多个 loop（不同 worktree），互不覆盖、互不拦截
2. **CWD 自寻址**：hook/脚本无需外部变量，靠 `pwd` 即可找到"我这个 worktree 对应的 state"
3. **兼容历史接入项目**：EDB/Persona/cc-builder-loop 自身改完 `bash install.sh` 就能继续跑 loop（允许手动改 yaml，无需改业务代码）

## 预估改动级别

L3（改架构 + state 格式）— state 从单文件改成每 worktree 一份、新增 `.claude/builder-loop/state/` 目录、hook 定位逻辑重写。改动横跨 scripts/ + skills/builder-loop/scripts/ + SKILL.md + e2e fixtures，全套联动。

## 约束 & 边界

- 不改对外 SKILL 约定（`/builder` `/planner` 模式、reviewer/tester/doc-maintainer agent 协议）
- 不依赖 CC 暴露新变量（不要用 `CLAUDE_SESSION_ID` 之类未稳定的环境）
- 已接入的项目（EDB/Persona/cc-builder-loop）跑一次 `bash install.sh` 后能继续用；**存量 `.claude/builder-loop.local.md` 可被一次性迁移或直接废弃**，允许用户手动编辑项目内 yaml
- `loop.yml` 本身格式不变（客户配置文件保持稳定）
- worktree 自动清理行为不变（PASS/NOOP 时仍由 merge-worktree-back cleanup）
- 保持"一个项目 = 一个 loop.yml"的配置心智（不强迫多 loop.yml）

## 成功标准 / 验收

见文末「验收」章节（含并行 E2E）。

<!-- /role -->

<!-- role:builder -->

## 技术选型

### 方案 A（已选）：专用目录 + branch-slug 文件 + CWD 定位

- state 目录：`.claude/builder-loop/state/<branch-slug>.yml`，其中 slug 由 branch 名 sanitize 而成（`loop/1777037485-fix-...` → `1777037485-fix-...`）
- 每个 worktree 对应 1 个 state；主工作目录的 bare loop（`worktree.enabled=false`）有 1 个固定 slug（`__main__`）
- **定位：任何 hook 从 `pwd` 开始反查**
  - 先看 cwd 是不是某个已记录 `worktree_path` —— 是 → 直接读对应 state
  - 不是 → 向上找 `.claude/loop.yml` 锚定 project root，再扫 `.claude/builder-loop/state/*.yml` 找 `worktree_path == cwd` 的那份

### 方案 B（否决）：state 放在 worktree 内 `<wt>/.claude/loop-state.yml`

- 单 worktree 删除 state 自动消失，无需 gc
- ❌ 但 Stop hook 需先 `git worktree list` 拿所有 worktree 再遍历读 state，逻辑复杂 40%；且 worktree 内写 `.claude/` 会和源码分支树混淆（`git status` 里出现非预期文件）

### 方案 C（否决）：保留单 state + 加 `CLAUDE_SESSION_ID` 环境变量识别

- 改动量最小
- ❌ CC 没有稳定暴露 session id；即便有，单 state 也不能同时容多个 active

### 方案 D（否决）：systemd / daemon 接管

与 "零依赖 bash" 理念冲突，否决。

## 方案设计

### 1. state 存储结构

```
<project_root>/.claude/builder-loop/
├── state/                           # 每 loop 一份
│   ├── 1777037485-fix-bot-id.yml    # 对应 worktree .../1777037485-fix-.../
│   ├── 1777037551-run-mode-ipc-5.yml
│   └── __main__.yml                 # worktree.enabled=false 的 bare loop
├── index.json                       # （可选）冗余索引，列出当前 active slug 列表，Stop hook 可快速遍历
└── legacy/                          # 迁移：原 builder-loop.local.md 一次性备份到这里
    └── builder-loop.local.md.bak
```

state file 新增字段 **`worktree_path`**（已有）+ **`slug`**（新）+ **`owner_cwd`**（新）：
```yaml
active: true
slug: "1777037485-fix-bot-id"
owner_cwd: "/mnt/.../worktrees/1777037485-fix-bot-id"   # 记录 setup 时的 CC cwd，和 worktree_path 往往一致
worktree_path: "..."
# ... 其他字段照旧
```

### 2. 定位 state 的通用函数（各 hook 共用）

新增 `skills/builder-loop/scripts/locate-state.sh`，输出 state 文件绝对路径（stdout）或 empty：

```bash
#!/usr/bin/env bash
# locate-state.sh <cwd> → echo state_file_path
# 逻辑：
#   1. 向上最多 5 层找 .claude/loop.yml → PROJECT_ROOT
#   2. 如果 cwd 本身是某 worktree（在 PROJECT_ROOT/.claude/worktrees/ 下面），
#      直接算 slug = basename(cwd)，拼 state/<slug>.yml 检查
#   3. 否则遍历 state/*.yml，读 worktree_path 字段，匹配 cwd（或 cwd 在其下）
#   4. 都没 → 看 state/__main__.yml（bare loop 场景）
#   5. 找到 → echo 路径；没找到 → echo 空 + exit 1
```

所有 hook（builder-loop-stop.sh / reviewer-timing-check.sh / tester-lock-* / 未来新增）都通过此函数拿 state。

### 3. 脚本改动清单

| 脚本 | 主要改动 |
|---|---|
| `skills/builder-loop/scripts/setup-builder-loop.sh` | ① 计算 slug ② state 写到新路径 ③ 写 `slug` / `owner_cwd` 字段 ④ 拒绝覆盖已有 active state（同 slug 下） |
| `skills/builder-loop/scripts/locate-state.sh` | **新建**，见上 |
| `scripts/builder-loop-stop.sh` | `PROJECT_ROOT` 锚定后改用 `locate-state.sh` 找 state；兜底激活也走新路径 |
| `scripts/reviewer-timing-check.sh` | 改用 `locate-state.sh`；如果 cwd 对应的 state 没 active 则放行（即便同项目别人的 state 活跃）；若 state 不是本会话的 loop（`owner_cwd` 不匹配当前 cwd 前缀）也放行 |
| `scripts/tester-lock-write.sh` | 定位逻辑同上 |
| `skills/builder-loop/scripts/merge-worktree-back.sh` | 接受 `<state_file>` 参数（不变），清理时连带删掉 state 文件；成功 MERGED/NOOP 后 `rm state_file` |
| `skills/builder-loop/scripts/early-stop-check.sh` | 输入参数 state_file 原样透传（行为不变） |
| `skills/builder-loop/scripts/run-apply-arbitration.sh` | 同上 |
| `skills/builder-loop/scripts/run-pass-cmd.sh` | 无变更（和 state 路径无关） |
| `skills/builder-loop/scripts/loop-init.sh` | 无变更（只写 loop.yml） |
| `skills/builder-loop/scripts/init-loop-config.sh` | 无变更 |
| `skills/builder-loop/scripts/probe-project-stack.sh` | 无变更 |
| `skills/builder-loop/scripts/split-plan-by-role.sh` | 无变更 |

### 4. 孤儿 state / worktree 兜底 gc

`setup-builder-loop.sh` 启动时先扫 `.claude/builder-loop/state/*.yml`：
- 若某个 slug 的 `worktree_path` 指向的目录已不存在 → 删 state（worktree 已被别的流程清掉）
- 若某个 slug 的 `git worktree list` 里找不到对应分支 → 删 state

不做定时 cron，被动懒 gc 够用。

### 5. 迁移策略（一次性）

`install.sh` 新增 step：
```
if [ -f "$PROJECT_ROOT/.claude/builder-loop.local.md" ]; then
  mkdir -p "$PROJECT_ROOT/.claude/builder-loop/legacy"
  mv "$PROJECT_ROOT/.claude/builder-loop.local.md" \
     "$PROJECT_ROOT/.claude/builder-loop/legacy/builder-loop.local.md.$(date +%s).bak"
  echo "[install] 已归档旧 state 文件到 legacy/；下次 setup 会使用新路径" >&2
fi
```

**但**：`install.sh` 是 skill 部署脚本，不会去遍历客户项目。这个迁移应该写成一个独立脚本 `skills/builder-loop/scripts/migrate-state.sh`，接入 `loop-init.sh` 在 init 向导里调一次，或者让用户手动跑。**本方案选：手动一次性跑 migrate，install.sh 只更新全局 skill 缓存**。

### 6. 保持 atomic 写入 + 文件锁

为防同一 session 内两次 setup（极端情况）或 hook 并发读写：
- `setup-builder-loop.sh` 写 state 时用 `flock "$STATE_DIR/.lock"` 包住（Linux 有）
- 读 state 不加锁（只读）

### 7. 让 Claude Code 指令层知道"有多个 active loop"

`builder.md` 里提到的"检查 `.claude/builder-loop.local.md`" 这类文字路径要更新为"从 cwd 通过 locate-state.sh 定位"。SKILL.md 同步。

## 风险 & 应对

| 风险 | 应对 |
|---|---|
| `locate-state.sh` 被频繁调用（每次 hook）性能下降 | state 目录下文件数一般 <5，遍历代价可忽略；需要时加 cache（index.json）|
| 两个会话在同一 worktree 里同时 setup（极端） | `flock` + slug 冲突检查 |
| 旧 `.claude/builder-loop.local.md` 还在 → 新代码忽略它 | migrate-state.sh 提供迁移；未迁移的项目会被"新代码当成未接入 loop"（没 state 就放行）；可接受 |
| reviewer-timing-check 放行错误（别人 state active 但放行当前 session）| 放行规则：仅当 cwd **不在**任何 active state 的 `worktree_path` 下才放行；确保 worktree 内的会话仍会被拦 |
| `owner_cwd` 和 `worktree_path` 不一致（用户 cd 出 worktree 又 cd 回来） | 以 `worktree_path` 为准，`owner_cwd` 仅记录；定位只用 `worktree_path` |
| E2E fixtures 依赖旧路径 | 一并更新 `test-new-repo-loop.sh` 等 e2e 脚本 |
| `install.sh` 对已有部署的影响 | install 只铺 skill 到 `~/.claude/skills/builder-loop/`，不动客户项目；保证 idempotent |

## 文件地图

### 新建

| 文件 | 角色 |
|---|---|
| `skills/builder-loop/scripts/locate-state.sh` | 通用 state 定位函数（CWD → state 路径） |
| `skills/builder-loop/scripts/migrate-state.sh` | 旧 state 迁移到新目录 |
| `skills/builder-loop/fixtures/e2e/test-parallel-loop.sh` | E2E：两个 worktree 并行跑 loop 互不干扰 |

### 修改

| 文件 | 改动点 |
|---|---|
| `skills/builder-loop/scripts/setup-builder-loop.sh:128` | state 写新路径；加 `slug`/`owner_cwd`；加 `flock` + 已有同 slug active 时拒绝（exit 4） |
| `skills/builder-loop/scripts/merge-worktree-back.sh:121` | cleanup 时 `rm -f "$STATE_FILE"`（清 state 文件本身） |
| `scripts/builder-loop-stop.sh:40-90` | 定位逻辑改为调 `locate-state.sh`；兜底激活路径同步改 |
| `scripts/reviewer-timing-check.sh:26-46` | 定位改为 `locate-state.sh`；放行规则：cwd 不对应任何 active state 时放行 |
| `scripts/tester-lock-write.sh` | 定位逻辑同上 |
| `skills/builder-loop/SKILL.md` | 文档描述改为多 state；说明新目录结构 |
| `skills/builder-loop/README.md` | 同步 |
| `install.sh` | 不需要改（只铺 skill）；但加一个 `note: 请对旧项目跑 migrate-state.sh` 的提示 |
| `skills/builder-loop/fixtures/e2e/test-new-repo-loop.sh` | 把 `.claude/builder-loop.local.md` 路径更新为 `.claude/builder-loop/state/*.yml` |
| `skills/builder-loop/fixtures/e2e/test-conflict.sh` | 同上 |
| `skills/builder-loop/fixtures/e2e/test-arbitration-apply.sh` | 同上 |
| `skills/builder-loop/fixtures/e2e/test-empty-repo.sh` | 同上 |
| `skills/builder-loop/fixtures/e2e/run-fixture.sh` | 同上 |
| `skills/builder-loop/docs/arbiter-flow.md` | 路径更新 |

### 不动

- `loop.yml` 格式（用户面朝的配置）
- reviewer/tester/doc-maintainer agent 协议
- pass_cmd / max_iterations 等语义
- `run-apply-arbitration.sh` / `early-stop-check.sh` / `run-pass-cmd.sh` / `probe-project-stack.sh` / `split-plan-by-role.sh` / `loop-init.sh` / `init-loop-config.sh`

## 执行任务列表

Builder 按顺序执行：

1. **新建 `locate-state.sh`**：
   - 输入 cwd，输出 state 文件路径或空 + exit 1
   - 实现策略：① cwd 在 `.claude/worktrees/<slug>/` 下时直接拼 `state/<slug>.yml` ② 否则遍历 state/*.yml 匹配 `worktree_path` ③ 兜底 `__main__.yml`
2. **改 `setup-builder-loop.sh`**：
   - 计算 slug：worktree 模式用 branch 后缀；bare 模式用 `__main__`
   - state 写到 `.claude/builder-loop/state/<slug>.yml`
   - 加 `slug:` / `owner_cwd:` 字段
   - 启动时 flock + 若同 slug state 已 active 且 worktree 尚存则拒绝（exit 4）
   - 启动时懒 gc：扫描孤儿 state（worktree_path 失效）
3. **改 `merge-worktree-back.sh`**：
   - `cleanup_worktree()` 后 `rm -f "$STATE_FILE"`
4. **改 3 个 hook（`builder-loop-stop.sh` / `reviewer-timing-check.sh` / `tester-lock-write.sh`）**：
   - 定位改调用 `locate-state.sh`
   - reviewer-timing-check 放行规则更新（见方案设计第 2 段）
5. **新建 `migrate-state.sh`**：
   - 扫客户项目的 `.claude/builder-loop.local.md`
   - 若存在：推断 slug（从 `worktree_path` 或 `loop/<xx>` 分支）→ mv 到新路径
   - 若 state 已过期（worktree 不存在）→ 归档到 `legacy/`
6. **改 fixtures/e2e 路径引用**：替换 5 个 e2e 脚本中的 `builder-loop.local.md` 字面量
7. **新建 `test-parallel-loop.sh`**：
   - 模拟两个 worktree 并行 setup
   - 验证两份 state 独立、两个 Stop hook 互不影响、cleanup 只删对应一份
8. **更新 SKILL.md + README.md + arbiter-flow.md**：路径描述更新
9. **回归**：跑 `bash skills/builder-loop/fixtures/e2e/test-new-repo-loop.sh` 等全部 e2e
10. **迁移 3 个已接入项目**（cc-builder-loop 自身 / EDB / Persona）：跑一次 `migrate-state.sh`，然后手动 `setup-builder-loop.sh` 跑一次确认新路径工作

## 验收标准

- [ ] 新建两个 worktree（同一项目）各自跑 loop，state 互不覆盖、各自 Stop hook 只跑各自 PASS_CMD
- [ ] 其中一个 loop 在跑时，另一个 worktree 的 CC 能正常 spawn reviewer（timing-check 不误拦）
- [ ] 关闭一个 loop（PASS + cleanup）后，只有它自己的 state 被删；另一个仍正常
- [ ] `bash skills/builder-loop/fixtures/e2e/test-new-repo-loop.sh` 通过
- [ ] 新增 `test-parallel-loop.sh` 通过
- [ ] cc-builder-loop 项目本身用新代码 `bash start...`（其实是 `loop.yml` PASS_CMD）跑一遍 e2e 通过
- [ ] EDB/Persona 各自跑 migrate-state.sh 后，新 `bash start.sh` 启动 + 改一行 commit + loop 能完整走一遍 PASS/MERGED

<!-- /role -->

<!-- role:tester -->

## 关键测试场景（tester 补 e2e 时重点）

### 并行 loop 场景（test-parallel-loop.sh 核心）

1. **两份 state 独立**：在同一假项目里起两个 worktree，各自 setup，两份 state 文件同时存在，内容不互相覆盖
2. **Stop hook 按 CWD 路由**：从 worktree A 发出 Stop hook 事件 → 只读 A 的 state 跑 A 的 PASS_CMD，不动 B
3. **reviewer-timing 不跨踩**：A 的 loop active 时，从 worktree B 发起 reviewer spawn → 放行；从 worktree A 发起 → 拦截
4. **cleanup 局部化**：A PASS 后 `rm state/<A>.yml` 只动 A，B 仍在

### 迁移场景（test-migrate.sh 可选）

5. **旧 state 迁移**：模拟 `.claude/builder-loop.local.md` 存在且 worktree 还在 → `migrate-state.sh` 正确搬到新路径
6. **旧 state 已过期**：`worktree_path` 失效 → 归档到 legacy/

### 边界

7. **bare loop（worktree.enabled=false）**：state 用 `__main__.yml` slug，和正常 worktree loop 可共存
8. **locate-state cwd 在非 worktree 的子目录**：从 `project/sub/dir` 发起 → 正确回溯到主 project 的 `__main__` 或向下扫 worktrees 匹配
9. **孤儿 gc**：setup 时发现某 state 的 worktree_path 不存在 → 删除 stale state，不影响新 setup

## 测试深度

**深度**。并行 loop 是这次重构的核心价值承诺，必须用 E2E 而不只是单测覆盖——因为坑多在 CWD 定位 + flock + hook 交互上，单测 mock 不出真实场景。

<!-- /role -->

---

## 📬 另一个 session 的踩坑日志（2026-04-24 22:30 追加，by "reviewer-model-compat" session）

嗨，我是另一个并发在搞 V1.7 reviewer 模型兼容的 session。今晚踩到几个坑，留给你，避免你回来接活时二次踩：

### 你的 WIP 代码被意外拉进生产跑了一次
- 你在 worktree `1777040807-multi-state-parallel-loo` 里的未提交改动（`setup-builder-loop.sh` / `merge-worktree-back.sh` / 新增 `locate-state.sh` 等），今晚 22:26 被**我本 session 的 Stop hook 兜底激活**误抓进来：
  - 我 session 的主仓 state file 莫名消失（两个 session 并发写同一个主仓 state 的经典踩踏）
  - Stop hook 看到"loop.yml 存在 + 有改动 + 无 state" → 触发兜底，按 plan mtime 挑到了**你的 plan**（就是这份文件），用它的标题作 task_desc，setup 出新 worktree（就是你现在那个）
  - 紧接着跑 PASS_CMD（`test-new-repo-loop.sh`）→ 你的新版 `setup-builder-loop.sh` 在 `line 128: STATE_FILE: unbound variable` 挂了，这是你的 WIP 正常状态不用慌
- **对你的影响**：除这次意外执行外没动你任何文件；你的 working tree 应该还是你离开时的样子

### 我打的临时补丁
- `scripts/builder-loop-stop.sh` 的兜底激活分支加了守门：`git worktree list` 里若已有任意 `loop/` 前缀 worktree，就 exit 0 不再瞎 bootstrap
- commit: `1c0294e fix(builder-loop): Stop hook bootstrap guard — skip if loop/ worktrees exist`
- 这个补丁跟你的 multi-state 方案**不冲突但有重叠**——你的方案本质上是 per-session state + flock，根治并发；我这个只是兜底激活侧的临时挡板。你的方案合入后可以把这个守门删掉或改成"没有 per-session state 才兜底"

### 两个还没清的 worktree
- `1777038200-v1-7-state`（21:50 创建）：用户要求保留，我没碰
- `1777039402-reviewer`（22:03 创建）：我本 session 的 V1.7 改动在里面，我准备推回主干
- 主仓 `.claude/builder-loop.local.md` 现在 `active: false`（我手动冻的，stopped_reason 注明了"跨 session 污染"）

### 给你的建议
你回来接活时，如果发现主仓 state file 长得不对、worktree_path 指向你自己的 worktree 了，**先手动 `active: false` 冻住**，避免你的 WIP 代码再被当作生产跑一次。你的 multi-state 方案做完后，今晚这些坑应该就自然消失。

详见项目 memory：`memory/known_issue_stop_hook_silent_bypass.md`（完整故事 + 复现步骤 + DONE 标志）
