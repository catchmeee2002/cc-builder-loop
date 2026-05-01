# V2.2 — Tester 跨目录写防御 + 复盘强制分类闸门 + Bootstrap 空转修复

<!-- role:shared -->

## 背景 & 目标

### 议题 1：tester subagent 跨目录写文件

**复现 session**：`283ee3b2-7a4d-4c36-987a-cb2c53766711`（hongyu_Repo 项目，2026-04-26）。

L3 阶段 spawn tester（cwd=worktree、prompt 顶部明写 `worktree_path` + 「请在该 worktree 中执行所有操作，不要在主 repo」）后，tester 仍把 5 处工具调用写到主仓绝对路径：

| tester 操作 | 路径 |
|-------------|------|
| Edit ×3 | `/hongyu_Repo/tests/test_reviewer.py`（主仓） |
| Bash | `git -C /hongyu_Repo checkout tests/test_reviewer.py`（主仓 reset） |
| Write | `/hongyu_Repo/tests/test_reviewer_advanced.py`（主仓新建） |
| Bash | `cp /主仓/tests/test_reviewer_advanced.py /worktree/.../tests/`（手动复制到 worktree） |
| Edit ×2 | `/worktree/.../tests/test_reviewer_advanced.py`（仅 worktree 副本生效）|

下游级联：
1. tester 走完后主仓 untracked `tests/test_reviewer_advanced.py`，worktree 同名文件已含后续微调（两版仅差一行断言措辞）
2. CC stop → stop hook 跑 PASS_CMD（worktree 内）→ PASS
3. `merge-worktree-back.sh` auto-commit worktree 内 untracked → 进 worktree branch (292b2b5)
4. ff-merge 主仓 main → 撞主仓同名 untracked → `ERROR ff-after-rebase-failed`
5. builder 排查 + 手动删主仓副本后重跑 merge 才闭环

**根因**：`agents/tester.md` 输入字段表（L17-22）**没有 `worktree_path`**；步骤 1-5 中**没有"路径根校验"自检**；CLAUDE.md §1 hook 表的 5 个 hook **没有针对 tester Write/Edit 的边界拦截**——builder 临时塞的 spawn prompt 提示是 ad-hoc 的，tester 角色契约没承接。

**目标**：
1. tester subagent 接收明确的 `worktree_path` 输入契约，所有 Write/Edit 路径以此为根
2. 物理硬门禁：PreToolUse Write|Edit hook 在 tester 锁活跃期拦截非 worktree 路径
3. 拦截后给精确诊断（路径 X 不在 worktree=Y 内、主仓=Z），让 tester 改路径自动重试

### 议题 2：复盘 A/B 分类闸门失效

**同 session 复盘环节**：builder 输出 2 条候选都打 5/5 推荐落 memory，跳过 A 类候选清单：

| 候选 | builder 判定 | 实际正确判定 |
|------|-------------|-------------|
| ff-merge 失败 → diff 主仓与 worktree 同名 untracked → 删主仓副本 | B（落 memory，5/5） | A2（cc-builder-loop 机制缺口，能用 hook 防住，应改 tester.md + 加 hook） |
| setup 主动改主仓 .gitignore，工作树持续脏 | B（落 memory，5/5） | A2（已是 V2.1.1 设计行为，CLAUDE.md §5 已记，应判"已交付"不立项） |

**根因**：builder.md L171-211 步骤 5 已写 A vs B 判据（L177「能不能写成检查/断言/脚本让程序自动防住？能 → A 类，不许只塞 memory」），但：
1. 判据是描述性段落，**位置在 5 问表格上方但不够 imperative**
2. 流程结构上没有"先分桶后 5 问"的阻断式闸门，5 问表格只服务 B/C，A 类被自然挤出
3. 缺反例锚点，模型容易把"未来还会发生的现象"理解为 B 类

