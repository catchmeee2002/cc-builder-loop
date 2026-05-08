# A 批：install.sh / uninstall.sh 一致性修复

> 来源：`.claude/improvements.md` 的 #16（install.sh has_entry 仅比脚本名）+ #17（uninstall.sh bl_scripts 漏 reviewer-timing-check.sh）。
> V3.0 启动前的基础设施前置项 —— 升级 hook matcher / 拆脚本时，install/uninstall 必须可靠。

<!-- role:shared -->

## 背景 & 目标

**背景**：本仓两个安装脚本各有一处机械漏洞，平时不显，改 hook matcher 或拆脚本时会暴露：

- `install.sh` 的 `has_entry()` 只比脚本名，不比 matcher。改了 matcher 重跑，settings.json 的 hook 条目不会更新，旧 matcher 一直留着，新加的字段（比如 `Read|Grep|Glob` 加上 `WebFetch`）永远不被识别。
- `uninstall.sh` 的 `bl_scripts` 列表只列了 5 个脚本，漏了 `reviewer-timing-check.sh`。卸载后 settings.json 该条目残留，再装一次会变成两条同样的 hook，CC 跑两遍。

**目标**：让 install/uninstall 满足两条不变量 ——

1. **改 matcher 重跑能跟上**：install.sh 跑完后 settings.json 的 hook 条目必须跟当前代码里的 registrations 表完全一致（matcher 字面相等才算一致）。
2. **装→卸能完全还原**：uninstall.sh 跑完后 settings.json 必须跟装之前一字不差（不残留任何 builder-loop 相关条目）。

**预估改动级别**：L2（改 install.sh 函数逻辑 + uninstall.sh 列表 + 新增 2 个 e2e fixture，不动状态机不动 hook 行为）。Builder 根据实际 diff 确认或修正。

## 验收标准

- `bash skills/builder-loop/fixtures/e2e/test-install-uninstall-roundtrip.sh` 通过（装→卸后 settings.json 与装前 byte-equal）。
- `bash skills/builder-loop/fixtures/e2e/test-install-matcher-update.sh` 通过（matcher 改了重装后该条目只剩新 matcher）。
- 现有所有 e2e fixture 仍 PASS，无回归。
- 真实 `~/.claude/settings.json` 跑 `bash install.sh` 幂等：第一次添加 6 条 hook，第二次输出「6 条已存在」。

<!-- /role -->

<!-- role:builder -->

## 约束 & 边界

**不能碰**：

- `install.sh` 现有的备份机制（L72-74 cp .bak）/ 写前自检 / 写后自检 / 原子替换（5.3-5.6）—— 这些是 V2.4 加固的，本期不动。
- `uninstall.sh` 现有的备份机制（L40 cp .bak）/ 删 hook 块的整体结构。
- registrations 表里 6 个 hook 的 type / cmd_name / matcher 现值（本期只改"怎么比对"逻辑，不改 hook 注册项本身）。
- `set -euo pipefail` 风格。

**必须兼容**：

- jq 缺失时 `JQ_AVAILABLE=false` 跳过 hook 注册的早返路径 —— 本期改写后软链仍要正常创建。
- 用户在 settings.json 里手动加的非 builder-loop 条目 —— install/uninstall 都不能动它们。
- 老的 settings.json 已有 5 个旧 hook 条目（reviewer-timing-check 还没注册的状态）—— 跑新版 install.sh 应该补上第 6 条。

## 技术选型

### has_entry 升级策略

| 方案 | 行为 | 选/弃 |
|------|------|------|
| **B：发现 matcher 不一样就删旧加新** | has_entry 返回三态（找不到 / 完全一致 / 脚本名同但 matcher 异）；不一致时主循环先 remove 旧条目再 append 新条目 | ✅ 选定 — settings.json 最终只留一份对的 |
| A：matcher 不同则再 append 一条 | 旧条目保留，再加一条新 matcher 的 | ❌ 弃 — 同脚本会被 CC 跳两次 |
| C：每次 install 先清空所有 builder-loop 条目再批量 append | 最简单 | ❌ 弃 — 抹掉用户在条目上手加的修饰（disabled 字段等） |

