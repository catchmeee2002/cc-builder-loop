# Builder 契约适配

仅在用户已经选择 `builder_loop` 时读取。复用通用 Planner 的同一份语义计划，不另写产品方案。
只生成和验证 contract，不实现、不创建 run、不更新 ledger。公共 legacy v2/v3 `start` 保持关闭。

## 构造前检查

- 先用 `readlink -f` 解析本参考文件的真实路径，再完整读取相对其目录的
  `../../../schema/assurance-v4-contract.schema.json`。不要词法折叠安装软链接中的 `..`；
  字段以公共 schema 为准，不复制或猜测结构。
- 冻结 Authority 前，枚举本任务会修改的版本化实现、角色或 instruction source，并检查已有验证输入对
  路径、公开符号、blob 或 digest 的直接引用。必须同步更新的 manifest、re-export/公开聚合、registry/index
  精确普通文件一并纳入 Builder 写边界；checker 保持只读，除非任务本身修改它。不用 glob、猜测式
  传递依赖或实现后的 fixture 失败代替实现前的闭包。
- Builder 默认由主 session 承担，不询问承载选择；只有用户主动指定独立 Native thread 时才使用该模式。
- 当前任务涉及已有 run 的决策、修订、successor、终态重开或 continuation 时，读取
  [生命周期约束](builder-lifecycle.md)，先查询当前 session 的 ledger 与 Git，不解析 transcript。
- 当前任务涉及 dirty intake、串行公开前置产物、受保护 runtime preparation 或外部部署，或者
  `validate --repo` 报告 runtime_support 冲突时，读取 [环境约束](builder-environment.md)。
- 不支持的任务说明具体边界并停止交接；由用户决定是否切换执行路线，不能自动改为原生执行。

## Contract 约束

- `mission` 只放目标、行为、接口、验收场景和信任边界；不混入运行状态。

- `authority` 冻结 target branch、Builder/Tester 精确写边界、dirty intake、串行公开前置产物和受保护
  support path。权限扩大必须重新交给用户决定。
- 代码任务的 `assurance.required` 默认精确包含 `tester`、`proof`、`machine`、`blackbox`、`reviewer`。
  revision-one、1–3 behavior、单一 machine/blackbox command、无 dirty intake/publication/protected support/
  external target/deployment/continuity 的局部任务使用 `profile:compact` 并保持 preflight 与
  reviewer_preflight false；任一条件不满足时使用 `profile:full` 与默认 `reviewer_preflight:true`。不得为
  追求 compact 删除 gate 或改写验收。纯 Markdown L1 只要求 `reviewer` 且保持 preflight false，不得伪造 Tester、machine 或 blackbox。
- Tester 可信来源由独立 thread、提交的普通测试文件、source manifest 与 Reviewer 审查共同绑定；这不
  宣称操作系统级恶意代码 sandbox。
- `execution` 初始固定为 `version: 1`、`driver_enforced: true`、`candidate_head: null`、
  `builder_runtime:{"schema_version":1,"mode":"root_session"}`（用户主动指定独立 Native thread 时
  使用 `native_thread`；仅 Builder-loop 新 contract；缺失字段的
  retained legacy contract 按 `native_thread` 读取）、
  `recovery_policy:{"schema_version":1,"mode":"automatic_nonsemantic","continuation_window":3}`、
  `cost_ancestry:null`、空的 `builder_files/tester_files/dirty_snapshot/agents`、`tester_source: null`、
  `deployment: null`，Authority 的
  `external_targets` 默认空。Agent identity 只能在真实 spawn
  后由专用事务写入，Planner 不预填。
- 新 v4 contract 还必须提供固定的 `progress_policy` 和 `work_units`。进度策略使用有界重建：
  当前上下文最多尝试三次，每个工作单元最多重建一次，同一 thread 最多承载三个连续工作单元；
  上下文来源只能是当前 contract、ledger、Git 和已确认 evidence。
- 每个工作单元必须声明 `id`、`role`、`objective`、`depends_on`、精确 `scope` 和角色匹配的
  completion observation。依赖图在启动前冻结，Driver 不得动态拆分、插入或改写。
- Builder、Tester、Reviewer 的进度只能由 Core 根据 clean commit、source manifest 或正式
  evidence 观察并写入 ledger event。无副作用断流才沿现有限额 retry；副作用或观察漂移必须
  进入 reconcile/NEEDS_USER，不得自动 commit、rollback 或退回旧执行语义。
- `assurance.machine_commands` 冻结机器命令；`execution.commands` 冻结独立 blackbox 命令。argv 必须是
  字符串数组，`expected_returncodes` 冻结什么实际返回码算通过。交付必跑且本地无副作用的关键测试用
  `run_before_full_suite:true` 标记在 machine commands 中，并同时设置 `preflight_before_proof:true`；不得靠
  分析 argv 猜顺序。
- `validate --repo` 的派生 `admission` 必须同时为 `READY`。裸 executable 只从 Core 的 trusted system PATH
  解析；若工具只存在于 ambient PATH，应改成经用户环境确认的显式绝对路径，让 runtime 绑定 SHA-256，
  不得假定 Agent shell 能找到就可执行。仓库相对 executable 在规划时保持 candidate-bound deferred，最终
  machine/blackbox/deployment 执行仍重新绑定 candidate blob。
- 需要 blackbox 的新 contract 必须把每条 `mission.acceptance_cases` 写成单一可观察声明，并冻结
  `observation.surface_id/surface_description/execution_ids/required_dimensions`；观察外部目标时再绑定已授权
  `target_id`。同一声明跨多个观察面时拆成多个 case。surface id 只在本计划内命名，不建立强 E2E
  类型或项目 Profile；项目 AGENTS、用户目标或公开契约要求 UI、设备、CLI 等真实表面时，不能改写为
  API、fixture、内部 diagnostics 或资源存在性。每个 execution id 必须来自本 contract 的
  `execution.commands`。

## 验证与交接

1. 把 contract 的规范 JSON 通过 stdin 交给：

   `codex-builder-loop assurance --experimental-v4 validate --repo <repo> --contract -`

2. 只有返回 `status=READY` 且 `admission.status=READY` 才输出最终方案。若 admission 聚合报告任何
   `blocked` command，先修正 contract 中的 executable 表达；`deferred` 只允许仓库相对 executable 或尚待
   当前 Builder/carryover 形成的公开前置文件。最终方案必须包含唯一、完整且与已验证字节语义相同的：

~~~~markdown
<!-- assurance-v4-contract -->
```json
{ "schema_version": 4 }
```
<!-- /assurance-v4-contract -->
~~~~

3. contract marker 位于方案正文内；不要再输出 legacy `unit-test-spec`、`documentation-spec`、
   `workspace-intake` 或第二份计划状态。
4. 在方案正文之外输出独立行：

`BUILDER_HANDOFF_READY`

验证失败、用户选择原生 Plan、任务超出实验范围、方案仍有未决产品取舍时不得输出该标记，也不得创建
或启动 run。

新增 proof 计划声明实际 runner 的启动前缀与超时；仅在输入确实不影响判据时声明 evidence neutral paths，测试、runner 和依赖不得排除。需要完成后等待时使用显式 hold，不减少验收 gate。字段以公共 schema 为准。
