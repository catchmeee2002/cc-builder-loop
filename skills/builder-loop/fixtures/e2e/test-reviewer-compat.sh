#!/usr/bin/env bash
# test-reviewer-compat.sh — reviewer 模型兼容性 E2E
#
# 子测 A（lint，必跑）：校验 reviewer.md / builder.md / reviewer-fallback.md 的模型与 retry 文案一致
# 子测 B（live smoke，可选 --live）：在 CC CLI 可用时真 spawn 一次 reviewer，观察 REVIEW_SUMMARY
#
# 用法：
#   bash test-reviewer-compat.sh           # 只跑 lint
#   bash test-reviewer-compat.sh --live    # lint + live smoke（需 CC CLI）

set -uo pipefail

LIVE=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"; exit 0 ;;
    *)
      echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

REVIEWER_MD="${HOME}/.claude/agents/reviewer.md"
BUILDER_MD="${HOME}/.claude/commands/builder.md"
FALLBACK_MD="${REPO_ROOT}/skills/builder-loop/docs/reviewer-fallback.md"

FAILS=0
fail() { echo "❌ $*" >&2; FAILS=$((FAILS + 1)); }
ok()   { echo "✅ $*"; }

check_file_exists() {
  local path="$1" label="$2"
  if [ ! -f "$path" ]; then
    fail "$label 不存在：$path"
    return 1
  fi
  return 0
}

# ------ 子测 A：配置一致性 lint ------
echo "── 子测 A：配置一致性 lint ──"

if check_file_exists "$REVIEWER_MD" "reviewer.md"; then
  if grep -Eq '^model:[[:space:]]+sonnet[[:space:]]*$' "$REVIEWER_MD"; then
    ok "reviewer.md frontmatter: model=sonnet"
  else
    fail "reviewer.md frontmatter 缺少 'model: sonnet'（严格匹配 ^model: sonnet$）"
  fi
  if grep -Eq '^model:[[:space:]]+haiku[[:space:]]*$' "$REVIEWER_MD"; then
    fail "reviewer.md frontmatter 仍含残留 'model: haiku'"
  else
    ok "reviewer.md 无 haiku 残留"
  fi
fi

if check_file_exists "$BUILDER_MD" "builder.md"; then
  if grep -q "最多 2 次 haiku" "$BUILDER_MD"; then
    fail "builder.md 仍含旧文案 '最多 2 次 haiku'"
  else
    ok "builder.md 无旧 haiku retry 文案"
  fi
  if grep -q "sonnet" "$BUILDER_MD"; then
    ok "builder.md 含 sonnet"
  else
    fail "builder.md 缺少 sonnet 说明"
  fi
  if grep -q "错误分类" "$BUILDER_MD"; then
    ok "builder.md 含错误分类段"
  else
    fail "builder.md 缺少 '错误分类' 段"
  fi
fi

if check_file_exists "$FALLBACK_MD" "reviewer-fallback.md"; then
  if grep -q "haiku" "$FALLBACK_MD"; then
    fail "reviewer-fallback.md 含 haiku 残留（方案要求不含）"
  else
    ok "reviewer-fallback.md 无 haiku 残留"
  fi
fi

if [ "$FAILS" -eq 0 ]; then
  echo "✅ reviewer-compat lint PASS"
else
  echo "❌ reviewer-compat lint FAIL（$FAILS 项）" >&2
  exit 1
fi

# ------ 子测 B：live smoke（可选） ------
if [ "$LIVE" -eq 0 ]; then
  echo "⏭  live smoke skipped (无 --live)"
  exit 0
fi

echo "── 子测 B：live smoke ──"
if ! command -v claude >/dev/null 2>&1; then
  echo "⏭  live smoke skipped (CC CLI unavailable)"
  exit 0
fi

SMOKE_DIR="$(mktemp -d -t reviewer-smoke-XXXXXX)"
trap 'rm -rf "$SMOKE_DIR"' EXIT

cd "$SMOKE_DIR"
git init -q
echo "print('hello')" > hello.py
git add hello.py
git commit -q -m "init: [cr_id_skip] Add hello"

# 构造一个最小 changed_files + fake diff
DIFF_FILE="$SMOKE_DIR/diff.patch"
echo "+print('hello')" > "$DIFF_FILE"

REPORT_DIR="$SMOKE_DIR/.claude/review_reports"
mkdir -p "$REPORT_DIR"
REPORT_PATH="$REPORT_DIR/smoke.md"

PROMPT=$(cat <<'EOF'
你是代码审查 subagent，对以下改动做最小冒烟审查，然后按 reviewer.md 要求输出 REVIEW_SUMMARY。
changed_files: ["hello.py"]
diff_summary: "+print('hello')"
report_path: "/tmp/smoke.md"
EOF
)

OUT=$(echo "$PROMPT" | timeout 60 claude -p --no-tty 2>&1 || true)

if echo "$OUT" | grep -q "REVIEW_SUMMARY:"; then
  ok "live smoke: 收到 REVIEW_SUMMARY"
else
  echo "── live smoke 输出 ──" >&2
  echo "$OUT" | tail -20 >&2
  fail "live smoke: 未收到 REVIEW_SUMMARY"
fi

if [ "$FAILS" -eq 0 ]; then
  echo "✅ reviewer-compat E2E PASS"
  exit 0
else
  echo "❌ reviewer-compat E2E FAIL（$FAILS 项）" >&2
  exit 1
fi
