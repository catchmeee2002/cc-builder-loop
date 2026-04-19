#!/usr/bin/env bash
# split-plan-by-role.sh — 方案文件按角色视图过滤
#
# 用法：bash split-plan-by-role.sh <plan_file> <role>
#   role ∈ {shared, builder, tester, all}
#
# 行为：
#   - shared / builder / tester：输出对应 role 区块 + 所有 shared 区块
#   - all：原样输出
#
# 区块格式（在方案文件中）：
#   <!-- role:builder -->
#   ... 仅 builder 可读的内容 ...
#   <!-- /role -->
#
#   未被任何 role 标签包围的内容，默认归入 shared 视图。
#
# 退出码：0=成功 / 1=参数错 / 2=文件不存在

set -euo pipefail

PLAN_FILE="${1:?plan file required}"
ROLE="${2:?role required (shared|builder|tester|all)}"

if [ ! -f "$PLAN_FILE" ]; then
  echo "[split-plan-by-role] 方案文件不存在：$PLAN_FILE" >&2
  exit 2
fi

case "$ROLE" in
  shared|builder|tester|all) : ;;
  *) echo "[split-plan-by-role] 无效 role: $ROLE" >&2; exit 1 ;;
esac

if [ "$ROLE" = "all" ]; then
  cat "$PLAN_FILE"
  exit 0
fi

# 用 awk 处理：
#   - 状态机识别 <!-- role:xxx --> ... <!-- /role -->
#   - 区块外的内容 → 始终输出（视为 shared）
#   - 区块内的内容 → 仅当 role 匹配 或 标签是 shared 时输出
awk -v target="$ROLE" '
  BEGIN { inside = 0; current = "" }
  /<!-- role:[a-zA-Z]+ -->/ {
    match($0, /<!-- role:([a-zA-Z]+) -->/, arr)
    current = arr[1]; inside = 1; next
  }
  /<!-- \/role -->/ {
    inside = 0; current = ""; next
  }
  {
    if (inside == 0) { print; next }
    if (current == "shared") { print; next }
    if (current == target)   { print; next }
    # 其他 role 区块跳过
  }
' "$PLAN_FILE"
