---
name: file-github-issue
description: 当用户要求“提 Issue”“开 Issue”“记录 bug”“把问题记到 GitHub”，或要求补充、结案、关闭 GitHub Issue 时使用。创建时保留不可改写的事故快照，关闭时追加可解析的最终根因、决策和验收契约，再由当前 Agent 直接调用 gh 创建、评论或关闭；用户已经要求相应动作时不得再次请求确认。不要用于普通 Issue 查询、根因分析、修复、Planner 分流或影子分流。
---

# 记录 GitHub Issue

在 Issue 的创建和结案两端保留事实。不要替后续诊断提前决定修法，也不要把事后结论泄漏进事故快照。

## 判断授权

- 用户已要求创建、补充或关闭 Issue，或当前任务已关联某 Issue 时，直接执行范围内的评论和标签更新；
  关闭仍需用户明确要求或任务明确要求。
- 只是在其他任务中顺手发现可疑问题、但没有 GitHub 写入授权时，只向用户报告。
- 仓库归属不明确、证据可能含凭据或个人数据、动作实际要求改变产品目标或设计原则时，使用
  `request_user_input` 处理这个异常；不要把普通字段缺失升级成人工审批。

## 创建或补充

1. 读取适用的项目指令和设计原则，确定唯一责任仓库以及被违反的预期契约。
2. 在现场消失前收集触发场景、实际过程、复现步骤、观察到的现象、预期契约和已确认事实。无法稳定
   复现时如实记录尝试和边界，不补造步骤。
3. 记录命令及退出码、日志或 run 引用、相关文件、当前 `HEAD`、branch 和 dirty 状态。只摘取有效
   片段，不倾倒无关长日志。
4. 清洗 token、Authorization header、带凭据的 remote URL、个人数据和其他秘密。安全漏洞或无法
   确认可公开的证据不得发到普通 GitHub Issue。
5. 将根因状态写成 `unknown`、`candidate` 或 `confirmed`。不把模型推断写进“已确认事实”；默认不写
   修法、建议或实施方案。
6. 先用 `gh issue list --state all --search ...` 按触发条件、现象和责任边界查重。相同原子问题用
   `gh issue comment` 追加新现场；根因或责任仓库不同的分别创建并互相链接。
7. 新建时由当前 Agent 直接运行 `gh issue create`。标题描述可观察症状；正文末尾必须包含以下 v2
   快照，并填写真实值。Builder-loop run 使用 ledger 冻结的 `runtime_identity`；普通场景先执行
   `codex-builder-loop version --json`，逐字段复制其 `runtime_identity`，不得用当前 checkout 反推旧 run、
   也不得补造缺失 commit：

<!-- issue-capture:v2 -->
```json
{
  "captured_at": "<UTC ISO-8601>",
  "repository": "<owner/repo>",
  "incident_head": "<40-char commit or unavailable>",
  "branch": "<branch or detached>",
  "dirty": true,
  "root_cause_status": "<unknown|candidate|confirmed>",
  "builder_loop_runtime": {
    "builder_loop_version": "<SemVer or null>",
    "adapter": "<codex|claude-code|unknown>",
    "adapter_commit": "<40-char commit or null>",
    "adapter_dirty": false,
    "capture_status": "<captured|partial|unavailable|legacy-unavailable>"
  }
}
```
<!-- /issue-capture:v2 -->

创建后不要改写原始正文和 `issue-capture`；新增事实只通过评论追加。这个快照是后续离线评测允许读取
的事故输入，不得混入修复后的代码、结案结论或影子预测。历史 `issue-capture:v1` 只读兼容；新 Issue
不得继续生成 v1，也不得同时出现 v1/v2。

## 结案或关闭

1. 重新读取 Issue、项目关闭条件、实际修复提交和验收证据。项目要求真实观察期时，条件未满足就
   不得伪造结论或关闭。
2. 选择真实 `outcome`：`fixed`、`duplicate`、`not-a-bug`、`cannot-reproduce` 或 `wontfix`。
3. 只把有直接证据的根因标成 `confirmed`；否则保持 `candidate` 或 `unknown`。结论必须解释违反的
   不变量，而不是只重复症状或提交内容。