**目标**：
1. 复盘**先分桶 A1/A2/B/C 四桶**，空桶必须显式写"无"+一句话理由，再对 B/C 走 5 问
2. 钉具体反例锚点降低误判率（如「跨目录写 → A2」「.gitignore 自愈 → 已交付不立项」）
3. A 桶候选直接定向到对应 `.claude/improvements.md`，不进 5 问表格，不进 memory

### 议题 3：Bootstrap 兜底激活的 NOOP 空转

**复现 session**：同 `283ee3b2`，阶段 0 闭环后两次连续兜底激活：

| 触发 | 时序 | HEAD 变化 | 实际结果 |
|------|------|----------|---------|
| 第 1 次兜底 | tester+merge 完毕、builder 准备收尾 | 292b2b5 → 292b2b5 | NOOP PASS、写游标=292b2b5、输出 reviewer 流程提示（builder 早已 spawn 过 reviewer） |
| 第 2 次兜底 | builder 手动 commit 02aec58 + 7feb39f 收尾后 | 292b2b5 → 7feb39f | NOOP PASS、写游标=7feb39f、输出 reviewer 流程提示、changed_files=[] 让 builder 自反应"无事可做" |

**根因 1：触发器太激进**

`builder-loop-stop.sh` L173-179：
```bash
HAS_DIFF="$(git -C "$PROJECT_ROOT" diff --stat ...)"
[ -z "$HAS_DIFF" ] && HAS_DIFF="$(git -C "$PROJECT_ROOT" diff --cached --stat ...)"
HAS_RECENT_COMMIT="$(git -C "$PROJECT_ROOT" log --since='30 minutes ago' ...)"
if [ -z "$HAS_DIFF" ] && [ -z "$HAS_RECENT_COMMIT" ]; then
    exit 0
fi
```

`HAS_RECENT_COMMIT` 任意 30 分钟内的 commit 都触发兜底——**不区分** "用户/builder 主动 commit 已收尾" vs "loop merge 进去待审"。

**根因 2：游标语义只覆盖 HEAD 完全静止**

L186-191 游标检查：仅当 `HAS_DIFF=空 + CURRENT_HEAD == LAST_HEAD` 才放行。`builder` 自己 commit 一次让 HEAD 前进就脱离游标保护，重新触发 bootstrap。

**根因 3（次要，连锁）：bootstrap 模式下 start_head 选错基线**

兜底激活 → `setup-builder-loop.sh --no-worktree` → `state.start_head` 写当前 HEAD（即兜底那一刻的 HEAD）→ L464 `git diff start_head..HEAD` 必然为空 → `changed_files=[]` → builder 收到 PASS_MSG 但 reviewer 无事可做。

即使根因 1 不修，根因 3 也独立造成"PASS_MSG 输出 + reviewer 流程空转"的下游浪费。

**目标**（用户已拍板 A 方向）：

砍 `HAS_RECENT_COMMIT` 触发器。bootstrap 兜底**只看 HAS_DIFF**（工作树未提交改动）。

**取舍**：
- 用户/builder 手动 commit 后工作树干净 → 自动 bootstrap 静默放行（loop 不再纠缠）
- 损失场景：用户在主仓直接改代码 + commit + 关 CC（不经 loop），loop 失去自动补 PASS_CMD 兜底——需要用户手动 `bash ~/.claude/skills/builder-loop/scripts/setup-builder-loop.sh "<task>"` 起 loop。**这是 A 方向的有意权衡**

## 预估改动级别

**L2** — 改 prompt + 改 hook + 加 fixture，无新签名/新模块；完全向后兼容（tester.md 缺 worktree_path 字段时旧行为照旧；hook 找不到 lock 时放行；bootstrap 触发器收敛只影响首次启动场景）。

按 L2 直接进 loop（不 spawn tester，因为本次改 builder-loop 自身的 prompt + hook，正是测试 V2.2 闭环的最佳载体——tester 对自身的修复用 V1.x 时序的话会成"鸡生蛋"循环，所以改完 hook 后直接由 e2e fixture 黑盒覆盖）。

## 约束 & 边界

