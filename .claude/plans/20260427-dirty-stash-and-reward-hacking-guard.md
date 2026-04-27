# 方案：主仓 dirty 安全入 worktree + Reward Hacking 拦截（V2.3）

<!-- role:shared -->

## 背景 & 目标

session `3ed02147` 暴露 builder-loop 两条结构性裂缝，本期合并修复：

**裂缝 1：setup 默认走 worktree 但忽略主仓未 commit 改动**
- `setup-builder-loop.sh:196` 写死 `git worktree add ... HEAD`
- 用户主仓有 unstaged/untracked 改动 + 手动调 setup → worktree 从 HEAD 拉 → 改动**静默丢失**
- bootstrap 路径用 `--no-worktree` 绕过此问题，但手动入口（builder.md 步骤 1 / 用户主动）默认走 worktree → 三入口三行为，分叉

**裂缝 2：builder 在 PASS_CMD 配置加 reruns 等 reward hacking 关键词**
- 同 session 出现 `--reruns 2 --reruns-delay 1` 加进 `loop.yml.pass_cmd` 跑通 flaky → 实质上是用重试软化 PASS 判据
- judge agent 现有 reward hacking 检测仅看测试代码 diff，不看 `loop.yml` 配置 diff
- builder.md 没有"修改 pass_cmd 命令字符串属于敏感操作"的硬约束

### 成功标准

| 序号 | 标准 |
|---|---|
| S1 | 主仓 dirty + 手动调 setup → 自动 stash + worktree pop，主仓 working tree 变 clean，worktree 内可见原 dirty 改动（unstaged）|
| S2 | stash 失败（rebase/merge 进行中、submodule、lfs corner case）→ setup exit 2，stderr 给出明确人工处理建议，不丢数据 |
| S3 | EARLY_STOP / 异常退出 → 保留 worktree + worktree branch（不删），主仓还原原 dirty（apply 不 pop）|
| S4 | PASS 合回主仓 → 主仓 stash 自动 drop（已通过 worktree commit 合回），auto-commit message 明文列出"含主仓预存改动：file1, file2" |
| S5 | judge agent 扫描 git diff 命中 `--reruns / xfail / skip / pytest.mark.flaky / -k 'not'` 等关键词出现在 `loop.yml` 改动里 → confidence=0.3 + reason=suspected_reward_hack |
| S6 | 命中后 stop hook 注入 stderr，要求 builder 三选项 AskUserQuestion 二次确认（quarantine / 修测试 / 保留 cmd 改动）|
| S7 | e2e fixture 覆盖 S1–S6 + 兼容回归 |

## 预估改动级别

**L3**：state schema 新增字段 + 5 个脚本协作改造 + judge prompt 扩展 + builder.md 提示词增量。涉及向后兼容（V2.2 → V2.3 state 升级）。

## 约束 & 边界

- **多 builder 兼容**（留扩展点不验证）：stash 必须用 commit hash（如 `git stash create` 输出）而非 `stash@{N}` index；state 字段不与 slug 同名；错误路径不假设"唯一活跃 loop"
- **bootstrap 路径不变**：`builder-loop-stop.sh:213` `--no-worktree` 走 bare 模式不受本期影响
- **bare 模式不变**：`worktree.enabled=false` 或 `--no-worktree` 显式参数 → 跳过 stash 逻辑直接跑主仓
- **loop.yml schema 不破坏**：本期不动 `pass_cmd` / `worktree` / `judge` 段；reward hacking 关键词清单写进 `judge-system.md` system prompt + `run-judge-agent.sh` 一份本地正则兜底
- **回退路径**：`loop.yml.judge.reward_hacking_detection: false` 显式禁用 reward hacking 扫描；setup 的 dirty stash 行为可通过 `--no-stash` flag 跳过（直接走 bare）

## 验收标准

