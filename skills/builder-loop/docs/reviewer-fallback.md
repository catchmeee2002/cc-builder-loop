# Reviewer 兜底自审（2 次全败时执行）

用 Grep/Read 工具遍历 changed_files 执行轻量检查：

1. **语法检查**：Grep 搜索明显语法问题（未闭合括号、缩进错误）
2. **import 验证**：Grep 搜索 import 语句，检查是否有明显不存在的模块
3. **调用一致性**：Grep 函数定义与调用处参数数量是否匹配
4. **None/空值风险**：Grep `= None`、`if x:` 等模式

输出格式：

```
⚠️ Reviewer 2 次均失败，已执行轻量自审：
✅ 语法检查: 通过
✅ import 验证: 通过
...
共 X 项通过，Y 项需关注
📋 详细 reviewer 报告待 API 恢复后补充
```

兜底自审**不阻断工作流**，完成后正常继续 commit 流程。