### matcher 比对的等价性

| 方案 | 行为 | 选/弃 |
|------|------|------|
| **字面相等** | `Read\|Grep\|Glob` 与 `Glob\|Read\|Grep` 视为不同 → 触发更新 | ✅ 选定 — 本仓 install.sh 是 matcher 的单一源头，顺序固定不会乱 |
| split('\|') 后比集合 | 顺序无关 | ❌ 弃 — 增加代码复杂度，本仓没有顺序乱的风险 |

### fixture 拆分粒度

拆两个独立 e2e 脚本 ——

- `test-install-uninstall-roundtrip.sh` 专测 #17（装 → 卸后 settings.json 应跟装前 byte-equal）
- `test-install-matcher-update.sh` 专测 #16（先装 matcher=A，改 install.sh 用 matcher=B，再装，断言 settings.json 该条目只剩 matcher=B）

失败时定位精准，跟本仓现有 fixture 颗粒度一致。

## 方案设计

### install.sh has_entry 改写

把 `has_entry(arr, cmd_name)` 改成 `find_entry_status(arr, cmd_name, matcher)`，返回 `("missing" | "match" | "stale", index)`：

- 遍历 arr 找 command 含 cmd_name 的 item。
- 找到后比 item.get("matcher") 与传入 matcher 是否字面相等：相等返回 `("match", i)`，不相等返回 `("stale", i)`。
- 没找到返回 `("missing", -1)`。

主循环（registrations 遍历那段）按返回值分流：

- `missing`：append 新条目，added += 1。
- `match`：跳过，已存在。
- `stale`：`arr.pop(index)` + append 新条目，updated += 1。

最后输出 `f"✓ hooks: {added} 条新增，{updated} 条更新，{len(registrations) - added - updated} 条已存在"`。

### uninstall.sh bl_scripts 补一项

L49-51 的列表加 `"reviewer-timing-check.sh"`。

### fixture 1：test-install-uninstall-roundtrip.sh

```
1. mktemp -d 临时 HOME
2. HOME=<tmp> 跑一份合法的 settings.json（含若干非 builder-loop 条目）作为 baseline
3. 复制 baseline 到 <tmp>/.claude/settings.json
4. HOME=<tmp> bash <repo>/install.sh
5. 断言 settings.json 已含 6 条 builder-loop hook
6. HOME=<tmp> bash <repo>/uninstall.sh
7. 断言 settings.json 与 baseline byte-equal（diff 为空）
8. 清理 <tmp>
```

### fixture 2：test-install-matcher-update.sh

```
1. mktemp -d 临时 HOME + 临时仓 <repo-clone>
2. cp 本仓代码到 <repo-clone>
3. HOME=<tmp> bash <repo-clone>/install.sh
4. 抓取 settings.json 里 tester-lock-check.sh 那条的 matcher（应是 "Read|Grep|Glob"）
5. sed 改 <repo-clone>/install.sh 让该条 matcher 变 "Read|Grep|Glob|WebFetch"
6. HOME=<tmp> bash <repo-clone>/install.sh
7. 断言 settings.json 里 tester-lock-check.sh 那条 matcher = "Read|Grep|Glob|WebFetch"
8. 断言 PreToolUse 数组里只有一条 tester-lock-check.sh（不是 stale + new 两条）
9. 清理
```

## 风险 & 应对