- **不破坏现有 hook 行为**：tester-lock-{write,check,clear}.sh 现有职责（拦 Read|Grep|Glob 对 source_dirs）保持不变；新增的 Write|Edit 拦截走独立 hook 脚本（或扩展同一脚本走 matcher 分支）
- **路径白名单**：严格只允 worktree 内（`${worktree_path}/*`），其他路径一律 exit 2。tester 写 `/tmp` 之类临时文件场景需改用 Bash echo 或不落盘
- **拦截范围只限 tester**：reviewer / doc-maintainer / arbiter / 主 builder 不受影响（hook 看 lock 文件存在性识别 tester subagent）
- **复盘改造只改 prompt**：不动复盘的产出格式（improvements.md / memory 文件）的字段 schema，避免污染历史条目
- **bare loop 兼容**：worktree.enabled=false 时无 worktree_path 概念，tester 锁文件不写 worktree_path → hook 检测无 worktree_path 字段 → 放行所有 Write/Edit（保持旧行为）

## 技术选型

### 议题 1 路径白名单粒度（用户已拍板）

| 选项 | 优点 | 缺点 | 选择 |
|------|------|------|------|
| **A. 严格：只允 worktree 内**（推荐） | 边界清晰、防御彻底 | tester 写 /tmp 中间产物需改 Bash echo | ✅ |
| B. 中等：worktree + /tmp/ | 给诊断留口子 | 白名单要维护、/tmp 也可能被滥用 | ❌ |
| C. 宽松：黑名单（拒主仓 + 其他项目） | 启动代价小 | 拦截面窄、容易漏 | ❌ |

### 议题 1 hook 识别 tester 的机制（用户已隐含拍板：复用现有锁）

复用 V1.1 已有的 `scripts/tester-lock-write.sh`（SubagentStart hook 落锁）。锁文件 schema 扩展：

```
# .claude/builder-loop/tester.lock（旧 schema：仅存在性 + source_dirs 列表）
# V2.2 新 schema：增加 worktree_path / main_repo_path / slug 字段
worktree_path: /path/to/worktree
main_repo_path: /path/to/main/repo
slug: <slug>
source_dirs: <既有>
```

新 hook（PreToolUse Write|Edit）独立脚本 `scripts/tester-write-guard.sh`：
- 读 lock 文件，无 → 放行（不是 tester subagent）
- lock 无 worktree_path 字段（bare loop）→ 放行
- 工具入参 file_path 不以 `${worktree_path}/` 开头 → exit 2 + 精确 stderr

### 议题 1 hook 拒绝时 stderr 粒度（用户已拍板）

**精确诊断**。stderr 输出：
```
⛔ [builder-loop] tester 跨目录写禁止：
   尝试写入: /path/from/tool_input
   允许根:  ${worktree_path}
   主仓:    ${main_repo_path}（禁止跨界写）
   请改用 ${worktree_path}/<相对路径>
```

让 tester 看到 exit 2 错误后能直接拼出正确路径重试。

### 议题 2 闸门位置（用户已拍板）

**强制分类闸门放 5 问之前**：复盘步骤 5 改造为：

```
5.1 列全部候选（不分类，标号 c1/c2/c3...）
5.2 强制 4 桶分类（A1/A2/B/C 各列对应候选号或显式"无 + 一句话理由"）
5.3 仅对 B/C 桶候选走 5 问表格
5.4 提审：A 桶 → improvements.md / B/C 桶 → memory（一次 AskUserQuestion 合并展示）
```

### 议题 2 反例锚点（用户已拍板）

**钉**。在 builder.md L177 判据下方加 2-3 条具体错例 + 正解，作为 A2 误判的硬锚点。

### 议题 3 修复方向（用户已拍板）