4. 记录实际发生的人类决策，不替影子分流器填写答案：
   - `scope_approval`：原则方向已定，但批准了公共契约、角色边界或成组范围；
   - `goal_or_principle`：选择或改变了目标、原则；
   - `root_cause_correction`：人类推翻或实质修正了 Agent 根因；
   - `tradeoff`：在不可同时满足的方向间作了取舍。
5. 在关闭前通过 `gh issue close --comment` 追加以下契约；数组没有内容时写 `[]`，不要省略字段：

<!-- issue-resolution:v1 -->
```json
{
  "resolved_at": "<UTC ISO-8601>",
  "outcome": "<fixed|duplicate|not-a-bug|cannot-reproduce|wontfix>",
  "incident_head": "<capture incident_head or unavailable>",
  "resolved_head": "<40-char commit or unavailable>",
  "fix_commits": ["<commit>"],
  "root_cause_status": "<unknown|candidate|confirmed>",
  "root_cause": "<final root cause or unknown>",
  "violated_invariant": "<violated contract or unknown>",
  "human_decision": {
    "required": true,
    "kinds": ["<scope_approval|goal_or_principle|root_cause_correction|tradeoff>"],
    "evidence": ["<decision fact or reference>"]
  },
  "acceptance": {
    "deterministic": true,
    "evidence": ["<command, test, observation, review or reference>"]
  },
  "residual_uncertainty": ["<remaining uncertainty>"]
}
```
<!-- /issue-resolution:v1 -->

不要在结案评论中填写 `shadow_route`、`derived`、`batch_approval` 或 `needs_first_principles`。离线评测器
根据根因、决策和验收事实推导路线，避免关闭 Agent 事后给自己打分。

## Builder-loop retrospective 同步

只有调用方明确提供当前 retrospective 的 `snapshot_digest` 和 canonical binding 时才进入本节；普通
Issue 操作不自行制造 binding 或 receipt。binding 必须原样包含一个 `owner`、一个 canonical
`reference`、排序且唯一的 `signal_ids` 和 `signal_digest`。`signal_digest` 必须等于以下对象按 UTF-8、
key 排序、无空白 JSON 序列化后的 SHA-256，任何不一致都停止，不能从对话或 Issue 内容猜测修正：

```json
{
  "snapshot_digest": "<supplied snapshot digest>",
  "owner": "<supplied owner>",
  "reference": "<supplied reference>",
  "signal_ids": ["<supplied sorted signal id>"]
}
```

同一 `(owner, reference)` 的全部 routed signals 只写一条专用更新。新建 Issue 时先取得最终 Issue
reference，再追加这条更新；已有 Issue 直接追加。更新正文必须逐字保留下列 machine-readable block，
并在 block 外只写本 skill 允许的客观事故事实：

<!-- builder-retrospective-sync:v1 -->
```json
{
  "snapshot_digest": "<supplied snapshot digest>",
  "owner": "<supplied owner>",
  "reference": "<supplied reference>",
  "signal_ids": ["<supplied sorted signal id>"],
  "signal_digest": "<supplied signal digest>"
}
```
<!-- /builder-retrospective-sync:v1 -->

写入后必须使用 `gh` 返回的 comment/record identity 读取同一远端记录，不能用 Issue 列表、最后一条评论
或本地 body 冒充 read-back。分别对实际写入 body 和 API 回读的 body 字符串原值按 UTF-8 计算 SHA-256；
CLI 展示换行、pretty JSON 或整个 API response 都不参与 digest。只有两个 digest 相等时，向调用方返回
以下 receipt；字段不得改名、遗漏或附加第二个 binding：

```json
{
  "owner": "<supplied owner>",
  "reference": "<supplied reference>",
  "signal_ids": ["<supplied sorted signal id>"],
  "signal_digest": "<supplied signal digest>",
  "remote_record_reference": "<exact comment or record URL/id>",
  "written_body_digest": "<sha256>",
  "read_back_body_digest": "<same sha256>",
  "observed_at": "<UTC ISO-8601>"
}
```

写入成功但无法按 identity 回读、block 被平台改写、body digest 不同或远端记录归属不匹配时，只报告同步
失败，不返回 receipt，也不声称 retrospective 已完成。

## 返回结果

创建、补充或关闭后核对 repository、Issue 编号、状态和 URL，只向用户简短报告结果。除非用户另有
要求，不继续分析、分流或修复。