| 序号 | 验收方法 |
|---|---|
| V1 | `test-dirty-stash-flow.sh` 一次跑通 5 个 case：clean+worktree / dirty+worktree+stash / dirty+rebase 进行中拒绝 / dirty+--no-stash 降级 bare / dirty+stash+EARLY_STOP 还原 |
| V2 | `test-reward-hacking-detect.sh` 一次跑通 4 个 case：pass_cmd 加 --reruns 命中 / pass_cmd 加 xfail 命中 / 测试代码加 @pytest.mark.flaky 命中 / 普通改动不命中 |
| V3 | 现有 fixture 全过（特别是 `test-pass-cmd-runs-worktree.sh` / `test-bare-loop-merge.sh` / `test-judge-*` 不回归）|
| V4 | CLAUDE.md §5 增补 V2.3 条目；`docs/judge-agent.md` 加 reward hacking 扩展段；`known-risks.md` R1 标记本期已扩展到 pass_cmd 配置 diff |

<!-- /role -->

<!-- role:builder -->

## 技术选型

### 选型 1：dirty 改动入 worktree 的方式

| 方案 | 评分 | 取舍 |
|---|---|---|
| **A. stash + pop（推荐）** | ★★★ | git 原生机制；commit hash 引用避免多 builder 串味；EARLY_STOP 还原成本中等 |
| B. cp -a 同步 | ★ | 同份代码两处 dirty，merge 时撞 ff，心智成本高 |
| C. bare 模式（自动降级） | ★★ | 失去 worktree 隔离收益；race 风险；bootstrap 已覆盖此分支 |

**选 A**。具体：
- 用 `git stash create` 拿 commit hash → `git stash store -m "builder-loop:auto:slug=..." <hash>`，避免 stash@{N} 索引在多 builder 场景串味
- worktree 创建后用 `git -C $WORKTREE_PATH stash apply <hash>`（不 pop），保留主仓 stash 副本以防 worktree 异常时回滚
- PASS 后 merge-worktree-back 路径 drop 主仓 stash（已通过 worktree commit 合回，副本冗余）

### 选型 2：reward hacking 检测落点

| 方案 | 评分 | 取舍 |
|---|---|---|
| **A. judge agent system prompt + 本地正则兜底（推荐）** | ★★★ | LLM 判据 + 静态规则双层；与 V1.9 judge 架构一致；可通过 `loop.yml.judge.reward_hacking_detection: false` 关 |
| B. 独立 hook 脚本 | ★ | 多一层 hook 注册成本；与 judge 重复扫 diff |
| C. 仅 builder.md 提示词约束 | ★ | 软约束无机器判定，没有 stop hook 注入路径 → 拦截力弱 |

**选 A**。两层判据：
- **Layer 1（LLM）**：judge-system.md system prompt 增补一段"diff 中出现 `--reruns / xfail / skip / pytest.mark.flaky / -k 'not'` 等关键词，且文件命中 `loop.yml` / `pyproject.toml` / `pytest.ini` 等 PASS_CMD 配置时，输出 reason=suspected_reward_hack + confidence ≤ 0.3"
- **Layer 2（正则兜底）**：`run-judge-agent.sh` 本地扫 diff 命中关键词 → 强制覆盖 confidence ≤ 0.3，防 LLM 漏判
- 命中后 stop hook 注入 stderr：`[builder-loop reward-hack-guard] 检测到 X 关键词在 Y 文件。请用 AskUserQuestion 列出三选项：① quarantine 该测试 ② 修测试根因 ③ 保留 cmd 改动（需提供必要性理由）`

### 选型 3：EARLY_STOP / 异常退出的现场保留策略

用户已确认选"保留 builder 改动 + 还原原 dirty"。具体实现：

```
EARLY_STOP / 异常退出钩子序列：
1. worktree branch / worktree path 都不删（git worktree list 仍可见）
2. 切主仓 → git stash apply <pre_loop_stash_ref>（不 pop，stash 副本保留）
3. 写入 .claude/builder-loop/legacy/<ts>-early-stop-<slug>.bak（含 worktree path / stash ref / iter / reason）
4. exit 2 + stderr 注入：「现场已保留：worktree=<path>, 主仓 stash=<short_hash>, 详情见 legacy bak」
```

