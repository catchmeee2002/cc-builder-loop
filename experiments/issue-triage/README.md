# 离线 Issue 分流实验

本模块评估：现有项目原则和事故现场是否足以让 Agent 正确判断根因，并分别判断技术证据状态、
人类注意力和实施前范围盘点，再推导下一工作队列。它只在需要校准时按需运行，不是实时服务，也不进入
builder-loop runtime、ledger 或交付门禁。

## 边界

- `issue_triage_eval.py` 对固定样本执行诊断、攻击、聚类、三轴和工作队列评分。
- `issue_triage_shadow.py` 只读抓取一个真实 GitHub Issue、项目原则和仓库证据，生成私有影子结果。
- 模型请求只发送角色合同和任务材料，固定 `tools=[]`、`store=false`；不会向模型暴露本地文件系统。
- 影子运行不评论或修改 Issue，不改标签、Planner、代码仓库或 builder-loop ledger。
- 运行结果写入 `~/.codex/issue-triage/runs/`，实验分数和模型配置不提交到项目文档。

## 分流契约

三个判断彼此独立：

- 技术根因已成立，或仍需 Agent 补证据；证据不足不自动升级给人。
- 不需要人、只需成组批准，或必须由人做目标、品味、设计原则取舍。
- 是否必须系统盘点多个消费者、契约边界、状态变体、兼容面或手写投影的权威成员全集；范围宽本身不
  要求人批准。

下一工作队列由确定性代码推导：技术证据不足时先交给 Agent 调查；根因成立后，再按第一性裁决、成组
批准、Agent 直接执行的顺序分流。公共契约、角色边界或难回退后果走成组批准；目标、品味、新设计原则
或原则冲突才进入第一性裁决。恢复已有原则、补齐既有契约遗漏和修正实现偏差不因此升级。

聚类按共同的底层失败机制和系统性修复纪律归组，不按标题、代码目录或引用了同一设计原则归组。多个
边界若都把异常、损坏或缺失投影成合法成功或空状态，可以由一次系统性 fail-open 审计共同覆盖；仅仅
现象相似则保持分开。红队分别判断单个 Issue 的根因和 cluster；聚类错误会进入聚类评分，但不能把已经
成立的单项根因降级为技术证据不足。

## 样本契约

`$file-github-issue` 在创建正文中写入 `issue-capture:v1`，在关闭评论中写入
`issue-resolution:v1`。前者保存事故版本和当时根因状态，后者保存最终根因、人类决策、验收证据和
剩余不确定性。

有效的历史评测必须满足：

1. 模型输入只包含创建时正文和允许的事故现场，不读取结案评论、修复提交或影子预测；
2. 仓库证据绑定 `incident_head`，不能从已经修复的当前 checkout 反推事故；
3. 关闭评论只记录事实，不直接填写影子路线；路线由评测器根据决策和验收事实推导；
4. 普通 Agent 结案记录只能作为低置信对照；有人类根因纠正、明确取舍或独立审查证据的样本才用于
   判断危险错放。

现有固定 fixtures 用于验证路由和攻击协议。GitHub 结案语料的自动抽取、事故 commit 隔离 checkout、
批量聚类和高置信评分属于后续路线，通过本仓 GitHub Issue 跟踪，不在稳定文档中维护进度快照。

## 运行

```bash
python3 experiments/issue-triage/scripts/issue_triage_eval.py --help
python3 experiments/issue-triage/scripts/issue_triage_shadow.py --help
python3 experiments/issue-triage/scripts/issue_triage_shadow.py shadow \
  --repo /path/to/repository --issue 123
python3 -m unittest discover -s experiments/issue-triage/tests -p 'test_*.py'
```

项目原则来源由 `profiles/projects.json` 声明。新增项目时只引用该仓已经存在的原则文档，不复制原则
快照；缺少可读取的原则源时拒绝运行。
