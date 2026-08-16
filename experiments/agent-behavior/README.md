# 角色行为实验场

这个模块用于比较角色指令是否在正确场景触发，并识别漏触发或误触发。它不是交付门禁，也不调用
真实模型。

## 文件

- `scenarios.json`：版本化场景、触发类型、机械检查和待人工判断标准。
- `variants.json`：基线或按角色匹配的指令来源路径及摘要；不复制第二份角色规则。
- `runner.py`：只提供离线 `prepare` 和 `score`。
- `canaries.json`：把高风险场景绑定到确定性 fixture 或安全宿主探针。
- `canary.py`：在仓库外准备 fixture、验证区分性前提，或运行不调用模型的宿主探针。

## 使用

```bash
python3 experiments/agent-behavior/runner.py prepare
python3 experiments/agent-behavior/runner.py score
```

无参数形式会离线遍历固定场景，分别输出可复现的准备结果和使用确定性假响应得到的机械评分；语义判断
始终保持待人工确认。定向检查单个场景时使用：

```bash
python3 experiments/agent-behavior/runner.py prepare \
  --scenario-id insufficient-evidence-stop \
  --variant-id builder-current

python3 experiments/agent-behavior/runner.py score \
  --scenario-id insufficient-evidence-stop \
  --variant-id builder-current \
  --response-file /tmp/response.txt
```

`prepare` 拒绝场景与变体角色不匹配的组合，并向 stdout 输出可直接交给模型的临时指令、场景请求、
指令来源、仓库提交和输入摘要；调用方自行决定是否调用模型，不在仓库内复制指令正文。
`baseline` 表示不附加角色指令，`request.instructions` 为空；角色指令变体才按摘要读取对应来源。
场景需要区分用户入口 Skill 与实际内部 role 时，可声明唯一 `variant_id`；runner 会拒绝用同角色的其他
指令面替代，避免把入口授权行为误当成 Builder/Tester/Reviewer 工作行为。
`score` 只做字符串级机械检查，并把语义判断明确标为 `semantic_pending`。

风险 canary 先机械证明场景确实能区分弱验收和目标行为，再交给调用方采集 fresh model 样本：

```bash
python3 experiments/agent-behavior/canary.py list
python3 experiments/agent-behavior/canary.py prepare \
  --case-id large-diff-review-depth \
  --output /tmp/builder-loop-large-diff-canary
python3 experiments/agent-behavior/canary.py probe \
  --case-id host-background-contention
```

`prepare` 拒绝仓库内目录，并要求弱检查按预期通过、区分性检查按预期失败后才返回 `READY`。需要
mutation 的 case 先在正确 baseline 上验证完整检查为绿，再只改 manifest 声明的普通文件，确认同一检查
变红并恢复原树；输出保留 baseline、mutation、失败检查和恢复摘要。每个 fixture case 声明的角色都必须
有独立场景，fresh 样本按场景采集，不能用一个角色的结果代替另一角色。输出绑定 fixture 文件摘要、
场景、角色和最少 fresh 样本数。`host-background-contention` 只证明既有宿主进程可
确定性阻塞后续验证且能被探针回收；它不证明进程来源、具体业务依赖或 Builder-loop 已具备归因能力。

生成的 fixture 只能位于仓库外。探针结果、真实响应和评分结果只能由调用方写到仓库外，或忽略目录
`.builder-loop/experiments/agent-behavior/`。不得提交结果、写 runtime ledger，或把实验结果冒充
Tester、机器验证和 Reviewer 证据。