| 选项 | 优点 | 缺点 | 选择 |
|------|------|------|------|
| **A. 砍 HAS_RECENT_COMMIT 触发器**（推荐） | 工作树干净就静默；语义清晰；改动面小 | 用户主仓直接改不经 loop 的兜底场景失效，需手动 setup | ✅ |
| B. start_head 基线修正 | 兜底激活后 reviewer 真去验证用户 commit | 用户期望"已收尾"时被强迫多走一轮 reviewer | ❌ |
| C. last_reviewed_head 增强游标 | 精确，向后兼容 | builder.md + stop hook 都要改、动差大 | ❌ |
| D. loop.yml 配置化 (A+B 切换) | 灵活 | 配置项多、用户需知道差异 | ❌ |

<!-- /role -->

<!-- role:builder -->

## 方案设计

### M1: 锁文件 schema 扩展

**文件**：`scripts/tester-lock-write.sh`

**当前行为**（推断，需 Read 确认）：SubagentStart hook 落锁，写 source_dirs 列表到 `.claude/builder-loop/tester.lock`。

**改造**：从环境/state 读取 worktree_path / main_repo_path / slug，追加到 lock 文件。锁文件新增字段全部可选，旧 hook 看不见新字段不影响行为。

锁文件 V2.2 格式（YAML 风格）：
```yaml
# 既有字段
source_dirs:
  - novel_writer
# V2.2 新增字段
worktree_path: /mnt/.../worktrees/1777210026-outline   # bare loop 时此字段不写
main_repo_path: /mnt/.../hongyu_Repo
slug: 1777210026-outline
```

**state 字段读取来源**：tester-lock-write.sh 已经知道 project_root（通过 SubagentStart hook 上下文），按 V2.0 schema 读 state.yml 的 `project_root` / `main_repo_path` / `slug`。

### M2: 新增 PreToolUse Write|Edit hook

**新文件**：`scripts/tester-write-guard.sh`

**职责**：
1. 读 stdin 的 PreToolUse 输入 JSON
2. 解析 tool_name（`Write` / `Edit` / `MultiEdit`）+ tool_input.file_path
3. 检查 `.claude/builder-loop/tester.lock` 存在性
4. 不存在 → exit 0 放行（非 tester subagent）
5. 存在 → 解析 worktree_path 字段
6. 字段缺失 → exit 0 放行（bare loop / V1.x 旧锁）
7. 字段存在 → 检查 file_path 是否以 `${worktree_path}/` 开头（含尾斜杠防 prefix 误判）
8. 不在 → exit 2 + 精确诊断 stderr
9. 在 → exit 0 放行

**install.sh 注册项**：
```json
{
  "PreToolUse": [
    {
      "matcher": "Write|Edit|MultiEdit",
      "hooks": [{"type": "command", "command": "~/.claude/scripts/tester-write-guard.sh"}]
    }
  ]
}
```

**链接映射**：`scripts/tester-write-guard.sh` → `~/.claude/scripts/tester-write-guard.sh`（逐文件 ln -sf）。

### M3: tester.md 输入字段 + 自检改造

**文件**：`agents/tester.md`

**改动 1**：输入字段表（L17-22）增加 `worktree_path`：
```
- worktree_path：worktree 启用时为 worktree 绝对路径（如 /path/to/worktrees/<slug>），bare loop 时为空。所有 Write/Edit 必须以此为根的绝对路径前缀
```

**改动 2**：硬性约束段（L24-32）增加：
```
6. **路径根硬约束**：worktree_path 非空时，所有 Write/Edit/MultiEdit 的 file_path **必须**以 ${worktree_path}/ 开头。违反会被 PreToolUse hook 物理拦截（exit 2）。worktree_path 为空时（bare loop）按主仓相对路径写。
```

**改动 3**：步骤 4（自检）增加一条：
```
- worktree_path 非空时：检查所有 Write/Edit 的 file_path 都以 ${worktree_path}/ 开头
- 不允许通过 Bash cp / mv / ln 把文件从主仓搬进 worktree（应该一开始就写在 worktree）
```

### M4: builder.md spawn 段强制声明

**文件**：`~/.claude/commands/builder.md`（dotfiles 仓）

**改动点 1**：L119（loop 活跃 spawn tester 段）参数列表显式列出 `worktree_path` 必填：
```
1. 过滤 tester 视图、spawn tester（同步），传 spec_view / interface_signatures / target_test_dirs / missing_cases / **worktree_path**
```