| 风险 | 应对 |
|------|------|
| has_entry 改返回值后 install.sh 主循环没适配，跑下去报错 | 改完 grep 全文确认 has_entry 调用点全部更新；fixture 1 跑通即覆盖 |
| fixture 用 HOME 环境变量伪装 ~/.claude/，没 override 干净污染真实 settings.json | fixture 顶部 `export HOME=<tmp>` + `unset CLAUDE_DIR`；脚本结束 trap 清 tmp；fixture 名字带 `roundtrip` 避免误以为是真装 |
| install.sh 里 jq 自检（其实是 python3 自检，注释跟代码都说 jq 但实际用 python3）跑出错被 fixture 误判 | fixture 跑前先 `command -v python3` 拒绝跑，把环境前置条件写在 fixture 头部 |
| V3.0 后 registrations 列表会改（拆 merge-worktree-back.sh 后 hook 总数增加），fixture 写死 6 条会过时 | fixture 1 不写死 6，改成「装前条目数 + builder-loop 注册数 = 装后条目数」；fixture 2 写死 tester-lock-check.sh 这一条（V3.0 不改这条） |

**退路**：install.sh 改炸的话，`~/.claude/settings.json.bak.<ts>` 时间戳备份在（5.2 节已有），手动 cp 还原即可。

## 文件地图

存量改动：

- `install.sh` L96-101 `has_entry` 函数 → 改写为 `find_entry_status` 返回三态
- `install.sh` L112-117 主循环 → 适配新返回值（added / updated / 已存在 三分支）
- `install.sh` L123 print 输出 → 加 updated 计数字段
- `uninstall.sh` L49-51 bl_scripts 列表 → 加 `"reviewer-timing-check.sh"` 一行

新增：

- `skills/builder-loop/fixtures/e2e/test-install-uninstall-roundtrip.sh`
- `skills/builder-loop/fixtures/e2e/test-install-matcher-update.sh`

不动：

- `install.sh` 5.1-5.6 备份/写前自检/写后自检/原子替换/终检 一整段
- `install.sh` registrations 表（L103-110）的 6 条 hook 内容
- `uninstall.sh` 软链删除 + python3 改 settings.json 的整体结构

## 执行任务列表

按顺序：

1. **改 install.sh `has_entry`** —— 改名 `find_entry_status`，返回 (status, index)；status ∈ {"missing", "match", "stale"}。
2. **改 install.sh 主循环** —— 按 status 分流：missing → append + added++；stale → arr.pop(idx) + append + updated++；match → 跳过。print 末尾输出三个计数。
3. **改 uninstall.sh `bl_scripts`** —— L49-51 列表加 `"reviewer-timing-check.sh"`。
4. **新建 `test-install-uninstall-roundtrip.sh`** —— mktemp HOME → install → uninstall → diff 断言 byte-equal。
5. **新建 `test-install-matcher-update.sh`** —— mktemp HOME → install → sed 改 install.sh 的某条 matcher → 再 install → 断言 settings.json 只剩新 matcher 一条。
6. **跑两个 fixture + 跑现有相邻 fixture（`test-empty-repo.sh` 等）** 验证无回归。
7. **跑 `bash install.sh` 在真实 ~/.claude/** —— 第一次应该 0 条更新（因为已是最新）；故意 sed 改 matcher 重跑应识别 1 条更新。

<!-- /role -->

## 测试计划

**测试目标**：验证 install/uninstall 满足两条不变量（改 matcher 重跑能跟上 / 装→卸能完全还原）。

**关键测试场景**（落到两个 fixture）：

1. **装→卸还原**：fixture 1。预置 baseline settings.json（含 3 条非 builder-loop 条目）→ install → 断言加了 6 条 → uninstall → 断言 settings.json 与 baseline byte-equal。
2. **matcher 升级识别**：fixture 2。空 settings.json → install matcher=A → 改 install.sh matcher=B → 再 install → 断言只剩 1 条 matcher=B（不残留 stale）。
3. **幂等回归**：fixture 1 内追加一步 —— install 跑两遍，第二次输出应是「0 新增 0 更新 6 已存在」。
4. **jq/python3 缺失早返**（不入 fixture，留作手动验证）：临时把 python3 重命名跑 install.sh，应跳过 hook 注册不报错，软链仍创建。

**测试深度**：快速。两个 fixture 均为黑盒 e2e（mktemp HOME 跑真 install.sh / uninstall.sh，断言 settings.json 内容），不写单元测试（has_entry 的三态返回逻辑由 fixture 间接覆盖足够）。