GC 责任：用户人工 / 后续单独 GC 工具（不在本期）。

## 方案设计

### 数据模型变更（state schema V2.3）

`.claude/builder-loop/state/<slug>.yml` 新增字段：

```yaml
# V2.3 新增（向后兼容：缺字段视为 bare 模式或纯 clean 起点）
pre_loop_stash_ref: ""        # git stash commit hash（worktree-dirty 模式才填）
pre_loop_dirty_files: ""      # 进 stash 的文件列表，逗号分隔（merge commit message 用）
worktree_mode: "clean"        # clean | dirty | bare（语义清晰化，老 state 缺字段按 bootstrap 旧行为推断）
```

**兼容性**：
- 老 V2.2 state 缺三字段 → `worktree_mode` 推断：有 `worktree_path` 非空 → "clean"，空 → "bare"；`pre_loop_stash_ref` 空 → 跳过 EARLY_STOP 还原逻辑
- migrate-state.sh 不强制升级，新 state 写新 schema、读老 state 时按缺字段处理

### 核心流程

#### 流程 A：setup 入口判定（裂缝 1 修复）

```
setup-builder-loop.sh：
├─ pre-flight 检查（新增）
│  ├─ git status --porcelain → CHANGED_FILES 数组
│  ├─ git rev-parse --git-path MERGE_HEAD / REBASE_HEAD / CHERRY_PICK_HEAD → 任一存在 = 特殊状态
│  ├─ git submodule foreach git status --porcelain → submodule dirty 检查
│  └─ 决策：
│     ├─ CHANGED_FILES=[] → worktree_mode=clean，走原 worktree 路径
│     ├─ CHANGED_FILES=[*] + 特殊状态 → exit 2 "git 状态特殊（rebase 等），请手动结束后重试"
│     ├─ CHANGED_FILES=[*] + clean 状态 + worktree.enabled=true → worktree_mode=dirty，走 stash + apply
│     ├─ CHANGED_FILES=[*] + worktree.enabled=false 或 --no-worktree → worktree_mode=bare，走原 bare 路径
│     └─ --no-stash flag（用户显式 opt-out）→ worktree_mode=bare（dirty 留主仓）
│
├─ stash 阶段（worktree_mode=dirty 才执行）
│  ├─ STASH_HASH=$(git stash create) → 失败 exit 2
│  ├─ STASH_REF="builder-loop:auto:slug=${SLUG}:ts=${TS}"
│  ├─ git stash store -m "$STASH_REF" "$STASH_HASH"
│  ├─ git checkout -- . && git clean -fd → 主仓 working tree clean
│  └─ stderr 提示用户「📦 主仓 dirty 已 stash → stash@{0}: ${STASH_REF}（hash=${STASH_HASH:0:8}）」
│
├─ worktree 创建（不变）
│  └─ git worktree add -b $BRANCH $WORKTREE_PATH HEAD
│
├─ stash apply 到 worktree（worktree_mode=dirty 才执行）
│  ├─ git -C $WORKTREE_PATH stash apply $STASH_HASH（不 pop，副本保留）
│  ├─ 失败 → 还原（rm worktree, branch -D, 主仓 stash pop）→ exit 2
│  └─ 成功 → stderr 提示「✅ stash 已 apply 到 worktree（unstaged 形态）」
│
└─ 写 state（新增三字段）
```

#### 流程 B：merge-worktree-back PASS 路径（drop 主仓 stash）

```
merge-worktree-back.sh PASS 分支：
├─ git add -A && git commit（不变；含原 dirty + builder 新改动）
├─ commit message 增强（新增）：
│  ├─ 若 state.pre_loop_dirty_files 非空 → message 末尾加：
│  │  "（含主仓预存改动：foo.py, bar.py）"
│  └─ 让用户 PR review 时一眼看到边界
├─ rebase / ff merge（不变）
├─ drop 主仓 stash（新增）：
│  ├─ state.pre_loop_stash_ref 非空 → git -C $PROJECT_ROOT stash drop <hash>
│  └─ 失败仅 warn 不阻断（stash 可能被用户手动清掉了）
└─ cleanup（不变）
```

