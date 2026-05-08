# 仲裁分支流程

> 收到 Stop hook 仲裁请求（`[builder-loop] ⚠️ PASS_CMD 通过，但 worktree rebase 主干时发生冲突`）时执行。

> ⚠️ **V3.0 已知缺口**：当前 arbiter 续路径调 `merge-worktree-back.sh`（V2.x 立即合语义），冲突解决后 commit **直接 ff 进主线，跳过 reviewer-as-gate**。即 V3.0 worktree 模式下 arbiter 路径不走 phase=passed_pending_review，reviewer 看不到合并后的代码。本期 V3.0 保留这条 V2.x 路径是为了不破坏 `test-conflict.sh` 等 fixture，属于落地的已知缺口。详见 `.claude/improvements.md`「arbiter 续路径迁移到 reviewer-as-gate」候选条目（V3.x 修）。

Stop hook 已预填所有参数并给出后处理脚本路径，按其指示：

1. **spawn arbiter subagent**（`run_in_background: false`，同步等待），用 hook 给出的参数：
   ```
   subagent_type: "arbiter"
   prompt:
     worktree_path: <hook 给出的 worktree_path>
     main_branch: <hook 给出的 main_branch>
     conflict_files: <hook 给出的 conflict_files>
     task_context: <hook 给出的 task_context>
     their_commits: <hook 给出的 their_commits（对方 builder 合入主干的 commit 摘要）>
   ```
2. **保存 arbiter 输出**到 `/tmp/arbiter-output.txt`（用 Write 工具写入 arbiter 返回的完整文本）
3. **调后处理脚本**：
   ```bash
   bash <hook 给出的 apply_script> <hook 给出的 state_file> /tmp/arbiter-output.txt
   ```
4. **根据退出码决策**：
   - `APPLIED`（exit 0）→ 清理 state 并继续走 Reviewer 流程
   - `LOW_CONFIDENCE`（exit 1）→ 用 AskUserQuestion 把冲突概览 + arbiter 理由呈给用户决策
   - `APPLY_FAILED`（exit 2）→ 重试（不超过 hook 给出的 max_attempts），仍失败则交用户
   - `MERGE_FAILED`（exit 3）→ 同 APPLY_FAILED 处理
