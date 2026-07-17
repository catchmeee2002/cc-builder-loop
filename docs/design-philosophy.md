# Codex Builder Loop 设计哲学

本项目不是第二套 Codex runtime，而是 Codex 原生推理和调度之下的独立判据契约层。模型能力越强，越需要一个不能靠语言说服的交付地基。

## 零、独立判据编排器

项目定位是为自动化交付提供独立于被审计者的 ground truth。Codex 负责理解、实现和协调；builder-loop 只固定完成条件、角色边界、证据和 Git 事务。

## 一、判据按独立性分层

- 机器判据以命令退出码、文件树和 Git HEAD 提供最强 ground truth。
- Tester 依据 Planner 冻结的目标在独立 thread 中写测和黑盒验收，角色契约禁止读取 Builder
  候选实现。v1 不宣称具备操作系统级读 ACL；runtime 机械保证的是 worktree 与写 ownership。
- Reviewer 检查代码、测试、计划和文档语义，但不能单独覆盖机器或 Tester 失败。

低独立性判据不能推翻高独立性判据，只能补充其无法表达的质量判断。

## 二、每个事实只有一个家

计划定义目标，Tester branch 定义测试实现，runtime ledger 记录执行事实，schema 定义结构，Git 保存交付历史。消费者只引用源头，不维护旁路副本。

## 三、显式授权，默认拒绝

Builder 和 Tester 只写计划声明的路径。无法识别的文件、dirty change、agent thread、evidence 或 Git 状态一律不推断归属；保留现场并停止。

## 四、改输入条件，不堆输出特判

通过同基线 worktree、冻结计划、测试所有权和 evidence HEAD 让正确路径自然发生。Hook 只做身份记录和完成门禁，不解析 transcript，也不重建 phase 编排器。

## 五、连续性属于 thread 身份

同名新 Reviewer 或 Tester 不等于原角色。后续 iteration 必须 follow-up 原 agent thread；continuity 丢失时明确失败，不用新上下文伪装续接。

## 六、契约先于实现

CLI 子命令、JSON 状态、计划 marker、agent 输出和副作用先固定，再写实现。Fixtures 验证契约和最终 Git tree，不绑定内部函数或自然语言措辞。

## 七、原生能力优先，确定性脚本兜底

Plan mode、Skills、custom agents 和 multi-agent coordination 由 Codex 提供。只有 schema 校验、测试执行、文件所有权、evidence 和 Git 收尾进入本项目脚本。原生能力增强时应能继续删除适配代码。

## 八、优化单任务可靠闭环

目标是稳定完成一到数小时的单任务交付，不追求无限续跑。达到迭代上限、无进展、需要改变测试目标或出现架构取舍时，把决定交还用户。

## 九、项目自身遵守同一原则

README 解释用户入口，AGENTS.md 提供仓库导航，本文件保存设计原则，架构细节进入 architecture，当前运行状态进入 ledger/报告，历史进入 CHANGELOG。禁止为了方便复制原则正文。
