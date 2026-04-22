#!/usr/bin/env bash
# loop-init.sh — 一键初始化 builder-loop 配置
#
# 用法：bash loop-init.sh <project_root>
#
# 整合流程：
#   1. 检测 git 仓库，无则自动 git init + initial commit
#   2. 调 probe-project-stack.sh 探测项目栈
#   3. 用探测结果构造 choice JSON
#   4. 调 init-loop-config.sh 生成 loop.yml
#   5. 调 run-pass-cmd.sh 做 smoke test
#   6. 输出汇报
#
# 退出码：0=成功 / 1=失败

set -euo pipefail

PROJECT_ROOT="${1:?project_root required}"
cd "$PROJECT_ROOT"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- 1. 确保 git 仓库 ----
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[loop-init] 检测到非 git 仓库，自动初始化..." >&2
  git init >&2
  git add -A >&2
  git commit -m "chore(init): [cr_id_skip] Initialize project" --allow-empty >&2
  echo "[loop-init] git 仓库已初始化" >&2
fi

# ---- 2. 探测项目栈 ----
echo "[loop-init] 探测项目栈..." >&2
PROBE_JSON="$(bash "$SCRIPT_DIR/probe-project-stack.sh" "$PROJECT_ROOT")"
echo "[loop-init] 探测结果：$PROBE_JSON" >&2

# ---- 3. 构造 choice JSON ----
# 从探测结果提取 recommended_pass_cmd / source_dirs / test_dirs
CHOICE_JSON="$(echo "$PROBE_JSON" | python3 -c "
import sys, json
probe = json.load(sys.stdin)
choice = {
    'pass_cmd': probe.get('recommended_pass_cmd', [{'stage': 'test', 'cmd': 'echo no-test-configured', 'timeout': 60}]),
    'max_iterations': 5,
    'layout': {
        'source_dirs': probe.get('source_dirs', []),
        'test_dirs': probe.get('test_dirs', []),
    },
    'worktree': {'enabled': False},
}
print(json.dumps(choice))
")"

echo "[loop-init] 配置 JSON：$CHOICE_JSON" >&2

# ---- 4. 写 loop.yml ----
echo "$CHOICE_JSON" | bash "$SCRIPT_DIR/init-loop-config.sh" "$PROJECT_ROOT"
INIT_RC=$?
if [ "$INIT_RC" -ne 0 ]; then
  echo "[loop-init] ❌ init-loop-config.sh 失败 (exit $INIT_RC)" >&2
  exit 1
fi

# ---- 5. Smoke test ----
echo "[loop-init] 运行 smoke test..." >&2
SMOKE="$(bash "$SCRIPT_DIR/run-pass-cmd.sh" "$PROJECT_ROOT" 0 || true)"
SMOKE_LAST="$(echo "$SMOKE" | tail -1)"

# ---- 6. 汇报 ----
PASS_CNT="$(python3 -c "
import yaml
cfg = yaml.safe_load(open('$PROJECT_ROOT/.claude/loop.yml'))
print(len(cfg.get('pass_cmd', [])))
" 2>/dev/null || echo '?')"

LANG="$(echo "$PROBE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('language','unknown'))")"

echo ""
echo "✅ builder-loop 已初始化"
echo "   项目语言：$LANG"
echo "   配置文件：$PROJECT_ROOT/.claude/loop.yml"
echo "   PASS_CMD 阶段数：$PASS_CNT"
echo "   Smoke test：$SMOKE_LAST"
echo ""
echo "下一步：执行 /builder 开始开发，loop 会自动接管测试循环。"