#### 流程 C：EARLY_STOP / 异常退出还原

```
builder-loop-stop.sh EARLY_STOP 分支 + run-apply-arbitration.sh 失败分支：
├─ 不删 worktree / 不删 branch
├─ state.pre_loop_stash_ref 非空 → git -C $PROJECT_ROOT stash apply <hash>（不 pop）
│  ├─ 主仓现场被 builder 中途修改可能 apply 冲突 → warn + 继续（用户自行处理）
│  └─ 成功 → stderr「主仓 dirty 已还原（stash 副本仍保留）」
├─ 写 .claude/builder-loop/legacy/<ts>-early-stop-<slug>.bak（迁 state + worktree path + stash ref）
└─ exit 2 注入「现场已保留：worktree=$WORKTREE_PATH, stash=$STASH_HASH」
```

#### 流程 D：reward hacking 检测（裂缝 2 修复）

```
run-judge-agent.sh phase 2（构造 prompt 前）新增：
├─ 扫描 git diff（PASS 分支：worktree HEAD~..HEAD；FAIL 分支：worktree 当前 unstaged）
├─ 命中规则：
│  ├─ 文件路径匹配 ^(\.claude/loop\.yml|pyproject\.toml|pytest\.ini|setup\.cfg|conftest\.py)$
│  └─ AND diff 内容匹配 (--reruns|@pytest\.mark\.flaky|@flaky|xfail|pytest\.skip|-k\s+['\"]not |@unittest\.skip)
├─ 命中 → 设置 LOCAL_REWARD_HACK_FLAG=1
└─ 用作 confidence 上限：min(LLM_confidence, 0.3) when flag 命中
              + 注入 reason="suspected_reward_hack"
              + 注入 nudge_text 三选项模板（quarantine / 修测试 / 保留 cmd）
```

`builder-loop-stop.sh` 收到 judge 输出 `reason=suspected_reward_hack` 时不直接 PASS，注入 stderr 强制 builder 走 AskUserQuestion 三选项。

## 文件地图

### 修改的存量文件

| 文件 | 改动点 | 行数估算 |
|---|---|---|
| `skills/builder-loop/scripts/setup-builder-loop.sh` | 加 pre-flight 判定函数 + stash store + apply 阶段 + state 新字段写入 | +120 行 |
| `skills/builder-loop/scripts/merge-worktree-back.sh` | PASS 分支 commit message 增强 + stash drop | +20 行 |
| `skills/builder-loop/scripts/early-stop-check.sh` 或 `builder-loop-stop.sh` 的 EARLY_STOP 分支 | EARLY_STOP 还原主仓 dirty + legacy bak 增字段 | +30 行 |
| `skills/builder-loop/scripts/run-apply-arbitration.sh` | arbiter 失败路径调用 EARLY_STOP 还原 | +10 行 |
| `skills/builder-loop/scripts/run-judge-agent.sh` | reward hacking 关键词扫描 + confidence 上限钳制 | +50 行 |
| `skills/builder-loop/prompts/judge-system.md` | system prompt 增补 reward hacking 段 | +20 行 |
| `skills/builder-loop/scripts/migrate-state.sh` | 老 state 兼容（缺字段时设默认值） | +15 行 |
| `~/.claude/commands/builder.md`（dotfiles 仓） | 增补硬约束："改 loop.yml.pass_cmd 字符串属敏感操作"，**控制在 80 字以内** | +5 行 |
| `skills/builder-loop/docs/judge-agent.md` | reward hacking 扩展段 | +30 行 |
| `skills/builder-loop/known-risks.md` | R1 标记已扩展 | +5 行 |
| `CLAUDE.md` §5 | V2.3 条目（按 V2.2 同样格式） | +15 行 |

