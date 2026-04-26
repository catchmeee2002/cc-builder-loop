# doc-maintainer audit checklist（cc-builder-loop 专用）

> Builder 在 spawn doc-maintainer 时**必须把本文件路径附进 prompt**，让 maintainer 先 Read 一次再做评估。
> 目的：补 doc-maintainer 引导式 prompt（"我已更新 X 请 audit Y"）容易漏掉的清单项，强制 maintainer 自行 audit 全集而非只 audit Builder 提到的范围。
> 历史教训：V1.5–V1.8 累计漏更 6 个 e2e fixture 表格条目，V1.9 又漏 3 个；都源自 Builder 报"我已更新 SKILL.md 5.3 e2e 表格 N 行"，maintainer 只验证那 N 行而没回头审视全表是否完整。

## 触发条件

Builder 走步骤 3.5 文档评估时，命中下述任一项即 spawn doc-maintainer 并附本文件路径：
- 新增 / 修改 / 删除 fixture（`skills/builder-loop/fixtures/e2e/test-*.sh`）
- 新增 / 修改对外脚本（`skills/builder-loop/scripts/*.sh`、`scripts/*.sh`）
- state schema 字段变更（增 / 改 / 删字段名）
- hook 注册条目变化
- 输出格式变化（commit message / state file 注释 / stderr 文案）
- 跨文件依赖关系变化（CLAUDE.md / README.md / SKILL.md 的导航锚点失效）

## maintainer 必跑的 audit 步骤（黑盒式，不依赖 Builder 提示）

### 1. e2e fixture 全集 vs README/SKILL 表格交叉对账

```bash
# 列出当前所有 fixture
ls skills/builder-loop/fixtures/e2e/test-*.sh | sort

# 列出 README/SKILL 表格里出现的 fixture（搜 test-*.sh 文件名）
grep -ohE 'test-[a-z0-9-]+\.sh' skills/builder-loop/README.md skills/builder-loop/SKILL.md | sort -u
```

两份输出必须完全一致。差集 → 表格漏写或写错；如果文件被删而表格还在 → 表格条目须删除。

### 2. CLAUDE.md "已交付能力" 章节版本号 vs git tag / commit 日志

- 当前最新 commit 引入的版本号是否在 CLAUDE.md 中有对应的"V*x*：xxx"小节
- 小节是否含：能力描述、关键 commit hash、已知风险、降级方法
- 如版本未在 CLAUDE.md 出现 → 必加

### 3. SKILL.md 状态字段 schema vs setup-builder-loop.sh 实际写入字段

state 文件实际字段定义在 setup-builder-loop.sh 的 `cat > "$STATE_FILE" <<EOF ... EOF` heredoc 段内：

```bash
# 提取 setup 写 state 的 heredoc 段中的字段名
sed -n '/^cat > "\$STATE_FILE" <<EOF/,/^EOF$/{ /^[a-z_]\+:/p }' skills/builder-loop/scripts/setup-builder-loop.sh

# vs SKILL.md "状态文件 schema" 块内文档化的字段（YAML 形式）
sed -n '/^## 状态文件 schema/,/^##[^#]/{ /^[a-z_]\+:/p }' skills/builder-loop/SKILL.md
```

两份输出取字段名集合对账（去注释、去引号），任一字段在代码中存在但文档缺失（或反之）→ 必须同步。

### 4. 链接映射表 vs install.sh 实际链接

CLAUDE.md 的"链接映射表"声明哪些文件被 `ln -sf` 软链到 `~/.claude/`。逐项对照 `install.sh` 实际链接动作。新增 / 删除 / 重命名 → 同步表格。

### 5. hook 注册表 vs settings.json 实际注册条目

CLAUDE.md "注册的 N 个 hook" 表格 vs `install.sh` 的 hook 合并段。matcher / 脚本 / 类型必须一一对应。

### 6. 已知问题排查手册的 fix 状态

CLAUDE.md "7.x 已知问题"章节中标注"V*x*已修复"的条目，必须能在该版本号的代码中找到对应防御逻辑（例如 V1.8.3 修过的"PASS 分支预读 start_head" 在 stop hook 当前代码里能 grep 到）。被回滚 / 重写未保留防御 → maintainer 必须更新条目状态。

## 必须输出的报告字段

doc-maintainer 完成 audit 后输出（不允许跳过任何字段）：

```markdown
## 文档变更类型分类（V2.0+ 必填）

- [ ] 新增能力（CLAUDE.md 已交付能力章节）
- [ ] 新增 fixture（README/SKILL fixture 表格）
- [ ] schema 变更（SKILL.md 状态字段）
- [ ] 行为变更（输出格式 / commit message / stderr 文案）
- [ ] 排查手册新增（CLAUDE.md 已知问题）
- [ ] 链接映射 / hook 注册更新

## audit 步骤覆盖（必须每条勾选）

- [ ] 1. e2e fixture 全集对账（diff 列出）
- [ ] 2. CLAUDE.md 版本号 vs commit 日志
- [ ] 3. SKILL.md schema vs 实际字段
- [ ] 4. 链接映射表 vs install.sh
- [ ] 5. hook 注册表 vs settings.json 合并段
- [ ] 6. 已知问题 fix 状态（最近 3 个版本）

## 历史欠账反查

> 浏览 git log --oneline -50 中 type 为 feat / fix 但 commit message 未含 docs 痕迹的提交。
> 抽样 3 个，验证对应文档已落地。如发现欠账，列在此处由 Builder 决定是否本次补齐。
```

## 错误模式（maintainer 应避免）

- ❌ 只看 Builder 在 prompt 里说的"我已更新这些"就停止，不做全集 audit
- ❌ 报告说"无需更新"但跳过了 audit 6 步中的某步
- ❌ 报告只列勾选项，不列具体的 diff（例如该改 README 哪一行）
- ✅ 即使 Builder 在 prompt 中说"应该不影响文档"，也要跑 audit 6 步，跑完再得结论