**改动点 2**：L122-127（loop 已结束 spawn tester 段）显式说明 `worktree_path: ""`（bare 语义）：
```
1. 同样 spawn tester（同步）补测，传参同上；**worktree_path: ""**（loop 已结束，tester 在主仓 cwd 工作）
```

**dotfiles 同步**：CLAUDE.md §3 已警示 cc-builder-loop 与 dotfiles 解耦待办，本期手工同步两仓 builder.md。

### M5: builder.md 复盘步骤 5 改造（议题 2）

**文件**：`~/.claude/commands/builder.md` L171-211（步骤 5）

**改造内容**：

1. **L171（触发条件）保持不变**

2. **L173-181 候选先分类段** 重写：
```markdown
**1. 列全部候选**（标号 c1/c2/c3...，不分类）

每条候选用一句话写明触发上下文。

**2. 强制 4 桶分类**

| 桶 | 定义 | 出口 |
|----|------|------|
| A1 业务项目缺口 | 能写成检查/断言/fixture/hook/prompt 防住，且属于当前业务代码/测试/项目流程 | `<CWD>/.claude/improvements.md` |
| A2 builder-loop 机制缺口 | 能写成检查/断言/fixture/hook/prompt 防住，且属于 loop 机制（hook / agent / SKILL prompt / 仓库脚本 / fixture） | cc-builder-loop 仓库 `.claude/improvements.md` |
| B 行为/平台/约定 | **必须**靠 Claude 自己记得才能避免（平台陷阱/工具隐式行为/业务事实） | `/memory` |
| C 两栖 | A + B 都做 | 各走一条线；memory 条目注明"代码已/待固化于 X" |

**桶分类输出格式**（4 桶并列、空桶必须显式写"无"+一句话理由）：

```
A1: <c?, c?> / 无（理由：...）
A2: <c?, c?> / 无（理由：...）
B:  <c?, c?> / 无（理由：...）
C:  <c?, c?> / 无（理由：...）
```

**反例锚点**（误判常见模式）：
- 错例：「tester subagent 跨目录写文件」判 B → ❌ 应判 A2，能用 PreToolUse Write|Edit hook 物理防住
- 错例：「.gitignore 自愈追加 telemetry 规则」判 B → ❌ 应判"已交付"不立项，CLAUDE.md V2.1.1 已实现
- 错例：「stop hook 续接要 exit 2 不是 stdout JSON」判 A → ❌ 应判 B，CC 平台契约自己改不了，只能 Claude 记住

A1 vs A2 判据：**问题产生在哪一层**。loop 机制 → A2；当前业务代码/测试/项目流程 → A1。涉及多层时拆开各落一处。CWD 本身就是 cc-builder-loop 项目时 A1 = A2 不区分。
```

3. **L183-191 A 类落盘段** 保持不变（modulo 输出文件路径已在新版表格里写明）

4. **L193-209 5 问表格段** 改为「**仅对 B/C 桶候选走 5 问**」，开头加：
```markdown
**3. B/C 桶候选走 5 问自检**（A 桶不走 5 问，直接落 improvements.md）
```

5. **L209 提审段** 保持「A 与 B/C 合并到一次 AskUserQuestion」，但要求选项前缀强制：
```markdown
- A1 候选 → `[A1] <一句话>`
- A2 候选 → `[A2] <一句话>`
- B/C 候选 → `[mem] <一句话>`
```

### M6: e2e fixture 三组场景

**文件**：`skills/builder-loop/fixtures/e2e/test-tester-write-guard.sh`

**场景 A：tester 跨界写主仓 → 拦截**
1. 模拟 SubagentStart hook 落 lock（含 worktree_path）
2. 直接调用 `tester-write-guard.sh`，stdin 喂 PreToolUse JSON（tool_name=Write、file_path=主仓路径）
3. 断言 exit 2 + stderr 含「tester 跨目录写禁止」+ 含 worktree_path 真实值

