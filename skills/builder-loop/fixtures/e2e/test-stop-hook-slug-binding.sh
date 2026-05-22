#!/usr/bin/env bash
# test-stop-hook-slug-binding.sh — V3.2 stop hook 读 local.md slug 精确绑定
#
# 验证：
#   A1: local.md 有 slug + state 存在且 active → hook 正常操作（EC=2）
#   A2: local.md 不存在 → hook exit 0 放行
#   A3: local.md slug 指向不存在的 state → hook exit 0 放行
#   D1: 有 loop.yml + dirty + 无 local.md + 无 state → hook exit 0（不自动启动 loop）

set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$THIS_DIR/../../../.." && pwd)"
HOOK_SCRIPT="${REPO_ROOT}/scripts/builder-loop-stop.sh"

PASS=0
FAIL=0
assert() {
  local desc="$1" cond="$2"
  if eval "$cond"; then echo "  ✅ $desc"; PASS=$(( PASS + 1 ))
  else echo "  ❌ $desc (cond: $cond)"; FAIL=$(( FAIL + 1 )); fi
}

run_hook() {
  local cwd="$1" err_file="$2" ec=0
  printf '{"cwd": "%s"}' "$cwd" | bash "$HOOK_SCRIPT" 2>"$err_file" >/dev/null || ec=$?
  echo "$ec"
}

setup_repo() {
  local dir="$1"
  cd "$dir"
  git init -q
  git config user.email "e2e@test.local"
  git config user.name "e2e-test"
  mkdir -p .claude
  cat > .claude/loop.yml <<'Y'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
worktree:
  enabled: false
Y
  echo "seed" > README.md
  cat > .gitignore <<'G'
.claude/builder-loop/
.claude/loop-runs/
G
  git add -A
  git commit -q -m "chore(test): [cr_id_skip] Slug binding fixture seed"
}

create_state() {
  local dir="$1" slug="$2"
  mkdir -p "$dir/.claude/builder-loop/state"
  local head1
  head1="$(git -C "$dir" rev-parse --short HEAD)"
  cat > "$dir/.claude/builder-loop/state/${slug}.yml" <<EOF
active: true
phase: "active"
slug: "${slug}"
owner_cwd: "$dir"
iter: 0
max_iter: 5
project_root: "$dir"
main_repo_path: "$dir"
start_head: "${head1}"
last_iter_head: "${head1}"
worktree_path: ""
worktree_mode: "bare"
plan_file: ""
task_description: |
  slug-binding-test
source_dirs: ""
test_dirs: ""
last_pass_stage: ""
last_error_hash: ""
last_error_count: 0
stopped_reason: ""
cleanup_phase: ""
created_at: "2026-05-23T00:00:00+08:00"
EOF
}

create_local_md() {
  local dir="$1" slug="$2"
  mkdir -p "$dir/.claude"
  cat > "$dir/.claude/builder-loop.local.md" <<EOF
slug: "${slug}"
worktree_path: ""
state_file: "$dir/.claude/builder-loop/state/${slug}.yml"
EOF
}

echo "=== V3.2 stop hook slug 绑定 fixture ==="

# ============================================================
# A1: local.md + active state → hook 正常操作
# ============================================================
echo ""
echo "=== A1: local.md + active state → EC=2 ==="
TMPA1="$(mktemp -d)"
trap 'rm -rf "${TMPA1:-}" "${TMPA2:-}" "${TMPA3:-}" "${TMPD1:-}"' EXIT
setup_repo "$TMPA1"
echo "change" >> "$TMPA1/README.md"
SLUG_A1="slug-binding-a1"
create_state "$TMPA1" "$SLUG_A1"
create_local_md "$TMPA1" "$SLUG_A1"

ERR_A1="$(mktemp)"
EC_A1="$(run_hook "$TMPA1" "$ERR_A1")"
assert "A1 hook EC=2（正常操作）" "[ '$EC_A1' -eq 2 ]"

# ============================================================
# A2: 无 local.md → exit 0
# ============================================================
echo ""
echo "=== A2: 无 local.md → exit 0 ==="
TMPA2="$(mktemp -d)"
setup_repo "$TMPA2"
echo "change" >> "$TMPA2/README.md"
SLUG_A2="slug-binding-a2"
create_state "$TMPA2" "$SLUG_A2"

ERR_A2="$(mktemp)"
EC_A2="$(run_hook "$TMPA2" "$ERR_A2")"
assert "A2 hook EC=0（放行）" "[ '$EC_A2' -eq 0 ]"

# ============================================================
# A3: local.md slug → 不存在的 state → exit 0
# ============================================================
echo ""
echo "=== A3: local.md → 不存在的 state → exit 0 ==="
TMPA3="$(mktemp -d)"
setup_repo "$TMPA3"
create_local_md "$TMPA3" "nonexistent-slug"

ERR_A3="$(mktemp)"
EC_A3="$(run_hook "$TMPA3" "$ERR_A3")"
assert "A3 hook EC=0（放行）" "[ '$EC_A3' -eq 0 ]"

# ============================================================
# D1: loop.yml + dirty + 无 local.md → exit 0（不兜底激活）
# ============================================================
echo ""
echo "=== D1: loop.yml + dirty + 无 local.md → 不兜底激活 ==="
TMPD1="$(mktemp -d)"
setup_repo "$TMPD1"
echo "dirty-change" >> "$TMPD1/README.md"

ERR_D1="$(mktemp)"
EC_D1="$(run_hook "$TMPD1" "$ERR_D1")"
assert "D1 hook EC=0（放行，不自动启动 loop）" "[ '$EC_D1' -eq 0 ]"
assert "D1 state 目录不存在或为空" "[ ! -d '$TMPD1/.claude/builder-loop/state' ] || [ -z \"\$(ls '$TMPD1/.claude/builder-loop/state/' 2>/dev/null)\" ]"

echo ""
echo "=== 总计 ==="
echo "  ✅ PASS: $PASS"
echo "  ❌ FAIL: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
