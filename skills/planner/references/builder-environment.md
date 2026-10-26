# Builder 环境与前置产物

仅在任务涉及 dirty intake、串行公开前置产物、受保护准备、外部部署，或准入报告 runtime_support
冲突时读取。已有 run 的处理同时遵循 [生命周期约束](builder-lifecycle.md)。

- target dirty 默认不进入任务。任务确实依赖 dirty 时，先用选项卡取得 exact-path 授权，再计算每个
  普通文件当前内容的 SHA-256；不得 stash、复制、清理或授权目录/glob。
- 串行公开前置产物按 schema 冻结精确路径和 ownership；只能消费 Core 确认的当前 Builder checkpoint
  或一致的 carryover。不得把旧角色产出冒充本轮产出，publication 修复遵循既有 recomposition 事务。
- `validate --repo <repo>` 返回 self-hosted `runtime_support` 冲突时，
  必须改为 protected preparation，移除 `affected_gates` 并加入全部 `required_independent_gates`。
- protected preparation 使用 `mission.delivery_kind=preparation` 与 exact `protected_support_paths`；后续
  business contract 只通过 `execution.continuation` 消费 Core 已验证的 finalized run 事实。不得解析
  transcript 重建 continuation，也不得让 preparation supersede 业务 run；事实不足时停止规划交接。
  生命周期参考中的 rejected-checkpoint 例外只允许新的 preparation successor 承接未完成的 preparation candidate，
  后续 business run 仍必须消费 finalized continuation。
  `runtime_support.affected_paths` 是自托管 runtime 写边界的唯一来源；不得让同一 run 热切换 evidence
  writer、补写旧 observation，或用 candidate 改写 manifest 后继续当前 proof gate。
- 拆分公开前置文件准备任务时，将尚未就绪的精确路径纳入对应 Builder 单元 scope，并确保准备单元
  在 publication 前可达，不依赖 Tester/Reviewer。当前单元必须完成其 scope 内的前置文件；仅后续
  合法 Builder 单元负责的文件允许暂缓。Tester 仍等待完整 publication，不把单元完成当成全局就绪。
- 需要真实环境时，只允许计划冻结一个 `authority.external_targets` 目标和项目提供的
  `execution.deployment` probe/build/deploy/restore wrapper。若当前 probe 证明同一目标已经承载同一候选
  制品，Core 可跳过重复 deploy，但本 Revision 的 blackbox 仍重新执行。只有用户明确允许 Revision
  期间保留目标状态时才冻结 `revision_retention: lease`，否则使用默认恢复；通用远程部署编排和多仓
  原子事务超出支持范围，停止交接并由用户决定路线。

受保护 preparation 的基线对照若属于验收条件，应在冻结命令中输出可核对的输入身份与具体行为差异，不依赖尚未交付的补充观察通道完成自举。