### 新增文件

| 文件 | 用途 |
|---|---|
| `skills/builder-loop/fixtures/e2e/test-dirty-stash-flow.sh` | 5 个 case 覆盖 S1–S4 |
| `skills/builder-loop/fixtures/e2e/test-reward-hacking-detect.sh` | 4 个 case 覆盖 S5–S6 |

### 不动的文件

- `scripts/builder-loop-stop.sh`（仅 EARLY_STOP 分支几行调用，主流程不动）
- `scripts/tester-*.sh` / `scripts/reviewer-timing-check.sh`（hook 脚本不涉及）
- `agents/tester.md` / `agents/arbiter.md`（subagent prompt 不变）
- `install.sh` / `uninstall.sh`（无新脚本注册 hook）

## 执行任务列表

> 按依赖顺序，每步独立 commit。每个 commit 走完整 PASS_CMD（自身 e2e 含 V3 兼容回归）。

**Step 1：state schema V2.3 + 兼容**
- 改 `setup-builder-loop.sh`：state YAML 写入新增 `pre_loop_stash_ref` / `pre_loop_dirty_files` / `worktree_mode` 三字段（暂时全填默认值，不动现有逻辑）
- 改 `migrate-state.sh`：读老 state 缺字段时按 worktree_path 推断 `worktree_mode`
- 改 `merge-worktree-back.sh` + `builder-loop-stop.sh`：读 state 时容忍三字段缺失
- 完工：现有 fixture 全过（V3）

**Step 2：dirty 检测 + stash + worktree apply 主流程**
- 改 `setup-builder-loop.sh` 加 pre-flight 判定函数（git status / git path 检测特殊状态 / submodule）
- 加 stash store（commit hash 形式）+ worktree apply 阶段
- stash 失败 → exit 2 + 文案
- 加 `--no-stash` flag
- 完工：S1 + S2 通过

**Step 3：merge PASS drop stash + commit message 增强**
- 改 `merge-worktree-back.sh` PASS 分支
- commit message 末尾追加"（含主仓预存改动：...）"
- 完工：S4 通过

**Step 4：EARLY_STOP 现场保留 + 主仓还原**
- 改 `builder-loop-stop.sh` EARLY_STOP 分支
- 改 `run-apply-arbitration.sh` 失败分支
- legacy bak 加字段
- 完工：S3 通过

**Step 5：reward hacking 检测**
- 改 `run-judge-agent.sh` 加正则扫描 + confidence 钳制
- 改 `prompts/judge-system.md` 加 system prompt 段
- 改 `builder-loop-stop.sh` 接收 `reason=suspected_reward_hack` 时注入 stderr 三选项模板
- 改 `~/.claude/commands/builder.md`（dotfiles 仓）加 1 条硬约束（≤80 字）
- 完工：S5 + S6 通过

**Step 6：e2e fixture**
- 写 `test-dirty-stash-flow.sh` 5 case
- 写 `test-reward-hacking-detect.sh` 4 case
- run-fixture.sh 加注册
- 完工：S7 通过

**Step 7：文档同步**
- CLAUDE.md §5 V2.3 条目
- docs/judge-agent.md reward hacking 扩展段
- known-risks.md R1 状态更新
- 同步 dotfiles 仓 commit（builder.md 改动）

**Step 8（可选 / 兜底）：状态机自检**
- 跑 `bash skills/builder-loop/fixtures/e2e/run-fixture.sh all` 全量过
- 手动验：cc-builder-loop 自身 dirty 状态下跑一次 setup（端到端 smoke）

## 风险 & 应对

