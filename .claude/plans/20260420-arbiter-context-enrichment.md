# Arbiter 对手方上下文补齐（V1.x 丐版）

## 背景 & 目标

多个 builder 并行工作在各自 worktree 时，先合回主干的 builder 赢，后合回的触发仲裁。
当前 arbiter 只知道「自己这边改了什么」，不知道「对面是谁、改了什么、为什么改」——
退化成单边 merge 工具，无法做真正的仲裁。

**目标**：merge-worktree-back.sh 检测冲突时，从 `git log` 提取先合入主干的 commit 摘要
（hash + message + files），写入 state file，经 stop hook 传给 arbiter。

**成功标准**：arbiter 在输入字段里能看到 `their_commits` 列表，裁决时引用对方意图。

## 预估改动级别

L2（实现改动）—— 不新增签名/模块，只在现有脚本和 prompt 里加字段。

## 约束 & 边界

- **不改 arbiter 的职责边界**：仍然只解 rebase 冲突，不升级为编排者
- **不改 merge-worktree-back.sh 的退出码协议**：MERGED / NOOP / NEED_ARBITRATION / ERROR
- **commit 摘要截断**：最多取 20 条 commit，每条 message 截断 200 字符，防 state 膨胀
- **向后兼容**：`their_commits` 缺失时 arbiter 退回旧行为（自行 `git log`），不崩溃

## 技术选型

| 方案 | 做法 | 优劣 |
|------|------|------|
| **A: state file 内嵌 YAML** | `mark_arbitration()` 里 `git log --format` → 写多行 YAML 到 state | ✅ 推荐。与现有 state 一致、stop hook 直接读 |
| B: 独立 JSON 文件 | 写 `.claude/conflict-context.json` | 多一个文件要管、清理时易遗漏 |

选 A。

## 方案设计

### 数据流

```
merge-worktree-back.sh
  ├── git log PROJECT_ROOT ^START_HEAD --oneline --stat (提取对方 commits)
  ├── 写入 state: their_commits (JSON 字符串，python 安全写入)
  └── 输出 NEED_ARBITRATION

stop hook (NEED_ARBITRATION 分支)
  ├── 读 state.their_commits
  └── 注入 arbiter spawn 参数: their_commits

arbiter.md
  ├── 新输入字段: their_commits
  ├── 步骤 2 更新: 直接用 their_commits 理解对方意图（不再需要自己 git log）
  └── 步骤 3: 决策时引用双方 task_context
```

### state file 新增字段

```yaml
their_commits: '[{"hash":"abc123","message":"feat: add retry","files":["src/client.py"]},...]'
```

用 JSON 字符串存储，避免 YAML 多行嵌套的转义坑。python 写入、python 读取。

## 风险 & 应对

- **git log 为空**（主干无新 commit，不该走到 NEED_ARBITRATION）：加空值兜底，写 `their_commits: '[]'`
- **commit message 含特殊字符**：用 `json.dumps` 安全序列化，不手拼

## 文件地图

| 文件 | 改动 |
|------|------|
| `skills/builder-loop/scripts/merge-worktree-back.sh:54-73` | `mark_arbitration()` 加 git log 提取 + 写 their_commits |
| `scripts/builder-loop-stop.sh:119-170` | NEED_ARBITRATION 分支加读 their_commits + 传 arbiter |
| `agents/arbiter.md:10-16,39-41` | 输入字段加 their_commits，步骤 2 改为直接引用 |
| `~/.claude/commands/builder.md:109-120` | spawn arbiter 参数加 their_commits |
| `skills/builder-loop/fixtures/e2e/test-conflict.sh:80-89` | 阶段 1.5 加断言 their_commits 存在 |

## 执行任务列表

1. **merge-worktree-back.sh** `mark_arbitration()` 函数：在写 need_arbitration/conflict_files 之后，用 python 执行 `git log PROJECT_ROOT ^START_HEAD --format='%h|%s' --stat`，解析为 JSON 数组 `[{hash, message, files}]`，写入 state 的 `their_commits` 字段
2. **stop hook** NEED_ARBITRATION 分支：读 state 的 `their_commits`，加入 arbiter spawn 的 msg 模板
3. **arbiter.md**：输入字段加 `their_commits`；步骤 2 改为「从 their_commits 理解对方意图（无需自行 git log）」；步骤 3 决策表加「参考 their_commits 中对方的 commit message 判断意图」
4. **builder.md**：仲裁流程 spawn 参数加 `their_commits: <hook 给出的 their_commits>`
5. **test-conflict.sh**：阶段 1.5 加断言 `grep -q 'their_commits:' "$STATE"` + 验证 JSON 含 "main edit" commit message
6. 跑全量 e2e fixture 验证

## 验收标准

1. `test-conflict.sh` PASS：state 含 their_commits 且 JSON 解析正确
2. 全量 e2e fixture（5 个）全 PASS，无回归
3. arbiter.md 输入字段含 their_commits 文档描述
