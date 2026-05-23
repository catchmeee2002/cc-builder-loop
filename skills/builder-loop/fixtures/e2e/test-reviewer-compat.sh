#!/usr/bin/env bash
# test-reviewer-compat.sh — reviewer 模型兼容性 E2E
#
# 子测 A（lint，必跑）：校验 reviewer.md / builder.md / reviewer-fallback.md 的模型与 retry 文案一致
# 子测 B（live smoke，可选 --live）：在 CC CLI 可用时真 spawn 一次 reviewer，观察 REVIEW_SUMMARY
#
# 用法：
#   bash test-reviewer-compat.sh           # 只跑 lint
#   bash test-reviewer-compat.sh --live    # lint + live smoke（需 CC CLI）

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "reviewer-compat"

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

REVIEWER_MD="${HOME}/.claude/agents/reviewer.md"
BUILDER_MD="${HOME}/.claude/commands/builder.md"
FALLBACK_MD="${HARNESS_REPO_ROOT}/skills/builder-loop/docs/reviewer-fallback.md"

# ------ 子测 A：配置一致性 lint ------
section "子测 A：配置一致性 lint"

if [ -f "$REVIEWER_MD" ]; then
  assert "reviewer.md frontmatter: model=sonnet" \
    "grep -Eq '^model:[[:space:]]+sonnet[[:space:]]*$' '$REVIEWER_MD'"
  assert "reviewer.md 无 haiku 残留" \
    "! grep -Eq '^model:[[:space:]]+haiku[[:space:]]*$' '$REVIEWER_MD'"
else
  assert "reviewer.md 存在" "[ -f '$REVIEWER_MD' ]"
fi

if [ -f "$BUILDER_MD" ]; then
  assert "builder.md 无旧 haiku retry 文案" \
    "! grep -q '最多 2 次 haiku' '$BUILDER_MD'"
  assert "builder.md 含 sonnet" \
    "grep -q 'sonnet' '$BUILDER_MD'"
  assert "builder.md 含错误分类段" \
    "grep -q '错误分类' '$BUILDER_MD'"
else
  assert "builder.md 存在" "[ -f '$BUILDER_MD' ]"
fi

if [ -f "$FALLBACK_MD" ]; then
  assert "reviewer-fallback.md 无 haiku 残留" \
    "! grep -q 'haiku' '$FALLBACK_MD'"
else
  assert "reviewer-fallback.md 存在" "[ -f '$FALLBACK_MD' ]"
fi

# ------ 子测 B：live smoke（可选） ------
if [ "$LIVE" -eq 0 ]; then
  echo "  live smoke skipped (无 --live)"
  harness_report
fi

section "子测 B：live smoke"
if ! command -v claude >/dev/null 2>&1; then
  echo "  live smoke skipped (CC CLI unavailable)"
  harness_report
fi

SMOKE_DIR="$(mktemp -d -t reviewer-smoke-XXXXXX)"
_HARNESS_TMPDIRS+=("$SMOKE_DIR")

cd "$SMOKE_DIR"
git init -q
echo "print('hello')" > hello.py
git add hello.py
git commit -q -m "init: [cr_id_skip] Add hello"

DIFF_FILE="$SMOKE_DIR/diff.patch"
echo "+print('hello')" > "$DIFF_FILE"

REPORT_DIR="$SMOKE_DIR/.claude/review_reports"
mkdir -p "$REPORT_DIR"

PROMPT=$(cat <<'EOF'
你是代码审查 subagent，对以下改动做最小冒烟审查，然后按 reviewer.md 要求输出 REVIEW_SUMMARY。
changed_files: ["hello.py"]
diff_summary: "+print('hello')"
report_path: "/tmp/smoke.md"
EOF
)

OUT=$(echo "$PROMPT" | timeout 60 claude -p --no-tty 2>&1 || true)
assert "live smoke: 收到 REVIEW_SUMMARY" "echo '$OUT' | grep -q 'REVIEW_SUMMARY:'"

harness_report