| 风险 | 等级 | 应对 |
|---|---|---|
| stash apply 在 worktree 内冲突（HEAD 不一致） | 中 | 不会发生：worktree 刚从 HEAD 拉，stash 也基于同 HEAD。但兜底路径仍要写 apply 失败回滚（删 worktree + 主仓 stash pop）|
| 用户主仓 .git/info/exclude 或 .gitignore'd 文件 | 低 | `git stash -u` 默认不进 stash（符合预期），文档说明即可 |
| submodule dirty | 中 | pre-flight 检测到 submodule dirty → exit 2 拒绝（本期不支持），文档建议 `git submodule foreach git stash` 后重试 |
| LFS 文件 stash | 低 | `git stash` 对 LFS 兼容性 git 版本相关，文档列已知风险，命中时降级 bare |
| reward hacking 关键词清单不全 | 中 | 留 `loop.yml.judge.reward_hacking_keywords` 列表覆盖默认（默认 6 个关键词，用户可加） |
| Layer 1（LLM）和 Layer 2（正则）判据冲突 | 低 | 正则命中作为 confidence 上限钳制（覆盖 LLM 高 confidence），不冲突 |
| EARLY_STOP 还原时主仓被 builder 中途修改 | 中 | apply 失败仅 warn 不阻断；stash 副本不 drop，用户人工处理 |
| 老 V2.2 state 跑 V2.3 setup 报错 | 高 | migrate-state.sh 兼容层 + 缺字段默认值；新增 fixture `test-state-schema-compat.sh` 验证 |
| 多 builder 同时 setup 时 stash hash 串味 | 已规避 | 用 commit hash 不用 stash@{N} index；state 文件按 slug 区分；锁文件 per-slug |

### 退路

- **完全回滚 V2.3 dirty 流程**：`setup-builder-loop.sh` 加 `LOOP_NO_DIRTY_STASH=1` env 变量绕过整段 pre-flight，回 V2.2 行为
- **完全回滚 reward hacking 检测**：`loop.yml.judge.reward_hacking_detection: false` 直接关
- **整段 V2.3 回滚**：`git revert <V2.3 commits>` + 老 state 自动通过 migrate-state.sh 兼容层

## 演进路径 & 扩展预留

### 留给未来（不在本期）

- **多 builder 并行验证**：本期已用 commit hash 形式 stash + per-slug 锁，扩展点齐备；未来加 `test-multi-builder-parallel.sh` fixture 验证
- **arbiter 跨 worktree MR 冲突仲裁**：worktree A 合并后 worktree B rebase 冲突，arbiter 需扩展上下文读"已合并 worktree 的近 N 个 commit"。改 `run-apply-arbitration.sh` + `agents/arbiter.md`
- **PASS / merge 时拆分 commit**：本期把"原 dirty + builder 改动"合并提交，未来可拆 user_baseline + builder_iter 两个 commit，PR review 视角更清晰
- **GC legacy bak**：本期 EARLY_STOP 留现场永不删，未来加 `gc-legacy.sh` 按时间 / 数量 GC

<!-- /role -->

<!-- role:tester -->

## 测试计划

### 测试目标

验证 V2.3 两条新流程（dirty stash + reward hacking 检测）和兼容性：

1. **dirty stash 流程**：S1-S4 路径正确性 + 异常分支（stash 失败 / EARLY_STOP）数据安全
2. **reward hacking 检测**：S5-S6 的命中 / 不命中两类 + 关键词扩展
3. **兼容性**：老 V2.2 state 在 V2.3 跑通，无破坏性升级

### 关键测试场景

#### `test-dirty-stash-flow.sh`（5 case，约 25 个 assert）

| Case | 场景 | 关键 assert |
|---|---|---|
| A1 | clean 主仓 + setup → 走原 worktree 路径，state.worktree_mode=clean | state 字段 / worktree 创建 / 主仓状态不变 |
| A2 | dirty 主仓（含 untracked + modified） + setup → stash + worktree apply | 主仓 working tree clean、worktree 内能 cat 出原 dirty 内容、state.pre_loop_stash_ref 非空、worktree_mode=dirty |
| A3 | 主仓在 rebase 进行中 + setup → exit 2 拒绝 | exit code=2 + stderr 含 "rebase" 关键字 + 主仓状态零改动 |
| A4 | dirty + `--no-stash` flag → 降级 bare 模式 | worktree_mode=bare、主仓 dirty 保留、跳过 stash 阶段 |
| A5 | dirty + setup 成功 + 模拟 EARLY_STOP（手动 max_iter 触发）→ worktree 保留 + 主仓还原 | worktree path 仍存在、worktree branch 仍存在、主仓 dirty 已还原（与 setup 前一致）、legacy bak 创建 |

