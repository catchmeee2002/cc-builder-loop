# Native App Server 稳定性压测交接

本文是 `cc-builder-loop` Native App Server 稳定性工作的交接入口，面向
`#217`。它冻结问题边界、观测要求和执行顺序；运行快照、原始日志和压测结果不写在本文，
分别以 GitHub Issue、结构化 diagnostic receipt 和压测报告为准。

[保质期: #217 关闭, owner: Native Driver maintainer, 正向归宿: GitHub Issue #217 与结构化 evidence]

## 交接结论

下一阶段不是继续扩大 #216 的恢复逻辑，而是单独确认 App Server 频繁异常的触发条件、
可观测性和恢复边界。顺序固定为：

1. 先补足可关联到 process、transport generation、RPC method、thread、turn 和 cleanup
   的有限日志。
2. 再用可重复的本地 fixture 和受控 App Server 运行做压力/故障注入。
3. 最后依据结构化观察决定是否需要修复、调整 admission 或另开外部平台问题。

在得到证据前，不修改 timeout 数值、retry 上限、协议降级策略或 Reviewer replacement 语义。

## 与 #216 的边界

`#216` 已实装并推送，解决的是 Native Driver 在 role dispatch 前没有持久化 activation
事实的问题：

- `dispatch_intent` 在 `thread/resume` 或 `turn/start` 前先写入 `activation_state=pending`。
- 已知 transport failure 复用同一 action/thread 的有界 retry。
- Tester 首个 turn 前的 `no rollout` 在 source、candidate、target 无副作用时转入
  continuity replacement。
- 无法证明激活是否产生副作用时保留 active dispatch，进入 `NEEDS_USER`。

这些规则保证失败现场不再丢失，但不能证明 App Server 本身稳定。#217 必须把“恢复逻辑
正确”与“服务在压力下不会异常”分成两条独立证据链。

## 已知现象

历史 Native 运行曾观察到两类前置失败：

1. App Server 在 Tester activation 附近关闭输出，表现为 transport disconnect。
2. `thread/resume` 返回 `no rollout found`，当时由于 dispatch 尚未先落账，现场无法完整
   关联到 action 和 generation。

上述内容是触发压测和日志工作的事实，不是根因结论。新的运行必须从当前 process identity、
generation 和完整 RPC 生命周期重新采集，不能用旧日志或 Agent 自述代替。

## 第一阶段：诊断日志

修改范围优先限制在 Native App Server transport 和其测试 fixture。每个失败至少应能关联：

- App Server child process identity、generation、启动和退出状态；
- RPC method、request id、thread id、client action/turn id；
- 生命周期阶段：initialize、thread start/resume/read、turn start/stream、timeout、
  disconnect 和 cleanup；
- 原始错误 code、归一后的 failure classification、returncode、耗时和 wire sequence；
- 有界、脱敏的 stderr receipt，包括截断状态、字节数和 digest。

日志要求：

- 不记录 API key、Authorization header、token 或完整敏感请求；
- stderr 读取不能阻塞 protocol stdout reader；
- 原始长日志写入 candidate 外的受控 artifact，ledger 只保存必要的 receipt 和 digest；
- 失败、清理未知和恢复结果必须能与唯一 dispatch intent 对齐；
- 日志增强不能改变现有成功/失败分类，也不能制造 PASS。

## 第二阶段：压测矩阵

先跑不依赖真实服务状态的本地 fixture，再跑当前 admission 允许的真实 App Server。每类
故障单独记录，避免把多个原因混成一次“压力失败”：

- child startup、initialize 或 protocol canary 失败；
- `thread/start`、`thread/resume`、`thread/read` 请求断开或返回错误；
- `turn/start` 后 stream disconnect、turn timeout、空结果和部分结果；
- stderr 持续输出或输出量异常时的 reader backpressure；
- cleanup、process-group 回收和下一 generation 启动；
- 连续故障下同一 action/thread 的 retry、exhaustion 和 resume。

每次测试至少保存 action、generation、attempt、thread、turn、RPC method、process identity、
failure code、classification、cleanup observation 和最终 ledger phase。重复失败的次数和
阈值必须在 #217 的结构化测试契约中预先冻结，不在本文临时追加。

## 验收边界

#217 的结果只有在以下事实都可回读时才有效：

- 能从日志把一次异常定位到唯一 process/generation/RPC 生命周期；
- 同一故障在相同输入下可重复，且不同故障不会被错误归并；
- 大量 stderr、断流和 timeout 不造成 reader 死锁或失控增长；
- recovery、retry、exhausted 和 `NEEDS_USER` 与 ledger 事实一致；
- 未产生副作用的失败不会伪造 turn/result/evidence；
- 需要修改产品契约、外部目标或协议语义时，停止并另走对应 decision/issue 路由。

## 代码与验证入口

优先阅读和修改：

- `runtime/codex_builder_loop/native_driver/app_server.py`
- `runtime/codex_builder_loop/native_driver/transport_failures.py`
- `runtime/codex_builder_loop/native_driver/coordinator.py`
- `runtime/codex_builder_loop/native_driver/cli.py`
- `tests/test_native_driver_v1.py`
- `docs/architecture.md`

基线验证：

```bash
python3 -m unittest tests.test_native_driver_v1
bash scripts/verify-all.sh
```

压测新增的原始日志、过程快照和统计数据放在 candidate 外的 artifact/report 目录，并在
`#217` 中绑定命令、输入、returncode、process/generation identity 和报告 digest。

## 设计依据

本交接遵循 [设计哲学](design-philosophy.md)：

- **每个事实只有一个家**：transport observation 进入 receipt、ledger 或结构化报告，本文
  只保留稳定规则，不复制动态日志。
- **改输入条件，不堆输出特判**：先补齐能区分 startup、RPC、stream 和 cleanup 的观测，
  再决定修复；不通过增加 retry 或放宽分类掩盖问题。
- **连续性属于 thread 和事务身份**：压测必须区分同一 thread/generation 的恢复和新
  process 的重新绑定，不能用相同名称代替连续性。
- **独立判据绑定真实输入**：结论必须绑定实际命令、fixture、process identity、日志
  artifact 和 returncode，不能只凭 exit 0 或 Agent 自述。
- **同类问题出现三次就是架构缺陷**：同一 failure 在不同输入或运行中重复出现时，应
  先审视边界和问题定义，再决定是否继续扩展实现。

## 新 session 交接步骤

1. 读取本文、`docs/design-philosophy.md`、`docs/architecture.md`、`docs/known-issues.md`
   和当前 `AGENTS.md`。
2. 检查当前 Git、宿主 load、D-state、磁盘等待和 App Server admission；不满足安全前提时
   只做诊断，不启动长程压力。
3. 先复现已有 fixture 的 startup、activation、turn 和 cleanup 基线，确认日志字段完整。
4. 在 clean development worktree 中实现日志与最小 fixture，先通过 focused tests。
5. 冻结 #217 压测契约后执行故障矩阵；原始 artifact 不进入 candidate 或普通文档。
6. 将根因、触发条件和验收结果分别写回 #217；若归属外部平台，另建原子问题，不把
   Native Driver 修复和平台事故合并。
