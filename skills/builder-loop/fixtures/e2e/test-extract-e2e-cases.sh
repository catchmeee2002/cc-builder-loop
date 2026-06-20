#!/usr/bin/env bash
# test-extract-e2e-cases.sh — fixture for scripts/extract-e2e-cases.sh
source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "test-extract-e2e-cases"

SCRIPT="${HARNESS_REPO_ROOT}/scripts/extract-e2e-cases.sh"
TMPDIR="$(mktemp -d -t "e2e-extract-XXXXXX")"
_HARNESS_TMPDIRS+=("$TMPDIR")

# Helper: run script, capture stdout to file, return exit code
run_extract() {
  local input="$1" outfile="$2"
  "$SCRIPT" "$input" > "$outfile" 2>/dev/null
  return $?
}

# ---- 1. Happy path: plan with e2e-cases tag ----
cat > "$TMPDIR/plan-with-cases.md" <<'EOF'
## 验收标准

<!-- e2e-cases -->
- 启动 app (python main.py --port 8080)
- 打开浏览器访问 localhost:8080
- 应能看到至少1个 rival 文明
<!-- /e2e-cases -->

其他内容
EOF

run_extract "$TMPDIR/plan-with-cases.md" "$TMPDIR/out1.txt"
assert "happy path exits 0" "[ $? -eq 0 ]"
assert_contains "extracts first case" "$TMPDIR/out1.txt" "启动 app"
assert_contains "extracts last case" "$TMPDIR/out1.txt" "rival"
assert "excludes opening tag" "! grep -q 'e2e-cases' '$TMPDIR/out1.txt'"

# ---- 2. No tags → exit 1 ----
cat > "$TMPDIR/plan-no-tag.md" <<'EOF'
## 验收标准
普通内容，没有 e2e 标签
EOF

run_extract "$TMPDIR/plan-no-tag.md" "$TMPDIR/out2.txt" || true
assert "no tags exits 1" "[ $? -ne 0 ] || [ ! -s '$TMPDIR/out2.txt' ]"

# ---- 3. Empty tags → exit 1 ----
cat > "$TMPDIR/plan-empty-tag.md" <<'EOF'
<!-- e2e-cases -->
<!-- /e2e-cases -->
EOF

run_extract "$TMPDIR/plan-empty-tag.md" "$TMPDIR/out3.txt" || true
assert "empty tags exits non-zero or empty output" "[ ! -s '$TMPDIR/out3.txt' ]"

# ---- 4. Multiple tag segments → all extracted ----
cat > "$TMPDIR/plan-multi.md" <<'EOF'
## Phase 1
<!-- e2e-cases -->
- 检查首页加载
<!-- /e2e-cases -->

## Phase 2
<!-- e2e-cases -->
- 检查 rival 面板
<!-- /e2e-cases -->
EOF

run_extract "$TMPDIR/plan-multi.md" "$TMPDIR/out4.txt"
assert "multi segments exits 0" "[ $? -eq 0 ]"
assert_contains "first segment present" "$TMPDIR/out4.txt" "首页加载"
assert_contains "second segment present" "$TMPDIR/out4.txt" "rival 面板"

# ---- 5. Missing file → exit 1 ----
"$SCRIPT" "$TMPDIR/nonexistent.md" > /dev/null 2>&1 || true
assert "missing file exits non-zero" "! '$SCRIPT' '$TMPDIR/nonexistent.md' > /dev/null 2>&1"

# ---- 6. No argument → exit 1 ----
assert "no argument exits non-zero" "! '$SCRIPT' > /dev/null 2>&1"

# ---- 7. Tag content preserves special chars ----
cat > "$TMPDIR/plan-special.md" <<'EOF'
<!-- e2e-cases -->
- 执行 curl http://localhost:8080/api/status
  - 应返回 JSON 包含 status ok
- 检查链接可访问
<!-- /e2e-cases -->
EOF

run_extract "$TMPDIR/plan-special.md" "$TMPDIR/out7.txt"
assert "special chars exits 0" "[ $? -eq 0 ]"
assert_contains "preserves curl" "$TMPDIR/out7.txt" "curl"
assert_contains "preserves nested indent" "$TMPDIR/out7.txt" "应返回"

harness_report
