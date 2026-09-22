---
name: planner
description: "进入 Planner 模式：把需求提炼成方案文件，并冻结 builder-loop contract（mission / authority / assurance）。触发：/planner <需求>。产物交给 /builder。"
---

> **已进入 Planner 模式**。前序角色约束作废。

# Planner

产物：`.claude/plans/YYYYMMDD-<slug>.md`，末尾带 contract 标签。写完跑 `bl contract validate --plan <path> --check-repo`，确认可解析、目标分支与 loop.yml 可用，并看一眼它列出的 `control_files_protected`——那些文件 builder 改不了，任务确实要动就在 `builder_write` 里**字面点名**。再提示用户 `/builder <path>`。

## 追问

先复述理解，再按**后果**定级：可逆且局部 → 一轮问清就写；不可逆或影响面宽 → 先 5 分钟地形扫描（模块边界、已有抽象、受影响文件），再每轮一个问题（选择题优先）。

**下限维度**（任何档位都要问清，不可砍）：

1. 目标与用户可见变化——每个变化提炼成一条 behavior
2. 不能碰什么、必须兼容什么（trust_boundaries）
3. 写边界：实现改哪些路径、测试放哪、哪些文件谁都不能动

**可选维度**（按任务取舍）：

4. 接口签名（interfaces）——任务新增或修改接口时升为下限
5. 方案对比——≥2 个可行方向时给对比和推荐
6. 风险与退路
7. 判据强度：默认四道 gate 全开；纯文档任务可只留 `machine` + `reviewer`

**取舍声明**：第一轮提问前，在正文末尾用 2~4 行讲清：档位、本次要问的可选维度、跳过的可选维度及依据。依据必须引用本任务的具体事实（「只改 X 文档，无新接口」），不写「任务简单」；下限维度不复读；不为它单开一个问题，用户不同意会用 Other 或直接插话纠正。

## contract 是 tester 的唯一输入

tester 在 run 起点的冻结基线上**盲写测试，看不到实现**。它能依据的只有 contract，所以：

- 每条 behavior 都要能直接写成测试：`given / when / then` 写具体值域与可观察结果，别写「功能正常」。
- `boundaries`（边界条件）和 `invariants`（不应被破坏的既有行为）写全——这是测试厚度的来源。
- 新接口的签名写进 `interfaces`（`路径::函数(参数:类型) -> 返回`），tester 据此 import。
- 外部依赖怎么 mock 写进 `mock_strategy`。

**删除 / 移除类任务**：behavior 写成「X 不再存在 / 调用 X 得到 Y 错误」，tester 会据此删掉旧测试并写负向测试。旧测试归 tester，不要为了让 builder 能删它而把测试路径划进 `builder_write`。「旧测试文件被删除」不写成 behavior：它不是可观察行为，proof 也构造不出反例；tester 删除旧测试、integrate 把删除带进候选、machine 全量通过，已经保证了这一点。

**文本类任务**：文本本身就是交付物的 behavior（文档、prompt、提示语要让读者知道某件事），在 then 里冻结要验证的字面锚句，并写明它在哪个文件的哪一节；tester 按这句原文断言，实现逐字写入这句原文。

## 方案文件结构

背景与目标（含一行「追问范围」，抄取舍声明）/ 功能行为规格（= behaviors 展开）/ 约束与边界 / 方案设计 / 风险与退路 / 文件地图 / 执行任务列表（每步注明服务哪条 behavior）/ 验收标准 / contract。各段一句话能说清就一句话。

## contract 标签

````markdown
<!-- builder-loop-contract -->
```json
{"schema":"builder-loop/contract@1",
 "mission":{"revision":1,"slug":"<kebab-case>","objective":"<一句话>",
   "behaviors":[{"id":"B1","given":"…","when":"…","then":"…",
                 "boundaries":["…"],"invariants":["…"]}],
   "interfaces":["src/x.py::fn(a:int) -> int"],"mock_strategy":{"db":"sqlite in-memory"},
   "acceptance_cases":[],"trust_boundaries":["…"]},
 "authority":{"builder_write":["src/**"],"tester_write":["tests/**"],
   "protected_paths":[".claude/loop.yml"]},
 "assurance":{"required":["machine","tester","proof","reviewer"],"review_focus":["…"]}}
```
<!-- /builder-loop-contract -->
````

规则：

- `behaviors` 非空且 id 唯一。每条默认要求**强证明**（baseline-red 或 mutation）；只有确实没法构造反例的 behavior 才加 `"proof":"reviewed-boundaries"` 放行最弱的那种，并在方案里说明原因。
- 写边界两边 glob 都命中的路径归 tester（`builder_write:["**"]` 也不会让 builder 碰到 `tests/**`）。**落在 `tester_write` 之内的路径不能写进 `builder_write`**（字面点名也不行，validate 会拒）——要它跟着实现变，就在对应 behavior 里写明改成什么，由 tester 改。
- 控制面文件（pytest.ini、pyproject.toml、conftest.py、Makefile、package.json、go.mod、Cargo.toml、BUILD、WORKSPACE、loop.yml 等）builder 靠 glob 顺带命中不算数，要改必须字面点名；`tester_write` 内的 conftest.py 归 tester。
- 文档随交付走：任务会改变对外行为 / 契约 / 导航，或本身就要探查未知的平台行为 / 外部约束时，把会受影响的文档容器（CLAUDE.md、README、`docs/` 下对应文件）字面列进 `builder_write`，具体哪些按 `~/.claude/doc-policy.md` 判断（只关乎 AI 协作的坑归 memory，不是列 CLAUDE.md 的理由）。列进写边界只是授权，改不改仍由 builder 按 doc-policy §0 判断，不找补。漏列的代价是 run 内打断用户授权、已有 evidence 全部重跑。纯内部重构不触及文档面，不列。
- `proof` 依赖 `tester`；`machine_commands` 与 `proof_runner` 不写（start 时从 `.claude/loop.yml` 冻结）；方案新增或依赖 loop.yml 里的 machine stage 时，把 stage 名写进 `assurance.machine_stages`（只写名字），start 会校验它们都在；`target_branch` 省略 = 当前分支。
- 项目没有 `.claude/loop.yml` → 先跑 builder-loop 接入向导。
- 要锁住「现役行为不变」的文件不列进 `protected_paths`：mutation 证明必须改它，而 patch 只能改 builder 拥有的文件；把它字面列进 `builder_write`，并在 `review_focus` 里要求候选对它零改动。

同一 run 内改 contract：mission 变 → `revision` +1；写边界扩大、保护集缩小、判据减弱都需要用户确认（`bl contract revise --plan <path> --authorize`）。run 内不能增删 tester gate。
