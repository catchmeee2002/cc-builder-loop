# 离线 Issue 分流实验

本模块评估：现有项目原则和事故现场是否足以让 Agent 正确判断根因，并分别判断技术证据状态、
人类注意力和实施前范围盘点，再推导下一工作队列。它可以按需运行，也可以由本机 cron 周期扫描；两种
入口都只产出私有影子结果，不进入 builder-loop runtime、ledger 或交付门禁。

## 边界

- `issue_triage_eval.py` 对固定样本执行诊断、攻击、聚类、三轴和工作队列评分。
- `issue_triage_shadow.py` 只读抓取一个真实 GitHub Issue、项目原则和仓库证据，生成私有影子结果。
- `issue_triage_poller.py` 每 10 分钟扫描三个受管仓库中新建或更新的契约化 Issue，并在本地保存预测、
  重试状态和结案对照。
- 模型请求只发送角色合同和任务材料，固定 `tools=[]`、`store=false`；不会向模型暴露本地文件系统。
- 影子运行不评论或修改 Issue，不改标签、Planner、代码仓库或 builder-loop ledger。
- 手工 shadow 结果写入 `~/.codex/issue-triage/runs/`；poller 的结果与状态写入
  `~/.codex/issue-triage/poller/`。实验分数和模型配置不提交到项目文档。

周期流水线首次安装时记录 `enabled_at`，只处理此后创建且带有效、唯一的 `issue-capture:v1` 或
`issue-capture:v2` 的 Issue，默认不
回灌历史。每个预测必须在 `incident_head` 的 detached worktree 中运行，事故 commit 不存在时拒绝回退
当前 HEAD。`dirty=true` 只能恢复事故 commit 中的项目原则，不能恢复未提交文件，因此模型只读取创建
正文，不读取文件摘录或 identifier 命中。

预测幂等键绑定 repository、Issue、`incident_head`、capture digest、schema 和 prompt hash。单个 Issue
失败进入待重试，不阻塞其他 Issue；创建正文或 capture 在首次观察后漂移时停止继续预测。Issue 在首次
预测前已经关闭或已经出现 `issue-resolution:v1` 时记录 `missed_prediction`，禁止根据结案事实事后补
预测。

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

`$file-github-issue` 在新建正文中写入 `issue-capture:v2`，历史 v1 保持只读兼容；关闭评论仍写入
`issue-resolution:v1`。capture 保存事故版本、当时根因状态和独立的 Builder-loop runtime 身份，
resolution 保存最终根因、人类决策、验收证据和剩余不确定性。混用、重复或字段与 marker 版本漂移的
capture 一律拒绝。

有效的历史评测必须满足：

1. 模型输入只包含创建时正文和允许的事故现场，不读取结案评论、修复提交或影子预测；
2. 仓库证据绑定 `incident_head`，不能从已经修复的当前 checkout 反推事故；
3. 关闭评论只记录事实，不直接填写影子路线；路线由评测器根据决策和验收事实推导；
4. 普通 Agent 结案记录只能作为低置信对照；有人类根因纠正、明确取舍或独立审查证据的样本才用于
   判断危险错放。

结案 Gold 由契约确定性推导：非 `fixed`、根因未确认、有人类修正根因或仍有剩余不确定性时，技术轴为
`needs_evidence`；目标/原则选择、取舍或非确定性验收进入第一性裁决；公共范围批准进入成组批准；其余
不要求人类注意力。真实结案契约不能证明实施前是否需要系统盘点，因此结案评分不评价 scope 轴。累计
安全指标重点记录危险自动执行、低估人类注意力和无谓打断。

现有固定 fixtures 用于验证路由和攻击协议。批量聚类、样本置信分层和从影子证据晋级到自动执行的门禁
仍通过本仓 GitHub Issue 跟踪，不在稳定文档中维护进度快照。

## 运行

```bash
python3 experiments/issue-triage/scripts/issue_triage_eval.py --help
python3 experiments/issue-triage/scripts/issue_triage_shadow.py --help
python3 experiments/issue-triage/scripts/issue_triage_shadow.py shadow \
  --repo /path/to/repository --issue 123
python3 experiments/issue-triage/scripts/issue_triage_poller.py install-cron
python3 experiments/issue-triage/scripts/issue_triage_poller.py run
python3 experiments/issue-triage/scripts/issue_triage_poller.py status
python3 experiments/issue-triage/scripts/issue_triage_poller.py uninstall-cron
python3 -m unittest discover -s experiments/issue-triage/tests -p 'test_*.py'
```

poller 状态位于 `~/.codex/issue-triage/poller/`。cron 安装只维护带
`cc-builder-loop:issue-triage-poller` marker 的唯一受管行，保留其他 crontab 内容；外层 `flock` 与
进程内锁共同阻止重叠运行。

项目原则来源由 `profiles/projects.json` 声明。新增项目时只引用该仓已经存在的原则文档，不复制原则
快照；缺少可读取的原则源时拒绝运行。