**场景 B：tester 写 worktree 内 → 放行**
1. 同上落 lock
2. 调 hook 喂 file_path=`${worktree_path}/tests/foo.py`
3. 断言 exit 0、无 stderr

**场景 C：无 lock（非 tester subagent）→ 放行**
1. 不落 lock
2. 调 hook 喂任意 file_path
3. 断言 exit 0

**场景 D：bare loop（lock 无 worktree_path 字段）→ 放行**
1. 落 lock 但不写 worktree_path
2. 调 hook
3. 断言 exit 0

**场景 E：path traversal 防御**
1. 落 lock，worktree_path=`/a/b`
2. 喂 file_path=`/a/b/../c/foo.py`（resolve 后是 `/a/c/foo.py`，越界）
3. 断言 exit 2（实现需在 hook 里跑 readlink -f / realpath 后再 prefix 比较）

**fixture 工程红线**（按 tester.md 步骤 4.5）：
- bare loop fixture slug=`__main__`
- worktree fixture state 含 `main_repo_path` 字段
- worktree fixture 启用前先 `git add .claude/loop.yml && git commit`
- bash grep+head+sed 末尾 `|| true` 收尾

### M7: install.sh / uninstall.sh

- `install.sh`：注册新 PreToolUse Write|Edit hook + 创建 `tester-write-guard.sh` 软链
- `uninstall.sh`：清理对应 hook 条目 + 删软链
- 注册 hook 时按 CLAUDE.md §1 表格风格在表中追加一行

### M8: CLAUDE.md / known-risks.md 更新

- §1 hook 表追加「PreToolUse `Write|Edit|MultiEdit` → tester-write-guard.sh」
- §5 已交付能力追加 V2.2 段（含三议题）
- §7 追加排查手册「7.7 兜底激活不再补漏 `HAS_RECENT_COMMIT` 场景 → 用户/builder 主仓直接 commit 后期望 loop 自动验证 PASS_CMD 的，请手动调 `setup-builder-loop.sh "<task>"` 起 loop」

### M9: bootstrap 触发器收敛（议题 3）

**文件**：`scripts/builder-loop-stop.sh`

**改动点 1**：L173-179 改动检测段简化：
```bash
# V2.2 议题 3：砍 HAS_RECENT_COMMIT 触发器
# 旧行为：30 分钟内有 commit 也触发兜底 → 用户 commit 后空转
# 新行为：只看未提交工作树改动；commit 完工作树干净 → 静默放行
HAS_DIFF="$(git -C "$PROJECT_ROOT" diff --stat 2>/dev/null)" || true
[ -z "$HAS_DIFF" ] && { HAS_DIFF="$(git -C "$PROJECT_ROOT" diff --cached --stat 2>/dev/null)" || true; }
if [ -z "$HAS_DIFF" ]; then
    exit 0
fi
```

**改动点 2**：L180-192 游标段降级。HAS_DIFF 非空才会到这里，游标主要场景（HAS_DIFF=空 + HEAD 未变）已经被新触发器覆盖，原游标段可以删——但保留作为防御冗余（如果未来某次重启 HAS_RECENT_COMMIT 此处仍能拦）。**保留游标 + 写入逻辑不动**。

**改动点 3**：注释/文档化 `last_processed_head` 游标的新角色（不再是触发去抑制器，而是审计/排查辅助）。

**改动点 4**：注释 `HAS_RECENT_COMMIT` 变量已移除原因，避免后续维护者误以为是漏写。

### M10: bootstrap 修复对其他逻辑的影响审计

| 现有路径 | 是否受影响 | 处理 |
|----------|-----------|------|
| 正常 stop hook（state 文件命中）→ 跑 PASS_CMD | ❌ 不影响 | 不动 |
| 跨 session 污染守门（L168 `EXISTING_LOOP_WORKTREES`）| ❌ 不影响 | 不动 |
| flock 互斥（L140-152）| ❌ 不影响 | 不动 |
| 已处理 HEAD 游标写入（L458 `write_processed_cursor`）| ❌ 不影响 | 不动 |
| `setup-builder-loop.sh` 的 task_description 推断（L194-205）| ❌ 不影响 | 不动 |

