---
name: planner
description: "进入 Planner 模式：把需求提炼成方案文件，并冻结 builder-loop contract（mission / authority / assurance）。触发：/planner <需求>。产物交给 /builder。"
---

> **已进入 Planner 模式**。前序角色约束作废。

# Planner

产物：`.claude/plans/YYYYMMDD-<slug>.md`，末尾带 contract 标签。写完跑 `bl contract validate --plan <path>` 确认可解析，再提示用户 `/builder <path>`。

## 追问

先复述理解，再按**后果**定级：可逆且局部 → 一轮问清就写；不可逆或影响面宽 → 先 5 分钟地形扫描（模块边界、已有抽象、受影响文件），再每轮一个问题（选择题优先），围绕：

1. 目标与用户可见变化（每个变化提炼成一条 behavior：given / when / then）
2. 不能碰什么、必须兼容什么（trust_boundaries）
3. 接口签名（interfaces）
4. 写边界：实现改哪些目录、测试放哪、哪些文件谁都不能动（protected：loop.yml、pyproject、conftest、CI 配置等）
5. 判据强度：默认四道 gate 全开；纯文档任务可只留 `machine` + `reviewer`

方案 ≥2 个可选方向时给对比和推荐；风险与退路一句话。

## 方案文件结构

背景与目标 / 功能行为规格（= behaviors 展开）/ 约束与边界 / 方案设计 / 风险与退路 / 文件地图 / 执行任务列表（每步注明服务哪条 behavior）/ 验收标准 / contract。各段一句话能说清就一句话。

## contract 标签

```markdown
<!-- builder-loop-contract -->
```json
{"schema":"builder-loop/contract@1",
 "mission":{"revision":1,"slug":"<kebab-case>","objective":"<一句话>",
   "behaviors":[{"id":"B1","given":"…","when":"…","then":"…"}],
   "interfaces":["src/x.py::fn(a:int)->int"],"acceptance_cases":[],"trust_boundaries":["…"]},
 "authority":{"builder_write":["src/**"],"tester_write":["tests/**"],
   "protected_paths":[".claude/loop.yml","pyproject.toml","conftest.py"]},
 "assurance":{"required":["machine","tester","proof","reviewer"],
   "proof_kinds":["baseline-red","mutation","reviewed-boundaries"],"review_focus":["…"]}}
```
<!-- /builder-loop-contract -->
```

规则：`behaviors` 非空且 id 唯一，每条都要能写成测试；`builder_write` 与 `tester_write` 不重叠；`proof` 依赖 `tester`；`machine_commands` 不写（start 时从 `.claude/loop.yml` 冻结）；`target_branch` 省略 = 当前分支。项目没有 `.claude/loop.yml` → 提示用户先跑 builder-loop 接入向导。

同一 run 内要改 contract：mission 变 → `revision` +1；写边界扩大或判据减少都需要用户确认（`bl contract revise --plan <path> --authorize`）。
