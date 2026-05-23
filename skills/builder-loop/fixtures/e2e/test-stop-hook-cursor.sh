#!/usr/bin/env bash
# test-stop-hook-cursor.sh — E2E：bootstrap 触发器 + HEAD 游标兼容性
#
# V2.2 行为变更：bootstrap 兜底**只看** HAS_DIFF，砍 HAS_RECENT_COMMIT 触发器。
# V3.2 行为变更：无 local.md 直接 exit 0，不再兜底激活。
#
# 覆盖场景：
#   Step 2: 首次 Stop（HEAD 刚 commit + 工作树干净）→ 静默 exit 0
#   Step 3: 同 HEAD 再次 Stop → 仍 exit 0
#   Step 4: 新 commit + 工作树干净 → 静默 exit 0
#   Step 5: HAS_DIFF 非空 + 无 local.md → exit 0（V3.2 不兜底）
#   Step 6: 游标损坏 + 无 local.md → 仍 exit 0
#   Step 7: 纯文档改动 → 静默放行
#   Step 8: mixed 改动（doc + code）→ 无 local.md 仍 exit 0
#   Step 9: docs/ 子目录改动 → 静默放行

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "stop-hook-cursor"

# --- 初始化仓库（不用 create_test_env：需步进式改动 + 无 local.md 场景）---
TMP="$(mktemp -d)"
_HARNESS_TMPDIRS+=("$TMP")
(
  cd "$TMP"
  git init -q
  git config user.email "e2e@test.local"
  git config user.name "e2e-test"
  mkdir -p .claude src tests
  cat > .claude/loop.yml <<'YMLEOF'
pass_cmd:
  - stage: smoke
    cmd: "true"
    timeout: 10
max_iterations: 3
layout:
  source_dirs: [src]
  test_dirs: [tests]
worktree:
  enabled: false
YMLEOF
  echo "seed" > README.md
  cat > .gitignore <<'GITIGNORE'
.claude/builder-loop/
.claude/loop-runs/
.claude/loop-trace.jsonl
.claude/reviewer-params.json
.claude/reviewer-diff.txt
GITIGNORE
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Initial seed for e2e cursor test"
)

CURSOR="$TMP/.claude/builder-loop/last_processed_head"
STATE_FILE="$TMP/.claude/builder-loop/state/__main__.yml"

section "Step 2: 首次 Stop（工作树干净）→ 静默"
r2=$(run_hook "$TMP")
assert_ec "第 1 次 exit 0" "$r2" 0
assert "第 1 次 stderr 不含兜底激活" "! grep -q '兜底激活' '$r2/stderr'"
assert_file_missing "游标未创建" "$CURSOR"
assert_file_missing "state 未创建" "$STATE_FILE"

section "Step 3: 同 HEAD 再次 Stop → 仍 exit 0"
r3=$(run_hook "$TMP")
assert_ec "第 2 次 exit 0" "$r3" 0
assert "第 2 次 stderr 不含兜底激活" "! grep -q '兜底激活' '$r3/stderr'"
assert_file_missing "第 2 次未创建 state" "$STATE_FILE"

section "Step 4: 新 commit + 工作树干净 → 仍 exit 0"
(cd "$TMP" && echo "more" > CHANGES.md && git add -A && git -c core.hooksPath=/dev/null commit -q -m "feat(test): [cr_id_skip] Second commit for cursor test")
r4=$(run_hook "$TMP")
assert_ec "第 3 次 exit 0" "$r4" 0
assert "第 3 次 stderr 不含兜底激活" "! grep -q '兜底激活' '$r4/stderr'"
assert_file_missing "游标仍未创建" "$CURSOR"

section "Step 5: 未提交非文档改动 + 无 local.md → exit 0"
(cd "$TMP" && echo "package main" > src/main.go && git add src/main.go)
r5=$(run_hook "$TMP")
assert_ec "第 4 次 exit 0（V3.2 无 local.md 不兜底）" "$r5" 0
assert "第 4 次 stderr 不含兜底激活" "! grep -q '兜底激活' '$r5/stderr'"
assert_file_missing "游标未创建" "$CURSOR"
assert_file_missing "state 未创建" "$STATE_FILE"

section "Step 6: 游标损坏 + 无 local.md → exit 0"
(cd "$TMP" && git reset -q HEAD src/main.go 2>/dev/null || true; rm -f "$TMP/src/main.go"; git -C "$TMP" checkout -q -- README.md 2>/dev/null || true)
mkdir -p "$(dirname "$CURSOR")" 2>/dev/null || true
echo "not-a-valid-sha" > "$CURSOR"
r6=$(run_hook "$TMP")
assert_ec "第 5 次 exit 0" "$r6" 0
assert "第 5 次 stderr 不含兜底激活" "! grep -q '兜底激活' '$r6/stderr'"

section "Step 7: 纯文档改动 → 静默放行"
(
  cd "$TMP"
  mkdir -p docs
  echo "# initial doc" > CLAUDE.md
  echo "# license" > LICENSE
  echo "# changelog" > docs/CHANGELOG.md
  echo "*.bak" >> .gitignore
  git add -A
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Seed for doc whitelist test"
  echo "## new section" >> CLAUDE.md
  echo "## v2" >> docs/CHANGELOG.md
  echo "MIT" >> LICENSE
  echo "*.tmp" >> .gitignore
)
r7=$(run_hook "$TMP")
assert_ec "第 6 次 exit 0（纯文档放行）" "$r7" 0
assert "第 6 次 stderr 不含兜底激活" "! grep -q '兜底激活' '$r7/stderr'"
assert_file_missing "第 6 次未创建 state" "$STATE_FILE"

section "Step 8: mixed 改动（doc + code）→ 无 local.md 仍 exit 0"
(cd "$TMP" && echo "real code" > src/main.py && git add src/main.py)
r8=$(run_hook "$TMP")
assert_ec "第 7 次 exit 0（V3.2 无 local.md 不兜底）" "$r8" 0
assert "第 7 次 stderr 不含兜底激活" "! grep -q '兜底激活' '$r8/stderr'"

section "Step 9: docs/ 子目录任意文件改动 → 静默放行"
(
  cd "$TMP"
  git reset -q --hard HEAD
  git rm -q --cached -r .claude/builder-loop/ 2>/dev/null || true
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Drop builder-loop runtime files from git" 2>/dev/null || true
  rm -rf .claude/builder-loop/
  git clean -qfd
  mkdir -p docs/diagrams
  echo "binary placeholder" > docs/diagrams/arch.svg
  git add docs/diagrams/arch.svg
  git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Add arch diagram"
  echo "updated svg" > docs/diagrams/arch.svg
)
r9=$(run_hook "$TMP")
assert_ec "第 8 次 exit 0（docs/ 路径放行 .svg）" "$r9" 0
assert "第 8 次 stderr 不含兜底激活" "! grep -q '兜底激活' '$r9/stderr'"

harness_report