议题 3 改动是**纯收紧**——任何被新触发器拒掉的场景，旧触发器原本也会走完后输出 NOOP PASS+空 changed_files。新版本提早静默放行，不引入新行为差。

## 实施顺序

按依赖性串行：M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8 → **M9 → M10**。

M1+M2 是议题 1 核心机制，M3+M4 是 prompt 教育层，M5 是议题 2，M6 是 fixture 验证，M7+M8 是部署 + 文档收尾，M9+M10 是议题 3。

议题 3 与议题 1/2 完全正交，可并行。建议**单独 commit**避免 commit 范围过大（议题 1 + 议题 2 → 1 commit；议题 3 → 1 commit）。

## 回退路径

- 议题 1 回退：删除 tester-write-guard.sh + uninstall.sh 自动清 hook 条目；tester.md 多出来的 worktree_path 字段对旧 builder spawn 不传不影响（向后兼容）
- 议题 2 回退：builder.md 步骤 5 改回旧版段落即可；4 桶分类输出对旧复盘格式向后兼容（旧版只列 B/C 候选可视为 A1/A2 桶为"无"）
- 议题 3 回退：恢复 `builder-loop-stop.sh` L173-179 旧版（重新引入 `HAS_RECENT_COMMIT` 变量与 `||` 触发条件）

<!-- /role -->

<!-- role:tester -->

## 验收标准

### 议题 1

**A 段：tester-write-guard hook 拦截**

| Case | 输入 | 期望 |
|------|------|------|
| A1 | lock 含 worktree_path=/wt，喂 Write file_path=/main/foo.py | exit 2、stderr 含 worktree_path / main_repo_path / 改用建议 |
| A2 | lock 含 worktree_path=/wt，喂 Edit file_path=/wt/sub/foo.py | exit 0 |
| A3 | lock 含 worktree_path=/wt，喂 MultiEdit file_path=/wt/foo.py | exit 0 |
| A4 | 无 lock 文件，喂任意 Write | exit 0 |
| A5 | lock 无 worktree_path 字段，喂任意 Write | exit 0（bare loop 兼容） |
| A6 | lock 含 worktree_path=/wt，喂 Write file_path=/wt（无尾斜杠 prefix 误判防御）| exit 2（不能恰好等于 worktree_path） |
| A7 | lock 含 worktree_path=/wt，喂 Write file_path=/wt2/foo.py（前缀部分匹配）| exit 2（必须含尾斜杠才放行）|
| A8 | path traversal：file_path=/wt/../main/foo.py | exit 2（realpath 解析后越界）|
| A9 | symlink：worktree 内 symlink 指向主仓文件，写 symlink 路径 | 视实现：建议按 file_path 字面值判断（不 resolve symlink，免误伤合法 symlink）|

**B 段：tester.md 自检约束**

| Case | 输入 | 期望 |
|------|------|------|
| B1 | tester.md 文件 | 输入字段表含 `worktree_path` 行 |
| B2 | tester.md 文件 | 硬性约束段含「路径根硬约束」第 6 条 |
| B3 | tester.md 文件 | 步骤 4 自检含路径根校验项 |

**C 段：锁文件 schema**

| Case | 输入 | 期望 |
|------|------|------|
| C1 | worktree loop 触发 SubagentStart | tester.lock 含 worktree_path / main_repo_path / slug 字段 |
| C2 | bare loop 触发 SubagentStart | tester.lock 不含 worktree_path 字段（或 worktree_path=""） |
| C3 | V1.x 旧锁文件（无新字段） | hook 看到无字段 → 放行 |

### 议题 2

**D 段：builder.md 复盘段落改造**

