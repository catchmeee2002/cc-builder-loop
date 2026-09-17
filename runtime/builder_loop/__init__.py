"""builder-loop V8 runtime — 独立判据 + Git 事务层。

编排（subagent 调度、续接、提问用户）交给 Claude Code 原生能力；本包只负责：
contract 冻结与 digest、candidate worktree、machine/tester/proof/reviewer 四类 evidence
的绑定与失效判定、finalize CAS 写回。ledger 的唯一写入者是本包的 CLI。
"""

__version__ = "8.2.0"
