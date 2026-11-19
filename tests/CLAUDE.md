# tests

pytest 套件，按 `runtime/builder_loop/` 模块对应组织（`test_contract.py` ↔ `contract.py`，以此类推）。运行命令见根 CLAUDE.md Commands；`conftest.py` 提供 `repo`（临时 git 仓）/ `cli` / `hook` 三个基础 fixture，`started` 组合三者起 run，新用例优先复用它们，不另起平行的仓库搭建代码。

## 写法纪律

- fixture 里的 git 操作一律走 conftest 的 `git()`（自带 `core.hooksPath=/dev/null`）或合规 commit message；绕开它直接 `git commit` 会撞本机 git init 模板的 commit-msg 门禁。
- pytest 把 `test*` 开头的模块级函数都当用例收集：测试辅助函数别叫 `tester_xxx`。
- readiness 靠事件时间戳的字符串比较判先后（微秒精度、UTC）：构造测试数据别手写秒级时间戳。