| Case | 输入 | 期望 |
|------|------|------|
| D1 | builder.md L171-211 文本 | 含 4 桶并列输出格式说明 |
| D2 | builder.md L177 附近 | 含至少 2 条反例锚点（跨目录写 / .gitignore 自愈 / stop hook 平台契约 中至少 2 条）|
| D3 | builder.md 5 问表格段 | 开头明示「仅对 B/C 桶候选走 5 问」|
| D4 | builder.md 提审段 | 选项前缀强制 [A1]/[A2]/[mem] 标识 |
| D5 | builder.md 复盘段（端到端） | 「不许只塞 memory」规则保留 + 4 桶空桶填"无"+理由的格式硬约束 |

**E 段：复盘端到端**（语义验证，可由用户复盘时观察）

| Case | 输入 | 期望 |
|------|------|------|
| E1 | 候选「tester 跨目录写文件」 | 复盘判 A2、落 cc-builder-loop `.claude/improvements.md`、不进 memory |
| E2 | 候选「.gitignore 自愈追加规则」 | 复盘标"已交付"或判 A2 标历史欠账，不进 memory |
| E3 | 候选「ANTHROPIC_API_KEY env 加载顺序」 | 复盘判 B/C，进 memory（CC 平台契约 + V2.1 文档已涵盖） |
| E4 | 复盘所有 4 桶为"无" | 输出「📝 本次任务无候选」+一句话原因 |

### F 段：路径白名单边界覆盖

| Case | 输入 | 期望 |
|------|------|------|
| F1 | tester 调 Bash 跑 `cp 主仓/x worktree/x` | 不被 hook 拦（Bash 不是 Write/Edit/MultiEdit）。**已知缺口**——但 Bash 拦截会引入大量误伤（pytest / git / wc 等），方案不覆盖；靠 prompt 自检兜底 |
| F2 | tester 用 BashOutput 读取主仓文件 | 放行（Read 系列由既有 tester-lock-check.sh 处理，本方案不动）|

### G 段：bootstrap 兜底激活触发器（议题 3）

| Case | 输入 | 期望 |
|------|------|------|
| G1 | 主仓有 unstaged 改动（HAS_DIFF 非空）+ 无 state 文件 + loop.yml 存在 | 触发兜底激活（旧行为保留）|
| G2 | 主仓有 staged 改动（git diff --cached 非空）+ 无 state | 触发兜底激活（旧行为保留）|
| G3 | 主仓工作树干净（HAS_DIFF 空）+ HAS_RECENT_COMMIT 非空 + 无 state | **静默放行**（exit 0，新行为；旧版会触发兜底走 NOOP）|
| G4 | 主仓工作树干净 + HAS_RECENT_COMMIT 空 + 无 state | 静默放行（旧行为保留）|
| G5 | state 文件存在（active=true）| 走正常 PASS_CMD 流程（不进 bootstrap 分支，旧行为保留）|
| G6 | 跨 session 污染（EXISTING_LOOP_WORKTREES > 0）+ HAS_DIFF 非空 | stderr 警告 + 静默放行（旧行为保留，不被议题 3 影响）|
| G7 | 非 git 仓库 + HAS_DIFF 想读但 git 命令失败 | 静默放行（旧行为保留）|
| G8 | 同 session 第一次 PASS 写游标后 + 用户 commit 让 HEAD 前进 + 工作树干净 → 第二次 stop | **静默放行**（议题 3 修复目标）|
| G9 | 用户在主仓直接 commit 后 → 期望 loop 自动验证 PASS_CMD | **不再触发**——需手动 setup（M8 排查手册需明示）|

## 关键场景（用户视角）

1. tester subagent 误用主仓绝对路径 → 立刻 exit 2，stderr 显示真实 worktree_path 让 tester 自动改路径重试
2. bare loop 项目接入 tester → 完全不受 hook 影响（worktree_path 字段缺失自动放行）
3. 复盘 builder 漏判 A2 → 4 桶空桶必填理由格式让用户立刻识别异常并指正
4. 用户/builder 阶段闭环手动 commit 后 → 工作树干净 → bootstrap 静默放行（不再被无意义 NOOP 触发的 reviewer 提示困扰）

<!-- /role -->