#### `test-reward-hacking-detect.sh`（4 case，约 16 个 assert）

| Case | 场景 | 关键 assert |
|---|---|---|
| B1 | builder 改 `.claude/loop.yml` 加 `--reruns 2` → judge 命中 | judge-trace.jsonl 含 reason=suspected_reward_hack、confidence ≤ 0.3、stop hook stderr 注入三选项文案 |
| B2 | builder 改 `pyproject.toml` 加 xfail marker → 命中 | 同 B1，文件路径不同 |
| B3 | builder 改测试代码加 `@pytest.mark.flaky` 装饰器 → 命中（测试代码也算配置） | 同 B1，覆盖测试文件路径 |
| B4 | builder 改实现代码（普通 src/ 改动）→ 不命中 | judge-trace.jsonl 无 suspected_reward_hack reason、按原 PASS/FAIL 逻辑走 |

#### `test-state-schema-compat.sh`（2 case，约 8 个 assert）

| Case | 场景 | 关键 assert |
|---|---|---|
| C1 | 老 V2.2 state（缺三新字段）+ V2.3 setup 重入 | migrate-state.sh 不报错、新字段被写入默认值、worktree_mode 按 worktree_path 推断 |
| C2 | V2.3 state + V2.2 老脚本（向前兼容）| 老脚本读到三字段不报错（YAML 解析容错）|

### 测试深度

**深度**（与 V1.5–V2.2 现有 fixture 同等深度）：
- 真实 git 操作（不 mock）
- 完整 hook 链路触发（stop hook 接 setup → run-pass-cmd → run-judge-agent → merge-worktree-back）
- 跨进程 state 一致性验证
- legacy bak 文件结构 schema 校验

### 边界条件思路

| 边界 | 思考方向 |
|---|---|
| stash 时主仓 working tree 含 untracked 大文件（>1MB） | stash -u 应能处理，确认 hash 写入 state 不损坏 |
| 主仓 dirty 含 .gitignore 文件本身被改 | git stash -u 默认不收 .gitignore'd 文件，但 .gitignore 文件本身是 tracked 的，应该被 stash |
| dirty + 同时存在老 V2.2 active state（slug=__main__）| pre-flight 应在 slug 冲突检测之前还是之后？建议 slug 冲突检测优先（exit 4），然后才走 dirty 检测（exit 2） |
| reward hacking 关键词命中但 builder 在 commit message 写明"跳过 X 测试 因为 Y" | 本期不智能区分；用户可手工通过 AskUserQuestion 选项 3 保留 cmd 改动 |
| EARLY_STOP 时 `git stash apply` 主仓状态冲突（用户开了别的 IDE 改主仓）| 仅 warn 不阻断；stash 副本不 drop；文档说明 |
| reward hacking 假阴性（关键词没命中但实质是 reward hacking，如 `--ignore-glob`）| 本期接受；known-risks.md R1 持续追踪 |
| 多个 stash 嵌套（用户主仓本来就有手动 stash）| 用 commit hash 不用 stash@{N} index → 不串味 |

### 不需要测试

- bare 模式（worktree.enabled=false）→ 跳过整段 dirty 检测，行为不变（V3 现有 fixture 已覆盖）
- bootstrap 路径（自动 `--no-worktree`）→ 不走 dirty stash 分支（V3 现有 fixture 已覆盖）
- judge 凭证缺失 / API 超时 → 现有 V2.1 fixture `test-judge-env-file-load.sh` 已覆盖

<!-- /role -->
