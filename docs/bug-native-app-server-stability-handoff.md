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

## 本轮调查结果：根因已实锤

[保质期: 2026-08-31, owner: Native Driver maintainer, 正向归宿: #217 与仓库外结构化报告]

本轮已把 `NATIVE_APP_SERVER_DISCONNECTED` 的主要原因从“App Server 可能崩溃”
收敛为 Native transport 的帧读取错误；原始 run 的确切 stderr 仍因旧版
root-session 收尾缺陷没有保存。原始现场、受控复现和独立压力机制继续分开记录。

### #217 原始现场已确认的事实

- `vehicle-jumpbox-startup-compat-recovery-20260831` 只绑定了一个新的 Native
  stdio child；ledger 记录其 process identity、generation 和 `tester_author`
  dispatch。
- 该 dispatch 的 `activation_state=pending`、`turn_id=null`，说明失败发生在
  线程激活阶段，而不是 Tester 已经产生结果之后。
- 既有 Tester thread 是根 session 通过 `spawn_agent` 建立并已经完成过多个 turn
  的 `01a05860...`；原始 artifact 没有证明“先创建未落盘 thread、关 child、再恢复”
  这条调查者构造的流程。
- 原始错误是 `Codex App Server closed its output`；旧收尾路径没有把原始 stderr、
  child exit code 和 transport cleanup receipt 写回 ledger。

### 主要根因：Native transport 错误地判定 EOF

生产代码 `[app_server.py](../runtime/codex_builder_loop/native_driver/app_server.py:817)`
原来只调用一次 `os.read(..., 65536)`；只要这次读取没有 `\n`，就直接抛出
`NATIVE_APP_SERVER_DISCONNECTED`。`thread/resume` 返回的完整 JSON 可以超过 64 KiB，
所以一次 read 拿到半帧并不代表 child 已关闭。

受控复现使用与事故相同的 `codex-cli 0.145.0`、effective `CODEX_HOME` 和真实
Tester thread：生产 transport 已发出 `thread/resume`，读到 11 条通知后在 65536
字节的半帧处报断流；继续读取 25145 字节后，拼出的 90145 字节首帧是合法的
`id=2`、带 `result`、无 `error` 的 response，且当时 child 仍未退出。临时把读取
循环改成“在总 timeout 内持续拼帧”后，同一输入连续 2 次成功。

因此，`#217` 的主要 `output closed` 结论是：**Native Driver 把合法的
大 JSON-RPC response 半帧误报成 App Server 断流，不是已证实的 App Server
自身崩溃。** 原始 PID 的半帧没有被旧版本保存，因此对原始单进程只能说是
同版本、同 home、同真实 thread 的高置信复现，而不伪造原始 stderr。

### 独立的次要机制：共享 CODEX_HOME 启动争用

调查者另用生产 `AppServerTransport` 主动启动 12 个同版本 child，全部访问同一
`CODEX_HOME` 和同一真实 thread：8 个在 `initialize` 阶段以 `returncode=1` 退出，
stderr 明确为 `failed to initialize sqlite state runtime`；其余 4 个在恢复阶段
出现 output-closed。该压力批次证明共享状态争用是独立可复现机制，但**不是原始
run 启动了 12 个 child 的证据**，也没有把它冒充本次主要根因。

### 证据丢失缺陷与当前修复

root-session handoff 在 `[cli.py](../runtime/codex_builder_loop/native_driver/cli.py:519)`
局部创建 transport；异常离开该函数后，outer fatal 路径拿不到它，导致
`diagnostic_receipt` 和 `record-transport-cleanup` 都没有执行。当前 candidate 已：

- 在一个总 timeout 内持续读取并拼接 newline-delimited JSON frame；
- 真正 EOF 时记录 partial frame 的字节数和 digest；
- 把 root-session transport 交给 outer fatal 收尾，持久化 stderr/cleanup；
- 增加大于 64 KiB frame 和 root-session fatal receipt 回归测试。

`tests.test_native_driver_v1` 全套 93 项通过。动态日志、PID、frame digest、
压力矩阵和命令回读保存在：
`/mnt/hongyu.liao_docker/native-app-server-stability-artifacts/20260831/report-v3-root-cause.json`
（SHA-256 `dc09cb9f575efd728197c35c3c1c0f16be2ebbfe607cfb9f4ba8282aa0ce3063`）。

这次修正遵循设计哲学：**每个事实只有一个家**，动态数据归报告；
**改输入条件，不堆输出特判**，修复帧边界而不扩大 timeout/retry；
**连续性属于 thread 和事务身份**，用真实 thread、process 和 RPC 复现而不臆造流程；
**独立判据绑定真实输入**，回归测试同时绑定 frame 边界和收尾 receipt。

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
