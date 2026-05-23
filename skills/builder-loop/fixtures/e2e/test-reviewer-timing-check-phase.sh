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

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "reviewer-timing-check-phase"

HOOK_SCRIPT="${HARNESS_REPO_ROOT}/scripts/reviewer-timing-check.sh"
assert "reviewer-timing-check.sh 存在" "[ -f '$HOOK_SCRIPT' ]"

# 跑 hook：构造一个临时项目环境，在 worktree 子目录下喂 stdin 跑
# 参数：$1=base_dir $2=slug $3=state_body（多行字符串，整段写到 state/<slug>.yml）
#       $4=subagent_type（默认 reviewer）
# 输出：handle 路径（与 harness run_hook 格式兼容）
run_timing_hook() {
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

  local handle
  handle="$(mktemp -d -t harness-result-XXXXXX)"
  _HARNESS_TMPDIRS+=("$handle")

  local stdin_json
  stdin_json="$(printf '{"tool_input": {"subagent_type": "%s"}}' "$subagent")"
  local ec=0
  ( cd "$base/.claude/worktrees/$slug" && printf '%s' "$stdin_json" | bash "$HOOK_SCRIPT" >"$handle/stdout" 2>"$handle/stderr" ) || ec=$?
  echo "$ec" > "$handle/ec"
  echo "$handle"
}

TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")

# ============================================================
# Case A: phase=active + active=true → exit 2（拦）
# ============================================================
section "Case A: phase=active + active=true → exit 2（拦）"
SLUG_A="rtc-case-a"
BODY_A=$(cat <<'EOF'
active: true
phase: "active"
slug: "rtc-case-a"
worktree_path: ""
EOF
)
RES_A="$(run_timing_hook "$TMP" "$SLUG_A" "$BODY_A")"
assert_ec "Case A exit code = 2" "$RES_A" 2
assert_stderr_contains "Case A stderr 含 blocked reviewer spawn" "$RES_A" 'blocked reviewer spawn'
assert_stderr_contains "Case A stderr 含 phase=active" "$RES_A" 'phase=active'

# ============================================================
# Case B: phase=passed_pending_review + active=true → exit 0（放行）
# ============================================================
section "Case B: phase=passed_pending_review + active=true → exit 0（放行，V3.0 主链路）"
SLUG_B="rtc-case-b"
BODY_B=$(cat <<'EOF'
active: true
phase: "passed_pending_review"
slug: "rtc-case-b"
worktree_path: ""
EOF
)
RES_B="$(run_timing_hook "$TMP" "$SLUG_B" "$BODY_B")"
assert_ec "Case B exit code = 0（放行 reviewer spawn）" "$RES_B" 0
assert "Case B stderr 不含 blocked" "! grep -q 'blocked' '$RES_B/stderr'"

# ============================================================
# Case C: 缺 phase + active=true → exit 2（V2.x 老 state 兼容）
# ============================================================
section "Case C: 缺 phase + active=true → exit 2（V2.x 兼容路径）"
SLUG_C="rtc-case-c"
BODY_C=$(cat <<'EOF'
active: true
slug: "rtc-case-c"
worktree_path: ""
EOF
)
RES_C="$(run_timing_hook "$TMP" "$SLUG_C" "$BODY_C")"
assert_ec "Case C exit code = 2" "$RES_C" 2
assert_stderr_contains "Case C stderr 含 V2.x 老 state warning" "$RES_C" 'V2.x 老 state'
assert_stderr_contains "Case C stderr 含 fallback active 判定" "$RES_C" 'fallback active=true'
assert_stderr_contains "Case C stderr 含 blocked reviewer spawn" "$RES_C" 'blocked reviewer spawn'

# ============================================================
# Case D: 缺 phase + active=false → exit 0（V2.x 不活跃放行）
# ============================================================
section "Case D: 缺 phase + active=false → exit 0（V2.x 不活跃放行）"
SLUG_D="rtc-case-d"
BODY_D=$(cat <<'EOF'
active: false
slug: "rtc-case-d"
worktree_path: ""
EOF
)
RES_D="$(run_timing_hook "$TMP" "$SLUG_D" "$BODY_D")"
assert_ec "Case D exit code = 0（放行）" "$RES_D" 0
assert_stderr_contains "Case D stderr 含 V2.x 老 state warning" "$RES_D" 'V2.x 老 state'
assert "Case D stderr 不含 blocked（不该报拦截日志）" "! grep -q 'blocked reviewer spawn' '$RES_D/stderr'"

# ============================================================
# Case E: subagent_type=tester（非 reviewer）→ exit 0（早退，不看 phase）
# ============================================================
section "Case E: subagent_type=tester → exit 0（不拦非 reviewer subagent）"
SLUG_E="rtc-case-e"
BODY_E=$(cat <<'EOF'
active: true
phase: "active"
slug: "rtc-case-e"
worktree_path: ""
EOF
)
RES_E="$(run_timing_hook "$TMP" "$SLUG_E" "$BODY_E" "tester")"
assert_ec "Case E exit code = 0（tester 早退放行）" "$RES_E" 0
assert "Case E stderr 不含 blocked" "! grep -q 'blocked' '$RES_E/stderr'"

harness_report
