# 角色行为实验场

这个模块用于比较角色指令是否在正确场景触发，并识别漏触发或误触发。它不是交付门禁，也不调用
真实模型。

## 文件

- `scenarios.json`：版本化场景、触发类型、机械检查和待人工判断标准。
- `variants.json`：基线或按角色匹配的指令来源路径及摘要；不复制第二份角色规则。
- `runner.py`：只提供离线 `prepare` 和 `score`。

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
`score` 只做字符串级机械检查，并把语义判断明确标为 `semantic_pending`。

真实响应和评分结果只能由调用方写到仓库外，或忽略目录
`.builder-loop/experiments/agent-behavior/`。不得提交结果、写 runtime ledger，或把实验结果冒充
Tester、机器验证和 Reviewer 证据。
