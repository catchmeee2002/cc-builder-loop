# Assurance v4 生命周期规划

仅在 Builder 规划涉及已有 run 的决策、修订、successor、终态重开或 continuation 时读取。
先读取当前 session 的 `driver-next`、`driver-context`、`status.lineage` 和结构化 decision_request。
`contract_decision` 仅在现有 update-facet / revise-mission 能安全表达时走同 run 路径；execution、
publication 路径、dirty intake 等专用事务变化按真实边界规划 successor。
涉及 protected preparation 或 residue 时同时读取 [环境约束](builder-environment.md)。

- 只有 Mission 语义变化才提升 revision。
  已授权且现有 `update-facet` 或 `revise-mission` 事务能安全表达的 plan decision，先在同一 active run
  收敛；普通执行信息变化不得先 abandon 或自动升级为新 run。同 run Revision 绑定上一 mission digest。
- 只有现有事务不能保持语义、授权或事务安全时才规划新 run。Assurance v4 source 必须在 successor
  contract 完成验证、`start` 持久化 target 前保持 active；不得先调用 `abandon`。新 contract 用
  `mission.supersedes` 绑定 source run、revision、mission digest 和 candidate HEAD；通常绑定 execution
  facet 已 checkpoint 的 candidate。唯一例外是 active source 因 `runtime-preparation-required` 在 Builder
  checkpoint 被拒：只有 ledger 中恰有一个仍 open 的结构化问题、其 candidate 比 execution candidate
  更新且仍位于原 candidate branch/worktree、对应 Builder dispatch 已 completed 但未 consumed、dispatch
  artifact 未漂移、问题按冻结 runtime support 可精确重现、target 未移动且全部变更仍在 source authority
  内，才绑定该 rejected candidate。必须同时冻结它的 source problem disposition；任一事实不足时停止，
  不搜索 orphan、不读 transcript、不 resume source。`start` 创建 target 后才把 source 封为 superseded。
  ignored residue 不阻止该 successor 只在 source 留存，但 Planner 只有在 ledger 中唯一 open
  `builder_loop` problem、当前 candidate/problem snapshot、完整 exact path/SHA-256 manifest 与普通文件
  类型都可机械证明时，才可把后续 `resolve-candidate-residue` 作为独立 cleanup 事务；不得把删除授权
  塞进 successor contract、猜测 `__pycache__` 模式或采用目录/glob。source terminal cleanup 还必须由
  target ledger 证明该 problem 已逐条 disposition 为 `handled_elsewhere`。
- `abandon` 只用于用户显式取消且没有 successor。abandoned、superseded 或 finalized run 都是 terminal，
  不重新激活、不 rescue，也不能作为连续 supersession source；遇到 terminal source 时停止交接并说明
  continuity 已不可恢复。以上 active-to-superseded 路由只适用于 Assurance v4，legacy v2/v3 保持既有
  abandoned-source revision 契约。
- 更高 revision 先读取 `status.lineage`，只向用户呈现发生变化的语义 facet、逐条 open problem 处理和
  `lineage.health` 要求的架构复审。`execution.revision_transition.predecessor_pressure_digest` 必须绑定当前
  `task_pressure_digest`。第三次任务级非语义 transition、同 category 第三次或 incomplete ancestry 首次
  停止时，continue decision 也精确绑定该 digest；Core 将它记录为当前 transition 加同 category 后续三次
  的 grant。授权范围内不重复询问；过期、stale pressure、新 category 或 Mission 语义变化重新停止。
  Mission change 归类为 semantic，不增加非语义压力，也不能消费该 grant。
  category 只使用 `execution_contract`、`resource_parameter`、`target_drift`、`role_continuity`、
  `tester_correction`、`mission_change`、`git_conflict`。
- 跨 run supersession 用 `execution.prior_problem_dispositions` 绑定 source
  `lineage.open_problem_snapshot_digest`；每个
  `open_problem_keys` key 恰好选择
  `included`、`handled_elsewhere` 或 `discarded`。`included` 保持 owner 与问题意图，不能携带旧角色身份、
  Tester source 或 evidence。最终仍只输出一份完整、可验证的 v4 contract，不另建 delta contract。
- successor 的 `execution.carryover` 只记录 source candidate 相对 target 的 exact path/blob；当前 revision 的
  `builder_files` 与 `tester_files` 仍从空集合开始。公开前置文件只有在当前 Builder 实际创建或修改并
  checkpoint 后，或 candidate blob 与 carryover 完全一致时才算 ready，不能把旧角色产出复制成本轮产出。
- 当同一 Mission 必须从 `failed` 或用户取消的 `abandoned` terminal root 重新开始、且只需累计交付成本时，
  新 contract 保持 `mission.revision=1`、`supersedes:null`，以 `execution.cost_ancestry` 精确绑定 source 的
  `lineage_digest` 与 `task_pressure_digest`。只允许一个未被消费的 terminal source、相同 target 与完全相同
  Mission 语义；新 root 的 candidate、files、roles、Tester source、carryover、evidence 和 dirty intake 都
  为空。active、finalized、superseded、forked、cyclic、stale 或语义不同的 source 不得使用该通道。

## 同 run 决策方案

- 人类可读正文只展示当前 run/problem、拟改 delta、原因、保留内容、失效 gate 与目标分支状态；不要把
  未变化内容重新讲一遍。旧 problem 缺少 `decision_request` 时允许从当前完整 contract 和用户明确选择
  补出一次精确 delta，但不得靠猜测扩大变化。
- 从 ledger 当前 `facets` 生成唯一完整 replacement contract。Mission 修订同时填入绑定当前 lineage 的
  `execution.revision_transition`；Authority/Assurance 之外的 facet 必须逐字节语义不变。
- 先执行普通 `validate --repo <repo> --contract -`，再执行：

  `codex-builder-loop assurance --experimental-v4 validate-decision --repo <repo> --run <run> --session-id <session-id> --problem-key <key> --action-id <action-id> --facet <facet> --facet-digest <digest> --contract -`

- 只有两次都返回 `status=READY` 才在方案正文放入唯一完整 `assurance-v4-contract` marker，并额外放入一个
  只含交接身份、不是第二份 contract 的 marker：

~~~~markdown
<!-- assurance-v4-decision -->
```json
{"schema_version":1,"run_id":"...","problem_key":"...","action_id":"...","facet":"authority","facet_digest":"..."}
```
<!-- /assurance-v4-decision -->
~~~~

- Planner 不更新 ledger、不 resume run。validation 失败或变化需要 successor 时不得输出该 decision marker。
  正文中的“失效 gate”使用 `validate-decision.invalidated_evidence`，不靠 facet 名称猜测。
