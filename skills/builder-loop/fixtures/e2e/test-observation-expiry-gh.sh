#!/usr/bin/env bash
# test-observation-expiry-gh.sh — check_observation_expiry() GitHub fallback
#
# Case A: mock gh returns expired observation issue → stderr 含 ⏰
# Case B: gh not available → stderr 含 ⚠️

source "$(dirname "${BASH_SOURCE[0]}")/harness.sh"
harness_init "observation-expiry-gh"

SETUP_SCRIPT="${HARNESS_REPO_ROOT}/skills/builder-loop/scripts/setup-builder-loop.sh"
assert_file_exists "setup-builder-loop.sh 存在" "$SETUP_SCRIPT"

mk_mock_gh() {
  local mock_dir="$1" response="$2"
  mkdir -p "$mock_dir/mock_bin"
  cat > "$mock_dir/mock_bin/gh" << GHEOF
#!/usr/bin/env bash
echo '$response'
GHEOF
  chmod +x "$mock_dir/mock_bin/gh"
}

mk_mock_gh_fail() {
  local mock_dir="$1"
  mkdir -p "$mock_dir/mock_bin"
  cat > "$mock_dir/mock_bin/gh" << 'GHEOF'
#!/usr/bin/env bash
exit 1
GHEOF
  chmod +x "$mock_dir/mock_bin/gh"
}

mk_bare_repo() {
  local d
  d=$(mktemp -d -t "harness-obs-gh-XXXXXX")
  _HARNESS_TMPDIRS+=("$d")
  (
    cd "$d"
    git init -q
    git config user.email "harness@test.local"
    git config user.name "harness"
    git remote add origin "https://github.com/testowner/testrepo.git"
    echo "hello" > README
    git add -A
    git -c core.hooksPath=/dev/null commit -q -m "chore(test): [cr_id_skip] Seed"
  )
  echo "$d"
}

extract_and_run_check() {
  local project_root="$1"
  local extra_path="$2"
  local wrapper
  wrapper=$(mktemp -t "harness-obs-wrapper-XXXXXX.sh")
  _HARNESS_TMPDIRS+=("$wrapper")
  cat > "$wrapper" << 'WRAPEOF'
#!/usr/bin/env bash
set -uo pipefail
PROJECT_ROOT="$1"
WRAPEOF
  # Extract the function from setup-builder-loop.sh
  sed -n '/^check_observation_expiry()/,/^}$/p' "$SETUP_SCRIPT" >> "$wrapper"
  echo 'check_observation_expiry' >> "$wrapper"

  if [ -n "$extra_path" ]; then
    PATH="$extra_path:$PATH" bash "$wrapper" "$project_root" 2>&1
  else
    PATH="/usr/bin:/bin" bash "$wrapper" "$project_root" 2>&1
  fi
}

# ---- Case A: expired observation issue → stderr 含 ⏰ ----
section "Case A: GitHub observation issue expired → stderr ⏰"
envA=$(mk_bare_repo)
mk_mock_gh "$envA" '[{"title":"2026-01-01 [观察期] old fix","body":"验证条件：截止日期：2026-01-15 前无复现","labels":[{"name":"observation"}]}]'
STDERR_A=$(extract_and_run_check "$envA" "$envA/mock_bin")
assert "Case A stderr 含 ⏰" "echo '$STDERR_A' | grep -q '⏰'"
assert "Case A stderr 含 old fix" "echo '$STDERR_A' | grep -q 'old fix'"

# ---- Case B: gh not available → stderr 含 ⚠️ ----
section "Case B: gh not available → stderr ⚠️"
envB=$(mk_bare_repo)
mk_mock_gh_fail "$envB"
STDERR_B=$(extract_and_run_check "$envB" "$envB/mock_bin")
assert "Case B stderr 含 ⚠️" "echo '$STDERR_B' | grep -q '⚠️'"

harness_report
