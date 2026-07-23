# Codex Builder Loop 设计哲学

本项目不是第二套 Codex runtime，而是 Codex 原生推理和调度之下的独立判据契约层。模型能力越强，越需要一个不能靠语言说服的交付地基。

## 零、独立判据编排器

Codex 负责理解、实现和协调；builder-loop 只固定完成条件、角色边界、证据和 Git 事务。认知能力可以由原生平台演进，独立判据不能因此消失。

## 一、判据按独立性分层，并绑定真实输入

- 机器判据以命令退出码、文件树和 Git 对象提供最强 ground truth。
- Tester 依据 Planner 冻结的目标在独立 thread 中写测和黑盒验收。
- Reviewer 检查代码、测试、计划和文档语义，但不能覆盖机器或 Tester 失败。

Evidence 绑定的是产生结论的真实输入 digest，不是碰巧承载这些输入的全局 HEAD。只有冻结 scope 内的输入完全不变时，机器或黑盒证据才能跨 HEAD 复用；Reviewer 和文档审查始终面对完整 integrated HEAD。

## 二、每个事实只有一个家

计划定义目标和授权，Tester branch 定义测试实现，runtime ledger 记录执行事实，schema 定义结构，Git 保存交付历史。消费者只引用源头，不维护旁路副本。兼容输出必须由唯一事实派生，不能反向成为第二状态源。

工程事故也必须只有一个责任仓库。业务项目缺陷、builder-loop 缺陷和外部平台缺陷分别落到自己的
问题容器；跨边界因果链先拆成可独立复现、修复和关闭的原子事故，再用链接表达关联。Memory 只保存
无法工程固化的稳定隐含知识，不能替代代码、测试、契约、项目文档或 issue。

## 三、显式授权，默认隔离

不认识的状态不碰，也不默认把整个任务停掉。未授权 dirty、文件和 worktree 默认留在原处且不进入任务；只有无法证明它与候选写入互不干扰，或同步会覆盖它时才停止。

用户明确授权的 dirty 输入必须冻结为 exact-path、content-bound 契约。授权不是清理权：runtime 不删除无关缓存、日志、环境文件或未知 worktree。

## 四、改输入条件，不堆输出特判

通过同基线 worktree、冻结计划、不可变 workspace snapshot、测试 ownership 和 evidence scope 让正确路径自然发生。Hook 只记录身份并做完成门禁，不解析 transcript，也不重建 phase 编排器。

## 五、同类问题出现三次就是架构缺陷

同一失败在不同候选上反复出现，说明缺少抽象、边界或正确问题定义。runtime 应把重复事实结构化并停止，让用户重新审视架构；不能用更多重试掩盖。

## 六、连续性属于 thread 和事务身份

同名新 Reviewer 或 Tester 不等于原角色；同名新提交也不等于已审 candidate。Agent 后续 iteration 必须 follow-up 原 thread，Git 恢复必须续接同一个持久化 intent。连续性丢失时保留现场，不伪装续接。

## 七、契约与成熟行为先于实现

CLI、JSON、计划 marker、ledger、agent 输出和副作用先固定，再写实现。迁移不能只证明新实现内部自洽：成熟行为与对应 fixtures 必须逐项标为等价覆盖、优化替代或明确退役，且退役理由可审查。

## 八、原生能力优先，但替代必须证明等价

Plan mode、Skills、custom agents 和 multi-agent coordination 由 Codex 提供。Git workspace 边界、事务恢复、ownership、evidence、诊断和收尾属于本项目的确定性责任，不能因为原生平台“看起来能做”就删除。

删除适配代码前必须列出旧语义、原生覆盖面、剩余确定性责任和迁移测试。原生能力只能替代其真正覆盖的层。

## 九、优化单任务可靠闭环

目标是稳定完成一到数小时的单任务交付，不追求无限续跑。达到迭代上限、无进展、重复架构失败、测试目标变化或出现产品取舍时，把决定交还用户。

## 十、项目自身遵守同一原则

README 解释用户入口，AGENTS.md 提供动手时必须触发的仓库规则，本文保存设计原则，architecture 保存工程推导，ledger/doctor 保存运行事实，历史进入 CHANGELOG。

设计原则、公共契约或角色边界的增删改属于产品决策，必须在计划中单独披露并取得用户接受；不得混在“adapter 简化”“重构”或普通实现细节中。
