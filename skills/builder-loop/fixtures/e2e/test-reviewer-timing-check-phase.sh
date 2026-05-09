#!/usr/bin/env bash
# test-reviewer-timing-check-phase.sh — V3.0 P0 缺口修复验证
#
# 验证 reviewer-timing-check.sh 按 phase 字段决策（V3.0），且兼容 V2.x 老 state（无 phase 字段）。
#
# 场景：
#   Case A: phase=active + active=true → exit 2（拦），stderr 含 blocked
#   Case B: phase=passed_pending_review + active=true → exit 0（放行，V3.0 PASS 后 reviewer-as-gate 必经）
#   Case C: 缺 phase 字段 + active=true → exit 2（V2.x 老 state 兼容拦），stderr 含「V2.x 老 state」warning
#   Case D: 缺 phase 字段 + active=false → exit 0（V2.x 老 state 不活跃放行）
#   Case E: 非 reviewer 的 subagent_type（例：tester）→ exit 0（无论 phase 都放行）

set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/reviewer-timing-check.sh"

PASS=0
FAIL=0
assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ))
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

# 跑 hook：构造一个临时项目环境，在 worktree 子目录下喂 stdin 跑
# 参数：$1=base_dir $2=slug $3=state_body（多行字符串，整段写到 state/<slug>.yml）
#       $4=subagent_type（默认 reviewer）
# 输出：通过 env var 回写 EC / ERR_FILE
run_case() {
  local base="$1" slug="$2" state_body="$3" subagent="${4:-reviewer}"
  mkdir -p "$base/.claude/builder-loop/state"
  mkdir -p "$base/.claude/worktrees/$slug"
  # 锚 PROJECT_ROOT
  cat > "$base/.claude/loop.yml" <<'Y'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
worktree:
  enabled: true
Y
  printf '%s\n' "$state_body" > "$base/.claude/builder-loop/state/$slug.yml"

  local err_file
  err_file="$(mktemp)"
  local stdin_json
  stdin_json="$(printf '{"tool_input": {"subagent_type": "%s"}}' "$subagent")"
  local ec=0
  ( cd "$base/.claude/worktrees/$slug" && printf '%s' "$stdin_json" | bash "$HOOK_SCRIPT" 2>"$err_file" >/dev/null ) || ec=$?
  echo "$ec|$err_file"
}

echo "=== reviewer-timing-check.sh phase 字段决策 fixture ==="

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP:-}"' EXIT

# ============================================================
# Case A: phase=active + active=true → exit 2（拦）
# ============================================================
echo ""
echo "=== Case A: phase=active + active=true → exit 2（拦）==="
SLUG_A="rtc-case-a"
BODY_A=$(cat <<'EOF'
active: true
phase: "active"
slug: "rtc-case-a"
worktree_path: ""
EOF
)
RES_A="$(run_case "$TMP" "$SLUG_A" "$BODY_A")"
EC_A="${RES_A%%|*}"
ERR_A="${RES_A#*|}"
assert "Case A exit code = 2" "[ '$EC_A' -eq 2 ]"
assert "Case A stderr 含 blocked reviewer spawn" "grep -q 'blocked reviewer spawn' '$ERR_A'"
assert "Case A stderr 含 phase=active" "grep -q 'phase=active' '$ERR_A'"

# ============================================================
# Case B: phase=passed_pending_review + active=true → exit 0（放行）
# 这是 V3.0 reviewer-as-gate 主链路：PASS 后 builder spawn reviewer 必须放行
# ============================================================
echo ""
echo "=== Case B: phase=passed_pending_review + active=true → exit 0（放行，V3.0 主链路）==="
SLUG_B="rtc-case-b"
BODY_B=$(cat <<'EOF'
active: true
phase: "passed_pending_review"
slug: "rtc-case-b"
worktree_path: ""
EOF
)
RES_B="$(run_case "$TMP" "$SLUG_B" "$BODY_B")"
EC_B="${RES_B%%|*}"
ERR_B="${RES_B#*|}"
assert "Case B exit code = 0（放行 reviewer spawn）" "[ '$EC_B' -eq 0 ]"
assert "Case B stderr 不含 blocked（不该报拦截日志）" "! grep -q 'blocked' '$ERR_B'"

# ============================================================
# Case C: 缺 phase + active=true → exit 2（V2.x 老 state 兼容）
# ============================================================
echo ""
echo "=== Case C: 缺 phase + active=true → exit 2（V2.x 兼容路径）==="
SLUG_C="rtc-case-c"
BODY_C=$(cat <<'EOF'
active: true
slug: "rtc-case-c"
worktree_path: ""
EOF
)
RES_C="$(run_case "$TMP" "$SLUG_C" "$BODY_C")"
EC_C="${RES_C%%|*}"
ERR_C="${RES_C#*|}"
assert "Case C exit code = 2" "[ '$EC_C' -eq 2 ]"
assert "Case C stderr 含 V2.x 老 state warning" "grep -q 'V2.x 老 state' '$ERR_C'"
assert "Case C stderr 含 fallback active 判定" "grep -q 'fallback active=true' '$ERR_C'"
assert "Case C stderr 含 blocked reviewer spawn" "grep -q 'blocked reviewer spawn' '$ERR_C'"

# ============================================================
# Case D: 缺 phase + active=false → exit 0（V2.x 不活跃放行）
# ============================================================
echo ""
echo "=== Case D: 缺 phase + active=false → exit 0（V2.x 不活跃放行）==="
SLUG_D="rtc-case-d"
BODY_D=$(cat <<'EOF'
active: false
slug: "rtc-case-d"
worktree_path: ""
EOF
)
RES_D="$(run_case "$TMP" "$SLUG_D" "$BODY_D")"
EC_D="${RES_D%%|*}"
ERR_D="${RES_D#*|}"
assert "Case D exit code = 0（放行）" "[ '$EC_D' -eq 0 ]"
assert "Case D stderr 含 V2.x 老 state warning（仍记 fallback 路径）" "grep -q 'V2.x 老 state' '$ERR_D'"
assert "Case D stderr 不含 blocked（不该报拦截日志）" "! grep -q 'blocked reviewer spawn' '$ERR_D'"

# ============================================================
# Case E: subagent_type=tester（非 reviewer）→ exit 0（早退，不看 phase）
# ============================================================
echo ""
echo "=== Case E: subagent_type=tester → exit 0（不拦非 reviewer subagent）==="
SLUG_E="rtc-case-e"
BODY_E=$(cat <<'EOF'
active: true
phase: "active"
slug: "rtc-case-e"
worktree_path: ""
EOF
)
RES_E="$(run_case "$TMP" "$SLUG_E" "$BODY_E" "tester")"
EC_E="${RES_E%%|*}"
ERR_E="${RES_E#*|}"
assert "Case E exit code = 0（tester 早退放行）" "[ '$EC_E' -eq 0 ]"
assert "Case E stderr 不含 blocked（不该报拦截日志）" "! grep -q 'blocked' '$ERR_E'"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
